"""
Decision - Agent decision produced in the Think phase.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from ..infrastructure.utils import try_parse_json, llm_extract_json

# Full JSON schema shown to the LLM extraction fallback so it returns the
# complete Decision structure, not just the minimum required fields.
_DECISION_SCHEMA = """{
  "reasoning": "<string: your reasoning process>",
  "tool_name": "<string | null: tool to call, or omit/null when goal is achieved>",
  "parameters": {<object | null: tool parameters, required when tool_name is set>},
  "error": "<string | null: error message if goal is unachievable>",
  "factual_outcome": ["<string: one factual statement about what was accomplished>", "<string: another statement>"],
  "artifacts": ["<string: file or resource created/modified>"],
  "key_findings": ["<string: important discovery>"],
  "blockers": ["<string: blocker preventing goal completion>"]
}"""


@dataclass
class ToolCall:
    """A single tool call within a Decision (for parallel execution)."""
    call_id: str
    tool_name: str
    parameters: Dict[str, Any]


@dataclass
class Decision:
    """Agent decision produced by the Think phase.

    Completion contract:
      - tool_calls non-empty → execute all tools concurrently (1 or N), then observe
      - tool_calls empty, tool_name None → goal is achieved; exit the loop
      - error is set → goal is unachievable; exit with failure

    tool_calls is the single source of truth for what to execute.
    tool_name / parameters are convenience properties derived from tool_calls[0]
    and kept for logging / backward-compat read access only — do not set them
    directly; build a ToolCall and put it in tool_calls instead.
    """

    reasoning: str
    error: Optional[str] = None

    # Structured completion info (only when tool_calls is empty and no error)
    factual_outcome: Optional[List[str]] = None
    artifacts: Optional[List[str]] = None
    key_findings: Optional[List[str]] = None
    blockers: Optional[List[str]] = None

    # All tool calls to execute this turn (1 = sequential, N = concurrent).
    tool_calls: List[ToolCall] = field(default_factory=list)

    # ── Convenience read-only properties (derived from tool_calls[0]) ─────────

    @property
    def tool_name(self) -> Optional[str]:
        """Name of the first tool call, or None when there are no tool calls."""
        return self.tool_calls[0].tool_name if self.tool_calls else None

    @property
    def parameters(self) -> Optional[Dict[str, Any]]:
        """Parameters of the first tool call, or None when there are no tool calls."""
        return self.tool_calls[0].parameters if self.tool_calls else None

    @property
    def is_parallel(self) -> bool:
        """Return True if this decision contains more than one concurrent tool call."""
        return len(self.tool_calls) > 1

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Decision':
        """Create a Decision from a dict (plain-text / JSON fallback path).

        Handles the legacy single-tool format where the LLM returns
        ``tool_name`` + ``parameters`` as top-level keys.
        """
        tool_calls: List[ToolCall] = []
        tool_name = data.get("tool_name")
        if tool_name:
            tool_calls = [ToolCall(
                call_id="call_0",
                tool_name=tool_name,
                parameters=data.get("parameters") or {},
            )]
        return cls(
            reasoning=data.get("reasoning", ""),
            error=data.get("error"),
            factual_outcome=data.get("factual_outcome"),
            artifacts=data.get("artifacts"),
            key_findings=data.get("key_findings"),
            blockers=data.get("blockers"),
            tool_calls=tool_calls,
        )

    @classmethod
    async def from_data(
        cls,
        raw_content: Any,
        llm_services: Any = None,
    ) -> 'Decision':
        """
        Parse an LLM response into a Decision.

        Flow:
        1. try_parse_json(raw_content)
           - Returns dict with "reasoning" key  -> use it directly
           - Otherwise                          -> go to LLM fallback

        2. LLM fallback via llm_extract_json() (requires llm_services):
           Passes the full Decision schema so the LLM returns the complete
           structure, not just the minimum required fields.
           - Returns dict with "reasoning" key  -> use it
           - Otherwise                          -> final fallback

        3. Final fallback:
           Decision(error=<original raw_content>)

        Args:
            raw_content: Raw LLM response (str or dict).
            llm_services: Pre-sliced list of LLMService instances for the
                extraction fallback (index 0 = highest priority within the
                allowed range).

        Returns:
            Parsed Decision (never raises).
        """
        _EXPECTED = ["reasoning"]

        original_str: str = (
            raw_content if isinstance(raw_content, str) else str(raw_content)
        )

        # Step 1: try_parse_json
        parsed: Union[dict, str] = try_parse_json(original_str)

        if isinstance(parsed, dict) and all(k in parsed for k in _EXPECTED):
            return cls.from_dict(parsed)

        # Step 2: LLM extraction fallback
        if llm_services is not None and len(llm_services) > 0:
            result: Union[dict, str] = await llm_extract_json(
                content=original_str,
                expected_keys=_EXPECTED,
                llm_services=llm_services,
                schema=_DECISION_SCHEMA,
            )
            if isinstance(result, dict):
                return cls.from_dict(result)

        # Step 3: final fallback — treat natural-language text as a completion
        # summary rather than an error.  When the LLM outputs a plain-text
        # summary instead of JSON (e.g. after finishing a task), setting
        # error=original_str causes the agent to restart and re-execute
        # side-effectful operations.  Using the text as `factual_outcome` instead
        # signals completion without triggering a restart.
        # A failed JSON parse does NOT mean the previous tool call failed —
        # the tool may have succeeded while the LLM's summary failed to
        # serialize.
        return cls(
            reasoning=original_str[:500] if original_str else "Failed to parse LLM response as JSON.",
            factual_outcome=[original_str[:500]] if original_str else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        result: Dict[str, Any] = {"reasoning": self.reasoning}
        if self.tool_calls:
            result["tool_name"] = self.tool_calls[0].tool_name
            result["parameters"] = self.tool_calls[0].parameters
        if self.error:
            result["error"] = self.error
        if self.factual_outcome is not None:
            result["factual_outcome"] = self.factual_outcome
        if self.artifacts is not None:
            result["artifacts"] = self.artifacts
        if self.key_findings is not None:
            result["key_findings"] = self.key_findings
        if self.blockers is not None:
            result["blockers"] = self.blockers
        return result

    def is_valid(self) -> bool:
        """Return True if the decision specifies at least one tool to execute."""
        return bool(self.tool_calls) and not self.error

    def __str__(self) -> str:
        if self.error:
            return f"Decision(error='{self.error}')"
        elif not self.tool_calls:
            return f"Decision(complete, reasoning='{self.reasoning[:50]}...')"
        elif self.is_parallel:
            names = [tc.tool_name for tc in self.tool_calls]
            return f"Decision(parallel_tools={names})"
        else:
            return f"Decision(tool='{self.tool_calls[0].tool_name}', params={self.tool_calls[0].parameters})"
