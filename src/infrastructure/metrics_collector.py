"""
Metrics Collector — aggregates per-task and per-step execution metrics.

Tracks:
  • Task-level outcomes (success / failure counts)
  • Per-step planner confidence scores
  • Replanning events (step marked FAILED and retried)
  • User interrupt events
  • Per-step iteration counts
  • Failed-approach reuse (whether a previously-failed tool was retried)

Thread safety
─────────────
  A threading.Lock protects all writes, matching ExecutionRecorder's pattern
  so that parallel agents can safely call record_step_result concurrently.

Usage
─────
  mc = MetricsCollector()
  mc.record_task_start(plan_id, goal)
  mc.record_step_result(plan_id, step_index, confidence, iterations,
                        tool_names_tried, success)
  mc.record_replan(plan_id)
  mc.record_interrupt(plan_id)
  mc.record_task_end(plan_id, success=True)
  metrics = mc.get_metrics()
  print(metrics.to_json())
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from ..models.token_usage import TokenUsage


# ── TaskMetrics dataclass ─────────────────────────────────────────────────────

@dataclass
class TaskMetrics:
    """
    Aggregated metrics across all recorded tasks.

    Primary fields
    --------------
    step_confidence_avg : float
        Mean of all per-step planner confidence scores across all tasks.
        0.0 when no confidence scores have been recorded.
    replan_count : int
        Total number of replanning events across all tasks.
        A replan is recorded each time record_replan() is called (i.e. the
        planner decided to re-plan mid-task).
    interrupt_count : int
        Total number of user interrupt events across all tasks.
    avg_iterations_per_step : float
        Mean number of Think/Act iterations per step across all tasks.
        0.0 when no steps have been recorded.
    failed_approach_reuse_rate : float
        Fraction of steps (after the first failure in a task) where the agent
        AVOIDED reusing a previously-failed tool.
        = avoided_reuse_steps / steps_after_first_failure
        1.0 means the agent always avoided retrying failed tools.
        0.0 means it always retried them (or no steps after a failure exist).

    Duration fields
    ---------------
    total_duration_seconds : float
        Sum of all per-task durations in seconds.
        0.0 when no tasks have recorded a duration.
    avg_duration_seconds : float
        Mean task duration in seconds across all tasks that have a duration.
        0.0 when no tasks have recorded a duration.
    min_duration_seconds : float
        Shortest task duration in seconds.
        0.0 when no tasks have recorded a duration.
    max_duration_seconds : float
        Longest task duration in seconds.
        0.0 when no tasks have recorded a duration.

    Helper / diagnostic fields
    --------------------------
    task_count_succeeded : int
    task_count_failed    : int
    total_steps          : int
    total_iterations     : int
    """

    # ── Primary metrics ───────────────────────────────────────────────────────
    step_confidence_avg: float = 0.0
    replan_count: int = 0
    interrupt_count: int = 0
    avg_iterations_per_step: float = 0.0
    failed_approach_reuse_rate: float = 0.0

    # ── Duration fields ───────────────────────────────────────────────────────
    total_duration_seconds: float = 0.0
    avg_duration_seconds: float = 0.0
    min_duration_seconds: float = 0.0
    max_duration_seconds: float = 0.0

    # ── Goal / replan context fields ──────────────────────────────────────────
    goals: List[str] = field(default_factory=list)
    replan_trigger_messages: List[str] = field(default_factory=list)

    # ── Helper / diagnostic fields ────────────────────────────────────────────
    task_count_succeeded: int = 0
    task_count_failed: int = 0
    total_steps: int = 0
    total_iterations: int = 0
    # Token usage totals across all tasks
    token_usage: TokenUsage = field(default_factory=TokenUsage)

    # ── Backward-compat property accessors ───────────────────────────────────

    @property
    def total_input_tokens(self) -> int:
        return self.token_usage.input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.token_usage.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.token_usage.total_tokens

    @property
    def total_cache_creation_tokens(self) -> int:
        return self.token_usage.cache_creation_tokens

    @property
    def total_cache_read_tokens(self) -> int:
        return self.token_usage.cache_read_tokens

    # ── Serialisation helpers ─────────────────────────────────────────────────

    def to_dict(self) -> Dict:
        """Return a plain dict representation of all fields.

        token_usage is flattened into the top level so that the serialised
        shape of metrics_summary.json remains backward-compatible.
        """
        d = asdict(self)
        tu = d.pop("token_usage", {})
        d["total_input_tokens"] = tu.get("input_tokens", 0)
        d["total_output_tokens"] = tu.get("output_tokens", 0)
        d["total_tokens"] = tu.get("input_tokens", 0) + tu.get("output_tokens", 0)
        d["total_cache_creation_tokens"] = tu.get("cache_creation_tokens", 0)
        d["total_cache_read_tokens"] = tu.get("cache_read_tokens", 0)
        return d

    def to_json(self, indent: int = 2) -> str:
        """Return a JSON string representation of all fields."""
        return json.dumps(self.to_dict(), indent=indent)


# ── Per-task raw data ─────────────────────────────────────────────────────────

@dataclass
class _TaskData:
    """
    Raw per-task data stored internally by MetricsCollector.

    Storing raw data (rather than pre-aggregated counters) allows the metrics
    to be recomputed at any time without losing precision.
    """
    plan_id: str
    goal: str
    # Per-step records: list of dicts with keys:
    #   step_index, confidence, iterations, tool_names_tried, success,
    #   input_tokens, output_tokens
    steps: List[Dict] = field(default_factory=list)
    replan_count: int = 0
    interrupt_count: int = 0
    # User messages that triggered a replan (empty string when confidence-triggered)
    replan_trigger_messages: List[str] = field(default_factory=list)
    # None = task still running; True/False = task ended
    ended_success: Optional[bool] = None
    # None = duration not recorded; float = elapsed seconds for the task
    duration_seconds: Optional[float] = None


# ── MetricsCollector ──────────────────────────────────────────────────────────

class MetricsCollector:
    """
    Collects and aggregates execution metrics across multiple tasks.

    All public methods are thread-safe via an internal threading.Lock,
    matching the pattern used by ExecutionRecorder.

    Typical call sequence per task
    ──────────────────────────────
      record_task_start(plan_id, goal)
      for each step:
          record_step_result(plan_id, step_index, confidence,
                             iterations, tool_names_tried, success)
          if replanning occurred:
              record_replan(plan_id)
          if user interrupted:
              record_interrupt(plan_id)
      record_task_end(plan_id, success)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keyed by plan_id; preserves insertion order (Python 3.7+).
        self._tasks: Dict[str, _TaskData] = {}

    # ── Write methods ─────────────────────────────────────────────────────────

    def record_task_start(self, plan_id: str, goal: str) -> None:
        """
        Register the start of a new task.

        Safe to call multiple times with the same plan_id — subsequent calls
        are ignored so that a restart does not corrupt existing data.

        Args:
            plan_id: Unique task identifier (matches ExecutionRecorder's plan_id).
            goal:    Human-readable task goal.
        """
        with self._lock:
            if plan_id not in self._tasks:
                self._tasks[plan_id] = _TaskData(plan_id=plan_id, goal=goal)

    def record_step_result(
        self,
        plan_id: str,
        step_index: int,
        confidence: float,
        iterations: int,
        tool_names_tried: List[str],
        success: bool,
        token_usage: Optional[TokenUsage] = None,
        # Backward-compat individual kwargs
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """
        Record the result of a single step execution.

        Failed-approach reuse logic
        ───────────────────────────
        tool_names_tried is the ordered list of tool names attempted during
        this step (one entry per Think/Act iteration).  Within the context of
        the current task, if a tool name appears in this list AND that tool
        has previously failed in an earlier step of the same task, the step
        counts as a "reuse" of a failed approach.  If the step avoids all
        previously-failed tools it counts as "avoided".

        failed_approach_reuse_rate = avoided_steps / steps_eligible
        where steps_eligible = steps that ran after at least one tool failure
        existed in the task history.

        Args:
            plan_id:          Task identifier (must have been started first).
            step_index:       0-based index of the step within the task.
            confidence:       Planner confidence score for this step (0.0–1.0).
            iterations:       Number of Think/Act iterations the agent used.
            tool_names_tried: Ordered list of tool names attempted in this step.
            success:          Whether the step was marked as successful.
        """
        if token_usage is None:
            token_usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        with self._lock:
            if plan_id not in self._tasks:
                # Auto-create if record_task_start was not called (defensive).
                self._tasks[plan_id] = _TaskData(plan_id=plan_id, goal="")
            task = self._tasks[plan_id]
            task.steps.append({
                "step_index": step_index,
                "confidence": confidence,
                "iterations": iterations,
                "tool_names_tried": list(tool_names_tried),
                "success": success,
                "token_usage": token_usage,
            })

    def record_replan(self, plan_id: str, trigger_message: str = '') -> None:
        """
        Record that a replanning event occurred for the given task.

        Call this whenever the planner decides to re-plan mid-task (e.g. after
        a confidence failure causes a step to be marked FAILED and the planner
        generates corrective next_steps).

        Args:
            plan_id:         Task identifier.
            trigger_message: The user message text that triggered this replan,
                             if any.  Pass an empty string (default) when the
                             replan was triggered by a planner-internal event
                             (e.g. confidence failure) rather than a user message.
        """
        with self._lock:
            if plan_id not in self._tasks:
                self._tasks[plan_id] = _TaskData(plan_id=plan_id, goal="")
            self._tasks[plan_id].replan_count += 1
            self._tasks[plan_id].replan_trigger_messages.append(trigger_message)

    def record_interrupt(self, plan_id: str) -> None:
        """
        Record that a user interrupt event occurred for the given task.

        Args:
            plan_id: Task identifier.
        """
        with self._lock:
            if plan_id not in self._tasks:
                self._tasks[plan_id] = _TaskData(plan_id=plan_id, goal="")
            self._tasks[plan_id].interrupt_count += 1

    def record_task_end(
        self,
        plan_id: str,
        success: bool,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """
        Mark a task as ended with the given success flag.

        Args:
            plan_id:          Task identifier.
            success:          True if the task completed successfully.
            duration_seconds: Elapsed wall-clock time for the task in seconds.
                              Pass None (default) when duration is unavailable.
        """
        with self._lock:
            if plan_id not in self._tasks:
                self._tasks[plan_id] = _TaskData(plan_id=plan_id, goal="")
            self._tasks[plan_id].ended_success = success
            if duration_seconds is not None:
                self._tasks[plan_id].duration_seconds = duration_seconds

    # ── Aggregation ───────────────────────────────────────────────────────────

    def get_metrics(self) -> TaskMetrics:
        """
        Aggregate all recorded data and return a TaskMetrics instance.

        This method is non-destructive — it can be called at any time and
        will include data from tasks that are still in progress.

        Returns:
            A TaskMetrics dataclass with all fields populated.
        """
        with self._lock:
            tasks = list(self._tasks.values())

        task_count_succeeded = sum(
            1 for t in tasks if t.ended_success is True
        )
        task_count_failed = sum(
            1 for t in tasks if t.ended_success is False
        )

        # Flatten all steps across all tasks
        all_steps = [step for t in tasks for step in t.steps]
        total_steps = len(all_steps)

        # Confidence average
        confidences = [s["confidence"] for s in all_steps]
        step_confidence_avg = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )

        # Iteration totals
        total_iterations = sum(s["iterations"] for s in all_steps)
        avg_iterations_per_step = (
            total_iterations / total_steps if total_steps > 0 else 0.0
        )

        # Replan / interrupt totals
        replan_count = sum(t.replan_count for t in tasks)
        interrupt_count = sum(t.interrupt_count for t in tasks)

        # Goals: one per task (in insertion order)
        goals = [t.goal for t in tasks]

        # Replan trigger messages: flat list across all tasks (in order)
        replan_trigger_messages = [
            msg for t in tasks for msg in t.replan_trigger_messages
        ]

        # Failed-approach reuse rate (computed per-task, then aggregated)
        total_eligible_steps = 0   # steps that ran after ≥1 tool failure in task
        total_avoided_steps = 0    # eligible steps that did NOT reuse a failed tool

        for task in tasks:
            failed_tools: set = set()   # tools that have failed in this task so far
            for step in task.steps:
                tools_tried: List[str] = step["tool_names_tried"]
                step_success: bool = step["success"]

                if failed_tools:
                    # This step is "eligible": at least one tool has failed before it.
                    total_eligible_steps += 1
                    reused = any(t in failed_tools for t in tools_tried)
                    if not reused:
                        total_avoided_steps += 1

                # Update failed_tools: add any tool that failed in this step.
                # A tool "failed" in a step if the step itself was not successful.
                # We attribute the failure to all tools tried in a failed step,
                # since we cannot pinpoint which iteration caused the failure.
                if not step_success:
                    for tool_name in tools_tried:
                        failed_tools.add(tool_name)

        failed_approach_reuse_rate = (
            total_avoided_steps / total_eligible_steps
            if total_eligible_steps > 0
            else 0.0
        )

        # Token totals across all tasks
        total_token_usage = TokenUsage()
        for s in all_steps:
            tu = s.get("token_usage")
            if isinstance(tu, TokenUsage):
                total_token_usage += tu

        # Duration statistics across tasks that have a recorded duration
        durations = [
            t.duration_seconds for t in tasks if t.duration_seconds is not None
        ]
        total_duration_seconds = sum(durations)
        avg_duration_seconds = (
            total_duration_seconds / len(durations) if durations else 0.0
        )
        min_duration_seconds = min(durations) if durations else 0.0
        max_duration_seconds = max(durations) if durations else 0.0

        return TaskMetrics(
            step_confidence_avg=step_confidence_avg,
            replan_count=replan_count,
            interrupt_count=interrupt_count,
            avg_iterations_per_step=avg_iterations_per_step,
            failed_approach_reuse_rate=failed_approach_reuse_rate,
            total_duration_seconds=total_duration_seconds,
            avg_duration_seconds=avg_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            goals=goals,
            replan_trigger_messages=replan_trigger_messages,
            task_count_succeeded=task_count_succeeded,
            task_count_failed=task_count_failed,
            total_steps=total_steps,
            total_iterations=total_iterations,
            token_usage=total_token_usage,
        )

    def get_task_summary(self, plan_id: str) -> dict:
        """
        Return per-task metrics for a single task.

        Args:
            plan_id: Task identifier.

        Returns:
            A dict with keys: plan_id, goal, step_count, replan_count,
            interrupt_count, ended_success, avg_confidence,
            avg_iterations_per_step, failed_approach_reuse_rate.
            Returns an empty dict if plan_id is not known.
        """
        with self._lock:
            task = self._tasks.get(plan_id)

        if task is None:
            return {}

        steps = task.steps
        step_count = len(steps)

        confidences = [s["confidence"] for s in steps]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        total_iters = sum(s["iterations"] for s in steps)
        avg_iters = total_iters / step_count if step_count > 0 else 0.0

        # Per-task failed-approach reuse rate
        failed_tools: set = set()
        eligible = 0
        avoided = 0
        for step in steps:
            tools_tried: List[str] = step["tool_names_tried"]
            step_success: bool = step["success"]
            if failed_tools:
                eligible += 1
                if not any(t in failed_tools for t in tools_tried):
                    avoided += 1
            if not step_success:
                for tool_name in tools_tried:
                    failed_tools.add(tool_name)
        reuse_rate = avoided / eligible if eligible > 0 else 0.0

        return {
            "plan_id": task.plan_id,
            "goal": task.goal,
            "step_count": step_count,
            "replan_count": task.replan_count,
            "interrupt_count": task.interrupt_count,
            "replan_trigger_messages": list(task.replan_trigger_messages),
            "ended_success": task.ended_success,
            "avg_confidence": avg_confidence,
            "avg_iterations_per_step": avg_iters,
            "failed_approach_reuse_rate": reuse_rate,
        }

    # ── Reset ─────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all recorded data. Useful between test runs or sessions."""
        with self._lock:
            self._tasks.clear()
