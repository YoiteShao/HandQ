# -*- coding: utf-8 -*-
"""Web search across Qualcomm internal sources via the authenticated browser.

Auth model
----------
The persistent Edge profile owned by ``browser_tool`` already holds the user's
SSO cookies (Atlassian, Microsoft Identity, intranet portal). Rather than
extracting them through DPAPI / SQLite, this tool reuses the live Playwright
session: ``browser_tool.evaluate_fetch`` runs ``fetch()`` from inside a page
on the target's origin so cookies are sent automatically. The user logs in
once per source via ``browser.request_user_login``; cookies survive across
HandQ sessions in the persistent profile.

Sources
-------
* ``confluence``  — Confluence Cloud (`qualcomm-confluence.atlassian.net`).
                    REST + CQL.
* ``jira``        — Jira Data Center (`jira-dc.qualcomm.com`).
                    REST + JQL.
* ``sharepoint``  — SharePoint Online (`qualcomm.sharepoint.com`).
                    Search REST (KQL) with ODATA flattening.
* ``orbit``       — intranet portal. No JSON API — DOM-extract fallback;
                    the result selector lives in ``handq_config.yaml``
                    (``web_search.sources.orbit.result_selector``).

Login recovery
--------------
A 401 / 403 / SSO-redirect raises a structured error telling the agent which
source needs ``browser.request_user_login`` and what ``success_url_pattern``
to use. The agent recovers reactively — no preemptive login dance at
provider activation.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .base_tool import BaseTool, ToolResult
from .browser_tool import evaluate_fetch, is_browser_available, _module_holder
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.logger import get_logger


# ── Defaults (overridable via handq_config.yaml web_search section) ──────────
_DEFAULT_LIMIT = 10
_HARD_MAX_LIMIT = 25
_DEFAULT_SNIPPET_MAX_CHARS = 300
_DEFAULT_ORBIT_TIMEOUT_MS = 30_000

# Per-process cache for repeat (source, query, limit) lookups. Bounded LRU
# with a 60s TTL — covers the common case where the agent re-runs the same
# query during retry / re-plan within a single task. Skipped for orbit
# because its DOM-extract path may legitimately return different results
# as the portal mutates.
#
# TODO(perf): cross-source parallel fan-out is gated on the browser session
# lock (``holder.acquire``) being held for each fetch. True parallelism needs a
# refactor of ``evaluate_fetch`` (per-source dedicated tabs so concurrent
# fetches don't collide on same_origin navigation).
_QUERY_CACHE_TTL = 60.0
_QUERY_CACHE_MAX = 50
# Cache key includes offset/cursor so paged results never collide with or
# shadow page-1 results. Cached value is the full (hits, has_more,
# next_cursor) tuple so a cache hit is transparent to pagination metadata.
_CacheKey = Tuple[str, str, int, int, str]
_CacheValue = Tuple[List[Dict[str, Any]], bool, Optional[str]]
_query_cache: "OrderedDict[_CacheKey, Tuple[float, _CacheValue]]" = OrderedDict()


def _cache_get(key: _CacheKey) -> Optional[_CacheValue]:
    """Return cached (hits, has_more, next_cursor) if present and unexpired;
    refresh LRU position."""
    entry = _query_cache.get(key)
    if entry is None:
        return None
    timestamp, value = entry
    if (time.time() - timestamp) > _QUERY_CACHE_TTL:
        _query_cache.pop(key, None)
        return None
    _query_cache.move_to_end(key)
    return value


def _cache_put(key: _CacheKey, value: _CacheValue) -> None:
    """Insert a (hits, has_more, next_cursor) value, evicting the oldest
    entry on overflow."""
    _query_cache[key] = (time.time(), value)
    _query_cache.move_to_end(key)
    while len(_query_cache) > _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)

# Markers that indicate a body is actually an SSO login page rather than the
# requested JSON. The list is intentionally conservative — only well-known
# strings — so a substring false-positive is unlikely on real API payloads.
_SSO_BODY_MARKERS = (
    "<title>sign in to your account</title>",
    "login.microsoftonline.com",
    "id.atlassian.com/login",
    "okta-signin",
    "samlrequest",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class SearchHit:
    """Normalised search result. All four sources collapse to this shape."""
    title: str
    url: str
    snippet: str
    source: str
    last_modified: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _is_auth_failure(status: int, body: str) -> bool:
    """True if the response looks like an SSO login wall, not real data.

    Catches three patterns:
      - explicit auth statuses (401/403)
      - redirect statuses with no auto-follow (302/303/307/308)
      - SSO HTML body markers
      - empty body with non-error status (status<400 + body="" — covers
        Playwright's evaluate_fetch returning status=0 on JS-side abort,
        and proxies that swallow the SSO redirect into a blank 200)
    """
    if status in (401, 403):
        return True
    if status in (302, 303, 307, 308):
        return True
    bl = (body or "").lower()
    if any(marker in bl for marker in _SSO_BODY_MARKERS):
        return True
    if not (body or "").strip() and status < 400:
        return True
    return False


def _strip_html(s: str) -> str:
    """Cheap tag stripper for excerpt fields (Confluence highlights, SP <c0>).

    Also decodes HTML entities left behind after tag removal (e.g.
    "Audio &amp; Streaming" -> "Audio & Streaming") — found via real
    confluence/sharepoint data during 2026-07-17 E2E verification.
    """
    return html.unescape(_HTML_TAG_RE.sub("", s or "")).strip()


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_snippet(s: str) -> str:
    """Collapse runs of whitespace (including NBSP, \\r\\n, multi-newline
    table dumps) into single spaces. Found necessary via real data
    2026-07-17: jira titles carried literal trailing NBSP bytes,
    sharepoint/confluence snippets carried raw \\r\\n / multi-newline table
    dumps — all pure noise for an LLM reader, none of it substantive content.
    """
    return _WHITESPACE_RE.sub(" ", s or "").strip()


def _extract_sp_cells(row: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a SharePoint Search ``Row.Cells`` array into a plain dict.

    Each cell is ``{"Key": "<column>", "Value": "<scalar>", "ValueType": ...}``.
    The shape is deeply nested by ODATA convention; we only need a key→value
    lookup so callers can read e.g. ``cells["Title"]`` directly.
    """
    out: Dict[str, Any] = {}
    for cell in (row or {}).get("Cells") or []:
        key = cell.get("Key")
        if key:
            out[str(key)] = cell.get("Value")
    return out


