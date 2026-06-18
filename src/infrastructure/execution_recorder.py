"""
Execution Recorder — Structured execution log, one file per session.

Log format
──────────
Each record is a tagged block. Tags are ALL_CAPS prefixes on their own line,
making the file trivially parseable with a simple line scanner.

  ┌─ Session header ───────────────────────────────────────────────────────────
  │ SESSION_START
  │ session_id: <id>
  │ goal      : <session goal>
  │ started_at: <YYYY-MM-DD HH:MM:SS>
  │ ════════════════════════════════════════════════════════════════════════
  │
  │ ┌─ Item block ─────────────────────────────────────────────────────────────
  │ │ ITEM_START
  │ │ item_id    : <id>
  │ │ goal       : <item goal / instruction>
  │ │ started_at : <timestamp>
  │ │ reasoning  :
  │ │   <planner reasoning — why this item was chosen>
  │ │ expected_outcomes:
  │ │   1. <observable success criterion>
  │ │   2. <another criterion>
  │ │ ════════════════════════════════════════════════════════════════════════
  │ │
  │ │ ┌─ Dispatch (in-flight) ───────────────────────────────────────────────────
  │ │ │ DISPATCH
  │ │ │ item_id  : <id>
  │ │ │ iter     : <N>
  │ │ │ ts       : <timestamp>
  │ │ │ tool     : <tool_name>
  │ │ │ snippet  : <truncated params summary>
  │ │ │ ════════════════════════════════════════════════════════════════════════
  │ │ └─────────────────────────────────────────────────────────────────────────
  │ │
  │ │ ┌─ Iteration ─────────────────────────────────────────────────────────────
  │ │ │ ITER
  │ │ │ item_id       : <id>
  │ │ │ iter          : <N>
  │ │ │ ts            : <timestamp>
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
  │ │ ITEM_END
  │ │ item_id  : <id>
  │ │ status   : success | failed
  │ │ ended_at : <timestamp>
  │ │ goal     : <item goal>
  │ │ factual_outcome: <statement1>; <statement2>
  │ │ artifacts: <file1>, <file2>
  │ │ findings : <finding1>; <finding2>
  │ │ issues   : <issue1>; <issue2>
  │ │ tools_used: <tool1: input>, <tool2: input>
  │ │ ════════════════════════════════════════════════════════════════════════
  │ └──────────────────────────────────────────────────────────────────────────
  │
  │ ACCEPTANCE
  │ ts        : <timestamp>
  │ verdict   : PASS | PARTIAL | FAIL | SKIPPED
  │ test_step : yes | no
  │ rationale : <one-sentence explanation>
  │ ════════════════════════════════════════════════════════════════════════
  │
  └─ Session footer ───────────────────────────────────────────────────────────
    SESSION_END
    session_id: <id>
    status    : success | failed
    ended_at  : <timestamp>
    completion: <completion reason>
    ════════════════════════════════════════════════════════════════════════

Parsing
───────
  Records are delimited by tag lines (SESSION_START, ITEM_START, DISPATCH,
  ITER, ITEM_END, ACCEPTANCE, SESSION_END).  Each record ends at the next
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

  Item metadata (structured LLM output — stored in full, no truncation):
  - ITEM_START goal, description, expected_outcomes, planner_reasoning
  - ITEM_END goal, factual_outcome, findings, issues

Thread safety
─────────────
  A threading.Lock protects all file writes so parallel tool dispatches
  can safely write to the same file concurrently.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..tools.base_tool import ToolResult
from ..models.token_usage import TokenUsage

if TYPE_CHECKING:
    from ..controller_v2.agent_utils import TurnOutcome

_SEP = "═" * 72


class ExecutionRecorder:
    """
    Persists execution traces to a structured log file — one per session.

    The V2 controller creates a single recorder at session start. Each
    checklist item produces an ITEM_START → ITER* → ITEM_END sequence.
    """

    MAX_OUTPUT_LEN: int = 2000
    MAX_PARAM_VALUE_LEN: int = 500

    def __init__(
        self,
        plan_id: str,
        goal: str,
        log_dir: str = "./executions_logs",
    ) -> None:
        self.session_id = plan_id
        if "[Current request]" in goal:
            goal = goal.split("[Current request]", 1)[1].strip()
        self.goal = goal
        self.completion_reason: str = ""
        self._lock = threading.Lock()
        self._token_usage = TokenUsage()
        self._session_ended = False

        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"session_{timestamp}_{plan_id[:8]}.log"
        self.log_path = log_dir_path / filename

        self._write_session_header()

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
        lines = [f"{label}:"]
        for line in text.splitlines():
            lines.append(f"  {line}")
        return "\n".join(lines)

    # ── Output formatter ─────────────────────────────────────────────────────

    def _format_output(self, tool_result: ToolResult) -> tuple[str, str]:
        out = tool_result.output
        if isinstance(out, dict) and "returncode" in out:
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

    # ── Session header / footer ───────────────────────────────────────────────

    def _write_session_header(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        block = "\n".join([
            "SESSION_START",
            f"session_id: {self.session_id}",
            f"goal      : {self.goal}",
            f"started_at: {now}",
            _SEP, "",
        ])
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(block)

    def write_session_end(self, success: bool, completion_reason: str = "") -> None:
        if self._session_ended:
            return
        self._session_ended = True
        reason = completion_reason or self.completion_reason
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "SESSION_END",
            f"session_id: {self.session_id}",
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

    # Backward-compat alias — V2 flow_controller still passes plan_id="persistent_session"
    write_plan_end = write_session_end

    # ── Item header / footer ─────────────────────────────────────────────────

    def write_agent_start(
        self,
        step_id: str,
        goal: str = "",
        planner_reasoning: str = "",
        expected_outcomes: Optional[List[str]] = None,
        active_tools: Optional[List[str]] = None,
        ssh_target: str = "",
        skills_required: Optional[List[str]] = None,
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "ITEM_START",
            f"item_id    : {step_id}",
            f"goal       : {goal}",
            f"started_at : {now}",
        ]
        if active_tools:
            lines.append(f"active_tools  : {', '.join(active_tools)}")
        if skills_required:
            lines.append(f"skills_required: {', '.join(skills_required)}")
        if ssh_target:
            lines.append(f"ssh_target : {ssh_target}")
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
            "ITEM_END",
            f"item_id  : {step_id}",
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

    # ── Acceptance synthesis decision ─────────────────────────────────────────

    def write_acceptance_decision(
        self,
        verdict: str,
        rationale: str = "",
        has_test_step: bool = False,
    ) -> None:
        """Record the goal-level acceptance synthesis verdict (PASS/PARTIAL/FAIL/SKIPPED)."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "ACCEPTANCE",
            f"ts        : {now}",
            f"verdict   : {verdict}",
            f"test_step : {'yes' if has_test_step else 'no'}",
        ]
        if rationale:
            lines.append(f"rationale : {rationale}")
        lines.extend([_SEP, ""])
        self._append("\n".join(lines))

    # ── Iteration (Think + Act) ───────────────────────────────────────────────

    def write_iteration(
        self,
        tool_result: ToolResult,
        decision: "TurnOutcome",
        iteration: int,
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

        params = tool_result.tool_parameters or {}
        param_lines: list[str] = []
        for k, v in sorted(params.items()):
            s = v if isinstance(v, str) else str(v)
            param_lines.append(f"  {k}: {self._truncate(s, self.MAX_PARAM_VALUE_LEN)}")

        out_label, display = self._format_output(tool_result)

        lines = [
            "ITER",
            f"item_id  : {step_id}",
            f"iter     : {iteration}",
            f"ts       : {ts_str}",
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

    # ── Log parser ────────────────────────────────────────────────────────────

    @classmethod
    def parse_log(cls, log_path: str) -> dict:
        """
        Parse a structured execution log file into a dashboard-ready dict.

        Returns
        -------
        {
          "session_id": str,
          "goal"      : str,
          "status"    : "running" | "success" | "failed" | "unknown",
          "started_at": str,
          "ended_at"  : str | None,
          "completion": str,
          "items": [
            {
              "item_id"    : str,
              "description": str,
              "goal"       : str,
              "status"     : "running" | "success" | "failed" | "unknown",
              "started_at" : str,
              "ended_at"   : str | None,
              "factual_outcome": [str, ...],
              "artifacts"  : [str, ...],
              "findings"   : [str, ...],
              "issues"     : [str, ...],
              "iterations" : int,
              "tools_used" : [str, ...],
              "thinking"   : str,   # most recent reasoning snippet
            },
            ...
          ],
          "stats": {
            "total_items"    : int,
            "completed_items": int,
            "failed_items"   : int,
            "total_iters"    : int,
          },
        }

        The parser is tolerant: unknown lines are silently skipped.
        A log still being written (no SESSION_END yet) returns status="running".
        """
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls._empty_parse_result()

        return cls._parse_log_text(text)

    @classmethod
    def _empty_parse_result(cls) -> dict:
        return {
            "session_id": "", "goal": "", "status": "unknown",
            "started_at": "", "ended_at": None, "completion": "",
            "items": [],
            "stats": {"total_items": 0, "completed_items": 0,
                      "failed_items": 0, "total_iters": 0},
        }

    @staticmethod
    def _empty_item_dict(item_id: str = "", description: str = "",
                         goal: str = "", started_at: str = "") -> dict:
        return {
            "item_id": item_id,
            "step_id": item_id,  # backward-compat alias
            "description": description,
            "goal": goal,
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "factual_outcome": [],
            "artifacts": [], "findings": [], "issues": [],
            "expected_outcomes": [],
            "active_tools": [],
            "skills_required": [],
            "ssh_target": "",
            "iterations": 0,
            "tools_used": [], "thinking": "",
            "reasoning": "",
            "latest_iteration": None,
        }

    @classmethod
    def _parse_log_text(cls, text: str) -> dict:
        result = cls._empty_parse_result()
        sep = "═" * 72
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
                if not line.startswith("  ") and ":" in line:
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

        item_index: dict = {}

        for record_lines in raw_records:
            if record_lines and record_lines[0] and record_lines[0][0] == " ":
                continue

            tag, fields = _parse_record(record_lines)

            # ── Session header (also accepts legacy PLAN_START) ──────────────
            if tag in ("SESSION_START", "PLAN_START"):
                result["session_id"] = fields.get("session_id", "") or fields.get("plan_id", "")
                result["goal"] = fields.get("goal", "")
                result["started_at"] = fields.get("started_at", "")
                result["status"] = "running"

            # ── Session footer (also accepts legacy PLAN_END) ────────────────
            elif tag in ("SESSION_END", "PLAN_END"):
                result["ended_at"] = fields.get("ended_at", "")
                result["completion"] = fields.get("completion", "")
                raw_status = fields.get("status", "")
                result["status"] = "success" if raw_status == "success" else "failed"

            # ── Item start (also accepts legacy STEP_START) ──────────────────
            elif tag in ("ITEM_START", "STEP_START"):
                item_id = fields.get("item_id", "") or fields.get("step_id", "")
                item: dict = cls._empty_item_dict(
                    item_id=item_id,
                    description=fields.get("description", ""),
                    goal=fields.get("goal", ""),
                    started_at=fields.get("started_at", ""),
                )
                item["reasoning"] = fields.get("reasoning", "")
                tools_raw = fields.get("active_tools", "")
                if tools_raw:
                    item["active_tools"] = [t.strip() for t in tools_raw.split(",") if t.strip()]
                skills_raw = fields.get("skills_required", "")
                if skills_raw:
                    item["skills_required"] = [t.strip() for t in skills_raw.split(",") if t.strip()]
                item["ssh_target"] = fields.get("ssh_target", "")
                raw_outcomes = fields.get("expected_outcomes", "")
                if raw_outcomes:
                    items_list = []
                    for line in raw_outcomes.splitlines():
                        line = line.strip()
                        if line and line[0].isdigit() and ". " in line:
                            line = line.split(". ", 1)[1]
                        if line:
                            items_list.append(line)
                    item["expected_outcomes"] = items_list
                item_index[item_id] = len(result["items"])
                result["items"].append(item)

            # ── Item end (also accepts legacy STEP_END) ──────────────────────
            elif tag in ("ITEM_END", "STEP_END"):
                item_id = fields.get("item_id", "") or fields.get("step_id", "")
                idx = item_index.get(item_id)
                if idx is None:
                    item = cls._empty_item_dict(
                        item_id=item_id,
                        goal=fields.get("goal", ""),
                    )
                    item["status"] = "unknown"
                    item_index[item_id] = len(result["items"])
                    result["items"].append(item)
                    idx = item_index[item_id]
                s = result["items"][idx]
                raw_status = fields.get("status", "")
                s["status"] = "success" if raw_status == "success" else "failed"
                s["ended_at"] = fields.get("ended_at", "")
                if fields.get("goal"):
                    s["goal"] = fields["goal"]
                factual_raw = fields.get("factual_outcome", "")
                s["factual_outcome"] = [o.strip() for o in factual_raw.split(";") if o.strip()] if factual_raw else []
                artifacts_raw = fields.get("artifacts", "")
                s["artifacts"] = [a.strip() for a in artifacts_raw.split(",") if a.strip()] if artifacts_raw else []
                findings_raw = fields.get("findings", "")
                s["findings"] = [f.strip() for f in findings_raw.split(";") if f.strip()] if findings_raw else []
                issues_raw = fields.get("issues", "")
                s["issues"] = [i.strip() for i in issues_raw.split(";") if i.strip()] if issues_raw else []
                tools_used_raw = fields.get("tools_used", "")
                if tools_used_raw:
                    s["tools_used"] = [t.strip() for t in tools_used_raw.split(",") if t.strip()]

            # ── Iteration ────────────────────────────────────────────────────
            elif tag == "ITER":
                item_id = fields.get("item_id", "") or fields.get("step_id", "")
                tool = fields.get("tool", "")
                thinking = fields.get("think", "")
                if item_id and item_id in item_index:
                    idx = item_index[item_id]
                elif result["items"]:
                    idx = len(result["items"]) - 1
                else:
                    continue
                s = result["items"][idx]
                s["iterations"] += 1
                if tool:
                    s["tools_used"].append(tool)
                if thinking:
                    s["thinking"] = thinking
                s["latest_iteration"] = {
                    "tool": tool,
                    "think": thinking,
                    "params": fields.get("params", ""),
                    "output": fields.get("output", "") or fields.get("error", ""),
                    "status": fields.get("status", ""),
                    "ts": fields.get("ts", ""),
                }

            # ── Dispatch (in-flight tool) ────────────────────────────────────
            elif tag == "DISPATCH":
                item_id = fields.get("item_id", "") or fields.get("step_id", "")
                tool = fields.get("tool", "")
                snippet = fields.get("snippet", "")
                if item_id and item_id in item_index:
                    idx = item_index[item_id]
                elif result["items"]:
                    idx = len(result["items"]) - 1
                else:
                    continue
                s = result["items"][idx]
                s["latest_iteration"] = {
                    "tool": tool,
                    "think": "",
                    "params": snippet,
                    "output": "(running...)",
                    "status": "running",
                    "ts": fields.get("ts", ""),
                }

            # ── Skip PLAN/REVIEW/VERIFY — older planner tags ─────────────────
            elif tag in ("PLAN", "REVIEW", "VERIFY"):
                continue

        items = result["items"]
        result["stats"] = {
            "total_items": len(items),
            "completed_items": sum(1 for s in items if s["status"] == "success"),
            "failed_items": sum(1 for s in items if s["status"] == "failed"),
            "total_iters": sum(s["iterations"] for s in items),
        }
        # Backward-compat aliases for consumers using older field names (e.g. TUI)
        result["plan_id"] = result["session_id"]
        result["steps"] = result["items"]
        result["plans"] = []
        result["stats"]["total_steps"] = result["stats"]["total_items"]
        result["stats"]["completed_steps"] = result["stats"]["completed_items"]
        result["stats"]["failed_steps"] = result["stats"]["failed_items"]
        return result

    # ── Backward-compat aliases for callers using old field names ─────────────

    @property
    def plan_id(self) -> str:
        return self.session_id

    @plan_id.setter
    def plan_id(self, value: str) -> None:
        self.session_id = value
