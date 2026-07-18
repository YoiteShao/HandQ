"""Scheduler — fire pinned HandQ prompts on a cadence.

Why we need this
----------------
Users keep typing the same prompt every morning ("summarise yesterday's
PRs", "draft my standup update", "check the build"). The Scheduler lets
them pin those into a JSON store and have the bridge run them
automatically at the cadence they pick.

Safety story (the user's "is this safe?" question)
--------------------------------------------------
- Each fire goes through the EXACT SAME ``FlowController`` flow as a
  user-typed request. Every confirmation gate, every risk gate, every
  permission switch in ``handq_config.yaml :: interaction_switches``
  applies. There is no separate "script execution" sandbox to audit.
- We refuse to start a scheduled task while a session is already
  active (``SCHEDULER_BUSY_POLICY = "skip"``). The user is always in
  the loop for at most one task at a time.
- A task that fails ``SCHEDULER_MAX_FAILURES_BEFORE_DISABLE`` times in a
  row is auto-disabled and the user gets an IPC notification. This
  prevents a misconfigured task from looping in the background.
- ``SCHEDULER_MIN_INTERVAL_SEC`` rejects schedules that fire faster
  than the floor — protects the LLM stack from runaway use.

UI integration
--------------
The bridge exposes ``cron_list`` / ``cron_create`` /
``cron_delete`` / ``cron_set_enabled`` / ``cron_run_now`` IPC envelopes.
When a task fires, the scheduler synthesises a regular ``request``
envelope through the bridge so the renderer sees the new conversation
land in the UI exactly like a manual run — that's why "结果可以走 handq
的 UI 就行" works without any new rendering surface.

Agent integration (Claude-Code CronCreate/ScheduleWakeup parity)
----------------------------------------------------------------
Beyond the UI, the AGENT can drive scheduling directly via on-demand tools
(``src/tools/schedule_tool.py`` + ``schedule_wakeup_tool.py``):

- ``schedule_create`` / ``schedule_list`` / ``schedule_delete`` wrap this
  service (reached through ``ctx.scheduler``); each fire runs in a fresh
  ``sched-{uuid}`` session, same as a UI-created cron. ``schedule_create``
  defaults ``durable=False`` (session-only, in-memory) matching Claude Code's
  ``CronCreate`` default; ``durable=True`` persists to scheduled_tasks.json.
- ``schedule_wakeup`` is a SEPARATE, session-scoped self-paced-loop primitive
  that does NOT touch this service — it re-queues a prompt onto the current
  session's TaskChannel after a delay (see SessionContext.schedule_wakeup),
  preserving the agent's context across the tick.

Schedule grammar accepts both the friendly forms (``every N minutes`` /
``daily HH:MM`` / ``weekly DOW HH:MM`` / ``once at`` / ``once in N``) AND
standard 5-field cron (``*/5 * * * *``). Recurring tasks auto-expire after
7 days (SCHEDULER_RECURRING_MAX_AGE_SEC); fires carry deterministic jitter
to avoid thundering-herd alignment.
"""
from __future__ import annotations

from .service import Scheduler

__all__ = ["Scheduler"]
