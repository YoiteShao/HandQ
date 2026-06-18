"""In-memory recall hit buffer + periodic flush.

Why a separate module
---------------------
Recall is a hot-path call (every receptionist message + every planner
observe). Writing a row to ``recall_log`` synchronously on every hit
would push SQLite write contention into the user-visible latency
budget. Instead we buffer hits in memory and flush in batches piggybacked
onto the dream worker's existing 60s tick — that path already holds the
write lock anyway, so the flush is free.

Why we even need recall_log
---------------------------
The retriage / correction pipeline uses recall recency as a *priority
signal*: an entry the user actually recalled in the last 30 days is
load-bearing context, so any LLM-emitted archive proposal against it
should be surfaced more prominently for review (yansu philosophy:
never silently drop user-relevant data; surface it for explicit consent).

The deque is size-bounded so a forgotten flush hook can't grow without
limit. ``maxlen=10000`` covers ~3000 recall calls × 3-5 entries each
without losing data, far above realistic burst rates.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Iterable, List, Optional, Tuple

from .models import EntryKind

_logger = logging.getLogger("handq.ltm.recall_log")

# (entry_id, kind, recalled_at) — kept as a tuple so executemany inserts
# are zero-copy and the deque holds simple primitives.
_RecallTuple = Tuple[str, str, int]


class RecallLogger:
    """Process-singleton in-memory buffer for recall hits.

    The store reference is captured lazily on first ``flush`` rather than
    construction so the module imports cleanly even before the LTM is
    initialised (e.g. during tests that import recall.py to reach
    ``format_memory_block``).
    """

    _instance: Optional["RecallLogger"] = None

    def __init__(self, *, max_buffer: int = 10_000) -> None:
        self._buf: "deque[_RecallTuple]" = deque(maxlen=max_buffer)

    @classmethod
    def get(cls) -> "RecallLogger":
        if cls._instance is None:
            cls._instance = RecallLogger()
        return cls._instance

    def record(self, entry_ids: Iterable[str], *, kind: str) -> None:
        """Append one row per entry id. Cheap; called on the recall hot path.

        ``kind`` is the str value of :class:`EntryKind`, kept as a plain
        string to avoid an enum lookup per hit. Empty / falsy ids are
        silently dropped so a misshapen recall result row can't poison
        the buffer.
        """
        if not entry_ids:
            return
        now = int(time.time())
        for eid in entry_ids:
            if not eid:
                continue
            self._buf.append((eid, kind, now))

    def buffered(self) -> int:
        """Number of rows currently waiting to be flushed. For diagnostics."""
        return len(self._buf)

    async def flush(self, store) -> int:
        """Drain the buffer into ``recall_log`` via one batched insert.

        Returns the number of rows actually persisted. A flush during
        zero traffic returns 0 instantly (no SQL touched).

        Catches its own exceptions and logs — the dream worker's tick
        loop should never abort because of a recall log hiccup.
        """
        if not self._buf:
            return 0
        rows: List[_RecallTuple] = list(self._buf)
        # Clear BEFORE the await so concurrent recall hits during the
        # flush land in a fresh window rather than getting double-written.
        self._buf.clear()
        try:
            await store.insert_mem_recall_log_batch(rows)
        except Exception:
            # Re-add lost rows so they survive to the next flush attempt
            # rather than being silently dropped.
            self._buf.extendleft(reversed(rows))
            _logger.exception("recall_log flush failed; %d rows re-queued", len(rows))
            return 0
        return len(rows)


def kind_str_for(kind: EntryKind) -> str:
    """Tiny helper so call sites that have an EntryKind don't have to
    remember the ``.value`` access — keeps recall.py call sites readable.
    """
    return kind.value
