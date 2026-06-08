"""Runnable end-to-end demo for the v2 backend.

    python -m backend.demo

What it does (default = fully deterministic, no LLM, no network):

    1. Builds the whole stack (Coordinator + Service + Lifecycle).
    2. Submits a small fleet of goals — one per representative pattern:
         - audit ─ "audit src/ for SQL injection"
         - modify ─ "fix the add() function in calc.py to return a + b"
         - novel  ─ "compose a release note from the latest commits"
              (router can't classify → planner produces a custom draft)
    3. Streams every event you'd want to watch a real workflow do:
         · router decision (which tier picked the pattern, with confidence)
         · planner output (the draft JSON the LLM emitted)
         · every node start with sub_goal & allowed tools
         · every tool call with arguments and (mock) output
         · every node end with ok / route / latency / tokens
         · final converged findings table
         · final Markdown report

Switches:

    --real-embedder    Use the LTM dense embedder (reads api.txt). Falls back
                       to no-embedder if the provider is unavailable. The
                       Router's tier-1 embedding match becomes real cosine
                       similarity instead of fail-safe.

    --persist          Use a Coordinator state file under
                       ``./.handq_demo_state/`` so detector traces and
                       _learned set survive across runs (try running with
                       this twice and watch the second run pick up where
                       the first left off).

Mock tools (read/grep/glob/shell/edit) return canned but realistic-looking
observations so the subagents can complete a multi-turn dialog. The
"LLMClient" is a scripted ScenarioLLM that knows what each node's subagent
is supposed to do — entirely deterministic, runnable on any machine.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from backend.agent.contracts import Message, MessageRole, ToolCall, ToolResult, ToolSpec
from backend.agent.executor import SubagentExecutor
from backend.agent.llm import LLMResponse
from backend.config import BackendConfig
from backend.coordinator import Coordinator
from backend.engine.findings import converge
from backend.orchestration.exemplar_builder import ExemplarBuilder
from backend.orchestration.exemplars import ExemplarStore
from backend.orchestration.planner import WorkflowPlanner
from backend.orchestration.report import render_report
from backend.orchestration.router import LongTermMemoryEmbedder, Router
from backend.service import ControlChannel, Lifecycle, OrchestrationService
from backend.tests._mock_tools import (
    MockToolStep,
    ScenarioLLM,
    asst_calls_tool,
    asst_text,
    mock_tool,
)


# ── Pretty printing ─────────────────────────────────────────────────────────

_INDENT = "    "


def banner(title: str, char: str = "═") -> None:
    line = char * 78
    print(f"\n{line}\n  {title}\n{line}", flush=True)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * (74 - len(title)), flush=True)


def kv(label: str, value: Any, indent: int = 1) -> None:
    print(f"{_INDENT * indent}{label}: {value}", flush=True)


def listing(items, indent: int = 1) -> None:
    for it in items:
        print(f"{_INDENT * indent}• {it}", flush=True)


def code_block(text: str, lang: str = "", indent: int = 1) -> None:
    pad = _INDENT * indent
    for line in text.splitlines() or [""]:
        print(f"{pad}│ {line}", flush=True)


# ── Mock tool registry ──────────────────────────────────────────────────────


def build_logged_tools(log) -> dict[str, ToolSpec]:
    """The mock tool surface every subagent in this demo can draw from.

    Each tool's runner returns a canned-but-realistic output and emits a
    log line so the demo can show the call. Real tool implementations
    plug in here for production.
    """
    def wrap_tool(name: str, *, concurrency_safe=False, mutating=False,
                  responder) -> ToolSpec:
        async def runner(**kwargs):
            log("tool", f"{name}({_args(kwargs)})")
            step = responder(kwargs)
            await asyncio.sleep(step.delay_s) if step.delay_s else None
            result = ToolResult(call_id="", ok=step.ok,
                                output=step.output,
                                error=step.error if not step.ok else None,
                                metadata=step.metadata)
            log("tool_out", f"{name} → ok={step.ok} {_short(step.output)}")
            return result
        return ToolSpec(
            name=name, description=f"mock {name}", parameters={"type": "object"},
            run=runner, concurrency_safe=concurrency_safe, mutating=mutating,
        )

    return {
        "glob": wrap_tool("glob", concurrency_safe=True, responder=_glob_responder),
        "grep": wrap_tool("grep", concurrency_safe=True, responder=_grep_responder),
        "read": wrap_tool("read", concurrency_safe=True, responder=_read_responder),
        "shell": wrap_tool("shell", responder=_shell_responder),
        "edit": wrap_tool("edit", mutating=True, responder=_edit_responder),
    }


def _args(kwargs: dict) -> str:
    bits = []
    for k, v in kwargs.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        bits.append(f"{k}={s}")
    return ", ".join(bits)


def _short(value, n: int = 60) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


def _glob_responder(kwargs):
    pattern = kwargs.get("pattern", "**")
    if "*.py" in pattern or "py" in pattern:
        return MockToolStep(output={"files": ["src/db/query.py", "src/api/users.py", "calc.py"]})
    return MockToolStep(output={"files": ["src/db/query.py"]})


def _grep_responder(kwargs):
    pattern = kwargs.get("pattern", "")
    if "return a" in pattern:
        return MockToolStep(output={"matches": [{"file": "calc.py", "line": 4,
                                                   "text": "    return a - b"}]})
    if "SQL" in pattern.upper() or "query" in pattern.lower():
        return MockToolStep(output={"matches": [
            {"file": "src/db/query.py", "line": 45, "text": "f\"SELECT ... {user_id}\""},
            {"file": "src/api/users.py", "line": 88, "text": "query = f\"...{name}...\""},
        ]})
    return MockToolStep(output={"matches": []})


def _read_responder(kwargs):
    path = kwargs.get("path", "")
    if "calc.py" in path:
        return MockToolStep(output="def add(a, b):\n    # BUG: subtracts\n    return a - b\n")
    if "query.py" in path:
        return MockToolStep(output='def get_user(user_id):\n    sql = f"SELECT * FROM users WHERE id={user_id}"\n    return db.exec(sql)\n')
    if "users.py" in path:
        return MockToolStep(output='def list_users(name):\n    query = f"SELECT * FROM users WHERE name=\\"{name}\\""\n    return db.exec(query)\n')
    return MockToolStep(output=f"(mock contents of {path})")


def _shell_responder(kwargs):
    cmd = kwargs.get("command", "")
    if "pytest" in cmd or "test" in cmd:
        return MockToolStep(output="2 passed in 0.04s",
                            metadata={"exit_code": 0})
    return MockToolStep(output=f"$ {cmd}\n(mock stdout)")


def _edit_responder(kwargs):
    path = kwargs.get("path", "")
    return MockToolStep(output=f"patched {path}")


# ── Scripted LLMs ───────────────────────────────────────────────────────────


def planner_llm_for_demo():
    """Returns a JSON draft matching whatever goal we throw at it.

    Production replaces this with a real LLM client (Anthropic / QGenie /
    custom). The draft shape is what the planner expects to receive.
    """
    async def llm(prompt: str) -> str:
        # Inspect the user-side of the prompt to decide which draft to emit.
        goal_marker = "Goal:\n"
        idx = prompt.find(goal_marker)
        goal = prompt[idx + len(goal_marker):].split("\n\n", 1)[0].strip() if idx >= 0 else ""
        # We only HAVE the planner path for goals the router can't classify
        # (FREEFORM). Still, give the LLM a crack at producing a useful draft
        # for arbitrary text.
        return json.dumps({
            "entry": "discover",
            "nodes": [
                {"name": "discover", "type": "agent",
                 "sub_goal": f"Discover what's relevant for: {goal}"},
                {"name": "synthesize", "type": "agent",
                 "sub_goal": f"Synthesize a concise summary for: {goal}",
                 "context_keys": ["discover"]},
            ],
            "edges": {"discover": {"*": "synthesize"}, "synthesize": {"*": "END"}},
        })
    return llm


def subagent_llm_for_demo() -> ScenarioLLM:
    """The brain of every AgentNode in this demo.

    Each entry in ``scripts`` is a goal-substring → list of canned ASSISTANT
    messages, popped FIFO across the subagent's turns. The first un-matched
    goal falls through to the catch-all "(no script)" message which converges
    immediately — useful so a stray sub_goal doesn't hang the demo.
    """
    audit_db = json.dumps({"findings": [{
        "category": "sql_injection", "severity": "critical",
        "location": "src/db/query.py:45",
        "summary": "f-string SQL with raw user_id",
        "evidence": "f\"... WHERE id={user_id}\"",
        "recommendation": "use parameterized query (db.exec(sql, (user_id,)))",
        "source": "audit_db", "confidence": 0.95,
    }]})
    audit_api = json.dumps({"findings": [{
        "category": "sql_injection", "severity": "high",
        "location": "src/api/users.py:88",
        "summary": "f-string SQL with raw name",
        "evidence": "query = f\"...WHERE name=...{name}\"",
        "recommendation": "use parameterized query",
        "source": "audit_api", "confidence": 0.85,
    }]})

    return ScenarioLLM(scripts={
        # ── audit pattern (template) — three scanners running in parallel ──
        # validators.py templates the sub_goals with these exact prefixes:
        "Review the prior work for": [
            asst_calls_tool(content="locating risky files", calls=[
                ("c1", "glob", {"pattern": "**/*.py"}),
                ("c2", "grep", {"pattern": "SELECT.*FROM"}),
            ]),
            asst_text(audit_db),
        ],
        "Adversarially audit the prior work": [
            asst_calls_tool(content="adversarial sweep", calls=[
                ("c1", "grep", {"pattern": "f\"SELECT|f\"INSERT"}),
            ]),
            asst_text(audit_api),
        ],
        "Critique whether the work actually satisfies": [
            asst_text(json.dumps({"findings": []})),
        ],

        # ── modify pattern (template) ──
        "Locate the code relevant": [
            asst_calls_tool(content="globbing", calls=[
                ("c1", "glob", {"pattern": "**/*.py"}),
                ("c2", "grep", {"pattern": "def add"}),
            ]),
            asst_text("calc.py defines add() at line 1; the tests live in tests/test_calc.py"),
        ],
        "Understand how the located code": [
            asst_calls_tool(content="reading the suspect file", calls=[
                ("c1", "read", {"path": "calc.py"}),
            ]),
            asst_text("add() currently does `return a - b` — clearly a bug; the docstring says it should return the sum"),
        ],
        "Make the change": [
            asst_calls_tool(content="applying the fix", calls=[
                ("c1", "edit", {"path": "calc.py", "diff": "return a - b -> return a + b"}),
            ]),
            asst_text("patched calc.py: add() now returns a + b"),
        ],
        "Verify the change": [
            asst_calls_tool(content="running tests", calls=[
                ("c1", "shell", {"command": "pytest tests/test_calc.py -q"}),
            ]),
            asst_text("tests pass: 2 passed in 0.04s"),
        ],

        # ── planner-pattern generic discover/synthesize ──
        "Discover what's relevant": [
            asst_calls_tool(content="discovery", calls=[
                ("c1", "shell", {"command": "git log --oneline -5"}),
            ]),
            asst_text("Recent commits: a) added /v2/users endpoint, b) bumped postgres driver, c) deprecated /v1 auth"),
        ],
        "Synthesize a concise summary": [
            asst_text("# Release Note\n- New /v2/users API\n- Postgres driver upgrade\n- /v1 auth deprecated"),
        ],
    })


# ── Lifecycle sink that streams everything to stdout ────────────────────────


class TraceSink(Lifecycle):
    """A Lifecycle that prints every event with timing as it happens."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def _ts(self) -> str:
        return f"[{(time.monotonic() - self.t0) * 1000:7.1f}ms]"

    def on_goal_received(self, goal: str) -> None:
        section(f"goal received {self._ts()}")
        kv("goal", goal)

    def on_node_done(self, name: str, result) -> None:
        ok_marker = "ok " if result.ok else "FAIL"
        kv(f"{ok_marker} {name}",
           f"route={result.label!r} summary={_short(result.summary, 80)}")
        if result.findings:
            for f in result.findings:
                listing([f"finding[{f.severity}] {f.category} @ {f.location}: "
                         f"{_short(f.summary, 60)}"], indent=2)

    def on_goal_complete(self, report) -> None:
        section(f"goal complete {self._ts()}")
        kv("ok", report.ok)
        kv("steps", report.steps)
        kv("pattern", report.pattern)
        kv("last_summary", _short(report.last_summary, 100))


