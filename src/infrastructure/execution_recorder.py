"""
Execution Recorder — per-turn incremental LLM-interaction trace, one file per session.

Purpose
───────
This file is the authoritative entry point for reviewing a task's complete
LLM-interaction chain. It answers one question a human debugger actually
asks: *"what NEW content did the model see on each turn, and what did our
compaction layer do to the older content?"*

It is deliberately NOT a full-context-per-turn dump. Re-serialising the
entire ``messages`` array every turn is O(N²) — turn 50 would write 50
copies of an ever-growing array. Instead each turn records only the
INCREMENT: the messages newly appended that turn (the assistant's reasoning
+ tool_calls, and each tool observation rendered EXACTLY as it was sent to
the LLM), plus a compact note for any older turn that crossed a
render-tier boundary this turn (elided / kept-full / …). Log size is
O(total turns); the full context at turn K is reconstructable by replaying
records 1..K in order.

Format
──────
JSONL — one JSON object per line, ``kind`` discriminates the record type.
Greppable, diffable, and programmatically replayable. Filename:
``session_<YYYYMMDD_HHMMSS>_<id>.jsonl``.

  {"kind": "session_start", "session_id": ..., "goal": ..., "ts": ...}
  {"kind": "user_request",  "ts": ..., "message": <verbatim, untruncated>}
  {"kind": "first_request_snapshot", "ts": ..., "messages": [...],
                            "tool_count": <N>, "tool_names": [...]}
  {"kind": "item_start",    "item_id": ..., "goal": ..., "ts": ...,
                            "active_tools": [...], "skills_required": [...]}
  {"kind": "turn",          "turn": <N>, "item_id": ..., "ts": ...,
                            "appended": [<messages new this turn, as sent>],
                            "retiered": [<older turns whose tier changed>],
                            "tokens": {...}, "totals": {...}}
  {"kind": "item_end",      "item_id": ..., "status": ..., "ts": ...,
                            "factual_outcome": [...], "artifacts": [...],
                            "findings": [...], "issues": [...]}
  {"kind": "session_end",   "session_id": ..., "status": ..., "ts": ...,
                            "completion": ..., "tokens": {...}}

The ``turn`` record's ``appended`` entries mirror what actually entered the
``messages`` list for the freshest (tier-1) turn:
  - assistant: {"role":"assistant", "think": <reasoning>,
                "extended_thinking": <thinking_text if present>,
                "tool_calls": [{"name", "args"}],
                "claim_tool": [...], "release_tool": [...] (only if
                non-empty — see PersistentAgent._apply_self_extension),
                "stop_reason": <Anthropic stop_reason, if present>}
  - tool obs:  {"role":"tool", "tool": <name>, "ok": <bool>,
                "content": <obs.to_tool_result_json() — the real bytes sent>}

The ``first_request_snapshot`` record (written once, before the first
turn's stream opens) is the ONE place the complete, untruncated message
list — including the full system prompt and skill prelude — is captured;
every ``turn`` record after it is the truncated increment only.

``retiered`` records the observation-elision events from this build's
microcompact pass (see PersistentAgent._microcompact_old_outputs): one entry
per tool result newly elided under budget pressure, carrying the tool name,
decision, and chars saved so a reviewer can see, per turn, what the older
content collapsed into WITHOUT the full bytes being repeated.

Truncation policy
─────────────────
  Agent runtime content (arbitrarily large — truncated to stay readable):
  - tool-call arg values : MAX_PARAM_VALUE_LEN (500 chars)
  - observation content  : MAX_OUTPUT_LEN      (2000 chars)
  - reasoning / thinking : MAX_OUTPUT_LEN      (2000 chars)
  Truncation appends a ``…[+N]`` marker so the reviewer sees how much was cut.

  Item metadata (structured LLM output — stored in full, no truncation):
  - item_start goal / reasoning / expected_outcomes
  - item_end factual_outcome / findings / issues
  - user_request message (the user's raw prompt — ground truth)

Thread safety
─────────────
  A threading.Lock protects all file writes so parallel tool dispatches
  can safely write to the same file concurrently.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models.token_usage import TokenUsage


class ExecutionRecorder:
    """
    Persists an incremental LLM-interaction trace — one JSONL file per session.

    The V2 controller creates a single recorder at session start. Each task
    item produces an ``item_start`` → ``turn``* → ``item_end`` sequence; the
    session as a whole is bracketed by ``session_start`` / ``session_end``.
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
        filename = f"session_{timestamp}_{plan_id[:8]}.jsonl"
        self.log_path = log_dir_path / filename

        self._write_session_header()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _truncate(cls, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"…[+{len(text) - max_len}]"

    def _append_record(self, record: Dict[str, Any]) -> None:
        """Serialise one record as a JSON line under the write lock."""
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)

    # ── Session header / footer ───────────────────────────────────────────────

    def _write_session_header(self) -> None:
        line = json.dumps({
            "kind": "session_start",
            "session_id": self.session_id,
            "goal": self.goal,
            "ts": self._now(),
        }, ensure_ascii=False, default=str) + "\n"
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(line)

    def write_session_end(self, success: bool, completion_reason: str = "") -> None:
        if self._session_ended:
            return
        self._session_ended = True
        reason = completion_reason or self.completion_reason
        record: Dict[str, Any] = {
            "kind": "session_end",
            "session_id": self.session_id,
            "status": "success" if success else "failed",
            "ts": self._now(),
        }
        if reason:
            record["completion"] = reason
        record["tokens"] = {
            "in": self._token_usage.input_tokens,
            "out": self._token_usage.output_tokens,
            "total": self._token_usage.total_tokens,
        }
        if self._token_usage.cache_creation_tokens or self._token_usage.cache_read_tokens:
            record["tokens"]["cache_create"] = self._token_usage.cache_creation_tokens
            record["tokens"]["cache_read"] = self._token_usage.cache_read_tokens
        self._append_record(record)

    # ── User request (verbatim, one record per user send) ─────────────────────

    def write_user_request(self, message: str) -> None:
        """Record a verbatim user message as its own top-level record.

        One record per user send — deliberately NOT folded into item blocks.
        Stored in full (no truncation): this is the user's raw prompt, the
        ground truth from which the item-instruction ``goal`` is derived, so
        it must survive unflattened.
        """
        self._append_record({
            "kind": "user_request",
            "ts": self._now(),
            "message": message or "",
        })

    # ── Item header / footer ─────────────────────────────────────────────────

    def write_agent_start(
        self,
        step_id: str,
        goal: str = "",
        reasoning: str = "",
        expected_outcomes: Optional[List[str]] = None,
        active_tools: Optional[List[str]] = None,
        ssh_target: str = "",
        skills_required: Optional[List[str]] = None,
    ) -> None:
        record: Dict[str, Any] = {
            "kind": "item_start",
            "item_id": step_id,
            "goal": goal,
            "ts": self._now(),
        }
        if active_tools:
            record["active_tools"] = list(active_tools)
        if skills_required:
            record["skills_required"] = list(skills_required)
        if ssh_target:
            record["ssh_target"] = ssh_target
        if reasoning:
            record["reasoning"] = reasoning
        if expected_outcomes:
            record["expected_outcomes"] = list(expected_outcomes)
        self._append_record(record)

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
        record: Dict[str, Any] = {
            "kind": "item_end",
            "item_id": step_id,
            "status": "success" if success else "failed",
            "ts": self._now(),
        }
        if goal:
            record["goal"] = goal
        if factual_outcome:
            record["factual_outcome"] = list(factual_outcome)
        if artifacts:
            record["artifacts"] = list(artifacts)
        if key_findings:
            record["findings"] = list(key_findings)
        if issues:
            record["issues"] = list(issues)
        if tools_used:
            record["tools_used"] = list(tools_used)
        self._append_record(record)

    # ── First-request snapshot (one-shot, full fidelity) ─────────────────────

    def write_first_request_snapshot(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Record the session's first outgoing message list in full.

        Called once, before the first turn's stream opens — the only moment
        nothing has been prefix-cached, microcompacted, or elided yet, so
        this is the one place the complete, untruncated system prompt +
        skill prelude + tools is worth paying the size cost to capture.
        Every subsequent turn goes back to the incremental, truncated
        ``write_turn`` records; cross-referencing this snapshot against a
        later ``turn`` record's ``appended`` shows exactly what was added
        since session start.
        """
        self._append_record({
            "kind": "first_request_snapshot",
            "ts": self._now(),
            "messages": messages,
            "tool_count": len(tools) if tools else 0,
            "tool_names": [t.get("name") or t.get("function", {}).get("name", "")
                            for t in (tools or [])],
        })

    # ── Turn (incremental LLM interaction) ────────────────────────────────────

    def write_turn(
        self,
        *,
        turn: int,
        step_id: str,
        decision: Any,
        tool_results: List[Any],
        token_usage: Optional[TokenUsage] = None,
        retiered: Optional[List[Dict[str, Any]]] = None,
        totals: Optional[Dict[str, Any]] = None,
        ts: Optional[str] = None,
    ) -> None:
        """Record the INCREMENT the LLM saw/produced on one turn.

        ``appended`` is what newly entered the messages list this turn: the
        assistant message (reasoning + optional extended-thinking +
        tool_calls + claim_tool/release_tool + stop_reason, when non-empty)
        followed by each tool observation rendered EXACTLY as it was sent to
        the model (``ToolResult.to_tool_result_json()`` — the real bytes, so
        an elided/superseded observation shows the placeholder, not the
        original payload).

        ``retiered`` (optional) is this turn's microcompact elision events
        for OLDER turns' observations (tool/decision/chars_saved) — reused
        verbatim from ``PersistentAgent._microcompact_old_outputs``, not
        recomputed here.

        ``totals`` (optional) records the full-context size this turn
        (message count / char estimate) so context growth is trackable
        without dumping the whole array.
        """
        if token_usage is None:
            token_usage = TokenUsage()

        appended: List[Dict[str, Any]] = []

        # Assistant message — reasoning + optional extended-thinking + tool_calls.
        reasoning = self._truncate(
            getattr(decision, "reasoning", "") or "", self.MAX_OUTPUT_LEN
        )
        thinking_text = self._truncate(
            getattr(decision, "thinking_text", None) or "", self.MAX_OUTPUT_LEN
        )
        asst: Dict[str, Any] = {"role": "assistant"}
        if reasoning:
            asst["think"] = reasoning
        if thinking_text:
            asst["extended_thinking"] = thinking_text
        tool_calls = getattr(decision, "tool_calls", None) or []
        if tool_calls:
            asst["tool_calls"] = [
                {
                    "name": getattr(tc, "tool_name", "") or "",
                    "args": self._truncate_args(getattr(tc, "parameters", {}) or {}),
                }
                for tc in tool_calls
            ]
        claim_tool = getattr(decision, "claim_tool", None) or []
        if claim_tool:
            asst["claim_tool"] = list(claim_tool)
        release_tool = getattr(decision, "release_tool", None) or []
        if release_tool:
            asst["release_tool"] = list(release_tool)
        stop_reason = getattr(decision, "stop_reason", None)
        if stop_reason:
            asst["stop_reason"] = stop_reason
        appended.append(asst)

        # Tool observations — rendered EXACTLY as sent to the LLM.
        for tr in tool_results:
            content = self._truncate(tr.to_tool_result_json(), self.MAX_OUTPUT_LEN)
            appended.append({
                "role": "tool",
                "tool": getattr(tr, "tool_name", "") or "",
                "ok": bool(getattr(tr, "success", False)),
                "content": content,
            })

        record: Dict[str, Any] = {
            "kind": "turn",
            "turn": turn,
            "item_id": step_id,
            "ts": ts or self._now(),
            "appended": appended,
        }
        if retiered:
            record["retiered"] = retiered
        if token_usage.input_tokens or token_usage.output_tokens:
            record["tokens"] = {
                "in": token_usage.input_tokens,
                "out": token_usage.output_tokens,
                "total": token_usage.total_tokens,
            }
            if token_usage.cache_creation_tokens or token_usage.cache_read_tokens:
                record["tokens"]["cache_create"] = token_usage.cache_creation_tokens
                record["tokens"]["cache_read"] = token_usage.cache_read_tokens
        if totals:
            record["totals"] = totals

        with self._lock:
            self._token_usage += token_usage
        self._append_record(record)

    def _truncate_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Truncate large string values in a tool-call args dict for the log."""
        out: Dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str):
                out[k] = self._truncate(v, self.MAX_PARAM_VALUE_LEN)
            else:
                out[k] = v
        return out

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def log_file(self) -> str:
        return str(self.log_path.resolve())
