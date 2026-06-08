"""Coordinator — top-level intake.

The flow:

    goal ─► Router.classify ─► template / learned draft / planner / single loop
                                    └────────────┬────────────┘
                                                 ▼
                                      WorkflowRunner walks it
                                                 │
                                                 ▼
                                      record Trace → detector

There is NO per-step planner brain: branching during a run is the runner's
code-routing; re-planning happens only via a template's fail edges.

**State persistence**: the Coordinator owns two pieces of cross-goal state
that should survive a process restart — the ``_learned`` set (which patterns
the detector has learned into runnable draft for) and the detector's own trace
log (which feeds the next promotion). Pass ``state_path`` to make these
durable; the Coordinator auto-saves after every ``handle_goal`` /
``promote_ready_candidates``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .config import BackendConfig
from .engine.blackboard import Blackboard
from .engine.budget import BudgetManager
from .engine.executor import ExecutorProtocol
from .engine.observability import TraceRecorder, TraceStore
from .orchestration.routing.detector import TemplateDetector, Trace
from .orchestration.routing.exemplar_builder import ExemplarBuilder
from .orchestration.planning.planner import WorkflowPlanner
from .orchestration.routing.router import METHOD_CLASSIFIER, Router
from .orchestration.execution.runner import RunReport, WorkflowRunner
from .orchestration.routing.patterns import FREEFORM
from .orchestration.planning.templates import TemplateLoader
from .orchestration.planning.workflow import END, AgentNode, Workflow


# Default template directory — JSON files under orchestration/planning/templates/.
_DEFAULT_TEMPLATE_DIR = Path(__file__).parent / "orchestration" / "planning" / "templates"


def single_loop_workflow(goal: str, executor: ExecutorProtocol) -> Workflow:
    """The universal fallback: one bare AgentNode, full self-planning, done.

    For anything not a recognized recurring pattern we run a single loop — we do
    NOT synthesize a one-off DAG.
    """
    node = AgentNode(name="agent", sub_goal=goal, executor=executor)
    return Workflow(nodes={node.name: node}, edges={"agent": {"*": END}}, entry="agent")


class Coordinator:
    def __init__(
        self,
        *,
        executor: ExecutorProtocol,
        router: Router,
        config: Optional[BackendConfig] = None,
        detector: Optional[TemplateDetector] = None,
        trace_store: Optional[TraceStore] = None,
        planner: Optional[WorkflowPlanner] = None,
        state_path: Optional[Path | str] = None,
        exemplar_builder: Optional[ExemplarBuilder] = None,
        template_loader: Optional[TemplateLoader] = None,
    ) -> None:
        self._executor = executor
        self._router = router
        self._config = config or BackendConfig()
        self._detector = detector or TemplateDetector()
        self._template_loader = template_loader or TemplateLoader(_DEFAULT_TEMPLATE_DIR)
        # Optional persistence sink for the structured trace. When set, every
        # finished run's RunTrace is written as JSON for audit / progress.
        self._trace_store = trace_store
        # Patterns the detector has learned: recognized recurring skeletons with
        # no hand-authored template. We build these per-goal from the restricted
        # draft — data, not code — so the proactivity loop closes:
        # traces → promote → subsequent goals of that pattern run the learned DAG.
        self._learned: set[str] = set()
        # Optional dynamic-workflow planner. When set, a goal the router can't
        # classify into a known pattern gets one planning attempt before falling
        # back to the single-loop default.
        self._planner = planner
        # Cross-restart durable state. Single JSON file holding {_learned,
        # detector traces}. Auto-loaded on construct, auto-saved after every
        # state-changing operation. None ⇒ in-memory only (tests, throwaway demo).
        self._state_path: Optional[Path] = Path(state_path) if state_path else None
        if self._state_path is not None and self._state_path.exists():
            self._load_state()
        # Optional auto-exemplar builder: a successful Tier-2 (classifier)
        # routed run becomes a Tier-1 exemplar candidate, growing the
        # Router's embedding pool over time. Wired here so Coordinator can
        # call it from handle_goal's tail; pass None to disable.
        self._exemplar_builder = exemplar_builder

    def _load_state(self) -> None:
        """Restore ``_learned`` + detector traces from ``_state_path``."""
        try:
            doc = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt or missing — start cold rather than crash. The next save
            # will overwrite the bad file.
            return
        self._learned = set(doc.get("learned") or [])
        det_doc = doc.get("detector")
        if det_doc:
            # Replace the detector contents but keep the construction-time
            # thresholds (those are deployment policy, not durable state).
            restored = TemplateDetector.from_dict(
                det_doc,
                min_occurrences=self._detector._min_occurrences,
                min_success_rate=self._detector._min_success_rate,
            )
            self._detector._traces = restored._traces

    def _save_state(self) -> None:
        """Persist ``_learned`` + detector traces. No-op without ``state_path``."""
        if self._state_path is None:
            return
        doc: dict[str, Any] = {
            "learned": sorted(self._learned),
            "detector": self._detector.to_dict(),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def promote_ready_candidates(self) -> list[str]:
        """Promote every detector candidate lacking a hand-authored template.

        Returns the pattern ids newly learned this call. Deliberately explicit
        (not auto-run mid-``handle_goal``) so promotion is an observable step the
        receptionist/operator can gate."""
        newly: list[str] = []
        for cand in self._detector.candidates():
            if self._template_loader.has(cand.pattern_id) or cand.pattern_id in self._learned:
                continue
            # Sanity check: the pattern's skeleton must be non-empty so a real
            # draft can be built later. ``goal=""`` is fine — we discard the draft.
            self._detector.build_draft(cand.pattern_id, goal="")
            self._learned.add(cand.pattern_id)
            newly.append(cand.pattern_id)
        if newly:
            self._save_state()
        return newly

    def _build_workflow(self, goal: str, pattern_id: str) -> tuple[Workflow, str]:
        """Pick a workflow for the pattern, cheapest-known-good first.

        1. JSON template via the loader (builtin + user-added),
        2. a detector-learned pattern, built per-goal from the restricted draft,
        3. else the universal single-loop fall-safe.

        Sync path — used as the fallback inside ``_resolve_workflow`` when no
        planner is set or the planner refused the goal.
        """
        if self._template_loader.has(pattern_id):
            return self._template_loader.build(pattern_id, goal=goal, executor=self._executor), pattern_id
        if pattern_id in self._learned:
            from .orchestration.planning import dag_draft
            spec = self._detector.build_draft(pattern_id, goal=goal)
            return dag_draft.build(spec, executor=self._executor), pattern_id
        return single_loop_workflow(goal, self._executor), "freeform"

    async def _resolve_workflow(
        self, goal: str, pattern_id: str,
    ) -> tuple[Workflow, str]:
        """Async wrapper around ``_build_workflow`` that adds the planner gate.

        Order: known template → learned draft → **dynamic planner** → single loop.
        The planner only fires when the router *gave up* (``pattern_id == FREEFORM``)
        AND a planner is wired AND its output validates. A recognized-but-unbuilt
        pattern (router matched a known pattern, no template, not yet learned) keeps
        running single-loop and accumulating traces under its real id — that's
        what feeds the detector toward promotion. Sending those goals to the
        planner instead would short-circuit the proactivity loop and forever
        spend an LLM round-trip on patterns that should have grown into a template.
        Any planner failure (parse / draft rejection / size cap / path-to-END)
        silently falls through to the single-loop default — so the planner
        never makes the run worse.
        """
        if self._template_loader.has(pattern_id) or pattern_id in self._learned:
            return self._build_workflow(goal, pattern_id)
        if pattern_id == FREEFORM and self._planner is not None:
            result = await self._planner.plan(goal)
            if result.ok and result.draft is not None:
                from .orchestration.planning import dag_draft
                return dag_draft.build(result.draft, executor=self._executor), "planned"
        return self._build_workflow(goal, pattern_id)

    def _make_runner(
        self, *, should_abort=None, on_node_done=None
    ) -> WorkflowRunner:
        """Build a runner with the cross-cutting concerns wired so a fresh run
        is always set up the same way."""
        return WorkflowRunner(
            max_steps=self._config.runner.max_steps,
            on_node_done=on_node_done,
            should_abort=should_abort,
            budget=BudgetManager(self._config.budget),
            observer=TraceRecorder(),
        )

    async def handle_goal(
        self,
        goal: str,
        *,
        working_dir: Optional[str] = None,
        should_abort=None,
        on_node_done=None,
    ) -> RunReport:
        decision = await self._router.classify(goal)
        wf, resolved_pattern = await self._resolve_workflow(goal, decision.pattern_id)

        bb = Blackboard(goal=goal, working_dir=working_dir)
        runner = self._make_runner(
            should_abort=should_abort, on_node_done=on_node_done,
        )
        report = await runner.run(wf, bb)
        # Stamp the resolved pattern for observability (which graph actually ran).
        report.pattern = resolved_pattern

        # Feed the proactivity loop: the detector mines the *router's* recognized
        # pattern (decision.pattern_id), NOT the resolved workflow pattern. A recurring
        # pattern with no template still ran as a single loop, but it must
        # accumulate under its real id — otherwise it could never clear the
        # promotion threshold and grow a template. FREEFORM (router couldn't
        # classify) is excluded by the detector itself.
        self._detector.record(
            Trace(pattern_id=decision.pattern_id, goal=goal, ok=report.ok, steps=report.steps)
        )
        # Persist the structured trace for audit / progress views when a sink is set.
        if self._trace_store is not None and report.trace is not None:
            self._trace_store.save(report.trace)
        # Cross-restart state save (no-op without ``state_path``).
        self._save_state()
        # Auto-exemplar feedback: a Tier-2 (classifier) routed run that
        # cleanly succeeded becomes a candidate for Tier-1 promotion. The
        # builder cosine-dedups + caps + persists; failures are silent.
        # Skip on partial runs (their goals haven't really "succeeded" at this
        # pattern) and on FREEFORM (no pattern to anchor on).
        if (
            self._exemplar_builder is not None
            and decision.method == METHOD_CLASSIFIER
            and decision.pattern_id != FREEFORM
            and report.ok
            and not report.partial
        ):
            await self._exemplar_builder.consider(
                decision.pattern_id, goal, source=f"trace:{decision.pattern_id}",
            )
        return report

    @property
    def detector(self) -> TemplateDetector:
        return self._detector

    @property
    def learned(self) -> set[str]:
        """Snapshot of pattern ids the detector has learned (for inspection)."""
        return set(self._learned)