# ── Lifecycle sink + tool log in one place (so tool calls interleave w/ nodes) ──


class StreamingSink(TraceSink):
    """Adds a printer for tool events that the wrapped tools call into."""

    def log(self, kind: str, msg: str) -> None:
        prefix = "  ↳ tool   " if kind == "tool" else "  ↳ result "
        print(f"{prefix}{self._ts()} {msg}", flush=True)


# ── Entry ───────────────────────────────────────────────────────────────────


async def _classifier_for_demo(goal: str) -> Optional[str]:
    """Tiny rule-based classifier — stands in for the cheap LLM tier.

    Production replaces this with one Haiku-class call. Demo uses keyword
    rules so we get a deterministic Tier-2 hit when Tier-1 (embedding)
    can't get to threshold.
    """
    g = goal.lower()
    if "audit" in g or "review" in g or "vulnerabilit" in g:
        return "audit"
    if "fix" in g or "modify" in g or "refactor" in g or "rename" in g:
        return "modify"
    return None


def build_router(*, real_embedder: bool, exemplar_store: ExemplarStore = None) -> tuple[Router, "Embedder | None"]:
    embedder = None
    if real_embedder:
        try:
            from src.infrastructure.long_term_memory.embedding import from_config
            api_path = Path("api.txt")
            api_key = api_path.read_text(encoding="utf-8").strip() if api_path.exists() else ""
            provider = from_config({"llm": {"API_KEY": api_key}})
            if getattr(provider, "available", False):
                embedder = LongTermMemoryEmbedder(provider)
                print("  [router] LTM embedder available — Tier-1 embedding match enabled.")
            else:
                print("  [router] LTM embedder NOT available (FTS-only / no API key) — falling back to classifier tier.")
        except Exception as exc:
            print(f"  [router] could not build LTM embedder: {exc!r} — falling back.")
    return Router(
        embedder=embedder,
        classifier=_classifier_for_demo,
        exemplar_store=exemplar_store,
    ), embedder


