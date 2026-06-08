"""Reactive compaction policy — *when* and *how much* to compress agent history.

This is the v2 answer to context-window management (PROGRESS.md §5, ARCHITECTURE
§7). It is a **pure decision function**: given the current per-observation sizes,
it decides whether to compact and how many recent observations to keep so the
post-compaction size lands inside a target *hysteresis band*. It calls no LLM and
mutates no state — ``RuntimeAgent`` still owns the actual summarization machinery
(LLM semantic summary, KV-cache-safe message rebuild, supersession elision, the
prompt-too-long backstop). This object only answers the trigger question.

Why a separate policy, benchmarked against the field:

* **Claude Code** uses a single high threshold (~auto-compact near the window
  limit) and then summarizes the whole conversation in one shot. Simple, but it
  fires late and re-summarizes from a near-full window — expensive and a stall
  right when the task is busiest.
* **Yansu** externalizes everything: per-turn / per-tool ``stream_items`` are
  persisted and the working context is reconstructed from summaries, so the live
  context never has to hold the full trace.
* **HandQ v1** (``_compact_old_observations``) was already size-reactive at 80%
  but had two wrong parts we discard here: (a) it kept a *fixed* last-10
  observations regardless of their size, so it would re-trigger almost every turn
  once near the limit (no hysteresis), and (b) it ran an extra proactive pass on
  an iteration *timer* (every 50 turns) — non-reactive churn. Its valuable parts
  (semantic summary, cache discipline, supersession, PTL recovery) are kept.

Our policy keeps the externalized spirit of Yansu and the semantic summary of
v1/Claude Code, but adds a **hysteresis band** so compaction is rare and decisive:

    trigger at TRIGGER_PCT (default 60%)  ── compact ──►  land at ~TARGET_PCT (35%)
    hard backstop at HARD_DROP_PCT (90%)  ── drop, skip the LLM, never stall

Because we compact *down to* the target band (not to a fixed count), the next
trigger is many turns away — the window breathes instead of thrashing.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import CompactionConfig


@dataclass(frozen=True)
class CompactionDecision:
    """The policy's verdict for one compaction check.

    ``keep_recent`` newest observations survive verbatim; everything older is
    folded into one summary (or dropped when ``hard_drop``). ``should_compact``
    False means leave history untouched.
    """

    should_compact: bool
    keep_recent: int = 0
    hard_drop: bool = False
    reason: str = ""


class CompactionPolicy:
    """Decides when to compact and how many recent observations to retain.

    Parameters
    ----------
    config:
        The trigger/target/backstop fractions (``CompactionConfig``).
    budget_chars:
        Serialized-history budget in characters (RuntimeAgent derives this from
        the model's token window; we mirror its ``_OBS_BUDGET_CHARS``).
    min_observations:
        Never compact a history shorter than this — not worth the summary call,
        and a freshly-started agent should keep its full short trace.
    min_keep_recent:
        Floor on retained recent observations, so continuity of the agent's
        immediate working memory is preserved even when those observations are
        individually large.
    """

    def __init__(
        self,
        config: CompactionConfig,
        *,
        budget_chars: int,
        min_observations: int = 15,
        min_keep_recent: int = 4,
    ) -> None:
        self._config = config
        self._budget_chars = max(1, budget_chars)
        self._min_observations = min_observations
        self._min_keep_recent = min_keep_recent

    def decide(self, *, obs_sizes: list[int]) -> CompactionDecision:
        """Pure trigger decision from per-observation char sizes (newest last)."""
        n = len(obs_sizes)
        if n <= self._min_observations:
            return CompactionDecision(False, reason=f"only {n} obs (<= {self._min_observations})")

        total = sum(obs_sizes)
        ratio = total / self._budget_chars
        if ratio < self._config.trigger_pct:
            return CompactionDecision(
                False, reason=f"at {ratio:.0%} (< trigger {self._config.trigger_pct:.0%})"
            )

        keep = self._keep_for_target(obs_sizes)
        # Guarantee progress: there must be at least one old observation to fold,
        # otherwise compaction is a no-op that would re-fire next turn.
        keep = min(keep, n - 1)
        hard = ratio >= self._config.hard_drop_pct
        reason = (
            f"at {ratio:.0%} (>= {'hard-drop ' + format(self._config.hard_drop_pct, '.0%') if hard else 'trigger ' + format(self._config.trigger_pct, '.0%')}); "
            f"keep {keep}/{n} recent to land near target {self._config.target_pct:.0%}"
        )
        return CompactionDecision(True, keep_recent=keep, hard_drop=hard, reason=reason)

    def _keep_for_target(self, obs_sizes: list[int]) -> int:
        """Largest count of newest observations whose chars stay under the target.

        This is the hysteresis: after folding everything older into a (small)
        summary, the live history is ~target_pct of budget, so the next trigger
        is far away instead of one turn later.
        """
        target_chars = self._budget_chars * self._config.target_pct
        kept = 0
        acc = 0
        for size in reversed(obs_sizes):
            if kept >= self._min_keep_recent and acc + size > target_chars:
                break
            acc += size
            kept += 1
        return max(self._min_keep_recent, kept)
