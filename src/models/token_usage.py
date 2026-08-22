"""
TokenUsage — unified token-count data structure.

Tracks all token categories produced by a single LLM call or accumulated
across multiple calls:

  input_tokens           — prompt tokens billed at the standard input rate
  output_tokens          — generated tokens billed at the output rate
  cache_creation_tokens  — tokens written to the Anthropic prompt cache
  cache_read_tokens      — tokens read from the Anthropic prompt cache

total_tokens is a derived property (input + output) and is never stored,
so it can never diverge from the component fields.

Accumulation
------------
TokenUsage supports += so callers can accumulate across iterations:

    total = TokenUsage()
    for result in llm_results:
        total += TokenUsage.from_llm_result(result)

Serialisation
-------------
to_dict() returns a flat dict with all five keys (including total_tokens)
for backward-compatible JSON output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def total_tokens(self) -> int:
        """Total billed tokens (input + output). Cache tokens are not included."""
        return self.input_tokens + self.output_tokens

    # ── Arithmetic ────────────────────────────────────────────────────────────

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a flat dict including the derived total_tokens field."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_llm_result(cls, result: object) -> "TokenUsage":
        """Construct from an LLMChatResult.

        Handles the Anthropic SDK naming convention where cache fields are
        named ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
        rather than the shorter form used everywhere else.

        Returns an empty TokenUsage when *result* is None.
        """
        if result is None:
            return cls()
        return cls(
            input_tokens=getattr(result, "input_tokens", 0) or 0,
            output_tokens=getattr(result, "output_tokens", 0) or 0,
            cache_creation_tokens=getattr(result, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(result, "cache_read_input_tokens", 0) or 0,
        )


def flatten_model_stats(stats: Dict[str, TokenUsage]) -> List[dict]:
    """``{model_name: TokenUsage}`` -> a JSON-safe list, heaviest model first.

    The single place that turns non-serialisable ``TokenUsage`` dataclasses
    into plain dicts, so every UI delegate (local ``_StdioUI`` and remote
    ``NetworkUIDelegate`` alike) receives identical, wire-safe data rather
    than each doing its own ad-hoc flattening.
    """
    if not isinstance(stats, dict):
        return []
    return [
        {"model": name, **usage.to_dict()}
        for name, usage in sorted(
            stats.items(), key=lambda kv: kv[1].total_tokens, reverse=True,
        )
    ]
