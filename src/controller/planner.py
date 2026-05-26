"""
Planner - Task planning and decomposition

Role
----
The Planner is responsible for the agent and controls all agent behaviours.
It is the sole decision-maker for:
  • What steps the agent executes next (observe_and_plan).
  • Whether the current agent should be terminated immediately (terminate intent).

The Planner does NOT classify user messages and does NOT own any I/O queues.
User-message classification (chat vs task, respond_only vs replan) is the
exclusive responsibility of the Receptionist.

InteractionManager owns the user-message queue and provides
get_pending_user_message() to FlowController.  FlowController drains that
queue and passes REPLAN-intent messages to observe_and_plan() here.
The Planner never reads stdin directly.
"""
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, cast

from ..infrastructure.llm_pool import call_with_fallback
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.progress_checker import ProgressAnalyzerBase
from ..infrastructure.utils import try_parse_json
from ..infrastructure.gep_template import validate_instantiated_steps
from ..models.plan import Plan, Step, StepStatus
from .planner_prompts import (
    PLANNER_SYSTEM_PROMPT,
    build_planner_system_prompt,
    OBSERVE_AND_PLAN_TEMPLATE,
    ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT,
    ACCEPTANCE_SYNTHESIS_TEMPLATE,
    GEP_MARKER,
    GEP_REPLAN_CONSTRAINT,
    GEP_ADAPTIVE_INSTANTIATION_SYSTEM,
    GEP_ADAPTIVE_INSTANTIATION_TEMPLATE,
    STEP_COMPRESSION_SYSTEM_PROMPT,
    STEP_COMPRESSION_TEMPLATE,
)


# ── Planner progress status (used by FlowController) ─────────────────────────

@dataclass
class PlannerProgressStatus:
    """Result of a planner progress analysis."""
    should_inject_reminder: bool
    should_abort: bool
    reminder_message: Optional[str] = None
    abort_reason: Optional[str] = None
    # True when the system has detected severe stagnation and is asking the
    # planner to explicitly reason about whether the task is still achievable.
    # The planner may then return empty next_steps with high confidence if it
    # concludes the task is infeasible (to signal completion/abort).
    should_assess_feasibility: bool = False


# ── Acceptance verdict dataclass ─────────────────────────────────────────────

@dataclass
class AcceptanceVerdict:
    """Structured result of Planner.synthesize_acceptance().

    Goal-level verdict for whether the completed work satisfies the user's
    ORIGINAL GOAL.  Replaces the prior independent-agent verification step:
    per-step confidence is already validated by observe_and_plan; this is
    the single seam-checking pass at task completion.

    Fields:
      verdict          — 'PASS' | 'PARTIAL' | 'FAIL'.
      gap_summary      — one-sentence description of what is missing
                         (PARTIAL/FAIL); empty when PASS.
      corrective_step  — concrete corrective Step to inject when verdict is
                         PARTIAL/FAIL.  None when PASS.
      code_test_step   — narrow shell test step (e.g. py_compile / pytest)
                         emitted ONLY when has_code_edits=True and the work
                         has not yet been tested.  None otherwise.
    """
    verdict: str = "PASS"
    gap_summary: str = ""
    corrective_step: Optional[Step] = None
    code_test_step: Optional[Step] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'AcceptanceVerdict':
        """Create an AcceptanceVerdict from a parsed LLM response dict.

        Defensive parsing: unknown verdicts coerce to PASS (fail-open) so a
        malformed LLM response does not block task completion indefinitely.
        """
        verdict_raw = (data.get('verdict') or '').strip().upper()
        if verdict_raw not in ('PASS', 'PARTIAL', 'FAIL'):
            verdict_raw = 'PASS'

        def _build_step(node: Any) -> Optional[Step]:
            if not isinstance(node, dict):
                return None
            step_id = (node.get('step_id') or '').strip()
            goal = (node.get('goal') or '').strip()
            if not step_id or not goal:
                return None
            description = (node.get('description') or '').strip() or step_id
            expected = node.get('expected_outcomes') or []
            if not isinstance(expected, list):
                expected = [str(expected)]
            else:
                expected = [str(e).strip() for e in expected if str(e).strip()]
            return Step.from_planner(
                step_id=step_id,
                description=description,
                goal=goal,
                expected_outcomes=expected,
                risk_assessment="Low risk — synthesized acceptance step",
                required_context_keys=[],
            )

        corrective = _build_step(data.get('corrective_step'))
        code_test = _build_step(data.get('code_test_step'))
        # Normalize the test step id so the runtime can detect it for the
        # already_tested loop guard regardless of the LLM's chosen suffix.
        if code_test is not None and not code_test.step_id.startswith('acceptance_test_'):
            code_test.step_id = f"acceptance_test_{code_test.step_id}"
        return cls(
            verdict=verdict_raw,
            gap_summary=(data.get('gap_summary') or '').strip(),
            corrective_step=corrective,
            code_test_step=code_test,
        )

    @classmethod
    async def from_data(cls, raw_content: str) -> 'AcceptanceVerdict':
        """Parse a raw LLM response string into an AcceptanceVerdict.

        Returns a default PASS instance if parsing fails so a malformed LLM
        response does not stall the task (fail-open).
        """
        parsed = try_parse_json(raw_content)
        if isinstance(parsed, dict):
            return cls.from_dict(parsed)
        return cls()


# ── Planner ───────────────────────────────────────────────────────────────────

