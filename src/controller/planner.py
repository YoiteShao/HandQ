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
    OBSERVE_AND_PLAN_TEMPLATE,
    VERIFICATION_STEP_SYSTEM_PROMPT,
    VERIFICATION_STEP_TEMPLATE,
    VERIFICATION_TIER_LIGHT,
    VERIFICATION_TIER_STANDARD,
    VERIFICATION_TIER_ADVERSARIAL,
    VERIFICATION_GOAL_SUFFIX_LIGHT,
    VERIFICATION_GOAL_SUFFIX_STANDARD,
    VERIFICATION_GOAL_SUFFIX_ADVERSARIAL,
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


# ── Verification step dataclass ──────────────────────────────────────────────

@dataclass
class VerificationStep:
    """Structured result of the LLM call in generate_verification_step.

    Fields mirror the JSON keys returned by the LLM:
      should_verify      — whether a verification step should be injected at all.
      goal               — the verification goal text (nested under verification_step).
      step_id            — identifier for the step (nested under verification_step).
      description        — human-readable label (nested under verification_step).
      rationale          — failure patterns being targeted (nested under verification_step).
      skip_reason        — why verification was skipped (only when should_verify=False).
    """
    should_verify: bool = False
    goal: str = ""
    step_id: str = "verification"
    description: str = "Adversarial acceptance check"
    rationale: str = ""
    skip_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> 'VerificationStep':
        """Create a VerificationStep from a parsed LLM response dict."""
        verification_info = data.get('verification_step') or {}
        return cls(
            should_verify=data.get('should_verify', False),
            goal=verification_info.get('goal', '').strip(),
            step_id=(
                verification_info.get('step_id', 'verification').strip()
                or 'verification'
            ),
            description=(
                verification_info.get('description', 'Adversarial acceptance check').strip()
                or 'Adversarial acceptance check'
            ),
            rationale=verification_info.get('rationale', '').strip(),
            skip_reason=data.get('skip_reason', '').strip(),
        )

    @classmethod
    async def from_data(cls, raw_content: str) -> 'VerificationStep':
        """Parse a raw LLM response string into a VerificationStep.

        Uses try_parse_json (already imported) to handle JSON fence-stripping
        and parsing.  Returns a default (should_verify=False) instance if
        parsing fails or the result is not a dict.
        """
        parsed = try_parse_json(raw_content)
        if isinstance(parsed, dict):
            return cls.from_dict(parsed)
        return cls()



# ── Verification helpers ──────────────────────────────────────────────────────