async def run_demo(*, real_embedder: bool, persist: bool) -> None:
    banner("HandQ v2 backend — end-to-end demo")
    print("Mode: deterministic (scripted LLM + mock tools); "
          f"real_embedder={real_embedder} persist={persist}")

    sink = StreamingSink()

    section("build stack")
    state_dir = Path(".handq_demo_state")
    coord_state_path = state_dir / "coord_state.json" if persist else None
    exemplar_path = state_dir / "exemplars.json" if persist else None

    # ExemplarStore is shared between Router (read) + ExemplarBuilder (write).
    exemplar_store = ExemplarStore(exemplar_path)
    if persist and exemplar_path and exemplar_path.exists():
        kv("loaded user exemplars", sum(exemplar_store.user_count(s)
                                          for s in exemplar_store.all_pattern_ids()))
        kv("loaded auto exemplars", sum(exemplar_store.auto_count(s)
                                          for s in exemplar_store.all_pattern_ids()))

    router, embedder = build_router(real_embedder=real_embedder, exemplar_store=exemplar_store)

    tools = build_logged_tools(sink.log)
    kv("tools", ", ".join(sorted(tools)))

    subagent_llm = subagent_llm_for_demo()
    # The channel carries stop + amend from a frontend to the running subagents;
    # build it first so the executor can drain mid-node amendments off it (a real
    # frontend wires it the same way — this demo's scripted run never amends).
    channel = ControlChannel()
    executor = SubagentExecutor(
        llm=subagent_llm, tools=tools, max_steps=8,
        check_interrupt=channel.check_stop,
        drain_amendments=channel.drain_amendments,
    )
    planner = WorkflowPlanner(planner_llm_for_demo())

    # ExemplarBuilder grows the Tier-1 pool from successful Tier-2 runs.
    # Needs a real embedder to do cosine dedup; without one, we skip the
    # auto-promotion feedback loop entirely.
    exemplar_builder = None
    if embedder is not None:
        exemplar_builder = ExemplarBuilder(
            exemplar_store, embedder, dedup_threshold=0.95, max_auto_per_pattern=50,
        )
        kv("auto-exemplar feedback", "enabled (real embedder)")
    else:
        kv("auto-exemplar feedback", "disabled (no embedder; pass --real-embedder)")

    coord = Coordinator(
        executor=executor, router=router, config=BackendConfig(),
        planner=planner, state_path=coord_state_path,
        exemplar_builder=exemplar_builder,
    )
    service = OrchestrationService(
        coord, working_dir=".", channel=channel,
    )
    kv("planner", "enabled (scripted)")
    kv("coord state", str(coord_state_path) if coord_state_path else "—")
    kv("exemplar store", str(exemplar_path) if exemplar_path else "—")
    if persist and coord_state_path and coord_state_path.exists():
        kv("loaded learned patterns", sorted(coord.learned) or "(none)")
        kv("loaded detector traces", coord.detector.total_traces())

    goals = [
        "audit src/ for SQL injection vulnerabilities",
        "fix the add function in calc.py to return a + b",
        "compose a release note from the latest commits",
    ]

    for i, goal in enumerate(goals, start=1):
        banner(f"GOAL {i}/{len(goals)}: {goal}", char="═")

        section("router classify")
        decision = await router.classify(goal)
        kv("pattern_id", decision.pattern_id)
        kv("confidence", f"{decision.confidence:.3f}")
        kv("method", decision.method)

        section("resolve workflow")
        # Peek what coord will pick (we replicate _resolve_workflow's logic
        # but only for inspection — handle_goal will do the real work).
        if decision.pattern_id in {"modify", "audit"}:
            kv("path", "hand-authored template")
        elif decision.pattern_id in coord.learned:
            kv("path", f"learned draft ({decision.pattern_id})")
        elif decision.pattern_id == "freeform":
            kv("path", "planner → draft (LLM call)")
        else:
            kv("path", "single-loop fallback")

        section("run")
        report = await service.run_goal(goal, lifecycle=sink)

        section("converged findings")
        ranked = converge([report.blackboard.findings])
        if not ranked:
            print("    (none)")
        else:
            for f in ranked:
                print(f"    {f.severity.upper():<8} {f.category:<20} {f.location:<28} "
                      f"conf={f.confidence:.2f}  {_short(f.summary, 50)}")

        section("Markdown report")
        md = render_report(trace=report.trace, findings=ranked,
                           summaries=report.blackboard.summaries)
        code_block(md, indent=1)

    if persist:
        section("persistence summary")
        kv("learned patterns (memorized)", sorted(coord.learned) or "(none)")
        kv("detector traces", coord.detector.total_traces())
        kv("state file", str(coord_state_path))
        # Exemplar growth — interesting only if we had a real embedder.
        auto_total = sum(exemplar_store.auto_count(s) for s in exemplar_store.all_pattern_ids())
        kv("auto exemplars (Tier-1 pool growth)", auto_total)
        kv("exemplar file", str(exemplar_path))
        print("\n(Try running with --persist again — second run picks up traces "
              "+ auto exemplars; the routing tier may shift from classifier→embedding.)")

    banner("DONE")


def main() -> None:
    p = argparse.ArgumentParser(description="HandQ v2 backend end-to-end demo")
    p.add_argument("--real-embedder", action="store_true",
                   help="use the LTM dense embedder for Tier-1 routing")
    p.add_argument("--persist", action="store_true",
                   help="persist Coordinator state under .handq_demo_state/")
    args = p.parse_args()

    # Force UTF-8 on Windows so our box-drawing characters don't crash cp1252.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        asyncio.run(run_demo(real_embedder=args.real_embedder, persist=args.persist))
    except KeyboardInterrupt:
        print("\n(interrupted)")


if __name__ == "__main__":
    main()
