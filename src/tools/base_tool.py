"""
Base Tool - Abstract base class for all tools.
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional
from datetime import datetime

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


@dataclass
class ToolResult:
    """Tool execution result — carries the full execution context and outcome."""
    success: bool
    output: Any
    tool_name: str = ""
    tool_parameters: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time: float = 0.0
    timestamp: Optional[datetime] = None
    diff_output: Optional[str] = None      # unified diff string from edit/write operations
    lines_written: Optional[int] = None    # line count from write operations
    exit_code: Optional[int] = None        # exit code from bash operations
    superseded_note: Optional[str] = None  # when set, output is elided in LLM context (a newer same-action snapshot supersedes this one)

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    def to_obs_dict(self, step_idx: int) -> dict:
        """
        Minimal, fixed-key-order observation dict for LLM context.

        KV-cache contract:
          - No timestamps (timestamps change every run and break prefix caching).
          - Top-level key order is always: step → tool → params → ok → out | err.
          - params keys are sorted alphabetically for determinism.

        Output size policy:
          - output / error are passed through WITHOUT truncation.
          - Each tool is responsible for controlling its own output size at the
            source (e.g. read_tool enforces a 100 KB file size limit).
          - Truncating here would silently discard information the agent needs
            (e.g. a keyword that appears beyond the truncation point), causing
            false negatives and wasted tool calls.
          - Only parameter string values are capped (_MAX_PARAM_STR) because
            large parameter echoes add no value to the agent's reasoning.

        Args:
            step_idx: 1-based index of this observation in the current step.

        Returns:
            dict with fixed key order, ready for json.dumps().
        """
        _MAX_PARAM_STR = 500   # max chars for any single param string value

        def _trunc_param(v: Any, limit: int) -> str:
            s = v if isinstance(v, str) else str(v)
            return s if len(s) <= limit else s[:limit] + "…"

        # Sort param keys for determinism; truncate large string values.
        raw_params = self.tool_parameters or {}
        params: Dict[str, Any] = {
            k: (_trunc_param(v, _MAX_PARAM_STR) if isinstance(v, str) else v)
            for k, v in sorted(raw_params.items())
        }

        # Fixed top-level key order — insertion order is preserved in Python 3.7+.
        d: dict = {
            "step": step_idx,
            "tool": self.tool_name,
            "params": params,
            "ok": self.success,
        }
        if self.superseded_note is not None:
            d["out"] = self.superseded_note  # heavy output elided; tool/params kept for traceability
            return d
        if self.success:
            d["out"] = self.output  # full output, no truncation
        else:
            d["err"] = self.error or str(self.output)  # full error, no truncation
        if self.diff_output is not None:
            d['diff_output'] = self.diff_output
        if self.lines_written is not None:
            d['lines_written'] = self.lines_written
        if self.exit_code is not None:
            d['exit_code'] = self.exit_code
        return d

    def to_obs_json(self, step_idx: int) -> str:
        """Serialise to_obs_dict() as a compact JSON string (no ASCII escaping)."""
        return json.dumps(self.to_obs_dict(step_idx), ensure_ascii=False)

    def to_tool_result_dict(self) -> dict:
        """Minimal result dict for OpenAI tool-role messages.

        When the assistant message already carries the tool name and parameters
        in its ``tool_calls`` field, the paired tool-role message only needs to
        convey the outcome: whether the call succeeded and what it returned.
        Omitting the redundant ``step``, ``tool``, and ``params`` fields saves
        a significant number of tokens — especially for write/edit operations
        where the ``params`` echo can be very large.
        """
        d: dict = {"ok": self.success}
        if self.superseded_note is not None:
            d["out"] = self.superseded_note  # heavy output elided to save context
            return d
        if self.success:
            d["out"] = self.output
        else:
            d["err"] = self.error or str(self.output)
        if self.diff_output is not None:
            d["diff"] = self.diff_output
        if self.lines_written is not None:
            d["lines_written"] = self.lines_written
        if self.exit_code is not None:
            d["exit_code"] = self.exit_code
        return d

    def to_tool_result_json(self) -> str:
        """Serialise to_tool_result_dict() as a compact JSON string."""
        return json.dumps(self.to_tool_result_dict(), ensure_ascii=False)


class BaseTool(ABC):
    """Abstract base class — all tools must inherit from this."""

    # Concurrency safety flags — subclasses override as needed.
    # is_read_only:        True  → tool never modifies filesystem/state (read, grep, etc.)
    # is_concurrency_safe: True  → tool can run in parallel with other safe tools
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    # Interrupt behavior when a user message arrives mid-execution:
    #   "block"  (default) → the in-flight call finishes; the user message is
    #            queued and handled at the next iteration boundary. Correct for
    #            anything with side effects (edit, shell mutating state) — you
    #            don't want a half-applied action.
    #   "cancel" → the call may be aborted immediately when the user redirects.
    #            Correct ONLY for tools whose cancellation is safe and whose work
    #            is pure waiting/observation (wait_interval). Mirrors Claude
    #            Code's Tool.interruptBehavior — the coordinator uses it to decide
    #            whether a hard_task can interrupt right now or must queue.
    interrupt_behavior: str = "block"

    # Cancellation contract.
    #
    # shutdown_deadline: seconds the tool gets to comply with a stop request
    # AFTER cancellation fires. NOT a wall-clock cap on legitimate work — a
    # 10-minute compile is fine, but once the engine signals stop, the tool
    # must abort within this many seconds. Tools that own resources which
    # can be side-effect-aborted (paramiko transport, subprocess, fd) should
    # register cleanup callbacks during execute(); a fired AbortHandle then
    # invokes those callbacks from the asyncio thread to wake the blocked
    # syscall on the executor thread.
    #
    # Default 5s is generous for socket teardown and process kill on both
    # Windows and Linux. Tools may override.
    shutdown_deadline: float = 5.0

    # Optional asyncio.Event the engine sets to request cancellation. Tools
    # that run async-native (bash_tool's asyncio.create_subprocess_exec)
    # await this directly. Tools that run blocking work via
    # cancellation.run_with_abort_handle pass this event in and the helper
    # mirrors it into a thread-safe token.
    interrupt_event = None

    def __init__(self, name: str, ctx: Optional["SessionContext"] = None):
        """
        Args:
            name: Tool name (used as identifier).
            ctx: Optional :class:`SessionContext` carrying per-session
                resources (IM, file_state, ssh_pool, browser_session,
                session_registry, desktop_state, interrupt_event). Tools
                that don't need it can ignore the parameter; tools that
                do read from it via ``self.ctx``. ``None`` is allowed for
                test fixtures and for tools constructed before a session
                has started.
        """
        self.name = name
        self.ctx: Optional["SessionContext"] = ctx
    
    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool operation (atomic).

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            ToolResult: Execution result.
        """
        pass
    
    def validate_params(self, required_params: list, provided_params: dict) -> None:
        """Raise ValueError if any required parameter is missing."""
        missing = [p for p in required_params if p not in provided_params]
        if missing:
            raise ValueError(f"Parameters missing: {', '.join(missing)}")

    def resolve_in_workspace(self, path: str) -> str:
        """Resolve a possibly-relative *path* against the per-session workspace.

        Absolute paths are returned unchanged. Relative paths are joined to
        ``ctx.working_directory`` so resolution is independent of the process
        cwd — required for session concurrency (process cwd is a shared global
        and is no longer mutated via os.chdir). Falls back to legacy
        cwd-relative behavior only when there is no ctx / no working_directory
        (test fixtures).
        """
        p = Path(path)
        if p.is_absolute():
            return str(p)
        base = self.ctx.working_directory if (self.ctx and self.ctx.working_directory) else None
        return str(Path(base) / p) if base else str(p)

    def emit_file_touch(self, path: str, kind: str) -> None:
        """Best-effort file-touch event to the session sidebar (nebula +
        change list). Silent on missing ctx / interaction_manager /
        rewind_store — the tool's own success is never affected by whether
        the UI is wired.

        Every tool that CAN identify a specific file the agent just touched
        should call this on success — write/edit → ``edit``, read → ``read``,
        grep/glob matches → ``hit``. Callers that don't know which files
        were touched (shell running an arbitrary command) use the
        workspace-mtime scan in shell_tool instead of this helper.
        """
        try:
            if self.ctx is None or self.ctx.interaction_manager is None:
                return
            item_id = (
                self.ctx.rewind_store.current_item_id
                if self.ctx.rewind_store is not None else None
            )
            self.ctx.interaction_manager.notify_file_touch(
                path=str(path or ""),
                kind=str(kind or ""),
                tool=self.name,
                item_id=item_id,
            )
        except Exception:
            pass