def _build_verifier_context(completed_steps: List[Step], tier: str) -> str:
    """
    Build tier-appropriate context for the verification planning LLM.

    Artifacts are labelled by their originating step so the verifier can
    distinguish intermediate files (produced by earlier steps) from the
    primary result (the artifact that directly satisfies the goal).

    Fix #2 — final step has no local artifacts (e.g. upload/deploy step):
      If the logical final step produced no local artifacts and is NOT an
      aggregation step, walk backwards to find the last artifact-producing
      step and treat its output as PRIMARY.  This prevents intermediate files
      from being mislabelled when the last step was a side-effect-only
      operation (SSH push, remote deploy, API call with no local output).

    Fix #4 — no local artifacts at all (SSH/remote/API/DB tasks):
      When the entire task produced no local artifacts, include key_findings
      from all steps labelled as REMOTE CONTEXT — navigation only.  This gives
      the verifier job IDs, log paths, and remote host references so it can
      locate the remote state to check, without treating agent conclusions as
      evidence of correctness.

    What is passed by tier:
      light       — PRIMARY RESULT paths only (2-check spot-test; no need for
                    intermediate context).
      standard    — PRIMARY RESULT + INTERMEDIATE FILE paths labelled by
                    provenance.  Remote context included when no local artifacts.
      adversarial — Same as standard; verifier derives criteria from goal only.

    key_findings are withheld for local-artifact tasks (they are the agent's
    narrative and anchor the verifier on the agent's framing).  They are
    included only for no-artifact tasks where they are the sole locating signal.
    factual_outcome is never passed.
    """
    if not completed_steps:
        return ""

    # ── Identify the logical final step ──────────────────────────────────────
    # Prefer the last aggregation step; fall back to the last step overall.
    final_idx = len(completed_steps) - 1
    for i in range(len(completed_steps) - 1, -1, -1):
        if getattr(completed_steps[i], 'is_aggregation', False):
            final_idx = i
            break

    # Fix #2: if the logical final step is NOT an aggregation and has no
    # local artifacts (e.g. it was a deploy/upload/SSH push step), walk
    # backwards to the last step that actually produced local files.
    # Aggregation steps are exempt — if they produced no local artifact
    # that is intentional (they may only update remote state).
    final_is_aggregation = getattr(completed_steps[final_idx], 'is_aggregation', False)
    if not completed_steps[final_idx].artifacts and not final_is_aggregation:
        for i in range(len(completed_steps) - 1, -1, -1):
            if completed_steps[i].artifacts:
                final_idx = i
                break

    final_step = completed_steps[final_idx]
    final_artifacts = list(final_step.artifacts or [])
    intermediate_steps = [s for i, s in enumerate(completed_steps) if i != final_idx]

    # Intermediate artifact paths with provenance labels (deduped against final)
    intermediate_artifacts: List[tuple] = []
    for s in intermediate_steps:
        for p in (s.artifacts or []):
            if p not in final_artifacts:
                intermediate_artifacts.append((s.description, p))

    all_local_paths = final_artifacts + [p for _, p in intermediate_artifacts]

    # Fix #4: no local artifacts at all → remote/SSH/API/DB task.
    # Include key_findings as navigation context (job IDs, log paths, hosts)
    # so the verifier can locate what to check.  Label clearly as unverified.
    if not all_local_paths:
        remote_lines: List[str] = []
        for s in completed_steps:
            for kf in (s.key_findings or [])[:3]:
                remote_lines.append(f"  · {kf}")
        if not remote_lines:
            return ""
        header = [
            "[REMOTE OPERATION CONTEXT — navigation only, not evidence]",
            "No local artifacts were produced (remote/SSH/API/DB task).",
            "Use these findings only to locate what to check (job IDs, log",
            "paths, remote hosts). Verify the actual remote state independently",
            "against the original goal — do NOT treat these as evidence of success.",
            "",
        ]
        return "\n".join(header + remote_lines) + "\n\n"

    # ── Local artifact task: build labelled context ───────────────────────────
    if tier == 'light':
        if not final_artifacts:
            return ""
        lines = ["[PRIMARY RESULT — check these against the goal]"]
        for p in final_artifacts:
            lines.append(f"  • {p}")
        return "\n".join(lines) + "\n\n"

    # standard / adversarial
    lines: List[str] = []
    has_content = False

    if final_artifacts:
        lines.append("[PRIMARY RESULT — verify these against the goal]")
        for p in final_artifacts:
            lines.append(f"  • {p}")
        has_content = True

    if intermediate_artifacts:
        lines.append("")
        lines.append("[INTERMEDIATE FILES — read as shortcuts; do NOT verify as goals]")
        lines.append("These were produced by earlier steps. READ them directly to avoid")
        lines.append("re-running expensive operations. Your verdict must trace back to the")
        lines.append("PRIMARY RESULT above, not to these files.")
        for desc, p in intermediate_artifacts:
            lines.append(f"  · {p}  (from: {desc})")
        has_content = True

    if not has_content:
        return ""
    return "\n".join(lines) + "\n\n"


# ── Verification tier determination ───────────────────────────────────────────

