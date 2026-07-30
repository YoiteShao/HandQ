"""
claim_tool / release_tool — real structured tool_use for self-extension.

Confirmed bug (2026-07-14 API-compliance audit): claim_tool/release_tool used
to be a convention, not a tool — the model was expected to embed a JSON
object INSIDE its free-text `reasoning` on a tool-call turn, which
PersistentAgent._think_streaming then tried to json.loads() as a whole and
pull `claim_tool`/`release_tool` fields out of. A model that writes prose
reasoning (the normal, correct thing to do on a tool-call turn) has NO path
for that intent to reach the message structure, and gets zero feedback that
its stated intent ("I need to claim schedule_create") silently went nowhere.

Fix: claim_tool/release_tool are now real, always-visible tools (like
todo_write — on_demand=False, never gated behind claim_tool itself). Calling
one is a genuine Anthropic tool_use block that gets a real tool_result, so
the model's intent is structurally guaranteed to enter the conversation —
and if the name is invalid, it gets told so IN the tool_result instead of
silently vanishing.

execute() does NOT flip activation state itself — it stores the requested
names on the SessionContext (mirroring todo_write_tool's ctx.agent_todo
pattern), and PersistentAgent reads them back after the tool result comes
in, routing into the existing, unchanged _apply_self_extension(claim,
release). This keeps ToolRegistry.get_tool_names() / _hidden_tools /
_task_channel.activate_tools as the single source of truth for activation,
with the tool itself only being the model-facing intent channel.

The completion-turn claim_tool/release_tool path (TurnOutcome.
from_completion_text parsing the completion JSON envelope) is UNCHANGED —
that JSON is the documented completion schema, not free text, so parsing it
for these fields is structurally correct and not part of this bug.
"""
import time
from typing import Any, List, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


def _normalize_names(names: Any) -> List[str]:
    if not isinstance(names, list):
        return []
    return [str(n).strip() for n in names if str(n).strip()]


def _suggest_for_unknown(unknown: List[str], known: "set[str]") -> str:
    """Build an actionable 'did you mean?' clause for unknown claim names.

    The [Available Tools] menu the agent sees only lists FAMILY prefixes
    (``desktop_*``), never the individual claimable leaf names. So when a model
    guesses a plausible-but-wrong name (``desktop_click`` for the real
    ``desktop_click_at``), the bare "unknown name" error used to send it back to
    a menu that doesn't contain the answer — a dead end that, in the 2026-07-25
    flash-meta stall, made the agent abandon the whole desktop family and
    hand-roll its own automation for 6 hours.

    Two complementary hints per unknown name:
      1. difflib close matches against the real tool names (catches typos /
         near-misses like desktop_click → desktop_click_at).
      2. the full same-family roster (prefix before the first ``_``), so even a
         name difflib can't rank still surfaces every real sibling to pick from.
    """
    import difflib

    known_sorted = sorted(known)
    clauses: List[str] = []
    for name in unknown:
        close = difflib.get_close_matches(name, known_sorted, n=3, cutoff=0.6)
        # Family roster: everything sharing the prefix up to and including the
        # first underscore (desktop_click → 'desktop_'). Bare names with no
        # underscore contribute no family and rely on the difflib hint alone.
        family_hint = ""
        if "_" in name:
            prefix = name.split("_", 1)[0] + "_"
            family = [k for k in known_sorted if k.startswith(prefix)]
            if family:
                shown = family[:12]
                more = "" if len(family) <= 12 else f", … (+{len(family) - 12} more)"
                family_hint = f"; '{prefix}' family: {', '.join(shown)}{more}"
        if close:
            clauses.append(f"'{name}' → did you mean: {', '.join(close)}?{family_hint}")
        elif family_hint:
            clauses.append(f"'{name}' is not a tool{family_hint}")
        else:
            clauses.append(f"'{name}' matches no known tool")
    return " | ".join(clauses)


