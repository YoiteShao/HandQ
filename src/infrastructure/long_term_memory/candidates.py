"""Convenience helpers for submitting candidates from the various trigger
points. Each helper formats raw_text + hint + metadata for its source kind
and dispatches to ``LongTermMemory.submit_candidate``.

Source values come from :class:`models.CandidateSource` so adding a new
trigger point requires updating the enum first — preventing typos from
silently creating untracked source strings.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from . import _constants as C
from .models import CandidateSource

_logger = logging.getLogger("handq.ltm.candidates")


# ── Per-message capture pre-filter knobs ────────────────────────────────────
#
# Tuned against real-world data: ~50% receptionist_turn acceptance
# (against a "default skip" prompt) was driven by short clarifying /
# acknowledgement messages slipping past the LLM. A length floor + an
# imperative-keyword OR catches the obvious noise without burning
# triage budget on it. The keyword lists are deliberately narrow —
# false-rejecting a real preference is fine because the user can
# /remember it explicitly.
_MIN_RECEPTIONIST_CHARS: int = 20
_IMPERATIVE_KEYWORDS_EN = (
    "always", "never", "prefer", "from now on", "stop ", "use ",
    "don't ", "dont ", "do not", "instead of", "switch to", "remember",
)
_IMPERATIVE_KEYWORDS_ZH = (
    "总是", "永远", "永不", "不要", "别", "改用", "改成", "以后",
    "记住", "记一下", "习惯", "我喜欢", "我希望", "我想要",
    "替换为", "切换到",
)


def _is_trivial_session(*, last_steps) -> Optional[str]:
    """Multi-signal heuristic for session_complete: return a reason
    string if this session is too trivial to be worth triaging, else
    None.

    The previous version counted "substantive steps" (any step with
    artifacts / factual_outcome / key_findings). It had two failure
    modes worth fixing:
      - One genuinely productive write step (e.g. "implement feature X
        in file Y", produces 1 artifact + outcome) was rejected because
        it's only 1 step.
      - Three steps with empty placeholder outputs squeaked through.

    The new scoring weighs *what was produced* over *how many turns*.
    Signals (sum to a single ``score``):

        +2.0 per write/edit/create/implement/refactor step
        +1.0 per other step that has any structured output
        +0.3 per pure read/search/list step (mostly informational)
        +0.5 per DISTINCT artifact path
        +0..5 for total output volume (factual_outcome + key_findings
              text length, capped — diminishing returns past 2.5 KB)

    REJECT when score < TRIVIAL_SCORE_THRESHOLD (default 4.0).
    The reason string returned to logs encodes the score so an
    operator can see why a real-looking session was filtered out.
    """
    if not last_steps:
        return "no_steps"

    # ── Categorise verbs we look for in step descriptions.
    write_verbs = (
        "write", "edit", "create", "implement",
        "fix", "refactor", "modify", "delete", "add", "remove",
    )
    read_verbs = (
        "search", "find", "list", "read", "view", "inspect",
        "check", "look", "browse",
    )

    score = 0.0
    artifacts: set = set()
    total_output_chars = 0
    write_steps = 0
    read_steps = 0
    other_steps = 0

    for s in last_steps:
        # Step type via verb in description.
        desc = (getattr(s, "description", "") or "").lower()
        is_write = any(v in desc for v in write_verbs)
        is_read = any(v in desc for v in read_verbs)
        has_output = bool(
            getattr(s, "artifacts", None)
            or getattr(s, "factual_outcome", None)
            or getattr(s, "key_findings", None)
        )

        if is_write:
            write_steps += 1
            score += 2.0
        elif is_read and not has_output:
            read_steps += 1
            score += 0.3
        elif has_output:
            other_steps += 1
            score += 1.0
        # else: step has no signal at all — no score change.

        # Distinct artifacts (de-duplicated across the session).
        for a in (getattr(s, "artifacts", None) or []):
            artifacts.add(str(a))
        # Total textual output across factual_outcome + key_findings.
        for key in ("factual_outcome", "key_findings"):
            v = getattr(s, key, None)
            if not v:
                continue
            if isinstance(v, list):
                total_output_chars += sum(len(str(x)) for x in v)
            else:
                total_output_chars += len(str(v))

    score += len(artifacts) * 0.5
    # Diminishing returns past ~2.5 KB. A 200-char outcome is barely
    # worth 0.4; a 5000-char one is worth 5.0 (capped). This rewards
    # genuinely substantial output without letting a single huge dump
    # game the score.
    score += min(total_output_chars / 500.0, 5.0)

    if score < C.TRIVIAL_SCORE_THRESHOLD:
        return (
            f"trivial_score={score:.1f}"
            f"_writes={write_steps}_reads={read_steps}"
            f"_artifacts={len(artifacts)}_chars={total_output_chars}"
        )
    return None


async def submit_session_complete(
    *,
    ltm,
    session_dir: str,
    goal: str,
    summary: str,
    last_steps: Optional[List] = None,
    success: bool,
) -> str:
    """Submit a completed session as a triage candidate.

    The ``source`` is split by outcome so the triage prompt can apply
    different conservatism to each path:

    - ``session_complete`` (success=True): mine durable user preferences and
      reusable team/project facts. Trivial sessions are pre-filtered out
      before reaching the LLM (see :func:`_is_trivial_session`).
    - ``session_failed``  (success=False): mine ONLY the lesson — no user
      preferences, because user actions on a failed task are evidence of
      what didn't work, not what they want next time. Failed sessions
      bypass the trivial-session filter (a 1-line failure may carry a
      genuine env-constraint lesson).
    """
    last_steps = last_steps or []

    # Pre-filter ONLY successful sessions. Failed sessions might be very
    # short (one step that hit the env limit) but carry a real lesson, so
    # we let them all go through the LLM to decide.
    if success:
        trivial_reason = _is_trivial_session(last_steps=last_steps)
        if trivial_reason is not None:
            _logger.info(
                "session_complete pre-filtered as trivial: %s | goal=%r",
                trivial_reason, (goal or "")[:60],
            )
            return ""

    raw_text = _render_session(goal, summary, last_steps, success)
    if success:
        source = CandidateSource.SESSION_COMPLETE
        hint = (
            "Session just completed successfully. Look for stable user "
            "preferences (memory) and reusable team/project conventions "
            "(knowledge). Reject one-off task details."
        )
    else:
        source = CandidateSource.SESSION_FAILED
        hint = (
            "Session ENDED IN FAILURE. Do NOT promote any user behaviour as a "
            "preference — failure context is not consent. Look only for "
            "reusable knowledge about why the approach failed (environmental "
            "constraints, library bugs, missing prerequisites). When in doubt, "
            "reject."
        )
    return await ltm.submit_candidate(
        source=source.value,
        source_ref=session_dir,
        raw_text=raw_text,
        hint=hint,
        metadata={"success": success, "step_count": len(last_steps)},
    )


async def submit_user_turn(
    *,
    ltm,
    msg_id: str,
    user_message: str,
    current_goal: Optional[str] = None,
) -> str:
    # Pre-filter trivial chatter before paying for an LLM triage call.
    # Every user message gets captured here, so most are noise (small
    # talk, clarifying questions, error pastes). Default-skip in the
    # prompt isn't enough — the LLM still accepted ~50% in practice.
    # Drop here when BOTH:
    #   1. the message is short (< MIN_RECEPTIONIST_CHARS), AND
    #   2. it contains no imperative / preference keyword (en + zh)
    # Either condition alone is too strict (a long error paste IS noise;
    # a short "always lint" IS a preference). Both together robustly
    # catch the noisy short-message bucket without dropping real signal.
    text = (user_message or "").strip()
    if (
        len(text) < _MIN_RECEPTIONIST_CHARS
        and not any(kw in text.lower() for kw in _IMPERATIVE_KEYWORDS_EN)
        and not any(kw in text for kw in _IMPERATIVE_KEYWORDS_ZH)
    ):
        _logger.info(
            "receptionist_turn pre-filtered as trivial: len=%d msg=%r",
            len(text), text[:60],
        )
        return ""

    parts = [f"# User message\n[SELF] {user_message}"]
    if current_goal:
        parts.append(f"# Current goal context\n{current_goal[:300]}")
    raw_text = "\n\n".join(parts)
    return await ltm.submit_candidate(
        source=CandidateSource.RECEPTIONIST_TURN.value,
        source_ref=msg_id,
        raw_text=raw_text,
        hint=(
            "User just sent a message during a conversation. "
            "Promote only explicit, durable preferences."
        ),
    )


async def submit_manual(*, ltm, text: str, ref: str = "") -> str:
    return await ltm.submit_candidate(
        source=CandidateSource.MANUAL_REMEMBER.value,
        source_ref=ref or None,
        raw_text=f"# User explicitly asked to remember\n[SELF] {text}",
        hint="High-priority candidate from explicit /remember command.",
    )


async def submit_post_commit(
    *,
    ltm,
    sha: str,
    msg: str,
    diff_stat: str,
    author_is_self: bool,
) -> str:
    tag = "[SELF]" if author_is_self else "[OTHER]"
    raw_text = (
        f"# Git commit {sha[:8]}\n"
        f"{tag} {msg}\n\n"
        f"# Diff stat\n{diff_stat[:500]}"
    )
    return await ltm.submit_candidate(
        source=CandidateSource.POST_COMMIT.value,
        source_ref=sha,
        raw_text=raw_text,
        hint="Commit just landed. Look for project conventions or user habits.",
    )


def _render_session(goal: str, summary: str, last_steps, success: bool) -> str:
    parts: List[str] = [
        f"# Goal\n[SELF] {goal}",
        f"# Final summary (success={success})\n{summary}",
    ]
    if last_steps:
        parts.append("# Recent steps")
        for s in last_steps:
            status = getattr(s, "status", None)
            status_name = getattr(status, "name", str(status) if status else "?")
            description = getattr(s, "description", "") or ""
            line = f"- [{status_name}] {description}"
            factual = getattr(s, "factual_outcome", None) or []
            if factual:
                line += "\n  outcome: " + "; ".join(map(str, factual[:3]))
            artifacts = getattr(s, "artifacts", None) or []
            if artifacts:
                line += "\n  artifacts: " + ", ".join(map(str, artifacts[:3]))
            parts.append(line)
    return "\n\n".join(parts)