class Planner:
    """
    Task planner — observes execution history, decides next step(s), and
    evaluates user messages on behalf of the agent system.

    User-message flow
    -----------------
    InteractionManager owns the user-message queue and exposes
    get_pending_user_message().  FlowController drains that queue at the
    start of each planning cycle and calls evaluate_user_message() here for
    each message to decide what to do.

    The Planner never reads stdin and never owns any I/O queues.
    """

    def __init__(
        self,
        llm_services: List[LLMService],
        working_directory: Optional[str] = None,
        storage_directory: Optional[str] = None,
        step_verification_threshold: float = 0.7,
        from_data_services: Optional[List[LLMService]] = None,
        gep_template=None,
    ):
        if not llm_services:
            raise ValueError("Planner requires at least one LLMService in llm_services")
        self._services: List[LLMService] = list(llm_services)

        # from_data_services: used for Plan.from_data JSON extraction fallback.
        # Falls back to last service in _services when not provided by FlowController.
        if from_data_services:
            self._from_data_services: List[LLMService] = list(from_data_services)
        else:
            self._from_data_services = [self._services[-1]]

        # See FlowController.__init__ for the working_directory=None semantics.
        self.working_directory: Optional[str] = working_directory
        self.storage_directory: str = storage_directory or working_directory or "."
        self.step_verification_threshold = step_verification_threshold
        self.logger = get_logger()
        # Token count from the most recent observe_and_plan() LLM call.
        # Read by FlowController after observe_and_plan() returns to forward
        # the count to notify_reasoning() for display in the TUI.
        self.last_token_count: int = 0
        # Input tokens from the most recent observe_and_plan() call.
        # Used by maybe_compress_steps() to decide whether compression is needed
        # (input size is what exhausts the context window, not output).
        self.last_input_tokens: int = 0
        self.detail_window: int = 4  # steps shown in full detail by _build_completed_summary
        self._gep_template = gep_template
        # Dynamic tool table — set by FlowController after provider registration.
        # Each row is a pipe-delimited Markdown table line from provider.planner_description().
        self._on_demand_tools_table: str = ""
        self._on_demand_routing_rules: str = ""
        self._on_demand_antipatterns: str = ""
        self._coding_rule_num: int = 6
        self.logger.info("Planner initialized successfully", component="Planner")

        # ── Context compression state ─────────────────────────────────────────
        # Compress early steps when input_tokens reaches context_compression_ratio
        # of the most constrained model's context_window.  Using the minimum
        # across all services ensures safety even when a 200K model is a fallback.
        self.context_compression_ratio: float = 0.85
        self.min_early_steps_for_compression: int = 3
        self._compressed_steps_cache: Optional[List[dict]] = None
        self._compressed_up_to_count: int = 0  # how many early steps are cached

    def _instantiate_gep_steps(self, params: dict) -> List[Step]:
        """
        Instantiate GEP template steps as proper Step objects with parameter
        placeholders resolved.  Merges user-supplied params with schema defaults;
        unresolved placeholders are left as-is so the agent can surface the gap.

        Returns an empty list when no GEP template is loaded.
        """
        if self._gep_template is None:
            return []

        # Support both GEPTemplate dataclass and plain dict (for forward compat)
        if isinstance(self._gep_template, dict):
            guide_steps  = self._gep_template.get('guide_steps', [])
            params_schema = self._gep_template.get('params_schema', {})
        else:
            guide_steps   = self._gep_template.guide_steps
            params_schema = getattr(self._gep_template, 'params_schema', {})

        # Build merged params: schema defaults → caller overrides
        defaults: dict = {}
        for k, v in params_schema.items():
            if isinstance(v, dict):
                defaults[k] = v.get('default')
            else:
                defaults[k] = getattr(v, 'default', None)
        merged_params = {**defaults, **params}

        def _sub(text: str) -> str:
            return re.sub(
                r'\{\{params\.(\w+)\}\}',
                lambda m: str(merged_params.get(m.group(1), m.group(0))),
                text,
            )

        result: List[Step] = []
        for raw in guide_steps:
            if isinstance(raw, dict):
                sid        = raw.get('step_id', f'gep_step_{len(result) + 1}')
                desc       = raw.get('description', '')
                goal       = _sub(raw.get('goal', ''))
                supplement = _sub(raw.get('step_supplement', ''))
                pg         = raw.get('parallel_group', '')
                is_agg     = bool(raw.get('is_aggregation', False))
                reasoning  = raw.get('planner_reasoning', '')
                expected   = list(raw.get('expected_outcomes', []))
                risk       = _sub(raw.get('risk_assessment', ''))
                req_ctx    = list(raw.get('required_context_keys', []))
            else:
                sid        = getattr(raw, 'step_id', f'gep_step_{len(result) + 1}')
                desc       = getattr(raw, 'description', '')
                goal       = _sub(getattr(raw, 'goal', ''))
                supplement = _sub(getattr(raw, 'step_supplement', ''))
                pg         = getattr(raw, 'parallel_group', '')
                is_agg     = bool(getattr(raw, 'is_aggregation', False))
                reasoning  = getattr(raw, 'planner_reasoning', '')
                expected   = list(getattr(raw, 'expected_outcomes', []))
                risk       = _sub(getattr(raw, 'risk_assessment', ''))
                req_ctx    = list(getattr(raw, 'required_context_keys', []))

            result.append(Step.from_planner(
                step_id=sid,
                description=desc,
                goal=goal,
                step_supplement=supplement,
                parallel_group=pg,
                is_aggregation=is_agg,
                planner_reasoning=reasoning,
                expected_outcomes=expected,
                risk_assessment=risk,
                required_context_keys=req_ctx,
                ssh_target=_sub(
                    raw.get('ssh_target', '') if isinstance(raw, dict)
                    else getattr(raw, 'ssh_target', '')
                ),
            ))

        return result

    # ── GEP parameter extraction & direct instantiation ───────────────────

    async def extract_gep_params(self, goal: str) -> dict:
        """
        Extract GEP template parameter values from the user's goal description.

        Makes a single LLM call to map the user's natural-language request onto
        the template's params_schema.  Unmentioned parameters fall back to their
        schema defaults.  Returns a dict of resolved {param_name: value} pairs.
        """
        if self._gep_template is None:
            return {}

        if isinstance(self._gep_template, dict):
            params_schema = self._gep_template.get('params_schema', {})
            template_name = self._gep_template.get('name', 'this task')
            description  = self._gep_template.get('description', '')
        else:
            params_schema = getattr(self._gep_template, 'params_schema', {})
            template_name = getattr(self._gep_template, 'name', 'this task')
            description   = getattr(self._gep_template, 'description', '')

        if not params_schema:
            return {}

        defaults: dict = {}
        param_lines: List[str] = []
        for pname, pspec in params_schema.items():
            if isinstance(pspec, dict):
                pdesc    = pspec.get('description', '')
                pdefault = pspec.get('default')
                ptype    = pspec.get('type', 'string')
                pemphasis = pspec.get('emphasis', False)
            else:
                pdesc    = getattr(pspec, 'description', '')
                pdefault = getattr(pspec, 'default', None)
                ptype    = getattr(pspec, 'type', 'string')
                pemphasis = getattr(pspec, 'emphasis', False)
            if pdefault is not None:
                defaults[pname] = pdefault
            emphasis_marker = " ▶" if pemphasis else ""
            suffix = f" [default: {pdefault}]{emphasis_marker}"
            param_lines.append(f"  {pname} ({ptype}): {pdesc}{suffix}")

        params_text = "\n".join(param_lines)
        prompt = (
            f'Template: "{template_name}"\n'
            f'Description: {description}\n\n'
            f"User's request:\n{goal}\n\n"
            f"Extract parameter values from the user's request. "
            f"For parameters not explicitly mentioned, use the default value.\n\n"
            f"Parameters:\n{params_text}\n\n"
            f"Output ONLY a JSON object mapping parameter names to their values. "
            f'Example: {{"source_path": "/data/input", "target_path": "/data/output"}}'
        )

        try:
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=[
                    {"role": "system", "content": "You extract parameter values from task descriptions. Output only valid JSON, no prose."},
                    {"role": "user",   "content": prompt},
                ]),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"GEP param extraction LLM fallback {idx}: {e}", component="Planner"
                ),
            ))
            content = (_raw.content or "").strip()
            result = try_parse_json(content)
            if isinstance(result, dict):
                # Extracted values override defaults
                return {**defaults, **result}
            return defaults
        except Exception as e:
            self.logger.warning(
                f"GEP param extraction failed: {e} — using schema defaults",
                component="Planner",
            )
            return defaults

    async def instantiate_gep_plan(self, goal: str) -> Optional["Plan"]:
        """
        Resolve template parameters from *goal* and return a Plan whose
        next_steps are the template steps fully adapted to the user's task.

        Uses LLM-based adaptive instantiation (_adapt_gep_steps) which handles
        both declared {{params.X}} placeholders AND hardcoded paths/identifiers
        in step goals that the mechanical substitution pipeline would miss.
        Falls back to the legacy extract_gep_params + _instantiate_gep_steps
        pipeline when the LLM call fails.

        Returns None when the template produces no steps (caller falls back to
        normal observe_and_plan).
        """
        steps = await self._adapt_gep_steps(goal)
        if not steps:
            self.logger.warning(
                "GEP template instantiation produced no steps", component="Planner"
            )
            return None
        self.logger.info(
            f"GEP instantiated {len(steps)} steps from template", component="Planner"
        )
        return Plan(goal=goal, next_steps=steps)

    async def _adapt_gep_steps(self, goal: str) -> List[Step]:
        """
        LLM-based adaptive GEP instantiation.

        Single LLM call that receives the full template (name, description,
        params_schema, guide_steps with complete goal text) and the user's
        task goal, and outputs fully adapted steps with ALL values resolved —
        both {{params.X}} placeholders AND hardcoded paths/identifiers that
        would differ for the user's task.

        Falls back to the legacy extract_gep_params + _instantiate_gep_steps
        pipeline on any LLM or parse failure.
        """
        import json as _json

        if self._gep_template is None:
            return []

        if isinstance(self._gep_template, dict):
            name = self._gep_template.get('name', 'template')
            description = self._gep_template.get('description', '')
            guide_steps = self._gep_template.get('guide_steps', [])
            params_schema = self._gep_template.get('params_schema', {})
        else:
            name = self._gep_template.name
            description = self._gep_template.description
            guide_steps = self._gep_template.guide_steps
            params_schema = getattr(self._gep_template, 'params_schema', {})

        # Serialize template for the LLM — normalise dataclass → dict
        def _norm_spec(v: Any) -> dict:
            if isinstance(v, dict):
                return v
            return {
                "type": getattr(v, 'type', 'string'),
                "description": getattr(v, 'description', ''),
                "default": getattr(v, 'default', None),
            }

        def _norm_step(s: Any) -> dict:
            if isinstance(s, dict):
                return s
            return {
                "step_id": getattr(s, 'step_id', ''),
                "description": getattr(s, 'description', ''),
                "goal": getattr(s, 'goal', ''),
                "step_supplement": getattr(s, 'step_supplement', ''),
                "parallel_group": getattr(s, 'parallel_group', ''),
                "is_aggregation": bool(getattr(s, 'is_aggregation', False)),
                "planner_reasoning": getattr(s, 'planner_reasoning', ''),
                "expected_outcomes": list(getattr(s, 'expected_outcomes', [])),
                "risk_assessment": getattr(s, 'risk_assessment', ''),
                "required_context_keys": list(getattr(s, 'required_context_keys', [])),
                "ssh_target": getattr(s, 'ssh_target', ''),
            }

        template_json = _json.dumps({
            "name": name,
            "description": description,
            "params_schema": {k: _norm_spec(v) for k, v in params_schema.items()},
            "guide_steps": [_norm_step(s) for s in guide_steps],
        }, indent=2, ensure_ascii=False)

        prompt = GEP_ADAPTIVE_INSTANTIATION_TEMPLATE.format(
            template_json=template_json,
            goal=goal,
        )

        try:
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=[
                    {"role": "system", "content": GEP_ADAPTIVE_INSTANTIATION_SYSTEM},
                    {"role": "user",   "content": prompt},
                ]),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"GEP adaptive instantiation LLM fallback {idx}: {e}",
                    component="Planner",
                ),
            ))
            content = (_raw.content or "").strip()
            parsed = try_parse_json(content)

            if not isinstance(parsed, dict) or "adapted_steps" not in parsed:
                raise ValueError(f"LLM response missing 'adapted_steps': {content[:200]}")

            result: List[Step] = []
            for i, s in enumerate(parsed["adapted_steps"]):
                if not isinstance(s, dict):
                    continue
                result.append(Step.from_planner(
                    step_id=s.get("step_id", f"gep_step_{i + 1}"),
                    description=s.get("description", ""),
                    goal=s.get("goal", ""),
                    step_supplement=s.get("step_supplement", ""),
                    parallel_group=s.get("parallel_group", ""),
                    is_aggregation=bool(s.get("is_aggregation", False)),
                    planner_reasoning=s.get("planner_reasoning", ""),
                    expected_outcomes=list(s.get("expected_outcomes", [])),
                    risk_assessment=s.get("risk_assessment", ""),
                    required_context_keys=list(s.get("required_context_keys", [])),
                    ssh_target=s.get("ssh_target", ""),
                ))
            if not result:
                raise ValueError("LLM returned empty adapted_steps list")
            expected_count = len([s for s in guide_steps if s is not None])
            if len(result) < expected_count:
                self.logger.warning(
                    f"GEP adaptive instantiation: LLM returned {len(result)} steps "
                    f"but template has {expected_count} — falling back to mechanical substitution",
                    component="Planner",
                )
                raise ValueError(
                    f"LLM dropped steps: got {len(result)}, expected {expected_count}"
                )
            self.logger.info(
                f"GEP adaptive instantiation: {len(result)} steps resolved by LLM",
                component="Planner",
            )
            self._inject_instantiation_warnings(result, guide_steps, goal)
            return result

        except Exception as e:
            self.logger.warning(
                f"GEP adaptive instantiation failed ({type(e).__name__}: {e}) "
                f"— falling back to mechanical substitution",
                component="Planner",
            )
            # Legacy fallback: extract declared params only + mechanical substitution
            params = await self.extract_gep_params(goal)
            fallback = self._instantiate_gep_steps(params)
            self._inject_instantiation_warnings(fallback, guide_steps, goal)
            return fallback

    def _inject_instantiation_warnings(
        self,
        steps: List[Step],
        template_guide_steps: List,
        user_goal: str,
    ) -> None:
        """
        Run validate_instantiated_steps (deterministic invariant check) and
        prepend any violations as a structured warning block into the
        step_supplement of each affected step so the runtime agent sees them
        before acting on that specific step.

        Violations referencing step[N] are injected into steps[N]; violations
        without a step index (unexpected format) fall back to steps[0].

        Mutates steps in place; no-op when all invariants are satisfied.
        """
        if not steps:
            return
        adapted_goals = [s.goal for s in steps]

        # Extract params_schema from the active GEP template so I2 can check
        # param defaults rather than doing an imprecise regex scan.
        params_schema: Optional[dict] = None
        if self._gep_template is not None:
            if isinstance(self._gep_template, dict):
                params_schema = self._gep_template.get('params_schema') or {}
            else:
                params_schema = getattr(self._gep_template, 'params_schema', None) or {}

        violations = validate_instantiated_steps(
            template_guide_steps, adapted_goals, user_goal, params_schema
        )
        if not violations:
            return
        self.logger.warning(
            f"GEP instantiation invariant violations ({len(violations)}): {violations}",
            component="Planner",
        )

        import re as _re
        # Group violations by the step index they reference ("step[N]: ...")
        # Violations without a recognisable step index fall back to step 0.
        per_step: dict = {}
        for v in violations[:8]:
            m = _re.match(r'^step\[(\d+)\]:', v)
            idx = int(m.group(1)) if m else 0
            idx = min(idx, len(steps) - 1)  # clamp to valid range
            per_step.setdefault(idx, []).append(v)

        for idx, step_violations in per_step.items():
            warning_block = (
                "[INSTANTIATION WARNING — verify before acting]\n"
                "The following values in this step may not be correctly adapted "
                "for the current task. Confirm each path/identifier against the "
                "user's actual task context before executing:\n"
                + "\n".join(f"  • {v}" for v in step_violations)
                + "\n\n"
            )
            steps[idx].step_supplement = warning_block + (steps[idx].step_supplement or "")

    # ── Context compression ───────────────────────────────────────────────────

    async def maybe_compress_steps(self, completed_steps: List[Step]) -> None:
        """Compress early completed steps when context grows large.

        Called by FlowController after each observe_and_plan() so the
        compression takes effect for the *next* planning call.

        Trigger condition (both must be true):
          • last_input_tokens >= 85% of the most constrained model's
            context_window (min across all services in self._services)
          • at least min_early_steps_for_compression early steps exist
            (steps beyond the detail_window)

        Using min(context_window) ensures safety when the service list
        contains models with different context sizes (e.g. 200K + 1M).
        """
        min_ctx = min(
            getattr(svc, 'context_window', 200_000) for svc in self._services
        )
        threshold_tokens = int(min_ctx * self.context_compression_ratio)
        if self.last_input_tokens < threshold_tokens:
            return
        early = (
            completed_steps[:-self.detail_window]
            if len(completed_steps) > self.detail_window
            else []
        )
        if len(early) < self.min_early_steps_for_compression:
            return
        if len(early) <= self._compressed_up_to_count:
            return  # cache already up to date
        entries = await self._compress_steps_llm(early)
        if entries:
            self._compressed_steps_cache = entries
            self._compressed_up_to_count = len(early)
            self.logger.info(
                f"Context compression: {len(early)} early steps → {len(entries)} entries "
                f"(input_tokens={self.last_input_tokens}, "
                f"threshold={threshold_tokens}, min_ctx={min_ctx})",
                component="Planner",
            )

    async def _compress_steps_llm(self, early_steps: List[Step]) -> List[dict]:
        """LLM call that compresses early_steps into a smaller list of dicts.

        Each output dict has: covers (list[str]), summary (str),
        artifacts (list[str]), key_findings (list[str]).

        Returns an empty list on any failure (caller treats as no-op).
        """
        import json as _json

        steps_data = []
        for s in early_steps:
            steps_data.append({
                "step_id": s.step_id,
                "description": s.description,
                "status": s.status.value if hasattr(s.status, 'value') else str(s.status),
                "factual_outcome": s.factual_outcome or [],
                "artifacts": s.artifacts or [],
                "key_findings": s.key_findings or [],
                "issues": s.issues or [],
            })

        prompt = STEP_COMPRESSION_TEMPLATE.format(
            steps_json=_json.dumps(steps_data, indent=2, ensure_ascii=False)
        )

        try:
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=[
                    {"role": "system", "content": STEP_COMPRESSION_SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ]),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"Step compression LLM fallback {idx}: {e}", component="Planner"
                ),
            ))
            content = (_raw.content or "").strip()
            parsed = try_parse_json(content)
            if not isinstance(parsed, dict) or "compressed_entries" not in parsed:
                raise ValueError(f"Missing 'compressed_entries' key: {content[:200]}")
            entries = parsed["compressed_entries"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("Empty or non-list 'compressed_entries'")
            # Validate all input step_ids are covered
            covered = {sid for e in entries for sid in (e.get("covers") or [])}
            input_ids = {s["step_id"] for s in steps_data}
            missing = input_ids - covered
            if missing:
                self.logger.warning(
                    f"Compression dropped step_ids {missing} — discarding result",
                    component="Planner",
                )
                return []
            return entries
        except Exception as e:
            self.logger.warning(
                f"Step compression failed ({type(e).__name__}: {e}) — skipping",
                component="Planner",
            )
            return []

    # ── Core planning ─────────────────────────────────────────────────────────

    async def observe_and_plan(
        self,
        goal: str,
        completed_steps: List[Step],
        current_lookahead: List[Step],
        user_message: Optional[str] = None,
        accumulated_findings: str = "",
    ) -> Plan:
        """
        Single planning entry point.
        - completed_steps empty  → initial planning
        - completed_steps has failures → naturally generates corrective steps (implicit replan)
        - user_message set → incorporates user instruction into decision
        - accumulated_findings set → planner has a global view of all key findings
          from all completed steps, not just the detail_window-limited summary.

        When a user_message is present the Planner decides internally whether
        the message affects the current plan step (adjust lookahead only) or
        requires a fundamentally different approach (full replan).  This
        distinction is made inside the LLM call — FlowController does not need
        to pre-classify it.

        Returns:
            Updated Plan: empty next_steps + last_step_confidence >= threshold
            signals task completion; otherwise next_steps is a flat list where
            the first batch is executed immediately and the rest are the lookahead.
        """
        self.logger.debug(
            f"observe_and_plan called — completed={len(completed_steps)}, "
            f"lookahead={len(current_lookahead)}",
            component="Planner"
        )

        loop_warning = self._detect_loops(completed_steps)
        epistemic_preamble = self._build_epistemic_inventory_warning(goal, completed_steps)

        accumulated_findings_section = (
            f"\n[Accumulated Task Findings]\n"
            f"(Key findings and outcomes from all completed steps — "
            f"use this for globally-informed planning)\n"
            f"{accumulated_findings}\n"
        ) if accumulated_findings else ""

        # When a user message is present, add a brief note reminding the planner
        # to assess whether it affects the current step or only future steps.
        # When the goal contains a GEP proven trajectory, append a strong constraint
        # to preserve the remaining lookahead steps unless hard-blocked.
        _is_gep_goal = GEP_MARKER in goal
        user_instruction_block = ""
        if user_message:
            gep_constraint = ("\n" + GEP_REPLAN_CONSTRAINT) if _is_gep_goal else ""
            user_instruction_block = (
                f"\n[User Message]\n"
                f"{user_message}\n\n"
                f"Note: Assess whether this message affects the currently executing step "
                f"or only future steps.  If it only affects future steps, keep the current "
                f"step in steps[0] and adjust the lookahead.  If it requires changing the "
                f"current approach, design a new steps[0] accordingly.\n"
                f"{gep_constraint}"
            )
        elif _is_gep_goal:
            # No user message, but this is a GEP session (auto-replan on step result).
            # Inject the trajectory constraint so the planner does not silently
            # discard the proven lookahead on confidence-driven or stagnation replans.
            user_instruction_block = "\n" + GEP_REPLAN_CONSTRAINT

        system_prompt_content = build_planner_system_prompt(
            on_demand_tools_table=self._on_demand_tools_table,
            on_demand_routing_rules=self._on_demand_routing_rules,
            on_demand_antipatterns=self._on_demand_antipatterns,
            coding_rule_num=self._coding_rule_num,
        )
        gep_template_section = self._build_gep_template_section()

        # Build directory section dynamically. When working_directory is None
        # (Windows GUI mode) we show only [Session Storage Directory] and adapt
        # the parenthetical note that distinguishes "user project" from session
        # storage — that distinction makes no sense without a user project.
        if self.working_directory:
            directory_block = (
                f"[Working Directory]\n{self.working_directory}\n"
                f"[Session Storage Directory]\n{self.storage_directory}\n"
            )
            directory_note = (
                " The working directory above is the user's project directory — "
                "prefer writing task artifacts to the session storage directory "
                "to avoid cluttering the user's workspace."
            )
        else:
            directory_block = f"[Session Storage Directory]\n{self.storage_directory}\n"
            directory_note = (
                " There is no separate user project directory in this session — "
                "every artifact goes here, and relative paths resolve against it."
            )

        messages = [
            {"role": "system", "content": system_prompt_content},
            {"role": "user", "content": OBSERVE_AND_PLAN_TEMPLATE.format(
                goal=goal,
                completed_count=len(completed_steps),
                loop_warning=loop_warning,
                epistemic_preamble=epistemic_preamble,
                completed_summary=self._build_completed_summary(completed_steps, self.detail_window),
                accumulated_findings_section=accumulated_findings_section,
                lookahead_summary=self._build_lookahead_summary(current_lookahead),
                directory_block=directory_block,
                directory_note=directory_note,
                gep_template_section=gep_template_section,
                user_instruction=user_instruction_block,
                step_verification_threshold=self.step_verification_threshold
            )}
        ]

        try:
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=messages),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"Planner LLM fallback to index {idx}: {e}",
                    component="Planner",
                ),
            ))
            self.last_token_count = _raw.total_tokens
            self.last_input_tokens = _raw.input_tokens
            data = _raw.content or ""
            plan = await Plan.from_data(goal=goal, raw_content=data, llm_services=self._from_data_services)
            plan.token_count = _raw.total_tokens

            # Structural guard: when signalling task completion (empty next_steps),
            # last_step_confidence must be explicitly set and >= threshold.
            # If the LLM omitted it or set it too low, inject a corrective step
            # rather than silently completing with unverified confidence.
            if not plan.next_steps:
                conf = plan.last_step_confidence
                if conf is None or conf < self.step_verification_threshold:
                    self.logger.warning(
                        f"Planner signalled completion but last_step_confidence="
                        f"{conf!r} < threshold={self.step_verification_threshold}. "
                        f"Injecting corrective step.",
                        component="Planner",
                    )
                    from ..models.plan import Step as _Step
                    corrective = _Step.from_planner(
                        step_id="corrective_confidence",
                        description="[Re-evaluate completion confidence]",
                        goal=(
                            "The planner signalled task completion but did not provide "
                            f"sufficient confidence (got {conf!r}, need >= "
                            f"{self.step_verification_threshold}). "
                            "Re-read the key artifacts produced so far, verify they "
                            "satisfy the original goal, and report your findings."
                        ),
                        expected_outcomes=[
                            "Key artifacts exist and are non-empty",
                            "Artifacts satisfy the original goal requirements",
                        ],
                        risk_assessment="Low risk — read-only re-evaluation step",
                        required_context_keys=[],
                    )
                    plan.next_steps = [corrective]
                    plan.last_step_confidence = None

            # Structural guard: enforce observation-before-action ordering.
            # If any step in next_steps carries an epistemic_inventory with ASSUMED
            # claims whose observation_step_id does not appear earlier in next_steps,
            # inject a corrective observation step before the offending action step.
            if plan.next_steps:
                scheduled_ids = set()
                corrected_steps = []
                from ..models.plan import Step as _Step
                for step in plan.next_steps:
                    inventory = getattr(step, 'epistemic_inventory', None) or []
                    for claim in inventory:
                        obs_id = claim.get('observation_step_id') if isinstance(claim, dict) else None
                        if (
                            isinstance(claim, dict)
                            and claim.get('tag') == 'ASSUMED'
                            and obs_id
                            and obs_id not in scheduled_ids
                        ):
                            self.logger.warning(
                                f"Epistemic guard: step '{step.step_id}' depends on "
                                f"ASSUMED claim '{claim.get('claim', '')[:60]}' but "
                                f"observation step '{obs_id}' is not scheduled before it. "
                                f"Injecting observation step.",
                                component="Planner",
                            )
                            obs_step = _Step.from_planner(
                                step_id=obs_id,
                                description=f"[Observe: verify '{claim.get('claim', '')[:50]}']",
                                goal=(
                                    f"Observation obligation injected by epistemic guard. "
                                    f"Verify the following assumed claim before proceeding: "
                                    f"{claim.get('claim', '')}. "
                                    f"Verifiability: {claim.get('verifiability', 'check with appropriate tool')}."
                                ),
                                expected_outcomes=[
                                    f"Assumed claim confirmed or refuted by tool output",
                                ],
                                risk_assessment="Low risk — observation-only step",
                                required_context_keys=[],
                            )
                            corrected_steps.append(obs_step)
                            scheduled_ids.add(obs_id)
                    scheduled_ids.add(step.step_id)
                    corrected_steps.append(step)
                if len(corrected_steps) != len(plan.next_steps):
                    plan.next_steps = corrected_steps

            self.logger.debug(
                f"observe_and_plan result — next_steps={len(plan.next_steps)}",
                component="Planner"
            )
            return plan
        except Exception as e:
            self.logger.error(f"observe_and_plan failed: {e}", component="Planner")
            raise

    # ── Goal-level acceptance synthesis ───────────────────────────────────────

    async def synthesize_acceptance(
        self,
        original_goal: str,
        completed_steps: List[Step],
        has_code_edits: bool,
        accumulated_findings: str = "",
        already_tested: bool = False,
    ) -> AcceptanceVerdict:
        """Single Planner-side LLM call that decides whether the completed work
        satisfies the user's ORIGINAL GOAL.

        Replaces the prior ``generate_verification_step`` flow which spawned an
        independent verification agent.  The new flow:

          • Per-step confidence is already validated by ``observe_and_plan`` for
            every step in ``completed_steps`` — that part is settled.
          • This method asks the goal-level question: do these N steps as a
            whole deliver what the user asked for?  Catches gaps at the seam
            between steps (e.g. user named attribute "v2" but no step
            explicitly verified the v2 attribute survives into the final
            artifact).
          • For tasks whose ground truth is reachable cheaply (code edits →
            py_compile / pytest / tsc), it may also propose ONE narrow shell
            test step.  ``has_code_edits`` is the gate; ``already_tested``
            prevents re-injection loops after the test step itself completes.

        No tool calls; one LLM call.  Fail-open on parse / LLM errors so a
        malformed response cannot indefinitely block task completion.

        Returns an :class:`AcceptanceVerdict` whose ``corrective_step`` and
        ``code_test_step`` are pre-built :class:`Step` objects ready to be
        injected into ``next_steps`` by the FlowController.
        """
        # ── Build per-step block ──────────────────────────────────────────────
        if completed_steps:
            step_lines: List[str] = []
            for i, s in enumerate(completed_steps, 1):
                expected = '; '.join(s.expected_outcomes) if s.expected_outcomes else '(none)'
                outcome = '; '.join(s.factual_outcome) if s.factual_outcome else '(none)'
                artifacts = ', '.join(s.artifacts) if s.artifacts else '(none)'
                findings = '; '.join(s.key_findings) if s.key_findings else '(none)'
                step_lines.append(
                    f"{i}. [{s.step_id}] {s.description}\n"
                    f"   Goal: {s.goal}\n"
                    f"   Expected: {expected}\n"
                    f"   Factual outcome: {outcome}\n"
                    f"   Artifacts: {artifacts}\n"
                    f"   Key findings: {findings}"
                )
            completed_steps_block = '\n'.join(step_lines)
        else:
            completed_steps_block = '(no completed steps)'

        accumulated_findings_block = (
            f"\n[Accumulated Findings]\n{accumulated_findings}\n"
            if accumulated_findings else ""
        )

        prompt = ACCEPTANCE_SYNTHESIS_TEMPLATE.format(
            original_goal=original_goal,
            completed_steps_block=completed_steps_block,
            accumulated_findings_block=accumulated_findings_block,
            has_code_edits=str(has_code_edits).lower(),
            already_tested=str(already_tested).lower(),
        )

        self.logger.info(
            f"Synthesizing acceptance verdict — "
            f"steps={len(completed_steps)}, "
            f"has_code_edits={has_code_edits}, "
            f"already_tested={already_tested}",
            component='Planner',
        )

        try:
            messages = [
                {"role": "system", "content": ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=messages),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"Acceptance synthesis LLM fallback to index {idx}: {e}",
                    component="Planner",
                ),
            ))
            response_text = _raw.content or ""
            verdict = await AcceptanceVerdict.from_data(response_text)

            # Defensive sanitisation of LLM violations of the prompt rules.
            # has_code_edits=False forbids code_test_step.
            if verdict.code_test_step is not None and not has_code_edits:
                self.logger.warning(
                    "Acceptance synthesis returned code_test_step but has_code_edits=False; "
                    "dropping the test step.",
                    component='Planner',
                )
                verdict.code_test_step = None
            # already_tested=True forbids code_test_step (loop guard).
            if verdict.code_test_step is not None and already_tested:
                self.logger.warning(
                    "Acceptance synthesis returned code_test_step but already_tested=True; "
                    "dropping the test step to avoid an injection loop.",
                    component='Planner',
                )
                verdict.code_test_step = None

            self.logger.info(
                f"Acceptance verdict: {verdict.verdict} "
                f"(gap={verdict.gap_summary[:80]!r}, "
                f"corrective={'yes' if verdict.corrective_step else 'no'}, "
                f"code_test={'yes' if verdict.code_test_step else 'no'})",
                component='Planner',
            )
            return verdict

        except Exception as e:
            self.logger.warning(
                f'synthesize_acceptance failed ({type(e).__name__}): {e}; '
                f'falling back to PASS to avoid blocking task completion.',
                component='Planner',
            )
            return AcceptanceVerdict(verdict='PASS')

    def _detect_loops(self, completed_steps: List[Step]) -> str:
        """
        Detect if the same step goal has been attempted multiple times with failures.

        A loop is when the planner keeps generating steps with the same goal
        that keep failing — indicating it's stuck rather than exploring.
        Returns a formatted warning string to inject into the prompt, or "" if
        no loop is detected.
        """
        if len(completed_steps) < 3:
            return ""

        goal_attempts: dict = {}
        for step in completed_steps:
            key = step.goal.lower().strip()[:80]
            if key not in goal_attempts:
                goal_attempts[key] = []
            goal_attempts[key].append(step.status == StepStatus.COMPLETED)

        repeated = [
            (goal, attempts)
            for goal, attempts in goal_attempts.items()
            if len(attempts) >= 2 and not all(attempts)
        ]

        if not repeated:
            return ""

        lines = ["⚠️ LOOP DETECTED — the following goals have been attempted multiple times:"]
        for goal_key, attempts in repeated[:3]:
            fail_count = sum(1 for s in attempts if not s)
            lines.append(
                f"  • '{goal_key[:70]}' — "
                f"{fail_count}/{len(attempts)} attempts failed"
            )
        lines.append(
            "  → You MUST try a fundamentally different approach "
            "(different tool, method, or decomposition) — not a minor variation.\n"
        )
        return "\n".join(lines) + "\n"

    # ── Epistemic inventory pre-pass ──────────────────────────────────────────

    def _build_epistemic_inventory_warning(
        self,
        goal: str,
        completed_steps: List[Step],
    ) -> str:
        """
        Builds the epistemic_preamble block for OBSERVE_AND_PLAN_TEMPLATE.

        Initial call (completed_steps empty):
          Scans goal text for external-world entity references and lists them
          as ASSUMED claims, reminding the LLM to schedule observation steps
          before acting on them.

        Post-exploration (completed_steps non-empty, recent findings exist):
          Injects a concretization requirement reminding the planner to rewrite
          subsequent step goals using concrete values confirmed by tool output.
          Only fires for early steps (≤ 6) to avoid noise in later replans.

        This is injected into OBSERVE_AND_PLAN_TEMPLATE as {epistemic_preamble}.
        """
        if completed_steps:
            # Fire exactly once: after the first step completes (the exploration step).
            # That is the single most important moment to force goal concretization —
            # the planner just received real findings and should rewrite subsequent
            # step goals from abstract to concrete. After step 1 the system prompt's
            # "Goal grounding" rule carries it forward; no need to repeat in the prompt.
            if len(completed_steps) == 1 and (
                completed_steps[0].key_findings or completed_steps[0].factual_outcome
            ):
                return (
                    "\n[Goal Grounding Requirement]\n"
                    "The first step has completed with observed findings (see [Completed Work] "
                    "and [Accumulated Task Findings] above). "
                    "REQUIREMENT: every next_steps[i].goal MUST now use concrete values "
                    "confirmed by that output — actual file paths, exact function names, "
                    "confirmed command syntax. Abstract goals ('fix the bug', 'read "
                    "relevant files') are not acceptable when the concrete target is "
                    "now known from observations.\n"
                )
            return ""

        import re

        # Heuristic patterns for external-world entity references in natural language
        # Each pattern targets a different claim type.
        patterns = [
            # File/directory paths: sequences with / or \ and .ext
            (r'[\w./\-]+/[\w./\-]+', 'file/directory path'),
            (r'[\w.\\\-]+\\[\w.\\\-]+', 'file/directory path'),  # Windows backslash paths
            (r'[A-Za-z]:\\[\w.\\\- ]+', 'file/directory path'),  # Windows drive paths (C:\...)
            (r'[\w\-]+\.(?:py|js|ts|json|yaml|yml|toml|cfg|ini|sh|md|txt|csv|sql|env)', 'file name'),
            # Environment variables: ALL_CAPS_WITH_UNDERSCORES
            (r'\b[A-Z][A-Z0-9_]{2,}\b', 'environment variable or constant'),
            # API-like paths: /word/word patterns
            (r'/[a-z][a-z0-9_/\-]{2,}', 'API endpoint path'),
        ]

        assumed_claims = []
        seen = set()
        for pattern, claim_type in patterns:
            for match in re.finditer(pattern, goal):
                entity = match.group(0)
                if entity in seen:
                    continue
                seen.add(entity)
                assumed_claims.append((entity, claim_type))

        if not assumed_claims:
            return ""

        lines = [
            "⚠️ EPISTEMIC INVENTORY — the following entities from the task description "
            "have not yet been confirmed by tool output (tagged ASSUMED):"
        ]
        for entity, claim_type in assumed_claims[:10]:  # cap at 10 to avoid prompt bloat
            lines.append(f"  • [{claim_type}] '{entity}' — ASSUMED (not yet observed in tool output)")
        lines.append(
            "  → Per the Epistemic State Separation requirement: for each ASSUMED claim "
            "with non-trivial risk, schedule an observation step BEFORE the action step "
            "that depends on it.\n"
        )
        return "\n".join(lines) + "\n"

    # ── Summary builders ──────────────────────────────────────────────────────

    def _build_completed_summary(
        self,
        completed_steps: List[Step],
        detail_window: int = 4
    ) -> str:
        """
        Builds the execution history summary for the Planner.

        Context budget strategy — different detail levels for different steps:

          Early steps (beyond detail_window from the end):
            One-liner (status + description).  The Planner only needs to know
            these happened; re-examining their tool outputs adds no value.

          Recent steps (last detail_window, excluding the most recent):
            • Failed steps  → full detail via to_planner_summary(compact=False).
            • Successful steps → compact detail via to_planner_summary(compact=True).

          Most recent step (always full detail):
            This is the step the Planner must evaluate for last_step_confidence.
        """
        if not completed_steps:
            return "(no completed steps yet)"

        history = completed_steps[:-1]
        last_step = completed_steps[-1]
        lines = []

        if history:
            early = history[:-detail_window] if len(history) > detail_window else []
            recent_history = history[-detail_window:] if len(history) > detail_window else history

            if early:
                # Split early steps into cached (compressed) and uncached (one-liners).
                cached_count = min(self._compressed_up_to_count, len(early))
                cached_early = early[:cached_count]
                uncached_early = early[cached_count:]

                if cached_early and self._compressed_steps_cache:
                    lines.append("[Compressed History]")
                    for entry in self._compressed_steps_cache:
                        covers_str = ", ".join(entry.get("covers") or [])
                        lines.append(f"  [{covers_str}] {entry.get('summary', '')}")
                        artifacts = entry.get("artifacts") or []
                        if artifacts:
                            lines.append(f"    Artifacts: {', '.join(artifacts)}")
                        findings = entry.get("key_findings") or []
                        for kf in findings[:3]:
                            lines.append(f"    • {kf}")

                if uncached_early:
                    lines.append("[Earlier steps]")
                    offset = cached_count
                    for i, s in enumerate(uncached_early, offset + 1):
                        status = "COMPLETED" if s.status == StepStatus.COMPLETED else "FAIL"
                        line = f"  {i}. {status} {s.description}"
                        if s.factual_outcome:
                            line += f" — {'; '.join(s.factual_outcome)}"
                        lines.append(line)
                elif not cached_early:
                    # No early steps at all (shouldn't reach here, but guard)
                    pass

            if recent_history:
                lines.append("\n[Recent steps]")
                offset = len(early)
                for i, s in enumerate(recent_history, offset + 1):
                    use_compact = s.status == StepStatus.COMPLETED
                    lines.append(f"\n{i}. {s.to_planner_summary(compact=use_compact)}")

        last_idx = len(completed_steps)
        lines.append(f"\n[Most Recent Step — set last_step_confidence based on this]")
        lines.append(f"{last_idx}. {last_step.to_planner_summary(compact=False)}")

        return "\n".join(lines)

    def _build_lookahead_summary(self, lookahead: List[Step]) -> str:
        if not lookahead:
            return "(none)"
        return "\n".join(
            f"{i}. [step_id={s.step_id}] {s.description} — goal: {s.goal}"
            for i, s in enumerate(lookahead, 1)
        )

    def _build_gep_template_section(self) -> str:
        """
        Build a [Historical Execution Template] reference block for the planner prompt.

        The template is shown as structural reference material so the planner can
        understand the proven step sequence and adapt it to the user's actual goal.
        Unlike passing pre-instantiated steps as lookahead, this lets the planner
        resolve parameter placeholders and adjust granularity based on current context.
        Returns an empty string when no template is loaded.
        """
        t = self._gep_template
        if t is None:
            return ""

        name = t.get('name') if isinstance(t, dict) else t.name
        description = t.get('description', '') if isinstance(t, dict) else t.description
        guide_steps = t.get('guide_steps', []) if isinstance(t, dict) else t.guide_steps
        params_schema = t.get('params_schema', {}) if isinstance(t, dict) else getattr(t, 'params_schema', {})

        lines = [
            f"\n[Historical Execution Template: {name}]",
            f"{description}",
        ]

        if params_schema:
            lines.append("Template parameters (may appear as {{params.X}} in step goals):")
            for k, v in params_schema.items():
                if isinstance(v, dict):
                    desc = v.get('description', '')
                    default = v.get('default')
                else:
                    desc = getattr(v, 'description', '')
                    default = getattr(v, 'default', None)
                default_str = f", default: {default}" if default is not None else ""
                emphasis_str = " ▶" if (v.get('emphasis', False) if isinstance(v, dict) else getattr(v, 'emphasis', False)) else ""
                lines.append(f"  - {k}: {desc}{default_str}{emphasis_str}")

        lines.append(
            "Reference steps (proven sequence from a prior successful run — "
            "adapt to the user's actual goal; resolve {{params.X}} from context):"
        )
        for i, raw in enumerate(guide_steps, 1):
            if isinstance(raw, dict):
                sid = raw.get('step_id', f'gep_step_{i}')
                desc = raw.get('description', '')
                goal = raw.get('goal', '')
                expected = raw.get('expected_outcomes', [])
            else:
                sid = getattr(raw, 'step_id', f'gep_step_{i}')
                desc = getattr(raw, 'description', '')
                goal = getattr(raw, 'goal', '')
                expected = getattr(raw, 'expected_outcomes', [])
            lines.append(f"{i}. [{sid}] {desc}")
            lines.append(f"   Goal: {goal}")
            if expected:
                lines.append(f"   Expected: {'; '.join(expected)}")

        lines.append("")
        return "\n".join(lines) + "\n"


