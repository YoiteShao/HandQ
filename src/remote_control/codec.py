"""Codecs for values that cross the control channel.

Two jobs, both narrow:

**Confirmation results.** ``request_risk_confirmation`` / ``request_tool_confirmation``
return a :class:`UserConfirmation`; ``request_secret_input`` returns a bare
``str``; ``request_user_form`` returns a ``{field_id: value}`` dict. The
controlling side produces whichever its local ``_StdioUI`` produced and the
controlled side must reconstruct the same type, so the wire value is tagged rather
than positional.

**Argument sanitisation.** ``infrastructure.chatroom.protocol.encode`` already
passes ``default=str``, so an unserialisable value (a ``Path``, ``bytes``, a
coordinate tuple) can no longer raise ``TypeError`` mid-encode — which was the
failure the design doc's §9.3 warned about, where the exception would be
swallowed by ``InteractionManager._ui_call`` and the event would vanish. What
``default=str`` does *not* solve is size: a single ``read`` result or a base64
screenshot in a tool ``output`` would otherwise be copied verbatim into both the
wire frame and the retained event log. :func:`safe_args` bounds each value with a
visible marker, so a truncation is something the operator can see rather than a
silent difference between the local and remote views of the same session.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..controller_v2.user_confirmation import ConfirmationType, UserConfirmation

#: Per-value ceiling for anything crossing the wire. Chosen well above what a
#: human reads in a tool-result panel and well below what hurts to hold 50k of.
MAX_VALUE_CHARS = 256_000

#: Tool-confirmation ``params`` are rendered in a modal, matching the local
#: ``_StdioUI._trunc`` ceiling at ``stdio_bridge.py:813``.
MAX_PARAM_CHARS = 200


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}…[truncated {dropped} chars]"


def safe_value(value: Any, limit: int = MAX_VALUE_CHARS) -> Any:
    """Bound one delegate argument, preserving JSON shape where it is cheap to.

    Containers are walked so a dict of small strings stays a dict (the renderer
    branches on type for tool params and results); only leaf strings are
    truncated. Non-JSON leaf types are left alone — ``encode``'s ``default=str``
    handles them, and stringifying here would lose the renderer's ability to
    distinguish a number from its text.
    """
    if isinstance(value, str):
        return _truncate(value, limit)
    if isinstance(value, dict):
        return {str(k): safe_value(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v, limit) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _truncate(str(value), limit)


def safe_args(args: List[Any]) -> List[Any]:
    """Sanitise a whole delegate argument list."""
    return [safe_value(a) for a in args]


def safe_params(params: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Tool-confirmation ``params`` → the same shape the local modal receives.

    Mirrors ``_StdioUI.request_tool_confirmation``'s ``_trunc`` comprehension
    (``stdio_bridge.py:813-818``) exactly: every value becomes a truncated
    string. That flattening is the delegate's own obligation, not the
    InteractionManager's — ``interaction_manager.py:335`` forwards ``params or {}``
    untouched — so each delegate implementation has to do it independently.
    """
    return {
        str(k): _truncate(str(v), MAX_PARAM_CHARS)
        for k, v in (params or {}).items()
    }


# ── Confirmation results ─────────────────────────────────────────────────────

def encode_confirmation(result: UserConfirmation) -> Dict[str, Any]:
    """:class:`UserConfirmation` → wire dict."""
    return {
        "kind": "confirmation",
        "type": result.confirmation_type.value,
        "message": result.message,
    }


def encode_text(result: str) -> Dict[str, Any]:
    """Secret answer → wire dict."""
    return {"kind": "text", "text": str(result or "")}


def encode_form(result: Dict[str, Any]) -> Dict[str, Any]:
    """``request_user_form`` answer (``{field_id: value}``) → wire dict."""
    return {"kind": "form", "fields": result if isinstance(result, dict) else {}}


def decode_confirmation(value: Any) -> UserConfirmation:
    """Wire dict → :class:`UserConfirmation`.

    Anything unrecognised decodes to ``no()``. That default is deliberate and
    matches ``InteractionManager``'s own (``interaction_manager.py:333``): a
    corrupt or unexpected answer must never read as approval.
    """
    if not isinstance(value, dict):
        return UserConfirmation.no()
    raw_type = str(value.get("type") or "").strip().lower()
    message = value.get("message")
    message = str(message) if message is not None else None
    try:
        kind = ConfirmationType(raw_type)
    except ValueError:
        return UserConfirmation.no()
    if kind is ConfirmationType.YES:
        return UserConfirmation.yes()
    if kind is ConfirmationType.MESSAGE:
        return UserConfirmation.with_message(message or "")
    if kind is ConfirmationType.RISK_GUIDANCE:
        return UserConfirmation.risk_guidance(message or "")
    return UserConfirmation.no()


def decode_text(value: Any) -> str:
    """Wire dict → the plain string ``request_secret_input`` is contracted to
    return. Default ``""`` matches ``interaction_manager.py``'s own default."""
    if isinstance(value, dict):
        return str(value.get("text") or "")
    if isinstance(value, str):
        return value
    return ""


def decode_form(value: Any) -> Dict[str, Any]:
    """Wire dict → the ``{field_id: value}`` dict ``request_user_form`` is
    contracted to return. Default ``{}`` matches ``InteractionManager``'s own
    default when no delegate answers."""
    if isinstance(value, dict):
        fields = value.get("fields")
        if isinstance(fields, dict):
            return fields
    return {}


#: Which decoder each parked ``request_*`` needs when its answer arrives.
TEXT_METHODS = frozenset({"request_secret_input"})
FORM_METHODS = frozenset({"request_user_form"})


def decode_for_method(method: str, value: Any) -> Any:
    """Decode a ``confirm_response`` value according to the method that asked."""
    if method in TEXT_METHODS:
        return decode_text(value)
    if method in FORM_METHODS:
        return decode_form(value)
    return decode_confirmation(value)
