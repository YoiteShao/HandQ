"""
PlannerMixin — Planning intelligence, quality controls, and acceptance synthesis.

This mixin provides planning capabilities to the Orchestrator class:
  - Loop detection: repeated failed goals → warning injection
  - Epistemic inventory: ASSUMED claims → observation obligation warning
  - Failed-tail check: about to end on a failed item → advisory warning
    injected into the planner prompt (supervisor, not enforcer — the planner
    keeps authority; the bounded acceptance gate is the real backstop)
  - Acceptance synthesis: goal-level 5-verdict tiered judgment after a
    task-complete candidate state. Verifier self-bounds via the ACCEPT
    verdict; no host-side round counter.

Usage:
  class Orchestrator(PlannerMixin, ReceptionistMixin):
      def __init__(self, ...):
          self._init_planner()
          # Receptionist no longer has init — skills state lives in checklist.
          ...
"""
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, cast

from ..infrastructure.llm_pool import call_with_fallback
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.utils import try_parse_json
from .shared_checklist import CheckListItem, ItemResult, INTERRUPTED_BY_PLANNER

if TYPE_CHECKING:
    from .shared_checklist import SharedCheckList


# ── AcceptanceVerdict ────────────────────────────────────────────────────────

# Allowed verdict tiers. Order matters for documentation only — the dispatcher
# in Orchestrator._handle_task_complete_candidate routes on these strings.
_ALLOWED_VERDICTS = ("PASS", "TRIVIAL", "EXTEND", "VALIDATE", "ACCEPT")


@dataclass
class AcceptanceVerdict:
    """Structured result of PlannerMixin.synthesize_acceptance().

    Goal-level tiered verdict. The verifier is *asked* to self-bound via
    ACCEPT once `acceptance_*` items are visible in the completed list, but
    that is advisory only. The enforced termination axis is **information
    gain per round**, not a round count: `_handle_task_complete_candidate`
    converts EXTEND/VALIDATE into a terminal ACCEPT only when a round adds no
    new information (mechanical delta == 0) AND the cheap auditor confirms the
    candidate is semantically spinning. `_ACCEPTANCE_SEATBELT_ROUNDS` is a
    high defense-in-depth ceiling, never the deciding mechanism in normal
    operation.

    Fields:
      verdict          — one of PASS | TRIVIAL | EXTEND | VALIDATE | ACCEPT.
      gap_summary      — one-sentence description of what's missing
                         (used by ACCEPT; empty for PASS/TRIVIAL).
      items_to_inject  — CheckListItem(s) to inject as new pending tail
                         (non-empty for EXTEND/VALIDATE; empty otherwise).
      fallback         — True when synthesis raised and we returned a
                         fail-open ACCEPT default.
      user_summary     — 2-3 sentence narrative answer to the user's original
                         request, written when the verifier terminates the
                         task (PASS/TRIVIAL/ACCEPT). Empty for EXTEND/VALIDATE
                         (no completion reply is emitted at that point) and
                         for the fail-open fallback. This is the ONLY LLM-
                         authored prose in the completion reply — Orchestrator
                         ._compose_completion_reply renders it above the
                         structured outcomes/findings/artifacts block instead
                         of duplicating them, so the verifier is instructed to
                         conclude rather than re-list.
    """
    verdict: str = "PASS"
    gap_summary: str = ""
    items_to_inject: List[CheckListItem] = field(default_factory=list)
    fallback: bool = False
    user_summary: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> 'AcceptanceVerdict':
        """Coerce a parsed JSON dict from the verifier into an AcceptanceVerdict.

        Defensive normalisation:
          - Unknown verdict values snap to ACCEPT (fail-open: surface a gap
            rather than loop).
          - PASS/TRIVIAL/ACCEPT enforce empty items_to_inject regardless of
            what the LLM emitted.
          - Items with empty `instruction` are dropped.
          - Item.item_id without `acceptance_` prefix is left as-is — the
            prompt asks for the prefix but we don't reject the verdict if
            the LLM forgot.
        """
        verdict = (data.get('verdict') or '').strip().upper()
        if verdict not in _ALLOWED_VERDICTS:
            verdict = 'ACCEPT'

        gap_summary = (data.get('gap_summary') or '').strip()
        user_summary = (data.get('user_summary') or '').strip()

        items_raw = data.get('items_to_inject') or []
        if not isinstance(items_raw, list):
            items_raw = []

        items: List[CheckListItem] = []
        for entry in items_raw:
            if not isinstance(entry, dict):
                continue
            instr = (entry.get('instruction') or '').strip()
            if not instr:
                continue
            items.append(CheckListItem.from_planner_dict(entry))

        if verdict in ('PASS', 'TRIVIAL', 'ACCEPT'):
            items = []
        if verdict in ('PASS', 'TRIVIAL'):
            gap_summary = ''
        if verdict in ('EXTEND', 'VALIDATE'):
            # No completion reply is emitted on these verdicts — more work is
            # queued. A stray user_summary here would be silently dropped by
            # the dispatcher anyway; clear it so from_dict's output is a
            # faithful contract, not just what happens to be discarded later.
            user_summary = ''

        return cls(
            verdict=verdict,
            gap_summary=gap_summary,
            items_to_inject=items,
            user_summary=user_summary,
        )

    @classmethod
    async def from_data(cls, raw_content: str) -> 'AcceptanceVerdict':
        """Parse LLM raw content into a verdict; fall back to ACCEPT on failure."""
        parsed = try_parse_json(raw_content)
        if isinstance(parsed, dict):
            return cls.from_dict(parsed)
        return cls(
            verdict='ACCEPT',
            gap_summary='Acceptance response could not be parsed; verification did not run.',
            fallback=True,
        )