# ── Planner-level progress tracker ───────────────────────────────────────────

class PlannerProgressTracker(ProgressAnalyzerBase):
    """
    Planner-level pattern analyzer (step granularity).

    Used by FlowController to detect when the Planner is stuck and inject
    graduated reminders into observe_and_plan() via user_message.

    Graduation levels (by consecutive failures):
      moderate_stagnation_threshold  → warning: try a different approach
      severe_stagnation_threshold    → critical: fundamentally different strategy required
      feasibility_assessment_threshold → ask the planner to explicitly assess feasibility
      abort_threshold                → hard abort (safety net only)
    """

    def _default_config(self) -> dict:
        return {
            "window_size": 5,
            "moderate_stagnation_threshold": 3,      # warning reminder
            "severe_stagnation_threshold": 5,         # critical reminder
            "feasibility_assessment_threshold": 7,    # explicit feasibility check
            "abort_threshold": 10,                    # hard abort (last resort)
            "enable_reminders": True,
        }

    # ── Public API ───────────────────────────────────────────────────────────

    def add_step_result(self, success: bool) -> None:
        """Record the outcome of a completed step (alias for add_result)."""
        self.add_result(success)

    def analyze(self) -> PlannerProgressStatus:
        """
        Analyse the current step history and return a status signal.

        Returns:
            PlannerProgressStatus with should_inject_reminder / should_abort /
            should_assess_feasibility flags and the corresponding message.
        """
        consecutive_failures = self._count_consecutive_failures()
        success_rate = self._get_success_rate()

        # ── Hard abort (highest priority, safety net only) ────────────────────
        if consecutive_failures >= self.config["abort_threshold"]:
            return PlannerProgressStatus(
                should_inject_reminder=False,
                should_abort=True,
                abort_reason=(
                    f"{consecutive_failures} consecutive step failures "
                    f"(success rate {success_rate:.1%}) — "
                    f"task appears unachievable with the current approach"
                ),
            )

        # ── Feasibility assessment (before hard abort) ────────────────────────
        if consecutive_failures >= self.config["feasibility_assessment_threshold"]:
            return PlannerProgressStatus(
                should_inject_reminder=True,
                should_abort=False,
                should_assess_feasibility=True,
                reminder_message=self._generate_feasibility_assessment(
                    consecutive_failures, success_rate
                ),
            )

        # ── Graduated reminder injection ──────────────────────────────────────
        should_remind = self._should_add_reminder(consecutive_failures)
        reminder = (
            self._generate_reminder(consecutive_failures, success_rate)
            if should_remind else None
        )

        return PlannerProgressStatus(
            should_inject_reminder=should_remind,
            should_abort=False,
            reminder_message=reminder,
        )

    # ── Reminder content ──────────────────────────────────────────────────────

    def _generate_warning_reminder(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        return (
            f"📊 {consecutive_failures} consecutive step failures "
            f"(success rate: {success_rate:.1%}). "
            f"The current approach may not be working — "
            f"consider a different decomposition, tool, or method for the next step."
        )

    def _generate_critical_reminder(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        return (
            f"⚠️ {consecutive_failures} consecutive step failures "
            f"(success rate: {success_rate:.1%}). "
            f"The current strategy is not working. "
            f"Pivot to a fundamentally different approach — "
            f"different tools, different decomposition, or a different angle entirely. "
            f"Do not retry what has already failed."
        )

    def _generate_feasibility_assessment(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        return (
            f"🔍 {consecutive_failures} consecutive step failures "
            f"(success rate: {success_rate:.1%}). "
            f"Critically assess whether this task is achievable with the available tools and information. "
            f"If yes, design a fundamentally different approach. "
            f"If the task is genuinely infeasible, return empty next_steps with "
            f"last_step_confidence=1.0 and completion_reason explaining why — "
            f"the system will treat this as task done."
        )