def _count_effective_iterations(steps: List[Step]) -> int:
    """
    Count "effective" (mutating) iterations across all steps.

    An iteration is effective when the tool used is a write/edit/bash operation
    that modifies state.  Pure read-only iterations (find, grep, ls, cat, read,
    head, tail, wc, test, echo, python read-only scripts, etc.) are exploratory
    overhead — they do NOT indicate that the task was complex or risky, and
    should not inflate the verification tier.

    The tool name comes from _metrics_tools_used entries, which are formatted as
    "<tool_name>: <truncated_input>" by RuntimeAgent._format_tool_entry().
    We extract the tool name prefix and classify it.
    """
    # Tools that are purely read-only / exploratory — do not count toward
    # effective iterations.
    _READ_ONLY_TOOLS = frozenset({
        "read", "glob", "grep", "bash_read", "shell_read",
    })
    # Bash/shell commands that are read-only by nature (prefix match on the command).
    _READ_ONLY_BASH_PREFIXES = (
        "find ", "grep ", "ls ", "cat ", "head ", "tail ", "wc ",
        "test ", "echo ", "stat ", "file ", "du ", "df ", "pwd",
        "which ", "type ", "env ", "printenv ", "sort ", "uniq ",
        "diff ", "comm ", "cut ", "awk ", "sed -n", "python3 -c \"import",
        "Get-ChildItem", "Select-String", "Test-Path", "Get-Content",
        "Get-Location",
        "#",  # comment-only lines (no-op probes)
    )
    # SSH actions that are purely observational — polling or reading remote state.
    _SSH_READONLY_ACTIONS = frozenset({"job_status", "tail_log", "fetch_log"})

    effective = 0
    for s in steps:
        tools: List[str] = getattr(s, '_metrics_tools_used', [])
        for entry in tools:
            # entry format: "<tool_name>: <input>" or just "<tool_name>"
            tool_name = entry.split(":", 1)[0].strip().lower()
            if tool_name in _READ_ONLY_TOOLS:
                continue
            if tool_name in ("bash", "shell"):
                # Inspect the command text after "bash: " or "shell: "
                cmd = entry.split(":", 1)[1].strip() if ":" in entry else ""
                if any(cmd.startswith(pfx) for pfx in _READ_ONLY_BASH_PREFIXES):
                    continue
            if tool_name == "ssh":
                # Classify SSH sub-action: job_status / tail_log / fetch_log are
                # read-only polling; everything else (exec, exec_bg, write_file,
                # run_script, safe_exit) mutates remote state.
                action = entry[len("ssh:"):].strip().split()[0].lower() if ":" in entry else ""
                if action in _SSH_READONLY_ACTIONS:
                    continue
            effective += 1
    return effective