def _tool_registry():
    # Deferred import: tool_registry.py imports THIS module to register
    # ClaimToolTool/ReleaseToolTool, so importing ToolRegistry at module
    # level here would be circular.
    from .tool_registry import ToolRegistry
    return ToolRegistry


class ClaimToolTool(BaseTool):
    """Activate one or more hidden/on-demand tools for the rest of this task."""

    is_read_only = True  # only flips visibility state, no filesystem/network effect
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("claim_tool", ctx=ctx)

    async def execute(self, names: Any = None, **kwargs: Any) -> ToolResult:
        start = time.time()
        params = {"names": names}
        requested = _normalize_names(names)
        if not requested:
            return ToolResult(
                success=False, output=None,
                error="claim_tool expects 'names': a non-empty list of exact tool names.",
                execution_time=time.time() - start,
                tool_name=self.name, tool_parameters=params,
            )

        known = set(_tool_registry().get_tool_names())
        valid = [n for n in requested if n in known]
        unknown = [n for n in requested if n not in known]

        if self.ctx is not None:
            try:
                self.ctx.pending_claim_tool.extend(valid)
            except Exception:
                pass

        output = {"claimed": valid}
        error: Optional[str] = None
        if unknown:
            output["unknown"] = unknown
            # Always attach did-you-mean guidance when ANY name is unknown —
            # even on a partial success where some names claimed fine. The
            # [Available Tools] menu only shows family prefixes, so "call an
            # exact name from the menu" is not actionable on its own; the
            # difflib + family-roster hint is what actually lets the agent
            # recover to the real leaf name instead of giving up on the family.
            suggestion = _suggest_for_unknown(unknown, known)
            # Put it in `output` directly (not just `error`) — on a partial
            # claim (some valid names present) success=True below, and
            # to_obs_dict/to_tool_result_dict used to only serialize `error`
            # on the failure branch, silently dropping this hint. Confirmed
            # 2026-07-26 flash-meta: 10/10 guessed desktop_* leaf names were
            # wrong, the hint was computed but never reached the agent, so it
            # abandoned the whole desktop family instead of correcting itself.
            output["hint"] = suggestion
            if valid:
                error = (
                    f"Claimed {valid}. Unknown name(s) not claimed: {unknown}. "
                    f"{suggestion}"
                )
            else:
                error = (
                    f"Unknown tool name(s), not claimed: {unknown}. {suggestion}"
                )
        return ToolResult(
            success=bool(valid),
            output=output,
            error=error,
            execution_time=time.time() - start,
            tool_name=self.name, tool_parameters=params,
        )

    @classmethod
    def get_schema(cls):
        return {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact tool name(s) to activate, e.g. "
                    "[\"schedule_create\", \"schedule_list\", \"schedule_delete\"]. "
                    "Must match a name from the [Available Tools] menu exactly — "
                    "no wildcards/family names."
                ),
            },
        }


class ReleaseToolTool(BaseTool):
    """Hide one or more tools from the visible tool list for the rest of this task.

    The tool's loaded instance stays warm — releasing then re-claiming later
    is free (no re-instantiation), mirroring _apply_self_extension.
    """

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("release_tool", ctx=ctx)

    async def execute(self, names: Any = None, **kwargs: Any) -> ToolResult:
        start = time.time()
        params = {"names": names}
        requested = _normalize_names(names)
        if not requested:
            return ToolResult(
                success=False, output=None,
                error="release_tool expects 'names': a non-empty list of exact tool names.",
                execution_time=time.time() - start,
                tool_name=self.name, tool_parameters=params,
            )

        if self.ctx is not None:
            try:
                self.ctx.pending_release_tool.extend(requested)
            except Exception:
                pass

        return ToolResult(
            success=True,
            output={"released": requested},
            execution_time=time.time() - start,
            tool_name=self.name, tool_parameters=params,
        )

    @classmethod
    def get_schema(cls):
        return {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact tool name(s) to hide from the visible tool list.",
            },
        }
