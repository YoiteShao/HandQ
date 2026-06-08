"""LLM Workflow Planner — model produces a ``DAGDraft`` from a goal.

This module closes the central gap the report calls out as the *defining*
capability of dynamic workflow orchestration (§3.2 / §11.2): the model emits
a task-specific JSON harness, our restricted draft validates it, and the runtime
walks the resulting graph. The planner is *gated* by the Router so trivial
work never pays the planner tax:

  Router decision tree
    ├─ recognized pattern  → hand-authored / promoted template (no planner)
    ├─ trivial / freeform → single-loop default                 (no planner)
    └─ NOVEL COMPLEX     → planner produces a draft           (this module)

**Safety** (§8.1 / §11.5): the planner is just an LLM client adapter. The
output is JSON text; ``DAGDraft.from_dict`` then enforces the whitelist
builder — agent nodes are fully data-described, every other node references
a *registered factory by name*. There is no eval, no import, no path by
which a malformed plan can run arbitrary code. A planner that returns
unparseable / over-sized / unreachable draft is rejected with a deterministic
``PlanResult(ok=False)`` and the caller falls back to the single-loop
default. The fail-safe is therefore unconditional: a wrong plan degrades
to a slower-but-correct execution, never to a broken one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .dag_draft import DAGDraft, DAGDraftError
from .workflow import END

# A planner LLM is just an async ``(prompt: str) -> str`` callable. The
# concrete client (HTTP, streaming, retries) lives outside this module so
# tests can swap in a deterministic stub.
PlannerLLM = Callable[[str], Awaitable[str]]


PLANNER_SYSTEM_PROMPT = """\
You are a workflow planner. Given a user goal, you produce a JSON workflow plan
that a deterministic runtime will execute. You do NOT execute the goal yourself.

The runtime walks a DAG of nodes and routes between them based on edge labels.

Two node *categories* are allowed:

  agent : a scope-bounded subagent that runs a single self-planning loop.
          Required:  name, type="agent", sub_goal
          Optional:  tools (list[str]), context_keys (list[str], names of
                     prior agent nodes whose output this node may read)

  builder nodes (whitelisted, by name):
    verdict_gate   — gates ok / repair / give_up paths after a verifier.
                     params: attempt_key, max_attempts, pass_label,
                             repair_label, give_up_label.
    convergence    — merges parallel children's findings, dedups, ranks.
    parallel_group — fans out concurrent children, then joins. ``children`` is
                     a list of agent-node names declared elsewhere in the draft.
                     ``params.join`` is one of "all_ok"/"any_ok"/"majority_ok".
                     Children are internal to the group — they MUST NOT appear
                     as edge targets at the top level (only the group itself).

Edges map node_name -> {label: target}. The reserved target "END" terminates
the walk. Use "*" to match any label. The graph must be acyclic at the
data level — bounded retries are expressed via verdict_gate's repair edge,
not raw cycles.

Output ONLY a JSON object with these top-level keys:

  entry  : string — the starting node's name
  nodes  : array of node specs
  edges  : object — {node_name: {label: target}}

Do not include any prose, comments, or markdown fences. Just the JSON.

Plan principles (per the report §3 / §11):
  1. Decompose the goal into discovery → analysis → convergence → reporting
     phases when the task naturally splits.
  2. Use parallel children only when the work is genuinely independent.
  3. Always converge and validate — concurrency is for coverage; convergence
     is the goal.
  4. Default to read-only. Only propose write/edit when the goal asks for
     code modification AND the plan includes a verification phase.
  5. Bound the graph: keep total nodes small. If the task needs more,
     propose a first slice with a final 'plan_more' node so the runtime
     can re-enter the planner with the partial result.
"""


PLANNER_USER_TEMPLATE = """\
Goal:
{goal}

{context}

Produce the workflow plan now.
"""


@dataclass
class PlannerConfig:
    """Bounds the planner output so a runaway plan can't smother the runtime."""

    max_nodes: int = 8
    max_edges_per_node: int = 6
    require_path_to_end: bool = True


