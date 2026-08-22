"""Per-session event log with seq'd replay — the "lose nothing" mechanism.

The design doc (§9.4) proposed keeping only the *latest* task plan / todo list /
takeover flag as the reconnect snapshot. That is enough to redraw the panels but
throws away everything that happened while the controller was away: every tool
call, every streamed reply, every inline event. The requirement is explicitly
"no loss of any message content", so instead every fire-and-forget UI event gets a
monotonic ``seq`` and is retained here. ``attach_session`` carries ``since_seq``
and the server replays exactly the tail the controller has not seen.

Three deliberate choices:

**In-memory, bounded, and honest about the bound.** A ``deque(maxlen=…)`` of
~50k events covers hours of agent work at a few MB. When a controller asks for a
seq that has already aged out, :meth:`replay_since` reports ``gap=True`` instead
of quietly starting from the oldest retained event — a truncated transcript that
*looks* complete is worse than one labelled incomplete. Not disk-backed: the log
only has to outlive a client disconnect, not a controlled-side process restart (a restart
takes the ``FlowControllerV2`` with it, so there is no live session left to
re-attach to).

**Reply streaming is coalesced on replay, never on write.** ``stream_coordinator_reply_chunk``
arrives once per token. Merging chunks at *write* time looks tempting and is
wrong: the log entry then holds accumulated text while the wire frame holds the
increment, so one ``seq`` means two different things, and a controller that has
seen ``seq=14`` ("Hel") and replays from 14 either loses the increment or
re-receives the whole run. Chunks are therefore stored individually and merged
only in :meth:`replay_since`, where the merged frame can safely carry the last
seq of the run. Chunk payloads are a few characters each, so the storage cost is
far below that of a single tool result.

**The snapshot is maintained here too.** Task plan / agent todos / takeover state
are latched as they stream past, so the gap path has something to fall back on
without needing a separate query interface into ``TaskChannel`` (which would
raise the question of which coroutine owns that reference).
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

#: Retained events per session. At ~200 bytes/event this is ~10 MB worst case
#: for a session that never disconnects, and real sessions are far smaller
#: because reply chunks coalesce.
DEFAULT_CAPACITY = 50_000

#: Delegate methods whose payload is a complete replacement of prior state, so
#: only the newest value is ever needed after a gap.
_SNAPSHOT_METHODS = {
    "notify_task_plan_changed": "task_plan",
    "notify_agent_todo_changed": "agent_todos",
    "notify_model_stats_changed": "model_stats",
}

_CHUNK_METHOD = "stream_coordinator_reply_chunk"

#: Beyond this many characters a coalesced reply bubble stops absorbing further
#: chunks and a fresh event starts. Keeps one runaway reply from producing a
#: single multi-megabyte frame that stalls the wire on replay.
_CHUNK_COALESCE_LIMIT = 8_000


@dataclass
class ReplayResult:
    """Outcome of :meth:`EventLog.replay_since`."""

    #: Events the caller should send, oldest first, as ``(seq, method, args)``.
    events: List[Tuple[int, str, List[Any]]]
    #: The log's newest seq. The controller stores this as its resume point.
    cur_seq: int
    #: True when ``since_seq`` predates the oldest retained event, i.e. the
    #: replay is incomplete and ``snapshot`` is the only account of what was lost.
    gap: bool


@dataclass
class _Entry:
    seq: int
    method: str
    args: List[Any]


@dataclass
class EventLog:
    """Append-only (bounded) event log for one session.

    Thread-safe: the controlled side's delegate is called from the agent's event loop
    while attach handling runs on the server's, and in the Electron host both can
    additionally be touched from the stdin reader thread. A plain lock is
    cheaper here than the reasoning required to prove single-threaded access.
    """

    capacity: int = DEFAULT_CAPACITY
    _entries: Deque[_Entry] = field(default_factory=deque, init=False)
    _seq: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _snapshot: Dict[str, Any] = field(default_factory=dict, init=False)
    _desktop_takeover: Optional[str] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._entries = deque(maxlen=max(1, int(self.capacity)))

    # ── Write path ───────────────────────────────────────────────────────────

    def append(self, method: str, args: List[Any]) -> int:
        """Record one delegate event; returns its ``seq``.

        Always allocates a fresh seq — see the module docstring on why chunks are
        not merged here.
        """
        with self._lock:
            self._latch_snapshot(method, args)
            self._seq += 1
            self._entries.append(_Entry(seq=self._seq, method=method, args=list(args)))
            return self._seq

    def _latch_snapshot(self, method: str, args: List[Any]) -> None:
        """Keep the newest value of each replace-in-full event for the gap path."""
        key = _SNAPSHOT_METHODS.get(method)
        if key is not None:
            self._snapshot[key] = args[0] if args else []
            return
        if method == "notify_desktop_takeover_started":
            self._desktop_takeover = str(args[0]) if args else "input_action"
        elif method == "notify_desktop_takeover_ended":
            self._desktop_takeover = None

    # ── Read path ────────────────────────────────────────────────────────────

    @property
    def cur_seq(self) -> int:
        with self._lock:
            return self._seq

    def replay_since(self, since_seq: int) -> ReplayResult:
        """Everything with ``seq > since_seq``, plus whether that is complete.

        ``since_seq=0`` from a fresh attach is NOT a gap even when the ring has
        already evicted events: a controller that has seen nothing is asking for
        "as much history as you have", and the honest answer for a brand-new
        attach to a long-running session is the retained tail. A gap is only
        reported when the controller names a specific seq that has aged out,
        because that is the case where it can no longer stitch its own
        transcript together.
        """
        since = max(0, int(since_seq))
        with self._lock:
            cur = self._seq
            oldest = self._entries[0].seq if self._entries else cur + 1
            gap = bool(since and self._entries and since + 1 < oldest)
            if since == 0 and self._entries and oldest > 1:
                # Fresh attach onto an already-trimmed session: not a "lost my
                # place" gap, but the transcript still isn't from the beginning.
                gap = True
            events = [
                (e.seq, e.method, list(e.args))
                for e in self._entries
                if e.seq > since
            ]
        return ReplayResult(
            events=_coalesce_chunks(events), cur_seq=cur, gap=gap
        )

    def snapshot(self) -> Dict[str, Any]:
        """Latest cumulative UI state, for the gap path and for panel redraws.

        Shape matches what the corresponding delegate calls expect, so the
        controller can replay it through the ordinary delegate methods rather
        than needing bespoke rendering.
        """
        with self._lock:
            return {
                "task_plan": list(self._snapshot.get("task_plan") or []),
                "agent_todos": list(self._snapshot.get("agent_todos") or []),
                "model_stats": list(self._snapshot.get("model_stats") or []),
                "desktop_takeover": self._desktop_takeover,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _coalesce_chunks(
    events: List[Tuple[int, str, List[Any]]]
) -> List[Tuple[int, str, List[Any]]]:
    """Merge runs of adjacent reply chunks into single events.

    Replaying a 400-token reply as 400 frames is pure overhead — the renderer
    appends them into one bubble anyway. Merging is safe here (unlike at write
    time) because a replay always starts from a boundary the controller has
    already passed: every chunk in the run is one it has not seen, so handing it
    the concatenation is equivalent.

    Each merged event carries the seq of the LAST chunk in the run, which keeps
    the controller's ``since_seq`` monotonic and correct — acknowledging the run
    acknowledges every chunk in it. Adjacent-only, so any event between two
    chunks still splits them and ordering is never violated. The run is also
    capped, so one enormous reply cannot become a single frame big enough to
    stall the wire.
    """
    out: List[Tuple[int, str, List[Any]]] = []
    for seq, method, args in events:
        if (
            method == _CHUNK_METHOD
            and out
            and out[-1][1] == _CHUNK_METHOD
            and isinstance(out[-1][2][0] if out[-1][2] else None, str)
            and isinstance(args[0] if args else None, str)
            and len(out[-1][2][0]) + len(args[0]) <= _CHUNK_COALESCE_LIMIT
        ):
            out[-1] = (seq, method, [out[-1][2][0] + args[0]])
            continue
        out.append((seq, method, list(args)))
    return out
