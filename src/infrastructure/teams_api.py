# -*- coding: utf-8 -*-
"""TeamsApi — auth + Graph + Teams-internal-API in one module.

This module is the single source of truth for "how HandQ talks to
Microsoft" on behalf of the Teams tool. It is split into three
sections that share state (the on-disk token cache + cached httpx
sessions) but address different surfaces:

  1. **TeamsAuth** — local cache of access tokens harvested from
     Teams Web. No network calls; only reads / writes
     ``%USERPROFILE%/HandQ/teams_cache/tokens.json``. The tokens
     themselves come from ``teams_web_bridge.bootstrap_from_teams_web``
     which reads them out of MSAL.js's localStorage.

  2. **TeamsClient** — async thin wrapper over Microsoft Graph REST
     endpoints (calendar, send_chat, list_teams, find_person, files,
     tasks). Uses the Graph audience token from the cache.

  3. **TeamsChatClient** — wrapper over Teams' internal API for the
     scopes Microsoft does not grant via Graph (Chat.Read*,
     ChannelMessage.Read.All). The chat roster + presence use the
     chatsvcagg.teams.microsoft.com audience token; per-conversation
     message history uses the ic3.teams.office.com token against the
     region-scoped /api/chatsvc/{region}/v1 endpoint (the chatsvcagg
     aggregator does not serve message bodies).

Why one file?
-------------
The three concerns share state and identifiers (the token cache, the
``GRAPH_RESOURCE`` constant, the per-resource token dispatch). Splitting
them into three files added cross-imports without separating any real
ownership boundary. They're deployed together, evolve together, and
fail together; one file is easier to navigate.

The only Teams-related modules that stay separate:

  * ``teams_setup.py`` — ContextProvider + agent hint (each tool
    has one of these; pattern-match with ``email_setup`` etc.).
  * ``teams_web_bridge.py`` — Playwright-based bootstrap. It is the
    only Teams module that depends on Playwright; keeping it separate
    lets ``teams_api`` stay import-cheap.

Why no OAuth refresh
--------------------
Microsoft Teams Web is a Single-Page App. Microsoft's policy gives
SPA refresh tokens a hard 24-hour ceiling and refuses to extend them
via the OAuth token endpoint (AADSTS700084 — observed). So we don't
bother with refresh tokens at all. Re-running the browser bootstrap
is far simpler and almost as fast (cookies are warm, MSAL silently
renews in ~3 seconds).

Cache file shape (``%USERPROFILE%/HandQ/teams_cache/tokens.json``)::

    {
      "client_id":   "5e3ce6c0-2b1f-4285-8d4b-75ee78787346",
      "tenant_id":   "<GUID>",
      "account":     "<oid>.<tid>",
      "username":    "alice@example.com",
      "region":      "amer",
      "tokens": {
        "https://graph.microsoft.com": {
            "access_token": "eyJ...",
            "expires_at":   1780600000   # unix seconds
        },
        "https://chatsvcagg.teams.microsoft.com": { ... },
        ...
      }
    }
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx  # type: ignore[import-not-found]

from .config_manager import ConfigManager
from .logger import get_logger


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — TeamsAuth (token cache)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_CACHE_DIR = Path(os.path.expanduser("~")) / "HandQ" / "teams_cache"
_DEFAULT_CACHE_FILE = "tokens.json"

# Resource (audience) shorthand — keys for the per-resource AT cache.
GRAPH_RESOURCE = "https://graph.microsoft.com"

# Refresh proactively when fewer than this many seconds remain so an
# in-flight tool call never trips a stale-token 401. Re-bootstrapping
# takes ~3-5 seconds; refreshing 60s early absorbs that without ever
# letting a 401 leak to the agent.
_AT_EXPIRY_BUFFER_S = 60


class TeamsAuthError(RuntimeError):
    """Raised on configuration / cache issues. ``code`` distinguishes
    setup problems from cache corruption."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# Singleton state
_auth_instance_lock = threading.Lock()
_auth_instance: Optional["TeamsAuth"] = None


def get_teams_auth() -> "TeamsAuth":
    """Process-wide TeamsAuth singleton."""
    global _auth_instance
    if _auth_instance is not None:
        return _auth_instance
    with _auth_instance_lock:
        if _auth_instance is None:
            _auth_instance = TeamsAuth()
    return _auth_instance


