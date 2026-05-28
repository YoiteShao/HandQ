"""PII / secrets pre- and post-filter.

v1 strategy: pure regex, conservative bias toward false negatives over false
positives so that triage isn't constantly rejecting normal user prose. The
patterns target structural shapes — credit-card digit groups, JWT triplets,
known token prefixes (sk-..., ghp_..., AKIA..., xox?-...).

P5 will add an ONNX PII NER model and an app-level blocklist. Code in this
module is intentionally a single class so the upgrade can swap implementation
behind ``has_secret`` and ``redact``.
"""
from __future__ import annotations

import re
from typing import List, Pattern


SECRET_PATTERNS: List[Pattern] = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                                    # AWS access key id
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),                # OpenAI / Anthropic
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                                  # GitHub PAT
    re.compile(r"gho_[A-Za-z0-9]{36}"),                                  # GitHub OAuth
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),                             # GitLab PAT
    re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{20,}"),                       # Slack
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    # Credit-card-ish: 13–19 digits in groups separated by space/dash.
    # Bias toward the longer Luhn-shaped span; short numeric runs trigger noise.
    re.compile(r"\b(?:\d[ -]?){15,19}\d\b"),
]


class PIIFilter:
    """Stateless detector. Cheap to instantiate (one per LongTermMemory)."""

    def __init__(self, patterns: List[Pattern] = SECRET_PATTERNS) -> None:
        self._patterns = patterns

    def has_secret(self, text: str) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in self._patterns)

    def redact(self, text: str) -> str:
        """Return *text* with each match replaced by ``[REDACTED]``.

        Used by callers that want to log or display a candidate without
        storing the raw secret. The current store path always rejects
        candidates with secrets rather than redacting in-place — that is
        deliberate: a redacted memory entry is not useful enough to keep.
        """
        out = text
        for p in self._patterns:
            out = p.sub("[REDACTED]", out)
        return out
