"""
Plan and Step - Core data models for task planning and execution.

Step is the unified lifecycle object: it starts as a planner output,
becomes the execution unit, and ends as the memory record.
Use factory methods to create steps in different contexts.
"""
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional, Union
from ..infrastructure.utils import try_parse_json, llm_extract_json
from ..tools.base_tool import ToolResult


class StepStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class Step:
    """
    Unified step model covering the full lifecycle:
      planning output → execution unit → memory record.

    Lifecycle phases and their fields:
      Planning   : step_id, description, goal, step_supplement,
                   parallel_group, is_aggregation, planner_reasoning
      Execution  : status, started_at, completed_at
      Record     : observations, final_reasoning,
                   verified, confidence, issues
    """

    # ── Core identity (always present) ──────────────────────────────────────
    step_id: str
    description: str
    goal: str

    # ── Planning phase ───────────────────────────────────────────────────────
    step_supplement: str = ""         # Extra input appended to goal; data slice for parallel steps, artifacts summary for verification steps
    parallel_group: str = ""         # Non-empty = part of a parallel batch; same value for all steps in the batch
    is_aggregation: bool = False     # True = aggregation step that follows a parallel batch
    planner_reasoning: str = ""      # Why Planner decided to execute this step (set on steps[0])
    expected_outcomes: List[str] = field(default_factory=list)  # Planner's per-dimension expectations of success (reference only — agent may deviate and must explain)
    risk_assessment: str = ""        # Planner's assessment of what could go wrong and the fallback strategy
    required_context_keys: List[str] = field(default_factory=list)  # step_id values of prior steps whose findings this step needs; empty = full isolation
    ssh_target: str = ""             # "user@hostname" — Planner fills when step requires SSH remote work; empty for local steps

    # ── Execution state ──────────────────────────────────────────────────────
    status: StepStatus = StepStatus.PENDING

    # ── Execution record ─────────────────────────────────────────────────────
    observations: List[ToolResult] = field(default_factory=list)
    agent_runtime_reasoning: str = ""        # Raw agent reasoning (kept for debugging / planner context)
    issues: List[str] = field(default_factory=list)

    # ── Structured completion info (from agent's final Decision) ──────────────
    # Captured from Decision.factual_outcome / .artifacts / .key_findings when
    # and aggregation steps can see what was produced without relying on
    # truncated tool-output snippets.
    factual_outcome: List[str] = field(default_factory=list)  # Factual statements of what was accomplished
    artifacts: List[str] = field(default_factory=list)    # Files / resources created or modified
    key_findings: List[str] = field(default_factory=list) # Important discoveries from this step

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # ── Factory methods ──────────────────────────────────────────────────────

    @classmethod
    def from_planner(cls, step_id: str, description: str, goal: str,
                     step_supplement: str = "", parallel_group: str = "",
                     is_aggregation: bool = False,
                     planner_reasoning: str = "",
                     expected_outcomes: Optional[List[str]] = None,
                     risk_assessment: str = "",
                     required_context_keys: Optional[List[str]] = None,
                     ssh_target: str = "") -> "Step":
        """Create a step from planner output."""
        return cls(
            step_id=step_id,
            description=description,
            goal=goal,
            step_supplement=step_supplement,
            parallel_group=parallel_group,
            is_aggregation=is_aggregation,
            planner_reasoning=planner_reasoning,
            expected_outcomes=expected_outcomes or [],
            risk_assessment=risk_assessment,
            required_context_keys=required_context_keys or [],
            ssh_target=ssh_target,
        )

    @classmethod
    def for_aggregation(cls, step_id: str, goal: str) -> "Step":
        """Create a fallback aggregation step (when planner omits one)."""
        return cls(
            step_id=step_id,
            description="Aggregate sub-task results",
            goal=goal,
            is_aggregation=True,
        )

    # ── Status management ────────────────────────────────────────────────────

    def update_status(self, status: StepStatus) -> None:
        """Update step status and set the corresponding timestamp."""
        self.status = status
        if status == StepStatus.IN_PROGRESS:
            self.started_at = datetime.now()
        elif status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
            self.completed_at = datetime.now()

    def has_status(self, *statuses: StepStatus) -> bool:
        return self.status in statuses

    # ── Observation management ────────────────────────────────────────────────

    def add_observation(self, obs: ToolResult) -> None:
        self.observations.append(obs)

    def clear_observations(self) -> None:
        """Remove all stored observations (used by compaction logic)."""
        self.observations.clear()

    def get_all_observations(self) -> List[ToolResult]:
        return self.observations.copy()

    def get_observation_count(self) -> int:
        return len(self.observations)

    # ── Planner summary ───────────────────────────────────────────────────────

    def to_planner_summary(self, compact: bool = False) -> str:
        """Structured summary for Planner consumption.

        Two detail levels:

        compact=False  (default — used for the most recent step and recent
                        failed steps):
          Full evidence so the Planner can set last_step_confidence accurately
          and diagnose failure patterns.
          Successful: goal + agent reasoning (300 chars) + last 3 tool outputs
                      (400 chars each).
          Failed:     goal + issues + agent reasoning (200 chars) + last 3
                      failed tool calls (300 chars each).

        compact=True  (used for recent successful steps that are NOT the most
                       recent — they are already confirmed done and the Planner
                       only needs to know what was accomplished):
          Successful: goal + agent reasoning (100 chars) + last 1 tool output
                      (150 chars).  Saves ~1,600 chars per step vs. full mode.
          Failed:     same as full mode — failure detail is always preserved
                      so the Planner can detect repeated failure patterns.

        The agent's factual report (factual_outcome, key_findings, artifacts) is the
        primary information for the planner to assess goal achievement.
        agent_runtime_reasoning is the agent's internal thought process and is
        shown as supplementary context.
        """
        if self.status == StepStatus.COMPLETED:
            text = f"[Done] {self.goal}"

            # ── Structured completion info — always included, never truncated ──
            # These fields are captured from Decision.factual_outcome / .artifacts /
            # .key_findings and are the primary cross-step information channel:
            # artifacts tells subsequent steps what files were created;
            # key_findings summarises what was discovered.
            if self.factual_outcome:
                text += f"\n  Factual outcome: {'; '.join(self.factual_outcome)}"
            if self.artifacts:
                text += f"\n  Artifacts: {', '.join(self.artifacts)}"
            if self.key_findings:
                text += f"\n  Key findings: {'; '.join(self.key_findings)}"

            if compact:
                # Compact: just enough to confirm the step was done.
                if self.agent_runtime_reasoning:
                    text += f"\n  Agent notes: {self.agent_runtime_reasoning}"
                for op in self.observations[-1:]:
                    status = "✓" if op.success else "✗"
                    output_snippet = str(op.output or op.error or "")[:600]
                    text += f"\n  {status} {op.tool_name}: {output_snippet}"
            else:
                # Full: expose enough context for last_step_confidence assessment.
                # expected_outcomes shows what the planner predicted — the planner
                # can compare this against outcome/key_findings when scoring confidence.
                if self.expected_outcomes:
                    text += f"\n  Expected outcomes: {'; '.join(self.expected_outcomes)}"
                if self.agent_runtime_reasoning:
                    text += f"\n  Agent notes: {self.agent_runtime_reasoning}"
                for op in self.observations[-3:]:
                    status = "✓" if op.success else "✗"
                    output_snippet = str(op.output or op.error or "")[:1600]
                    text += f"\n  {status} {op.tool_name}: {output_snippet}"
            return text

        # Failed step — always full detail regardless of compact flag.
        # The Planner must understand WHY a step failed to avoid repeating it.
        text = f"[Failed] {self.goal}"
        if self.issues:
            text += f"\n  Issues: {'; '.join(self.issues)}"
        if self.agent_runtime_reasoning:
            text += f"\n  Agent notes: {self.agent_runtime_reasoning}"
        # Show last 3 failed tool calls with enough detail to diagnose the failure.
        for op in [o for o in self.observations if not o.success][-3:]:
            text += f"\n  ✗ {op.tool_name}: {str(op.error or op.output)[:1600]}"
        return text

    def to_dict(self) -> dict:
        """Serialize to dict (excludes raw observations for brevity)."""
        return {
            "step_id": self.step_id,
            "description": self.description,
            "goal": self.goal,
            "step_supplement": self.step_supplement,
            "parallel_group": self.parallel_group,
            "is_aggregation": self.is_aggregation,
            "status": self.status.value,
            "issues": self.issues,
            "agent_runtime_reasoning": self.agent_runtime_reasoning,
            "factual_outcome": self.factual_outcome,
            "artifacts": self.artifacts,
            "key_findings": self.key_findings,
            "expected_outcomes": self.expected_outcomes,
            "risk_assessment": self.risk_assessment,
            "required_context_keys": self.required_context_keys,
            "ssh_target": self.ssh_target,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class Plan:
    """
    Current planning view — returned by Planner.observe_and_plan() on every call.

    This is a transient object, not a persistent record.
    Execution history lives in Memory.completed_steps (List[Step]).

      - next_steps empty and last_step_confidence >= threshold → task is done.
      - next_steps non-empty → flat list where the first batch
                                (steps sharing the same parallel_group, or
                                just next_steps[0]) is executed immediately;
                                the rest are the lookahead.
      - next_steps[0].planner_reasoning carries the decision rationale.
      - last_step_confidence: Planner's confidence (0.0–1.0) that the most
        recently completed step truly achieved its goal, based on actual tool
        outputs.  None = no completed steps yet (first iteration).
        FlowController compares this against STEP_VERIFICATION_THRESHOLD; if
        below threshold the last step is marked FAILED in memory before the
        corrective step (next_steps[0]) is executed.
      - interrupt_current_step: When True, FlowController immediately aborts
        the currently executing agent step before applying the new lookahead.
        When False (default), the current step is allowed to finish naturally;
        the new lookahead takes effect on the next step boundary.
    """
    goal: str
    next_steps: List[Step] = field(default_factory=list)
    last_step_confidence: Optional[float] = None  # None = no completed steps yet
    interrupt_current_step: bool = False           # True = abort running step now
    # Required when next_steps is empty: one-sentence explanation of why the task
    # ended — either what was accomplished (success) or why it is infeasible.
    completion_reason: Optional[str] = None
    # LLM's free-text explanation for the last_step_confidence score.
    # Empty string if the LLM did not output it (backward-compatible default).
    confidence_rationale: str = ''
    # Token count from the LLM call that produced this plan.
    # Set by Planner.observe_and_plan() after the chat() call returns.
    token_count: int = 0

    # ── Planner response parsing ──────────────────────────────────────────────

    @classmethod
    def from_planner_dict(cls, goal: str, data: dict) -> "Plan":
        """Parse a planner LLM response dict into a Plan."""
        next_steps = [
            Step.from_planner(
                step_id=s.get("step_id", str(_uuid.uuid4())),
                description=s.get("description", ""),
                goal=s.get("goal", ""),
                step_supplement=s.get("step_supplement", s.get("input_context", "")),
                parallel_group=s.get("parallel_group", ""),
                is_aggregation=s.get("is_aggregation", False),
                planner_reasoning=s.get("planner_reasoning", ""),
                expected_outcomes=s.get("expected_outcomes", []),
                risk_assessment=s.get("risk_assessment", ""),
                required_context_keys=s.get("required_context_keys", []),
                ssh_target=s.get("ssh_target", ""),
            )
            for s in data.get("next_steps", [])
        ]
        raw = data.get("last_step_confidence", None)
        last_step_confidence = float(raw) if raw is not None else None
        plan = cls(
            goal=goal,
            next_steps=next_steps,
            last_step_confidence=last_step_confidence,
            interrupt_current_step=bool(data.get("interrupt_current_step", False)),
            completion_reason=data.get("completion_reason") or None,
            confidence_rationale=data.get("confidence_rationale") or '',
        )
        plan._validate()
        return plan

    def _validate(self) -> None:
        """Raise ValueError if the plan violates structural invariants.

        Checks (in order):
          1. Each step goal is substantive (> 20 non-whitespace chars).
          2. Each step has at least one non-empty expected_outcome.
          3. Every parallel_group has exactly one is_aggregation=True step.
          4. completion_reason is present when next_steps is empty.
          5. last_step_confidence is within [0.0, 1.0] when present.

        These are structural rules that can be expressed as code — they do not
        belong in the prompt.  Raising here causes Plan.from_data() to fall
        through to the LLM-extraction fallback, which re-parses the raw content
        and typically produces a corrected plan.
        """
        for step in self.next_steps:
            if len(step.goal.strip()) <= 20:
                raise ValueError(
                    f"Step '{step.step_id}' goal is too vague "
                    f"(≤20 chars): {step.goal!r}. "
                    f"Goals must be precise and verifiable."
                )
            substantive = [o for o in step.expected_outcomes if o and o.strip()]
            if not substantive:
                raise ValueError(
                    f"Step '{step.step_id}' has no substantive expected_outcomes "
                    f"(empty list or all-blank strings). "
                    f"Each step must list at least one observable success criterion."
                )

        # parallel_group integrity: every non-empty group must have exactly one aggregation step
        groups: dict[str, list[Step]] = {}
        for step in self.next_steps:
            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(step)
        for group_id, steps in groups.items():
            agg_count = sum(1 for s in steps if s.is_aggregation)
            if agg_count == 0:
                raise ValueError(
                    f"parallel_group '{group_id}' has no aggregation step "
                    f"(is_aggregation=True). "
                    f"Every parallel group must have exactly one aggregation step."
                )
            if agg_count > 1:
                raise ValueError(
                    f"parallel_group '{group_id}' has {agg_count} aggregation steps; "
                    f"exactly one is_aggregation=True step is required per group."
                )

        # completion_reason required when signalling task done
        if not self.next_steps and not self.completion_reason:
            raise ValueError(
                "next_steps is empty (task completion signal) but completion_reason "
                "is missing. Provide a completion_reason summarising what was accomplished."
            )

        # last_step_confidence must be within [0.0, 1.0]
        if self.last_step_confidence is not None:
            if not (0.0 <= self.last_step_confidence <= 1.0):
                raise ValueError(
                    f"last_step_confidence={self.last_step_confidence!r} is outside [0.0, 1.0]. "
                    f"Provide a value between 0.0 and 1.0."
                )

    @classmethod
    async def from_data(
        cls,
        goal: str,
        raw_content: Any,
        llm_services: Any = None,
    ) -> "Plan":
        """
        Parse an LLM response into a Plan.

        Flow:
        1. try_parse_json(raw_content)
           - Returns dict with all expected keys  -> use it directly
           - Returns dict missing expected keys   -> go to LLM fallback
           - Returns str (parse failed)           -> go to LLM fallback

        2. LLM fallback via llm_extract_json() (requires llm_services):
           Passes the full Plan schema so the LLM returns the complete
           structure, not just the minimum required fields.
           - Returns dict with all expected keys  -> use it
           - Otherwise                            -> final fallback

        3. Final fallback:
           Plan with a self-healing recovery step whose planner_reasoning
           stores the raw content and whose goal instructs the planner to
           retry with valid JSON.

        Args:
            goal: The planning goal.
            raw_content: Raw LLM response (str or dict).
            llm_services: Pre-sliced list of LLMService instances for the
                extraction fallback (index 0 = highest priority within the
                allowed range).

        Returns:
            Parsed Plan (never raises).
        """

        _EXPECTED = ["next_steps"]
        _SCHEMA = """{
  "interrupt_current_step": <boolean: true = abort the currently running agent step immediately; false = let it finish>,
  "last_step_confidence": <number 0.0-1.0 | null: confidence that the last completed step achieved its goal; omit or set empty next_steps to signal task completion>,
  "confidence_rationale": "<string | null: one sentence explaining why this confidence score was assigned, referencing specific evidence from tool outputs>",
  "completion_reason": "<string | null: required when next_steps is empty — one sentence explaining what was accomplished or why the task is infeasible>",
  "next_steps": [
    {
      "step_id": "<string: unique id>",
      "description": "<string: brief description of what this step does>",
      "goal": "<string: specific goal for this step>",
      "step_supplement": "<string: extra input appended to goal; data slice for parallel steps, empty otherwise>",
      "parallel_group": "<string: shared group id for parallel steps, empty otherwise>",
      "is_aggregation": <boolean: true if this step aggregates parallel results>,
      "planner_reasoning": "<string: why this step is needed (set on first step only)>",
      "expected_outcomes": ["<string: one observable dimension of success>", "<string: another dimension>"],
      "risk_assessment": "<string: what could go wrong and the fallback strategy>",
      "required_context_keys": ["<string: step_id of a prior step whose findings this step needs>"]
    }
  ]
}"""

        original_str: str = (
            raw_content if isinstance(raw_content, str) else str(raw_content)
        )

        _validation_error: str = ""

        # Step 1: try_parse_json
        parsed: Union[dict, str] = try_parse_json(original_str)

        if isinstance(parsed, dict) and all(k in parsed for k in _EXPECTED):
            try:
                return cls.from_planner_dict(goal=goal, data=parsed)
            except ValueError as exc:
                _validation_error = str(exc)
                # fall through to LLM extraction

        # Step 2: LLM extraction fallback
        if llm_services is not None and len(llm_services) > 0:
            result: Union[dict, str] = await llm_extract_json(
                content=original_str,
                expected_keys=_EXPECTED,
                llm_services=llm_services,
                schema=_SCHEMA,
            )
            if isinstance(result, dict):
                try:
                    return cls.from_planner_dict(goal=goal, data=result)
                except ValueError as exc:
                    _validation_error = str(exc)
                    # fall through to final fallback

        # Step 3: final fallback — self-healing recovery step
        _error_context = (
            f"Validation error: {_validation_error}"
            if _validation_error
            else "The previous planner response could not be parsed as JSON."
        )
        fallback_step = Step.from_planner(
            step_id=str(_uuid.uuid4()),
            description="[Planner response error — retry required]",
            goal=(
                f"{_error_context} "
                "Review the raw response in planner_reasoning and produce a "
                "correctly formatted JSON plan for the original goal."
            ),
            planner_reasoning=original_str,
        )
        return cls(goal=goal, next_steps=[fallback_step])

    # ── Step navigation ───────────────────────────────────────────────────────

    def get_step(self, step_id: str) -> Optional[Step]:
        for step in self.next_steps:
            if step.step_id == step_id:
                return step
        return None

    def get_next_pending(self) -> Optional[Step]:
        """Return the first PENDING step."""
        for step in self.next_steps:
            if step.has_status(StepStatus.PENDING):
                return step
        return None

    def get_steps_by_status(self, *statuses: StepStatus) -> List[Step]:
        return [step for step in self.next_steps if step.has_status(*statuses)]

    def get_progress(self) -> tuple[int, int]:
        """Return (completed_count, total_count)."""
        completed = len(self.get_steps_by_status(StepStatus.COMPLETED, StepStatus.SKIPPED))
        return (completed, len(self.next_steps))

    def get_aggregated_completion_info(self) -> dict:
        """Aggregate completion information from all completed steps.
        
        Returns a dict with:
        - outcomes: List of all step outcomes
        - artifacts: Deduplicated list of all artifacts created/modified
        - key_findings: List of all key findings
        
        This is useful for generating comprehensive completion summaries.
        """
        outcomes = []
        artifacts_set = set()
        key_findings = []
        
        for step in self.get_steps_by_status(StepStatus.COMPLETED):
            if step.factual_outcome:
                outcomes.extend(step.factual_outcome)
            if step.artifacts:
                artifacts_set.update(step.artifacts)
            if step.key_findings:
                key_findings.extend(step.key_findings)
        
        return {
            "outcomes": outcomes,
            "artifacts": sorted(list(artifacts_set)),  # Sort for consistent ordering
            "key_findings": key_findings,
        }
