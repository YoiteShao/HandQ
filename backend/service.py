"""OrchestrationService — the long-running production seam.

``Coordinator.handle_goal`` is one-shot: one goal in, one ``RunReport`` out.
A real session is the opposite — a service that comes up idle, waits for
goals to arrive on a queue, runs each to completion, reports lifecycle, and
survives interrupts. This module is the missing layer between the two.

Three pieces:

  * ``ControlChannel`` — owns the two control signals a frontend raises against
    an in-flight goal. **STOP** (the hard interrupt): ``event`` kills in-flight
    shell/session work, ``check_stop`` is polled before every agent iteration to
    break the loop, ``should_abort`` stops the runner's graph walk between nodes.
    **AMEND** (the soft redirect): ``amend`` enqueues a follow-up note that
    ``drain_amendments`` hands the running subagent mid-node — it folds the note
    in as a USER turn and *keeps going* rather than aborting. One object, so a
    frontend (or test) steers a single run and every layer sees it.

  * ``Lifecycle`` — the public-API observer. Four lifecycle moments
    (``on_goal_received`` / ``on_node_done`` / ``on_goal_complete`` /
    ``on_idle_tick``) live as default no-op methods on one class; subclass
    and override what you need. A single object replaces the previous "four
    independent callables threaded through every call".

  * ``OrchestrationService`` — wraps a ``Coordinator`` + ``ControlChannel``
    into ``run_goal`` (one goal, fires lifecycle) and ``run_idle`` (poll a goal
    source forever, running each goal to completion).

Deliberately pure orchestration: NO ``src`` import, so the whole idle/interrupt
service loop is testable with ``StubExecutor`` and a scripted goal source — no
LLM, no filesystem, no subprocess. A real frontend builds the executor (with
this channel's primitives) and supplies the queue-backed goal source + a
``Lifecycle`` subclass that maps the events onto its UI / IPC.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Optional, Union

from .coordinator import Coordinator
from .orchestration.runner import RunReport


async def _maybe_await(value: Union[Any, Awaitable[Any]]) -> Any:
    """Await ``value`` if it is awaitable, else return it as-is.

    Lets every injected hook (goal source, exit predicate, lifecycle sink
    methods) be either sync or async — the queue-backed production version
    is a sync read; a test may hand in a coroutine.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class ControlChannel:
    """Two independent control signals shared by the executor and the runner.

    **STOP** — the hard interrupt. ``stop`` sets the event (kills in-flight shell
    work immediately), arms the message ``check_stop`` returns (breaks the agent
    loop with the stop reason on its next poll), and trips ``should_abort`` (stops
    the graph walk between nodes). ``reset`` clears it between goals so a stale
    stop never leaks into the next run.

    **AMEND** — the soft, additive redirect. ``amend`` enqueues a follow-up note;
    ``drain_amendments`` hands the running subagent the pending notes (and clears
    them) so it folds them in as USER messages mid-node and *keeps going*. Unlike
    STOP, an amendment never aborts — it steers work already underway. ``reset``
    drops any undrained notes so they can't leak into the next goal.
    """

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self._message: Optional[str] = None
        self._amendments: list[str] = []

    def reset(self) -> None:
        self._message = None
        self.event.clear()
        self._amendments.clear()

    # ── STOP ──────────────────────────────────────────────────────────────────
    def stop(self, message: str = "user interrupt") -> None:
        """Hard-stop the in-flight goal: arm every STOP seam at once."""
        self._message = message
        self.event.set()

    # executor seam: the agent polls this before every iteration; a non-None
    # return breaks the loop.
    def check_stop(self) -> Optional[str]:
        return self._message

    # runner seam: WorkflowRunner calls this between nodes to abort the walk.
    def should_abort(self) -> bool:
        return self.event.is_set()

    @property
    def is_set(self) -> bool:
        return self.event.is_set()

    # ── AMEND ─────────────────────────────────────────────────────────────────
    def amend(self, message: str) -> None:
        """Queue a follow-up instruction for the running subagent to fold in.

        Blank notes are dropped; a real one waits until the subagent's next
        between-step ``drain_amendments`` poll, then enters the buffer as a USER
        turn without aborting the run.
        """
        text = message.strip()
        if text:
            self._amendments.append(text)

    def drain_amendments(self) -> list[str]:
        """Return and clear the pending amendment notes (oldest first)."""
        pending = self._amendments
        self._amendments = []
        return pending