@dataclass
class PlanResult:
    """Outcome of a planner attempt.

    ``ok=True``  ⇒ ``draft`` is a validated ``DAGDraft`` ready to hand to ``build()``.
    ``ok=False`` ⇒ ``error`` explains why (parse / validate / size / network);
    the caller falls back to the single-loop default. ``raw`` carries the
    untouched LLM output for debugging — a parser failure usually points to
    a fixable system-prompt issue.
    """

    ok: bool
    draft: Optional[DAGDraft] = None
    error: Optional[str] = None
    raw: Optional[str] = None


# Crude but effective: extract the first JSON object from a wrapped response.
# LLMs love to wrap in ```json fences or pad with apologies; this regex grabs
# the outermost {...} block. Strict parsing in a second pass.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class WorkflowPlanner:
    """Produces a ``DAGDraft`` from a goal via an injected LLM.

    Dependency-injects the LLM (a ``PlannerLLM`` callable) so production code
    plugs in a real client and tests pass a deterministic stub. The planner
    owns prompt formatting, output extraction, and draft validation; everything
    else (HTTP, retries, streaming) is the LLM client's problem.
    """

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        config: Optional[PlannerConfig] = None,
        system_prompt: str = PLANNER_SYSTEM_PROMPT,
    ) -> None:
        self._llm = llm
        self._config = config or PlannerConfig()
        self._system_prompt = system_prompt

    async def plan(self, goal: str, *, context: str = "") -> PlanResult:
        """Run one planner attempt and return a ``PlanResult``.

        Failures never raise — they return ``ok=False``. This makes the
        Coordinator's gating logic unconditional ("if not result.ok: fall
        back to single-loop") and keeps the planner from poisoning a goal
        the runtime could perfectly well finish without it.
        """
        prompt = self._format_prompt(goal, context)
        try:
            raw = await self._llm(prompt)
        except Exception as exc:  # pragma: no cover - defensive
            return PlanResult(ok=False, error=f"planner LLM failed: {exc!s}")
        return self._parse(raw)

    def _format_prompt(self, goal: str, context: str) -> str:
        body = PLANNER_USER_TEMPLATE.format(goal=goal, context=context or "")
        return f"{self._system_prompt}\n\n{body}"

    def _parse(self, raw: str) -> PlanResult:
        text = self._strip_wrapping(raw)
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            return PlanResult(ok=False, error=f"invalid JSON: {exc.msg}", raw=raw)
        try:
            draft = DAGDraft.from_dict(doc)
        except DAGDraftError as exc:
            return PlanResult(ok=False, error=f"draft rejected: {exc!s}", raw=raw)

        cfg = self._config
        if len(draft.nodes) > cfg.max_nodes:
            return PlanResult(
                ok=False,
                error=f"plan has {len(draft.nodes)} nodes, max {cfg.max_nodes}",
                raw=raw,
            )
        for src, table in draft.edges.items():
            if len(table) > cfg.max_edges_per_node:
                return PlanResult(
                    ok=False,
                    error=f"node {src!r} has {len(table)} edges, max {cfg.max_edges_per_node}",
                    raw=raw,
                )
        if cfg.require_path_to_end and not _reaches_end(draft):
            return PlanResult(ok=False, error="plan has no path to END", raw=raw)

        return PlanResult(ok=True, draft=draft, raw=raw)

    @staticmethod
    def _strip_wrapping(raw: str) -> str:
        """Pull the first JSON object out of an LLM response.

        Three layers, fall-through:
          1. A ```json … ``` fence — extract its body.
          2. Any markdown fence — extract its body.
          3. Failing both, the outermost {...} substring.
        Trailing apologies / preludes are tolerated; truly broken output
        falls through to ``json.loads`` which raises a parse error.
        """
        text = raw.strip()
        m = _FENCE_RE.search(text)
        if m:
            return m.group(1).strip()
        m = _OBJECT_RE.search(text)
        if m:
            return m.group(0)
        return text


def _reaches_end(draft: DAGDraft) -> bool:
    """BFS from ``draft.entry`` over labeled edges; success means END is reachable.

    The draft validator already checks targets resolve, so a "no path to END"
    plan looks structurally fine — but it would loop the runtime forever.
    Catching it at planner time turns the failure into a rejected plan + a
    fallback, instead of a runaway run that has to hit ``max_steps``.
    """
    seen: set[str] = set()
    frontier: list[str] = [draft.entry]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for dst in draft.edges.get(cur, {}).values():
            if dst == END:
                return True
            if dst not in seen:
                frontier.append(dst)
    return False