class TeamsAuth:
    """On-disk access-token cache. No network calls — bootstrap is
    delegated to ``teams_web_bridge``."""

    def __init__(self) -> None:
        self.logger = get_logger()
        cfg: Dict[str, Any] = {}
        try:
            cfg = ConfigManager().get_section("teams") or {}
        except Exception:
            cfg = {}

        cache_dir_cfg = (cfg.get("cache_dir") or "").strip()
        self.cache_dir: Path = (
            Path(cache_dir_cfg) if cache_dir_cfg else _DEFAULT_CACHE_DIR
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path: Path = self.cache_dir / _DEFAULT_CACHE_FILE

        self._cache: Dict[str, Any] = self._load_cache()

    # ── Cache I/O ────────────────────────────────────────────────────────
    def _load_cache(self) -> Dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning(
                f"TeamsAuth: tokens.json unreadable, starting fresh: {exc}",
                component="TeamsAuth",
            )
            return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.warning(
                f"TeamsAuth: failed to persist tokens.json: {exc}",
                component="TeamsAuth",
            )

    # ── Public API ───────────────────────────────────────────────────────
    def is_bootstrapped(self) -> bool:
        """True iff we have at least one access token (any resource)."""
        return bool((self._cache.get("tokens") or {}))

    def signed_in_account(self) -> Optional[str]:
        return self._cache.get("username") or self._cache.get("account")

    def get_region(self) -> str:
        """Teams messaging region (e.g. ``amer``) harvested at bootstrap.

        Used to build the region-scoped chatsvc message-read URL. Empty
        string when unknown — callers should treat that as "needs
        re-bootstrap" rather than guessing a region.
        """
        return self._cache.get("region") or ""

    def install_bootstrap(self, creds: Dict[str, Any]) -> None:
        """Persist identity + access tokens extracted by teams_web_bridge.

        Expected shape of ``creds``::

            {
              "client_id":  str,
              "tenant_id":  str,
              "account":    str,           # homeAccountId
              "username":   str,           # may be ""
              "region":     str,           # messaging region, e.g. "amer"
              "tokens":     {
                  "https://graph.microsoft.com": {
                      "access_token": str,
                      "expires_at":   int  # unix seconds
                  },
                  ...
              }
            }

        Wipes any previously cached tokens because the new bootstrap
        may belong to a different account / tenant.
        """
        self._cache = {
            "client_id":     creds.get("client_id") or "",
            "tenant_id":     creds.get("tenant_id") or "",
            "account":       creds.get("account") or "",
            "username":      creds.get("username") or "",
            "region":        creds.get("region") or "",
            "tokens":        dict(creds.get("tokens") or {}),
        }
        self._save_cache()

    def clear_cache(self) -> None:
        """Wipe everything. Used after detected tampering or to force
        a fresh bootstrap on the next call."""
        self._cache = {}
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except Exception:
            pass

    async def get_graph_token(self) -> Optional[str]:
        """Return a Microsoft Graph access token from the cache, or
        ``None`` when none is cached or it's expired / about to expire.

        ``None`` is the caller's signal to run the browser bootstrap
        again — TeamsAuth itself never makes network calls.
        """
        return self._get_token_for(GRAPH_RESOURCE)

    async def get_token_for_resource(self, resource: str) -> Optional[str]:
        """Lookup any other resource's AT (e.g.
        ``https://chatsvcagg.teams.microsoft.com``) for non-Graph APIs.
        Same caching rules as Graph.
        """
        return self._get_token_for(resource)

    # ── Internals ────────────────────────────────────────────────────────
    def _get_token_for(self, resource: str) -> Optional[str]:
        entry = (self._cache.get("tokens") or {}).get(resource)
        if not entry:
            return None
        if (entry.get("expires_at", 0) - _AT_EXPIRY_BUFFER_S) <= time.time():
            return None
        return entry.get("access_token")

    def has_unexpired_token(self, resource: str = GRAPH_RESOURCE) -> bool:
        """Cheap predicate used by teams_tool's auth gate to decide
        whether to bootstrap. Same expiry buffer as get_graph_token.
        """
        return self._get_token_for(resource) is not None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — TeamsClient (Microsoft Graph)
# ─────────────────────────────────────────────────────────────────────────────

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class TeamsClientError(RuntimeError):
    """Raised on auth failure, HTTP error, or transport failure.

    Carries ``status`` (int, 0 when transport failed) and ``retry_after``
    (float, seconds — populated for 429 responses).
    """

    def __init__(self, message: str, status: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


_graph_client_lock = asyncio.Lock()
_graph_client_instance: Optional["TeamsClient"] = None


async def get_teams_client() -> "TeamsClient":
    """Return the process-wide TeamsClient singleton (async-safe)."""
    global _graph_client_instance
    if _graph_client_instance is not None:
        return _graph_client_instance
    async with _graph_client_lock:
        if _graph_client_instance is None:
            _graph_client_instance = TeamsClient()
    return _graph_client_instance


async def close_teams_client() -> None:
    """Close the underlying httpx session. Called from session-boundary cleanup."""
    global _graph_client_instance
    if _graph_client_instance is None:
        return
    try:
        await _graph_client_instance.aclose()
    finally:
        _graph_client_instance = None


class TeamsClient:
    """Lazy httpx.AsyncClient + Graph API operations."""

    def __init__(self, auth: Optional[TeamsAuth] = None) -> None:
        self.logger = get_logger()
        self._auth = auth or get_teams_auth()
        self._http = None  # lazy httpx.AsyncClient

    async def _ensure_http(self):
        if self._http is not None:
            return self._http
        # 30s default — Graph endpoints respond in < 2s normally; the
        # generous timeout absorbs the occasional cold-cache lookup.
        # verify=False mirrors the corporate-proxy fix for SSL inspection.
        self._http = httpx.AsyncClient(timeout=30.0, verify=False)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    # ── Internal request helper ─────────────────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Issue a Graph request and return parsed JSON.

        Raises TeamsClientError on auth failure, non-2xx status, or
        transport error. 429 carries Retry-After in seconds.
        """
        try:
            token = await self._auth.get_graph_token()
        except Exception as exc:
            raise TeamsClientError(
                f"could not obtain Graph token: {exc}", status=401,
            ) from exc
        if not token:
            raise TeamsClientError(
                "no Microsoft Teams Web session cached. The teams tool "
                "needs to bootstrap once via the browser; trigger a "
                "teams action and the auth gate will open Teams Web "
                "automatically.",
                status=401,
            )
        http = await self._ensure_http()
        url = path if path.startswith("http") else f"{_GRAPH_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = await http.request(
                method, url, params=params, json=json_body, headers=headers,
            )
        except Exception as exc:
            raise TeamsClientError(f"transport error: {exc}", status=0) from exc

        # 401 — token rejected. We don't retry; the silent path already
        # refreshed if it could. Surface to caller so user re-signs in.
        if resp.status_code == 401:
            raise TeamsClientError(
                "Graph rejected the access token (401). The user needs to "
                "re-authenticate; clear the token cache and retry.",
                status=401,
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After") or 1.0)
            raise TeamsClientError(
                f"Graph throttled the request (429). Retry after "
                f"{retry_after:.0f} seconds.",
                status=429,
                retry_after=retry_after,
            )
        if resp.status_code >= 400:
            # Surface Graph's own error JSON when present — usually has
            # actionable detail (missing permission, bad id format, etc.)
            detail = resp.text
            try:
                err = resp.json()
                if isinstance(err, dict) and "error" in err:
                    e = err["error"]
                    detail = f"{e.get('code', '?')}: {e.get('message', '')}"
            except Exception:
                pass
            raise TeamsClientError(
                f"Graph {method} {path} → {resp.status_code}: {detail}",
                status=resp.status_code,
            )

        # 204 No Content — POST replies usually return the created item,
        # but a few endpoints return 204; handle gracefully.
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise TeamsClientError(
                f"Graph response was not JSON: {exc}", status=resp.status_code,
            ) from exc

    # ── Helpers (shape simplification) ───────────────────────────────────
    @staticmethod
    def _shape_chat(c: Dict[str, Any]) -> Dict[str, Any]:
        members = c.get("members") or []
        member_names = [
            (m.get("displayName") or m.get("email") or "(unknown)")
            for m in members
            if isinstance(m, dict)
        ]
        return {
            "chat_id": c.get("id"),
            "topic": c.get("topic") or "",
            "chat_type": c.get("chatType") or "",
            "last_updated": c.get("lastUpdatedDateTime") or "",
            "members": member_names,
        }

    @staticmethod
    def _shape_message(m: Dict[str, Any]) -> Dict[str, Any]:
        from_user = ""
        f = (m.get("from") or {}).get("user") or {}
        if isinstance(f, dict):
            from_user = f.get("displayName") or ""
        body = (m.get("body") or {}).get("content") or ""
        return {
            "message_id": m.get("id"),
            "from": from_user,
            "created_at": m.get("createdDateTime") or "",
            "body_html": body,
            "content_type": (m.get("body") or {}).get("contentType") or "html",
            "importance": m.get("importance") or "normal",
            "subject": m.get("subject") or "",
            "deleted": m.get("deletedDateTime") is not None,
        }

    # ── Public API: chats (Graph; chat read 403s — see TeamsChatClient) ─
    async def list_chats(self, top: int = 50) -> List[Dict[str, Any]]:
        top = max(1, min(int(top), 50))
        data = await self._request(
            "GET", "/me/chats",
            params={"$top": top, "$expand": "members"},
        )
        return [self._shape_chat(c) for c in (data.get("value") or [])]

    async def list_chat_messages(
        self, chat_id: str, top: int = 50,
    ) -> List[Dict[str, Any]]:
        if not chat_id:
            raise TeamsClientError("list_chat_messages: chat_id is required")
        top = max(1, min(int(top), 50))
        data = await self._request(
            "GET", f"/chats/{chat_id}/messages",
            params={"$top": top},
        )
        return [self._shape_message(m) for m in (data.get("value") or [])]

    async def send_chat_message(
        self, chat_id: str, html_body: str,
    ) -> Dict[str, Any]:
        if not chat_id:
            raise TeamsClientError("send_chat_message: chat_id is required")
        if not html_body:
            raise TeamsClientError("send_chat_message: html_body is required")
        body = {"body": {"contentType": "html", "content": html_body}}
        data = await self._request(
            "POST", f"/chats/{chat_id}/messages", json_body=body,
        )
        return self._shape_message(data) if data else {}

    # ── Public API: teams + channels ────────────────────────────────────
    async def list_joined_teams(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/me/joinedTeams")
        return [
            {
                "team_id": t.get("id"),
                "name": t.get("displayName") or "",
                "description": t.get("description") or "",
            }
            for t in (data.get("value") or [])
        ]

    async def list_channels(self, team_id: str) -> List[Dict[str, Any]]:
        if not team_id:
            raise TeamsClientError("list_channels: team_id is required")
        data = await self._request("GET", f"/teams/{team_id}/channels")
        return [
            {
                "channel_id": c.get("id"),
                "name": c.get("displayName") or "",
                "description": c.get("description") or "",
                "membership_type": c.get("membershipType") or "",
            }
            for c in (data.get("value") or [])
        ]

    async def list_channel_messages(
        self, team_id: str, channel_id: str, top: int = 50,
    ) -> List[Dict[str, Any]]:
        if not team_id or not channel_id:
            raise TeamsClientError(
                "list_channel_messages: both team_id and channel_id are required"
            )
        top = max(1, min(int(top), 50))
        data = await self._request(
            "GET", f"/teams/{team_id}/channels/{channel_id}/messages",
            params={"$top": top},
        )
        return [self._shape_message(m) for m in (data.get("value") or [])]

    async def send_channel_message(
        self, team_id: str, channel_id: str, html_body: str,
    ) -> Dict[str, Any]:
        if not team_id or not channel_id:
            raise TeamsClientError(
                "send_channel_message: both team_id and channel_id are required"
            )
        if not html_body:
            raise TeamsClientError("send_channel_message: html_body is required")
        body = {"body": {"contentType": "html", "content": html_body}}
        data = await self._request(
            "POST", f"/teams/{team_id}/channels/{channel_id}/messages",
            json_body=body,
        )
        return self._shape_message(data) if data else {}

    # ── Public API: calendar events ─────────────────────────────────────
    @staticmethod
    def _shape_event(e: Dict[str, Any]) -> Dict[str, Any]:
        organizer = ""
        org = (e.get("organizer") or {}).get("emailAddress") or {}
        if isinstance(org, dict):
            organizer = org.get("name") or org.get("address") or ""
        attendees: List[Dict[str, Any]] = []
        for a in (e.get("attendees") or []):
            if not isinstance(a, dict):
                continue
            ea = a.get("emailAddress") or {}
            attendees.append({
                "name":     ea.get("name") or "",
                "email":    ea.get("address") or "",
                "response": (a.get("status") or {}).get("response") or "none",
                "type":     a.get("type") or "required",
            })
        online = e.get("onlineMeeting") or {}
        join_url = online.get("joinUrl") if isinstance(online, dict) else ""
        location = ""
        loc = e.get("location") or {}
        if isinstance(loc, dict):
            location = loc.get("displayName") or ""
        return {
            "event_id":          e.get("id"),
            "subject":           e.get("subject") or "",
            "start":             (e.get("start") or {}).get("dateTime") or "",
            "start_tz":          (e.get("start") or {}).get("timeZone") or "",
            "end":               (e.get("end") or {}).get("dateTime") or "",
            "end_tz":            (e.get("end") or {}).get("timeZone") or "",
            "is_online_meeting": bool(e.get("isOnlineMeeting")),
            "join_url":          join_url or "",
            "organizer":         organizer,
            "attendees":         attendees,
            "location":          location,
            "is_cancelled":      bool(e.get("isCancelled")),
            "preview":           e.get("bodyPreview") or "",
            "web_link":          e.get("webLink") or "",
        }

    async def list_calendar_events(
        self,
        top: int = 10,
        start_after: Optional[str] = None,
        end_before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List events in a time window via /me/calendarView.

        Default window: now → now + 14 days. Pass ISO 8601 datetimes
        (with offset, e.g. ``2026-06-04T00:00:00+08:00``) for explicit
        bounds. calendarView correctly expands recurring events into
        their occurrences in the window.
        """
        from datetime import datetime, timezone, timedelta
        top = max(1, min(int(top), 50))
        now = datetime.now(timezone.utc)
        sa = start_after or now.isoformat().replace("+00:00", "Z")
        eb = end_before or (now + timedelta(days=14)).isoformat().replace("+00:00", "Z")
        data = await self._request(
            "GET", "/me/calendarView",
            params={
                "startDateTime": sa,
                "endDateTime":   eb,
                "$top":          top,
                "$orderby":      "start/dateTime",
            },
        )
        return [self._shape_event(e) for e in (data.get("value") or [])]

    async def get_event(self, event_id: str) -> Dict[str, Any]:
        if not event_id:
            raise TeamsClientError("get_event: event_id is required")
        data = await self._request("GET", f"/me/events/{event_id}")
        return self._shape_event(data) if data else {}

    async def create_meeting(
        self,
        subject: str,
        start: str,
        end: str,
        attendees: Optional[List[Dict[str, str]]] = None,
        body_html: str = "",
        online: bool = True,
        location: str = "",
        time_zone: str = "UTC",
    ) -> Dict[str, Any]:
        """Create a calendar event with an optional Teams online meeting.

        ``attendees`` is a list of ``{"email": "...", "name": "...",
        "type": "required"|"optional"}`` dicts. Setting ``online=True``
        (default) attaches a Teams join link to the event.
        """
        if not subject or not start or not end:
            raise TeamsClientError(
                "create_meeting: subject, start, end are required"
            )
        body: Dict[str, Any] = {
            "subject":         subject,
            "start":           {"dateTime": start, "timeZone": time_zone},
            "end":             {"dateTime": end,   "timeZone": time_zone},
            "isOnlineMeeting": bool(online),
        }
        if online:
            body["onlineMeetingProvider"] = "teamsForBusiness"
        if body_html:
            body["body"] = {"contentType": "html", "content": body_html}
        if location:
            body["location"] = {"displayName": location}
        if attendees:
            body["attendees"] = [
                {
                    "emailAddress": {
                        "address": a["email"],
                        "name":    a.get("name", ""),
                    },
                    "type": a.get("type", "required"),
                }
                for a in attendees if a.get("email")
            ]
        data = await self._request("POST", "/me/events", json_body=body)
        return self._shape_event(data) if data else {}

    async def respond_event(
        self,
        event_id: str,
        response: str = "accept",
        comment: str = "",
        send_response: bool = True,
    ) -> Dict[str, Any]:
        """Accept / decline / tentatively accept a meeting invite."""
        if not event_id:
            raise TeamsClientError("respond_event: event_id is required")
        norm = (response or "").strip().lower()
        # Common synonyms — Graph wants the exact verb in the URL.
        alias = {
            "accept":            "accept",
            "yes":               "accept",
            "decline":           "decline",
            "no":                "decline",
            "tentative":         "tentativelyAccept",
            "tentativelyaccept": "tentativelyAccept",
            "maybe":             "tentativelyAccept",
        }
        if norm not in alias:
            raise TeamsClientError(
                f"respond_event: invalid response {response!r} "
                "(use accept / decline / tentative)"
            )
        verb = alias[norm]
        body: Dict[str, Any] = {"sendResponse": bool(send_response)}
        if comment:
            body["comment"] = comment
        await self._request(
            "POST", f"/me/events/{event_id}/{verb}", json_body=body,
        )
        return {"event_id": event_id, "response": verb, "ok": True}

    async def find_meeting_times(
        self,
        attendees: List[Dict[str, str]],
        duration_minutes: int = 30,
        start_after: Optional[str] = None,
        end_before: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ask Graph to suggest time slots when everyone is free.

        ``attendees`` is the same shape as ``create_meeting``.
        """
        from datetime import datetime, timezone, timedelta
        if not attendees:
            raise TeamsClientError("find_meeting_times: attendees required")
        now = datetime.now(timezone.utc)
        sa = start_after or now.isoformat().replace("+00:00", "Z")
        eb = end_before or (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
        body = {
            "attendees": [
                {
                    "emailAddress": {
                        "address": a["email"],
                        "name":    a.get("name", ""),
                    },
                    "type": a.get("type", "Required"),
                }
                for a in attendees if a.get("email")
            ],
            "timeConstraint": {
                "timeslots": [{
                    "start": {"dateTime": sa, "timeZone": "UTC"},
                    "end":   {"dateTime": eb, "timeZone": "UTC"},
                }],
            },
            "meetingDuration":          f"PT{int(duration_minutes)}M",
            "returnSuggestionReasons":  True,
            "minimumAttendeePercentage": 100,
        }
        data = await self._request(
            "POST", "/me/findMeetingTimes", json_body=body,
        )
        suggestions: List[Dict[str, Any]] = []
        for s in (data.get("meetingTimeSuggestions") or []):
            slot = s.get("meetingTimeSlot") or {}
            suggestions.append({
                "confidence": s.get("confidence"),
                "start":      (slot.get("start") or {}).get("dateTime") or "",
                "end":        (slot.get("end")   or {}).get("dateTime") or "",
                "reason":     s.get("suggestionReason") or "",
            })
        return {"suggestions": suggestions, "count": len(suggestions)}

    # ── Public API: people ──────────────────────────────────────────────
    async def find_person(self, query: str, top: int = 10) -> List[Dict[str, Any]]:
        """Search the user's relevance graph for a colleague by name /
        email. Returns display name, title, department, email candidates.

        Email handling: Graph's ``$search`` parameter rejects '@' even
        when URL-encoded, AND ``$filter`` on ``/me/people`` doesn't
        accept ``scoredEmailAddresses/any(...)``. So when the query
        looks like an email, we route to ``GET /users/{upn}`` directly
        — Microsoft Graph supports this for any user in the same
        organisation under the ``User.ReadBasic.All`` scope (which the
        Teams Web client has).
        """
        if not query:
            raise TeamsClientError("find_person: query is required")
        top = max(1, min(int(top), 25))

        looks_like_email = "@" in query and "." in query.split("@", 1)[-1]
        if looks_like_email:
            # Direct user lookup. Returns a single user dict on success,
            # or 404 if the email is not in the org's directory.
            try:
                u = await self._request("GET", f"/users/{quote(query)}")
            except TeamsClientError as exc:
                if exc.status == 404:
                    return []
                raise
            if not isinstance(u, dict) or not u.get("id"):
                return []
            return [{
                "id":           u.get("id"),
                "display_name": u.get("displayName") or "",
                "given_name":   u.get("givenName") or "",
                "surname":      u.get("surname") or "",
                "title":        u.get("jobTitle") or "",
                "department":   u.get("department") or "",
                "office":       u.get("officeLocation") or "",
                "emails":       [u.get("mail") or u.get("userPrincipalName") or ""],
            }]

        # Free-text path — relevance-ranked across the user's people graph.
        data = await self._request(
            "GET", "/me/people",
            params={"$search": query, "$top": top},
        )
        out: List[Dict[str, Any]] = []
        for p in (data.get("value") or []):
            emails = [
                e.get("address")
                for e in (p.get("scoredEmailAddresses") or [])
                if e.get("address")
            ]
            out.append({
                "id":           p.get("id"),
                "display_name": p.get("displayName") or "",
                "given_name":   p.get("givenName") or "",
                "surname":      p.get("surname") or "",
                "title":        p.get("jobTitle") or "",
                "department":   p.get("department") or "",
                "office":       p.get("officeLocation") or "",
                "emails":       emails,
            })
        return out

    # ── Public API: files (OneDrive) ────────────────────────────────────
    @staticmethod
    def _shape_file(f: Dict[str, Any]) -> Dict[str, Any]:
        parent = (f.get("parentReference") or {}).get("path") or ""
        return {
            "file_id":     f.get("id"),
            "name":        f.get("name") or "",
            "size":        f.get("size", 0),
            "modified":    f.get("lastModifiedDateTime") or "",
            "web_url":     f.get("webUrl") or "",
            "parent_path": parent,
        }

    async def search_files(self, query: str, top: int = 10) -> List[Dict[str, Any]]:
        if not query:
            raise TeamsClientError("search_files: query is required")
        top = max(1, min(int(top), 25))
        # Note: query is interpolated into the path because Graph's
        # search syntax requires it inside parens.
        q = quote(query.replace("'", "''"), safe="")
        data = await self._request(
            "GET", f"/me/drive/root/search(q='{q}')",
            params={"$top": top},
        )
        return [self._shape_file(f) for f in (data.get("value") or [])]

    async def list_recent_files(self, top: int = 10) -> List[Dict[str, Any]]:
        top = max(1, min(int(top), 25))
        data = await self._request(
            "GET", "/me/drive/recent",
            params={"$top": top},
        )
        return [self._shape_file(f) for f in (data.get("value") or [])]

    # ── Public API: tasks (Microsoft To Do) ─────────────────────────────
    @staticmethod
    def _shape_task(t: Dict[str, Any], list_name: str = "") -> Dict[str, Any]:
        return {
            "task_id":    t.get("id"),
            "title":      t.get("title") or "",
            "status":     t.get("status") or "",
            "importance": t.get("importance") or "normal",
            "due_date":   ((t.get("dueDateTime") or {}).get("dateTime") or ""),
            "list_name":  list_name,
            "created":    t.get("createdDateTime") or "",
        }

    async def list_tasks(self, top: int = 20) -> List[Dict[str, Any]]:
        """Read Microsoft To Do across all task lists.

        Same backing store the Teams "Tasks" tab uses, so this surfaces
        what the user sees in Teams without needing the (admin-scoped)
        Planner API.
        """
        top = max(1, min(int(top), 50))
        lists_data = await self._request(
            "GET", "/me/todo/lists", params={"$top": 10},
        )
        lists = lists_data.get("value") or []
        out: List[Dict[str, Any]] = []
        for tl in lists:
            list_id = tl.get("id")
            if not list_id:
                continue
            try:
                tasks_data = await self._request(
                    "GET", f"/me/todo/lists/{list_id}/tasks",
                    params={"$top": top, "$orderby": "createdDateTime desc"},
                )
            except TeamsClientError:
                continue
            list_name = tl.get("displayName") or ""
            for t in (tasks_data.get("value") or []):
                out.append(self._shape_task(t, list_name=list_name))
            if len(out) >= top:
                break
        return out[:top]

    async def create_task(
        self,
        title: str,
        body: str = "",
        due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a To Do task in the default list. ``due_date`` is
        ISO 8601 (e.g. ``2026-06-10T17:00:00``)."""
        if not title:
            raise TeamsClientError("create_task: title is required")
        lists_data = await self._request(
            "GET", "/me/todo/lists", params={"$top": 5},
        )
        lists = lists_data.get("value") or []
        # Prefer the default ("Tasks") list if marked as such; otherwise
        # take the first list returned.
        default_list = None
        for tl in lists:
            if (tl.get("wellknownListName") or "").lower() == "defaultlist":
                default_list = tl
                break
        if not default_list and lists:
            default_list = lists[0]
        if not default_list:
            raise TeamsClientError("create_task: no task lists found")
        list_id = default_list["id"]
        body_obj: Dict[str, Any] = {"title": title}
        if body:
            body_obj["body"] = {"content": body, "contentType": "text"}
        if due_date:
            body_obj["dueDateTime"] = {"dateTime": due_date, "timeZone": "UTC"}
        data = await self._request(
            "POST", f"/me/todo/lists/{list_id}/tasks", json_body=body_obj,
        )
        return {
            "task_id":   data.get("id"),
            "title":     data.get("title") or "",
            "list_name": default_list.get("displayName") or "",
            "ok":        True,
        }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — TeamsChatClient (Teams internal API: chatsvcagg + presence)
# ─────────────────────────────────────────────────────────────────────────────
#
# Microsoft Graph's chat read scopes (Chat.Read, Chat.ReadWrite,
# ChannelMessage.Read.All) are NOT included in the scope set Teams Web's
# MSAL client gets — every GET /me/chats returns 403. But Teams Web
# itself reads chat history fine, because it uses Microsoft's internal
# Teams services with their own audience tokens (chatsvcagg.teams.microsoft.com,
# presence.teams.microsoft.com) that we already harvest into the cache
# during bootstrap.
#
# Caveats:
#   * The endpoints used here are NOT publicly documented. They are the
#     same URLs Teams Web uses, and have been stable for years, but
#     Microsoft can change them without notice. If a request returns
#     404, the URL pattern likely shifted — re-run
#     scripts/verify_teams_token_storage.py against a current Teams Web
#     session.
#   * Response shapes are normalised to match the Graph-style dicts
#     returned by TeamsClient, so the agent / hint sees one format
#     regardless of which transport is used.

# Endpoint base URLs. Constants at the top so a future MSAL.js shift
# only requires updating these and re-running the bootstrap.
_CHATSVC_BASE = "https://teams.microsoft.com/api/csa/api/v1"
_CHATSVC_RESOURCE = "https://chatsvcagg.teams.microsoft.com"
# Per-conversation message reads go through Teams' message service
# (ic3 audience), region-scoped: /api/chatsvc/{region}/v1/... . The
# chatsvcagg aggregator above serves the chat *roster* but 404s on
# every per-conversation message path.
_IC3_MSG_BASE = "https://teams.microsoft.com/api/chatsvc"  # + /{region}/v1
_IC3_RESOURCE = "https://ic3.teams.office.com"
_PRESENCE_BASE = "https://presence.teams.microsoft.com/v1"
_PRESENCE_RESOURCE = "https://presence.teams.microsoft.com"


class TeamsChatClientError(RuntimeError):
    """Internal-API error. ``status`` mirrors HTTP status (0 on
    transport failure)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


_chat_client_lock = asyncio.Lock()
_chat_client_instance: Optional["TeamsChatClient"] = None


async def get_teams_chat_client() -> "TeamsChatClient":
    """Process-wide TeamsChatClient singleton (async-safe)."""
    global _chat_client_instance
    if _chat_client_instance is not None:
        return _chat_client_instance
    async with _chat_client_lock:
        if _chat_client_instance is None:
            _chat_client_instance = TeamsChatClient()
    return _chat_client_instance


async def close_teams_chat_client() -> None:
    """Close the underlying httpx session at session boundary."""
    global _chat_client_instance
    if _chat_client_instance is None:
        return
    try:
        await _chat_client_instance.aclose()
    finally:
        _chat_client_instance = None


class TeamsChatClient:
    """httpx.AsyncClient + Teams internal endpoints."""

    def __init__(self, auth: Optional[TeamsAuth] = None) -> None:
        self.logger = get_logger()
        self._auth = auth or get_teams_auth()
        self._http = None  # lazy httpx.AsyncClient

    async def _ensure_http(self):
        if self._http is not None:
            return self._http
        # verify=False mirrors the corporate-proxy fix already applied
        # to the rest of teams_*; SSL inspection on Qualcomm's network.
        self._http = httpx.AsyncClient(timeout=30.0, verify=False)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    # ── Internal request helper ─────────────────────────────────────────
    async def _request(
        self,
        method: str,
        url: str,
        *,
        resource: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Issue a request to a Teams internal endpoint with the right
        per-resource Bearer token. ``resource`` selects which audience
        token from the cache to attach.
        """
        token = await self._auth.get_token_for_resource(resource)
        if not token:
            raise TeamsChatClientError(
                f"no cached access token for resource {resource}. "
                "Re-bootstrap via teams.microsoft.com so MSAL refreshes "
                "this audience.",
                status=401,
            )
        http = await self._ensure_http()
        headers = {
            "Authorization":   f"Bearer {token}",
            "Accept":          "application/json",
            # Teams web client accepts a custom client info header in
            # some endpoints — harmless to send always, helps the
            # backend pick the right route variant.
            "x-ms-client-version": "1415/25.04.04.001",
            "x-ms-client-type":    "web",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = await http.request(
                method, url, params=params, json=json_body, headers=headers,
            )
        except Exception as exc:
            raise TeamsChatClientError(
                f"transport error: {exc}", status=0,
            ) from exc

        if resp.status_code == 401:
            raise TeamsChatClientError(
                f"Teams internal API rejected the token (401) for {url}. "
                "Token may have expired since bootstrap; the next call "
                "will re-bootstrap automatically.",
                status=401,
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After") or 1.0)
            raise TeamsChatClientError(
                f"Teams internal API throttled (429); retry after "
                f"{retry_after:.0f}s.",
                status=429,
            )
        if resp.status_code >= 400:
            raise TeamsChatClientError(
                f"{method} {url} → {resp.status_code}: "
                f"{resp.text[:200]}",
                status=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise TeamsChatClientError(
                f"non-JSON response: {exc}", status=resp.status_code,
            ) from exc

    # ── Shape helpers — match TeamsClient's Graph-style output ──────────
    @staticmethod
    def _shape_chat(c: Dict[str, Any]) -> Dict[str, Any]:
        members = c.get("members") or []
        member_names = []
        for m in members:
            if isinstance(m, dict):
                name = (
                    m.get("displayName")
                    or m.get("upn")
                    or m.get("email")
                    or m.get("mri")
                    or "(unknown)"
                )
                member_names.append(name)
        # Internal API uses different field names than Graph; map them.
        return {
            "chat_id":       c.get("id") or c.get("threadId") or "",
            "topic":         c.get("title") or c.get("topic") or "",
            "chat_type":     c.get("type") or c.get("threadType") or "",
            "last_updated":  c.get("lastMessageTime") or c.get("lastUpdatedTime") or "",
            "members":       member_names,
            "is_group":      bool(c.get("isGroupChat", False)),
        }

    @staticmethod
    def _shape_message(m: Dict[str, Any]) -> Dict[str, Any]:
        # Internal-API message has a flatter structure than Graph's.
        from_user = (
            m.get("imdisplayname")
            or m.get("displayName")
            or ((m.get("from") or {}).get("displayName") if isinstance(m.get("from"), dict) else "")
            or ""
        )
        body = m.get("content") or ""
        if not body and isinstance(m.get("body"), dict):
            body = (m.get("body") or {}).get("content") or ""
        return {
            "message_id":   m.get("id") or m.get("messageId") or "",
            "from":         from_user,
            "created_at":   m.get("composetime") or m.get("originalArrivalTime") or m.get("createdDateTime") or "",
            "body_html":    body,
            "content_type": m.get("contenttype") or "html",
            "importance":   m.get("importance") or "normal",
            "subject":      m.get("subject") or "",
            "deleted":      bool(m.get("deletetime") or m.get("deletedDateTime")),
        }

    @staticmethod
    def _coerce_list(data: Any, *keys: str) -> List[Dict[str, Any]]:
        """chatsvcagg endpoints sometimes return a top-level array,
        sometimes wrap it as ``{"chats": [...]}`` or ``{"value": [...]}``,
        and the exact shape varies by region / version. Centralise
        unwrapping so each public method stays simple.
        """
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, list):
                    return v
        return []

    # ── Public API: chats ───────────────────────────────────────────────
    async def list_chats(self, top: int = 50) -> List[Dict[str, Any]]:
        """List 1:1 + group chats via chatsvcagg. Returns conversations
        sorted by most-recent-activity first."""
        top = max(1, min(int(top), 50))
        data = await self._request(
            "GET",
            f"{_CHATSVC_BASE}/teams/users/me/chats",
            resource=_CHATSVC_RESOURCE,
            params={"supportsBookmarks": "true", "pageSize": top},
        )
        chats = self._coerce_list(data, "chats", "value")
        return [self._shape_chat(c) for c in chats][:top]

    async def list_chat_messages(
        self, chat_id: str, top: int = 50,
    ) -> List[Dict[str, Any]]:
        """Read recent messages from a chat.

        Hits Teams' message service (ic3 audience) at
        ``/api/chatsvc/{region}/v1/users/ME/conversations/{id}/messages``
        — the same call Teams Web makes. The chatsvcagg aggregator that
        serves ``list_chats`` does NOT serve per-conversation message
        bodies (every path shape 404s), so this path is region-scoped
        and authed with the ic3.teams.office.com token; both the region
        and the token are harvested at bootstrap.

        ``chat_id`` is the Graph-style id (``19:...@thread.v2`` or
        ``19:...@unq.gbl.spaces``).
        """
        if not chat_id:
            raise TeamsChatClientError("list_chat_messages: chat_id required")
        region = self._auth.get_region()
        if not region:
            raise TeamsChatClientError(
                "no Teams messaging region cached — re-bootstrap via "
                "teams.microsoft.com so the region is harvested.",
                status=401,
            )
        top = max(1, min(int(top), 50))
        encoded = quote(chat_id, safe="")
        data = await self._request(
            "GET",
            f"{_IC3_MSG_BASE}/{region}/v1/users/ME/conversations/{encoded}/messages",
            resource=_IC3_RESOURCE,
            params={
                "view": "msnp24Equivalent|supportsMessageProperties",
                "pageSize": top,
                "startTime": 1,
            },
        )
        msgs = self._coerce_list(data, "messages", "value")
        return [self._shape_message(m) for m in msgs][:top]

    async def list_channel_messages(
        self, team_id: str, channel_id: str, top: int = 50,
    ) -> List[Dict[str, Any]]:
        """Read recent messages from a channel via chatsvcagg.

        Channel conversations have a different identity model than 1:1
        chats: Graph's team_id (a GUID) does NOT directly map to the
        messaging-thread id chatsvcagg expects. Different deployments
        accept different URL shapes; we try a small candidate set and
        fall back through 400 / 404 to the next one. If nothing works,
        the caller should route through ``browser_tool`` to the channel
        URL.
        """
        if not team_id or not channel_id:
            raise TeamsChatClientError(
                "list_channel_messages: team_id and channel_id required"
            )
        top = max(1, min(int(top), 50))
        encoded_team = quote(team_id, safe="")
        encoded_ch = quote(channel_id, safe="")
        last_err: Optional[Exception] = None
        # URL candidates ordered by likelihood. Real-world behaviour:
        #   teams/{guid}/channels/{thread}/messages → 400 'Invalid team id'
        #     (chatsvcagg wants a thread-style team id, not Graph GUID)
        #   users/ME/conversations/{thread}/messages → may work for some
        #     tenants where channels participate as threaded conversations
        candidates = [
            f"{_CHATSVC_BASE}/users/ME/conversations/{encoded_ch}/messages",
            f"{_CHATSVC_BASE}/teams/{encoded_team}/channels/{encoded_ch}/messages",
        ]
        for url in candidates:
            try:
                data = await self._request(
                    "GET", url,
                    resource=_CHATSVC_RESOURCE,
                    params={"pageSize": top},
                )
            except TeamsChatClientError as exc:
                # Treat both 400 (wrong shape / "Invalid team id") and
                # 404 (path not found) as "try next candidate". Other
                # statuses (401, 403, 5xx) propagate immediately.
                if exc.status in (400, 404):
                    last_err = exc
                    continue
                raise
            msgs = self._coerce_list(data, "messages", "value")
            return [self._shape_message(m) for m in msgs][:top]
        # Both candidates exhausted. Surface a clear error pointing to
        # the documented fallback so the agent doesn't keep retrying.
        raise TeamsChatClientError(
            "Channel messages are not reachable via the chatsvcagg "
            "internal API in this tenant configuration. Microsoft Graph's "
            "channel-read scope (ChannelMessage.Read.All) is also not in "
            "Teams Web's token set. Workaround: use browser_tool to "
            "navigate to teams.microsoft.com/_#/conversations and open "
            "the channel directly. "
            f"(last internal-api error: {last_err})",
            status=404,
        )

    # ── Public API: presence ────────────────────────────────────────────
    @staticmethod
    def _shape_presence(p: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "user_id":       p.get("mri") or p.get("id") or "",
            "availability":  p.get("availability") or "",
            "activity":      p.get("activity") or "",
            "device_type":   p.get("deviceType") or "",
            "out_of_office": (p.get("outOfOfficeNote") or {}).get("message") if isinstance(p.get("outOfOfficeNote"), dict) else "",
        }

    async def _request_presence(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Special _request variant for presence URLs. Tries the
        presence audience token first, then falls back to the
        chatsvcagg audience token — some Teams Web sessions never
        mint a separate presence audience and end up using their
        general Teams token for presence calls too.
        """
        last_err: Optional[Exception] = None
        for resource in (_PRESENCE_RESOURCE, _CHATSVC_RESOURCE):
            try:
                return await self._request(
                    method, url,
                    resource=resource,
                    json_body=json_body,
                )
            except TeamsChatClientError as exc:
                # Token missing for this resource → try the next one.
                if exc.status == 401 and "no cached access token" in str(exc).lower():
                    last_err = exc
                    continue
                raise
        # All audiences exhausted.
        raise TeamsChatClientError(
            "presence read failed: no usable token for "
            "presence.teams.microsoft.com or chatsvcagg.teams.microsoft.com. "
            "Workaround: use browser_tool to navigate to teams.microsoft.com "
            "and read the status indicator on your avatar. "
            f"(last error: {last_err})",
            status=401,
        )

    async def get_my_presence(self) -> Dict[str, Any]:
        """Read my own current presence (Available / Busy / DND / …).

        Returns ``{}`` (and logs a warning) when neither presence nor
        chatsvcagg audience tokens are available — caller should
        route to browser_tool fallback instead of erroring loudly.
        """
        data = await self._request_presence(
            "GET", f"{_PRESENCE_BASE}/me/presence",
        )
        # Some endpoints return an array under "presence", others a flat object.
        if isinstance(data, list) and data:
            return self._shape_presence(data[0])
        if isinstance(data, dict) and isinstance(data.get("presence"), list) and data["presence"]:
            return self._shape_presence(data["presence"][0])
        return self._shape_presence(data if isinstance(data, dict) else {})

    async def get_user_presence(self, user_mri: str) -> Dict[str, Any]:
        """Read another user's presence by Teams MRI ('8:orgid:...').

        For simple display-name → MRI resolution, call
        ``TeamsClient.find_person`` first to get the user's id, then
        format as ``f"8:orgid:{id}"``.
        """
        if not user_mri:
            raise TeamsChatClientError("get_user_presence: user_mri required")
        data = await self._request_presence(
            "POST",
            f"{_PRESENCE_BASE}/presence/getpresence/",
            json_body={"mris": [user_mri]},
        )
        items = data if isinstance(data, list) else (data.get("value") or [] if isinstance(data, dict) else [])
        return self._shape_presence(items[0]) if items else {}