class _AuthRequired(RuntimeError):
    """Raised by per-source executors when the response is an SSO wall."""

    def __init__(self, source: str, base_url: str, status: int):
        self.source = source
        self.base_url = base_url
        self.status = status
        super().__init__(
            f"{source} requires login (status={status}). "
            f"Recovery: 1) browser.navigate url='{base_url}'  "
            f"2) browser.request_user_login reason='auth {source}' "
            f"success_url_pattern='{re.escape(base_url)}'  "
            f"3) retry web_search.search."
        )


# ── Fast path: bare httpx using the live browser's real cookies ────────────
#
# evaluate_fetch's page.goto(origin) round trip exists purely to hold
# cookies for the fetch() that follows — the actual API call is a single
# GET. Verified live 2026-07-17 against real jira-dc.qualcomm.com,
# qualcomm-confluence.atlassian.net, and qualcomm.sharepoint.com: a bare
# httpx GET carrying the live session's real cookies gets an identical 200
# + real JSON body, cutting per-call latency from ~2-6s (evaluate_fetch,
# dominated by the goto) down to ~0.5-0.9s for jira/confluence. sharepoint's
# win is smaller since its own backend reports 4+ seconds of server-side
# search time regardless of fetch mechanism — the bottleneck there is
# SharePoint's search latency, not ours.
#
# An earlier pass concluded sharepoint needed an unidentified CSRF/
# X-RequestDigest token after seeing a 403 — that was wrong, caused by
# testing with the stale on-disk shared/storage_state.json instead of the
# live in-memory cookie jar. Cookies read via session.context.cookies()
# (what this helper actually does) work fine; no token is needed.
#
# orbit has no JSON API at all (DOM-extract only) — it does not use this path.
#
# Lock scoping (2026-07-17): the httpx attempt touches NO DOM — it reads
# holder.session (a plain attribute, not a critical section) and
# session.context.cookies() (a Playwright API call, not a DOM op), then
# fires an independent httpx.AsyncClient request. None of that needs
# BrowserSessionHolder's lock, whose real job is serialising DOM operations
# on the shared BrowserSession. So the httpx attempt runs lock-free —
# concurrent jira/confluence/sharepoint calls genuinely run in parallel
# instead of queueing. The lock is only acquired if/when this falls through
# to evaluate_fetch (real page navigation), which DOES touch the DOM.
_HTTPX_TIMEOUT_S = 30.0
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


