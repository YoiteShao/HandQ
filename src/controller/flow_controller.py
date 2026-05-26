"""
Flow Controller — orchestrates Planner loop and Agent loop as independent asyncio coroutines.

Communication primitives (reinitialised per task):
  _agent_step_batch  : Planner → Agent  (step batch to execute; None = stop signal)
  _step_batch_event  : Planner → Agent  (signals batch ready or stop)
  _completed_step    : Agent  → Planner (finished step awaiting collection)
  _interrupt_event   : Planner → Agent  (abort current step before next Think call)
  _agent_idle_event  : shared  (cleared by Planner at feed time; set by Agent on completion)

Planner loop responsibilities:
  • Drains REPLAN message queue and incorporates messages into observe_and_plan().
  • Calls observe_and_plan() on every new step result or REPLAN message.
  • Owns all memory writes (memory.add_step).
  • Feeds step batches to the Agent loop via _agent_step_batch / _step_batch_event.
  • Sets _interrupt_event to abort the running step when the Planner requests it.

Agent loop responsibilities:
  • Waits on _step_batch_event, executes the batch, stores result in _completed_step.
  • Checks _interrupt_event before every Think call; aborts with PLANNER_INTERRUPT if set.

Message processor responsibilities:
  • Runs as an independent asyncio.Task (started per task in _plan_and_execute).
  • Drains _user_message_queue, calls planner.evaluate_user_message() via callback.
  • Displays the planner's response immediately — independent of the Planner loop.
  • Routes REPLAN messages to _replan_queue; RESPOND_ONLY messages are done.
  • When the Planner is blocked on an LLM call the processor runs at the next
    asyncio await point, so the user always gets timely feedback.

When does the Planner loop block?
  • await planner.observe_and_plan(...)      — LLM call (seconds to tens of seconds)
  • await planner.evaluate_user_message(...) — removed from Planner loop; now done
                                               by the message processor concurrently
  • await _agent_idle_event.wait()           — waiting for agent after interrupt
  • await asyncio.sleep(_LOOP_SLEEP_INTERVAL) — short polling sleep (20 ms)
  During all of these the message processor coroutine can run, giving the user
  immediate feedback without waiting for the Planner loop to cycle.

Interrupt granularity: before each Think call. A long-running tool completes first.

Parallel interrupt isolation (Method C):
  • Each parallel sub-agent receives its own per-agent asyncio.Event (not _interrupt_event).
  • _broadcast_interrupt watches _interrupt_event and propagates it to all per-agent events.
  • The aggregation step that follows asyncio.gather uses _interrupt_event directly and is
    therefore NOT interrupted when the Planner fires _interrupt_event for the parallel batch.
  • This guarantees that sub-task results collected in results_text are always summarised by
    the aggregation agent, even when the Planner interrupts mid-batch.

Task completion: last_step_confidence >= step_verification_threshold AND next_steps empty.

Memory ownership: all memory.add_step calls are made exclusively by the Planner loop.

Progress tracking:
  • Normal path   — confidence-based (True if confidence >= threshold).
  • Interrupt path — step execution status (True if COMPLETED), no extra observe_and_plan.

Evaluate callback (_make_evaluate_callback):
  • Created once per task in _plan_and_execute; captures goal and reads
    self.current_plan at call time for current_step / lookahead context.
  • self.current_plan is already an instance variable updated by every
    observe_and_plan() call — no additional shared-state object is needed.
"""
import asyncio
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..agent.runtime_agent import RuntimeAgent
from ..infrastructure.llm_service import LLMService
from ..infrastructure.logger import get_logger, set_agent_context
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.execution_recorder import ExecutionRecorder
from ..infrastructure.metrics_collector import MetricsCollector
from ..infrastructure.memory import Memory
from ..infrastructure.step_context_provider import StepContextProvider
from ..models.agent_result import AgentResult
from ..models.plan import Plan, Step, StepStatus
from ..models.state import SystemState
from ..models.token_usage import TokenUsage
from .planner import Planner, PlannerProgressTracker
from .receptionist import Receptionist, UserMessageIntent, UserMessageEvaluation
from .interaction_manager import InteractionManager
from ..infrastructure.gep_template import load_template, GEPTemplate
from .planner_prompts import GEP_ENRICHED_GOAL_TEMPLATE


