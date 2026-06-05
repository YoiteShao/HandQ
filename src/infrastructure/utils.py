"""
JSON Utilities - Shared JSON parsing helpers.

try_parse_json()
    Attempts to parse a string as JSON using json_repair.
    Returns dict if the top-level result is a dict, otherwise the original str.

llm_extract_json()
    Async fallback: asks an LLM to extract a JSON object from text that
    try_parse_json() could not parse (or parsed without the expected keys).
    Accepts the full expected JSON schema so the LLM knows the complete
    structure to return, not just the minimum required fields.
    Returns dict if successful, otherwise the original str.
"""
import json
from typing import Any, List, Optional, Union

try:
    import json_repair as _json_repair  # type: ignore[import-untyped]
    _JSON_REPAIR_AVAILABLE = True
except ImportError:  # pragma: no cover
    _json_repair = None  # type: ignore[assignment]
    _JSON_REPAIR_AVAILABLE = False

from .llm_pool import call_with_fallback


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
    """
    if not content or not content.strip():
        return content

    # Step 1: stdlib json on the (de-fenced) content.
    inner = _strip_markdown_fence(content).strip()
    if inner:
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # Step 2: json_repair fallback for malformed / fenced / multi-object cases.
    if _JSON_REPAIR_AVAILABLE and _json_repair is not None:
        try:
            parsed = _json_repair.repair_json(content, return_objects=True)
            if isinstance(parsed, dict):
                return parsed
            # Multiple JSON objects: take the FIRST dict.
            # Each agent loop must produce exactly one Decision; if the model
            # returned several, the first one is its initial (authoritative)
            # answer.  Parallel operations belong in the planner as separate
            # steps, not as multiple JSON objects in a single LLM response.
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        return item
        except Exception:
            pass

    return content


def count_json_objects(content: str) -> int:
    """
    Return the number of top-level JSON objects found in *content*.

    Used by callers (e.g. RuntimeAgent.think) to detect and log the anomaly
    where the LLM returns multiple JSON objects in a single response.

    Returns 1 when json_repair is unavailable or parsing fails (safe default).
    """
    if not content or not content.strip():
        return 0
    if not _JSON_REPAIR_AVAILABLE or _json_repair is None:
        return 1
    try:
        parsed = _json_repair.repair_json(content, return_objects=True)
        if isinstance(parsed, list):
            return len(parsed)
        return 1
    except Exception:
        return 1


async def llm_extract_json(
    content: str,
    expected_keys: List[str],
    llm_services: Any,
    schema: Optional[str] = None,
) -> Union[dict, str]:
    """
    Ask an LLM to extract a JSON object from *content*.

    Used as a fallback when try_parse_json() either failed to produce a dict
    or produced a dict that is missing the required *expected_keys*.

    Args:
        content: The raw text that could not be parsed / was missing keys.
        expected_keys: Keys the resulting dict MUST contain (used for
                       validation after the LLM responds).
        llm_services: A list of LLMService instances (tried in order via call_with_fallback).
        schema: Optional string describing the FULL expected JSON structure
                (e.g. a JSON example with field descriptions).  When provided,
                the LLM is shown the complete schema so it returns all fields,
                not just the minimum required ones.

    Returns:
        dict  -- if the LLM produced a parseable dict with all expected_keys
        str   -- the original *content* if extraction failed
    """
    try:
        schema_section = (
            f"\nThe JSON must follow this exact schema:\n{schema}\n"
            if schema
            else f"\nThe JSON must at minimum contain these fields: {', '.join(expected_keys)}\n"
        )
        extraction_prompt = (
            "Extract the JSON object from the following text. "
            "Return ONLY the raw JSON object with no additional text or markdown."
            f"{schema_section}"
            f"\nText:\n{content}"
        )
        _result = await call_with_fallback(
            llm_services,
            dict(
                messages=[{"role": "user", "content": extraction_prompt}],
                json_mode=True,
                temperature=0.0,
            ),
        )
        llm_raw: str = (_result.content or "") if hasattr(_result, "content") else str(_result)
        parsed = try_parse_json(llm_raw)
        if isinstance(parsed, dict) and all(k in parsed for k in expected_keys):
            return parsed
    except Exception:
        pass

    return content
