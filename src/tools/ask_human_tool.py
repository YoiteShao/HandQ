# -*- coding: utf-8 -*-
"""AskHumanTool — open a modal asking the user a clarifying question.

Restraint contract
------------------
This tool interrupts the user. The system prompt's usage_guide instructs the
agent to default to deciding silently from context and to call this tool ONLY
when the task literally cannot proceed without information that cannot be
derived. The runtime does not try to enforce that rule programmatically — it
trusts the agent to follow the guidance — but the result of every ask_human
call goes back to the agent verbatim, so spurious prompts produce visible UX
regressions and are easy to spot.

Runtime model
-------------
The tool delegates to ``InteractionManager.request_user_form(question, fields)``:

  - In GUI mode (Electron bridge) the IM forwards a ``kind: "ask_human"``
    envelope to the renderer, which renders ``question`` as markdown and
    ``fields`` as labeled text/textarea/radio/checkbox controls in the same
    overlay used for the secret-input flow. The user fills them in and clicks
    Send; the answer (one value per field id) flows back through the
    confirmation queue.

  - In CLI / no-UI mode ``request_user_form`` falls back to a stderr prompt
    plus a blocking read on the confirmation queue. The fallback exists so
    importing the tool never crashes; callers get a low-friction experience
    when the GUI is wired up and a workable one when it isn't.

Fields
------
``fields`` lets the agent ask several distinct questions in one call instead
of being limited to a single line the user must cram everything into. Each
entry is ``{"id", "label", "type": "text"|"textarea"|"radio"|"checkbox",
"options": [...]  # radio/checkbox only, "placeholder": ...  # optional}``.
When the agent omits ``fields`` (the common one-question case), the tool
synthesizes a single implicit textarea field so the call still round-trips
through the same ``request_user_form`` contract. The reply is then flattened
back to a bare string — every caller that only ever asked one plain question
keeps seeing ``ToolResult.output`` as a string, not a single-key dict.

Concurrency
-----------
The V2 ``InteractionManager.request_user_form`` is a coroutine — it awaits a
bridge-side future that resolves when the user submits the modal — so
``execute()`` awaits it directly without an executor; the event loop stays
free while we wait.

Timeout
-------
``asyncio.wait_for()`` caps the wait at ``ASK_HUMAN_TIMEOUT_S`` seconds.
On timeout the tool returns ``success=False`` with an explicit instruction to
proceed with a sensible default, so an unattended task never stalls forever.
Each UI delegate (``_StdioUI`` for the local Electron bridge,
``NetworkUIDelegate`` for a Connect-panel remote session) independently
enforces the same ceiling on its own await of the modal reply and, on its own
timeout, emits an ``ask_human_expired`` envelope so the renderer closes the
stale modal and leaves a transcript record — see ``stdio_bridge.py``'s
``_await_user_response`` and ``remote_control/network_delegate.py``'s
``_ask``. This constant is the single source of truth both delegates import
rather than duplicate.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .base_tool import BaseTool, ToolResult
from ..infrastructure.logger import get_logger

# Hard ceiling for blocking on user input. Imported by stdio_bridge.py and
# remote_control/network_delegate.py so every UI delegate enforces the exact
# same timeout as this tool's own asyncio.wait_for below — see the module
# docstring's "Timeout" section for why each delegate needs its own copy of
# this deadline rather than relying solely on this tool's cancellation to
# propagate down through two more layers of delegate.
ASK_HUMAN_TIMEOUT_S: int = 1800  # 30 minutes

_FIELD_TYPES = ("text", "textarea", "radio", "checkbox")
_IMPLICIT_FIELD_ID = "answer"


def _validate_fields(fields: Any) -> "tuple[list, str]":
    """Normalize + validate a caller-supplied ``fields`` list.

    Returns ``(fields, error)``. ``error`` is non-empty and ``fields`` is `[]`
    when the input is malformed enough that asking would render a broken
    modal (missing id/type, radio/checkbox with no options).
    """
    if not isinstance(fields, list) or not fields:
        return [], ""
    seen_ids = set()
    out = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            return [], f"fields[{i}] must be an object."
        fid = str(f.get("id") or "").strip()
        ftype = str(f.get("type") or "").strip()
        if not fid:
            return [], f"fields[{i}] is missing 'id'."
        if fid in seen_ids:
            return [], f"fields[{i}]: duplicate field id {fid!r}."
        if ftype not in _FIELD_TYPES:
            return [], f"fields[{i}] ({fid}): 'type' must be one of {_FIELD_TYPES}."
        options = f.get("options")
        if ftype in ("radio", "checkbox"):
            if not isinstance(options, list) or not options:
                return [], f"fields[{i}] ({fid}): '{ftype}' requires a non-empty 'options' list."
            options = [str(o) for o in options]
        seen_ids.add(fid)
        out.append({
            "id": fid,
            "label": str(f.get("label") or ""),
            "type": ftype,
            "options": options if ftype in ("radio", "checkbox") else None,
            "placeholder": str(f.get("placeholder") or "") if f.get("placeholder") else None,
        })
    return out, ""


class AskHumanTool(BaseTool):
    """Ask the user a clarifying question — optionally with structured
    fields — and return their reply."""

    is_read_only = True
    is_concurrency_safe = False

    parameter_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "What to ask the user, in markdown (rendered in the "
                    "modal — headings, lists, bold, code, etc. all work). "
                    "Phrase it as the actual text the user will see — no "
                    "prefix like 'I need to ask:' and no chain-of-thought. "
                    "If you have several distinct questions, put the shared "
                    "context/framing here and use 'fields' for the actual "
                    "per-question inputs instead of cramming everything "
                    "into this one string."
                ),
            },
            "fields": {
                "type": "array",
                "description": (
                    "Optional structured inputs, rendered as labeled "
                    "controls below the question. Omit for a single "
                    "free-text answer. Each item: "
                    "{id, label, type: 'text'|'textarea'|'radio'|'checkbox', "
                    "options: [...] (required for radio/checkbox), "
                    "placeholder: '...' (optional)}. The reply is a "
                    "{field_id: value} object — checkbox values are arrays "
                    "of the selected option strings, everything else is a "
                    "plain string."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": list(_FIELD_TYPES),
                        },
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "placeholder": {"type": "string"},
                    },
                    "required": ["id", "type"],
                },
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None) -> None:
        super().__init__("ask_human", ctx=ctx)
        self.logger = get_logger()

    async def execute(self, **kwargs: Any) -> ToolResult:
        question = kwargs.get("question", "")
        if not isinstance(question, str) or not question.strip():
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error="ask_human requires a non-empty 'question' string.",
            )

        fields, field_error = _validate_fields(kwargs.get("fields"))
        if field_error:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=f"ask_human: invalid 'fields' — {field_error}",
            )
        implicit_field = not fields
        if implicit_field:
            fields = [{
                "id": _IMPLICIT_FIELD_ID,
                "label": "",
                "type": "textarea",
                "options": None,
                "placeholder": None,
            }]

        # ctx + IM are injected by the SessionContext wiring; the per-session
        # InteractionManager forwards to the renderer (GUI) or stderr (CLI).
        im = getattr(self.ctx, "interaction_manager", None) if self.ctx else None
        if im is None:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=(
                    "ask_human is unavailable (no interaction manager in this "
                    "session). Pick a sensible default and continue."
                ),
            )

        try:
            answers = await asyncio.wait_for(
                im.request_user_form(question.strip(), fields),
                timeout=ASK_HUMAN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=(
                    f"No answer within {ASK_HUMAN_TIMEOUT_S}s — the user may be "
                    "away. Proceed with a sensible default."
                ),
            )

        answers = answers if isinstance(answers, dict) else {}
        if implicit_field:
            answer = str(answers.get(_IMPLICIT_FIELD_ID) or "").strip()
            if not answer:
                return ToolResult(
                    success=False,
                    output=None,
                    tool_name=self.name,
                    tool_parameters=kwargs,
                    error=(
                        "The user dismissed the question without answering. "
                        "Proceed with a sensible default."
                    ),
                )
            return ToolResult(
                success=True,
                output=answer,
                tool_name=self.name,
                tool_parameters=kwargs,
            )

        if not answers:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=(
                    "The user dismissed the question without answering. "
                    "Proceed with a sensible default."
                ),
            )
        return ToolResult(
            success=True,
            output=answers,
            tool_name=self.name,
            tool_parameters=kwargs,
        )