class FlowController:
    """
    Manages the entire task execution flow for a single session.

    Each session creates a new FlowController instance.  When the user
    requests a new session (:new), the caller (handq.py _run_backend) is
    responsible for discarding the current instance and constructing a fresh
    one via FlowController(...).

    FlowController is intentionally single-use: it owns the asyncio
    communication primitives (_step_batch_event, _interrupt_event,
    _agent_idle_event, …), the Memory object, and the InteractionManager
    reference for exactly one task session.
    """

    MAX_PARALLEL_BATCH_SIZE = 5
    DEFAULT_STEP_VERIFICATION_THRESHOLD = 0.7

    # Timeout (seconds) to wait for the Agent loop to exit cleanly after a stop signal.
    _AGENT_STOP_TIMEOUT = 10.0
    # Polling interval (seconds) for the Planner loop's main iteration.
    _LOOP_SLEEP_INTERVAL = 0.02

    def __init__(
        self,
        agent_llm_services: List[LLMService],
        planner_llm_services: List[LLMService],
        receptionist_llm_services: List[LLMService],
        from_data_llm_services: List[LLMService],
        working_directory: Optional[str] = None,
        storage_directory: Optional[str] = None,
        step_verification_threshold: float = DEFAULT_STEP_VERIFICATION_THRESHOLD,
        venv_path: Optional[str] = None,
        config_path: Optional[str] = None,
        shell_context_path: Optional[str] = None,
    ):
        if not agent_llm_services:
            raise ValueError("agent_llm_services must contain at least one LLMService")
        # All four role service lists are pre-assigned by the caller (handq.py).
        # No slicing or selection logic lives here.
        self._agent_services:        List[LLMService] = agent_llm_services
        self._planner_services:      List[LLMService] = planner_llm_services
        self._receptionist_services: List[LLMService] = receptionist_llm_services
        self._from_data_services:    List[LLMService] = from_data_llm_services

        # working_directory is optional: Linux/CLI mode passes the cwd-of-invocation
        # (the user's project); Windows GUI mode passes None because there is no
        # "user project" concept — all artifacts live under storage_directory.
        # When working_directory is None, prompts omit the "[Working Directory]"
        # block entirely (handled in runtime_agent and planner).
        self.working_directory: Optional[str] = working_directory
        self.storage_directory: str = storage_directory or working_directory or "."
        self.venv_path = venv_path
        self.step_verification_threshold = step_verification_threshold

        self.planner = Planner(
            llm_services=planner_llm_services,
            from_data_services=from_data_llm_services,
            working_directory=working_directory,
            storage_directory=self.storage_directory,
            step_verification_threshold=step_verification_threshold,
        )

        self.receptionist = Receptionist(
            llm_services=receptionist_llm_services,
            shell_history_path=shell_context_path,
            long_term_memory_path=None,
        )

        self.memory = Memory(self.storage_directory)
        # Set config_path on the InteractionManager singleton if not already set.
        # Use getattr() with a default of None so this works for both
        # InteractionManager (which may have config_path) and
        # TmuxInteractionManager (which does not define config_path),
        # preventing an AttributeError when the tmux IM is the singleton.
        self.interaction_manager = InteractionManager.get_instance()
        if getattr(self.interaction_manager, 'config_path', None) is None and config_path:
            self.interaction_manager.config_path = config_path
        self.config_manager = ConfigManager(config_path)
        self.logger = get_logger()

        self.state = SystemState.IDLE
        self.current_plan: Optional[Plan] = None
        self._execution_recorder: Optional[ExecutionRecorder] = None
        self._metrics_collector = MetricsCollector()
        self._current_plan_id: Optional[str] = None

        # Async communication primitives — reinitialised per task in _plan_and_execute.
        self._agent_step_batch: Optional[List[Step]] = None
        self._step_batch_event: asyncio.Event = asyncio.Event()
        self._completed_step: Optional[Step] = None
        self._interrupt_event: asyncio.Event = asyncio.Event()
        self._agent_idle_event: asyncio.Event = asyncio.Event()
        self._agent_idle_event.set()
        self._agent_task: Optional[asyncio.Task] = None
        # Set by cancel_all_tasks() so that the _plan_and_execute finally block
        # knows NOT to call stop_message_processor() — the save flow will have
        # already taken ownership of the message processor.
        self._externally_cancelled: bool = False

        # Async hook called at every task-completion point, BEFORE notifying the
        # user, so side-effects (e.g. GEP template post-processing) are
        # guaranteed to finish before the user sees "complete" and can quit.
        # Fires on every completion (including after user replans) — hook must
        # be idempotent.  None means no hook is registered.
        self._on_task_complete_hook: Optional[Any] = None

        # GEP pending-confirm state:
        # _pending_gep_template_id is set when the Receptionist matched a template
        # on the initial message. The planner loop waits up to _GEP_CONFIRM_TIMEOUT
        # seconds for the user to decline before auto-activating the template.
        self._pending_gep_template_id: Optional[str] = None
        self._pending_gep_template_obj: Optional[GEPTemplate] = None  # loaded once in _make_gep_confirm_callback
        self._gep_confirm_deadline: float = 0.0
        # How long (seconds) to wait for user decline before auto-entering GEP.
        self._GEP_CONFIRM_TIMEOUT: float = 300.0
        # Accumulated parameter context from the GEP confirmation window.
        # Seeded with the initial goal message and extended by every subsequent
        # message during the window. Passed to instantiate_gep_plan() so the
        # LLM sees all parameter values the user provided before activation.
        self._gep_param_context: str = ""

        # Reference to the currently executing RuntimeAgent instance.
        # Set in _execute_step() before agent.run_streaming() and cleared after.
        # Read by _planner_loop() during replan to get the in-flight
        # agent's execution summary (fixes replan blindness).
        # Access is safe: _planner_loop reads it only when has_new_messages
        # is True, which happens between agent iterations (asyncio yield points).
        self._current_agent: Optional["RuntimeAgent"] = None

        self.interaction_manager.set_status_callback(self._get_status_string)

        # ── Step context providers ─────────────────────────────────────────────
        # Generic list of StepContextProvider instances.  Before each step
        # executes, every provider whose matches() returns True has its
        # prepare() called; the returned hint is appended to effective_goal.
        # Register additional providers via register_step_context_provider().
        self._step_context_providers: List[StepContextProvider] = []
        self._register_default_providers()
        self._update_planner_tool_table()

        self.logger.info(
            f"FlowController initialized. working_directory:{self.working_directory}, "
            f"storage_directory:{self.storage_directory}, "
            f"venv_path:{self.venv_path}, "
            f"step_verification_threshold:{self.step_verification_threshold}",
            component="FlowController"
        )

    # ── Save session (triggered by CLI `handq --save`) ────────────────────────

    def _build_save_session_goal(self, save_context: dict, log_file: Optional[str] = None) -> Optional[str]:
        """
        Package the completed session's essential info into a self-contained
        goal string for the save-session flow.

        The agent reads the execution log directly via the read tool rather than
        receiving pre-processed steps — avoids injecting large step blobs into
        the prompt for long sessions.

        Returns None when no log_file is available.
        """
        if log_file is None:
            if self._execution_recorder is None:
                return None
            log_file = self._execution_recorder.log_file

        from ..infrastructure.gep_template import _templates_dir
        from .planner_prompts import SAVE_SESSION_GOAL_TEMPLATE

        original_goal = (
            save_context.get("goal")
            or (self._execution_recorder.goal if self._execution_recorder else None)
            or "(unknown)"
        )
        templates_dir = str(_templates_dir())
        completion     = save_context.get("completion", "") or save_context.get("status", "")
        completion_block = (
            f"\n[Task completion status: {completion}]\n" if completion else ""
        )

        return SAVE_SESSION_GOAL_TEMPLATE.format(
            original_goal=original_goal,
            completion_block=completion_block,
            log_file=log_file,
            templates_dir=templates_dir,
        )

    async def _trigger_save_session(self, log_file: Optional[str] = None) -> None:
        """
        Start an independent save-session flow to generate and verify a GEP template.

        Called by the background monitor when it detects the save_requested
        sentinel file written by `handq --save`.

        Creates a fresh FlowController (new Memory, no prior history) and
        injects the packaged goal — including pre-detected params — as the
        first user message. The agent reads the execution log, generates the
        template interactively, and saves it after user confirmation.

        Parameters:
          log_file — optional path to an external execution log file.  When
                     provided, the execution recorder for the current session
                     is not required (supports ``handq --save <path>``).

        Requirements (when log_file is None):
          • _execution_recorder must be set (a task was run this session).
          • state must be IDLE or COMPLETED (task not currently executing).
        """
        if log_file is None and self._execution_recorder is None:
            self.interaction_manager.display_error(
                "No completed task to save — run a task first, "
                "or provide a session log path: handq --save <path>"
            )
            return

        if self.state == SystemState.EXECUTING or self.state == SystemState.REPLANNING:
            self.interaction_manager.display_error(
                "Task still running — wait for completion before saving."
            )
            return

        from ..infrastructure.execution_recorder import ExecutionRecorder

        # Resolve log file: use override if provided, else fall back to current recorder.
        resolved_log_file = log_file or (
            self._execution_recorder.log_file if self._execution_recorder else None
        )
        if not resolved_log_file:
            self.interaction_manager.display_error(
                "No completed task to save — run a task first, "
                "or provide a session log path: handq --save <path>"
            )
            return

        # Compute save context: extracts goal/completion/steps from the raw log.
        # The cleaned steps are written to a compact file that the save-session
        # agent reads via the read tool — avoids injecting large blobs into the prompt.
        try:
            save_context = ExecutionRecorder.prepare_save_context(resolved_log_file)
        except Exception:
            save_context = {}

        self.interaction_manager.display_message(
            "Starting template generation session…"
        )
        self.logger.info(
            "Starting independent save-session flow", component="FlowController"
        )

        # Stop the current message processor so the new flow can start its own
        # without conflict.  The current flow is in IDLE at this point, so no
        # in-flight evaluation is lost.
        await self.interaction_manager.stop_message_processor()
        self.interaction_manager.set_evaluate_callback(None)

        # Create a fresh FlowController — new Memory, no prior step history.
        # The shared IM singleton means all output goes to the same terminal,
        # so the transition is transparent to the user.
        # Use an isolated storage directory so the save flow's session_state.json
        # and execution logs do not overwrite the original session's files.
        import time as _time
        _save_storage = os.path.join(
            self.storage_directory,
            "save_" + str(int(_time.time())),
        )
        os.makedirs(_save_storage, exist_ok=True)

        # Write the cleaned execution log (noise stripped) to a file in the
        # save session's storage dir. The agent reads this compact file via
        # the read tool instead of the raw log, avoiding large prompt injection.
        import json as _json_clean
        _execution_summary_path = os.path.join(_save_storage, "execution_summary.json")
        try:
            _json_clean.dump(save_context, open(_execution_summary_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        except Exception:
            _execution_summary_path = resolved_log_file  # fallback to raw log

        goal = self._build_save_session_goal(save_context, log_file=_execution_summary_path)
        if goal is None:
            self.interaction_manager.display_error(
                "No completed task to save — run a task first, "
                "or provide a session log path: handq --save <path>"
            )
            return
        save_flow = FlowController(
            agent_llm_services=self._agent_services,
            planner_llm_services=self._planner_services,
            receptionist_llm_services=self._receptionist_services,
            from_data_llm_services=self._from_data_services,
            working_directory=self.working_directory,
            storage_directory=_save_storage,
            step_verification_threshold=self.step_verification_threshold,
            venv_path=self.venv_path,
            config_path=str(self.config_manager.config_path),
        )

        # Seed the save flow's receptionist with the save/GEP task context so
        # it starts aware of what is being saved rather than in an empty state.
        save_flow.receptionist.conversation_history = [
            {"role": "user", "content": goal},
            {"role": "assistant", "content": "Understood. I will assist with generating the GEP template."},
        ]

        # Inject the packaged goal directly into _replan_queue, bypassing
        # Receptionist classification.  The save-session goal is a complex
        # multi-line instruction that the LLM Receptionist may mis-classify
        # as "chat" (RESPOND_ONLY) due to its "when uncertain choose chat"
        # default, causing the planner loop to stall indefinitely.
        # inject_task_goal() skips classify_initial_goal() and routes the
        # message straight to the planner as a REPLAN intent.
        save_flow.interaction_manager.inject_task_goal(goal)

        try:
            await save_flow.start_idle_session()
        except Exception as exc:
            self.logger.error(
                f"Save-session flow failed: {exc}", component="FlowController"
            )
            self.interaction_manager.display_error(
                f"Template generation session failed: {exc}"
            )

    async def _run_save_post_process(
        self,
        existing_ids: set,
        log_file: str,
        search_dirs: Optional[list] = None,
    ) -> None:
        """
        Patch all newly written GEP templates (stamp system fields, version-bump)
        and warn when no template was written at all.

        Called from _on_task_complete_hook — which fires BEFORE
        notify_task_completed() — so the templates are fully persisted before
        the user sees the completion message and can close the process.
        Also called as a fallback from the finally block in _trigger_save_session
        if the hook never fired (e.g. the session exited without task completion).
        """
        from pathlib import Path as _Path
        import uuid as _uuid
        from ..infrastructure.gep_template import list_templates, save_template

        new_templates = [t for t in list_templates() if t.id not in existing_ids]

        # If the agent didn't write to gep_templates/, scan fallback dirs for any
        # JSON file that looks like a template (has name + guide_steps).
        if not new_templates and search_dirs:
            import json as _json_scan
            from ..infrastructure.gep_template import GEPTemplate, _templates_dir
            _tdir = _templates_dir()
            for _sdir in search_dirs:
                _sdirp = _Path(_sdir)
                if not _sdirp.exists():
                    continue
                for _jf in _sdirp.rglob("*.json"):
                    # Skip files already in gep_templates/
                    try:
                        if _jf.resolve().parent == _tdir.resolve():
                            continue
                    except Exception:
                        continue
                    try:
                        _d = _json_scan.loads(_jf.read_text(encoding="utf-8"))
                        if not (_d.get("name") and _d.get("guide_steps")):
                            continue
                        # Looks like a template — inject filename as id if missing
                        if not _d.get("id"):
                            _d["id"] = _jf.stem
                        _t = GEPTemplate.from_dict(_d)
                        if _t.id in existing_ids:
                            continue
                        # Copy into gep_templates/ so it is picked up below
                        _dest = _tdir / _jf.name
                        _tdir.mkdir(parents=True, exist_ok=True)
                        _dest.write_text(_json_scan.dumps(_d, indent=2, ensure_ascii=False), encoding="utf-8")
                        self.logger.info(
                            f"Save fallback: recovered template from {_jf}",
                            component="FlowController",
                        )
                    except Exception:
                        continue
            # Re-scan after recovery attempt
            new_templates = [t for t in list_templates() if t.id not in existing_ids]

        # Still nothing found — log quietly, no user-facing error.
        if not new_templates:
            self.logger.warning(
                "Save-session completed but no new template file was found. "
                "The agent may not have written the JSON to the templates directory.",
                component="FlowController",
            )
            return

        from ..infrastructure.gep_template import _utcnow, _templates_dir
        import json as _json_scan

        # Build a mapping from template id → source file path for all new
        # template files so we can delete the agent-written draft after the
        # post-processing saves the UUID-named canonical file.
        # Also record the filename alongside each id so we can fall back to a
        # direct path lookup when the id inside the JSON differs from old_id.
        _tdir = _templates_dir()
        _new_id_to_file: dict = {}
        for _jf in _tdir.glob("*.json"):
            try:
                _d = _json_scan.loads(_jf.read_text(encoding="utf-8"))
                _tid = _d.get("id", "")
                if _tid and _tid not in existing_ids:
                    _new_id_to_file[_tid] = _jf
            except Exception:
                pass

        def _delete_draft(draft_file: "_Path", saved_path: str, label: str) -> None:
            """Delete draft_file if it exists and is not the canonical saved file.

            Wraps unlink() in a try/except that logs a warning on failure so
            that a permissions error or race condition never silently leaves a
            stale draft in the templates directory.
            """
            if not draft_file.exists():
                return
            if str(draft_file.resolve()) == saved_path:
                # Same file — agent happened to use the UUID as filename; nothing to do.
                return
            try:
                draft_file.unlink()
                self.logger.info(
                    f"Post-save: removed agent draft file '{draft_file.name}' ({label})",
                    component="FlowController",
                )
            except Exception as _del_exc:
                self.logger.warning(
                    f"Post-save: could not remove draft file '{draft_file}' ({label}): {_del_exc}",
                    component="FlowController",
                )

        for template in new_templates:
            old_id = template.id
            # Resolve the draft file via the id→file mapping built above.
            # Pass old_id explicitly so the fallback path below can use it
            # without relying solely on a reverse-lookup that may fail on
            # JSON parse errors or id mismatches.
            old_file = _new_id_to_file.get(old_id)

            # Always overwrite system fields — these are never left to the LLM.
            template.id = str(_uuid.uuid4())
            template.created_at = _utcnow()
            template.source_log_path = str(_Path(log_file).resolve())

            # Version: 1 for new name, max_existing+1 for an update.
            # Exclude the old draft (old_id) from the comparison so a duplicate
            # name in the draft file does not incorrectly inflate the version.
            all_others = [t for t in list_templates() if t.id not in (template.id, old_id)]
            same_name = [t for t in all_others if t.name == template.name]
            template.version = (max(t.version for t in same_name) + 1) if same_name else 1

            saved_path = save_template(template)

            # Remove the agent-written draft file — it has a human-readable name
            # chosen by the LLM, while the canonical file uses the UUID id.
            if old_file is not None:
                _delete_draft(old_file, saved_path, "id-map lookup")
            else:
                # Fallback: the id→file mapping failed (e.g. JSON parse error or
                # the id inside the JSON body differed from old_id).  Try to
                # locate the draft by constructing its expected filename directly
                # from old_id — the LLM typically names the file after the id it
                # wrote into the JSON body.
                from ..infrastructure.gep_template import _sanitize_template_id
                try:
                    _fallback_name = f"{_sanitize_template_id(old_id)}.json"
                    _fallback_file = _tdir / _fallback_name
                    if _fallback_file.exists():
                        _delete_draft(_fallback_file, saved_path, "fallback glob")
                    else:
                        # Second fallback: the draft was written without an "id"
                        # field so old_id is a random UUID unrelated to the actual
                        # filename.  The LLM names the file after template.name
                        # (e.g. "directory-html-report.json"), so try that next.
                        _name_based = f"{_sanitize_template_id(template.name)}.json"
                        _name_file = _tdir / _name_based
                        if _name_file.exists() and str(_name_file.resolve()) != saved_path:
                            self.logger.warning(
                                f"Post-save: deleting name-based draft '{_name_file.name}' "
                                f"(template.name={template.name!r}); UUID draft lookup missed it.",
                                component="FlowController",
                            )
                            _delete_draft(_name_file, saved_path, "name-based fallback")
                        else:
                            self.logger.warning(
                                f"Post-save: draft file not found via id-map or fallback "
                                f"(old_id={old_id!r}); manual cleanup of '{_tdir}' may be needed.",
                                component="FlowController",
                            )
                except Exception as _fb_exc:
                    self.logger.warning(
                        f"Post-save: fallback draft cleanup failed for old_id={old_id!r}: {_fb_exc}",
                        component="FlowController",
                    )
            self.logger.info(
                f"Post-save: stamped system fields for template '{template.name}' "
                f"(id, created_at, source_log_path, version={template.version})",
                component="FlowController",
            )
            # Show a brief task-overview panel: template name and saved path only.
            # Full template content (description, steps, params) is shown by the
            # receptionist in the GEP intro message before execution begins.
            _review_lines = [
                f"[GEP] Template saved: \"{template.name}\" (v{template.version})",
                f"  File: {saved_path}",
            ]
            _review_text = "\n".join(_review_lines)
            
            self.interaction_manager.display_message(_review_text)

    # ── Cancellation ──────────────────────────────────────────────────────────

    def cancel_all_tasks(self) -> None:
        """
        Cancel all internal async sub-tasks immediately.

        Called by the background monitor before handing control to
        _trigger_save_session(), to ensure the old flow's sub-tasks
        (agent loop, message processor) do not compete with the new
        save-session flow for the shared InteractionManager.

        Safe to call at any time — no-ops on already-done or None tasks.
        """
        self._externally_cancelled = True
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
        # Signal the agent loop to wake up and see the cancellation
        self._step_batch_event.set()
        # Stop the message processor (non-blocking signal)
        try:
            self.interaction_manager.set_evaluate_callback(None)
        except Exception:
            pass

    # ── Status ────────────────────────────────────────────────────────────────

    def _get_status_string(self) -> str:
        completed = len(self.memory.get_completed_steps())
        state = self.state.value
        if self.current_plan and self.current_plan.next_steps:
            total = completed + len(self.current_plan.next_steps)
            return (
                f"State: {state}\n"
                f"Completed steps: {completed}/{total}\n"
                f"Current step: {self.current_plan.next_steps[0].description}"
            )
        return f"State: {state}\nCompleted steps: {completed}"

    # ── Step context providers ────────────────────────────────────────────────

    def register_step_context_provider(self, provider: StepContextProvider) -> None:
        """
        Register a StepContextProvider.

        Providers are called in registration order before each step executes.
        Each provider whose ``matches()`` returns True has its ``prepare()``
        called; the returned hint string is appended to ``effective_goal``.

        Use this to add context-injection concerns (SSH credentials, DB
        connections, API tokens, etc.) without modifying FlowController.
        """
        self._step_context_providers.append(provider)

    # ── GEP helpers ───────────────────────────────────────────────────────────

    def _build_gep_enriched_goal(self, original_goal: str, steps: List[Step]) -> str:
        """
        Return an enriched goal string that embeds the proven step sequence.

        Injected into the session goal after GEP template instantiation so that
        all subsequent observe_and_plan() calls see the proven pattern as context
        without needing the template object itself (which is cleared by then).
        """
        step_lines = "\n".join(
            f"  {i}. [{s.step_id}] {s.description}"
            for i, s in enumerate(steps, 1)
        )
        return GEP_ENRICHED_GOAL_TEMPLATE.format(
            original_goal=original_goal,
            step_lines=step_lines,
        )

    def _write_session_state(self, completed_steps: List[Step]) -> None:
        """Write a compact session state JSON to the storage directory.

        Called after every successful step commit so the agent and planner
        always have an up-to-date, machine-readable view of completed work —
        even when the planner's in-memory summary is compressed or truncated.

        The file is written to <storage_directory>/session_state.json.
        Any read/write error is silently swallowed (best-effort, non-critical).

        Format:
            { "steps": [ { "id", "description", "status",
                           "factual_outcome"[:3], "artifacts",
                           "key_findings"[:5], "issues"[:2] }, ... ] }
        """
        import json as _json
        from pathlib import Path as _Path

        steps_data = []
        for s in completed_steps:
            steps_data.append({
                "id":             s.step_id,
                "description":    s.description,
                "goal":           getattr(s, "goal", "") or "",
                "status":         s.status.value if hasattr(s.status, "value") else str(s.status),
                "factual_outcome": (s.factual_outcome or [])[:3],
                "artifacts":       s.artifacts or [],
                "key_findings":    (s.key_findings or [])[:5],
                "issues":          (s.issues or [])[:2],
            })

        try:
            state_path = _Path(self.storage_directory) / "session_state.json"
            state_path.write_text(
                _json.dumps({"steps": steps_data}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.debug(
                f"session_state.json write failed (non-critical): {exc}",
                component="FlowController",
            )

    # File extensions that warrant a narrow ground-truth test step
    # (py_compile / tsc / equivalent) when synthesize_acceptance fires.
    _CODE_EXTENSIONS = frozenset({
        '.py', '.pyx', '.pyi',
        '.ts', '.tsx', '.js', '.jsx', '.mjs',
        '.go', '.rs', '.java', '.kt', '.swift',
        '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp',
        '.cs', '.rb', '.php', '.scala', '.sql',
    })

    def _has_code_edits(self, completed_steps: List[Step]) -> bool:
        """True iff any completed step wrote or edited a source-code file.

        Detects code modification by:
          1. Looking for write/edit tool calls in the step's metrics, and
          2. Cross-checking that an artifact with a code extension is present.

        The cross-check prevents a step that only edited e.g. a .md file from
        triggering a code-test step.  Read-only or shell-only steps are
        ignored.

        Used as the gate for synthesize_acceptance(has_code_edits=...): when
        False, the synthesis prompt is forbidden from proposing a code-test
        step.
        """
        for s in completed_steps:
            tools = getattr(s, '_metrics_tools_used', []) or []
            wrote = any(
                entry.lower().startswith(('write:', 'edit:'))
                for entry in tools
            )
            if not wrote:
                continue
            for path in (s.artifacts or []):
                lower = path.lower()
                # Match by suffix; tolerate paths with trailing whitespace.
                if any(lower.endswith(ext) for ext in self._CODE_EXTENSIONS):
                    return True
        return False

    def _dedup_step_id(self, step: Step, completed_steps: List[Step]) -> Step:
        """Ensure step.step_id does not collide with any completed step's id.

        Mutates and returns the same Step object.  When a collision is found,
        appends a numeric suffix (`_2`, `_3`, …) until the id is unique.
        Used before injecting a corrective_step or code_test_step from
        synthesize_acceptance() so log readability and any future id-based
        lookups stay clean across replan cycles.
        """
        existing = {s.step_id for s in completed_steps}
        if step.step_id not in existing:
            return step
        base = step.step_id
        counter = 2
        while f"{base}_{counter}" in existing:
            counter += 1
        step.step_id = f"{base}_{counter}"
        return step

    def _register_default_providers(self) -> None:
        """Register built-in StepContextProviders.

        Browser, Desktop, and Session providers are Windows-only — they are
        not registered on Linux, so the Linux planner never sees their tools
        nor receives any hint mentioning them. SSH provider is registered on
        both platforms.
        """
        import sys as _sys
        _is_windows = _sys.platform == "win32"

        try:
            from ..infrastructure.ssh_setup import SSHContextProvider
            self.register_step_context_provider(SSHContextProvider())
        except ImportError:
            self.logger.debug(
                "SSHContextProvider not registered (paramiko or keyring missing)",
                component="FlowController",
            )

        # Coding-mode hint provider (cross-platform, no transitive deps).
        # Activated when planner declares "coding" in tools_required.
        try:
            from ..infrastructure.coding_setup import CodingContextProvider
            self.register_step_context_provider(CodingContextProvider())
        except ImportError as _coding_exc:
            self.logger.debug(
                f"CodingContextProvider not registered: {_coding_exc}",
                component="FlowController",
            )

        if not _is_windows:
            self.logger.info(
                "Linux platform: skipping Browser/Desktop/Session provider registration",
                component="FlowController",
            )
            return

        try:
            from ..infrastructure.browser_setup import BrowserContextProvider
            if self.config_manager.is_tool_enabled("browser"):
                self.register_step_context_provider(BrowserContextProvider())
            else:
                self.logger.info(
                    "BrowserContextProvider not registered: tool_browser disabled in interaction_switches",
                    component="FlowController",
                )
        except ImportError:
            self.logger.debug(
                "BrowserContextProvider not registered (transitive deps missing)",
                component="FlowController",
            )
        try:
            from ..infrastructure.web_search_setup import WebSearchContextProvider
            if self.config_manager.is_tool_enabled("web_search"):
                self.register_step_context_provider(WebSearchContextProvider())
            else:
                self.logger.info(
                    "WebSearchContextProvider not registered: tool_web_search disabled in interaction_switches",
                    component="FlowController",
                )
        except ImportError:
            self.logger.debug(
                "WebSearchContextProvider not registered (transitive deps missing)",
                component="FlowController",
            )
        try:
            from ..infrastructure.email_setup import EmailContextProvider
            if self.config_manager.is_tool_enabled("email"):
                self.register_step_context_provider(EmailContextProvider())
            else:
                self.logger.info(
                    "EmailContextProvider not registered: tool_email disabled in interaction_switches",
                    component="FlowController",
                )
        except ImportError:
            self.logger.debug(
                "EmailContextProvider not registered (pywin32 missing)",
                component="FlowController",
            )
        try:
            from ..infrastructure.desktop_setup import DesktopContextProvider
            if self.config_manager.is_tool_enabled("desktop"):
                self.register_step_context_provider(DesktopContextProvider())
            else:
                self.logger.info(
                    "DesktopContextProvider not registered: tool_desktop disabled in interaction_switches",
                    component="FlowController",
                )
        except ImportError:
            self.logger.debug(
                "DesktopContextProvider not registered (transitive deps missing)",
                component="FlowController",
            )
        try:
            from ..infrastructure.ask_human_setup import AskHumanContextProvider
            if self.config_manager.is_tool_enabled("ask_human"):
                self.register_step_context_provider(AskHumanContextProvider())
            else:
                self.logger.info(
                    "AskHumanContextProvider not registered: tool_ask_human disabled in interaction_switches",
                    component="FlowController",
                )
        except ImportError:
            self.logger.debug(
                "AskHumanContextProvider not registered (import failed)",
                component="FlowController",
            )
        try:
            from ..infrastructure.session_setup import SessionContextProvider
            self.register_step_context_provider(SessionContextProvider())
            self.logger.info(
                f"SessionContextProvider registered (total providers: {len(self._step_context_providers)})",
                component="FlowController",
            )
        except Exception as _session_exc:
            self.logger.warning(
                f"SessionContextProvider not registered: {type(_session_exc).__name__}: {_session_exc}",
                component="FlowController",
            )







    def _update_planner_tool_table(self) -> None:
        """Build all dynamic planner sections from registered providers and push to Planner.

        Called once after _register_default_providers() so the planner's system
        prompt only mentions tools that are both enabled and importable.
        """
        table_rows: List[str] = []
        routing_rules: List[str] = []
        antipatterns: List[str] = []

        for provider in self._step_context_providers:
            row = provider.planner_description()
            if row:
                table_rows.append(f"| {row} |\n")
            rule = provider.planner_routing_rule()
            if rule:
                routing_rules.append(rule)
            for ap in provider.planner_antipatterns():
                antipatterns.append(ap)

        # Number routing rules starting at 6 (after the 5 static ssh/session rules).
        numbered_rules = "".join(
            f"{6 + i}. {rule:<52}\n"
            for i, rule in enumerate(routing_rules)
        )
        coding_rule_num = 6 + len(routing_rules)

        formatted_antipatterns = "".join(
            f"  ❌ {ap}\n" for ap in antipatterns
        )

        self.planner._on_demand_tools_table   = "".join(table_rows)
        self.planner._on_demand_routing_rules = numbered_rules
        self.planner._on_demand_antipatterns  = formatted_antipatterns
        self.planner._coding_rule_num         = coding_rule_num

        tool_names = [p.tool_name for p in self._step_context_providers if p.planner_description()]
        self.logger.info(
            f"Planner tool table updated: {len(table_rows)} dynamic tool(s) — {tool_names}",
            component="FlowController",
        )

    def _make_evaluate_callback(self, goal: str) -> Callable[[str], Awaitable]:
        """
        Return an async callback for InteractionManager's message processor.

        The callback reads self.current_plan at call time (not at creation
        time) so it always uses the most recent plan — even if the Planner
        loop has advanced since the callback was created.  self.current_plan
        is already an instance variable updated by every observe_and_plan()
        call, so no additional shared-state object is needed.

        goal starts empty and is set lazily from the first REPLAN message;
        it never changes for the lifetime of the task.

        The callback is set on InteractionManager before the message processor
        starts and cleared after the task ends.
        """
        async def callback(msg: str):
            plan = self.current_plan

            def _format_step_for_receptionist(s: Step) -> str:
                """Pack description + goal + reasoning + expected outcomes.

                Receptionist needs WHY (planner_reasoning) and SUCCESS CRITERIA
                (expected_outcomes) so it can answer user questions about
                decisions without guessing.
                """
                lines = [f"{s.description}: {s.goal}"]
                reasoning = getattr(s, "planner_reasoning", "") or ""
                expected = getattr(s, "expected_outcomes", None) or []
                if reasoning:
                    lines.append(f"  Reasoning: {reasoning}")
                if expected:
                    lines.append(f"  Expected: {'; '.join(expected)}")
                return "\n".join(lines)

            # next_steps[0] is the first planned step — the best available
            # proxy for "what is currently executing" without needing to track
            # in_flight_batch separately.
            current_step_desc = (
                _format_step_for_receptionist(plan.next_steps[0])
                if plan and plan.next_steps
                else None
            )
            next_step_descs = [
                _format_step_for_receptionist(s)
                for s in (plan.next_steps[1:] if plan and plan.next_steps else [])
            ]

            # Completed / remaining counts for progress reporting.
            completed_count = len(self.memory.get_completed_steps())
            remaining_count = len(plan.next_steps) if plan else 0

            # Get task context from memory to provide completed steps summary
            # Intentional: planner evaluate callback always receives full context for accurate task-level assessment
            task_context = self.memory.get_prior_step_context(max_steps=15)
            accumulated_findings = self.memory.get_accumulated_findings_for_planner(
                already_covered_count=self.memory.count_context_entries_in_last_n_steps(self.planner.detail_window),
                max_steps=15,
            )

            # In-flight agent progress — same context the Planner injects at line 507-522.
            agent_progress = ""
            if self._current_agent is not None:
                agent_progress = self._current_agent.get_progress_summary() or ""

            try:
                # FR-1: Receptionist receives the same context as the Planner.
                # FR-4: uses the Receptionist's own independent LLM service.
                chunk_cb = (
                    self.interaction_manager.stream_receptionist_chunk
                    if self.interaction_manager._ui is not None
                    and hasattr(self.interaction_manager._ui, 'stream_receptionist_reply_chunk')
                    else None
                )
                evaluation = await self.receptionist.evaluate_user_message(
                    message=msg,
                    goal=goal,
                    current_step_description=current_step_desc,
                    lookahead_descriptions=next_step_descs,
                    task_context=task_context,
                    accumulated_findings=accumulated_findings,
                    agent_progress=agent_progress,
                    completed_count=completed_count,
                    remaining_count=remaining_count,
                    on_response_chunk=chunk_cb,
                )
            except Exception as e:
                self.interaction_manager.display_error(f"LLM API error: {e}")
                raise

            if evaluation.intent == UserMessageIntent.RESPOND_ONLY:
                evaluation.response_to_user = f"{evaluation.response_to_user}"

            # GEP is not available during task execution.
            # Defensively downgrade to RESPOND_ONLY in case the LLM ignores the prompt.
            if evaluation.intent in (UserMessageIntent.GEP_CONFIRM, UserMessageIntent.GEP_DECLINE):
                self.logger.warning(
                    f"GEP intent '{evaluation.intent.value}' received mid-task — "
                    "downgrading to RESPOND_ONLY (GEP only available before task start)",
                    component="FlowController",
                )
                return UserMessageEvaluation(
                    intent=UserMessageIntent.RESPOND_ONLY,
                    response_to_user=(
                        "GEP template mode can only be selected before starting a task. "
                        "Please finish or cancel the current task first, then start a new session with the template."
                    ),
                    reasoning="GEP not available mid-task",
                )

            return evaluation

        return callback

    def _make_initial_classify_callback(self) -> Callable[[str], Awaitable]:
        """
        Return the callback used before a goal is established.

        Handles all intents that can appear on the very first message:
          RESPOND_ONLY   → shown immediately, no task started.
          REPLAN (task)  → becomes the goal; planning begins.
          GEP_CONFIRM    → stores the matched template id for timeout-confirm, then
                           routes the message as REPLAN so the goal gets set.
          GEP_DECLINE    → clears any pending GEP state; stays idle.

        Note: SAVE_SESSION and LIST_TEMPLATES are handled via CLI (handq --save /
        handq --list), not through the receptionist.
        """
        async def callback(msg: str):
            chunk_cb = (
                self.interaction_manager.stream_receptionist_chunk
                if self.interaction_manager._ui is not None
                and hasattr(self.interaction_manager._ui, 'stream_receptionist_reply_chunk')
                else None
            )
            evaluation = await self.receptionist.classify_initial_goal(
                msg, on_response_chunk=chunk_cb
            )

            # ── GEP_CONFIRM ───────────────────────────────────────────────────
            # Store the template id for timeout-confirm and route as REPLAN so
            # the goal gets set. The user's original message becomes the goal.
            if evaluation.intent == UserMessageIntent.GEP_CONFIRM:
                if evaluation.matched_template_id:
                    self._pending_gep_template_id = evaluation.matched_template_id
                    self._gep_param_context = msg  # seed with initial goal
                    self._gep_confirm_deadline = (
                        time.monotonic() + self._GEP_CONFIRM_TIMEOUT
                    )
                result = UserMessageEvaluation(
                    intent=UserMessageIntent.REPLAN,
                    response_to_user=evaluation.response_to_user,
                    reasoning=evaluation.reasoning,
                    context_for_planner=evaluation.context_for_planner or msg,
                )
                if getattr(evaluation, '_streamed', False):
                    result._streamed = True  # type: ignore[attr-defined]
                return result

            # ── GEP_DECLINE ───────────────────────────────────────────────────
            if evaluation.intent == UserMessageIntent.GEP_DECLINE:
                self._pending_gep_template_id = None
                return evaluation

            return evaluation

        return callback

    def _make_gep_confirm_callback(self) -> Callable[[str], Awaitable]:
        """
        Return the callback used during the GEP confirmation window.

        Loads template name/description/steps once at creation time so the LLM
        has full context on every call.  Falls back to template ID if the template
        cannot be loaded.

        Three outcomes:
          GEP_CONFIRM  → force-expire the deadline so the timeout loop activates
                         the template immediately.
          GEP_DECLINE  → clear _pending_gep_template_id so the loop exits without
                         activating; normal planning proceeds.
          RESPOND_ONLY → answer the user and restore the paused countdown.

        All branches return RESPOND_ONLY to the message processor — nothing is
        ever forwarded to _replan_queue during the GEP confirmation window.
        """
        # Load template metadata once so each LLM call has full context.
        # Also store on self so the while loop can reuse the object directly
        # instead of calling load_template a second time.
        _template_name = self._pending_gep_template_id or "unknown"
        _template_description = ""
        _steps_summary = ""
        self._pending_gep_template_obj = None
        try:
            _t = load_template(self._pending_gep_template_id)
            self._pending_gep_template_obj = _t
            _template_name = _t.name
            _template_description = _t.description
            if _t.guide_steps:
                _steps_summary = "\n".join(
                    f"  {i+1}. {s.description}"
                    for i, s in enumerate(_t.guide_steps)
                )
        except Exception as _load_exc:
            self.logger.warning(
                f"GEP confirm callback: could not load template for context: {_load_exc}",
                component="FlowController",
            )

        async def callback(msg: str):
            # Accumulate every message into param context regardless of intent —
            # the user may be providing template parameter values via chat.
            if msg.strip():
                self._gep_param_context = (
                    (self._gep_param_context + "\n" + msg).strip()
                    if self._gep_param_context
                    else msg
                )

            # Pause the countdown while waiting for the LLM response so that
            # a slow server cannot trigger auto-activation mid-conversation.
            # remaining is capped at 0 in case the deadline was already past.
            remaining = max(0.0, self._gep_confirm_deadline - time.monotonic())
            self._gep_confirm_deadline = float('inf')

            try:
                evaluation = await self.receptionist.evaluate_gep_confirmation(
                    user_input=msg,
                    template_name=_template_name,
                    template_description=_template_description,
                    guide_steps_summary=_steps_summary,
                    on_response_chunk=(
                        self.interaction_manager.stream_receptionist_chunk
                        if self.interaction_manager._ui is not None
                        and hasattr(self.interaction_manager._ui, 'stream_receptionist_reply_chunk')
                        else None
                    ),
                )
            except Exception:
                # Restore the deadline so the countdown is not frozen on error.
                self._gep_confirm_deadline = time.monotonic() + remaining
                raise

            if evaluation.intent == UserMessageIntent.GEP_CONFIRM:
                self._gep_confirm_deadline = 0.0
                return UserMessageEvaluation(
                    intent=UserMessageIntent.RESPOND_ONLY,
                    response_to_user=evaluation.response_to_user or "Confirmed — activating template shortly.",
                    reasoning=evaluation.reasoning,
                )

            if evaluation.intent == UserMessageIntent.GEP_DECLINE:
                self._pending_gep_template_id = None
                return UserMessageEvaluation(
                    intent=UserMessageIntent.RESPOND_ONLY,
                    response_to_user=evaluation.response_to_user or "GEP declined. Using normal planning mode.",
                    reasoning=evaluation.reasoning,
                )

            # Any other intent (e.g. parameter update): reset the countdown to
            # the full timeout so the user has the full window to finish
            # reviewing/setting parameters before auto-activation.
            self._gep_confirm_deadline = time.monotonic() + self._GEP_CONFIRM_TIMEOUT
            response = evaluation.response_to_user or "Noted."
            response = (
                f"{response}\n\n"
                f"[GEP] {int(self._GEP_CONFIRM_TIMEOUT)}s remaining — type 'yes' to confirm or 'no' to skip."
            )
            return UserMessageEvaluation(
                intent=UserMessageIntent.RESPOND_ONLY,
                response_to_user=response,
                reasoning=evaluation.reasoning,
            )

        return callback

    # ── Task entry point ──────────────────────────────────────────────────────

    async def start_idle_session(self) -> Dict[str, Any]:
        """
        Start session in idle/receptionist mode — no initial goal.

        Starts the message processor (Receptionist) and planner loop
        immediately, but skips initial planning.  The planner loop waits
        for the first REPLAN message, which becomes the goal.

        Receptionist conversation_history accumulates from the very first
        message, preserving full context across state1 → state2 → state3.
        """
        self.logger.info("Starting idle session", component="FlowController")
        self._transition_state(SystemState.IDLE)
        try:
            result = await self._plan_and_execute()
            task_success = result.get("success", False)
            if self._execution_recorder:
                self._execution_recorder.write_plan_end(success=task_success)
            if self._current_plan_id:
                self._metrics_collector.record_task_end(
                    self._current_plan_id, success=task_success,
                    duration_seconds=time.monotonic() - self._task_start_time,
                )
            return result
        except Exception as e:
            self.logger.error(f"Idle session failed: {e}", component="FlowController")
            if self._execution_recorder:
                self._execution_recorder.write_plan_end(success=False)
            if self._current_plan_id:
                self._metrics_collector.record_task_end(
                    self._current_plan_id, success=False,
                    duration_seconds=time.monotonic() - self._task_start_time,
                )
            self._transition_state(SystemState.ERROR)
            self.interaction_manager.display_error(str(e))
            return {"success": False, "message": f"Session failed: {e}"}

    # ── Main execution entry ──────────────────────────────────────────────────

    async def _plan_and_execute(self) -> Dict[str, Any]:
        """
        Start Planner loop, Agent loop, and message processor; wait for
        Planner to finish, then stop Agent and message processor.

        Starts in idle mode: skips initial planning; the planner loop waits
        for the first REPLAN message which becomes the goal.
        """
        # Initialise a fresh Memory for this session.
        self.memory = Memory(self.storage_directory)

        self.state = SystemState.IDLE

        # Reinitialise all async primitives for this task.
        self._agent_step_batch = None
        self._step_batch_event = asyncio.Event()
        self._completed_step = None
        self._interrupt_event = asyncio.Event()
        self._agent_idle_event = asyncio.Event()
        self._agent_idle_event.set()

        # Set the initial callback to classify_initial_goal so the message
        # processor handles the first message correctly from the start —
        # RESPOND_ONLY replies are shown immediately, REPLAN messages enter
        # the queue. Once _planner_loop picks up the first REPLAN and sets
        # the goal, it switches to _make_evaluate_callback(goal).
        self.interaction_manager.set_evaluate_callback(
            self._make_initial_classify_callback()
        )
        await self.interaction_manager.start_message_processor()

        self._agent_task = asyncio.create_task(self._agent_loop())

        try:
            result = await self._planner_loop()
        finally:
            # Stop the message processor before tearing down the agent so
            # no new evaluation callbacks fire after the task context is stale.
            # Skip if cancel_all_tasks() was already called: in that case
            # _trigger_save_session() has already stopped the processor and
            # started a new one for the save flow — we must not kill it.
            if not self._externally_cancelled:
                await self.interaction_manager.stop_message_processor()
                self.interaction_manager.set_evaluate_callback(None)

            # Send stop signal (None batch) and wait for a clean Agent exit.
            self._agent_step_batch = None
            self._step_batch_event.set()
            try:
                await asyncio.wait_for(self._agent_task, timeout=self._AGENT_STOP_TIMEOUT)
            except asyncio.TimeoutError:
                self._agent_task.cancel()
                try:
                    await self._agent_task
                except asyncio.CancelledError:
                    pass

        return result

    # ── Agent loop ────────────────────────────────────────────────────────────

    async def _agent_loop(self) -> None:
        """Execute batches fed by the Planner loop. Exits on None batch (stop signal)."""
        while True:
            await self._step_batch_event.wait()
            self._step_batch_event.clear()

            batch_steps = self._agent_step_batch
            self._agent_step_batch = None

            if batch_steps is None or len(batch_steps) < 1:
                break

            self._agent_idle_event.clear()

            try:
                completed_step = await self._execute_steps(batch_steps)
            except Exception as execute_exception:
                completed_step = Step(
                    step_id=f"execute_fail_{uuid.uuid4().hex[:8]}",
                    description=f"Execute steps error {execute_exception}",
                    goal=batch_steps[0].goal,
                )
                completed_step.update_status(StepStatus.FAILED)
                completed_step.issues = [str(execute_exception)]

            self._completed_step = completed_step
            self._agent_idle_event.set()

    # ── Planner loop ──────────────────────────────────────────────────────────

    async def _planner_loop(self) -> Dict[str, Any]:
        """
        Main planning loop.

        Starts in idle mode: skips initial planning and waits for the first
        REPLAN message which becomes the goal.

        Each iteration:
          Phase 1 — drain REPLAN message queue (pre-evaluated by message processor).
          Phase 2 — collect agent step result (non-blocking).
          Phase 3 — replan on new step result or REPLAN message; handle interrupts.
          Feed    — dispatch next batch when agent is idle and steps are available.
        """
        progress_tracker = PlannerProgressTracker()
        replan_history: List[Tuple[str, int]] = []
        last_seen_msg_idx: int = 0
        in_flight_batch: Optional[List[Step]] = None

        # ── Wait for the first REPLAN message to become the goal ─────────────
        self.logger.info("Idle mode: waiting for first user message", component="FlowController")
        self._transition_state(SystemState.IDLE)
        next_steps_list: List[Step] = []

        while not replan_history:
            if self.interaction_manager.is_exit_requested():
                self._transition_state(SystemState.COMPLETED)
                return {"success": True, "message": "Session ended by user request."}
            replan_history = await self._collect_replan_messages(replan_history)
            if not replan_history:
                await asyncio.sleep(self._LOOP_SLEEP_INTERVAL)

        goal: str = replan_history[0][0]
        if self.working_directory and self.working_directory != ".":
            try:
                os.chdir(self.working_directory)
            except Exception:
                pass
        plan_id = uuid.uuid4().hex
        self._current_plan_id = plan_id
        exec_log_dir = os.path.join(self.storage_directory, "executions_logs")
        self._execution_recorder = ExecutionRecorder(
            plan_id=plan_id,
            goal=goal,
            log_dir=exec_log_dir,
        )
        self._metrics_collector.record_task_start(plan_id, goal)
        self._task_start_time = time.monotonic()
        self.logger.info(
            f"Goal set from first message: {goal[:60]}",
            component="FlowController",
        )

        # ── GEP timeout confirm ───────────────────────────────────────────────
        # If a template was matched on the initial message, wait up to
        # _GEP_CONFIRM_TIMEOUT seconds for the user to respond.
        # • GEP_CONFIRM  → _gep_confirm_deadline forced to 0, loop activates immediately.
        # • GEP_DECLINE  → _pending_gep_template_id cleared, loop exits, normal planning.
        # • Other input  → countdown paused during LLM call, restored after; user informed of remaining time.
        # • No response  → timeout → auto-activate.
        if self._pending_gep_template_id:
            self.interaction_manager.set_evaluate_callback(
                self._make_gep_confirm_callback()
            )
            _display_name = (
                self._pending_gep_template_obj.name
                if self._pending_gep_template_obj
                else self._pending_gep_template_id
            )
            _gep_intro_lines = [
                f"## 📋 Template: {_display_name}",
            ]
            _t_obj = self._pending_gep_template_obj
            if _t_obj is not None:
                # (0) Template description
                _t_intro_desc = getattr(_t_obj, 'description', '') or ''
                if _t_intro_desc:
                    _gep_intro_lines.append("### 📝 Description")
                    _gep_intro_lines.append(_t_intro_desc)
                # (1) Numbered list of steps (steps attr, fallback to guide_steps)
                _t_intro_steps = getattr(_t_obj, 'steps', None)
                if _t_intro_steps is None:
                    _t_intro_steps = getattr(_t_obj, 'guide_steps', []) or []
                if _t_intro_steps:
                    _gep_intro_lines.append("### 📌 Steps")
                    for _si, _s in enumerate(_t_intro_steps, 1):
                        if isinstance(_s, dict):
                            _sname = _s.get('name', '') or _s.get('description', '')
                            _sdesc = _s.get('description', '')
                        else:
                            _sname = getattr(_s, 'name', '') or getattr(_s, 'description', '')
                            _sdesc = getattr(_s, 'description', '')
                        _slabel = _sname or _sdesc
                        _gep_intro_lines.append(f"{_si}. {_slabel}")
                        if _sdesc and _sdesc != _slabel:
                            _gep_intro_lines.append(f"   {_sdesc}")
                # (2) Parameters block (emphasis first, then the rest)
                _ps = getattr(_t_obj, 'params_schema', {}) or {}
                if _ps:
                    def _gep_get_default(spec):
                        return spec.get('default') if isinstance(spec, dict) else getattr(spec, 'default', None)
                    def _gep_get_type(spec):
                        return (spec.get('type', '') if isinstance(spec, dict) else getattr(spec, 'type', '')) or ''
                    def _gep_get_desc(spec):
                        return (spec.get('description', '') if isinstance(spec, dict) else getattr(spec, 'description', '')) or ''
                    def _gep_get_emphasis(spec):
                        return spec.get('emphasis', False) if isinstance(spec, dict) else getattr(spec, 'emphasis', False)
                    _ps_emphasis = [(n, s) for n, s in _ps.items() if _gep_get_emphasis(s)]
                    _ps_normal   = [(n, s) for n, s in _ps.items() if not _gep_get_emphasis(s)]
                    if _ps_emphasis:
                        _gep_intro_lines.append("### ▶ Key Parameters — please review")
                        for _pname, _pspec in _ps_emphasis:
                            _pdesc    = _gep_get_desc(_pspec)
                            _pdefault = _gep_get_default(_pspec)
                            _ptype    = _gep_get_type(_pspec)
                            _ptype_str = f" `[{_ptype}]`" if _ptype else ""
                            _gep_intro_lines.append(
                                f"- **{_pname}**{_ptype_str} *(default: {_pdefault})*: {_pdesc}"
                            )
                    if _ps_normal:
                        _gep_intro_lines.append("### ⚙️ Other Parameters")
                        for _pname, _pspec in _ps_normal:
                            _pdesc    = _gep_get_desc(_pspec)
                            _pdefault = _gep_get_default(_pspec)
                            _ptype    = _gep_get_type(_pspec)
                            _ptype_str = f" `[{_ptype}]`" if _ptype else ""
                            _gep_intro_lines.append(
                                f"- **{_pname}**{_ptype_str} *(default: {_pdefault})*: {_pdesc}"
                            )
            else:
                _gep_intro_lines.append("*(parameters unavailable — template object not loaded)*")
            _gep_intro_lines.append(
                f"> ⏱️ Auto-entering GEP mode in {int(self._GEP_CONFIRM_TIMEOUT)}s — "
                f"provide parameters above, type 'yes' to start immediately, "
                f"or 'no' to use normal planning mode."
            )
            self.interaction_manager.display_receptionist_reply("\n\n".join(_gep_intro_lines)+
                                                                f"\n\n> ⏱️ GEP mode will start in {int(self._GEP_CONFIRM_TIMEOUT)} seconds. "
                    f"Please prepare your input.")
            _last_tmux_tick_second: int = -1
            try:
              while self._pending_gep_template_id:
                if self.interaction_manager.is_exit_requested():
                    self._pending_gep_template_id = None
                    break
                if time.monotonic() >= self._gep_confirm_deadline:
                    # Timeout (or user confirmed via deadline=0): activate the template.
                    # Reuse the already-loaded object; fall back to load_template only
                    # if the initial load failed.
                    template = None
                    try:
                        template = self._pending_gep_template_obj or load_template(
                            self._pending_gep_template_id
                        )
                    except FileNotFoundError as _gep_e:
                        self.logger.warning(
                            f"GEP template file not found, cannot activate: {_gep_e}",
                            component="FlowController",
                        )
                    except Exception as _gep_e:
                        self.logger.warning(
                            f"GEP template load failed: {_gep_e}",
                            component="FlowController",
                        )
                    if template is not None:
                        self.planner._gep_template = template
                        _activation_msg = (
                            f"[GEP] Template '{template.name}' (v{template.version}) activated. "
                            "GEP execution is now starting "
                            "-- the plan will follow the proven step sequence from the template."
                        )
                        self.interaction_manager.display_message(_activation_msg)
                        self.logger.info(
                            f"GEP template activated: {template.name}",
                            component="FlowController",
                        )
                    self._pending_gep_template_id = None
                    break
                # ── STATUS BAR countdown tick (every second) ─────────────────
                # Skip update when deadline is inf (paused during LLM call) so
                # the bar does not flash 0 while waiting for the response.
                if not math.isinf(self._gep_confirm_deadline):
                    _remaining_secs = max(0, int(self._gep_confirm_deadline - time.monotonic()))
                    if _remaining_secs != _last_tmux_tick_second:
                        _last_tmux_tick_second = _remaining_secs
                        self.interaction_manager.notify_gep_countdown(
                            _remaining_secs, _display_name
                        )
                # ─────────────────────────────────────────────────────────────
                # _make_gep_confirm_callback handles all cases:
                # GEP_CONFIRM → sets deadline=0 (activates on next tick)
                # GEP_DECLINE → clears _pending_gep_template_id (exits loop)
                # other input → resets deadline; loop continues
                await asyncio.sleep(self._LOOP_SLEEP_INTERVAL)
            finally:
                # Clear countdown from status bar regardless of how the loop exits
                # (normal completion, cancellation, or crash).
                try:
                    self.interaction_manager.notify_gep_countdown(-1, "")
                except Exception:
                    pass
            self._pending_gep_template_obj = None  # always clean up

        # Switch to task-execution callback after the GEP confirm window
        # (or immediately when no GEP was matched).
        self.interaction_manager.set_evaluate_callback(
            self._make_evaluate_callback(goal)
        )

        # ── GEP template direct instantiation ────────────────────────────────
        # When a template was confirmed, skip the first observe_and_plan() LLM
        # call and instead:
        #   1. Call extract_gep_params() (one LLM call) to resolve {{params.X}}.
        #   2. Instantiate the template steps directly → next_steps_list.
        #   3. Enrich the session goal with the proven step sequence so all
        #      subsequent replans see that context without the template object.
        #   4. Mark the initial message as seen (last_seen_msg_idx advance) so
        #      the first loop iteration skips observe_and_plan() and goes straight
        #      to step dispatch.
        # Template is cleared in the `finally` block regardless of outcome;
        # on failure the loop falls through to normal observe_and_plan().
        if self.planner._gep_template is not None:
            try:
                # Use accumulated param context as goal when available so that
                # parameter values provided during the confirmation window are
                # visible to the LLM during template step instantiation.
                _instantiation_goal = self._gep_param_context or goal
                _tmpl_name_hint = (
                    self.planner._gep_template.name
                    if not isinstance(self.planner._gep_template, dict)
                    else self.planner._gep_template.get('name', 'template')
                )
                self.interaction_manager.display_message(
                    f"[GEP] Resolving parameters and adapting steps for '{_tmpl_name_hint}' — please wait..."
                )
                gep_plan = await self.planner.instantiate_gep_plan(_instantiation_goal)
                if gep_plan is not None and gep_plan.next_steps:
                    self.current_plan = gep_plan
                    next_steps_list   = list(gep_plan.next_steps)
                    # Enrich goal: embeds step sequence for downstream planner
                    # calls; template object is gone after this block.
                    goal = self._build_gep_enriched_goal(goal, next_steps_list)
                    self.interaction_manager.set_evaluate_callback(
                        self._make_evaluate_callback(goal)
                    )
                    # Show a structured summary: which parameters were resolved
                    # and how each step is designed.  No other verbose output.
                    _summary_lines = ["[GEP] Template instantiated successfully."]
                    _tmpl_for_summary = self.planner._gep_template
                    _ps_summary = (
                        getattr(_tmpl_for_summary, 'params_schema', {})
                        if not isinstance(_tmpl_for_summary, dict)
                        else _tmpl_for_summary.get('params_schema', {})
                    ) or {}
                    if _ps_summary:
                        _summary_lines.append("  Parameters resolved:")
                        for _spname, _spspec in _ps_summary.items():
                            if isinstance(_spspec, dict):
                                _spdefault = _spspec.get('default')
                            else:
                                _spdefault = getattr(_spspec, 'default', None)
                            _spval = _spdefault if _spdefault is not None else "(provided by user)"
                            _summary_lines.append(f"    • {_spname} = {_spval}")
                    _summary_lines.append("  Steps:")
                    for _si, _ss in enumerate(next_steps_list, 1):
                        _summary_lines.append(f"    {_si}. {_ss.description}")
                    self.interaction_manager.display_message("\n".join(_summary_lines))
                    # Skip the first loop iteration's Phase 3 so execution
                    # begins immediately without an extra planning LLM call.
                    last_seen_msg_idx = len(replan_history)
                    if next_steps_list[0].planner_reasoning:
                        self.interaction_manager.notify_reasoning(
                            next_steps_list[0].planner_reasoning,
                            0,
                        )
                    self.logger.info(
                        f"GEP direct instantiation: {len(next_steps_list)} steps",
                        component="FlowController",
                    )
            except Exception as _gep_exc:
                self.logger.warning(
                    f"GEP instantiation failed: {_gep_exc} "
                    f"— falling back to normal planning",
                    component="FlowController",
                )
                self.interaction_manager.display_message(
                    "[GEP] Template instantiation failed — using normal planning mode. "
                    f"Reason: {_gep_exc}"
                )
            finally:
                self.planner._gep_template = None
                self._gep_param_context = ""  # clean up

        while True:
            # ── Exit check ───────────────────────────────────────────────────
            if self.interaction_manager.is_exit_requested():
                self.logger.info(
                    "Session ended by user request (:exit or :new command)",
                    component="FlowController",
                )
                self._transition_state(SystemState.COMPLETED)
                return {"success": True, "message": "Session ended by user request."}

            # ── Phase 1: Collect REPLAN messages ─────────────────────────────
            replan_history = await self._collect_replan_messages(
                replan_history
            )

            # ── Phase 2: Collect step result (non-blocking) ──────────────────
            # Hold as pending_step; commit to memory in Phase 3 after confidence
            # evaluation so the final state (COMPLETED/FAILED) is written once.
            step_result_received = False
            pending_step: Optional[Step] = None
            if self._completed_step is not None:
                pending_step = self._completed_step
                self._completed_step = None
                step_result_received = True
                in_flight_batch = None

            # ── Phase 3: Replan on new step result or REPLAN message ─────────
            has_new_messages = last_seen_msg_idx < len(replan_history)

            if step_result_received or has_new_messages:
                self.logger.debug("Trigger ob and replan", component="FlowController")
                # FIX P1-FIX-3: use _transition_state so self.state is updated.
                self._transition_state(SystemState.REPLANNING)
                completed = self.memory.get_completed_steps()
                # Include pending_step so confidence is evaluated on the
                # just-finished step before it is committed to memory.
                completed_for_plan = completed + ([pending_step] if pending_step else [])

                # Build the interleaved timeline (may be None when no user messages).
                timeline = self._build_interleaved_timeline(
                    completed_steps=completed_for_plan,
                    message_history=replan_history,
                    last_seen_idx=last_seen_msg_idx,
                )

                # Append in-flight agent progress when a user message triggered
                # this replan and an agent is currently executing.  This gives
                # the Planner visibility into what the running agent has already
                # tried, preventing blind replanning on stale assumptions.
                # Only injected when has_new_messages (pure step-result replans
                # don't need it — the step result itself carries that information).
                if has_new_messages and self._current_agent is not None:
                    agent_summary = self._current_agent.get_progress_summary()
                    if agent_summary:
                        self.logger.debug(
                            "Injecting in-flight agent summary into replan context",
                            component="FlowController",
                        )
                        prefix = timeline + "\n\n" if timeline else ""
                        timeline = (
                            prefix
                            + "[In-flight Agent Status]\n"
                            + "The step below is currently executing. "
                            + "Use this to decide whether to interrupt it "
                            + "(interrupt_current_step: true) or let it finish.\n"
                            + agent_summary
                        )

                # Proactively compress old findings before passing to planner.
                await self.memory.compress_findings_async(self._planner_services)

                self.current_plan = await self.planner.observe_and_plan(
                    goal=goal,
                    completed_steps=completed_for_plan,
                    current_lookahead=(in_flight_batch or []) + next_steps_list,
                    user_message=timeline,
                    accumulated_findings=self.memory.get_accumulated_findings_for_planner(
                        already_covered_count=self.memory.count_context_entries_in_last_n_steps(self.planner.detail_window)
                    ),
                )
                # Compress early steps when context grows large; takes effect for the next call.
                await self.planner.maybe_compress_steps(completed_for_plan)
                if self.planner._gep_template is not None:
                    self.planner._gep_template = None
                    self.logger.info(
                        "GEP template still set after observe_and_plan "
                        "(fallback clear — should not normally happen)",
                        component="FlowController",
                    )
                self.logger.debug(f"Current plan is: {self.current_plan}")

                if self._execution_recorder:
                    self._execution_recorder.write_plan_snapshot(self.current_plan, is_replan=True)

                last_seen_msg_idx = len(replan_history)

                # Confidence check — only when a new step result was received.
                # Guard: pending_step must be non-None so the failure is attributed
                # to the just-finished step, not to stale historical steps.
                # (completed_for_plan can be non-empty even when pending_step is None
                # because it includes already-committed history, which would cause a
                # spurious confidence failure and an incorrect re-plan.)
                confidence = self.current_plan.last_step_confidence
                confidence_failed = (
                    step_result_received
                    and pending_step is not None
                    and (confidence is None or confidence < self.step_verification_threshold)
                )

                # Write planner confidence to the execution log so the chain-of-thought
                # shows how the planner evaluated the just-completed step.
                if (step_result_received
                        and pending_step is not None
                        and confidence is not None
                        and self._execution_recorder):
                    self._execution_recorder.write_step_confidence(
                        confidence=confidence,
                        threshold=self.step_verification_threshold,
                        passed=not confidence_failed,
                        rationale=self.current_plan.confidence_rationale,
                    )

                # Commit pending_step with its final state in a single add_step call.
                if pending_step is not None:
                    if confidence_failed:
                        pending_step.update_status(StepStatus.FAILED)
                        confidence_str = f"{confidence:.2f}" if confidence is not None else "N/A"
                        pending_step.issues = list(pending_step.issues or []) + [
                            f"Planner verification failed "
                            f"(confidence {confidence_str} < "
                            f"{self.step_verification_threshold:.2f})"
                        ]
                    self.memory.add_step(pending_step)
                    # Persist compact session state for planner and agent reference.
                    # Written after every committed step so the file always reflects
                    # current progress, surviving planner context compression.
                    self._write_session_state(self.memory.get_completed_steps())
                    if confidence is not None:
                        self.interaction_manager.notify_step_confidence(confidence)
                    completed_count = len(self.memory.get_completed_steps())
                    self.interaction_manager.display_progress(
                        completed_count, completed_count + len(next_steps_list)
                    )
                    # Record step metrics NOW — confidence is genuinely known here
                    # (computed by observe_and_plan() above), unlike in _execute_step()
                    # where it was not yet available and was hardcoded as 0.0.
                    # _metrics_iterations / _metrics_tools_used were stashed on the
                    # step object by _execute_step() for exactly this purpose.
                    if self._current_plan_id:
                        _step_idx = completed_count - 1  # 0-based index of just-committed step
                        self._metrics_collector.record_step_result(
                            plan_id=self._current_plan_id,
                            step_index=_step_idx,
                            confidence=confidence if confidence is not None else 0.0,
                            iterations=getattr(pending_step, '_metrics_iterations', 1),
                            tool_names_tried=getattr(pending_step, '_metrics_tools_used', []),
                            success=not confidence_failed and pending_step.has_status(StepStatus.COMPLETED),
                            token_usage=getattr(pending_step, '_metrics_token_usage', None),
                        )

                # Progress tracking — only on new step results to avoid double-counting.
                if step_result_received:
                    progress_tracker.add_step_result(not confidence_failed)
                    progress_status = progress_tracker.analyze()

                    if progress_status.should_abort:
                        self.logger.error(
                            f"Aborting: {progress_status.abort_reason}",
                            component="FlowController",
                        )
                        self._transition_state(SystemState.ERROR)
                        error_msg = f"Task aborted: {progress_status.abort_reason}"
                        
                        # Display error and continue waiting for user input
                        self.interaction_manager.display_error(error_msg)
                        self.logger.info(
                            "Task aborted (UI mode) — continuing loop for user input",
                            component="FlowController",
                        )
                        # Clear next_steps_list to prevent executing stale steps
                        next_steps_list = []
                        # Keep history intact; user may provide guidance to retry
                        continue

                    if (progress_status.should_inject_reminder
                            and progress_status.reminder_message):
                        step_count_now = len(self.memory.get_completed_steps())
                        replan_history.append(
                            (progress_status.reminder_message, step_count_now)
                        )
                        if progress_status.should_assess_feasibility:
                            self.logger.warning(
                                "Feasibility assessment triggered",
                                component="FlowController",
                            )
                        else:
                            self.logger.warning(
                                "Stagnation detected — injecting reminder",
                                component="FlowController",
                            )

                if confidence_failed:
                    # Step's last_step_confidence failed the threshold.  This may
                    # be a regular execution step OR the optional acceptance_test_
                    # step injected by synthesize_acceptance().  Both go through
                    # the same corrective-replan path; the next is_complete
                    # cycle will re-run synthesize_acceptance() against the
                    # corrected state.
                    self.logger.warning(
                        f"Planner confidence for last step: "
                        f"{confidence:.2f} < threshold "
                        f"{self.step_verification_threshold:.2f} "
                        f"— step committed as FAILED; "
                        f"using corrective next_steps from current plan",
                        component="FlowController",
                    )
                    # Record replanning event for metrics.
                    if self._current_plan_id:
                        self._metrics_collector.record_replan(
                            self._current_plan_id,
                            trigger_message='',
                        )
                    # The Planner prompt instructs observe_and_plan to begin next_steps
                    # with a corrective step when confidence < threshold, so the plan
                    # already returned above contains the recovery steps.
                    # No additional observe_and_plan call is needed.
                    if not self.current_plan.next_steps:
                        self.logger.error(
                            "Confidence failed and planner has "
                            "no recovery steps. Aborting.",
                            component="FlowController",
                        )
                        self._transition_state(SystemState.ERROR)
                        error_msg = (
                            "Task failed: verification confidence too low "
                            f"({confidence:.2f} < "
                            f"{self.step_verification_threshold:.2f}) "
                            "and no recovery steps available."
                        )

                        # Display error and continue waiting for user input
                        self.interaction_manager.display_error(error_msg)
                        self.logger.info(
                            "Confidence failed with no recovery (UI mode) — continuing loop for user input",
                            component="FlowController",
                        )
                        # Clear next_steps_list to prevent executing stale steps
                        next_steps_list = []
                        # Keep history intact; user may provide guidance to retry
                        continue

                    # Recovery steps are available — adopt them and continue
                    # immediately.  The interrupt-handling block that follows
                    # new_next_steps = list(...) belongs exclusively to the
                    # confidence-passed (else) branch; falling through into it
                    # from the confidence_failed branch is incorrect.
                    # After replan → recovery steps → those steps complete →
                    # is_complete=True, synthesize_acceptance() will run again
                    # against the corrected state.
                    next_steps_list = list(self.current_plan.next_steps)
                    if next_steps_list and next_steps_list[0].planner_reasoning:
                        self.interaction_manager.notify_reasoning(
                            next_steps_list[0].planner_reasoning,
                            self.current_plan.token_count,
                        )
                    continue

                else:
                    # Confidence passed (or no step result this iteration).
                    # When step_result_received=False (pure-message replan), confidence
                    # may be None; trust the Planner's empty next_steps directly rather
                    # than requiring a numeric confidence, so we never raise a spurious
                    # "empty next_steps" exception when the Planner signals completion
                    # via completion_reason alone.
                    is_complete = not self.current_plan.next_steps and (
                        (confidence is not None
                         and confidence >= self.step_verification_threshold)
                        or not step_result_received
                    )
                    if is_complete:
                        # ── Goal-level acceptance synthesis ───────────────────
                        # Replaces the prior independent verification-agent
                        # step.  Per-step confidence has already passed for
                        # every step in completed_steps; this is the single
                        # goal-level seam check + optional narrow code-test.
                        #
                        # Possible outcomes:
                        #   PASS (no test step)     → complete the task
                        #   PASS + code_test_step   → inject test step, loop
                        #   PARTIAL / FAIL          → inject corrective step, loop

                        _do_complete = False  # set True when ready to declare done

                        completed_steps = self.memory.get_completed_steps()
                        has_code_edits = self._has_code_edits(completed_steps)
                        already_tested = any(
                            s.step_id.startswith('acceptance_test_')
                            and s.has_status(StepStatus.COMPLETED)
                            for s in completed_steps
                        )
                        try:
                            verdict = await self.planner.synthesize_acceptance(
                                original_goal=goal,
                                completed_steps=completed_steps,
                                has_code_edits=has_code_edits,
                                accumulated_findings=(
                                    self.memory.get_accumulated_findings_for_planner()
                                ),
                                already_tested=already_tested,
                            )
                        except Exception as e:
                            self.logger.error(
                                f'Acceptance synthesis failed: {e}',
                                component='FlowController',
                            )
                            if self._execution_recorder:
                                self._execution_recorder.write_acceptance_decision(
                                    verdict='SKIPPED',
                                    rationale=f'Exception during synthesis: {e}',
                                )
                            _do_complete = True
                            verdict = None

                        if verdict is not None:
                            if self._execution_recorder:
                                self._execution_recorder.write_acceptance_decision(
                                    verdict=verdict.verdict,
                                    rationale=verdict.gap_summary,
                                    has_test_step=verdict.code_test_step is not None,
                                )

                            if verdict.verdict == 'PASS' and verdict.code_test_step is None:
                                _do_complete = True
                            elif verdict.verdict == 'PASS' and verdict.code_test_step is not None:
                                test_step = self._dedup_step_id(
                                    verdict.code_test_step, completed_steps
                                )
                                self.logger.info(
                                    f"Acceptance PASS with code-test step: {test_step.step_id}",
                                    component='FlowController',
                                )
                                next_steps_list = [test_step]
                                continue
                            else:
                                # PARTIAL / FAIL
                                if verdict.corrective_step is None:
                                    self.logger.warning(
                                        f'Acceptance verdict={verdict.verdict} but no '
                                        f'corrective_step — completing anyway',
                                        component='FlowController',
                                    )
                                    _do_complete = True
                                else:
                                    corrective = self._dedup_step_id(
                                        verdict.corrective_step, completed_steps
                                    )
                                    self.logger.info(
                                        f"Acceptance {verdict.verdict} — "
                                        f"injecting corrective: {corrective.step_id} "
                                        f"({verdict.gap_summary[:80]})",
                                        component='FlowController',
                                    )
                                    next_steps_list = [corrective]
                                    continue

                        if _do_complete:
                            self._transition_state(SystemState.COMPLETED)
                            reason = (
                                self.current_plan.completion_reason
                                or "Task completed successfully"
                            )
                            # Surface non-PASS acceptance gap to the user so
                            # they can decide during review whether the gap
                            # warrants a follow-up.  Per anti-pattern #5 the
                            # synthesis prompt restricts gap_summary to the
                            # PRIMARY deliverable, so intermediate-artifact
                            # quirks should not show up here.  Synthesis
                            # exceptions (verdict is None) skip this branch.
                            if (
                                verdict is not None
                                and verdict.verdict != 'PASS'
                                and verdict.gap_summary
                            ):
                                reason = (
                                    f"{reason}\n\n"
                                    f"[Acceptance: {verdict.verdict}] "
                                    f"{verdict.gap_summary}"
                                )
                            if self._execution_recorder:
                                self._execution_recorder.completion_reason = reason
                            if self._current_plan_id:
                                self._metrics_collector.record_task_end(
                                    self._current_plan_id, success=True,
                                    duration_seconds=time.monotonic() - self._task_start_time,
                                )
                            # Write metrics_summary.json BEFORE notifying completion so
                            # the foreground process can read it when it detects state3.
                            self._report_metrics()
                            # Run end-of-task finaliser (user hook → internal
                            # resource flushes) BEFORE notifying the user so
                            # side-effects are flushed to disk before the user
                            # can see "complete" and quit the process.
                            await self._finalize_task()
                            self.interaction_manager.notify_task_completed(reason)
                            self.logger.info(
                                "Task complete (UI mode) — continuing loop for follow-up",
                                component="FlowController",
                            )
                            self._transition_state(SystemState.IDLE)
                            next_steps_list = []
                            continue

                    if not self.current_plan.next_steps:
                        raise Exception(
                            "Planner returned empty next_steps but task not complete"
                        )

                new_next_steps = list(self.current_plan.next_steps)

                # Interrupt the running step if Planner requests it and agent is busy.
                # Use in_flight_batch (Planner-owned) rather than _agent_idle_event
                # (Agent-owned) to avoid a race: _agent_idle_event stays set between
                # _step_batch_event.set() and _agent_idle_event.clear(), but
                # in_flight_batch is race-free (only the Planner loop modifies it).
                if (self.current_plan.interrupt_current_step
                        and in_flight_batch is not None):
                    self.logger.info(
                        "Interrupting agent step (Planner decision)",
                        component="FlowController",
                    )
                    # Record interrupt event for metrics.
                    if self._current_plan_id:
                        self._metrics_collector.record_interrupt(
                            self._current_plan_id
                        )
                    self._interrupt_event.set()
                    await self._agent_idle_event.wait()
                    self._interrupt_event.clear()

                    # Collect interrupted step; use execution status as success proxy
                    # (full confidence re-evaluation would require another observe_and_plan).
                    if self._completed_step is not None:
                        interrupted_step = self._completed_step
                        self._completed_step = None
                        # Replace the internal interrupt sentinel with a human-readable
                        # label so it does not pollute the Planner's history.
                        # RuntimeAgent stores "PLANNER_INTERRUPT" as agent_runtime_reasoning
                        # when a step is aborted, which is not meaningful to the Planner.
                        if interrupted_step.agent_runtime_reasoning == "PLANNER_INTERRUPT":
                            interrupted_step.agent_runtime_reasoning = (
                                "[Step was interrupted by Planner before completion]"
                            )
                        self.memory.add_step(interrupted_step)
                        self._write_session_state(self.memory.get_completed_steps())
                        progress_tracker.add_step_result(
                            interrupted_step.has_status(StepStatus.COMPLETED)
                        )
                    elif in_flight_batch:
                        # Agent was interrupted before producing any result
                        # (e.g. the step was aborted before the first Think call
                        # completed).  Record the first step of the in-flight batch
                        # as FAILED so the Planner has a complete execution history
                        # and loop-detection works correctly.  Without this record
                        # the Planner would have no evidence the step was ever
                        # attempted, potentially re-generating the same step and
                        # triggering a spurious loop.
                        placeholder = in_flight_batch[0]
                        placeholder.update_status(StepStatus.FAILED)
                        placeholder.issues = list(placeholder.issues or []) + [
                            "Step interrupted by Planner before producing a result"
                        ]
                        self.memory.add_step(placeholder)
                        self._write_session_state(self.memory.get_completed_steps())
                        progress_tracker.add_step_result(False)
                        self.logger.warning(
                            f"Interrupted step '{placeholder.step_id}' "
                            f"produced no result — recorded as FAILED in memory",
                            component="FlowController",
                        )
                    in_flight_batch = None

                    # The regular replan above already generated new_next_steps
                    # accounting for the user message and interrupt decision.
                    # The interrupted step's result is committed to memory above
                    # and will be visible to the Planner in the next iteration.
                    # No additional observe_and_plan call is needed here.
                    if not new_next_steps:
                        self._transition_state(SystemState.COMPLETED)
                        reason = (
                            self.current_plan.completion_reason
                            or "Task completed successfully"
                        )
                        if self._execution_recorder:
                            self._execution_recorder.completion_reason = reason
                        # ── Notify completion, then continue loop ────────────
                        # Do NOT return — the user may type a follow-up message
                        # that triggers a replan within the same session.
                        # Only :exit / :new (checked at the top of the loop via
                        # is_exit_requested()) will end the session.
                        # Record metrics per-task here because _planner_loop does NOT
                        # return after completion — it continues waiting for follow-up.
                        # record_task_end() is idempotent per plan_id, so it is safe
                        # to call again at session-end without double-counting.
                        if self._current_plan_id:
                            self._metrics_collector.record_task_end(
                                self._current_plan_id, success=True,
                                duration_seconds=time.monotonic() - self._task_start_time,
                            )
                        # Write metrics_summary.json BEFORE notifying completion so
                        # the foreground process can read it when it detects state3.
                        self._report_metrics()
                        # Run end-of-task finaliser (user hook → internal
                        # resource flushes) — same guarantee as the normal path.
                        await self._finalize_task()
                        self.interaction_manager.notify_task_completed(reason)
                        self.logger.info(
                            "Task complete after interrupt (UI mode) — continuing loop for follow-up",
                            component="FlowController",
                        )
                        # FIX P0-FIX-2: reset to IDLE so the state machine is
                        # ready for the next user message / follow-up task.
                        self._transition_state(SystemState.IDLE)

                        # Keep all execution history intact so that if the user
                        # is unsatisfied with the result and provides feedback,
                        # the planner can see the full context and decide whether
                        # additional steps are needed.
                        # next_steps_list is already empty (task complete).
                        # When the user types a new message, it will trigger a
                        # replan with full history visible to the planner.

                        # Skip the rest of Phase 3 for this iteration and go
                        # straight to the sleep / next iteration.
                        continue

                next_steps_list = new_next_steps

                if next_steps_list and next_steps_list[0].planner_reasoning:
                    self.interaction_manager.notify_reasoning(
                        next_steps_list[0].planner_reasoning,
                        self.current_plan.token_count,
                    )

            # ── Feed next batch to Agent ─────────────────────────────────────
            # in_flight_batch is the single guard: set at dispatch, cleared on
            # result collection or interrupt.
            if in_flight_batch is None and next_steps_list:
                first = next_steps_list[0]
                in_flight_batch = (
                    [s for s in next_steps_list
                     if s.parallel_group == first.parallel_group]
                    if first.parallel_group
                    else [first]
                )
                # Set-based removal handles non-contiguous parallel groups correctly.
                in_flight_ids = {s.step_id for s in in_flight_batch}
                next_steps_list = [s for s in next_steps_list
                                   if s.step_id not in in_flight_ids]
                # Clear _agent_idle_event HERE (Planner-side) before signalling
                # the Agent, not after the Agent reads the batch.  This closes
                # the race window where _agent_idle_event stays set between
                # _step_batch_event.set() and _agent_idle_event.clear() in the
                # Agent loop: if the Planner fires an interrupt in that window,
                # await _agent_idle_event.wait() would return immediately with
                # the stale set-state, causing _interrupt_event to be cleared
                # before the Agent ever checks it.  By clearing here the event
                # is already unset when the Agent loop runs, so the Planner's
                # wait() correctly blocks until the Agent finishes the batch.
                self._agent_idle_event.clear()
                self._agent_step_batch = in_flight_batch
                self._step_batch_event.set()
                # FIX P1-FIX-3: use _transition_state so self.state is updated.
                self._transition_state(SystemState.EXECUTING)

            await asyncio.sleep(self._LOOP_SLEEP_INTERVAL)

    # ── Planner loop helpers ──────────────────────────────────────────────────

    async def _collect_replan_messages(
        self,
        replan_history: List[Tuple[str, int]],
    ) -> List[Tuple[str, int]]:
        """
        Pick up any messages the Receptionist has already classified as REPLAN
        and append them to replan_history with the current step count.

        The message processor runs concurrently: it calls evaluate_user_message()
        (or, for the first message, classify_initial_goal()), displays the
        response to the user immediately, and only forwards REPLAN-intent
        messages to the pending queue.  By the time this method runs, each
        message has already been shown to the user — this method just records
        them so observe_and_plan() can incorporate them into the next plan.

        Returns replan_history explicitly (rather than mutating in place) so
        callers always reassign the variable, preventing silent staleness bugs.
        """
        current_step_count = len(self.memory.get_completed_steps())

        while True:
            msg = self.interaction_manager.get_pending_user_message()
            if not msg:
                break

            replan_history.append((msg, current_step_count))
            self.logger.info(
                f"REPLAN message collected: {msg[:60]}",
                component="FlowController",
            )

        return replan_history

    # ── Interleaved timeline ──────────────────────────────────────────────────

    @staticmethod
    def _build_interleaved_timeline(
        completed_steps: List[Step],
        message_history: List[Tuple[str, int]],
        last_seen_idx: int,
    ) -> Optional[str]:
        """
        Build a chronological timeline of completed steps and user messages for the Planner.

        Messages with step_count=N are inserted after step N.
        [NEW] marks messages that triggered the current reconsider.
        Returns None when there are no user messages — without messages there is nothing
        to interleave, and step history is already fully covered by completed_steps.
        """
        if not message_history:
            return None

        events: List[Tuple[Tuple[int, int, int], str, str]] = []

        for i, step in enumerate(completed_steps):
            status = "✅" if step.has_status(StepStatus.COMPLETED) else "❌"
            label = f"[Step {i + 1}] {step.description} {status}"
            events.append(((i, 0, 0), "step", label))

        for msg_idx, (msg, step_count) in enumerate(message_history):
            tag = "[User, NEW]" if msg_idx >= last_seen_idx else "[User, earlier]"
            label = f"{tag} {msg}"
            events.append(((step_count, 1, msg_idx), "message", label))

        events.sort(key=lambda e: e[0])

        lines = ["Execution timeline (steps and user instructions interleaved):"]
        for _, _, label in events:
            lines.append(f"  {label}")

        has_new = last_seen_idx < len(message_history)
        if has_new:
            lines.append(
                "\nNote: [NEW] instruction(s) above triggered this reconsider. "
                "Steps executed after a [NEW] instruction were already in-flight "
                "and may or may not have responded to it — assess based on their "
                "descriptions and the detailed step history below."
            )

        return "\n".join(lines)

    # ── Step execution ────────────────────────────────────────────────────────

    @staticmethod
    async def _broadcast_interrupt(
        source: asyncio.Event,
        targets: List[asyncio.Event],
    ) -> None:
        """
        Wait for *source* to be set, then set every event in *targets*.

        Designed to run as a background asyncio.Task alongside asyncio.gather
        so that each parallel sub-agent has its own interrupt event while still
        responding to the global _interrupt_event signal.  The task is cancelled
        (and awaited) immediately after gather returns, so it never outlives the
        chunk it was created for.

        If *source* is already set when this coroutine starts, all targets are
        set on the first iteration without any suspension — correct behaviour
        for the case where the Planner fired the interrupt before gather began.
        """
        await source.wait()
        for t in targets:
            t.set()

    async def _execute_steps(self, batch: List[Step]) -> Step:
        """
        Execute a batch of steps.

        Single step: run directly. Parallel batch: run non-aggregation steps
        concurrently in chunks of MAX_PARALLEL_BATCH_SIZE, then run aggregation.

        Interrupt isolation: each parallel sub-agent receives its own per-agent
        asyncio.Event driven by _broadcast_interrupt.  When _interrupt_event fires,
        only the parallel sub-agents are interrupted; the aggregation step that
        follows uses _interrupt_event directly (interrupt_event=None) and is
        therefore unaffected, allowing it to produce a meaningful summary of the
        (partial) sub-task results even after a mid-batch interrupt.
        """
        if not batch:
            raise ValueError("_execute_steps received an empty batch")

        parallel_steps = [s for s in batch if not s.is_aggregation]
        agg_step = next((s for s in batch if s.is_aggregation), None)

        # Fast path: single non-parallel step with no aggregation.
        if len(parallel_steps) == 1 and not agg_step:
            return await self._execute_step(parallel_steps[0])

        # Fast path: batch contains only an aggregation step (no parallel workers).
        # Execute it directly to avoid entering the parallel loop with total=0,
        # which would pass an empty results_text to the aggregation step.
        if len(parallel_steps) == 0:
            if agg_step:
                return await self._execute_step(agg_step)
            raise ValueError(
                "_execute_steps: batch has no parallel steps and no aggregation step"
            )

        total = len(parallel_steps)
        chunk_size = self.MAX_PARALLEL_BATCH_SIZE
        num_chunks = (total + chunk_size - 1) // chunk_size
        self.logger.info(
            f"Parallel execution: {total} agents "
            f"({'1 chunk' if num_chunks == 1 else f'{num_chunks} chunks of ≤{chunk_size}'})",
            component="FlowController"
        )

        completed: List[Step] = []
        for chunk_idx in range(num_chunks):
            chunk = parallel_steps[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size]
            if num_chunks > 1:
                self.logger.info(
                    f"  Chunk {chunk_idx + 1}/{num_chunks}: {len(chunk)} agents",
                    component="FlowController"
                )

            # Per-agent interrupt isolation: one independent asyncio.Event per agent.
            # _broadcast_interrupt propagates self._interrupt_event → per-agent events,
            # so sub-agents are interrupted by the global signal without affecting the
            # aggregation step that runs after gather.
            agent_interrupt_events: List[asyncio.Event] = [
                asyncio.Event() for _ in chunk
            ]

            # ── SSH pre-flight: establish credentials sequentially ────────────
            # Run all StepContextProvider.prepare() calls sequentially before
            # asyncio.gather so that:
            #   1. Password prompts never overlap (no tty conflicts).
            #   2. Per-hostname credentials are cached in memory before parallel
            #      execution begins; subsequent prepare() calls inside
            #      _execute_step() get a cheap cache hit.
            # prepare() is idempotent: calling it again for the same hostname
            # returns the brief hint without re-prompting or re-connecting.
            for _preflight_step in chunk:
                _pf_required = getattr(_preflight_step, "tools_required", None) or []
                for _provider in self._step_context_providers:
                    _pf_tool = getattr(_provider, "tool_name", None)
                    if not _pf_tool or _pf_tool not in _pf_required:
                        continue
                    try:
                        await _provider.prepare(
                            _preflight_step,
                            self.interaction_manager,
                            self.memory,
                        )
                    except Exception as _pf_exc:
                        self.logger.warning(
                            f"SSH pre-flight failed for step "
                            f"{_preflight_step.step_id}: {_pf_exc}",
                            component="FlowController",
                        )

            broadcast_task = asyncio.create_task(
                self._broadcast_interrupt(self._interrupt_event, agent_interrupt_events)
            )

            try:
                raw_results = await asyncio.gather(
                    *[
                        self._execute_step(
                            s,
                            agent_id=f"agent_{i + 1}",
                            interrupt_event=agent_interrupt_events[i],
                        )
                        for i, s in enumerate(chunk)
                    ],
                    return_exceptions=True,
                )
            finally:
                # Cancel the broadcast task once gather has returned.
                broadcast_task.cancel()
                try:
                    await broadcast_task
                except asyncio.CancelledError:
                    pass

            for i, result in enumerate(raw_results):
                if isinstance(result, Exception):
                    s = chunk[i]
                    self.logger.error(
                        f"Sub-task {s.step_id} raised an exception: {result}",
                        component="FlowController"
                    )
                    failed = Step(
                        step_id=s.step_id,
                        description=s.description,
                        goal=s.goal,
                        issues=[str(result)],
                        agent_runtime_reasoning=f"Sub-task failed with exception: {result}",
                    )
                    failed.update_status(StepStatus.FAILED)
                    completed.append(failed)
                else:
                    assert isinstance(result, Step)
                    completed.append(result)

        results_text = "\n\n".join(
            f"Sub-task {i + 1} ({s.step_id}):\n{s.to_planner_summary()}"
            for i, s in enumerate(completed)
        )

        # Clear the interrupt signal so the aggregation step is not immediately
        # aborted by the same signal that stopped the sub-agents.
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()

        # The aggregation step runs WITHOUT a per-agent interrupt event
        # (interrupt_event=None), so it falls back to self._interrupt_event.
        # It is only interrupted by a *new* Planner signal issued after the
        # parallel gather completes — not by the same signal that stopped the
        # sub-agents.
        if agg_step:
            agg_step.goal = f"{agg_step.goal}\n\nSub-task results:\n{results_text}"
            return await self._execute_step(agg_step)
        else:
            self.logger.warning(
                "Parallel batch has no aggregation step; creating a default one.",
                component="FlowController"
            )
            return await self._execute_step(Step.for_aggregation(
                step_id=f"aggregation_{uuid.uuid4().hex[:8]}",
                goal=f"Summarize sub-task results:\n{results_text}"
            ))

    def _make_confirmation_callback(self):
        """
        Return a confirmation callback for RuntimeAgent.

        Routes tool confirmations and high-risk confirmations.
        If `context` is a registered tool name → request_tool_confirmation.
        Otherwise (RiskGuard description string) → request_risk_confirmation.
        "Other input" on the tool path is queued for Planner evaluation; the tool
        is rejected so the agent re-thinks within the current step.
        """
        from ..models.state import UserConfirmation as UC
        from ..models.decision import Decision as D
        from ..tools.tool_registry import ToolRegistry

        def callback(decision: D, context: str) -> UC:
            if context in ToolRegistry.get_tool_names():
                result = self.interaction_manager.request_tool_confirmation(context, decision)
                if result.has_new_message() and result.message:
                    self.interaction_manager.inject_user_message(result.message)
                    self.logger.info(
                        f"Tool confirmation 'other input' injected "
                        f"for message processor evaluation: {result.message[:60]}",
                        component="FlowController",
                    )
                    return UC.no()
                return result
            else:
                return self.interaction_manager.request_risk_confirmation(decision, context)

        return callback

    def _make_interrupt_callback(
        self, interrupt_event: Optional[asyncio.Event] = None
    ) -> Callable[[], Optional[str]]:
        """
        Return a check_interrupt_callback for RuntimeAgent.

        Called before every Think iteration. Returns "PLANNER_INTERRUPT" to abort,
        or None to continue. Skips interrupt while a confirmation dialog is active.

        Args:
            interrupt_event: When provided (parallel agents), this per-agent event is
                checked instead of self._interrupt_event.  The per-agent event is driven
                by _broadcast_interrupt so it fires at the same time as the global event,
                but is independent: clearing self._interrupt_event after the parallel
                gather does not affect the per-agent events, and the aggregation step
                passes interrupt_event=None so it falls back to self._interrupt_event
                and is not prematurely interrupted by the same signal that stopped the
                sub-agents.
        """
        def callback() -> Optional[str]:
            if self.interaction_manager.is_confirmation_active():
                return None
            # Use the provided per-agent event for parallel agents;
            # fall back to the global event for sequential / aggregation steps.
            event = interrupt_event if interrupt_event is not None else self._interrupt_event
            if event is not None and event.is_set():
                return "PLANNER_INTERRUPT"
            return None

        return callback

    async def _execute_step(
        self,
        step: Step,
        agent_id: Optional[str] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Step:
        """
        Run a single step via RuntimeAgent; return the step with updated status.

        Args:
            step: The step to execute.
            agent_id: Optional identifier for parallel agents (sets logger context).
            interrupt_event: When provided (parallel agents), forwarded to
                _make_interrupt_callback so the agent responds to its own per-agent
                interrupt event rather than the shared self._interrupt_event.
                Sequential steps and the aggregation step pass None (default).
        """
        if agent_id:
            set_agent_context(agent_id)
        else:
            set_agent_context("")

        self.logger.info(
            f"Executing step: {step.step_id} - {step.description}",
            component="FlowController"
        )

        step.update_status(StepStatus.IN_PROGRESS)
        self.interaction_manager.notify_step_started(step.step_id, step.description)

        effective_goal = (
            f"{step.goal}\n\nInput: {step.step_supplement}"
            if step.step_supplement else step.goal
        )

        # Selective context injection: only inject findings for steps explicitly
        # declared in required_context_keys. If the list is empty, the step runs
        # in full isolation (no prior context). This is the runtime enforcement
        # of the planner's dependency declaration.
        # Backward-compatibility: if required_context_keys is absent on the step
        # object (e.g. steps created before this change), fall back to full
        # injection so existing behaviour is preserved.
        required_keys = getattr(step, 'required_context_keys', None)
        if required_keys is None:
            # Backward-compat path: field missing entirely, inject everything.
            task_context = self.memory.get_prior_step_context()
        elif len(required_keys) == 0:
            # Explicit empty list: full isolation, inject nothing.
            task_context = ""
        else:
            # Selective injection: only entries whose key matches a declared step_id.
            task_context = self.memory.get_prior_step_context(filter_keys=required_keys)

        if task_context:
            effective_goal = (
                f"{effective_goal}\n\n[Prior step findings]\n{task_context}"
            )

        # ── Step context providers ─────────────────────────────────────────────
        # Tool activation policy: Planner's step.tools_required is the SINGLE
        # source of truth. Each registered provider serves exactly one tool
        # (provider.tool_name). When that name appears in step.tools_required,
        # the provider's prepare() runs to set up resources (credentials,
        # profile dirs, etc.) and inject a hint into effective_goal.
        #
        # No keyword safety net: if the Planner under-declares, the agent
        # fails for lack of a tool, returns an error JSON, and the next
        # observe_and_plan() round corrects tools_required. This costs one
        # wasted iteration; it preserves agent focus and avoids context
        # inflation from keyword false-positives.
        extra_tool_names: List[str] = list(getattr(step, "tools_required", []) or [])
        self.logger.info(
            f"Step {step.step_id!r}: planner declared tools_required={extra_tool_names}; "
            f"{len(self._step_context_providers)} providers registered",
            component="FlowController",
        )
        # Defense-in-depth: silently drop any tool the user has disabled via
        # interaction switches. Provider registration is already gated in
        # _register_default_providers, so when a tool is disabled neither the
        # provider nor the tool itself reaches the agent. The planner's static
        # prompt may still mention the tool — if it declares one anyway, we
        # strip it here. We DO NOT inject a hint into effective_goal: the
        # agent will simply not have access to the tool, the call (if any)
        # will fail with a normal "tool not available" error, and the planner
        # will replan on the next round. Adding a [Tool Disabled] hint just
        # bloats context for what is already a self-correcting path.
        disabled_tools: List[str] = [
            t for t in extra_tool_names
            if not self.config_manager.is_tool_enabled(t)
        ]
        if disabled_tools:
            for t in disabled_tools:
                extra_tool_names.remove(t)
            self.logger.info(
                f"Step {step.step_id!r}: stripped disabled tools "
                f"{disabled_tools} from extra_tool_names",
                component="FlowController",
            )
        for provider in self._step_context_providers:
            provider_tool = getattr(provider, "tool_name", None)
            if provider_tool and provider_tool in extra_tool_names:
                try:
                    hint = await provider.prepare(step, self.interaction_manager, self.memory)
                    if hint:
                        effective_goal = f"{effective_goal}\n\n{hint}"
                except Exception as provider_exc:
                    self.logger.warning(
                        f"StepContextProvider {provider.__class__.__name__} "
                        f"failed for tool '{provider_tool}': {provider_exc}",
                        component="FlowController",
                    )
                    # Surface the error in the goal so the agent can report it.
                    effective_goal = (
                        f"{effective_goal}\n\n"
                        f"[Context Setup Warning]\n"
                        f"{provider.__class__.__name__} failed: {provider_exc}"
                    )

        if extra_tool_names:
            self.logger.info(
                f"Extra tools activated for step {step.step_id!r}: {extra_tool_names}",
                component="FlowController",
            )

        agent = RuntimeAgent(
            llm_services=self._agent_services,
            from_data_services=self._from_data_services,
            step=step,
            working_directory=self.working_directory,
            storage_directory=self.storage_directory,
            config_manager=self.config_manager,
            confirmation_callback=self._make_confirmation_callback(),
            check_interrupt_callback=self._make_interrupt_callback(interrupt_event),
            execution_recorder=self._execution_recorder,
            agent_id=agent_id or "",
            venv_path=self.venv_path,
            interaction_manager=self.interaction_manager,
            interrupt_event=interrupt_event if interrupt_event is not None else self._interrupt_event,
            extra_tool_names=extra_tool_names or None,
        )

        # Expose the agent to the planner loop so replan can read its progress.
        # Only set for sequential (non-parallel) steps; parallel sub-agents are
        # short-lived and their individual progress is less useful to the planner.
        if agent_id is None or agent_id == "":
            self._current_agent = agent
        try:
            agent_result: AgentResult = await agent.run_streaming(effective_goal)
        finally:
            # Always clear after run() returns so stale data is never read.
            if self._current_agent is agent:
                self._current_agent = None
        step.agent_runtime_reasoning = agent_result.reasoning or ""

        # Build a whitelist of files actually written/modified by write or edit
        # tool calls during this step.  The LLM sometimes fills Decision.artifacts
        # with paths it only READ (bash find/ls output, grep results), which inflates
        # artifact_count and incorrectly upgrades the verification tier.
        # Strategy (conservative):
        #   • Collect paths from successful write/edit ToolResults.
        #   • If write/edit calls exist: keep only artifacts in that whitelist.
        #   • If no write/edit calls AND no bash calls: clear artifacts entirely
        #     (pure read-only step — nothing was written).
        #   • If no write/edit calls BUT bash was called: keep artifacts as-is
        #     (bash may have written files we cannot detect by parsing commands).
        _written_paths: set = set()
        _had_bash = False
        for _obs in agent.step.get_all_observations():
            if _obs.tool_name in ("write", "edit") and _obs.success:
                _path = (_obs.tool_parameters or {}).get("path", "")
                if _path:
                    _written_paths.add(_path)
            elif _obs.tool_name in ("bash", "shell"):
                _had_bash = True

        def _filter_artifacts(raw: List[str]) -> List[str]:
            if not raw:
                return raw
            if _written_paths:
                # Keep only artifacts that were actually written/edited.
                filtered = [a for a in raw if a in _written_paths]
                if len(filtered) != len(raw):
                    self.logger.debug(
                        f"Artifact whitelist filter: {len(raw)} → {len(filtered)} "
                        f"(removed {len(raw) - len(filtered)} read-only path(s))",
                        component="FlowController",
                    )
                return filtered
            if not _had_bash:
                # No write/edit and no bash — definitely read-only.
                if raw:
                    self.logger.debug(
                        f"Artifact whitelist filter: cleared {len(raw)} artifact(s) "
                        f"(no write/edit/bash calls detected)",
                        component="FlowController",
                    )
                return []
            # Bash was called but no write/edit — keep as-is (bash may have written files).
            return raw

        if agent_result.success:
            step.factual_outcome = list(agent_result.factual_outcome)
            step.artifacts = _filter_artifacts(list(agent_result.artifacts))
            step.key_findings = list(agent_result.key_findings)

        if not agent_result.success:
            # Recovery heuristic: if the agent produced artifacts or key_findings
            # despite reporting failure (e.g. JSON parse error after a successful
            # tool call), treat the step as completed rather than failed.
            # A failed JSON parse does NOT mean the previous tool calls failed —
            # the tools may have succeeded while the LLM's summary failed to
            # serialize.  Restarting in this case causes side-effectful operations
            # (sed, write) to be re-executed unnecessarily.
            has_artifacts = bool(agent_result.artifacts or agent_result.key_findings)
            if has_artifacts:
                self.logger.warning(
                    f"Agent reported failure but has artifacts/key_findings — "
                    f"treating as completed to avoid re-executing side effects. "
                    f"error={agent_result.error!r}",
                    component="FlowController",
                )
                step.factual_outcome = list(agent_result.factual_outcome) or [agent_result.reasoning or ""]
                step.artifacts = _filter_artifacts(list(agent_result.artifacts))
                step.key_findings = list(agent_result.key_findings)
                step.update_status(StepStatus.COMPLETED)
            else:
                step.issues = [agent_result.error] if agent_result.error is not None else []
                step.update_status(StepStatus.FAILED)
        else:
            step.update_status(StepStatus.COMPLETED)

        # Stash agent-result data on the step object so the planner loop can
        # call record_step_result() AFTER observe_and_plan() returns and the
        # real planner confidence score is known.  Using private attributes
        # avoids adding fields to the Step dataclass for a metrics-only concern.
        step._metrics_iterations = agent_result.iterations          # type: ignore[attr-defined]
        step._metrics_tools_used = list(agent_result.tools_used)    # type: ignore[attr-defined]
        step._metrics_token_usage = agent_result.token_usage        # type: ignore[attr-defined]

        # Notify UI that this step has finished (no-op in CLI mode)
        self.interaction_manager.notify_step_completed(
            step.step_id,
            step.description,
            agent_result.success,
        )

        return step

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _transition_state(self, new_state: SystemState) -> None:
        old_state = self.state
        self.state = new_state
        self.logger.info(
            f"State transition: {old_state.value} -> {new_state.value}",
            component="FlowController"
        )
        # Notify UI (no-op in CLI mode)
        self.interaction_manager.notify_state_changed(new_state.value)

    def get_state(self) -> SystemState:
        return self.state

    async def _fire_task_complete_hook(self) -> None:
        """
        Await _on_task_complete_hook if set.

        Called at every task-completion point, BEFORE notify_task_completed(),
        so side-effects (e.g. GEP template post-processing) are guaranteed to
        finish before the user sees the completion message and can quit.

        The hook is NOT cleared after calling so it fires again on every
        subsequent completion within the same session (e.g. after a user
        replan).  The hook implementation is expected to be idempotent.
        Errors are logged and swallowed so task completion is never blocked.
        """
        hook = self._on_task_complete_hook
        if hook is None:
            return
        try:
            await hook()
        except Exception as _hook_exc:
            self.logger.warning(
                f"on_task_complete_hook failed: {_hook_exc}",
                component="FlowController",
            )

    async def _finalize_task(self) -> None:
        """End-of-task finaliser. Call this from every completion path.

        Pipeline (order matters):

          1. ``_fire_task_complete_hook`` — user-registered hook
             (e.g. GEP post-processing). Runs FIRST so user side-effects
             land before we tear down the framework's per-task resources
             they may depend on.
          2. ``_close_session_resources`` — internal subsystem flushes
             (browser pool, vision LLM client, desktop screenshot store,
             …). The dispatcher there knows nothing about specific
             subsystems beyond a registered list; new subsystems plug
             in by adding a row to the table inside it.

        Both steps are best-effort: each catches its own exceptions so
        task completion is never blocked. After this returns the user
        gets the "task complete" notification and can quit.

        Why a wrapper: prior to this method the two phases lived as
        sibling calls at every completion site (regular completion,
        post-interrupt completion, ...) — easy to drift in count or
        order as new sites are added. Centralising here removes the
        risk and gives Phase 3 activity_monitor a single hook point.
        """
        await self._fire_task_complete_hook()
        await self._close_session_resources()

    async def _close_session_resources(self) -> None:
        """Flush every per-task resource at task completion.

        This is a **dispatcher** — it does not know what each subsystem
        does. Each subsystem owns its cleanup contract and exposes a
        single ``async`` entry point. Phase 3 activity_monitor will add
        its own ``flush_activity_monitor`` and we just append a row.

        Contract for cleanup callbacks:
          * ``async`` callable with no required arguments.
          * Best-effort — must catch its own exceptions and never raise.
          * Returns something stringifiable for the log line, or None.

        The browser-pool / vision-client / desktop-store callbacks all
        already follow this shape. Cookies, login state, and other
        cross-task state survive whatever an individual cleanup chooses
        to discard — see each callback's docstring for specifics.

        Best-effort: any failure is logged and swallowed so completion is
        never blocked.
        """
        # Lazy imports keep the dispatcher decoupled from subsystem load
        # order — none of these modules need to be ready when
        # FlowController is constructed.
        from ..tools.browser_tool import flush_browser_pool
        from ..tools.desktop_tool import flush_desktop_store
        from ..infrastructure.vision import flush_vision_client
        from ..tools.session_tool import flush_session_pool

        # Order matters slightly: browser uses the vision client (for
        # vision_query); flush browser first so its in-flight calls
        # resolve, then close the LLM client, then the screenshot
        # stores (no dependents).
        cleanups = [
            ("browser pool",       flush_browser_pool),
            ("vision LLM client",  flush_vision_client),
            ("desktop screenshot store", flush_desktop_store),
            ("interactive sessions", flush_session_pool),
        ]
        for label, fn in cleanups:
            try:
                result = await fn()
                if result:
                    self.logger.info(
                        f"{label} flushed at task completion: {result}",
                        component="FlowController",
                    )
            except Exception as exc:
                self.logger.warning(
                    f"{label} cleanup at task completion failed: {exc}",
                    component="FlowController",
                )

        # Per-task LLM-prompt context cache for the browser provider.
        # Lives on FlowController state, not in any tool, so it stays
        # outside the cleanup table.
        try:
            self.memory.clear_browser_contexts()
        except Exception:
            pass
        try:
            # Same idea as clear_browser_contexts — drop the
            # progressive-disclosure cache so the next task's first
            # desktop-provider activation gets the full first-touch hint.
            self.memory.clear_desktop_contexts()
        except Exception:
            pass

        # Desktop takeover state: reset both flags so the next task
        # starts with a clean slate. If a takeover was still active we
        # also emit the 'task_ended' end event so the Electron overlay
        # hides. This is a one-line module-level call, not a registered
        # async cleanup, because there's nothing to await.
        try:
            from ..tools.desktop_tool import reset_takeover_state
            reset_takeover_state()
        except Exception:
            pass

    def get_metrics(self):
        """Return aggregated TaskMetrics across all tasks recorded this session."""
        return self._metrics_collector.get_metrics()

    def _report_metrics(self) -> None:
        """
        Display a Markdown-formatted metrics summary via the interaction
        manager (same output channel as task-completion messages) AND save
        the raw data to <workspace_dir>/metrics_summary.json.

        The Markdown table format blends naturally after the green ✓
        task-completion line rendered by notify_task_completed() in UI mode.
        In CLI mode interaction_manager.display_message() is a no-op, so a
        plain-text fallback is also printed to stdout for visibility.

        Exceptions from the file-save step are caught and logged as warnings
        so a save failure never interrupts normal task flow.
        """
        m = self._metrics_collector.get_metrics()

        # ── Build Markdown metrics block ──────────────────────────────────────
        # Only include rows for fields that carry meaningful information
        # (non-zero / non-trivial values) so the block stays concise.
        rows: list = []

        if m.total_duration_seconds > 0.0:
            rows.append(("Total duration", f"{m.total_duration_seconds:.1f}s"))

        if m.step_confidence_avg > 0.0:
            rows.append(("Avg confidence", f"{m.step_confidence_avg:.2f}"))

        if m.avg_iterations_per_step > 0.0:
            rows.append(("Avg iters/step", f"{m.avg_iterations_per_step:.1f}"))

        if m.replan_count > 0:
            rows.append(("Replans", str(m.replan_count)))

        if m.interrupt_count > 0:
            rows.append(("Interrupts", str(m.interrupt_count)))

        if m.total_steps > 0:
            rows.append(("Total steps", str(m.total_steps)))

        if m.total_tokens > 0:
            rows.append(("Total tokens", str(m.total_tokens)))
            rows.append(("  Input tokens", str(m.total_input_tokens)))
            rows.append(("  Output tokens", str(m.total_output_tokens)))
        if m.total_cache_creation_tokens > 0 or m.total_cache_read_tokens > 0:
            rows.append(("  Cache create tokens", str(m.total_cache_creation_tokens)))
            rows.append(("  Cache read tokens", str(m.total_cache_read_tokens)))

        avoidance_pct = m.failed_approach_reuse_rate * 100.0
        if m.failed_approach_reuse_rate > 0.0:
            rows.append(("Failed-approach avoidance", f"{avoidance_pct:.1f}%"))

        # Build the Markdown block and send to the UI conversation pane.
        if rows:
            md_lines = [
                "",
                "---",
                "📊 **Session Metrics** (Cumulative)",
                "",
                "| Metric | Value |",
                "|---|---|",
            ]
            for label, value in rows:
                md_lines.append(f"| {label} | {value} |")

            if m.goals and len(m.goals) > 1:
                md_lines.append("")
                md_lines.append("**Goals:**")
                for i, goal in enumerate(m.goals, 1):
                    truncated = goal[:100] + ("…" if len(goal) > 100 else "")
                    md_lines.append(f"{i}. {truncated}")

            meaningful_triggers = [msg for msg in m.replan_trigger_messages if msg.strip()]
            if meaningful_triggers:
                md_lines.append("")
                md_lines.append("**Replan triggers:**")
                for i, msg in enumerate(meaningful_triggers, 1):
                    truncated = msg[:100] + ("…" if len(msg) > 100 else "")
                    md_lines.append(f"{i}. {truncated}")

            md_lines.append("")
            markdown_block = "\n".join(md_lines)
            self.interaction_manager.notify_metrics_report(markdown_block)

        # Plain-text stdout fallback for CLI / non-UI mode
        avoidance_pct = m.failed_approach_reuse_rate * 100.0
        plain_lines = [
            "\n=== HandQ Session Metrics ===",
        ]
        if m.total_duration_seconds > 0.0:
            plain_lines.append(f"  Total duration:     {m.total_duration_seconds:.1f}s")
        if m.step_confidence_avg > 0.0:
            plain_lines.append(f"  Avg confidence:     {m.step_confidence_avg:.2f}")
        if m.avg_iterations_per_step > 0.0:
            plain_lines.append(f"  Avg iters/step:     {m.avg_iterations_per_step:.1f}")
        if m.replan_count > 0:
            plain_lines.append(f"  Replans:            {m.replan_count}")
        if m.interrupt_count > 0:
            plain_lines.append(f"  Interrupts:         {m.interrupt_count}")
        if m.failed_approach_reuse_rate > 0.0:
            plain_lines.append(f"  Avoidance rate:     {avoidance_pct:.1f}%")
        if m.total_tokens > 0:
            plain_lines.append(f"  Total tokens:       {m.total_tokens}")
            plain_lines.append(f"    Input tokens:     {m.total_input_tokens}")
            plain_lines.append(f"    Output tokens:    {m.total_output_tokens}")
        if m.total_cache_creation_tokens > 0 or m.total_cache_read_tokens > 0:
            plain_lines.append(f"    Cache create:     {m.total_cache_creation_tokens}")
            plain_lines.append(f"    Cache read:       {m.total_cache_read_tokens}")
        if m.goals and len(m.goals) > 1:
            plain_lines.append("  Goals:")
            for i, goal in enumerate(m.goals, 1):
                plain_lines.append(f"    [{i}] {goal[:80]}")

        # ── Save to file ──────────────────────────────────────────────────────
        try:
            metrics_path = os.path.join(self.storage_directory, "metrics_summary.json")
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "metrics": m.to_dict(),
            }
            os.makedirs(self.storage_directory, exist_ok=True)
            with open(metrics_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            self.logger.info(
                f"Metrics saved to {metrics_path}",
                component="FlowController",
            )
        except Exception as exc:
            self.logger.warning(
                f"Failed to save metrics to file: {exc}",
                component="FlowController",
            )
