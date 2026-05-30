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

import json
import re
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from .base_tool import BaseTool, ToolResult
from .browser_tool import acquire_browser_lock, evaluate_fetch, is_browser_available
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.logger import get_logger


# ── Defaults (overridable via handq_config.yaml web_search section) ──────────
_DEFAULT_LIMIT = 10
_HARD_MAX_LIMIT = 25
_DEFAULT_SNIPPET_MAX_CHARS = 300
_DEFAULT_ORBIT_TIMEOUT_MS = 10_000

# Per-process cache for repeat (source, query, limit) lookups. Bounded LRU
# with a 60s TTL — covers the common case where the agent re-runs the same
# query during retry / re-plan within a single task. Skipped for orbit
# because its DOM-extract path may legitimately return different results
# as the portal mutates.
#
# TODO(perf): cross-source parallel fan-out is gated on ``acquire_browser_lock``
# holding the global session lock for each fetch. True parallelism needs a
# refactor of ``evaluate_fetch`` (per-source dedicated tabs so concurrent
# fetches don't collide on same_origin navigation).
_QUERY_CACHE_TTL = 60.0
_QUERY_CACHE_MAX = 50
_query_cache: "OrderedDict[Tuple[str, str, int], Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()


def _cache_get(key: Tuple[str, str, int]) -> Optional[List[Dict[str, Any]]]:
    """Return cached hits if present and unexpired; refresh LRU position."""
    entry = _query_cache.get(key)
    if entry is None:
        return None
    timestamp, hits = entry
    if (time.time() - timestamp) > _QUERY_CACHE_TTL:
        _query_cache.pop(key, None)
        return None
    _query_cache.move_to_end(key)
    return hits


def _cache_put(key: Tuple[str, str, int], hits: List[Dict[str, Any]]) -> None:
    """Insert hits into the cache, evicting the oldest entry on overflow."""
    _query_cache[key] = (time.time(), hits)
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
    """True if the response looks like an SSO login wall, not real data."""
    if status in (401, 403):
        return True
    if status in (302, 303, 307, 308):
        return True
    bl = (body or "").lower()
    return any(marker in bl for marker in _SSO_BODY_MARKERS)


def _strip_html(s: str) -> str:
    """Cheap tag stripper for excerpt fields (Confluence highlights, SP <c0>)."""
    return _HTML_TAG_RE.sub("", s or "").strip()


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
                    "fan out by issuing parallel calls. Sources can be "
                    "individually disabled in handq_config.yaml under "
                    "web_search.sources.<name>.enabled."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Max results to return. Default {_DEFAULT_LIMIT}. "
                    f"Hard cap {_HARD_MAX_LIMIT}; values above are clamped."
                ),
            },
        },
        "required": ["query", "source"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        super().__init__("web_search")
        self.logger = get_logger()

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
        # same (source, query, limit) was answered recently. Orbit isn't
        # cached because its DOM-extract may legitimately change.
        cache_key: Optional[Tuple[str, str, int]] = (
            None if source == "orbit" else (source, query, limit)
        )
        if cache_key is not None:
            cached_hits = _cache_get(cache_key)
            if cached_hits is not None:
                return ToolResult(
                    success=True,
                    output={
                        "source": source,
                        "query": query,
                        "count": len(cached_hits),
                        "hits": cached_hits,
                        "cached": True,
                    },
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=time.time() - start,
                )

        try:
            async with acquire_browser_lock() as session:
                hits = await executor(session, query, limit, src_cfg)
        except _AuthRequired as exc:
            return self._fail(params, start, str(exc))
        except RuntimeError as exc:
            # acquire_browser_lock raises this when no session is launched.
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

        # Snippet truncation — applied last so per-source parsers can see
        # full excerpts during normalisation.
        for h in hits:
            if h.snippet and len(h.snippet) > snippet_max:
                h.snippet = h.snippet[:snippet_max] + "…"

        hit_dicts = [h.to_dict() for h in hits]
        if cache_key is not None:
            _cache_put(cache_key, hit_dicts)

        return ToolResult(
            success=True,
            output={
                "source": source,
                "query": query,
                "count": len(hits),
                "hits": hit_dicts,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── Per-source executors ─────────────────────────────────────────────────

    async def _search_confluence(
        self, session: Any, query: str, limit: int, src_cfg: Dict[str, Any],
    ) -> List[SearchHit]:
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
        qs = urllib.parse.urlencode({"cql": cql, "limit": limit})
        url = f"{base_url}{api_path}?{qs}"

        result = await evaluate_fetch(
            session, url=url, method="GET",
            headers={"Accept": "application/json"}, same_origin=True,
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
            raise RuntimeError(f"confluence returned non-JSON: {body[:300]}")

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
        return hits

    async def _search_jira(
        self, session: Any, query: str, limit: int, src_cfg: Dict[str, Any],
    ) -> List[SearchHit]:
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
        })
        url = f"{base_url}{api_path}?{qs}"

        result = await evaluate_fetch(
            session, url=url, method="GET",
            headers={"Accept": "application/json"}, same_origin=True,
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
            raise RuntimeError(f"jira returned non-JSON: {body[:300]}")

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
            desc_clean = re.sub(r"\{[^}]+\}", "", desc_raw).strip()
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
        return hits

    async def _search_sharepoint(
        self, session: Any, query: str, limit: int, src_cfg: Dict[str, Any],
    ) -> List[SearchHit]:
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
        })
        url = f"{base_url}{api_path}?{qs}"

        # Only the Accept header — SP Online uses OData 3.0; explicitly
        # setting OData-Version to 4.0 (an earlier draft of this code did
        # so) made the search service return HTTP 500 "Unknown Error".
        # ``odata=nometadata`` flattens the response so callers don't wade
        # through __metadata wrappers.
        result = await evaluate_fetch(
            session, url=url, method="GET",
            headers={"Accept": "application/json;odata=nometadata"},
            same_origin=True,
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
            raise RuntimeError(f"sharepoint returned non-JSON: {body[:300]}")

        rows = (
            ((data.get("PrimaryQueryResult") or {})
             .get("RelevantResults") or {})
            .get("Table", {})
            .get("Rows")
        ) or []

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
        return hits

    async def _search_orbit(
        self, session: Any, query: str, limit: int, src_cfg: Dict[str, Any],
    ) -> List[SearchHit]:
        # Orbit has no documented JSON API in this project — DOM-extract is
        # the fallback. The selector is configurable so it can be tuned
        # without code changes when the portal markup shifts.
        base_url = (src_cfg.get("base_url") or "https://orbit").rstrip("/")
        template = src_cfg.get("search_url_template") or "https://orbit/?q={q}"
        result_selector = src_cfg.get("result_selector") or "a"
        # 10s default keeps stalled-DNS failures actionable. Override via
        # web_search.sources.orbit.timeout_ms when the portal is genuinely slow.
        goto_timeout_ms = int(src_cfg.get("timeout_ms") or _DEFAULT_ORBIT_TIMEOUT_MS)

        try:
            search_url = template.format(q=urllib.parse.quote(query))
        except Exception as exc:
            raise RuntimeError(f"orbit search_url_template invalid: {exc}")

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
                    if (parent) {
                        const sib = parent.querySelector(
                            '.summary, .snippet, .description, p'
                        );
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
            hits.append(SearchHit(
                title=title_val,
                url=url_val,
                snippet=(r.get("snippet") or "").strip(),
                source="orbit",
                last_modified=None,
            ))
        return hits

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