def _determine_verification_tier(completed_steps: List[Step]) -> str:
    """
    Determine the verification depth tier based on execution signals.

    Returns one of: "skip" | "light" | "standard" | "adversarial"

    Decision matrix (evaluated in priority order):

      skip        — no local artifacts AND no remote mutations (SSH or otherwise);
                    task was purely read-only or conversational.
                    Calling the verification LLM would add latency with zero value.

      adversarial — any of the following high-risk signals:
                    • any step in history failed (agent may be masking errors)
                    • total artifacts > 3 (multi-file change, broad blast radius)
                    • avg effective iterations per step > 8 (agent genuinely
                      struggled with the mutation work, not just exploration)
                    • step count > 5 (complex multi-phase task)
                    • planner-declared high-risk operation

                    NOTE: "effective iterations" excludes read-only tool calls
                    (find, grep, ls, cat, SSH job_status/tail_log/fetch_log, etc.).
                    A step that spent 15 iterations on grep/find discovery but only
                    2 on actual writes is NOT adversarial — the verification agent
                    can trust the execution artifacts and verify them directly.

                    NOTE: SSH-primary tasks have no local artifacts but still mutate
                    remote state.  effective_iterations > 0 is sufficient to trigger
                    verification; tier is then determined by step/iteration counts.

      standard    — moderate scope:
                    • 2–5 steps, OR 2–3 artifacts, OR avg effective iters 4–8

      light       — everything else: single step, ≤1 artifact, low effective
                    iterations, no failure history.
    """
    if not completed_steps:
        return "skip"

    all_artifacts: List[str] = []
    had_failures = False

    for s in completed_steps:
        all_artifacts.extend(s.artifacts or [])
        if s.has_status(StepStatus.FAILED):
            had_failures = True

    artifact_count = len(all_artifacts)
    step_count = len(completed_steps)

    # Effective (mutating) iterations — excludes read-only exploration and
    # SSH polling actions (job_status / tail_log / fetch_log).
    # Computed before the skip check so SSH-primary tasks (no local artifacts
    # but remote mutations) are not incorrectly skipped.
    effective_iterations = _count_effective_iterations(completed_steps)
    avg_effective_iters = effective_iterations / step_count

    # skip — no local artifacts AND no remote-state mutations at all
    if artifact_count == 0 and effective_iterations == 0:
        return "skip"

    # adversarial — any high-risk signal (including planner-declared risk)
    _HIGH_RISK_KEYWORDS = (
        "production", "irreversible", "delete", "drop", "truncate",
        "overwrite", "external api", "database", "credential", "secret",
    )
    had_high_risk_step = any(
        any(kw in (getattr(s, 'risk_assessment', '') or '').lower()
            for kw in _HIGH_RISK_KEYWORDS)
        for s in completed_steps
    )
    if (
        had_failures
        or artifact_count > 3
        or avg_effective_iters > 8
        or step_count > 5
        or had_high_risk_step
    ):
        return "adversarial"

    # standard — moderate scope
    if step_count >= 2 or artifact_count >= 2 or avg_effective_iters >= 4:
        return "standard"

    # light — single step, single artifact, low effective iterations
    return "light"


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

        system_prompt_content = PLANNER_SYSTEM_PROMPT
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

    # ── Verification step generation ──────────────────────────────────────────

    async def generate_verification_step(
        self,
        original_goal: str,
        completed_steps_summary: str,
        completed_steps: Optional[List[Step]] = None,
    ) -> VerificationStep:
        """Generate a tiered verification step independent of the agent's self-reporting.

        Tier is determined by _determine_verification_tier() before any LLM call:
          skip        — no artifacts; returns should_verify=False immediately (zero LLM cost).
          light       — single step, single artifact, low iterations; fast spot-check.
          standard    — moderate scope; existence + goal-satisfaction checks, no full claim table.
          adversarial — high risk (failures, many steps/artifacts/iterations); full framework.

        Returns a VerificationStep dataclass. Callers should check .should_verify:
          - True  → build a Step from the fields and inject it
          - False → skip verification; .skip_reason explains why

        Design rationale (addresses W1–W7 from verification architecture analysis):

        W3/W7 — Independence: criteria are derived from ``original_goal`` (the only trusted
          input).  ``completed_steps_summary`` is passed to the LLM labelled as
          "UNVERIFIED AGENT CLAIMS" so the verifier treats it as hypotheses to falsify,
          not as evidence of completion.

        W1/W2 — Adversarial stance: VERIFICATION_STEP_SYSTEM_PROMPT instructs the LLM to
          *attempt to falsify* the result and explicitly lists known silent failure patterns
          (type errors invisible to py_compile, circular grep, wrong API signatures).

        W4 — Structured verdict: the generated goal mandates a machine-readable
          VERIFICATION REPORT block with per-criterion PASS/FAIL, verbatim evidence,
          confidence score, and an explicit "what was NOT checked" section.

        W4/W7 — Task-type awareness: a ``task_type_hint`` derived from the original goal
          is prepended to the user prompt so the LLM applies the correct negative checks
          (coding tasks → mypy/import test/API source read; general tasks → structural
          requirement checks from the goal).

        W5 — Silent failure: exceptions are caught and logged; a default VerificationStep
          with should_verify=False is returned so the session completes cleanly.
        """
        # ── Tier determination (pre-LLM, zero cost) ───────────────────────────
        tier = _determine_verification_tier(completed_steps or [])
        _eff_iters = _count_effective_iterations(completed_steps or [])
        _n_steps = len(completed_steps or [])
        self.logger.info(
            f'Verification tier: {tier} '
            f'(steps={_n_steps}, '
            f'artifacts={sum(len(s.artifacts or []) for s in (completed_steps or []))}, '
            f'effective_iters={_eff_iters}, '
            f'avg_effective={_eff_iters / _n_steps:.1f} per step'
            f')',
            component='Planner',
        )

        if tier == 'skip':
            return VerificationStep(
                should_verify=False,
                skip_reason='No artifacts produced — read-only task, verification not needed.',
            )

        # ── Task-type hint ────────────────────────────────────────────────────
        # Prepend a brief task-type hint so the planning LLM selects the right
        # check patterns (py_compile for code, test -f for files, etc.).
        _goal_lower = original_goal.lower()
        _coding_keywords = (
            'python', 'code', 'function', 'method', 'class', 'script',
            'module', 'import', 'def ', '.py', 'edit ', 'modify ', 'implement',
            'refactor', 'bug', 'fix ', 'patch', 'type', 'api', 'call',
        )
        _is_coding_task = any(kw in _goal_lower for kw in _coding_keywords)
        task_type_hint = (
            "TASK TYPE: Code modification / software engineering.\n"
            "Key checks: py_compile; grep for the changed symbol; import or call test.\n\n"
            if _is_coding_task else
            "TASK TYPE: General / content / file task.\n"
            "Key checks: test -f + wc -l; read key sections; grep for expected value.\n\n"
        )

        # ── Tier-specific depth instruction ───────────────────────────────────
        _TIER_PROMPTS = {
            'light':       VERIFICATION_TIER_LIGHT,
            'standard':    VERIFICATION_TIER_STANDARD,
            'adversarial': VERIFICATION_TIER_ADVERSARIAL,
        }
        tier_instruction = _TIER_PROMPTS[tier]

        # ── Build prompt ──────────────────────────────────────────────────────
        # Verifier context is tier-aware:
        #   light/adversarial → artifact paths only (no agent claims)
        #   standard          → paths + key_findings as labeled hypotheses
        # factual_outcome is never passed (it is the agent's self-assessment).
        verifier_context = _build_verifier_context(completed_steps or [], tier)
        prompt = (
            task_type_hint
            + tier_instruction
            + verifier_context
            + VERIFICATION_STEP_TEMPLATE.format(
                original_goal=original_goal,
                completed_steps_summary=completed_steps_summary,
                tier=tier.upper(),
            )
        )

        try:
            # Use the same LLM call pattern as observe_and_plan
            messages = [
                {"role": "system", "content": VERIFICATION_STEP_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            _raw = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(messages=messages),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"Planner verification LLM fallback to index {idx}: {e}",
                    component="Planner",
                ),
            ))
            response_text = _raw.content or ""

            vs = await VerificationStep.from_data(response_text)

            if not vs.should_verify:
                return vs  # caller reads .skip_reason for logging

            goal = vs.goal
            if not goal:
                # LLM said should_verify=True but provided no goal — treat as skip.
                vs.should_verify = False
                vs.skip_reason = "LLM returned should_verify=true but no goal was provided"
                return vs

            step_id = vs.step_id
            description = vs.description
            rationale = vs.rationale

            # ── Assemble full goal (tier-aware) ──────────────────────────────
            # Prefix and required output format are proportionate to tier so the
            # executing agent produces a lightweight report for simple tasks and
            # the full adversarial report only for high-risk ones.
            _tier_prefix = '[ADVERSARIAL VERIFICATION]' if tier == 'adversarial' else '[VERIFICATION]'
            full_goal = _tier_prefix + ' ' + goal
            if rationale:
                full_goal += (
                    f'\n\n[Verification rationale — failure patterns being targeted]\n'
                    f'{rationale}'
                )
            _tier_suffixes = {
                'light':       VERIFICATION_GOAL_SUFFIX_LIGHT,
                'standard':    VERIFICATION_GOAL_SUFFIX_STANDARD,
                'adversarial': VERIFICATION_GOAL_SUFFIX_ADVERSARIAL,
            }
            full_goal += _tier_suffixes.get(tier, VERIFICATION_GOAL_SUFFIX_STANDARD)

            # Attach the assembled Step onto the VerificationStep so the caller
            # can use it directly without re-constructing it.
            vs._step = Step.from_planner(  # type: ignore[attr-defined]
                step_id=step_id,
                description=description,
                goal=full_goal,
                risk_assessment="Low risk — adversarial read-only verification step",
                required_context_keys=[],
            )
            return vs

        except Exception as e:
            self.logger.warning(
                f'generate_verification_step failed ({type(e).__name__}): {e}, '
                f'skipping verification',
                component='Planner',
            )
            return VerificationStep(
                should_verify=False,
                skip_reason=f'Exception during generation ({type(e).__name__}): {e}',
            )

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
        First-pass epistemic pre-pass — runs only on the initial planning call
        (completed_steps empty).

        Scans the goal text for noun phrases that name external-world entities
        (file paths, API names, table names, env vars, etc.) and lists them as
        ASSUMED claims to remind the LLM to schedule observation steps before
        acting on them.

        On subsequent calls (completed_steps non-empty) the LLM already has the
        full completed_summary to reason about confirmed vs unconfirmed state, so
        this function returns "" and defers to that richer source of truth.

        This is injected into OBSERVE_AND_PLAN_TEMPLATE as {epistemic_preamble}.
        """
        if completed_steps:
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