# Goal source: produces the next goal text or ``None`` when the queue is empty.
GoalSource = Callable[[], Union[Optional[str], Awaitable[Optional[str]]]]
# Exit predicate: tells the idle loop to bail.
ExitPredicate = Callable[[], Union[bool, Awaitable[bool]]]


class Lifecycle:
    """Observer for service-level lifecycle moments.

    All four methods default to no-ops; subclass and override what you need —
    this is the cleaner replacement for "four independent ``Optional[Callable]``
    parameters threaded through every method".

    Sync-vs-async contract:

      * ``on_goal_received`` / ``on_goal_complete`` / ``on_idle_tick`` are
        awaited by the service via ``_maybe_await``, so a subclass may
        implement them as ``def`` or ``async def``.
      * ``on_node_done`` is invoked **synchronously by the runner** (between
        nodes, in the middle of a tight loop). It must be a plain ``def`` —
        anything that needs IO should schedule itself onto the loop with
        ``asyncio.create_task`` rather than declaring the method ``async``.
    """

    def on_goal_received(self, goal: str) -> Any:
        return None

    def on_node_done(self, name: str, result: Any) -> Any:
        return None

    def on_goal_complete(self, report: RunReport) -> Any:
        return None

    def on_idle_tick(self) -> Any:
        return None


class OrchestrationService:
    """Drives a ``Coordinator`` as an interruptible idle service.

    The coordinator stays one-shot and stateless across goals; this object owns
    the cross-goal concerns: the shared interrupt channel and the lifecycle
    sink the frontend uses to reflect progress.
    """

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        working_dir: Optional[str] = None,
        channel: Optional[ControlChannel] = None,
    ) -> None:
        self._coordinator = coordinator
        self._working_dir = working_dir
        # If the caller didn't pass the channel the executor was built around,
        # make a fresh one — useful for the StubExecutor path where the executor
        # ignores interrupts anyway. should_abort still gates the runner walk.
        self._channel = channel or ControlChannel()
        self._running = False

    @property
    def channel(self) -> ControlChannel:
        return self._channel

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self, message: str = "user interrupt") -> None:
        """Hard-stop the in-flight goal — aborts the run (no-op when idle)."""
        self._channel.stop(message)

    def amend(self, message: str) -> None:
        """Steer the in-flight goal with a follow-up instruction, no abort.

        The note rides the channel until the running subagent's next
        between-step poll, where it enters the buffer as a USER turn. A no-op
        in practice when idle (nothing is draining), and harmless either way —
        ``reset`` drops undrained notes at the next ``run_goal``.
        """
        self._channel.amend(message)

    async def run_goal(
        self,
        goal: str,
        *,
        lifecycle: Optional[Lifecycle] = None,
    ) -> RunReport:
        """Run one goal to completion (or interrupt), firing lifecycle hooks.

        Resets the interrupt channel up front so a stale signal from a prior
        goal never aborts this one immediately.
        """
        sink = lifecycle or Lifecycle()
        self._channel.reset()
        self._running = True
        await _maybe_await(sink.on_goal_received(goal))
        try:
            report = await self._coordinator.handle_goal(
                goal,
                working_dir=self._working_dir,
                should_abort=self._channel.should_abort,
                on_node_done=sink.on_node_done,
            )
        finally:
            self._running = False

        await _maybe_await(sink.on_goal_complete(report))
        return report

    async def run_idle(
        self,
        next_goal: GoalSource,
        *,
        should_exit: Optional[ExitPredicate] = None,
        lifecycle: Optional[Lifecycle] = None,
        poll_interval: float = 0.2,
    ) -> None:
        """Serve goals until ``should_exit`` returns True.

        Each tick: check exit, pull the next goal from ``next_goal`` (the queue
        drain in production). ``None`` means the queue is empty — fire
        ``sink.on_idle_tick`` and sleep ``poll_interval``. A goal string is run
        via ``run_goal``; goals are handled strictly one at a time (no overlap),
        so a long run naturally backpressures the queue.
        """
        sink = lifecycle or Lifecycle()
        while True:
            if should_exit is not None and await _maybe_await(should_exit()):
                return
            goal = await _maybe_await(next_goal())
            if goal is None:
                await _maybe_await(sink.on_idle_tick())
                await asyncio.sleep(poll_interval)
                continue
            await self.run_goal(goal, lifecycle=sink)
