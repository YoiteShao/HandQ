"""
mention_preprocessing — UNC paths + skill @-mention scanning at message ingress.

Both transformations consume @-prefixed tokens in the user message and run
BEFORE Stage 1 (INTENT). They are bundled here because they share the same
lookbehind discipline (avoid @user emails, @decorators, /@paths) and because
splitting them into separate modules would mean two regex passes scanning the
same text.

  - normalize_at_quoted(text)
        Strips surrounding double-quotes from @"..." mentions so the user
        can write @"C:\\Program Files\\foo.txt" or @"\\\\host\\share\\my dir\\f"
        with whitespace in the path. Quoted UNC content is also UNC-normalized
        (backslashes → forward slashes). Quotes always come in pairs.

  - normalize_at_unc(text)
        Rewrites Windows UNC paths: @\\\\host\\share\\path → @//host/share/path
        so downstream consumers see a single canonical path form.

  - extract_skill_mentions(text)
        Scans @name tokens and resolves them against the SkillRegistry,
        dropping unknown names. The returned set is the "prescan" — a
        defense net for Stage 1's skill commit step in case the LLM
        forgets to echo a user-mentioned skill in `skills_needed`.

  - preprocess_mentions(text)
        One-call wrapper that runs quote-stripping → UNC normalization →
        skill extraction. Returns (normalized, prescan).
"""
import re
from typing import Set, Tuple


# Quoted: @"..."  (any content between paired double quotes)
# Pairs are guaranteed by the user contract; we don't try to recover from
# unbalanced quotes — the regex simply won't match.
_AT_QUOTED_RE = re.compile(r'(?<![\w/.])@"([^"]*)"')

# UNC: @\\host\share\path  (the lookbehind is shared with skill mentions)
_AT_UNC_RE = re.compile(
    r"(?<![\w/.])"
    r"(@)"
    r"\\\\"
    r"([A-Za-z][A-Za-z0-9.\-]{0,62})"
    r"\\"
    r"([A-Za-z0-9_$][^\s,;<>\"'\)\]|]*)"
)

# Skill: @name  (lookbehind avoids @user, @decorator, /@path)
_SKILL_MENTION_RE = re.compile(r"(?<![\w/.])@([a-zA-Z0-9_\-]{1,64})")


def _normalize_quoted_at(m: "re.Match[str]") -> str:
    """Strip surrounding quotes; if content is a UNC path, normalize it.

    `@"\\\\host\\share\\file"` → `@//host/share/file`
    `@"C:\\Users\\foo bar.txt"` → `@C:\\Users\\foo bar.txt`
    `@"foo"`                   → `@foo`
    """
    content = m.group(1)
    if content.startswith("\\\\"):
        stripped = content[2:]
        sep = stripped.find("\\")
        if sep > 0:
            host = stripped[:sep]
            rest = stripped[sep + 1:].replace("\\", "/")
            return f"@//{host}/{rest}"
    return f"@{content}"


def normalize_at_quoted(text: str) -> str:
    """Rewrite `@"..."` → `@...` (strip surrounding double-quotes).

    Lets users write paths with whitespace, e.g.
    `@"C:\\Program Files\\foo.txt"` or `@"\\\\host\\share\\my dir\\f"`.
    UNC content inside quotes is normalized in the same step (quote stripping
    would otherwise leave the unquoted UNC regex unable to find the path
    boundary at the embedded space).
    """
    return _AT_QUOTED_RE.sub(_normalize_quoted_at, text)


def normalize_at_unc(text: str) -> str:
    """Rewrite @\\\\host\\share\\path → @//host/share/path."""
    def _sub(m: "re.Match[str]") -> str:
        host = m.group(2)
        rest = m.group(3).replace("\\", "/")
        return f"{m.group(1)}//{host}/{rest}"
    return _AT_UNC_RE.sub(_sub, text)


def extract_skill_mentions(text: str) -> Set[str]:
    """Return registered skill names mentioned via @name in text.

    Unknown @-mentions are filtered out — only names that resolve in
    the SkillRegistry survive. Returns empty set on registry import error.
    """
    if not text or "@" not in text:
        return set()
    try:
        from ..infrastructure.skills import SkillRegistry
        registry = SkillRegistry.get()
    except Exception:
        return set()
    found: Set[str] = set()
    for match in _SKILL_MENTION_RE.findall(text):
        if registry.has(match):
            found.add(match)
    return found


def preprocess_mentions(text: str) -> Tuple[str, Set[str]]:
    """Single ingress call: strip quoted @-paths, normalize UNC, scan skills.

    Returns (normalized_text, prescan_skills). The normalized text replaces
    the original message in conversation_history; prescan_skills is threaded
    into Stage 1's commit step so user-mentioned skills survive even when
    the LLM forgets to list them.

    Order matters:
      1. `normalize_at_quoted` strips `@"..."` → `@...`. Quoted UNC content
         is UNC-normalized in the same pass (otherwise the embedded spaces
         would defeat the unquoted UNC regex's path-boundary heuristic).
      2. `normalize_at_unc` handles bare UNC mentions.
      3. `extract_skill_mentions` scans the now-canonical text for skill names.
    """
    text = normalize_at_quoted(text)
    normalized = normalize_at_unc(text)
    prescan = extract_skill_mentions(normalized)
    return normalized, prescan
