"""Per-remote-session ``digest.json`` writer on the controlling side.

Answers "the remote session ran on Linux — how do I see it in Windows'
History/resume list later?" A remote session's real state lives on the controlled
machine (its ``FlowControllerV2``, its workspace, its own digest); this class
mirrors just enough of it into the local ``~/HandQ/History`` layout that the
Windows resume search can find it after the tab is closed.

What gets recorded here vs. left on the controlled side:

* **Recorded on Windows**: the conversation (what the user typed and what the
  agent replied), the running list of tools the agent invoked, whether the
  session is still alive, when it was last touched. This is what makes the
  resume list useful for a remote session.
* **NOT recorded**: the workspace files, per-turn execution logs, the agent's
  internal scratchpads. Those exist on the controlled machine; copying them across the
  wire for every event would be pointless (the file is over there, not here) and
  potentially very large. ``workspace_dir`` in the digest points at the controlled
  machine's path so a human viewing the record knows where the real work went.

Writes are throttled: a per-token ``reply_delta`` stream would otherwise trigger
a disk write for every fragment. The digest is refreshed at most every
``FLUSH_INTERVAL`` seconds during activity, and unconditionally on close.

Two things about identity, both learned from getting them wrong:

**The directory is keyed on the REMOTE session id, not the local tab.** One
remote session outlives many tabs — you close it, re-adopt it from a panel chip,
close it again — and each of those tabs has a different local sid. Naming the
directory after the tab produced N partial digests of one piece of work, all
competing in the resume list. The remote id is the thing that is stable, so it
goes in the directory name and a re-adopt reopens the existing directory.

**"Destroyed" describes the remote session, never the local tab.** Closing a tab
detaches; the agent on the other machine keeps working. Writing
``status="destroyed"`` at that moment told the resume list a live session was
finished. Only an actual close — ours or the controlled side's — writes that.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, List, Optional

from ..controller_v2.session_digest import SessionDigest

logger = logging.getLogger("handq.remote_control.history")

FLUSH_INTERVAL_SEC = 3.0

_SLUG_RE = re.compile(r"[^A-Za-z0-9一-鿿]+")


def _slugify(text: str, max_len: int = 30) -> str:
    """Same shape as stdio_bridge._slugify_goal — matches the local convention
    so a remote history dir sits alongside local ones in the same list."""
    if not text:
        return "untitled"
    cleaned = _SLUG_RE.sub("-", text.strip()).strip("-")
    return (cleaned[:max_len] or "untitled")


def _history_root() -> Path:
    """``%USERPROFILE%\\HandQ\\History`` — the same directory local sessions use."""
    return Path(os.path.expanduser("~")) / "HandQ" / "History"


def _remote_suffix(remote_session_id: str) -> str:
    """The directory-name tail that makes a remote session's dir findable again.

    Short on purpose — the full ``rc-`` + 16 hex chars would dominate the
    directory name a human is meant to read at a glance, and 8 hex chars of a
    per-session random id is already far beyond collision risk within one
    History folder. Empty when the remote id is not known yet (a fresh open
    before ``session_opened`` arrives), in which case no reuse is attempted and
    a normal timestamped directory is created.
    """
    sid = (remote_session_id or "").strip()
    if not sid:
        return ""
    return "-" + _SLUG_RE.sub("", sid)[-8:]


class RemoteHistory:
    """Maintains a ``digest.json`` for one remote session.

    The bridge feeds every agent_event through :meth:`record` (only three
    delegate methods actually contribute; the rest are silently skipped, because
    a partial digest is fine — the goal is "you can find and browse this
    session in the resume list", not "reconstruct the full agent trace"). On
    close, :meth:`finalize` writes the last update with ``status="destroyed"``.
    """

    def __init__(self, *, local_session_id: str, target_id: str,
                 server_name: str, remote_session_id: str = "",
                 title: str = "") -> None:
        self._local_sid = local_session_id
        self._target_id = target_id
        self._server_name = server_name
        self._remote_sid = remote_session_id
        self._title = (title or "").strip()

        self._conversation: List[dict] = []
        self._active_tools: List[str] = []
        # The one belief-y field we track: the reply the agent last said. Used
        # as agent_summary so the resume list has a preview.
        self._last_reply: str = ""
        # Streaming reply chunks accumulate here until seal_coordinator_reply
        # flushes them into the conversation. Keeps a single reply from
        # ballooning the conversation array with hundreds of "assistant"
        # entries (one per token).
        self._reply_buffer: str = ""

        self._created_at = time.time()
        #: Set only when we adopt an existing directory, so the digest keeps the
        #: remote session's original creation time rather than resetting it to
        #: whenever the user happened to re-open the tab.
        self._created_at_iso: Optional[str] = None
        self._last_flush = 0.0
        self._session_dir: Optional[Path] = None
        self._closed = False

    # ── writes ─────────────────────────────────────────────────────────────

    def bind_remote_session(self, remote_session_id: str, title: str = "") -> None:
        """Learn the remote session id once ``open_session`` has answered.

        A fresh open does not know it at construction time (the id is minted by
        the controlled side), so the bridge calls this the moment it arrives. It has to
        land before the first flush, because the id is part of the directory
        name — which is why the directory is created lazily on first content
        rather than in ``__init__``.
        """
        self._remote_sid = str(remote_session_id or "")
        if title and not self._title:
            self._title = title.strip()

    def note_user_message(self, text: str) -> None:
        """Called by the bridge when the operator types something into the
        remote tab. Doesn't come through agent_event (user input goes UP the
        wire, not down), so the bridge announces it separately."""
        if not text:
            return
        if not self._title:
            self._title = text.strip()[:60]
        self._conversation.append({"role": "user", "content": text})
        self._maybe_flush()

    def record(self, method: str, args: List[Any]) -> None:
        """Fold one agent_event into the digest state."""
        if method == "notify_tool_execution_started":
            # args = [iteration, tool_name, params, output]. Start-of-call fires
            # with output=None; only record the tool name on the start side, so
            # each invocation counts once regardless of how many times it fires.
            if len(args) >= 4 and args[3] is None and args[1]:
                name = str(args[1])
                if not self._active_tools or self._active_tools[-1] != name:
                    self._active_tools.append(name)
                self._maybe_flush()
            return

        if method == "stream_coordinator_reply_chunk":
            if args:
                self._reply_buffer += str(args[0])
            return
        if method == "seal_coordinator_reply":
            if self._reply_buffer:
                self._last_reply = self._reply_buffer
                self._conversation.append(
                    {"role": "assistant", "content": self._reply_buffer})
                self._reply_buffer = ""
                self._maybe_flush()
            return

        if method == "show_coordinator_reply":
            # One-shot reply (non-streamed or task-completion summary; see
            # network_delegate.show_coordinator_reply's docstring for the
            # bypass this catches). Same treatment as a sealed stream.
            if args:
                text = str(args[0])
                self._last_reply = text
                self._conversation.append({"role": "assistant", "content": text})
                self._maybe_flush()
            return

        if method == "show_user_message_echo":
            # Only reaches here for a genuine replay (a reattach, or another
            # tab's message) — session_bridge.py's on_agent_event intercepts
            # the sending tab's own live echo before it gets here, since
            # note_user_message already recorded that one when it was typed.
            if args:
                self._conversation.append(
                    {"role": "user", "content": str(args[0])})
                self._maybe_flush()
            return

        # Everything else (state changes, task plan pings, file touches,
        # confirmations, …) is deliberately not recorded — a remote session's
        # true trace lives on the controlled side, this file only needs enough for
        # someone to recognise the session in the resume list later.

    def finalize(self, reason: str = "closed", status: str = "destroyed") -> None:
        """Write one last update and stop tracking this tab.

        ``status`` is about the REMOTE session, so the caller has to say which
        case this is: a tab close passes ``"running"`` (the agent over there is
        untouched, and the entry must stay resumable), an actual close passes
        ``"destroyed"``. Getting this wrong is not cosmetic — the resume list
        reads ``status``, so a detach that claimed "destroyed" made every
        still-running remote task look finished.
        """
        if self._closed:
            return
        self._closed = True
        # Any buffered stream fragments are flushed rather than lost.
        if self._reply_buffer:
            self._conversation.append(
                {"role": "assistant", "content": self._reply_buffer})
            self._last_reply = self._reply_buffer
            self._reply_buffer = ""
        self._flush(force=True, status=status, reason=reason)

    # ── plumbing ───────────────────────────────────────────────────────────

    def _ensure_session_dir(self) -> Optional[Path]:
        """Resolve this remote session's directory, creating it on first content.

        Lazy so a tab that never receives any events (or a bind that fails
        immediately) doesn't leave an empty dir behind. Once resolved, the path
        is cached — the digest is rewritten in place from then on.

        Re-adopting an existing remote session REUSES its directory rather than
        starting a new one. The suffix that makes that possible is the remote
        session id: the local sid changes with every tab, the remote id does not,
        so it is the only thing a later tab can search on. Without this, one
        long-running remote task left a trail of half-written digests in the
        resume list, one per time it had been opened.
        """
        if self._session_dir is not None:
            return self._session_dir

        root = _history_root()
        suffix = _remote_suffix(self._remote_sid)
        if suffix:
            try:
                existing = sorted(root.glob(f"*-remote-*{suffix}"))
            except Exception:
                existing = []
            if existing:
                self._session_dir = existing[0]
                self._adopt_existing(self._session_dir)
                return self._session_dir

        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._created_at))
        slug = _slugify(self._title or self._server_name or "remote")
        # The `remote-` prefix is meaningful in the directory name: it lets
        # a human eyeballing History/ tell remote from local sessions at a
        # glance, and it means the resume-search retriever can weight them
        # differently in future if needed. The trailing id is what makes the
        # directory findable again on re-adopt.
        base = root / f"{ts}-remote-{slug}{suffix}"
        candidate = base
        n = 1
        while candidate.exists():
            n += 1
            candidate = base.with_name(f"{base.name}-{n}")
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("remote_control: could not create %s", candidate,
                           exc_info=True)
            return None
        self._session_dir = candidate
        return candidate

    def _adopt_existing(self, session_dir: Path) -> None:
        """Prepend what an earlier tab already recorded for this remote session.

        Reusing the directory without this would be worse than the fragmentation
        it fixes: ``_flush`` rewrites ``digest.json`` wholesale, so the first
        flush of a re-adopted tab would replace yesterday's transcript with the
        two lines typed since re-opening. Called from ``_ensure_session_dir``
        before the first write, which is the only moment ``_conversation`` holds
        purely new entries — so ordering is simply old-then-new.

        Best effort: a missing or malformed digest just means starting from what
        this tab has, which is the current behaviour and no worse.
        """
        try:
            previous = SessionDigest.load(session_dir)
        except Exception:
            previous = None
        if previous is None:
            return
        if previous.conversation:
            self._conversation = list(previous.conversation) + self._conversation
        for name in previous.active_tools:
            if name not in self._active_tools:
                self._active_tools.insert(0, str(name))
        # The original title is the stable one — it came from the goal. A
        # re-adopted tab's first message is a continuation, not a new subject.
        if previous.title:
            self._title = previous.title
        if previous.created_at:
            self._created_at_iso = previous.created_at
        if not self._last_reply and previous.agent_summary:
            self._last_reply = str(previous.agent_summary)

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush < FLUSH_INTERVAL_SEC:
            return
        self._flush(force=False, status="running")

    def _flush(self, *, force: bool, status: str, reason: str = "") -> None:
        """Rewrite ``digest.json``. Silent on failure — a full disk should not
        crash a remote session in progress."""
        session_dir = self._ensure_session_dir()
        if session_dir is None:
            return
        self._last_flush = time.monotonic()

        digest = SessionDigest(
            session_id=self._local_sid,
            title=SessionDigest.cap(self._title or self._server_name or "remote"),
            created_at=self._created_at_iso or _iso(self._created_at),
            updated_at=_iso(time.time()),
            # A controlled-machine path. Deliberately not resolved locally; it's a
            # human-readable pointer to where the real work went.
            workspace_dir=f"remote://{self._server_name}/{self._remote_sid}",
            workspace_files=[],
            status="running" if status != "destroyed" else "destroyed",
            conversation=[
                {"role": e["role"], "content": SessionDigest.cap(e["content"])}
                for e in self._conversation
            ],
            active_tools=list(self._active_tools),
            agent_summary=SessionDigest.cap(self._last_reply) or None,
        )
        digest.save(session_dir)


def _iso(t: float) -> str:
    """Match session_digest's created_at/updated_at spelling
    (FlowControllerV2 uses UTC ISO 8601)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
