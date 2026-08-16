"""Frame vocabulary for the direct control channel.

Wire format is newline-delimited JSON with a ``"t"`` discriminator, identical to
``infrastructure/chatroom/protocol.py`` — deliberately, so
:class:`infrastructure.chatroom.transport.JsonlConnection` can be reused verbatim
as the framing layer. The two *vocabularies* are disjoint (chatroom speaks
``hello``/``roster``/``chat``; this speaks ``auth``/``open_session``/…), which is
why they are separate modules rather than one merged protocol.

Three things about this vocabulary are worth reading before touching it:

**Every session-scoped frame carries ``session_id``.** One connection per remote
machine multiplexes N sessions, so the discriminator alone is never enough to
route a frame. The already-validated probe scripts in ``verify_fleet_scheduling/``
got away without it because they only ever opened one session per connection.

**``agent_event`` carries a DELEGATE method name, not an InteractionManager
method name.** ``InteractionManager.notify_state_changed`` calls
``delegate.show_state_changed``; the five ``notify_* → show_*`` renames live
entirely inside the IM. By putting the delegate-side name on the wire, the controlled
side (a delegate) and the controlling side (which replays onto ``_StdioUI``, also a
delegate) use the same string, and no mapping table is needed anywhere. See
``interaction_manager.py:16-18`` for the rename list this sidesteps.

**``confirm_request`` is NOT in the replayable event log.** Replaying an
already-answered confirmation would hang a dead modal in the reconnecting UI.
Still-pending confirmations are instead sent as explicit state on
``session_attached``, so the transcript comes from the log and the live
obligations come from the attach handshake.

**Session existence is the server's truth, never the controller's memory.**
The controller persists ``(session_id, capability, seq)`` per session so it can
re-attach tomorrow, but that file is a *credential cache*, not an inventory: the
controlled side may have destroyed a session (its operator clicked Close, the process
restarted) with the controller offline and unable to hear about it. ``auth_ok``
therefore carries the live session list on every connect, and ``list_sessions``
asks for it again at any time — the controller reconciles its records against
that answer instead of showing chips for sessions that no longer exist. The same
answer is what surfaces a session sitting on a parked confirmation while no tab
is open, which is otherwise invisible: the agent over there is blocked on a
human who cannot see it is being asked.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Bumped ONLY on a genuinely incompatible change — one where a peer that
# doesn't understand the change would MISbehave, not merely miss out. The gate
# in server.py is strict equality in both directions (a v(N) server refuses a
# v(N±1) client and vice-versa), and an old controlled-side daemon is already deployed in
# the field and cannot be taught new tolerance remotely — so a bump strands
# every machine paired before it. That makes "is this actually incompatible?"
# the load-bearing question, not "did the vocabulary change?".
#
# It stays at 1 despite two v6 vocabulary changes, because both degrade
# gracefully rather than misbehave:
#   * list_sessions / sessions_list are ADDITIVE. An old server hits its
#     _dispatch catch-all and ignores the unknown frame; the new client's
#     list_remote_sessions() times out and refresh_sessions() returns [] WITHOUT
#     reconciling (so no record is wrongly deleted). Reconcile-on-connect still
#     works off auth_ok's session list, which every version sends.
#   * Dropping the "mark_task_started" pseudo-event is safe both ways: an old
#     server still emits it and a new client drops it via the getattr(ui, ...)
#     miss; is_task for the chip badge rides on describe(), which old servers
#     already populate. Nothing depends on RECEIVING a frame the peer won't send.
# A future change that alters the MEANING of an existing field, or removes a
# frame a peer relies on receiving, is what earns a bump to 2.
PROTOCOL_VERSION = 1

# Heartbeat. The controlling side answers ``ping`` with ``pong``; both sides run
# a watchdog. Liveness is decided by the watchdog, NEVER by read-loop EOF —
# a wedged peer holding a half-open socket produces no EOF and no exception,
# which is exactly the case ``verify_fleet_scheduling/local_protocol_check.py``
# scenario 5 was written to catch.
HEARTBEAT_INTERVAL_SEC = 5.0
HEARTBEAT_TIMEOUT_SEC = 20.0

# An accepted TCP connection gets this long to produce a valid ``auth`` frame
# before it is dropped. Bounds the resource a bare port-scanner can hold.
AUTH_TIMEOUT_SEC = 5.0

CONNECT_TIMEOUT_SEC = 10.0

# How long the controller waits for the controlled side to CONFIRM a destruction it
# asked for (``close_session`` → ``session_closed``, ``release_server`` →
# ``error(closed_by_controller)``).
#
# These exist because destruction used to be fire-and-forget while the local
# bookkeeping that followed it was unconditional: the controller deleted the
# session record (or the whole pairing) whether or not the frame ever landed. A
# dropped frame therefore produced a session still running on the other machine
# that no controller held a record for — visible only as a greyed
# "not controllable" chip after the next connect, which is exactly the "I closed
# that, why is it back?" report this fixes. Waiting for the answer makes the
# local delete conditional on the remote delete, in that order.
CLOSE_CONFIRM_TIMEOUT_SEC = 8.0
RELEASE_CONFIRM_TIMEOUT_SEC = 8.0

# There is deliberately NO "destroy sessions when nobody is driving us" timer.
# A controlled machine is a server: it does not clear its state because no client is
# currently visiting. Sessions end only on an explicit command — a session-level
# close, or the one destructive action that ends the serving relationship
# (``release_server``, which additionally exits a Linux daemon since that process
# exists only to be driven).
#
# An earlier revision had ``ORPHAN_SESSION_GRACE_SEC = 90.0`` here, with the Linux
# daemon destroying everything 90s after the last controller left. It was aimed at
# "sessions I closed came back", but that report's real defect was accountability,
# not survival: those sessions were legitimately alive and the panel could not
# name, reconcile, or close them. That is fixed at the source now (titles in the
# records, ``list_sessions`` reconciliation, pending-confirm counts on the chips,
# and force-close for chips this machine holds no capability for). Bounding
# accumulation by killing live agent work — including a task still running for an
# operator whose laptop merely slept — was paying for it in the wrong currency.


# ── Frame types ──────────────────────────────────────────────────────────────

# Handshake
AUTH = "auth"                              # c→s {token, client_id, client_name, protocol_version}
AUTH_OK = "auth_ok"                        # s→c {server_name, platform, protocol_version, sessions[]}

# Session lifecycle
OPEN_SESSION = "open_session"              # c→s {goal, title}
SESSION_OPENED = "session_opened"          # s→c {session_id, capability, title}
ATTACH_SESSION = "attach_session"          # c→s {session_id, capability, since_seq}
SESSION_ATTACHED = "session_attached"      # s→c {session_id, cur_seq, gap, snapshot, pending_confirms}
DETACH_SESSION = "detach_session"          # c→s {session_id}
SESSION_SUPERSEDED = "session_superseded"  # s→old owner {session_id, generation}
CLOSE_SESSION = "close_session"            # c→s {session_id, capability}
SESSION_CLOSED = "session_closed"          # s→c {session_id, reason}

# Inventory query. Same descriptor shape as ``auth_ok``'s ``sessions``, asked
# for on demand rather than only at connect time. The controller uses it to
# reconcile its persisted records (drop chips for sessions the controlled side no
# longer has) and to notice a session parked on a confirmation while no local
# tab is open. Connection-scoped, not session-scoped: it needs the auth token,
# not any session's capability, and it reports every session this server hosts
# — including ones this controller holds no capability for, which it can then
# show as present-but-not-ours rather than pretend do not exist.
LIST_SESSIONS = "list_sessions"            # c→s {}
SESSIONS_LIST = "sessions_list"            # s→c {sessions[]}

# Skill upload. Connection-scoped like LIST_SESSIONS above — pushing a skill
# has nothing to do with any particular session, so it needs the auth token
# only. Each file's content travels as base64 inside the frame itself: there
# is no separate binary channel over this JSON-line transport, and skill
# folders are small enough (tens of KB) that chunking would be pure overhead.
# ``skills`` fully mirrors each named folder on the receiving side — deleting
# any file under that folder the sender didn't include — so the request
# carries every file the client wants present, not a diff.
SKILL_PUSH = "skill_push"                  # c→s {skills: [{name, files: [{path, content_b64}]}]}
SKILL_PUSH_RESULT = "skill_push_result"    # s→c {results: [{name, ok, error}]}

# Connection lifecycle
#
# RELEASE_SERVER is what makes "the operator clicked Disconnect" different from
# "the network dropped". A dropped socket parks sessions so the same controller
# can reattach and lose nothing; an explicit release means the controller is
# done with this machine, so the server destroys every session and returns to
# waiting for a new client (and a Linux daemon, whose only purpose is to be
# driven, exits entirely). Without a distinct frame the server cannot tell the
# two apart — a half-open TCP connection looks identical to a deliberate
# goodbye.
RELEASE_SERVER = "release_server"          # c→s {}

# Traffic
USER_MESSAGE = "user_message"              # c→s {session_id, text}
USER_INPUT = "user_input"                  # c→s {session_id, kind, payload}
SESSION_RPC = "session_rpc"                # c→s {session_id, rpc_id, action, payload}
SESSION_RPC_RESULT = "session_rpc_result"  # s→c {session_id, rpc_id, ok, result, error}
AGENT_EVENT = "agent_event"                # s→c {session_id, seq, method, args}
CONFIRM_REQUEST = "confirm_request"        # s→c {session_id, request_id, method, args, kwargs}
CONFIRM_RESPONSE = "confirm_response"      # c→s {session_id, request_id, value}
CONFIRM_CANCEL = "confirm_cancel"          # s→c {session_id, request_id}

# Connection-level
PING = "ping"                              # both {ts}
PONG = "pong"                              # both {ts}
ERROR = "error"                            # s→c {reason, detail}

#: Frames the server will process before ``auth`` has succeeded.
PRE_AUTH_FRAMES = frozenset({AUTH})

# ── ``session_closed`` / ``error`` reason codes ──────────────────────────────
# Kept as constants because the controlling side branches on them to decide whether a
# retry could ever succeed: ``INVALID_TOKEN`` means stop and re-pair, whereas
# ``NOT_ATTACHED`` means re-attach and try again.
REASON_INVALID_TOKEN = "invalid_token"
REASON_INVALID_CAPABILITY = "invalid_capability"
REASON_UNKNOWN_SESSION = "unknown_session"
REASON_VERSION_MISMATCH = "version_mismatch"
REASON_NOT_ATTACHED = "not_attached"
REASON_AUTH_TIMEOUT = "auth_timeout"
REASON_AUTH_REQUIRED = "auth_required"
REASON_SERVER_SHUTDOWN = "server_shutdown"
REASON_CLOSED_BY_CONTROLLER = "closed_by_controller"
REASON_FORCE_CLOSED = "force_closed"  # closed on connection auth alone, no matching capability
REASON_RELEASED_BY_CLIENT = "released_by_client"
REASON_SESSION_FAILED = "session_failed"
REASON_TOO_MANY_SESSIONS = "too_many_sessions"


# ── Builders ─────────────────────────────────────────────────────────────────
# Present for the same reason chatroom/protocol.py has them: the frame key
# spellings live in exactly one place, so a typo is an ImportError rather than a
# frame the peer silently ignores.

def make_auth(token: str, client_id: str, client_name: str) -> Dict[str, Any]:
    return {
        "t": AUTH,
        "token": token,
        "client_id": client_id,
        "client_name": client_name,
        "protocol_version": PROTOCOL_VERSION,
    }


def make_auth_ok(
    server_name: str,
    platform: str,
    sessions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """``sessions`` lets a reconnecting controller discover what it can re-attach
    to without having to remember anything but ``(session_id, capability)`` —
    and, just as importantly, lets it discover what has *gone*, so its persisted
    records get reconciled the moment a connection comes back rather than at the
    next failed attach. See :func:`make_sessions_list` for the on-demand form."""
    return {
        "t": AUTH_OK,
        "server_name": server_name,
        "platform": platform,
        "protocol_version": PROTOCOL_VERSION,
        "sessions": sessions,
    }


def make_open_session(goal: str, title: str = "") -> Dict[str, Any]:
    return {"t": OPEN_SESSION, "goal": goal, "title": title}


def make_session_opened(session_id: str, capability: str, title: str) -> Dict[str, Any]:
    return {
        "t": SESSION_OPENED,
        "session_id": session_id,
        "capability": capability,
        "title": title,
    }


def make_attach_session(
    session_id: str, capability: str, since_seq: int = 0
) -> Dict[str, Any]:
    return {
        "t": ATTACH_SESSION,
        "session_id": session_id,
        "capability": capability,
        "since_seq": int(since_seq),
    }


def make_session_attached(
    session_id: str,
    cur_seq: int,
    gap: bool,
    snapshot: Dict[str, Any],
    pending_confirms: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Sent after the replayed ``agent_event`` burst, so the controller can treat
    its arrival as "you are now caught up and live".

    ``gap=True`` means the requested ``since_seq`` had already aged out of the
    ring and the replay is therefore incomplete; ``snapshot`` (latest task plan /
    todos / takeover state) is the best available substitute. The controller
    surfaces this to the user instead of pretending the transcript is whole.
    """
    return {
        "t": SESSION_ATTACHED,
        "session_id": session_id,
        "cur_seq": int(cur_seq),
        "gap": bool(gap),
        "snapshot": snapshot,
        "pending_confirms": pending_confirms,
    }


