"""Deterministic fake frontend for the v2 orchestration service.

A real production frontend (tmux/electron/stdio bridge) reduces to the same
contract against ``OrchestrationService.run_idle``:

    goal source        — drain a queue (one queued message == one goal)
    should_exit        — an exit sentinel / the UI going away
    on_goal_received   — UI state -> "running"
    on_node_done       — status-bar detail
    on_goal_complete   — UI state -> "completed" + summary
    amend              — a follow-up message mid-run steers the running goal

This module plays that contract with NO platform, NO files, NO LLM: an in-memory
goal queue stands in for the IPC dir and a ``StubExecutor`` drives the graph. It
is therefore safe to import in tests AND runnable as a script
(``python -m backend.fake_frontend``) for a manual demo of the whole idle /
amend loop.

It is deliberately the ONLY frontend the backend tests depend on — the platform
entries stay untested here so a backend change can't be gated on tmux or the
stdio bridge.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Optional

from .config import BackendConfig
from .coordinator import Coordinator
from .engine.executor import AgentRunOutput, StubExecutor
from .orchestration.router import Router
from .service import ControlChannel, Lifecycle, OrchestrationService


class SlowStubExecutor(StubExecutor):
    """A ``StubExecutor`` that awaits ``delay`` seconds per node.

    Lets a demo show control signals landing *between* nodes of a multi-node
    graph: a hard stop trips the runner's ``should_abort`` check, and an
    amendment delivered during the delay is drained by the next node. With
    instant nodes the run finishes before the watcher's first poll.
    """

    def __init__(self, *, delay: float = 0.05, **kw: Any) -> None:
        super().__init__(**kw)
        self._delay = delay

    async def run(self, **kw: Any) -> AgentRunOutput:
        await asyncio.sleep(self._delay)
        return await super().run(**kw)


def build_stub_service(
    *,
    executor: Optional[StubExecutor] = None,
    classifier=None,
    channel: Optional[ControlChannel] = None,
    working_dir: str = "/tmp/fake",
) -> tuple[OrchestrationService, ControlChannel]:
    """Assemble the same Coordinator + OrchestrationService stack the real
    entries build, but around a ``StubExecutor`` so it runs with no model.

    ``classifier`` (a ``goal -> pattern_id`` coroutine) lets a caller force a
    multi-node template (e.g. ``modify``); without it the Router fails safe to a
    single freeform loop. Returns the service plus the channel it was built
    around so a caller can raise stop/amend signals.

    The default stub is wired to the channel's ``drain_amendments`` so a
    mid-run amendment is observable. A caller passing its own executor (e.g.
    ``SlowStubExecutor``) should construct it with
    ``drain_amendments=channel.drain_amendments`` to get the same behavior.
    """
    channel = channel or ControlChannel()
    coord = Coordinator(
        executor=executor or StubExecutor(drain_amendments=channel.drain_amendments),
        router=Router(classifier=classifier),
        config=BackendConfig(),
    )
    service = OrchestrationService(
        coord,
        working_dir=working_dir,
        channel=channel,
    )
    return service, channel


class FakeFrontend(Lifecycle):
    """In-memory stand-in for the IPC frontend driving ``OrchestrationService``.

    Subclasses ``Lifecycle`` so it plugs straight into
    ``service.run_idle(lifecycle=self)``. Mirrors a production frontend without
    files or a real UI:

      * ``goals``           — a deque queue of pending goal messages
      * ``exit_requested``  — the exit sentinel
      * ``state``           — the JSON the UI renders
      * ``events``          — an ordered lifecycle log, for test assertions

    Pair it with ``build_stub_service`` for a fully deterministic loop.
    """

    def __init__(
        self,
        service: OrchestrationService,
        channel: ControlChannel,
        *,
        exit_when_idle: bool = False,
    ) -> None:
        self.service = service
        self.channel = channel
        # When True, run_idle exits once the queue drains (and nothing is
        # running) — handy for a bounded test/demo. The real frontend leaves
        # this False and exits on its own sentinel instead.
        self._exit_when_idle = exit_when_idle
        self.goals: deque[str] = deque()
        self.exit_requested = False
        self.state: dict[str, Any] = {"status": "idle", "detail": "", "history": []}
        self.events: list[tuple] = []

    # ── frontend -> service inputs ────────────────────────────────────────────
    def enqueue(self, goal: str) -> None:
        """A user message arriving on the IPC queue."""
        self.goals.append(goal)

    def request_exit(self) -> None:
        self.exit_requested = True

    def request_stop(self) -> None:
        """Hard-stop the running goal (the abort path, distinct from amend)."""
        self.service.stop("user stop")

    def next_goal(self) -> Optional[str]:
        return self.goals.popleft() if self.goals else None

    def should_exit(self) -> bool:
        if self.exit_requested:
            return True
        if not self._exit_when_idle:
            return False
        if self.goals or self.service.is_running:
            return False
        return True

    # ── service -> frontend lifecycle ─────────────────────────────────────────
    def on_goal_received(self, goal: str) -> None:
        self.state.update(status="running", detail=goal[:200])
        self.events.append(("recv", goal))

    def on_node_done(self, name: str, result) -> None:
        self.state["detail"] = f"{name}: {'ok' if result.ok else 'fail'}"
        self.events.append(("node", name, bool(result.ok)))

    def on_goal_complete(self, report) -> None:
        status = "done" if report.ok else "failed"
        self.state.update(status=status, detail=report.last_summary)
        self.state["history"].append((status, report.last_summary))
        self.events.append(("complete", report.ok))

    # ── the amend watcher (mirrors a real frontend's mid-run redirect) ────────
    async def _amend_watcher(self, poll: float) -> None:
        # A message arriving WHILE a goal runs is a refinement, not a new task:
        # hand it to the in-flight subagent via amend (folded in mid-node) and
        # let the run continue. Messages that arrive while idle stay on the queue
        # and become the next goal instead.
        while not self.exit_requested:
            await asyncio.sleep(poll)
            while self.service.is_running and self.goals:
                self.service.amend(self.goals.popleft())

    async def serve(
        self,
        *,
        poll_interval: float = 0.01,
        watch_amendments: bool = True,
    ) -> None:
        """Run the service idle loop with the amend watcher attached."""
        watcher = (
            asyncio.create_task(self._amend_watcher(poll_interval))
            if watch_amendments else None
        )
        try:
            await self.service.run_idle(
                self.next_goal,
                should_exit=self.should_exit,
                lifecycle=self,
                poll_interval=poll_interval,
            )
        finally:
            self.exit_requested = True
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass


# ── runnable demo ──────────────────────────────────────────────────────────────

def _banner(title: str) -> None:
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72, flush=True)


async def _demo_drain() -> None:
    _banner("DEMO 1 — idle loop drains a queue of goals, one at a time")
    service, channel = build_stub_service()
    fe = FakeFrontend(service, channel, exit_when_idle=True)
    for g in ("goal-alpha", "goal-beta", "goal-gamma"):
        fe.enqueue(g)
    await fe.serve()
    print(f"  completed: {[h[1] for h in fe.state['history']]}", flush=True)
    print(f"  final state: status={fe.state['status']!r}", flush=True)
    print(f"  events: {fe.events}", flush=True)


async def _demo_amend() -> None:
    _banner("DEMO 2 — a follow-up message amends the running goal (no abort)")

    async def classifier(_goal: str) -> str:
        return "modify"

    ch = ControlChannel()
    svc, ch = build_stub_service(
        executor=SlowStubExecutor(delay=0.05, drain_amendments=ch.drain_amendments),
        classifier=classifier,
        channel=ch,
    )
    fe = FakeFrontend(svc, ch, exit_when_idle=True)
    fe.enqueue("long modify task")

    async def redirect() -> None:
        # Wait until the first goal is running, then drop a follow-up. The amend
        # watcher folds it into the running goal mid-node — the run continues.
        while not svc.is_running:
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.02)  # let it get a node or two in
        fe.enqueue("also rename the helper while you're in there")

    await asyncio.gather(fe.serve(), redirect())
    print(f"  events: {fe.events}", flush=True)
    print(f"  history: {fe.state['history']}", flush=True)


async def _main() -> None:
    await _demo_drain()
    await _demo_amend()
    _banner("DONE")


if __name__ == "__main__":
    asyncio.run(_main())