async def _fetch_direct_or_browser(
    holder: Any, *, url: str, headers: Dict[str, str], cookie_url: str,
) -> Dict[str, Any]:
    """Try a bare httpx GET using the live browser's real cookies, without
    holding the browser session lock (see module comment above). Falls back
    to evaluate_fetch (real page navigation + in-page fetch(), under the
    lock) on any failure. Returns the same ``{status, body}``-shaped dict
    either path produces, so callers need no changes downstream of this call.
    """
    session = holder.session
    if session is None:
        raise RuntimeError(
            "browser session is not launched. Call "
            "browser.launch_browser before reusing the browser session."
        )
    try:
        cookies = await session.context.cookies(urls=[cookie_url])
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        async with httpx.AsyncClient(verify=False, timeout=_HTTPX_TIMEOUT_S) as client:
            r = await client.get(
                url, cookies=cookie_dict,
                headers={**headers, "User-Agent": _BROWSER_UA},
                follow_redirects=True,
            )
        return {"status": r.status_code, "body": r.text}
    except Exception:
        pass  # fall through to the locked browser path below

    async with holder.acquire() as locked_session:
        return await evaluate_fetch(
            locked_session, url=url, method="GET", headers=headers, same_origin=True,
        )


class WebSearchTool(BaseTool):
    """Cross-source internal search. See module docstring for auth model."""

    is_read_only = True
    is_concurrency_safe = False  # serialised on browser session lock

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text search query. For Confluence, supports CQL "
                    "keywords (e.g. 'space=ENG AND text ~ \"power "
                    "management\"'); for Jira, JQL ('project = ANDR AND "
                    "summary ~ \"power\"'); plain text works against all "
                    "sources."
                ),
            },
            "source": {
                "type": "string",
                "enum": ["confluence", "jira", "sharepoint", "orbit"],
                "description": (
                    "Which source to query. Choose ONE per call; agent can "
                    "fan out by issuing parallel calls — jira/confluence/"
                    "sharepoint genuinely run concurrently (each independently "
                    "reads live cookies + fires its own request); only orbit "
                    "serialises on the browser session lock (real page "
                    "navigation + DOM read). Pick the most likely source "
                    "first; recommended order when you do not know which: "
                    "jira → sharepoint → orbit → confluence (confluence is "
                    "the flakiest under SSO and should be tried last). "
                    "Sources can be individually disabled in "
                    "handq_config.yaml under web_search.sources.<name>.enabled."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Max results to return. Default {_DEFAULT_LIMIT}. "
                    f"Hard cap {_HARD_MAX_LIMIT}; values above are clamped."
                ),
            },
            "offset": {
                "type": "integer",
                "description": (
                    "0-indexed starting position for pagination (default 0). "
                    "jira/sharepoint/orbit: any value works (orbit rounds "
                    "down to the nearest page boundary — page by multiples "
                    "of limit for predictable results). confluence: this "
                    "source paginates via an opaque cursor, not a numeric "
                    "offset — pass offset=0 for the first page, then read "
                    "'next_cursor' from the result and pass it back as the "
                    "'cursor' parameter (not offset) to get the next page. "
                    "Passing a nonzero offset to confluence without a prior "
                    "cursor fails clearly rather than silently returning "
                    "page 1 again."
                ),
            },
            "cursor": {
                "type": "string",
                "description": (
                    "confluence-only: the opaque 'next_cursor' value from a "
                    "previous confluence search result, used to fetch the "
                    "next page. Ignored by other sources (they use 'offset' "
                    "instead)."
                ),
            },
        },
        "required": ["query", "source"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None) -> None:
        super().__init__("web_search", ctx=ctx)
        self.logger = get_logger()
        # Share the BrowserTool's per-session live browser: same SessionContext
        # → same holder, so same-origin fetches reuse the cookies / SSO from the
        # session the agent already launched. Falls back to the module holder
        # when constructed without a ctx (legacy / test path).
        self.holder = (
            ctx.browser_session
            if ctx is not None and getattr(ctx, "browser_session", None) is not None
            else _module_holder()
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        start = time.time()
        params: Dict[str, Any] = dict(kwargs)

        if not is_browser_available():
            return self._fail(
                params, start,
                "web_search needs playwright. Run:\n"
                "  pip install playwright\n"
                "  playwright install msedge",
            )

        query = (kwargs.get("query") or "").strip()
        source = (kwargs.get("source") or "").strip().lower()
        if not query:
            return self._fail(params, start, "web_search requires 'query'.")
        if not source:
            return self._fail(params, start, "web_search requires 'source'.")

        try:
            cfg = ConfigManager().get_section("web_search") or {}
        except Exception:
            cfg = {}

        sources_cfg = cfg.get("sources") or {}
        src_cfg = sources_cfg.get(source) or {}
        if not src_cfg.get("enabled", True):
            return self._fail(
                params, start,
                f"source {source!r} is disabled (web_search.sources.{source}.enabled=false).",
            )

        default_limit = int(cfg.get("default_limit") or _DEFAULT_LIMIT)
        max_limit = int(cfg.get("max_limit") or _HARD_MAX_LIMIT)
        snippet_max = int(cfg.get("snippet_max_chars") or _DEFAULT_SNIPPET_MAX_CHARS)

        raw_limit = kwargs.get("limit")
        limit = int(raw_limit) if raw_limit is not None else default_limit
        limit = max(1, min(limit, max_limit))

        raw_offset = kwargs.get("offset")
        offset = max(0, int(raw_offset)) if raw_offset is not None else 0
        cursor = kwargs.get("cursor") or None

        executors = {
            "confluence": self._search_confluence,
            "jira":       self._search_jira,
            "sharepoint": self._search_sharepoint,
            "orbit":      self._search_orbit,
        }
        executor = executors.get(source)
        if executor is None:
            return self._fail(params, start, f"unknown source {source!r}.")

        # Per-process cache lookup — skips the browser round-trip when the
        # same (source, query, limit, offset, cursor) was answered recently.
        # Orbit isn't cached because its DOM-extract may legitimately change.
        cache_key: Optional[_CacheKey] = (
            None if source == "orbit" else (source, query, limit, offset, cursor or "")
        )
        if cache_key is not None:
            cached = _cache_get(cache_key)
            if cached is not None:
                cached_hits, cached_has_more, cached_next_cursor = cached
                return ToolResult(
                    success=True,
                    output={
                        "source": source,
                        "query": query,
                        "count": len(cached_hits),
                        "hits": cached_hits,
                        "has_more": cached_has_more,
                        "next_cursor": cached_next_cursor,
                        "cached": True,
                    },
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=time.time() - start,
                )

        try:
            hits, has_more, next_cursor = await executor(query, limit, offset, cursor, src_cfg)
        except _AuthRequired as exc:
            return self._fail(params, start, str(exc))
        except RuntimeError as exc:
            # holder.acquire raises this when no session is launched;
            # _search_confluence also raises this for a bad offset/cursor
            # combination (see its docstring).
            return self._fail(
                params, start,
                f"{source}: {exc}",
            )
        except NotImplementedError as exc:
            return self._fail(params, start, str(exc) or f"{source} not yet wired.")
        except Exception as exc:
            self.logger.error(
                f"web_search source={source} failed: {exc}",
                component="WebSearchTool", exc_info=True,
            )
            return self._fail(params, start, f"{source}: {exc}")

        # Whitespace normalization + snippet truncation — applied last so
        # per-source parsers can see full excerpts during normalisation.
        for h in hits:
            h.title = _normalize_snippet(h.title)
            if h.snippet:
                h.snippet = _normalize_snippet(h.snippet)
                if len(h.snippet) > snippet_max:
                    h.snippet = h.snippet[:snippet_max] + "…"

        hit_dicts = [h.to_dict() for h in hits]
        if cache_key is not None:
            _cache_put(cache_key, (hit_dicts, has_more, next_cursor))

        return ToolResult(
            success=True,
            output={
                "source": source,
                "query": query,
                "count": len(hits),
                "hits": hit_dicts,
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── Per-source executors ─────────────────────────────────────────────────

    async def _search_confluence(
        self, query: str, limit: int, offset: int, cursor: Optional[str],
        src_cfg: Dict[str, Any],
    ) -> Tuple[List[SearchHit], bool, Optional[str]]:
        """Confluence paginates via an opaque cursor, NOT a numeric offset —
        confirmed live 2026-07-17: this Atlassian Cloud instance's CQL
        search endpoint silently ignores a plain `start` param (start=0 and
        start=3 returned byte-identical `results`). The real mechanism is
        the response's `_links.next` field, which carries an opaque `cursor`
        token that must be forwarded verbatim to advance. There is no way
        to jump to an arbitrary offset, so a nonzero `offset` without a
        `cursor` is rejected with a clear error rather than silently
        re-returning page 1 (the same silent-noise failure mode this
        session's other fixes have been closing, not a pattern to repeat
        at the tool layer).
        """
        base_url = (src_cfg.get("base_url") or "https://qualcomm-confluence.atlassian.net").rstrip("/")
        api_path = src_cfg.get("api_path") or "/wiki/rest/api/search"
        # CQL: free-text against all content. Caller can pass a full CQL
        # expression in `query` and it will be forwarded verbatim — Atlassian
        # accepts both 'foo bar' and 'text ~ "foo bar"'.
        if "~" in query or "=" in query or " AND " in query.upper():
            cql = query
        else:
            escaped = query.replace('"', '\\"')
            cql = f'text ~ "{escaped}"'

        if cursor:
            # Mirrors the exact shape observed in a real _links.next value:
            # /rest/api/search?next=true&cursor=<opaque>&limit=N&cql=...
            qs = urllib.parse.urlencode({
                "next": "true", "cursor": cursor, "limit": limit, "cql": cql,
            })
        else:
            if offset > 0:
                raise RuntimeError(
                    "confluence does not support an arbitrary numeric "
                    "offset — pass cursor='<next_cursor from a previous "
                    "confluence result>' instead to get the next page. "
                    "offset is only valid as 0 (first page) for this source."
                )
            qs = urllib.parse.urlencode({"cql": cql, "limit": limit})
        url = f"{base_url}{api_path}?{qs}"

        result = await _fetch_direct_or_browser(
            self.holder, url=url, headers={"Accept": "application/json"},
            cookie_url=base_url,
        )
        status = int(result.get("status") or 0)
        body = result.get("body") or ""
        if _is_auth_failure(status, body):
            raise _AuthRequired("confluence", base_url, status)
        if status >= 400:
            raise RuntimeError(f"confluence HTTP {status}: {body[:300]}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"confluence returned non-JSON ({len(body)} bytes, status={status}): "
                f"{(body[:200] or '<empty body>')!r}. "
                "Likely SSO refresh needed or transient outage — try "
                "web_search source=jira instead, or browser.navigate url="
                f"'{base_url}' to re-auth."
            )

        hits: List[SearchHit] = []
        for entry in (data.get("results") or [])[:limit]:
            content = entry.get("content") or {}
            links = entry.get("_links") or content.get("_links") or {}
            webui = links.get("webui") or ""
            if webui.startswith("/"):
                full_url = base_url + webui
            elif webui:
                full_url = webui
            else:
                full_url = base_url
            title = entry.get("title") or content.get("title") or "(untitled)"
            excerpt = _strip_html(entry.get("excerpt") or "")
            last_modified = (
                entry.get("lastModified")
                or (content.get("history") or {}).get("lastUpdated", {}).get("when")
            )
            hits.append(SearchHit(
                title=_strip_html(title),
                url=full_url,
                snippet=excerpt,
                source="confluence",
                last_modified=last_modified,
            ))

        # Extract the opaque cursor token from _links.next (a relative URL
        # like "/rest/api/search?next=true&cursor=<opaque>&limit=N&cql=...")
        # so callers get back a plain token, not the full internal URL shape.
        next_link = (data.get("_links") or {}).get("next")
        next_cursor: Optional[str] = None
        if next_link:
            parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(next_link).query)
            cursor_values = parsed_qs.get("cursor")
            if cursor_values:
                next_cursor = cursor_values[0]
        has_more = next_cursor is not None
        return hits, has_more, next_cursor

    async def _search_jira(
        self, query: str, limit: int, offset: int, cursor: Optional[str],
        src_cfg: Dict[str, Any],
    ) -> Tuple[List[SearchHit], bool, Optional[str]]:
        base_url = (src_cfg.get("base_url") or "https://jira-dc.qualcomm.com").rstrip("/")
        api_path = src_cfg.get("api_path") or "/rest/api/2/search"

        # JQL: free-text against summary + description. Caller can pass a
        # full JQL expression in `query` and it's forwarded verbatim — the
        # presence of '~', '=', or ' AND ' is the heuristic.
        if "~" in query or "=" in query or " AND " in query.upper():
            jql = query
        else:
            escaped = query.replace('"', '\\"')
            jql = f'text ~ "{escaped}"'
        qs = urllib.parse.urlencode({
            "jql": jql,
            "fields": "summary,description,updated,status,priority,assignee",
            "maxResults": limit,
            # Real offset pagination — confirmed live 2026-07-17: startAt=3
            # returns the next distinct 3 issues, no overlap with startAt=0.
            "startAt": offset,
        })
        url = f"{base_url}{api_path}?{qs}"

        result = await _fetch_direct_or_browser(
            self.holder, url=url, headers={"Accept": "application/json"},
            cookie_url=base_url,
        )
        status = int(result.get("status") or 0)
        body = result.get("body") or ""
        if _is_auth_failure(status, body):
            raise _AuthRequired("jira", base_url, status)
        if status >= 400:
            raise RuntimeError(f"jira HTTP {status}: {body[:300]}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"jira returned non-JSON ({len(body)} bytes): "
                f"{(body[:200] or '<empty body>')!r}")

        hits: List[SearchHit] = []
        for issue in (data.get("issues") or [])[:limit]:
            key = issue.get("key") or ""
            fields = issue.get("fields") or {}
            summary = fields.get("summary") or "(no summary)"
            desc_raw = fields.get("description") or ""
            # description may be a string (Jira DC) or ADF dict (Cloud).
            # We only support DC's string form here; Cloud ADF would need
            # adf_to_text — out of scope until SharePoint/Confluence are
            # both stable and someone hits the Cloud Jira variant.
            if not isinstance(desc_raw, str):
                desc_raw = ""
            # Strip Jira wiki-markup macros: {color:red}foo{color}, {code}, etc.
            desc_clean = re.sub(r"\{[^}]+\}", "", desc_raw)
            # [text|scheme://url] -> text ; bare [scheme://url] -> dropped
            # entirely. Jira wiki-link syntax otherwise leaves a raw URL
            # burning a large fraction of the snippet budget on a bare
            # link instead of substantive text — found via real data
            # 2026-07-17 (AUTORFI-18466's http(s) case, BLIP-169031/
            # APTAUTOSH-37537's file:// UNC-path case — any URI scheme,
            # not just http(s), so the pattern matches a generic
            # "letters followed by ://" scheme rather than hardcoding http).
            desc_clean = re.sub(r"\[([^|\]]*)\|[a-zA-Z][a-zA-Z0-9+.-]*://[^\]]+\]", r"\1", desc_clean)
            desc_clean = re.sub(r"\[[a-zA-Z][a-zA-Z0-9+.-]*://[^\]]+\]", "", desc_clean).strip()
            updated = fields.get("updated") or ""
            full_url = f"{base_url}/browse/{key}" if key else base_url
            title = f"{key}: {summary}" if key else summary
            hits.append(SearchHit(
                title=title,
                url=full_url,
                snippet=desc_clean,
                source="jira",
                last_modified=updated,
            ))
        total = int(data.get("total") or 0)
        has_more = (offset + len(hits)) < total
        return hits, has_more, None

    async def _search_sharepoint(
        self, query: str, limit: int, offset: int, cursor: Optional[str],
        src_cfg: Dict[str, Any],
    ) -> Tuple[List[SearchHit], bool, Optional[str]]:
        base_url = (src_cfg.get("base_url") or "https://qualcomm.sharepoint.com").rstrip("/")
        api_path = src_cfg.get("api_path") or "/_api/search/query"

        # SharePoint SEARCH expects single-quoted querytext; selectproperties
        # narrows the deeply-nested ODATA payload to the four columns we
        # care about.
        escaped_q = query.replace("'", "''")
        select_props = "Title,Path,HitHighlightedSummary,LastModifiedTime"
        qs = urllib.parse.urlencode({
            "querytext": f"'{escaped_q}'",
            "rowlimit": limit,
            "selectproperties": f"'{select_props}'",
            # Real offset pagination — confirmed live 2026-07-17: startrow=3
            # returns distinct titles with no overlap against startrow=0.
            "startrow": offset,
        })
        url = f"{base_url}{api_path}?{qs}"

        # Only the Accept header — SP Online uses OData 3.0; explicitly
        # setting OData-Version to 4.0 (an earlier draft of this code did
        # so) made the search service return HTTP 500 "Unknown Error".
        # ``odata=nometadata`` flattens the response so callers don't wade
        # through __metadata wrappers.
        result = await _fetch_direct_or_browser(
            self.holder, url=url,
            headers={"Accept": "application/json;odata=nometadata"},
            cookie_url=base_url,
        )
        status = int(result.get("status") or 0)
        body = result.get("body") or ""
        if _is_auth_failure(status, body):
            raise _AuthRequired("sharepoint", base_url, status)
        if status >= 400:
            raise RuntimeError(f"sharepoint HTTP {status}: {body[:300]}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"sharepoint returned non-JSON ({len(body)} bytes): "
                f"{(body[:200] or '<empty body>')!r}")

        relevant = ((data.get("PrimaryQueryResult") or {}).get("RelevantResults") or {})
        rows = (relevant.get("Table", {}) or {}).get("Rows") or []

        hits: List[SearchHit] = []
        for row in rows[:limit]:
            cells = _extract_sp_cells(row)
            title = cells.get("Title") or "(untitled)"
            path = cells.get("Path") or base_url
            # HitHighlightedSummary contains <c0>...</c0> highlight markup +
            # <ddd/> ellipses; strip both for a plain-text snippet.
            summary_raw = cells.get("HitHighlightedSummary") or ""
            summary = re.sub(r"<c\d+>|</c\d+>|<ddd\s*/>", " ", summary_raw)
            summary = _strip_html(summary)
            last_modified = cells.get("LastModifiedTime") or None
            hits.append(SearchHit(
                title=_strip_html(title),
                url=path,
                snippet=summary,
                source="sharepoint",
                last_modified=last_modified,
            ))
        # TotalRows (confirmed live 2026-07-17: deduped total-match count,
        # distinct from TotalRowsIncludingDuplicates) drives has_more.
        total_rows = int(relevant.get("TotalRows") or 0)
        has_more = (offset + len(hits)) < total_rows
        return hits, has_more, None

    async def _search_orbit(
        self, query: str, limit: int, offset: int, cursor: Optional[str],
        src_cfg: Dict[str, Any],
    ) -> Tuple[List[SearchHit], bool, Optional[str]]:
        # Orbit has no documented JSON API in this project — DOM-extract is
        # the fallback, so unlike the other three sources it always needs
        # the browser session lock (real page navigation + DOM read).
        base_url = (src_cfg.get("base_url") or "https://orbit").rstrip("/")
        template = src_cfg.get("search_url_template") or "https://orbit/?q={q}"
        result_selector = src_cfg.get("result_selector") or "a"
        # 30s default keeps stalled-DNS failures actionable. Override via
        # web_search.sources.orbit.timeout_ms when the portal is genuinely slow.
        goto_timeout_ms = int(src_cfg.get("timeout_ms") or _DEFAULT_ORBIT_TIMEOUT_MS)

        try:
            search_url = template.format(q=urllib.parse.quote(query))
        except Exception as exc:
            raise RuntimeError(f"orbit search_url_template invalid: {exc}")

        # Orbit paginates by 1-indexed page number, not raw offset — confirmed
        # live 2026-07-17: &page=2 returned 10 distinct real CR numbers with
        # zero overlap against page 1. Only exact multiples of `limit` land
        # on a page boundary (documented in the schema's offset description).
        page_num = (offset // limit) + 1
        if page_num > 1:
            separator = "&" if "?" in search_url else "?"
            search_url = f"{search_url}{separator}page={page_num}"

        async with self.holder.acquire() as session:
            tab_id = session.first_tab_id()
            if tab_id is None:
                page = await session.context.new_page()
                tab_id = session.mint_tab_id()
                session.tabs[tab_id] = page
            else:
                page = session.tabs[tab_id]

            try:
                await page.goto(
                    search_url, wait_until="domcontentloaded", timeout=goto_timeout_ms,
                )
            except Exception as exc:
                raise RuntimeError(f"orbit navigate {search_url} failed: {exc}")

            # SSO redirect detection — if the user isn't logged in, orbit will
            # bounce to the corporate IdP. Use the post-navigation URL as the
            # signal; same body markers used elsewhere.
            landed = (page.url or "").lower()
            for marker in _SSO_BODY_MARKERS:
                if marker in landed:
                    raise _AuthRequired("orbit", base_url, 302)

            js = """
                ({selector, limit, baseUrl}) => {
                    const items = [];
                    const els = document.querySelectorAll(selector);
                    for (let i = 0; i < els.length && items.length < limit; i++) {
                        const a = els[i];
                        if (!a || !a.href) continue;
                        const title = (a.innerText || a.textContent || '').trim().slice(0, 200);
                        if (!title) continue;
                        let snippet = '';
                        let parent = a.closest('.search-result, .result, article, li, .card');
                        if (!parent) parent = a.parentElement;
                        // Real orbit structure: a sibling `.description` div
                        // under the same .search-result parent holds the
                        // actual description text (confirmed via live DOM
                        // probe 2026-07-17). Check this first.
                        if (parent) {
                            const descSibling = parent.querySelector('.description');
                            if (descSibling) {
                                snippet = (descSibling.innerText || descSibling.textContent || '')
                                    .trim().slice(0, 500);
                            }
                        }
                        // Generic fallback for other portal layouts / selector
                        // overrides that don't match this exact shape.
                        if (!snippet && parent) {
                            const sib = parent.querySelector('.summary, .snippet, p');
                            if (sib && sib !== a) {
                                snippet = (sib.innerText || sib.textContent || '')
                                    .trim().slice(0, 500);
                            }
                        }
                        items.push({title, url: a.href, snippet});
                    }
                    return items;
                }
            """
            try:
                raw = await page.evaluate(
                    js,
                    {"selector": result_selector, "limit": limit, "baseUrl": base_url},
                )
            except Exception as exc:
                raise RuntimeError(f"orbit DOM extract failed: {exc}")

        hits: List[SearchHit] = []
        for r in raw or []:
            url_val = (r.get("url") or "").strip()
            title_val = (r.get("title") or "").strip()
            if not url_val or not title_val:
                continue
            # Defense-in-depth: the default result_selector is now
            # `a.request-link`, which already excludes the portal's own
            # "create new CR" shortcuts (/CR/Create/Bug etc., bare <a> with
            # no class, confirmed via live DOM probe 2026-07-17) at the
            # selector level. This URL-substring check stays as a cheap
            # secondary filter in case a custom selector override ever
            # matches one of those chrome links too.
            if "/Create/" in url_val:
                continue
            hits.append(SearchHit(
                title=title_val,
                url=url_val,
                snippet=(r.get("snippet") or "").strip(),
                source="orbit",
                last_modified=None,
            ))
        # No total-count signal was found on the page during probing (no
        # "N results" text) — heuristic only: a full page of `limit` hits
        # suggests more may exist; an under-full page is treated as the last.
        has_more = len(hits) >= limit
        return hits, has_more, None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _fail(self, params: Dict[str, Any], start: float, msg: str) -> ToolResult:
        return ToolResult(
            success=False,
            output=None,
            tool_name=self.name,
            tool_parameters=params,
            error=msg,
            execution_time=time.time() - start,
        )