def make_detach_session(session_id: str) -> Dict[str, Any]:
    return {"t": DETACH_SESSION, "session_id": session_id}


def make_list_sessions() -> Dict[str, Any]:
    """Controller → server: "what sessions do you actually have right now?"

    Deliberately unparameterised. The controller cannot ask "is rc-abc still
    alive?" one id at a time, because the answer it needs is the *set* — a
    record it holds for a session the server no longer lists is a record to
    drop, and that inference requires the whole list, not per-id probes.
    """
    return {"t": LIST_SESSIONS}


def make_sessions_list(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Server → controller: every session hosted here, as ``describe()`` dicts.

    Same shape as ``auth_ok``'s ``sessions`` on purpose: one descriptor format
    means the controller's reconcile logic does not care whether the list
    arrived from a fresh handshake or from an explicit refresh.
    """
    return {"t": SESSIONS_LIST, "sessions": sessions}


def make_release_server() -> Dict[str, Any]:
    """Controller → server: "I'm done with you."

    Triggers session destruction on the server (see RELEASE_SERVER's comment
    for why this can't just be a socket close).
    """
    return {"t": RELEASE_SERVER}


def make_skill_push(skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Controller → server: mirror each of these skill folders onto this
    machine's user Skill root, replacing whatever is there under that name."""
    return {"t": SKILL_PUSH, "skills": skills}


def make_skill_push_result(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Server → controller: per-skill ``{name, ok, error}`` outcomes. Reported
    per skill rather than one pass/fail for the whole request, because one
    bad name (invalid characters, disk error) must not roll back the sibling
    skills in the same push."""
    return {"t": SKILL_PUSH_RESULT, "results": results}


def make_session_superseded(session_id: str, generation: int) -> Dict[str, Any]:
    return {
        "t": SESSION_SUPERSEDED,
        "session_id": session_id,
        "generation": int(generation),
    }


def make_close_session(session_id: str, capability: str) -> Dict[str, Any]:
    return {"t": CLOSE_SESSION, "session_id": session_id, "capability": capability}


def make_session_closed(session_id: str, reason: str) -> Dict[str, Any]:
    return {"t": SESSION_CLOSED, "session_id": session_id, "reason": reason}


def make_user_message(session_id: str, text: str) -> Dict[str, Any]:
    return {"t": USER_MESSAGE, "session_id": session_id, "text": text}


def make_user_input(
    session_id: str, kind: str, payload: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Non-message user input — currently only ``desktop_takeover_revoked``.
    Kept as an open envelope so the local UI's revoke hotkey reaches the controlled
    machine's ``DesktopState`` without a new frame type per gesture."""
    return {
        "t": USER_INPUT,
        "session_id": session_id,
        "kind": kind,
        "payload": payload or {},
    }


def make_session_rpc(
    session_id: str, rpc_id: str, action: str, payload: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """A session-scoped request that needs a real answer, not an event.

    Exists for ``file_undo``, whose renderer contract is a ``final`` carrying
    ``{mode, restored, conflicts}`` — the ↺ button in the sidebar cannot be
    served by a fire-and-forget event. Kept generic because the same shape is
    what any future "ask the controlled session a question" feature would need, and one
    RPC envelope is cheaper than a frame type per question.
    """
    return {
        "t": SESSION_RPC,
        "session_id": session_id,
        "rpc_id": rpc_id,
        "action": action,
        "payload": payload or {},
    }


def make_session_rpc_result(
    session_id: str,
    rpc_id: str,
    ok: bool,
    result: Optional[Dict[str, Any]] = None,
    error: str = "",
) -> Dict[str, Any]:
    return {
        "t": SESSION_RPC_RESULT,
        "session_id": session_id,
        "rpc_id": rpc_id,
        "ok": bool(ok),
        "result": result or {},
        "error": error,
    }


def make_agent_event(
    session_id: str, seq: int, method: str, args: List[Any]
) -> Dict[str, Any]:
    """``method`` is a **delegate** method name (``show_state_changed``,
    ``notify_tool_execution_started``, …) — see the module docstring."""
    return {
        "t": AGENT_EVENT,
        "session_id": session_id,
        "seq": int(seq),
        "method": method,
        "args": args,
    }


def make_confirm_request(
    session_id: str,
    request_id: str,
    method: str,
    args: List[Any],
    kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """``kwargs`` exists solely for ``request_risk_confirmation``, whose
    ``title``/``approve_label`` are keyword-only (``interaction_manager.py:317``)."""
    return {
        "t": CONFIRM_REQUEST,
        "session_id": session_id,
        "request_id": request_id,
        "method": method,
        "args": args,
        "kwargs": kwargs or {},
    }


def make_confirm_response(session_id: str, request_id: str, value: Any) -> Dict[str, Any]:
    return {
        "t": CONFIRM_RESPONSE,
        "session_id": session_id,
        "request_id": request_id,
        "value": value,
    }


def make_confirm_cancel(session_id: str, request_id: str) -> Dict[str, Any]:
    """controlled side gave up on a relayed prompt (ask_human's own timeout fired)
    before the controller answered it. Tells the controller to cancel
    whatever it already relayed locally for this request_id — see
    RemoteSession.discard_confirm_and_notify and
    RemoteSessionBridge.on_confirm_cancel."""
    return {
        "t": CONFIRM_CANCEL,
        "session_id": session_id,
        "request_id": request_id,
    }


def make_ping(ts: float) -> Dict[str, Any]:
    return {"t": PING, "ts": ts}


def make_pong(ts: float) -> Dict[str, Any]:
    return {"t": PONG, "ts": ts}


def make_error(reason: str, detail: str = "") -> Dict[str, Any]:
    return {"t": ERROR, "reason": reason, "detail": detail}
