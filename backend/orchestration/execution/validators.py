"""Validation gallery + convergence (report §3.6, §11.4).

The report makes validation the main load-bearing mechanism of *convergence*:
a robust workflow isn't "execute and hope", it's "execute → review → judge →
maybe repair → judge again, bounded". v1 had this only inline inside the modify
template. This module extracts the reusable pieces:

* **validator_node(role, …)** — an ``AgentNode`` specialized by role
  (Reviewer / Adversarial / Critic / Test / Synthesis). Each role is just a
  prompt + tool-scope preset over the same single-loop executor; no new machinery.
* **verdict_gate(…)** — the generalized "pass / repair / give_up" router with a
  bounded repair counter on the Blackboard (was hand-inlined in modify).
* **convergence_node(…)** — a deterministic ``FuncNode`` that folds the
  Blackboard's structured findings through merge→dedup→rank (findings.converge).

These compose with the existing workflow primitives — a validation stage is
``validator_node → verdict_gate`` with the gate's ``repair`` edge pointing back
at the work node, exactly like modify's verify loop.
"""
from __future__ import annotations

from typing import Iterable

from ...engine.blackboard import Blackboard
from ...engine.executor import ExecutorProtocol
from ..planning.workflow import AgentNode, FuncNode, GateNode

# Role → (prompt template, default tool scope). The prompt frames *what kind of
# scrutiny* the validator applies; the tools bound what it may touch.
REVIEWER = "reviewer"        # is the result reasonable / does it meet the goal?
ADVERSARIAL = "adversarial"  # hunt false positives, missed cases, overreach
CRITIC = "critic"            # does the plan/output actually satisfy the ORIGINAL goal?
TEST = "test"                # run or write a check that proves it
SYNTHESIS = "synthesis"      # merge multiple views into one judgement
FAILURE_DIAGNOSIS = "failure_diagnosis"  # root-cause a failed step + propose next steps

_ROLE_PROMPTS: dict[str, str] = {
    REVIEWER: (
        "Review the prior work for: {goal}. Judge whether the result is correct "
        "and complete. Report concrete problems, or state clearly that it passes."
    ),
    ADVERSARIAL: (
        "Adversarially audit the prior work for: {goal}. Actively hunt for false "
        "positives, missed cases, and changes that went beyond what was asked. "
        "Report every concern with evidence."
    ),
    CRITIC: (
        "Critique whether the work actually satisfies the ORIGINAL goal: {goal}. "
        "Ignore whether intermediate steps 'ran'; judge end-to-end intent."
    ),
    TEST: (
        "Verify by execution that this is satisfied: {goal}. Run existing tests "
        "or write a minimal check. Report pass/fail with the command output."
    ),
    SYNTHESIS: (
        "Synthesize the prior reviews of: {goal} into one judgement. Resolve "
        "disagreements and state the single verdict and the residual risks."
    ),
    FAILURE_DIAGNOSIS: (
        "A prior step failed while pursuing: {goal}. Read the failure output "
        "(stack trace, test stdout, error message), inspect the related code, "
        "and produce a structured Finding for each suspected root cause. For "
        "each one give: (a) where the bug is (file:line), (b) one-line evidence "
        "from the failure, and (c) a concrete suggested fix. Do NOT modify "
        "files — your job is to diagnose, the next phase will repair."
    ),
}

_ROLE_TOOLS: dict[str, list[str]] = {
    REVIEWER: ["read", "grep"],
    ADVERSARIAL: ["read", "grep", "glob"],
    CRITIC: ["read"],
    TEST: ["shell", "read"],
    SYNTHESIS: [],
    FAILURE_DIAGNOSIS: ["read", "grep", "glob", "shell"],
}


def validator_node(
    role: str,
    *,
    name: str,
    goal: str,
    executor: ExecutorProtocol,
    context_keys: Iterable[str] = (),
) -> AgentNode:
    """An AgentNode specialized to a validation *role* (see constants above)."""
    if role not in _ROLE_PROMPTS:
        raise ValueError(f"unknown validator role {role!r}; choose from {sorted(_ROLE_PROMPTS)}")
    return AgentNode(
        name=name,
        sub_goal=_ROLE_PROMPTS[role].format(goal=goal),
        executor=executor,
        tools=list(_ROLE_TOOLS[role]),
        context_keys=list(context_keys),
    )


def verdict_gate(
    *,
    name: str,
    attempt_key: str,
    max_attempts: int,
    pass_label: str = "ok",
    repair_label: str = "repair",
    give_up_label: str = "give_up",
) -> GateNode:
    """Pass / repair / give_up router based on the last node's ok flag.

    Bounds the repair loop with a counter stored on the Blackboard under
    ``attempt_key`` so we never loop forever. This generalizes the gate that was
    inlined in the modify template.
    """

    def predicate(bb: Blackboard) -> str:
        ok = bool(bb.history and bb.history[-1][1])  # last node's ok flag
        if ok:
            return pass_label
        attempts = int(bb.state.get(attempt_key, 0))
        if attempts >= max_attempts:
            return give_up_label
        bb.state[attempt_key] = attempts + 1
        return repair_label

    return GateNode(name=name, predicate=predicate)


def convergence_node(name: str = "converge") -> FuncNode:
    """Fold the Blackboard's structured findings through merge→dedup→rank.

    Writes the ranked list under ``converged_findings`` and a count under
    ``converged_count`` so a downstream gate/report can act on the merged view —
    this is the deterministic fan-in the report calls the real goal (§11.4)."""

    def fn(bb: Blackboard) -> dict:
        ranked = bb.converged_findings()
        bb.state["converged_findings"] = ranked
        return {"converged_count": len(ranked)}

    return FuncNode(name=name, fn=fn)


def self_critique_gate(
    *,
    name: str = "self_critique",
    critic=None,
) -> GateNode:
    """Terminal critique gate (report §3.6 / §9.4 self-critique).

    Wraps a critic predicate that decides whether the workflow's output is
    adequate. Routes ``ok`` (proceed to END / report) or ``inadequate``
    (route to a repair / re-plan branch in the graph). The default critic
    is a deterministic check of the Blackboard:

      - if convergence produced findings, the run did real work → ``ok``
      - if the run has a non-empty history, at least something completed → ``ok``
      - else the run produced nothing observable → ``inadequate``

    A caller can pass a custom ``critic(bb) -> (bool, str)`` when richer
    judgement is needed (an LLM-backed critic, a finding-quality threshold,
    a domain-specific completeness check). The summary string lands on the
    Blackboard under ``{name}.summary`` so a downstream report can quote it.
    """

    def default_critic(bb: Blackboard) -> tuple[bool, str]:
        ranked = bb.state.get("converged_findings") or []
        if ranked:
            return True, f"converged {len(ranked)} finding(s)"
        if bb.history:
            ok_count = sum(1 for _, ok, _ in bb.history if ok)
            return True, f"completed {ok_count}/{len(bb.history)} step(s)"
        return False, "no findings produced and no history recorded"

    check = critic or default_critic

    def predicate(bb: Blackboard) -> str:
        ok, summary = check(bb)
        bb.state[f"{name}.summary"] = summary
        bb.state[f"{name}.verdict"] = "ok" if ok else "inadequate"
        return "ok" if ok else "inadequate"

    return GateNode(name=name, predicate=predicate)