# ── PlannerMixin ─────────────────────────────────────────────────────────────

class PlannerMixin:
    """Planning intelligence mixed into Orchestrator.

    Expects the host class to provide:
      - self._services: List[LLMService]
      - self._checklist: SharedCheckList
      - self.logger
    """

    if TYPE_CHECKING:
        _services: List[LLMService]
        _checklist: "SharedCheckList"

    def _init_planner(self) -> None:
        """Initialize planner state. Called from Orchestrator.__init__."""
        self._on_demand_tools_table: str = ""
        self._on_demand_routing_rules: str = ""
        self._on_demand_antipatterns: str = ""
        # Tracks whether the last _call_and_parse_streaming actually pushed
        # response_to_user fragments to the UI. False when the call fell back
        # to non-streaming (no fragments emitted) — callers use this to decide
        # whether a non-empty reply still needs a batch emit.
        self._last_response_streamed: bool = False
        # Wall-clock anchor for the current task, set alongside
        # _task_root_query in _handle_user_message (same "task just started"
        # boundary). None while idle / before the first task. See
        # _TASK_WALLCLOCK_BUDGET_SEC for how this is used.
        self._task_started_at: Optional[float] = None

    # ── Loop detection ───────────────────────────────────────────────────────

    def _detect_loops(self, results: List[ItemResult]) -> str:
        """Detect if the same item instruction has been attempted multiple times with failures.

        Returns a formatted warning string for prompt injection, or "" if none.
        """
        if len(results) < 3:
            return ""

        items = getattr(self, '_checklist', None)
        if items is None:
            return ""
        all_items = items.items

        instruction_attempts: dict = {}
        for i, result in enumerate(results):
            item = all_items[i] if i < len(all_items) else None
            if item is None:
                continue
            key = item.instruction.lower().strip()[:80]
            if key not in instruction_attempts:
                instruction_attempts[key] = []
            instruction_attempts[key].append(result.success)

        repeated = [
            (instr_key, attempts)
            for instr_key, attempts in instruction_attempts.items()
            if len(attempts) >= 2 and not all(attempts)
        ]

        if not repeated:
            return ""

        lines = ["⚠️ LOOP DETECTED — the following instructions have been attempted multiple times:"]
        for instr_key, attempts in repeated[:3]:
            fail_count = sum(1 for s in attempts if not s)
            lines.append(
                f"  • '{instr_key[:70]}' — "
                f"{fail_count}/{len(attempts)} attempts failed"
            )
        lines.append(
            "  → You MUST try a fundamentally different approach "
            "(different tool, method, or decomposition) — not a minor variation.\n"
        )
        return "\n".join(lines) + "\n"

    # ── Epistemic inventory ──────────────────────────────────────────────────

    def _build_epistemic_inventory_warning(
        self,
        user_message: str,
        results: List[ItemResult],
    ) -> str:
        """Build epistemic_preamble block for planning prompt.

        Initial call (results empty):
          Scans user_message text for external-world entity references and lists
          them as ASSUMED claims.

        Post-exploration (results non-empty, recent findings exist):
          Injects a concretization requirement after the first item completes.
        """
        if results:
            if len(results) == 1 and (
                results[0].key_findings or results[0].factual_outcome
            ):
                return (
                    "\n[Instruction Grounding Requirement]\n"
                    "The first item has completed with observed findings. "
                    "REQUIREMENT: every subsequent item instruction MUST now use concrete "
                    "values confirmed by that output — actual file paths, exact "
                    "function names, confirmed command syntax. Abstract instructions are "
                    "not acceptable when the concrete target is now known.\n"
                )
            return ""

        patterns = [
            (r'\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+){2,}\b', 'hostname or server name'),
            (r'\b(?:[a-z0-9][\w\-]*\.){2,}[a-z]{2,}\b', 'domain or FQDN'),
            (r'[\w./\-]+/[\w./\-]+', 'file/directory path'),
            (r'[\w.\\\-]+\\[\w.\\\-]+', 'file/directory path'),
            (r'[A-Za-z]:\\[\w.\\\- ]+', 'file/directory path'),
            (r'[\w\-]+\.(?:py|js|ts|json|yaml|yml|toml|cfg|ini|sh|md|txt|csv|sql|env)', 'file name'),
            (r'\b[A-Z][A-Z0-9_]{2,}\b', 'environment variable or constant'),
            (r'/[a-z][a-z0-9_/\-]{2,}', 'API endpoint path'),
        ]

        assumed_claims = []
        seen: Set[str] = set()
        covered_spans: List[tuple] = []
        for pattern, claim_type in patterns:
            for match in re.finditer(pattern, user_message):
                start, end = match.span()
                if any(s <= start and end <= e for s, e in covered_spans):
                    continue
                entity = match.group(0)
                if entity in seen:
                    continue
                seen.add(entity)
                covered_spans.append((start, end))
                assumed_claims.append((entity, claim_type))

        if not assumed_claims:
            return ""

        lines = [
            "⚠️ EPISTEMIC INVENTORY — the following entities from the task description "
            "have not yet been confirmed by tool output (tagged ASSUMED):"
        ]
        for entity, claim_type in assumed_claims[:10]:
            lines.append(f"  • [{claim_type}] '{entity}' — ASSUMED (not yet observed)")
        lines.append(
            "  → For each ASSUMED claim with non-trivial risk, schedule an "
            "observation item BEFORE the action item that depends on it.\n"
        )
        return "\n".join(lines) + "\n"

    # ── Structural supervisor (advisory only) ─────────────────────────────────

    def _build_failed_tail_warning(self, results: List[ItemResult]) -> str:
        """Advisory warning when the task is about to end on a failed item.

        Supervisor, not enforcer: this returns a prompt-injection string (like
        `_detect_loops`) that flags "your most recent item failed — decide
        deliberately before ending" and hands the decision back to the planner.
        It does NOT rewrite the planner's `post_current_items`; the planner (and,
        when it chooses to end, the bounded acceptance gate in
        `_handle_task_complete_candidate`) hold final authority.

        Returns "" when there is nothing to flag:
          - no completed results,
          - the last completed item succeeded cleanly, or
          - the last failure was interrupt-driven (the planner already
            acknowledged a user/planner stop — don't nag about it).
        """
        if not results:
            return ""

        last = results[-1]
        if last.success and not last.issues:
            return ""
        # Don't second-guess interrupt-driven exits — the agent recorded the
        # interrupt and the planner is acknowledging it.
        if any(INTERRUPTED_BY_PLANNER in (i or "") for i in (last.issues or [])):
            return ""

        issues = "; ".join(i for i in (last.issues or []) if i) or "(no detail)"
        return (
            "⚠️ FAILED-TAIL CHECK — your most recently completed item "
            f"({last.item_id!r}) failed:\n"
            f"  • {issues[:300]}\n"
            "  → Before ending the task, decide DELIBERATELY:\n"
            "    - If this is a genuine terminal blocker (missing credential, "
            "user declined, tool unavailable, user asked to stop), you MAY end "
            "now — record the blocker explicitly in the completion summary.\n"
            "    - Otherwise, add corrective work using a DIFFERENT approach "
            "(different tool, method, or decomposition).\n"
            "  Do NOT silently drop the user's request, but do NOT loop on an "
            "unrecoverable blocker either.\n"
        )

    def _build_budget_warning(self) -> str:
        """Advisory warning once the task's wall-clock budget is exhausted.

        Supervisor, not enforcer — same posture as `_build_failed_tail_warning`
        and `_detect_loops`: returns a prompt-injection string that tells the
        planner to wrap up, but does not touch the checklist itself. No item
        is cancelled and no in-flight tool call is interrupted; a planner that
        ignores this and keeps extending is still bounded by the existing
        `_ACCEPTANCE_SEATBELT_ROUNDS` once a task-complete candidate state is
        finally reached.

        Returns "" when there is no active task timer (idle) or the budget
        has not yet been exceeded.
        """
        if self._task_started_at is None:
            return ""
        elapsed = time.monotonic() - self._task_started_at
        if elapsed <= self._TASK_WALLCLOCK_BUDGET_SEC:
            return ""

        return (
            "⏱️ TASK BUDGET EXCEEDED — this task has been running for "
            f"{elapsed / 60:.0f} minutes (soft budget: "
            f"{self._TASK_WALLCLOCK_BUDGET_SEC / 60:.0f} minutes).\n"
            "  → Do NOT open new exploratory work. Only add items that are "
            "strictly necessary to close out what is already in progress, or "
            "end the task now with whatever has been accomplished — noting "
            "any known gap explicitly rather than continuing to explore.\n"
        )

    # ── Summary builders ─────────────────────────────────────────────────────
    #
    # NOTE: V2 deliberately does NOT have v1's tiered _build_completed_summary
    # / _build_lookahead_summary helpers. The BACKGROUND_EVAL prompt evaluates
    # ONE item at a time and uses SharedCheckList.get_checklist_context_for_
    # planner() which already provides a status snapshot of every item. The
    # tiered v1 helpers existed because v1 rebuilt completed-step prose from
    # the Memory step list each replan; V2 reads directly from the shared
    # checklist, so no separate prose builder is needed.

    # ── Acceptance synthesis ──────────────────────────────────────────────────

    _ACCEPTANCE_PREFIX = "acceptance_"
    # Seatbelt (defense-in-depth only). Termination is normally decided by
    # information gain per round — see _handle_task_complete_candidate: a round
    # that adds nothing new (mechanical delta == 0) and is confirmed spinning by
    # the cheap auditor terminates immediately, while rounds that keep surfacing
    # genuinely new info are allowed to continue. This ceiling exists ONLY to
    # bound pathological cases where the novelty signal is defeated (e.g.
    # timestamped artifact paths that always read as "new"). Hitting it is
    # logged at WARNING; it must never be the deciding mechanism in normal runs.
    _ACCEPTANCE_SEATBELT_ROUNDS = 6

    # Task-level wall-clock soft budget. Bounds the gap between
    # MAX_ITEM_ITERATIONS (per-item, near-unlimited) and
    # _ACCEPTANCE_SEATBELT_ROUNDS (per-acceptance-round, only reachable once a
    # task-complete candidate state is reached): a planner that keeps
    # extending post_current_items without ever emptying the checklist would
    # otherwise have no ceiling at all. This is advisory, not a hard abort —
    # see _run_planner's use of _task_started_at: once exceeded, the planner
    # prompt is told to wrap up rather than open new exploratory work, but no
    # in-flight item or asyncio task is cancelled. If the planner still keeps
    # extending, the existing acceptance seatbelt above is the real backstop
    # once a task-complete candidate is finally reached.
    _TASK_WALLCLOCK_BUDGET_SEC = 1800

    async def synthesize_acceptance(
        self,
        conversation_history: List[Dict[str, str]],
        completed_results: List[ItemResult],
        checklist_items: List[CheckListItem],
    ) -> AcceptanceVerdict:
        """Goal-level 5-verdict tiered judgment.

        Single LLM call. Verifier sees the FULL conversation (not just the
        latest user message) and the completed items, and is asked to
        self-bound via the ACCEPT verdict. That self-bounding is advisory; the
        hard stop is information-gain based — `_handle_task_complete_candidate`
        terminates a round that adds no new info and is confirmed spinning,
        backed by the `_ACCEPTANCE_SEATBELT_ROUNDS` defense-in-depth ceiling.

        Failure path: returns ACCEPT with `fallback=True` so the dispatcher
        finalises the task with a "synthesis failed" gap rather than looping.
        """
        from .planner_prompts import (
            ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT,
            ACCEPTANCE_SYNTHESIS_TEMPLATE,
        )

        conversation_block = self._render_conversation_block(conversation_history)
        completed_items_block = self._render_completed_items_block(
            completed_results, checklist_items,
        )
        acceptance_history_line = self._render_acceptance_history_line(
            completed_results,
        )

        prompt = ACCEPTANCE_SYNTHESIS_TEMPLATE.format(
            conversation_block=conversation_block,
            completed_items_block=completed_items_block,
            acceptance_history_line=acceptance_history_line,
        )

        logger = getattr(self, 'logger', get_logger())
        logger.info(
            f"Synthesizing acceptance — items={len(completed_results)}, "
            f"acceptance_history={acceptance_history_line}",
            component='Orchestrator',
        )

        try:
            messages = [
                {"role": "system", "content": ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=messages),
                on_fallback=lambda idx, e: logger.warning(
                    f"Acceptance synthesis LLM fallback {idx}: {e}",
                    component="Orchestrator",
                ),
            ))
            response_text = _raw.content or ""
            verdict = await AcceptanceVerdict.from_data(response_text)

            logger.info(
                f"Acceptance verdict: {verdict.verdict} "
                f"(gap={verdict.gap_summary[:80]!r}, "
                f"items_to_inject={len(verdict.items_to_inject)})",
                component='Orchestrator',
            )
            return verdict

        except Exception as e:
            logger.warning(
                f'synthesize_acceptance failed ({type(e).__name__}): {e}; '
                f'returning fail-open ACCEPT.',
                component='Orchestrator',
            )
            return AcceptanceVerdict(
                verdict='ACCEPT',
                gap_summary='Acceptance synthesis failed; verification did not run.',
                fallback=True,
            )

    # ── Acceptance prompt rendering helpers ──────────────────────────────────

    @staticmethod
    def _render_conversation_block(
        conversation_history: List[Dict[str, str]],
    ) -> str:
        """Render full conversation as `User: ...` / `Assistant: ...` lines.

        Differs from Orchestrator._format_conversation_history (which is for
        prior-turn injection in the planner prompt and slices off the current
        message). Acceptance synthesis MUST see every turn including the
        latest user request — that's the goal anchor.
        """
        if not conversation_history:
            return "(no conversation history)"
        lines: List[str] = []
        for turn in conversation_history:
            role = turn.get("role", "")
            content = turn.get("content", "") or ""
            if role == "user":
                lines.append(f"User: {content}")
            elif role == "assistant":
                lines.append(f"Assistant: {content}")
            else:
                lines.append(f"{role.capitalize() or 'Other'}: {content}")
        return "\n".join(lines)

    def _render_completed_items_block(
        self,
        completed_results: List[ItemResult],
        checklist_items: List[CheckListItem],
    ) -> str:
        """Render every completed item with the planner-side fields the LLM needs.

        Items whose `item_id` starts with `acceptance_` get a `[acceptance
        attempt #N]` tag so the verifier can count prior rounds and decide
        when to ACCEPT.
        """
        if not completed_results:
            return "(no completed items)"

        item_by_id: Dict[str, CheckListItem] = {
            it.item_id: it for it in checklist_items
        }

        acceptance_seen = 0
        lines: List[str] = []
        for i, r in enumerate(completed_results, 1):
            tag = "Done" if r.success else "Failed"
            extra = ""
            if r.item_id.startswith(self._ACCEPTANCE_PREFIX):
                acceptance_seen += 1
                extra = f" [acceptance attempt #{acceptance_seen}]"

            it = item_by_id.get(r.item_id)
            instruction = (it.instruction if it else "(instruction unavailable)")
            expected = (
                "; ".join(it.expected_outcomes)
                if it and it.expected_outcomes else "(none)"
            )
            outcome = '; '.join(r.factual_outcome) if r.factual_outcome else '(none)'
            artifacts = ', '.join(r.artifacts) if r.artifacts else '(none)'
            findings = '; '.join(r.key_findings) if r.key_findings else '(none)'
            issues = '; '.join(r.issues) if r.issues else ''

            block = [
                f"{i}. [{tag}] item_id={r.item_id}{extra} (iters={r.iterations})",
                f"   Instruction: {instruction}",
                f"   Expected:    {expected}",
                f"   Outcome:     {outcome}",
                f"   Artifacts:   {artifacts}",
                f"   Findings:    {findings}",
            ]
            if issues:
                block.append(f"   Issues:      {issues}")
            lines.append("\n".join(block))
        return "\n".join(lines)

    def _render_acceptance_history_line(
        self,
        completed_results: List[ItemResult],
    ) -> str:
        """One-line summary of how many acceptance_* items have already run."""
        count = sum(
            1 for r in completed_results
            if r.item_id.startswith(self._ACCEPTANCE_PREFIX)
        )
        if count == 0:
            return "0 prior acceptance items — first verification round."
        plural = "items" if count > 1 else "item"
        return (
            f"{count} prior acceptance {plural} already ran. "
            f"If the gap they targeted is still open, return ACCEPT — do not loop."
        )
