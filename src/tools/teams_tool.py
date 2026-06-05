# -*- coding: utf-8 -*-
"""Teams Tool — Microsoft Teams chat / channel via Graph REST API.

Architecture
============
Single tool exposing seven actions for the most common Teams scenarios:

  list_chats           list user's recent 1:1 + group chats
  read_chat            read recent messages from a specific chat
  send_chat            send an HTML message to a chat
  list_teams           list joined teams (workspaces)
  list_channels        list channels in a team
  read_channel         read recent messages from a channel
  send_channel         post an HTML message to a channel

Authentication
--------------
Token acquisition is handled by ``infrastructure.teams_auth``; this
tool never sees a refresh token, just calls
``TeamsAuth.get_token_silent()`` once per request via TeamsClient.
First-time interactive sign-in is performed eagerly by
``TeamsContextProvider.prepare()`` so the agent never blocks on a
browser pop-up mid-step.

Concurrency
-----------
Mouse/keyboard would conflict with parallel desktop calls; Teams API
calls don't, but the singleton TeamsClient + token cache mutations
mean we still serialise here. ``is_concurrency_safe = False``.

Comparison to email_tool
------------------------
* email_tool talks to a local COM apartment and pins a single thread.
* teams_tool talks to an HTTPS endpoint via httpx; no thread pin
  required. Both share the asyncio.Lock idiom for predictable
  serialisation across agent steps.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from .base_tool import BaseTool, ToolResult
from ..infrastructure.logger import get_logger


# ── Module state ────────────────────────────────────────────────────────────
_teams_lock = asyncio.Lock()

# Separate lock for the lazy interactive sign-in. We don't want a long-blocking
# browser-login to hold _teams_lock and serialise everything; auth has its own
# critical section so a second concurrent agent waits for the same login flow
# instead of starting a duplicate browser pop-up.
_teams_auth_lock = asyncio.Lock()

# Hard cap on `top` per Graph API best practice: requesting more than
# 50 items per chat/channel paginates anyway and bloats the payload.
_MAX_TOP = 50

# Hard cap on outgoing HTML body so a runaway agent can't paste a
# 10 MB document into a chat. Teams' own UI also rejects multi-MB
# bodies; 32 KB is a generous ceiling for "long message".
_MAX_HTML_BODY = 32 * 1024


async def flush_teams_client() -> Dict[str, int]:
    """Session-boundary cleanup contract for the Teams clients.

    Mirrors :func:`browser_tool.flush_browser_pool` and
    :func:`desktop_tool.flush_desktop_store` so the FlowController
    dispatcher only sees one shape: ``async () -> something``.

    Closes both the Graph httpx.AsyncClient and the chatsvcagg
    httpx.AsyncClient when present. Best-effort — swallows IO failures.
    """
    closed = 0
    try:
        from ..infrastructure.teams_api import close_teams_client
        await close_teams_client()
        closed += 1
    except Exception:
        pass
    try:
        from ..infrastructure.teams_api import close_teams_chat_client
        await close_teams_chat_client()
        closed += 1
    except Exception:
        pass
    return {"closed": closed}


class TeamsTool(BaseTool):
    """Single tool exposing the Microsoft Teams Graph action set."""

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    # Calendar / meetings (Graph)
                    "list_calendar_events",
                    "get_event",
                    "create_meeting",
                    "respond_event",
                    "find_meeting_times",
                    # Chat / channel send (Graph)
                    "send_chat",
                    "send_channel",
                    # Chat / channel read (chatsvcagg internal)
                    "list_chats",
                    "read_chat",
                    "read_channel",
                    # Teams structure (Graph)
                    "list_teams",
                    "list_channels",
                    # People (Graph)
                    "find_person",
                    # Files (Graph / OneDrive)
                    "search_files",
                    "list_recent_files",
                    # Tasks (Graph / Microsoft To Do)
                    "list_tasks",
                    "create_task",
                    # Presence (presence.teams.microsoft.com internal)
                    "get_presence",
                ],
                "description": (
                    "Teams operation. Reads (list_*, read_*, get_*, "
                    "find_*) are non-disruptive. Writes (send_*, "
                    "create_*, respond_event) take real-world effect "
                    "— always discover ids with a list_* / find_* "
                    "call first."
                ),
            },
            # ── Identifiers ────────────────────────────────────────
            "chat_id": {
                "type": "string",
                "description": (
                    "[read_chat / send_chat] Chat identifier from "
                    "list_chats output (looks like '19:abc...@thread.v2')."
                ),
            },
            "team_id": {
                "type": "string",
                "description": (
                    "[list_channels / read_channel / send_channel] Team "
                    "identifier from list_teams output."
                ),
            },
            "channel_id": {
                "type": "string",
                "description": (
                    "[read_channel / send_channel] Channel identifier "
                    "from list_channels output."
                ),
            },
            "event_id": {
                "type": "string",
                "description": (
                    "[get_event / respond_event] Event identifier from "
                    "list_calendar_events output."
                ),
            },
            "user_id": {
                "type": "string",
                "description": (
                    "[get_presence] Teams MRI ('8:orgid:<oid>') of the "
                    "user whose presence to read. Omit to read your own."
                ),
            },
            # ── Common ─────────────────────────────────────────────
            "top": {
                "type": "integer",
                "description": (
                    "Max items to return. Default 10 for list_*/read_*; "
                    "hard cap 50. Paginate with multiple calls if you "
                    "need older history."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "[find_person / search_files] Free-text search query."
                ),
            },
            # ── Calendar params ────────────────────────────────────
            "start_after": {
                "type": "string",
                "description": (
                    "[list_calendar_events / find_meeting_times] ISO 8601 "
                    "lower bound (e.g. '2026-06-04T00:00:00+08:00'). "
                    "Default: now (UTC)."
                ),
            },
            "end_before": {
                "type": "string",
                "description": (
                    "[list_calendar_events / find_meeting_times] ISO 8601 "
                    "upper bound. Default: now + 14 days."
                ),
            },
            "subject": {
                "type": "string",
                "description": (
                    "[create_meeting] Event subject / title."
                ),
            },
            "start": {
                "type": "string",
                "description": (
                    "[create_meeting] ISO 8601 start time (e.g. "
                    "'2026-06-05T15:00:00')."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "[create_meeting] ISO 8601 end time."
                ),
            },
            "time_zone": {
                "type": "string",
                "description": (
                    "[create_meeting] IANA / Windows time zone for "
                    "start/end. Default 'UTC'. Use 'China Standard Time' "
                    "for Beijing, 'Pacific Standard Time' for US-West, etc."
                ),
            },
            "online": {
                "type": "boolean",
                "description": (
                    "[create_meeting] Attach a Teams online meeting "
                    "(join link in the response). Default true."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "[create_meeting] Optional physical location text."
                ),
            },
            "attendees": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "name":  {"type": "string"},
                        "type":  {
                            "type": "string",
                            "enum": ["required", "optional"],
                        },
                    },
                    "required": ["email"],
                },
                "description": (
                    "[create_meeting / find_meeting_times] List of "
                    "{email, name?, type?} dicts."
                ),
            },
            "duration_minutes": {
                "type": "integer",
                "description": (
                    "[find_meeting_times] Desired meeting length. "
                    "Default 30."
                ),
            },
            "response": {
                "type": "string",
                "enum": ["accept", "decline", "tentative"],
                "description": (
                    "[respond_event] How to respond to the invite."
                ),
            },
            "comment": {
                "type": "string",
                "description": (
                    "[respond_event] Optional reply text included in "
                    "the response."
                ),
            },
            "send_response": {
                "type": "boolean",
                "description": (
                    "[respond_event] Whether to send the response back "
                    "to the organiser. Default true."
                ),
            },
            # ── Send ───────────────────────────────────────────────
            "message_html": {
                "type": "string",
                "description": (
                    "[send_chat / send_channel] Message body in HTML. "
                    "Plain text accepted (Graph wraps it). 32 KB cap."
                ),
            },
            # ── Tasks ──────────────────────────────────────────────
            "title": {
                "type": "string",
                "description": (
                    "[create_task] Task title."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "[create_task] Optional task body / notes (plain text)."
                ),
            },
            "due_date": {
                "type": "string",
                "description": (
                    "[create_task] Optional due date as ISO 8601 "
                    "(e.g. '2026-06-10T17:00:00')."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        super().__init__("teams")
        self.logger = get_logger()

    async def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        _t0 = time.time()
        params: Dict[str, Any] = {"action": action, **kwargs}

        if not action:
            return self._fail(params, _t0, "teams tool requires 'action'.")

        dispatch = {
            # Calendar / meetings
            "list_calendar_events":  self._list_calendar_events,
            "get_event":             self._get_event,
            "create_meeting":        self._create_meeting,
            "respond_event":         self._respond_event,
            "find_meeting_times":    self._find_meeting_times,
            # Send (Graph)
            "send_chat":             self._send_chat,
            "send_channel":          self._send_channel,
            # Read (chatsvcagg internal)
            "list_chats":            self._list_chats,
            "read_chat":             self._read_chat,
            "read_channel":          self._read_channel,
            # Teams structure (Graph)
            "list_teams":            self._list_teams,
            "list_channels":         self._list_channels,
            # People / files / tasks (Graph)
            "find_person":           self._find_person,
            "search_files":          self._search_files,
            "list_recent_files":     self._list_recent_files,
            "list_tasks":            self._list_tasks,
            "create_task":           self._create_task,
            # Presence (chatsvcagg)
            "get_presence":          self._get_presence,
        }
        handler = dispatch.get(action)
        if handler is None:
            return self._fail(
                params, _t0,
                f"Unknown teams action: {action!r}. "
                f"Valid: {', '.join(dispatch)}",
            )

        # All Teams calls serialise — token cache mutation + singleton
        # client. Cheap (HTTP-bound) so no real throughput cost.
        async with _teams_lock:
            # Lazy auth gate — runs on every call, but the silent path is
            # ~1ms after the first successful sign-in (cache lookup only).
            # The interactive branch only fires on a true cold start
            # (cache empty / refresh-token expired) and pops a browser
            # exactly once thanks to _teams_auth_lock's double-check.
            auth_err = await self._ensure_token_or_auth()
            if auth_err:
                return self._fail(params, _t0, auth_err)
            try:
                return await handler(params, _t0, **kwargs)
            except Exception as exc:
                self.logger.error(
                    f"teams action {action!r} failed: {exc}",
                    component="TeamsTool", exc_info=True,
                )
                return self._fail(params, _t0, f"teams {action!r}: {exc}")

    # ── Action handlers ─────────────────────────────────────────────────
    async def _list_chats(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        # Routes through chatsvcagg internal API: Teams Web's MSAL
        # client doesn't get Chat.Read* scopes from Microsoft, so the
        # Graph endpoint always 403s. The internal API uses a different
        # audience token (chatsvcagg.teams.microsoft.com) which IS in
        # our cache after bootstrap.
        top = self._top_arg(kwargs, default=20)
        client = await self._chat_client()
        items = await client.list_chats(top=top)
        return self._ok(params, _t0, {"chats": items, "count": len(items)})

    async def _read_chat(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        chat_id = self._str_arg(kwargs, "chat_id")
        if not chat_id:
            return self._fail(params, _t0, "read_chat requires 'chat_id'.")
        top = self._top_arg(kwargs, default=20)
        client = await self._chat_client()
        items = await client.list_chat_messages(chat_id, top=top)
        return self._ok(params, _t0, {
            "chat_id":  chat_id,
            "messages": items,
            "count":    len(items),
        })

    async def _send_chat(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        chat_id = self._str_arg(kwargs, "chat_id")
        if not chat_id:
            return self._fail(params, _t0, "send_chat requires 'chat_id'.")
        body = self._str_arg(kwargs, "message_html")
        if not body:
            return self._fail(
                params, _t0, "send_chat requires non-empty 'message_html'."
            )
        if len(body) > _MAX_HTML_BODY:
            return self._fail(
                params, _t0,
                f"send_chat: message_html length {len(body)} exceeds cap "
                f"({_MAX_HTML_BODY}). Split into multiple messages or trim.",
            )
        client = await self._client()
        sent = await client.send_chat_message(chat_id, body)
        return self._ok(params, _t0, {
            "chat_id": chat_id, "sent": sent,
        })

    async def _list_teams(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        client = await self._client()
        items = await client.list_joined_teams()
        return self._ok(params, _t0, {"teams": items, "count": len(items)})

    async def _list_channels(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        team_id = self._str_arg(kwargs, "team_id")
        if not team_id:
            return self._fail(
                params, _t0, "list_channels requires 'team_id'."
            )
        client = await self._client()
        items = await client.list_channels(team_id)
        return self._ok(params, _t0, {
            "team_id": team_id,
            "channels": items,
            "count": len(items),
        })

    async def _read_channel(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        team_id = self._str_arg(kwargs, "team_id")
        channel_id = self._str_arg(kwargs, "channel_id")
        if not team_id or not channel_id:
            return self._fail(
                params, _t0,
                "read_channel requires both 'team_id' and 'channel_id'.",
            )
        top = self._top_arg(kwargs, default=20)
        # Same reasoning as _read_chat: Graph rejects without
        # ChannelMessage.Read.All; chatsvcagg accepts the existing token.
        client = await self._chat_client()
        items = await client.list_channel_messages(team_id, channel_id, top=top)
        return self._ok(params, _t0, {
            "team_id":    team_id,
            "channel_id": channel_id,
            "messages":   items,
            "count":      len(items),
        })

    async def _send_channel(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        team_id = self._str_arg(kwargs, "team_id")
        channel_id = self._str_arg(kwargs, "channel_id")
        if not team_id or not channel_id:
            return self._fail(
                params, _t0,
                "send_channel requires both 'team_id' and 'channel_id'.",
            )
        body = self._str_arg(kwargs, "message_html")
        if not body:
            return self._fail(
                params, _t0,
                "send_channel requires non-empty 'message_html'.",
            )
        if len(body) > _MAX_HTML_BODY:
            return self._fail(
                params, _t0,
                f"send_channel: message_html length {len(body)} exceeds cap "
                f"({_MAX_HTML_BODY}). Split into multiple messages or trim.",
            )
        client = await self._client()
        sent = await client.send_channel_message(team_id, channel_id, body)
        return self._ok(params, _t0, {
            "team_id": team_id,
            "channel_id": channel_id,
            "sent": sent,
        })

    # ── Calendar / meetings (Graph) ─────────────────────────────────────
    async def _list_calendar_events(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        top = self._top_arg(kwargs, default=10)
        start_after = self._str_arg(kwargs, "start_after") or None
        end_before  = self._str_arg(kwargs, "end_before")  or None
        client = await self._client()
        items = await client.list_calendar_events(
            top=top, start_after=start_after, end_before=end_before,
        )
        return self._ok(params, _t0, {
            "events":      items,
            "count":       len(items),
            "start_after": start_after or "",
            "end_before":  end_before  or "",
        })

    async def _get_event(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        event_id = self._str_arg(kwargs, "event_id")
        if not event_id:
            return self._fail(params, _t0, "get_event requires 'event_id'.")
        client = await self._client()
        ev = await client.get_event(event_id)
        return self._ok(params, _t0, ev or {})

    async def _create_meeting(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        subject = self._str_arg(kwargs, "subject")
        start_  = self._str_arg(kwargs, "start")
        end_    = self._str_arg(kwargs, "end")
        if not subject or not start_ or not end_:
            return self._fail(
                params, _t0,
                "create_meeting requires 'subject', 'start', 'end' "
                "(ISO 8601 datetimes).",
            )
        attendees = kwargs.get("attendees") or []
        if not isinstance(attendees, list):
            return self._fail(
                params, _t0, "create_meeting: attendees must be a list",
            )
        body_html = self._str_arg(kwargs, "body") or self._str_arg(kwargs, "message_html")
        online    = bool(kwargs.get("online", True))
        location  = self._str_arg(kwargs, "location")
        time_zone = self._str_arg(kwargs, "time_zone") or "UTC"
        client = await self._client()
        ev = await client.create_meeting(
            subject=subject, start=start_, end=end_,
            attendees=attendees, body_html=body_html,
            online=online, location=location, time_zone=time_zone,
        )
        return self._ok(params, _t0, ev or {})

    async def _respond_event(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        event_id = self._str_arg(kwargs, "event_id")
        if not event_id:
            return self._fail(params, _t0, "respond_event requires 'event_id'.")
        response = self._str_arg(kwargs, "response") or "accept"
        comment  = self._str_arg(kwargs, "comment")
        send_response = bool(kwargs.get("send_response", True))
        client = await self._client()
        out = await client.respond_event(
            event_id=event_id, response=response,
            comment=comment, send_response=send_response,
        )
        return self._ok(params, _t0, out or {})

    async def _find_meeting_times(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        attendees = kwargs.get("attendees") or []
        if not isinstance(attendees, list) or not attendees:
            return self._fail(
                params, _t0,
                "find_meeting_times requires non-empty 'attendees' list.",
            )
        try:
            duration = int(kwargs.get("duration_minutes") or 30)
        except (TypeError, ValueError):
            duration = 30
        start_after = self._str_arg(kwargs, "start_after") or None
        end_before  = self._str_arg(kwargs, "end_before")  or None
        client = await self._client()
        out = await client.find_meeting_times(
            attendees=attendees, duration_minutes=duration,
            start_after=start_after, end_before=end_before,
        )
        return self._ok(params, _t0, out)

    # ── People (Graph) ──────────────────────────────────────────────────
    async def _find_person(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        query = self._str_arg(kwargs, "query")
        if not query:
            return self._fail(params, _t0, "find_person requires 'query'.")
        top = self._top_arg(kwargs, default=10)
        client = await self._client()
        items = await client.find_person(query, top=top)
        return self._ok(params, _t0, {
            "query":  query,
            "people": items,
            "count":  len(items),
        })

    # ── Files (Graph / OneDrive) ────────────────────────────────────────
    async def _search_files(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        query = self._str_arg(kwargs, "query")
        if not query:
            return self._fail(params, _t0, "search_files requires 'query'.")
        top = self._top_arg(kwargs, default=10)
        client = await self._client()
        items = await client.search_files(query, top=top)
        return self._ok(params, _t0, {
            "query": query, "files": items, "count": len(items),
        })

    async def _list_recent_files(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        top = self._top_arg(kwargs, default=10)
        client = await self._client()
        items = await client.list_recent_files(top=top)
        return self._ok(params, _t0, {"files": items, "count": len(items)})

    # ── Tasks (Graph / Microsoft To Do) ─────────────────────────────────
    async def _list_tasks(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        top = self._top_arg(kwargs, default=20)
        client = await self._client()
        items = await client.list_tasks(top=top)
        return self._ok(params, _t0, {"tasks": items, "count": len(items)})

    async def _create_task(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        title = self._str_arg(kwargs, "title")
        if not title:
            return self._fail(params, _t0, "create_task requires 'title'.")
        body = self._str_arg(kwargs, "body")
        due_date = self._str_arg(kwargs, "due_date") or None
        client = await self._client()
        out = await client.create_task(title=title, body=body, due_date=due_date)
        return self._ok(params, _t0, out or {})

    # ── Presence (chatsvcagg internal) ──────────────────────────────────
    async def _get_presence(
        self, params: Dict[str, Any], _t0: float, **kwargs: Any,
    ) -> ToolResult:
        user_id = self._str_arg(kwargs, "user_id")
        client = await self._chat_client()
        if user_id:
            out = await client.get_user_presence(user_id)
        else:
            out = await client.get_my_presence()
        return self._ok(params, _t0, out or {})

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _str_arg(kwargs: Dict[str, Any], key: str) -> str:
        v = kwargs.get(key)
        return str(v).strip() if v is not None else ""

    @staticmethod
    def _top_arg(kwargs: Dict[str, Any], default: int) -> int:
        try:
            v = int(kwargs.get("top") or default)
        except (TypeError, ValueError):
            v = default
        return max(1, min(v, _MAX_TOP))

    async def _client(self):
        from ..infrastructure.teams_api import get_teams_client
        return await get_teams_client()

    async def _chat_client(self):
        """Lazy accessor for the chatsvcagg internal API client.

        Used by the read-side actions (list_chats, read_chat,
        read_channel, get_presence) which Microsoft does not grant via
        Graph for the Teams Web client. The underlying tokens (chatsvcagg
        + presence audiences) are harvested into teams_auth's cache
        during the same bootstrap as the Graph token, so the agent
        sees one unified 'auth-or-not' state regardless of which
        backend a given action talks to.
        """
        from ..infrastructure.teams_api import get_teams_chat_client
        return await get_teams_chat_client()

    async def _ensure_token_or_auth(self) -> Optional[str]:
        """Make sure a Graph access token is available; bootstrap from
        the user's already-signed-in Teams Web session when the cache
        is cold or the refresh token has expired.

        Returns ``None`` on success, or an error message string when
        bootstrap fails (browser_profile locked, MSAL.js shape changed,
        token endpoint rejected the RT).

        Why this lives in the tool (not in prepare()): prepare() runs
        once at step start. If the user installs deps mid-step, or the
        RT expires while the agent is mid-task, prepare() can't retry.
        Putting the auth gate on every tool call makes the recovery
        path automatic — the next call after a fix proceeds.
        """
        try:
            from ..infrastructure.teams_api import get_teams_auth, TeamsAuthError
        except Exception as exc:
            return f"teams auth module unavailable: {exc}"

        loop = asyncio.get_event_loop()
        try:
            auth = await loop.run_in_executor(None, get_teams_auth)
        except TeamsAuthError as exc:
            return str(exc)
        except Exception as exc:
            return f"teams auth init failed: {exc}"

        # Fast path — Graph AT in cache, valid for >60s.
        try:
            token = await auth.get_graph_token()
        except TeamsAuthError as exc:
            return f"teams token endpoint error: {exc}"
        except Exception as exc:
            return f"teams token error: {exc}"
        if token:
            return None

        # Cold start (or RT expired). Run web bridge under the auth
        # lock so concurrent agent calls share a single bootstrap.
        async with _teams_auth_lock:
            # Double-check: another waiter may have just bootstrapped.
            try:
                token = await auth.get_graph_token()
            except Exception:
                token = None
            if token:
                return None

            try:
                from ..infrastructure.teams_web_bridge import (
                    bootstrap_from_teams_web, BootstrapError,
                )
            except Exception as exc:
                return f"teams web bridge unavailable: {exc}"

            self.logger.info(
                "teams: cache cold or token expired; opening browser_profile "
                "against teams.microsoft.com to harvest fresh access tokens "
                "from the user's Teams Web session.",
                component="TeamsTool",
            )
            try:
                await bootstrap_from_teams_web()
            except BootstrapError as exc:
                # Map common bootstrap codes to actionable messages so
                # the agent can either retry or report something useful
                # to the user.
                if exc.code == "profile_locked":
                    return (
                        "Teams bootstrap blocked: the browser profile is "
                        "currently in use by another tool. Close any "
                        "ongoing browser actions and retry."
                    )
                if exc.code == "playwright_missing":
                    return (
                        "Teams bootstrap requires playwright. "
                        "Run: pip install playwright && playwright install chromium"
                    )
                if exc.code == "no_graph_token":
                    return (
                        "Teams bootstrap completed but no Microsoft Graph "
                        "access token was acquired. The user may need to "
                        "sign in to teams.microsoft.com manually in the "
                        "browser_profile (the SSO session may have expired). "
                        "Ask them to retry."
                    )
                if exc.code == "parse_failed":
                    return (
                        f"Teams bootstrap could not locate any AccessToken "
                        f"credentials in localStorage. Microsoft may have "
                        f"changed the MSAL.js storage format — re-run "
                        f"scripts/verify_teams_token_storage.py to inspect, "
                        f"then update parse_msal_access_tokens. Detail: {exc}"
                    )
                return f"Teams bootstrap failed: {exc}"
            except Exception as exc:
                return f"Teams bootstrap raised: {exc}"

            # Bootstrap installed and smoke-tested the token; one more
            # call should hit the cache.
            try:
                token = await auth.get_graph_token()
            except Exception as exc:
                return f"teams token error after bootstrap: {exc}"
            if not token:
                return (
                    "Teams bootstrap completed but no access token was "
                    "issued. The user may need to re-sign-in to Teams "
                    "Web in the browser_profile."
                )
        return None

    def _ok(
        self, params: Dict[str, Any], _t0: float, output: Any,
    ) -> ToolResult:
        return ToolResult(
            success=True, output=output,
            tool_name=self.name, tool_parameters=params,
            execution_time=time.time() - _t0,
        )

    def _fail(
        self, params: Dict[str, Any], _t0: float, msg: str,
    ) -> ToolResult:
        return ToolResult(
            success=False, output=None, error=msg,
            tool_name=self.name, tool_parameters=params,
            execution_time=time.time() - _t0,
        )
