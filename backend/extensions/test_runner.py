"""Test-runner workflow node (report §9.3 — Phase 3 'run tests' piece).

The Phase 3 modify pipeline ends in *verify by execution*: after the patch
applies, run the tests; if they fail, route to a failure-diagnosis branch
(which can re-trigger repair); if they pass, route to END.

This module provides a deterministic ``TestRunnerNode`` that:

  * Takes an injected async runner ``(Blackboard) -> TestResult``. Production
    code wires shell-tool-backed pytest invocations / make commands / CI
    triggers; tests stub it to return scripted outcomes.
  * Routes ``"pass"`` or ``"fail"`` (NOT the runner's default ok/fail) so a
    downstream gate can connect to ``failure_diagnosis`` cleanly without
    needing to inspect ``NodeResult.ok``.
  * Stashes the test output and pass flag on the Blackboard at
    ``{name}.output`` and ``{name}.passed`` so the markdown report or a
    critic gate can quote them later.

Deliberately not exposed via the JSON draft: a planner-emitted shell command
is exactly the kind of thing the safety-by-construction principle forbids
(report §11.5). Test runners stay in hand-authored templates that wire the
runner closure directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..engine.blackboard import Blackboard
from ..orchestration.planning.workflow import Node, NodeResult


@dataclass(frozen=True)
class TestResult:
    """Outcome of one test invocation.

    ``passed`` is the routing-relevant bit; ``output`` is the raw stdout/
    stderr or summary text the runner produced (kept full for the report);
    ``summary`` is a one-line gist for the receptionist progress view;
    ``metadata`` is open-ended (test count, duration, command, etc.).
    """

    passed: bool
    output: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # The Test-prefix triggers pytest's class collection heuristic; tell it
    # to skip — this is a domain dataclass, not a unittest TestCase.
    __test__ = False


# An async function that runs the tests and returns a TestResult. Reading
# the Blackboard lets the runner pick command / scope from upstream nodes
# (e.g. a 'change' node could write the modified file paths into bb.state
# and the runner could pass them to pytest via -k).
TestExecutor = Callable[[Blackboard], Awaitable[TestResult]]


@dataclass
class TestRunnerNode:
    """Workflow node that runs a test command and routes pass / fail.

    No LLM. No subprocess control. The injected ``runner`` does that work;
    this node owns only the routing semantics and the Blackboard handoff.
    """

    name: str
    runner: TestExecutor

    # Same pytest-collection escape hatch as TestResult.
    __test__ = False

    async def run(self, bb: Blackboard) -> NodeResult:
        try:
            result = await self.runner(bb)
        except Exception as exc:
            # A runner crash is a hard fail; route 'fail' so a downstream
            # diagnosis / repair branch can engage the same way it would for
            # legitimately failing tests. The error rides along for the report.
            return NodeResult(
                ok=False, route="fail",
                error=f"{type(exc).__name__}: {exc!s}",
                summary=f"{self.name}: runner raised {type(exc).__name__}",
            )

        bb.state[f"{self.name}.output"] = result.output
        bb.state[f"{self.name}.passed"] = result.passed
        if result.metadata:
            bb.state[f"{self.name}.metadata"] = dict(result.metadata)

        label = "pass" if result.passed else "fail"
        return NodeResult(
            ok=result.passed,
            route=label,
            data={f"{self.name}.passed": result.passed},
            summary=f"{self.name}: {result.summary or label}",
        )
