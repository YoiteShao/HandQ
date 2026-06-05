# -*- coding: utf-8 -*-
"""TeamsContextProvider — eager OAuth + planner hint when 'teams' declared.

Activation checks (in order):
  1. msal importable.
  2. httpx importable.
  3. Silent token check via TeamsAuth.get_token_silent().
  4. If silent fails → display message + interactive sign-in (browser pop).
  5. If interactive succeeds → cache the prepared flag in Memory and
     return the full hint.
  6. If interactive fails → return an UNAVAILABLE hint so the agent
     knows Teams cannot be used and can fall back / report.

Why eager auth: the user agreed to "激发式" — predictable timing. The
moment the planner declares ``teams``, prepare() either returns "all
set, here's how to use it" or pops the browser exactly once. After
that, every subsequent step that mentions teams sees a brief reminder
and the silent-token path keeps subsequent calls invisible.

Windows-only: registered alongside browser/desktop/email providers in
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
        "[Teams Context — first activation in this task]\n"
        "The 'teams' tool covers Microsoft Teams via Graph API + Teams\n"
        "internal API. It runs SILENTLY — Graph and Teams' chat backend\n"
        "are HTTPS endpoints; the user's Teams desktop app is unaffected,\n"
        "no UI is stolen. ~75% of Teams asks land in this tool; the rest\n"
        "have a documented browser_tool fallback below.\n"
        "\n"
        "FIRST CALL MAY BLOCK BRIEFLY:\n"
        "  Teams uses Microsoft Graph access tokens harvested from the\n"
        "  user's already-signed-in teams.microsoft.com session. The very\n"
        "  first teams.* call (and any call after the cached token has\n"
        "  expired, ~1 hour) opens an Edge window briefly to read the\n"
        "  freshest token MSAL.js has minted — typically 3-5 seconds when\n"
        "  the SSO cookie is warm, ~30 seconds on a fresh sign-in. Calls\n"
        "  within the same hour reuse the cached token and are completely\n"
        "  silent.\n"
        "  If bootstrap returns 'profile_locked', the browser tool is\n"
        "  currently using the same profile — finish the browser step\n"
        "  first, then retry teams.\n"
        "\n"
        "DO NOT try to find / read the token cache yourself. The tool\n"
        "owns that file (~/HandQ/teams_cache/tokens.json). Manually\n"
        "fishing the AT out and calling Graph via shell+curl is what\n"
        "previous failed runs tried — it is slower, brittle, and the\n"
        "tool already does it correctly. Just call the tool's actions.\n"
        "\n"
        "─────────────────────────────────────────────────────────────\n"
        "CAPABILITY MATRIX — what the user might ask vs what to do\n"
        "─────────────────────────────────────────────────────────────\n"
        "\n"
        "📅 CALENDAR / MEETINGS              ▶ teams tool (✅ direct)\n"
        "  'today's meetings' / 'next meeting'\n"
        "    → action='list_calendar_events' [start_after, end_before, top]\n"
        "    → top result is the next upcoming event\n"
        "  'details of a specific event'\n"
        "    → action='get_event' event_id='...'\n"
        "  'schedule a meeting with X tomorrow 3pm for 30 min'\n"
        "    → action='create_meeting' subject='...' start='...' end='...'\n"
        "      attendees=[{email,name}] online=true\n"
        "  'accept / decline / maybe an invite'\n"
        "    → action='respond_event' event_id='...' response='accept|decline|tentative'\n"
        "  'find a time when X, Y, Z are all free'\n"
        "    → action='find_meeting_times' attendees=[...] duration_minutes=30\n"
        "\n"
        "💬 CHAT (read & send)               ▶ teams tool (✅ mixed backend)\n"
        "  'list my chats' / 'who chatted me recently'\n"
        "    → action='list_chats' [top]    (uses Teams internal API)\n"
        "  'read messages from X' / 'last 20 messages in chat Y'\n"
        "    → action='read_chat' chat_id='...' [top]   (uses Teams internal)\n"
        "  'send X a message'\n"
        "    → action='send_chat' chat_id='...' message_html='...'\n"
        "      (always discover chat_id with list_chats first; do not guess)\n"
        "\n"
        "📢 TEAMS / CHANNELS                 ▶ teams tool (✅ direct)\n"
        "  'what teams am I in' / 'list channels in team X'\n"
        "    → action='list_teams' / 'list_channels' team_id='...'\n"
        "  'read messages from #general'\n"
        "    → action='read_channel' team_id='...' channel_id='...'\n"
        "  'post to channel'\n"
        "    → action='send_channel' team_id='...' channel_id='...' message_html='...'\n"
        "\n"
        "👤 PEOPLE                           ▶ teams tool (✅ direct)\n"
        "  'find Zhang San' / 'what's Alice's email'\n"
        "    → action='find_person' query='zhang san'\n"
        "    → returns display_name, title, department, emails[]\n"
        "\n"
        "🟢 PRESENCE / STATUS                ▶ teams tool (✅ read only)\n"
        "  'am I shown as busy / DND / available right now'\n"
        "    → action='get_presence'              (your own status)\n"
        "  'is Alice online'\n"
        "    → first find_person to get her id, then\n"
        "      action='get_presence' user_id='8:orgid:<her id>'\n"
        "  'set my status to Busy / DND'\n"
        "    → ❌ teams tool cannot WRITE presence (Microsoft scope policy)\n"
        "    → 🔁 use browser_tool: navigate to teams.microsoft.com,\n"
        "      click profile photo (top-right), pick status\n"
        "  'set custom status message'\n"
        "    → 🔁 same path; profile photo > Set status message\n"
        "\n"
        "📁 FILES (OneDrive backing Teams)   ▶ teams tool (✅ direct)\n"
        "  'find that PPT Alice shared yesterday'\n"
        "    → action='search_files' query='ppt yesterday alice'\n"
        "  'my recent files'\n"
        "    → action='list_recent_files' [top]\n"
        "\n"
        "✅ TASKS (Microsoft To Do)           ▶ teams tool (✅ direct)\n"
        "  'list my tasks' / 'what's due today'\n"
        "    → action='list_tasks' [top]\n"
        "  'add a task: review spec by Friday'\n"
        "    → action='create_task' title='...' due_date='2026-06-13T17:00:00'\n"
        "\n"
        "─────────────────────────────────────────────────────────────\n"
        "FALLBACK: when teams tool can't, route via browser_tool\n"
        "─────────────────────────────────────────────────────────────\n"
        "\n"
        "🔁 'join the next meeting' / 'open the conference link'\n"
        "    1. teams.list_calendar_events top=1   → grab join_url\n"
        "    2. browser_tool.navigate(join_url)\n"
        "    3. (optional) tell user the meeting opened in Edge\n"
        "\n"
        "🔁 'set my status to busy / DND / out of office'\n"
        "    browser_tool: navigate to teams.microsoft.com,\n"
        "    click avatar (top-right), select status / 'Set status message'\n"
        "\n"
        "🔁 'play the meeting recording from yesterday'\n"
        "    1. teams.list_calendar_events for yesterday → grab event web_link\n"
        "    2. browser_tool.navigate(web_link) → 'Recording' tab\n"
        "\n"
        "🔁 'show me what's on my Teams home / activity feed'\n"
        "    browser_tool: navigate to teams.microsoft.com/_#/activity\n"
        "\n"
        "─────────────────────────────────────────────────────────────\n"
        "❌ DO NOT TRY (genuinely impossible / out of scope)\n"
        "─────────────────────────────────────────────────────────────\n"
        "  • Join an active call's audio / video (no API for live media)\n"
        "    → tell the user to click join in Teams client themselves\n"
        "  • Change Teams settings / theme / notification preferences\n"
        "    → tell user where in Teams Settings to change it\n"
        "  • Read meeting recordings / transcripts (admin scope only)\n"
        "    → use the web_link from list_calendar_events\n"
        "  • Drive Teams desktop app via desktop_tool\n"
        "    → desktop_tool steals user mouse/keyboard; use browser fallback\n"
        "      against teams.microsoft.com instead — 10x faster, no UI theft\n"
        "\n"
        "─────────────────────────────────────────────────────────────\n"
        "KEY INVARIANTS\n"
        "─────────────────────────────────────────────────────────────\n"
        "  - top is capped at 50 per call. Paginate with multiple calls\n"
        "    if you need older history; do not push the cap higher.\n"
        "  - message_html is HTML (Teams' native format). Plain text works\n"
        "    too (Graph wraps it). 32 KB cap per message.\n"
        "  - send_* / create_meeting / respond_event are NOT undoable.\n"
        "    Verify identifiers with list_* / find_* BEFORE sending.\n"
        "  - For send_chat to a NEW person (no existing chat history),\n"
        "    you may need to start the chat in Teams Web first; the API\n"
        "    can post to existing chats only.\n"
        "  - Sensitive fields: message_html may contain proprietary text;\n"
        "    don't echo it back to the user when summarising — reference\n"
        "    by message_id instead.\n"
        "  - Every call is rate-limited (~12k req / 10 min / app). The\n"
        "    tool surfaces 429 with Retry-After on throttle.\n"
        "  - 401 mid-task: bootstrap auto-runs on the next call (~3-5s\n"
        "    while cookie is warm). Just retry the same teams action once.\n"
    )


def _build_brief_hint() -> str:
    return (
        "[Teams Context] 'teams' tool active — Graph + chatsvcagg cache warm. "
        "Reminders: list_*/find_* BEFORE create_*/send_*; calendar / chats / "
        "channels / files / tasks all in scope; for write-presence or join-call "
        "use browser_tool against teams.microsoft.com; do NOT shell-search the "
        "token cache, the tool handles auth internally."
    )


def _build_unavailable_hint(reason: str) -> str:
    return (
        "[Teams Context — UNAVAILABLE]\n"
        f"Could not initialise Teams Graph API: {reason}\n"
        "The 'teams' tool will fail on every call this task. If the user "
        "still needs Teams, ask them to retry after fixing the issue, or "
        "fall back to browser on teams.microsoft.com."
    )


class TeamsContextProvider(StepContextProvider):
    """Activate when the Planner declares ``teams`` in tools_required."""

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "teams"

    def planner_description(self) -> str:
        return (
            "`teams` | "
            "Microsoft Teams via Graph + Teams internal API. Calendar "
            "(view/create/respond meetings, find times), chats (list/read/send), "
            "channels, presence (read), people search, OneDrive files, "
            "Microsoft To Do tasks. Silent — does not steal mouse/keyboard or "
            "open the Teams UI. Routing: `[\"teams\"]`. | "
            "Step says Teams 日程 / 下个会 / 排会议 / 聊天 / 发消息 / "
            "channel post / 找人 / 我状态 / 看会议邀请 / send a Teams message"
        )

    def planner_routing_rule(self) -> str:
        return (
            "Microsoft Teams READ ops (calendar list/get, chat list/read, "
            "channel list, find people, files, tasks, presence READ) → "
            "`tools_required: [\"teams\"]`. "
            "Teams WRITE ops the Graph token doesn't cover (set presence "
            "Busy/DND, set custom status message, join a live call, "
            "play a recording, change Teams settings) → "
            "`tools_required: [\"teams\", \"browser\"]` so the agent can "
            "fall back to browser_tool against teams.microsoft.com after "
            "the teams tool reports the API gap."
        )

    def planner_antipatterns(self) -> list:
        return [
            '`["browser"]` for teams.microsoft.com when teams tool covers it — '
            "the Graph + internal API path is silent and ~10x faster than "
            "driving the web UI",
            '`["desktop"]` to operate the Teams desktop app — that steals '
            "user input. Use `[\"teams\"]` for data, `[\"browser\"]` against "
            "teams.microsoft.com only for write-presence / join-call",
            '`["shell"]` to search for the teams token cache and call Graph '
            "manually — the tool already handles tokens internally; calling "
            "shell+curl will be slower and is forbidden by the hint",
            '`["teams"]` ALONE for set-presence (Busy/DND) / set status '
            "message / join a live call / play a recording — Microsoft does "
            "not grant these scopes to Teams Web's client. Use "
            '`["teams", "browser"]` so the agent can teams.list_calendar_events '
            "(get the join_url) and then browser_tool.navigate to do the "
            "thing teams alone cannot.",
        ]

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        # 1. Library availability — same actionable error the agent gets
        # later if it tries the tool, but surfaced earlier so the planner
        # can route differently without burning a tool call.
        try:
            import httpx  # noqa: F401  type: ignore[import-not-found]
        except ImportError:
            return _build_unavailable_hint(
                "httpx is not installed. Run: pip install httpx"
            )
        try:
            import playwright  # noqa: F401  type: ignore[import-not-found]
        except ImportError:
            return _build_unavailable_hint(
                "playwright is not installed. Required for the one-time "
                "bootstrap that reads the user's Teams Web session. "
                "Run: pip install playwright && playwright install chromium"
            )

        # 2. Progressive disclosure. Auth is no longer triggered here —
        # it lives inside teams_tool's first-call gate (lazy bootstrap
        # via the browser_profile against teams.microsoft.com), so this
        # path stays cheap (~0ms) and resilient to mid-step environment
        # changes (e.g. agent installing playwright then immediately
        # using the tool).
        cached = memory.get_teams_context("default")
        if cached and cached.get("prepared"):
            return _build_brief_hint()
        memory.set_teams_context("default", {"prepared": True})
        return _build_full_hint()
