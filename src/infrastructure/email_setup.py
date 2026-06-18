# -*- coding: utf-8 -*-
"""EmailContextProvider — prepare hint when Planner declares 'email'.

Activation checks (in order):
  1. win32com.client importable.
  2. pythoncom importable.
  3. Smoke-test: submit _get_app() to the COM executor with a 5s timeout.
     Catches "Outlook not installed" or "no MAPI profile" at step-start time
     so the agent gets an actionable error rather than a cryptic COM exception.

Progressive disclosure:
  Full guide on first activation per task; brief reminder thereafter.
  Uses Memory._email_contexts (parallel slot to _browser/_desktop_contexts).

Windows-only: registered alongside browser/desktop/web_search providers in
FlowController._register_default_providers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import win32com.client  # noqa: F401  type: ignore[import-untyped]
import pythoncom  # noqa: F401  type: ignore[import-untyped]

from .logger import get_logger
from ..controller_v2.context import ContextProvider, ItemContext, ProviderCache

if TYPE_CHECKING:
    from ..controller_v2.interaction_manager import InteractionManager


def _build_full_hint() -> str:
    return (
        "[Email Context — first activation in this task]\n"
        "The 'email' tool reads local Outlook mail via win32com COM. "
        "It reuses your MAPI profile — no extra credentials needed.\n"
        "\n"
        "Workflow:\n"
        "  1. action='list_folders'\n"
        "     → see folder names + unread counts (top-level only)\n"
        "  2. action='list_messages' folder='Inbox' [unread_only=true] [limit=20]\n"
        "     → returns entry_id + subject + sender + 500-char body_preview\n"
        "     → DEFAULTS to recursive=true: scans 'Inbox' AND every sub-folder\n"
        "       up to 4 levels deep (configurable via email.max_recursion_depth)\n"
        "  3. action='read_message' entry_id='<from list>' [include_full_body=true]\n"
        "     → ONLY needed when you want full body, to/cc/bcc, or attachment\n"
        "       metadata that aren't already in list_messages / search output.\n"
        "       For 'show me subject + sender + preview' tasks the search /\n"
        "       list_messages result already has everything — DO NOT re-fetch.\n"
        "  4. action='search' query='keyword' [folder='Inbox'] [sender_contains='alice']\n"
        "       [match_mode='phrase'|'substring'] [since='2026-05-26'] [limit=20]\n"
        "     → Index-backed search on subject + body. Defaults: match_mode='phrase'\n"
        "       (Windows Search content index — fast, word-level: 'fail' matches\n"
        "       'fail' but not 'failed'); recursive=true.\n"
        "     → search RETURNS the same per-message dict as list_messages\n"
        "       (entry_id / subject / sender_name / sender_email / received_at /\n"
        "       body_preview 500 chars / folder). You almost never need a\n"
        "       follow-up read_message after search.\n"
        "     → match_mode choice — read carefully:\n"
        "         • 'phrase' (default): hits the WDS index. Sub-second on a\n"
        "           folder of any size. Word-level — won't match substrings of\n"
        "           longer words. Tokens like 'qprof', 'meeting', 'sa8797p'\n"
        "           usually appear as their own word in body text and phrase\n"
        "           catches them just fine.\n"
        "         • 'substring': falls back to LIKE '%query%' over each item's\n"
        "           body. On a small folder (< 1000 msgs) it's fine. On a large\n"
        "           folder (10k+ msgs) it can take MINUTES — observed 6+ min on\n"
        "           a 17,893-msg folder. Only use it when (a) phrase returned\n"
        "           empty/insufficient and you're confident the keyword is a\n"
        "           sub-word fragment, or (b) the folder is small.\n"
        "     → Combine sender_contains with query for 'topic FROM person' —\n"
        "       both are index-side DASL filters and stay fast together.\n"
        "  5. action='download_attachment' entry_id='...' attachment_name='file.pdf'\n"
        "     → saved to %USERPROFILE%\\HandQ\\email_attachments\\ by default\n"
        "\n"
        "Key invariants:\n"
        "  - body_preview is 500 chars by default. Set include_body_preview=false\n"
        "    on list_messages / search when you only need metadata — this skips\n"
        "    the Body COM materialisation. Measured savings on a 67k-mail Inbox:\n"
        "    limit=100 → ~30% faster (11s → 8s); limit=50 → ~38% faster (5s →\n"
        "    3s); limit=20 → ~15%. Recommended whenever limit >= 50, or when the\n"
        "    agent is counting / bucketing messages by sender/date without\n"
        "    reading content. The response shape is unchanged — body_preview is\n"
        "    just \"\" when off.\n"
        "  - For full body / to / cc, use read_message with include_full_body=true.\n"
        "  - list_messages / search default to recursive=true. Enterprise users\n"
        "    typically have rules routing mail into Inbox sub-folders (Inbox/MST,\n"
        "    Inbox/AUTO, Inbox/Project-X, …); recursive=true is what 'show me\n"
        "    today's mail' actually means. Each message row carries a `folder`\n"
        "    field so you can see where it lives. Pass recursive=false only when\n"
        "    you deliberately want a single-folder view.\n"
        "  - The response also reports `folders_scanned` so you can confirm the\n"
        "    walk did what you expected.\n"
        "  - Truncation: list_messages caps at limit=200, search at limit=100.\n"
        "    The response carries `truncated: bool` and `total_estimated: int|null`:\n"
        "      • truncated=true means more matches existed than were returned.\n"
        "      • total_estimated is the index-side count BEFORE sender_contains /\n"
        "        subject_contains substring filters (null when those are active —\n"
        "        Outlook can't cheaply count substring matches).\n"
        "    When truncated=true, do NOT silently summarise — tell the user the\n"
        "    real total (e.g. '今日共 1000 封，列出最新 200'), and offer to narrow\n"
        "    the query (tighter `since`, add `sender_contains` / `subject_contains`,\n"
        "    or scope to a specific sub-folder).\n"
        "  - Concurrency: email is COM-STA-serialised — every email action runs\n"
        "    on a single dedicated thread under one asyncio lock. Dispatching\n"
        "    multiple email tool calls in parallel gives ZERO speedup; they will\n"
        "    execute sequentially regardless. Make N calls only when each is\n"
        "    individually necessary, and don't waste planning effort on parallel\n"
        "    fan-out for email.\n"
        "  - Outlook stays open — the tool never calls app.Quit().\n"
        "  - Write actions (compose_draft / send) are NOT in scope yet.\n"
        "  - Attachments are sandboxed; absolute paths outside the sandbox are refused.\n"
    )


def _build_brief_hint() -> str:
    return (
        "[Email Context] 'email' tool active — Outlook MAPI session is warm. "
        "Reminders: search/list_messages already include 500-char body_preview "
        "(don't re-fetch with read_message unless you need full body or to/cc); "
        "set include_body_preview=false for ~30-40% speedup when you only need "
        "metadata (limit >= 50, counting/bucketing); match_mode='phrase' is the "
        "default and is sub-second even on huge folders — only use 'substring' "
        "on small folders or when phrase returned empty; email is "
        "COM-STA-serialised so parallel email dispatches give no speedup, just "
        "call sequentially."
    )


class EmailContextProvider(ContextProvider):
    """Activate when the Planner declares ``email`` in tools_required."""

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "email"

    def planner_description(self) -> str:
        return (
            "`email` | "
            "Read / search local Outlook mail via COM (list folders, messages, read full body, "
            "search by keyword/sender/date, download attachment). Reuses the user's MAPI profile — "
            "no extra auth. Routing: `[\"email\"]`. | "
            "Step says 邮件 / inbox / 收件箱 / outlook / mail / 谁发我 / 翻邮箱 / 查邮件"
        )

    def planner_routing_rule(self) -> str:
        return "Read / search local Outlook email → `tools_required: [\"email\"]`"

    def planner_antipatterns(self) -> list:
        return [
            '`["browser"]` to read mail through OWA when Outlook is installed — '
            "that's `[\"email\"]`; OWA loses the MAPI shortcut and burns 5-10k tokens on page rendering",
            '`["email"]` for sending mail — write path not yet wired; composer + send come later',
        ]

    async def before_item(
        self,
        ctx: ItemContext,
        im: "InteractionManager",
        cache: ProviderCache,
    ) -> Optional[str]:
        # Smoke-test: verify Outlook.Application is reachable before the agent
        # spends tokens on email steps that will just fail with COM errors.
        # Skip the 5s executor round-trip when the handle is already cached
        # (i.e. a previous step already paid the cost) — common case after the
        # first activation in a process.
        import asyncio
        try:
            from ..tools.email_tool import (
                _outlook_executor, _get_app, is_outlook_app_ready,
            )
            if not is_outlook_app_ready():
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(_outlook_executor, _get_app),
                    timeout=5.0,
                )
        except asyncio.TimeoutError:
            return (
                "[Email Context — UNAVAILABLE]\n"
                "Outlook did not respond within 5 seconds. "
                "Confirm Outlook desktop is installed and the user is signed "
                "in to a MAPI profile, then retry."
            )
        except Exception as exc:
            return (
                f"[Email Context — UNAVAILABLE]\n"
                f"Could not connect to Outlook.Application: {exc}\n"
                "Confirm Outlook desktop is installed and the user is signed "
                "in to a MAPI profile."
            )

        cached = cache.get("email")
        if cached and cached.get("prepared"):
            return _build_brief_hint()
        cache.set("email", "default", {"prepared": True})
        return _build_full_hint()
