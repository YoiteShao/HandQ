"""
Execution Recorder — Structured execution log, one file per task.

Log format
──────────
Each record is a tagged block. Tags are ALL_CAPS prefixes on their own line,
making the file trivially parseable with a simple line scanner.

  ┌─ Task header ──────────────────────────────────────────────────────────────
  │ PLAN_START
  │ plan_id   : <uuid>
  │ goal      : <task goal>
  │ started_at: <YYYY-MM-DD HH:MM:SS>
  │ ════════════════════════════════════════════════════════════════════════
  │
  │ ┌─ Plan snapshot (written after initial plan and every replan) ────────────
  │ │ PLAN
  │ │ ts              : <timestamp>
  │ │ replan          : yes | no   (yes = triggered by step result or user msg)
  │ │ goal            : <plan goal>
  │ │ completion_reason: <reason>  (only when next_steps is empty)
  │ │ last_step_confidence: <0.00> (only when present)
  │ │ confidence_rationale: <text> (only when present)
  │ │ interrupt_current_step: yes  (only when True)
  │ │ next_steps      :
  │ │   <step_id>: <description>
  │ │   <step_id>: <description>
  │ │ ════════════════════════════════════════════════════════════════════════
  │ └──────────────────────────────────────────────────────────────────────────
  │
  │ ┌─ Step block ─────────────────────────────────────────────────────────────
  │ │ STEP_START
  │ │ step_id    : <id>
  │ │ agent_id   : <id>
  │ │ description: <one-line description>
  │ │ goal       : <step goal>
  │ │ started_at : <timestamp>
  │ │ reasoning  :
  │ │   <planner reasoning — why this step was chosen>
  │ │ expected_outcomes:
  │ │   1. <observable success criterion>
  │ │   2. <another criterion>
  │ │ ════════════════════════════════════════════════════════════════════════
  │ │
  │ │ ┌─ Iteration ─────────────────────────────────────────────────────────────
  │ │ │ ITER
  │ │ │ step_id       : <id>
  │ │ │ iter          : <N>
  │ │ │ ts            : <timestamp>
  │ │ │ agent_id      : <id>
  │ │ │ tool          : <tool_name>
  │ │ │ parallel_index: <N>  (only for parallel tool calls; 0-based)
  │ │ │ status        : ok | err
  │ │ │ think         :
  │ │ │   <reasoning text>
  │ │ │ params        :
  │ │ │   key: value  (values truncated to 500 chars)
  │ │ │ output        :
  │ │ │   <output or error text>
  │ │ │ ════════════════════════════════════════════════════════════════════════
  │ │ └─────────────────────────────────────────────────────────────────────────
  │ │
  │ │ STEP_END
  │ │ step_id  : <id>
  │ │ agent_id : <id>
  │ │ status   : success | failed
  │ │ ended_at : <timestamp>
  │ │ goal     : <step goal>
  │ │ factual_outcome: <statement1>; <statement2>
  │ │ artifacts: <file1>, <file2>
  │ │ findings : <finding1>; <finding2>
  │ │ issues   : <issue1>; <issue2>
  │ │ tools_used: <tool1: input>, <tool2: input>
  │ │ ════════════════════════════════════════════════════════════════════════
  │ └──────────────────────────────────────────────────────────────────────────
  │
  │ REVIEW
  │ step_id   : <id>
  │ confidence: <0.00>
  │ threshold : <0.00>
  │ passed    : yes | no
  │ rationale : <one-sentence explanation>
  │ ════════════════════════════════════════════════════════════════════════
  │
  │ VERIFY
  │ ts        : <timestamp>
  │ decision  : run | skip
  │ step_id   : <id>   (only when decision=run)
  │ rationale : <why verification was run or skipped>
  │ ════════════════════════════════════════════════════════════════════════
  │
  └─ Task footer ──────────────────────────────────────────────────────────────
    PLAN_END
    plan_id   : <uuid>
    status    : success | failed
    ended_at  : <timestamp>
    completion: <completion reason>
    ════════════════════════════════════════════════════════════════════════

Parsing
───────
  Records are delimited by tag lines (PLAN_START, PLAN, STEP_START, ITER,
  STEP_END, REVIEW, PLAN_END).  Each record ends at the next
  ``════...════`` separator line (72 × ═).  Multi-line field values follow
  a `key:` line with no inline value; each subsequent indented line
  (2 spaces) is part of that value until the next non-indented key or
  separator.

Truncation policy
─────────────────
  Agent runtime content (can be arbitrarily large — truncated to stay readable):
  - tool params values : MAX_PARAM_VALUE_LEN  (500 chars)
  - output / error     : MAX_OUTPUT_LEN       (2000 chars)
  - ITER reasoning     : MAX_OUTPUT_LEN       (2000 chars)

  Step / Plan content (structured LLM output — stored in full, no truncation):
  - STEP_START goal, description, expected_outcomes, planner_reasoning
  - STEP_END goal, factual_outcome, findings, issues
  - PLAN goal, completion_reason, confidence_rationale, rationale fields

Thread safety
─────────────
  A threading.Lock protects all file writes so parallel agents can safely
  write to the same file concurrently.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..tools.base_tool import ToolResult
from ..models.token_usage import TokenUsage

if TYPE_CHECKING:
    from ..models.decision import Decision
    from ..models.plan import Plan

# Record separator — ends every block, easy to split on when parsing.
# 72 × ═ gives a visually prominent horizontal rule that is hard to miss
# when reading the log file in a text editor.
_SEP = "═" * 72


class ExecutionRecorder:
    """
    Persists the complete chain-of-thought to a structured log file.

    Each task (plan) maps to one file named with a timestamp and plan_id.
    See module docstring for the complete log format.
    """

    MAX_OUTPUT_LEN: int = 2000      # ITER reasoning / tool output — can be large
    MAX_PARAM_VALUE_LEN: int = 500  # ITER tool param values

    def __init__(
        self,
        plan_id: str,
        goal: str,
        log_dir: str = "./executions_logs",
    ) -> None:
        self.plan_id = plan_id
        # Strip planner context wrapper added by Receptionist so the log shows
        # only the user's actual request, not "[Context from prior conversation]".
        if "[Current request]" in goal:
            goal = goal.split("[Current request]", 1)[1].strip()
        self.goal = goal
        self.completion_reason: str = ""
        self._lock = threading.Lock()
        self._token_usage = TokenUsage()

        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"plan_{timestamp}_{plan_id[:8]}.log"
        self.log_path = log_dir_path / filename

        self._write_plan_header()

    # ── Truncation helper ─────────────────────────────────────────────────────

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"…[+{len(text) - max_len}]"

    # ── Low-level write ───────────────────────────────────────────────────────

    def _append(self, content: str) -> None:
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(content)

    # ── Multi-line field helper ───────────────────────────────────────────────

    @staticmethod
    def _multiline(label: str, text: str) -> str:
        """Format a multi-line field: label on its own line, content indented."""
        lines = [f"{label}:"]
        for line in text.splitlines():
            lines.append(f"  {line}")
        return "\n".join(lines)

    # ── Output formatter ─────────────────────────────────────────────────────

    def _format_output(self, tool_result: ToolResult) -> tuple[str, str]:
        """
        Return (label, display_text) for a tool result's output section.

        For bash tool results (output is a dict with stdout/stderr/returncode),
        formats them into a human-readable block instead of a raw Python dict.
        For all other tools, falls back to str(output) truncated.
        """
        out = tool_result.output
        if isinstance(out, dict) and "returncode" in out:
            # Bash-style result dict — render as structured text
            parts: list[str] = []
            rc = out.get("returncode")
            parts.append(f"returncode: {rc}")
            stdout = (out.get("stdout") or "").rstrip("\n")
            stderr = (out.get("stderr") or "").rstrip("\n")
            if stdout:
                parts.append(f"stdout:\n{self._truncate(stdout, self.MAX_OUTPUT_LEN)}")
            if stderr:
                parts.append(f"stderr:\n{self._truncate(stderr, self.MAX_OUTPUT_LEN)}")
            label = "output" if tool_result.success else "error"
            return label, "\n".join(parts)

        if tool_result.success:
            raw = str(out) if out is not None else ""
            return "output", self._truncate(raw, self.MAX_OUTPUT_LEN)
        else:
            raw = str(tool_result.error or out or "")
            return "error", self._truncate(raw, self.MAX_OUTPUT_LEN)

    # ── Plan header / footer ──────────────────────────────────────────────────

    def _write_plan_header(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        block = "\n".join([
            "PLAN_START",
            f"plan_id   : {self.plan_id}",
            f"goal      : {self.goal}",
            f"started_at: {now}",
            _SEP, "",
        ])
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(block)

    def write_plan_end(self, success: bool, completion_reason: str = "") -> None:
        reason = completion_reason or self.completion_reason
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "PLAN_END",
            f"plan_id   : {self.plan_id}",
            f"status    : {'success' if success else 'failed'}",
            f"ended_at  : {now}",
        ]
        if reason:
            lines.append(f"completion: {reason}")
        total = self._token_usage.total_tokens
        lines.append(
            f"tokens    : in={self._token_usage.input_tokens} "
            f"out={self._token_usage.output_tokens} "
            f"total={total}"
        )
        if self._token_usage.cache_creation_tokens or self._token_usage.cache_read_tokens:
            lines.append(
                f"cache     : create={self._token_usage.cache_creation_tokens} "
                f"read={self._token_usage.cache_read_tokens}"
            )
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    # ── Plan snapshot ─────────────────────────────────────────────────────────

    def write_plan_snapshot(self, plan: "Plan", is_replan: bool = False) -> None:
        """
        Record a plan snapshot after initial planning or every replan.

        Only non-empty/non-default fields are written.
        next_steps is recorded as step_id: description pairs only.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "PLAN",
            f"ts              : {now}",
            f"replan          : {'yes' if is_replan else 'no'}",
        ]
        if plan.goal:
            lines.append(f"goal            : {plan.goal}")
        if plan.completion_reason:
            lines.append(f"completion_reason: {plan.completion_reason}")
        if plan.last_step_confidence is not None:
            lines.append(f"last_step_confidence: {plan.last_step_confidence:.2f}")
        if plan.confidence_rationale:
            lines.append(f"confidence_rationale: {plan.confidence_rationale}")
        if plan.interrupt_current_step:
            lines.append("interrupt_current_step: yes")
        if plan.next_steps:
            lines.append("next_steps      :")
            for step in plan.next_steps:
                lines.append(f"  {step.step_id}: {step.description}")
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    # ── Step header / footer ──────────────────────────────────────────────────

    def write_agent_start(
        self,
        agent_id: str,
        step_id: str,
        description: str,
        goal: str = "",
        planner_reasoning: str = "",
        expected_outcomes: Optional[List[str]] = None,
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "STEP_START",
            f"step_id    : {step_id}",
            f"agent_id   : {agent_id}",
            f"description: {description}",
            f"goal       : {goal}",
            f"started_at : {now}",
        ]
        if planner_reasoning:
            lines.append(self._multiline("reasoning", planner_reasoning))
        if expected_outcomes:
            numbered = "\n".join(
                f"{i + 1}. {item}"
                for i, item in enumerate(expected_outcomes)
            )
            lines.append(self._multiline("expected_outcomes", numbered))
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    def write_agent_end(
        self,
        agent_id: str,
        step_id: str,
        success: bool,
        goal: str = "",
        factual_outcome: Optional[List[str]] = None,
        artifacts: Optional[List[str]] = None,
        key_findings: Optional[List[str]] = None,
        issues: Optional[List[str]] = None,
        tools_used: Optional[List[str]] = None,
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "STEP_END",
            f"step_id  : {step_id}",
            f"agent_id : {agent_id}",
            f"status   : {'success' if success else 'failed'}",
            f"ended_at : {now}",
        ]
        if goal:
            lines.append(f"goal     : {goal}")
        if factual_outcome:
            lines.append(f"factual_outcome: {'; '.join(factual_outcome)}")
        if artifacts:
            lines.append(f"artifacts: {', '.join(artifacts)}")
        if key_findings:
            lines.append(f"findings : {'; '.join(key_findings)}")
        if issues:
            lines.append(f"issues   : {'; '.join(issues)}")
        if tools_used:
            lines.append(f"tools_used: {', '.join(tools_used)}")
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    # ── Verification decision ─────────────────────────────────────────────────

    def write_verification_decision(
        self,
        will_verify: bool,
        rationale: str = "",
        step_id: str = "",
    ) -> None:
        """Record whether a verification step was injected or skipped, and why."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "VERIFY",
            f"ts        : {now}",
            f"decision  : {'run' if will_verify else 'skip'}",
        ]
        if step_id:
            lines.append(f"step_id   : {step_id}")
        if rationale:
            lines.append(f"rationale : {rationale}")
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    # ── Plan-review confidence ────────────────────────────────────────────────

    def write_step_confidence(
        self,
        confidence: float,
        threshold: float,
        passed: bool,
        rationale: str = "",
    ) -> None:
        lines = [
            "REVIEW",
            f"confidence: {confidence:.2f}",
            f"threshold : {threshold:.2f}",
            f"passed    : {'yes' if passed else 'no'}",
        ]
        if rationale:
            lines.append(f"rationale : {rationale}")
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    # ── Tool dispatch (in-flight) ────────────────────────────────────────────

    def write_tool_dispatch(
        self,
        step_id: str,
        agent_id: str,
        iteration: int,
        tool_name: str,
        snippet: str,
    ) -> None:
        """Record that a tool call has been dispatched (not yet completed).

        This gives real-time visibility into what the agent is doing right now,
        before the ITER record (which is written only after tool completion).
        Dashboard consumers can use DISPATCH records to show in-flight state.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "DISPATCH",
            f"step_id  : {step_id}",
            f"agent_id : {agent_id}",
            f"iter     : {iteration}",
            f"ts       : {now}",
            f"tool     : {tool_name}",
            f"snippet  : {self._truncate(snippet, self.MAX_PARAM_VALUE_LEN)}",
            _SEP, "",
        ]
        self._append("\n".join(lines))

    # ── Iteration (Think + Act) ───────────────────────────────────────────────

    def write_iteration(
        self,
        tool_result: ToolResult,
        decision: "Decision",
        iteration: int,
        agent_id: str = "",
        step_id: str = "",
        parallel_index: Optional[int] = None,
        token_usage: Optional[TokenUsage] = None,
        # Backward-compat individual kwargs
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        if token_usage is None:
            token_usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
            )

        ts = tool_result.timestamp
        ts_str = (
            ts.strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        reasoning = self._truncate(decision.reasoning or "", self.MAX_OUTPUT_LEN)

        # Format params — truncate each value to MAX_PARAM_VALUE_LEN
        params = tool_result.tool_parameters or {}
        param_lines: list[str] = []
        for k, v in sorted(params.items()):
            s = v if isinstance(v, str) else str(v)
            param_lines.append(f"  {k}: {self._truncate(s, self.MAX_PARAM_VALUE_LEN)}")

        # Format output/error
        out_label, display = self._format_output(tool_result)

        lines = [
            "ITER",
            f"step_id  : {step_id}",
            f"iter     : {iteration}",
            f"ts       : {ts_str}",
            f"agent_id : {agent_id}",
            f"tool     : {tool_result.tool_name}",
        ]
        if parallel_index is not None:
            lines.append(f"parallel_index: {parallel_index}")
        lines.append(f"status   : {'ok' if tool_result.success else 'err'}")
        if token_usage.input_tokens or token_usage.output_tokens:
            lines.append(f"tokens   : in={token_usage.input_tokens} out={token_usage.output_tokens} total={token_usage.total_tokens}")
        if token_usage.cache_creation_tokens or token_usage.cache_read_tokens:
            lines.append(f"cache    : create={token_usage.cache_creation_tokens} read={token_usage.cache_read_tokens}")
        if reasoning:
            lines.append(self._multiline("think", reasoning))
        if param_lines:
            lines.append("params   :")
            lines.extend(param_lines)
        else:
            lines.append("params   : (none)")
        lines.append(self._multiline(out_label, display))
        lines.extend([_SEP, ""])
        with self._lock:
            self._token_usage += token_usage
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def log_file(self) -> str:
        return str(self.log_path.resolve())

    # ── Static log discovery ──────────────────────────────────────────────────

    @staticmethod
    def find_latest_log(workspace_base: str = "workspace") -> Optional[str]:
        """
        Return the path of the most recently modified execution log under
        *workspace_base*, or None if no log exists.

        Searches ``<workspace_base>/*/executions_logs/*.log`` and returns the
        file with the greatest mtime.
        """
        base = Path(workspace_base)
        if not base.exists():
            return None
        candidates = list(base.glob("*/executions_logs/*.log"))
        if not candidates:
            return None
        return str(max(candidates, key=lambda p: p.stat().st_mtime))

    # ── Log parser ────────────────────────────────────────────────────────────

    @classmethod
    def parse_log(cls, log_path: str) -> dict:
        """
        Parse a structured execution log file into a dashboard-ready dict.

        Supports both the new ``···``-delimited format (written by this class)
        and the legacy ``===``/``───`` format from older sessions.

        Returns
        -------
        {
          "plan_id"   : str,
          "goal"      : str,
          "status"    : "running" | "success" | "failed" | "unknown",
          "started_at": str,
          "ended_at"  : str | None,
          "completion": str,
          "plans": [
            {
              "ts"                    : str,
              "replan"                : bool,
              "goal"                  : str,
              "completion_reason"     : str,
              "last_step_confidence"  : float | None,
              "confidence_rationale"  : str,
              "interrupt_current_step": bool,
              "next_steps"            : [{"step_id": str, "description": str}, ...],
            },
            ...
          ],
          "steps": [
            {
              "step_id"    : str,
              "agent_id"   : str,
              "description": str,
              "goal"       : str,
              "status"     : "running" | "success" | "failed" | "unknown",
              "started_at" : str,
              "ended_at"   : str | None,
              "factual_outcome": [str, ...],
              "artifacts"  : [str, ...],
              "findings"   : [str, ...],
              "issues"     : [str, ...],
              "confidence" : float | None,
              "iterations" : int,
              "tools_used" : [str, ...],   # raw names during run; formatted entries after STEP_END
              "thinking"   : str,   # most recent reasoning snippet
            },
            ...
          ],
          "stats": {
            "total_steps"    : int,
            "completed_steps": int,
            "failed_steps"   : int,
            "total_iters"    : int,
          },
        }

        The parser is tolerant: unknown lines are silently skipped.
        A log still being written (no PLAN_END / footer yet) returns
        status="running".
        """
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls._empty_parse_result()

        # Detect format: new format uses ═×72 separator; legacy used ···
        new_sep = "═" * 72
        if new_sep in text:
            return cls._parse_new_format(text, new_sep)
        elif "···" in text:
            return cls._parse_new_format(text, "···")
        else:
            return cls._parse_legacy_format(text)

    # ── New-format parser (═×72 delimited) ───────────────────────────────────

    @classmethod
    def _empty_parse_result(cls) -> dict:
        return {
            "plan_id": "", "goal": "", "status": "unknown",
            "started_at": "", "ended_at": None, "completion": "",
            "plans": [], "steps": [],
            "stats": {"total_steps": 0, "completed_steps": 0,
                      "failed_steps": 0, "total_iters": 0},
        }

    @staticmethod
    def _empty_step_dict(step_id: str = "", agent_id: str = "", description: str = "",
                         goal: str = "", started_at: str = "") -> dict:
        return {
            "step_id": step_id,
            "agent_id": agent_id,
            "description": description,
            "goal": goal,
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "outcome": "",
            "factual_outcome": [],
            "artifacts": [], "findings": [], "issues": [],
            "expected_outcomes": [],
            "confidence": None, "iterations": 0,
            "tools_used": [], "thinking": "",
            "reasoning": "",
            "latest_iteration": None,
        }

    @classmethod
    def _parse_new_format(cls, text: str, sep: str) -> dict:
        result = cls._empty_parse_result()
        raw_records: list = []
        current: list = []
        for line in text.splitlines():
            if line.strip() == sep:
                if current:
                    raw_records.append(current)
                current = []
            else:
                current.append(line)
        if current:
            raw_records.append(current)

        def _parse_record(lines):
            if not lines:
                return "", {}
            tag = lines[0].strip()
            fields: dict = {}
            i = 1
            while i < len(lines):
                line = lines[i]
                # Match both "key: value" and "key:" (no trailing space) forms,
                # but only on non-indented lines (indented lines are continuations).
                if not line.startswith("  ") and ":" in line:
                    # Split on first ": " if present, else on first ":"
                    if ": " in line:
                        key, _, val = line.partition(": ")
                    else:
                        key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if not key:
                        i += 1
                        continue
                    multi: list = []
                    if not val:
                        j = i + 1
                        while j < len(lines) and lines[j].startswith("  "):
                            multi.append(lines[j][2:])
                            j += 1
                        if multi:
                            fields[key] = "\n".join(multi)
                            i = j
                            continue
                    fields[key] = val
                i += 1
            return tag, fields

        step_index: dict = {}

        for record_lines in raw_records:
            tag, fields = _parse_record(record_lines)
            # Skip records whose tag line is indented — these are embedded content
            # from tool outputs (e.g. the agent read another log file) that happen
            # to contain the separator character.  Real records always start at
            # column 0.
            if record_lines and record_lines[0] and record_lines[0][0] == " ":
                continue

            if tag == "PLAN_START":
                result["plan_id"] = fields.get("plan_id", "")
                result["goal"] = fields.get("goal", "")
                result["started_at"] = fields.get("started_at", "")
                result["status"] = "running"

            elif tag == "PLAN_END":
                result["ended_at"] = fields.get("ended_at", "")
                result["completion"] = fields.get("completion", "")
                raw_status = fields.get("status", "")
                result["status"] = "success" if raw_status == "success" else "failed"

            elif tag == "PLAN":
                next_steps_raw = fields.get("next_steps", "")
                next_steps: list = []
                if next_steps_raw:
                    for line in next_steps_raw.splitlines():
                        line = line.strip()
                        if ": " in line:
                            sid, _, desc = line.partition(": ")
                            next_steps.append({"step_id": sid.strip(), "description": desc.strip()})
                try:
                    conf = float(fields.get("last_step_confidence", ""))
                except (ValueError, TypeError):
                    conf = None
                result["plans"].append({
                    "ts": fields.get("ts", ""),
                    "replan": fields.get("replan", "no") == "yes",
                    "goal": fields.get("goal", ""),
                    "completion_reason": fields.get("completion_reason", ""),
                    "last_step_confidence": conf,
                    "confidence_rationale": fields.get("confidence_rationale", ""),
                    "interrupt_current_step": fields.get("interrupt_current_step", "") == "yes",
                    "next_steps": next_steps,
                })

            elif tag == "STEP_START":
                step_id = fields.get("step_id", "")
                step: dict = cls._empty_step_dict(
                    step_id=step_id,
                    agent_id=fields.get("agent_id", ""),
                    description=fields.get("description", ""),
                    goal=fields.get("goal", ""),
                    started_at=fields.get("started_at", ""),
                )
                step["reasoning"] = fields.get("reasoning", "")
                # Parse numbered list fields back into plain string lists
                for list_field in ("expected_outcomes",):
                    raw = fields.get(list_field, "")
                    if raw:
                        items = []
                        for line in raw.splitlines():
                            line = line.strip()
                            # Strip leading "N. " numbering added by write_agent_start
                            if line and line[0].isdigit() and ". " in line:
                                line = line.split(". ", 1)[1]
                            if line:
                                items.append(line)
                        step[list_field] = items
                step_index[step_id] = len(result["steps"])
                result["steps"].append(step)

            elif tag == "STEP_END":
                step_id = fields.get("step_id", "")
                idx = step_index.get(step_id)
                if idx is None:
                    step = cls._empty_step_dict(
                        step_id=step_id,
                        agent_id=fields.get("agent_id", ""),
                        goal=fields.get("goal", ""),
                    )
                    step["status"] = "unknown"
                    step_index[step_id] = len(result["steps"])
                    result["steps"].append(step)
                    idx = step_index[step_id]
                s = result["steps"][idx]
                raw_status = fields.get("status", "")
                s["status"] = "success" if raw_status == "success" else "failed"
                s["ended_at"] = fields.get("ended_at", "")
                if fields.get("goal"):
                    s["goal"] = fields["goal"]
                s["outcome"] = fields.get("outcome", "")
                factual_outcome_raw = fields.get("factual_outcome", "")
                s["factual_outcome"] = [o.strip() for o in factual_outcome_raw.split(";") if o.strip()] if factual_outcome_raw else []
                artifacts_raw = fields.get("artifacts", "")
                s["artifacts"] = [a.strip() for a in artifacts_raw.split(",") if a.strip()] if artifacts_raw else []
                findings_raw = fields.get("findings", "")
                s["findings"] = [f.strip() for f in findings_raw.split(";") if f.strip()] if findings_raw else []
                issues_raw = fields.get("issues", "")
                s["issues"] = [i.strip() for i in issues_raw.split(";") if i.strip()] if issues_raw else []
                tools_used_raw = fields.get("tools_used", "")
                if tools_used_raw:
                    # Formatted entries from STEP_END override the raw names from ITER records
                    s["tools_used"] = [t.strip() for t in tools_used_raw.split(",") if t.strip()]

            elif tag == "ITER":
                step_id = fields.get("step_id", "")
                tool = fields.get("tool", "")
                thinking = fields.get("think", "")
                if step_id and step_id in step_index:
                    idx = step_index[step_id]
                elif result["steps"]:
                    idx = len(result["steps"]) - 1
                else:
                    continue
                s = result["steps"][idx]
                s["iterations"] += 1
                if tool:
                    s["tools_used"].append(tool)
                if thinking:
                    s["thinking"] = thinking
                # Store full latest iteration details for status display
                s["latest_iteration"] = {
                    "tool": tool,
                    "think": thinking,
                    "params": fields.get("params", ""),
                    "output": fields.get("output", "") or fields.get("error", ""),
                    "status": fields.get("status", ""),
                    "ts": fields.get("ts", ""),
                }

            elif tag == "DISPATCH":
                # In-flight tool dispatch — update latest_iteration with
                # pending state so the dashboard shows what's running now.
                step_id = fields.get("step_id", "")
                tool = fields.get("tool", "")
                snippet = fields.get("snippet", "")
                if step_id and step_id in step_index:
                    idx = step_index[step_id]
                elif result["steps"]:
                    idx = len(result["steps"]) - 1
                else:
                    continue
                s = result["steps"][idx]
                s["latest_iteration"] = {
                    "tool": tool,
                    "think": "",
                    "params": snippet,
                    "output": "(running...)",
                    "status": "running",
                    "ts": fields.get("ts", ""),
                }

            elif tag == "REVIEW":
                try:
                    conf = float(fields.get("confidence", ""))
                except (ValueError, TypeError):
                    conf = None
                if result["steps"] and conf is not None:
                    result["steps"][-1]["confidence"] = conf

        steps = result["steps"]
        result["stats"] = {
            "total_steps": len(steps),
            "completed_steps": sum(1 for s in steps if s["status"] == "success"),
            "failed_steps": sum(1 for s in steps if s["status"] == "failed"),
            "total_iters": sum(s["iterations"] for s in steps),
        }
        return result

    # ── Save-session preprocessing ────────────────────────────────────────────

    @classmethod
    def prepare_save_context(cls, log_path: str) -> dict:
        """
        Parse a structured text execution log and return a cleaned representation
        suitable for GEP template generation.

        Strips execution-phase noise (ITER tool outputs, token counts, timestamps,
        iteration details) and retains only the semantically meaningful fields per
        step that the template-generation LLM needs:
          - step_id, description, goal, status
          - factual_outcome, findings, issues, artifacts
          - reasoning (planner's why), expected_outcomes

        Parameterization (replacing session-specific paths/hostnames with
        {{params.X}} placeholders) is intentionally left to the LLM — the raw
        values in the cleaned steps give the LLM the concrete context it needs
        to choose meaningful parameter names and defaults.

        Returns:
            {
              'goal':       str   — overall task goal,
              'status':     str   — 'success' | 'failed' | 'running',
              'completion': str   — completion reason (if any),
              'steps':      list  — cleaned step dicts,
            }
        """
        parsed = cls.parse_log(log_path)

        cleaned_steps = []
        for step in parsed.get('steps', []):
            cleaned_steps.append({
                'step_id':           step.get('step_id', ''),
                'description':       step.get('description', ''),
                'goal':              step.get('goal', ''),
                'status':            step.get('status', ''),
                'factual_outcome':   step.get('factual_outcome', []),
                'findings':          step.get('findings', []),
                'issues':            step.get('issues', []),
                'artifacts':         step.get('artifacts', []),
                'reasoning':         step.get('reasoning', ''),
                'expected_outcomes': step.get('expected_outcomes', []),
            })

        return {
            'goal':       parsed.get('goal', ''),
            'status':     parsed.get('status', ''),
            'completion': parsed.get('completion', ''),
            'steps':      cleaned_steps,
        }

    # ── Legacy-format parser (=== / ─── delimited) ───────────────────────────

    @classmethod
    def _parse_legacy_format(cls, text: str) -> dict:
        """Parse the old hand-written log format used before ExecutionRecorder."""
        result = cls._empty_parse_result()
        lines = text.splitlines()

        step_index: dict = {}
        current_step: Optional[dict] = None
        in_think = False
        think_lines: list = []

        for line in lines:
            # Plan header
            if line.startswith("PLAN    :"):
                result["plan_id"] = line.split(":", 1)[1].strip()
                result["status"] = "running"
            elif line.startswith("Goal    :"):
                result["goal"] = line.split(":", 1)[1].strip()
            elif line.startswith("Started :") and not result["started_at"]:
                result["started_at"] = line.split(":", 1)[1].strip()
            # Plan footer
            elif line.startswith("Ended   :"):
                result["ended_at"] = line.split(":", 1)[1].strip()
            elif "Status  : SUCCESS" in line:
                result["status"] = "success"
            elif "Status  : FAILED" in line:
                result["status"] = "failed"
            # Step start
            elif line.startswith("AGENT   :"):
                if current_step:
                    if think_lines:
                        current_step["thinking"] = " ".join(think_lines).strip()
                    think_lines = []
                step_id = line.split(":", 1)[1].strip()
                current_step = {
                    "step_id": step_id, "agent_id": step_id,
                    "description": "", "goal": "",
                    "status": "running", "started_at": "", "ended_at": None,
                    "outcome": "", "factual_outcome": [], "artifacts": [], "findings": [], "issues": [],
                    "confidence": None, "iterations": 0,
                    "tools_used": [], "latest_tools": [], "thinking": "",
                }
                step_index[step_id] = len(result["steps"])
                result["steps"].append(current_step)
                in_think = False
            elif line.startswith("Desc    :") and current_step is not None:
                current_step["description"] = line.split(":", 1)[1].strip()
            elif line.startswith("Started :") and current_step is not None:
                current_step["started_at"] = line.split(":", 1)[1].strip()
            # Iteration tool line
            elif "] Tool:" in line and current_step is not None:
                tool = line.split("Tool:", 1)[1].strip()
                current_step["iterations"] += 1
                current_step["tools_used"].append(tool)
                current_step["latest_tools"] = current_step["tools_used"][-5:]
                in_think = False
                think_lines = []
            # Think block
            elif line.strip() == "Think   :" and current_step is not None:
                in_think = True
            elif in_think and line.startswith("    "):
                think_lines.append(line.strip())
            elif in_think and not line.startswith("    "):
                in_think = False
                if think_lines and current_step is not None:
                    current_step["thinking"] = " ".join(think_lines).strip()
                think_lines = []
            # Step success/failure markers
            elif "Success : True" in line and current_step is not None:
                pass  # iteration-level, not step-level
            elif line.startswith("════") and current_step is not None:
                # New agent block starts — mark previous as success if still running
                if current_step["status"] == "running":
                    current_step["status"] = "success"

        # Flush last step
        if current_step and current_step["status"] == "running":
            if result["status"] in ("success", "failed"):
                current_step["status"] = result["status"]
        if think_lines and current_step is not None:
            current_step["thinking"] = " ".join(think_lines).strip()

        steps = result["steps"]
        result["stats"] = {
            "total_steps": len(steps),
            "completed_steps": sum(1 for s in steps if s["status"] == "success"),
            "failed_steps": sum(1 for s in steps if s["status"] == "failed"),
            "total_iters": sum(s["iterations"] for s in steps),
        }
        return result
