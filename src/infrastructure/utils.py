"""
JSON Utilities - Shared JSON parsing helper.

try_parse_json()
    Attempts to parse a string as JSON: stdlib json.loads first, then
    json_repair library as fallback for malformed/truncated/fenced output.
    Returns dict if the top-level value is a dict, otherwise the original str.

try_parse_json_with_repair_flag()
    Same as try_parse_json but also returns whether json_repair had to
    salvage the input. Callers that treat a repaired parse as a signal of
    truncation (e.g. mid-stream cutoff) can inspect this flag.
"""
import json
from typing import Tuple, Union

import json_repair as _json_repair  # type: ignore[import-untyped]


def _strip_markdown_fence(content: str) -> str:
    """Strip a leading ``` ... ``` (optionally ```json) wrapper if present.

    Claude routinely wraps JSON output in a markdown code fence.  ``json_repair``
    handles fenced JSON natively, but ``json.loads`` does not — so we strip the
    fence here before the stdlib attempt.  Returns *content* unchanged when no
    fence is detected.
    """
    s = content.strip()
    if not s.startswith("```"):
        return content
    first_nl = s.find("\n")
    if first_nl == -1:
        return content
    inner = s[first_nl + 1:]
    # Remove a trailing fence if present (might have trailing whitespace or
    # newline characters after the ```).
    inner = inner.rstrip()
    if inner.endswith("```"):
        inner = inner[:-3].rstrip()
    return inner


def try_parse_json_with_repair_flag(content: str) -> Tuple[Union[dict, str], bool]:
    """
    Attempt to parse *content* as JSON and report whether repair was needed.

    Returns:
        (result, repaired) where:
          - result is a ``dict`` when parsing succeeded and the top-level
            value is a dict, otherwise the original *content* string.
          - repaired is ``True`` iff stdlib ``json.loads`` failed and
            ``json_repair`` had to salvage the parse. This is a strong
            signal that the input was truncated / malformed — callers can
            surface that as a partial-completion warning.

    The parsing order is identical to :func:`try_parse_json`:
      1. stdlib ``json.loads`` on the (de-fenced) content.
      2. ``json_repair.repair_json`` fallback for truncated / fenced /
         multi-object / trailing-comma cases.
    """
    if not content or not content.strip():
        return content, False

    # Step 1: stdlib json on the (de-fenced) content.
    inner = _strip_markdown_fence(content).strip()
    if inner:
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, dict):
                return parsed, False
        except Exception:
            pass

    # Step 2: json_repair fallback for malformed / fenced / multi-object cases.
    try:
        parsed = _json_repair.repair_json(content, return_objects=True)
        if isinstance(parsed, dict):
            return parsed, True
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    return item, True
    except Exception:
        pass

    return content, False


def try_parse_json(content: str) -> Union[dict, str]:
    """
    Attempt to parse *content* as JSON.

    Order of attempts:
      1. **stdlib ``json.loads``** (after stripping a markdown code fence).
         When the LLM produced well-formed JSON this preserves every escape
         sequence exactly as written — critical for paths and other strings
         that contain backslashes (e.g. Windows UNC paths ``\\\\server\\share``).
         ``json_repair`` has been observed to over-repair valid ``\\\\``
         escapes by collapsing them, silently corrupting the parsed value.
      2. **json_repair fallback** for everything stdlib cannot handle:
         - JSON embedded in surrounding prose
         - Truncated responses (missing closing braces / brackets)
         - Trailing commas, single-quoted keys, unquoted keys
         - Multiple JSON objects in one response (returns first dict)

    Returns:
        dict  -- if parsing succeeded and the top-level value is a dict
        str   -- the original *content* if parsing failed or the result is
                 not a dict (list, number, string, etc.)

    For the repair-aware variant see :func:`try_parse_json_with_repair_flag`.
    """
    result, _ = try_parse_json_with_repair_flag(content)
    return result
