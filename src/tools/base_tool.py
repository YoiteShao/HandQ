"""
Base Tool - Abstract base class for all tools.
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional
from datetime import datetime


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

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
    
    def get_display_info(self) -> Dict[str, str]:
        """
        Get formatted display information for UI with truncation.
        
        Returns a dict with:
        - tool_name: The tool name (for bash, truncated command)
        - params: Truncated parameters string
        - result: Truncated output/error string
        
        Truncation rules:
        - bash: command[:50] as tool_name, stdout[:100] as result
        - other tools: params and result truncated to 100 chars
        - All truncations use '...' suffix
        """
        tool_name = self.tool_name or "unknown"
        params_str = ""
        result_str = ""
        
        # Special handling for bash/shell tool
        if tool_name in ("bash", "shell"):
            # Extract command from parameters
            command = ""
            if self.tool_parameters:
                command = self.tool_parameters.get("command", "")
            
            # Truncate command to 50 chars for tool_name display
            if command:
                tool_name_display = command[:50]
                if len(command) > 50:
                    tool_name_display += "..."
            else:
                tool_name_display = "bash"
            
            # For result, show stdout truncated to 100 chars
            if self.success and self.output:
                output_str = str(self.output)
                result_str = output_str[:100]
                if len(output_str) > 100:
                    result_str += "..."
            elif self.error:
                error_str = str(self.error)
                result_str = error_str[:100]
                if len(error_str) > 100:
                    result_str += "..."
            
            return {
                "tool_name": tool_name_display,
                "params": "",  # Don't show params for bash, command is in tool_name
                "result": result_str
            }
        
        # For other tools
        # Format parameters
        if self.tool_parameters:
            # Create a compact params string
            param_parts = []
            for key, value in self.tool_parameters.items():
                value_str = str(value)
                # Truncate individual param values
                if len(value_str) > 50:
                    value_str = value_str[:50] + "..."
                param_parts.append(f"{key}={value_str}")
            
            params_str = ", ".join(param_parts)
            # Truncate overall params string
            if len(params_str) > 100:
                params_str = params_str[:100] + "..."
        
        # Format result (output or error)
        if self.success and self.output is not None:
            output_str = str(self.output)
            result_str = output_str[:100]
            if len(output_str) > 100:
                result_str += "..."
        elif self.error:
            error_str = str(self.error)
            result_str = error_str[:100]
            if len(error_str) > 100:
                result_str += "..."
        
        return {
            "tool_name": tool_name,
            "params": params_str,
            "result": result_str
        }
    
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

    def to_text(self) -> str:
        """Convert to a text description (kept for backward compatibility / logging)."""
        error_msg = f" Error: {self.error}" if self.error else ""
        return f"Tool: {self.tool_name}. Parameters: {self.tool_parameters}. Success: {self.success}. Output: {self.output}.{error_msg}"


class BaseTool(ABC):
    """Abstract base class — all tools must inherit from this."""

    # Concurrency safety flags — subclasses override as needed.
    # is_read_only:        True  → tool never modifies filesystem/state (read, grep, etc.)
    # is_concurrency_safe: True  → tool can run in parallel with other safe tools
    is_read_only: bool = False
    is_concurrency_safe: bool = False

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

    def __init__(self, name: str):
        """
        Args:
            name: Tool name (used as identifier).
        """
        self.name = name
    
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
    
    def get_description(self) -> str:
        """Return the tool description string."""
        return self.__doc__ or f"{self.name} tool"
