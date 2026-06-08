"""User + auto exemplar overlay over the builtin pattern catalogue.

Three layers compose into the Router's "what does each pattern look like" set:

  1. **Builtin** — ``patterns.py::PATTERNS``. Six pattern ids each with a frozen
     skeleton + a small batch of hand-picked exemplar phrasings. Read-only
     code; the v2 backbone always knows about these.

  2. **User** — phrasings (or whole new patterns) the deployment owner wants
     to add. Hand-edited or persisted via :func:`add_user_pattern` /
     :func:`add_user_exemplar`. Never evicted by the auto-feedback loop.

  3. **Auto** — exemplars promoted by ``ExemplarBuilder`` when a goal that
     was Tier-2 (classifier) routed succeeds: its text becomes a Tier-1
     anchor for next time. Bounded by ``max_auto_per_pattern``; LRU-evicted
     when the cap is exceeded so a long-running deployment doesn't
     accumulate noise indefinitely.

The store sits between Router and disk: Router queries ``all_exemplars()``
to score a goal; ExemplarBuilder calls ``add_auto_exemplar()`` to grow it.
A monotonic ``generation`` counter lets Router invalidate its embedding
cache only when something actually changed (no per-classify reload).

JSON file format::

    {
      "user_patterns": [
        {"id": "deploy",
         "skeleton": ["plan", "rollout", "verify"],
         "exemplars": ["deploy v2 to prod"]}
      ],
      "user_exemplars": {
        "modify": ["fix the parser regex"]
      },
      "auto_exemplars": {
        "modify": [
          {"text": "patch off-by-one in foo()",
           "ts": 1717689600.0,
           "source": "trace:run-42"}
        ]
      }
    }
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .patterns import PATTERNS, Pattern


@dataclass(frozen=True)
class AutoExemplar:
    """An exemplar added by the auto-feedback loop.

    ``timestamp`` is epoch seconds — the LRU policy keeps the newest. ``source``
    is free-form (``"trace:run-42"`` / ``"manual"`` / etc.) for telemetry only;
    the routing decision is identical whether an exemplar came from the
    auto loop or the user's hand.
    """

    text: str
    timestamp: float = 0.0
    source: str = ""


class ExemplarStore:
    """User + auto exemplar overlay; the source of truth Router queries.

    Builtin PATTERNS are always present; this overlay can ONLY *add* (a new
    pattern id, a new exemplar for an existing id, or an auto exemplar). It
    never modifies or deletes the builtin entries — so a deployment that
    deletes its ``user_patterns.json`` file degrades cleanly back to the
    out-of-the-box Router behaviour.
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self._path: Optional[Path] = Path(path) if path else None
        self._user_patterns: dict[str, Pattern] = {}
        self._user_exemplars: dict[str, list[str]] = {}
        self._auto_exemplars: dict[str, list[AutoExemplar]] = {}
        # Bumped on every mutation. Router caches keyed by generation;
        # any change forces it to re-embed exemplars on next classify.
        self._generation: int = 0
        if self._path and self._path.exists():
            self._load()

    # ── read API ─────────────────────────────────────────────────────────

    @property
    def generation(self) -> int:
        return self._generation

    def all_exemplars(self) -> list[tuple[str, str]]:
        """Flat ``(pattern_id, exemplar_text)`` list — the Router consumes this.

        Order: builtin first (so they win at lookup-by-text), then user
        patterns, then user exemplars on existing patterns, then auto. Internal
        order is stable, so the Router's cache keyed by generation never
        sees spurious differences.
        """
        out: list[tuple[str, str]] = []
        for pattern in PATTERNS.values():
            out.extend((pattern.id, ex) for ex in pattern.exemplars)
        for pattern in self._user_patterns.values():
            out.extend((pattern.id, ex) for ex in pattern.exemplars)
        for sid, texts in self._user_exemplars.items():
            out.extend((sid, t) for t in texts)
        for sid, entries in self._auto_exemplars.items():
            out.extend((sid, e.text) for e in entries)
        return out

    def get_pattern(self, pattern_id: str) -> Optional[Pattern]:
        """Resolve a pattern across builtin + user."""
        return PATTERNS.get(pattern_id) or self._user_patterns.get(pattern_id)

    def all_pattern_ids(self) -> set[str]:
        return set(PATTERNS.keys()) | set(self._user_patterns.keys())

    def auto_count(self, pattern_id: str) -> int:
        return len(self._auto_exemplars.get(pattern_id, []))

    def user_count(self, pattern_id: str) -> int:
        n = len(self._user_exemplars.get(pattern_id, []))
        if pattern_id in self._user_patterns:
            n += len(self._user_patterns[pattern_id].exemplars)
        return n

    # ── write API ────────────────────────────────────────────────────────

    def add_user_pattern(self, pattern: Pattern) -> None:
        """Define a brand-new pattern (must have a skeleton).

        Raises if ``pattern.id`` clashes with a builtin (use
        :func:`add_user_exemplar` for those) or if the skeleton is empty
        (a zero-phase pattern can't be promoted to a runnable draft).
        """
        if pattern.id in PATTERNS:
            raise ValueError(
                f"pattern {pattern.id!r} is a builtin; use add_user_exemplar to extend it"
            )
        if not pattern.skeleton:
            raise ValueError(f"pattern {pattern.id!r} requires a non-empty skeleton")
        self._user_patterns[pattern.id] = pattern
        self._bump()

    def add_user_exemplar(self, pattern_id: str, text: str) -> bool:
        """Append a hand-written exemplar. Returns False if duplicate text."""
        if pattern_id not in self.all_pattern_ids():
            raise ValueError(f"unknown pattern {pattern_id!r}")
        text = text.strip()
        if not text:
            return False
        bucket = self._user_exemplars.setdefault(pattern_id, [])
        if text in bucket:
            return False
        bucket.append(text)
        self._bump()
        return True

    def add_auto_exemplar(
        self, pattern_id: str, text: str, *, source: str = "",
    ) -> AutoExemplar:
        """Record an auto-promoted exemplar. Caller is responsible for dedup.

        The store stays dumb on dedup so the policy lives in
        :class:`ExemplarBuilder` (which has the embedder needed for
        cosine comparison). Here we only sanity-check the pattern exists
        and append.
        """
        if pattern_id not in self.all_pattern_ids():
            raise ValueError(f"unknown pattern {pattern_id!r}")
        entry = AutoExemplar(text=text, timestamp=time.time(), source=source)
        self._auto_exemplars.setdefault(pattern_id, []).append(entry)
        self._bump()
        return entry

    def evict_oldest_auto(self, pattern_id: str, *, keep: int) -> int:
        """LRU eviction of auto entries: keep the ``keep`` newest, drop the rest.

        Returns the number evicted. Only auto entries are touched —
        user exemplars are never evicted.
        """
        bucket = self._auto_exemplars.get(pattern_id)
        if not bucket or len(bucket) <= keep:
            return 0
        bucket.sort(key=lambda e: e.timestamp)
        evicted = len(bucket) - keep
        del bucket[:evicted]
        self._bump()
        return evicted

    # ── persistence ──────────────────────────────────────────────────────

    def save(self) -> None:
        """Write the overlay to ``path``. No-op without a path."""
        if not self._path:
            return
        doc: dict[str, Any] = {
            "user_patterns": [
                {"id": s.id,
                 "skeleton": list(s.skeleton),
                 "exemplars": list(s.exemplars)}
                for s in self._user_patterns.values()
            ],
            "user_exemplars": {sid: list(texts) for sid, texts in self._user_exemplars.items()},
            "auto_exemplars": {
                sid: [{"text": e.text, "ts": e.timestamp, "source": e.source}
                      for e in entries]
                for sid, entries in self._auto_exemplars.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── internal ─────────────────────────────────────────────────────────

    def _bump(self) -> None:
        self._generation += 1

    def _load(self) -> None:
        """Best-effort load. Corrupt file → keep defaults, don't crash."""
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        for raw in doc.get("user_patterns") or []:
            try:
                sid = str(raw["id"])
                if sid in PATTERNS:
                    # Can't redefine builtin — silently ignore; user wanted
                    # add_user_exemplar instead.
                    continue
                pattern = Pattern(
                    id=sid,
                    skeleton=tuple(str(p) for p in (raw.get("skeleton") or ())),
                    exemplars=tuple(str(e) for e in (raw.get("exemplars") or ())),
                )
                if pattern.skeleton:
                    self._user_patterns[pattern.id] = pattern
            except (KeyError, TypeError):
                continue

        for sid, texts in (doc.get("user_exemplars") or {}).items():
            cleaned = [str(t).strip() for t in (texts or []) if str(t).strip()]
            if cleaned:
                self._user_exemplars[str(sid)] = cleaned

        for sid, entries in (doc.get("auto_exemplars") or {}).items():
            normalized: list[AutoExemplar] = []
            for e in entries or []:
                if not isinstance(e, dict):
                    continue
                text = str(e.get("text", "")).strip()
                if not text:
                    continue
                normalized.append(AutoExemplar(
                    text=text,
                    timestamp=float(e.get("ts", 0.0)),
                    source=str(e.get("source", "")),
                ))
            if normalized:
                self._auto_exemplars[str(sid)] = normalized
