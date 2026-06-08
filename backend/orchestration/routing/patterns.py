"""Execution patterns — the ~5-8 recurring skeletons the router classifies into.

ARCHITECTURE.md §5: user *requests* are infinite; execution *patterns* are few.
The router maps a goal onto one of these patterns (or to ``freeform`` → single
loop). A pattern is NOT a runnable graph — it is the recognizable skeleton plus
the exemplars an embedding matcher scores against. Templates (§6) are the
runnable, parameterized realization of a pattern and are grown by the detector,
not authored here.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Reserved id for "no recognized pattern" — always routes to a single AgentNode.
FREEFORM = "freeform"


@dataclass(frozen=True)
class Pattern:
    """A recurring execution skeleton.

    Attributes
    ----------
    id:
        Stable identifier (also the templates/ module name when promoted).
    skeleton:
        Ordered phase labels describing the canonical flow — documentation and
        the basis a template's node layout mirrors.
    exemplars:
        Representative goal phrasings. The router's embedding matcher scores an
        incoming goal against these; high similarity = high-confidence match.
    """

    id: str
    skeleton: tuple[str, ...]
    exemplars: tuple[str, ...] = field(default_factory=tuple)


# The initial catalogue (ARCHITECTURE.md §5). Cold start ships these as
# classification targets only; none has a hand-authored template except
# ``modify`` (the one real template). The rest degrade to single-loop until the
# detector promotes them.
PATTERNS: dict[str, Pattern] = {
    "modify": Pattern(
        id="modify",
        skeleton=("locate", "understand", "change", "verify"),
        exemplars=(
            "fix the bug in the login handler",
            "rename this function everywhere it is used",
            "add a parameter to the export endpoint",
            "refactor the parser to handle empty input",
        ),
    ),
    "research": Pattern(
        id="research",
        skeleton=("gather", "synthesize", "report"),
        exemplars=(
            "compare the top three vector databases",
            "summarize how our competitors price their API",
            "find out what changed in the latest release",
        ),
    ),
    "etl": Pattern(
        id="etl",
        skeleton=("extract", "transform", "load"),
        exemplars=(
            "pull the sales CSV, clean it, and load it into the warehouse",
            "convert these logs into a parquet dataset",
            "scrape the table and write it to a spreadsheet",
        ),
    ),
    "diagnose": Pattern(
        id="diagnose",
        skeleton=("probe", "hypothesize", "fix", "confirm"),
        exemplars=(
            "the deploy is failing, find out why and fix it",
            "tests pass locally but break in CI, figure out what's wrong",
            "the service is slow under load, diagnose it",
        ),
    ),
    "watch_act": Pattern(
        id="watch_act",
        skeleton=("observe", "detect", "act"),
        exemplars=(
            "watch the queue and alert me when it backs up",
            "monitor the build and notify the channel on failure",
            "keep an eye on disk usage and clean up when it's high",
        ),
    ),
    "audit": Pattern(
        id="audit",
        skeleton=("scan", "converge", "verdict"),
        exemplars=(
            "audit this module for security vulnerabilities",
            "review the codebase for bugs and report what you find",
            "check this PR for issues across correctness and style",
            "scan the service for missing error handling",
        ),
    ),
}


def get(pattern_id: str) -> Pattern | None:
    return PATTERNS.get(pattern_id)


def all_exemplars() -> list[tuple[str, str]]:
    """Flat ``(pattern_id, exemplar_text)`` list for the embedding matcher."""
    return [(s.id, ex) for s in PATTERNS.values() for ex in s.exemplars]
