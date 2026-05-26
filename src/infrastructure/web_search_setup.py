# -*- coding: utf-8 -*-
"""WebSearchContextProvider — prepare hint when Planner declares 'web_search'.

The provider does no credential setup of its own — auth is reused from
``browser_tool``'s persistent profile. Activation only verifies that
playwright is importable (so tool calls can succeed) and emits the workflow
hint with progressive disclosure: full guide on first activation per task,
brief reminder thereafter.

Storage reuses ``Memory._browser_contexts`` under the key ``"web_search"``
(a parallel slot to BrowserContextProvider's ``"default"``) so no Memory
schema changes are needed and the cache is wiped together with the browser
session at task close.

Windows-only: registered alongside browser/desktop providers in
FlowController._register_default_providers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .logger import get_logger
from .step_context_provider import StepContextProvider

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


def _build_full_hint() -> str:
    return (
        "[Web Search Context — first activation in this task]\n"
        "The 'web_search' tool searches Qualcomm internal sources via the\n"
        "authenticated browser session. Sources: confluence, jira,\n"
        "sharepoint, orbit. Cookies + SSO are reused from the persistent\n"
        "browser profile — the user logs in once per source, future\n"
        "sessions inherit the cookie.\n"
        "\n"
        "Workflow:\n"
        "  1. Ensure browser is launched (browser.launch_browser is\n"
        "     idempotent — safe to call before every web_search if unsure).\n"
        "  2. action='search', source='confluence'|'jira'|'sharepoint'|'orbit',\n"
        "     query='...', limit=10. Returns up to 'limit' hits with\n"
        "     {title, url, snippet, source, last_modified}.\n"
        "  3. Snippets are capped at ~300 chars. To read the full document\n"
        "     after locating it via search, call browser.navigate to the\n"
        "     hit's url and use extract — do NOT auto-fetch full content\n"
        "     here; that would force unbounded payloads into LLM context.\n"
        "  4. Login recovery: when the result error reads\n"
        "     '<source> requires login (status=401|403|3xx)', the steps are:\n"
        "       a. browser.navigate url='<source's base_url>'\n"
        "       b. browser.request_user_login reason='auth <source>',\n"
        "          success_url_pattern='<base_url>'\n"
        "       c. After user approves, retry the same web_search call.\n"
        "       Cookies persist across HandQ sessions, so this dance is\n"
        "       at most once per source until cookies expire.\n"
        "  5. Sources: confluence (CQL), jira (JQL), sharepoint (KQL),\n"
        "     orbit (DOM-extract). Plain free-text queries work for all\n"
        "     four — query-language keywords are just available when\n"
        "     useful (e.g. 'space=ANDROID AND text ~ \"trace\"').\n"
    )


def _build_brief_hint() -> str:
    return (
        "[Web Search Context] tool 'web_search' available; "
        "the persistent browser session and SSO cookies from earlier "
        "steps are reused. All four sources (confluence / jira / "
        "sharepoint / orbit) are wired."
    )


class WebSearchContextProvider(StepContextProvider):
    """Activate when Planner declares ``web_search`` in tools_required."""

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "web_search"

    def planner_description(self) -> str:
        return (
            "`web_search` | "
            "Search Qualcomm internal sources (Confluence / Jira / SharePoint / orbit). "
            "Requires browser session — always pair with `\"browser\"` in tools_required. "
            "Routing: `[\"browser\", \"web_search\"]`. | "
            "Step says 搜 / search / find + internal source name, or 查内网/wiki/Confluence/Jira"
        )

    def planner_routing_rule(self) -> str:
        return (
            "Internal search across Confluence/Jira/SharePoint/orbit → "
            "`tools_required: [\"browser\", \"web_search\"]`  "
            "(web_search reuses the browser session for SSO cookies)"
        )

    def planner_antipatterns(self) -> list:
        return [
            '`["web_search"]` without `"browser"` — web_search reuses the browser session '
            'and will fail with "no session"',
        ]

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        try:
            from ..tools.browser_tool import is_browser_available
        except ImportError:
            self.logger.warning(
                "WebSearchContextProvider: browser_tool not importable",
                component="WebSearchContextProvider",
            )
            return (
                "[Web Search Context — UNAVAILABLE]\n"
                "browser_tool module is not importable; web_search depends "
                "on it. Report this to the user and skip the search path."
            )

        if not is_browser_available():
            return (
                "[Web Search Context — UNAVAILABLE]\n"
                "playwright is not installed. The web_search tool will "
                "fail. Run:\n"
                "  pip install playwright\n"
                "  playwright install msedge"
            )

        cached = memory.get_browser_context("web_search")
        if cached and cached.get("prepared"):
            return _build_brief_hint()
        memory.set_browser_context("web_search", {"prepared": True})
        return _build_full_hint()
