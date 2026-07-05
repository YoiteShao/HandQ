"""
mention_preprocessing — @-path normalization at message ingress.

These transformations consume @-prefixed tokens in the user message and run
BEFORE Stage 1 (INTENT). They share the same lookbehind discipline (avoid
@user emails, @decorators, /@paths), so bundling them here keeps the scan to
a single pass over the text.

  - normalize_at_quoted(text)
        Strips surrounding double-quotes from @"..." mentions so the user
        can write @"C:\\Program Files\\foo.txt" or @"\\\\host\\share\\my dir\\f"
        with whitespace in the path. Quoted UNC content is also UNC-normalized
        (backslashes → forward slashes). Quotes always come in pairs.

  - normalize_at_unc(text)
        Rewrites Windows UNC paths: @\\\\host\\share\\path → @//host/share/path
        so downstream consumers see a single canonical path form.

  - preprocess_mentions(text)
        One-call wrapper: quote-stripping → UNC normalization. Returns the
        normalized string. Skill @-mentions are NOT resolved or extracted here:
        under progressive disclosure the normalized @name simply rides along
        inline in the message, and the agent — which sees the [Available Skills]
        menu — decides whether to read_skill it.
"""
import re


# Quoted: @"..."  (any content between paired double quotes)
# Pairs are guaranteed by the user contract; we don't try to recover from
# unbalanced quotes — the regex simply won't match.
_AT_QUOTED_RE = re.compile(r'(?<![\w/.])@"([^"]*)"')

# UNC: @\\host\share\path
_AT_UNC_RE = re.compile(
    r"(?<![\w/.])"
    r"(@)"
    r"\\\\"
    r"([A-Za-z][A-Za-z0-9.\-]{0,62})"
    r"\\"
    r"([A-Za-z0-9_$][^\s,;<>\"'\)\]|]*)"
)


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


def preprocess_mentions(text: str) -> str:
    """Single ingress call: strip quoted @-paths, then normalize bare UNC.

    Returns the normalized text, which replaces the original message in
    conversation_history. Skill @-mentions are left inline and untouched —
    under progressive disclosure they are not force-activated; the agent sees
    the [Available Skills] menu and decides whether to read_skill them.

    Order matters:
      1. `normalize_at_quoted` strips `@"..."` → `@...`. Quoted UNC content
         is UNC-normalized in the same pass (otherwise the embedded spaces
         would defeat the unquoted UNC regex's path-boundary heuristic).
      2. `normalize_at_unc` handles bare UNC mentions.
    """
    text = normalize_at_quoted(text)
    return normalize_at_unc(text)
