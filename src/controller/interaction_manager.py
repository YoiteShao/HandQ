"""
Interaction Manager - Bridge between user and the rest of the system
"""
import asyncio
import queue
import sys
import threading
from typing import Any, Awaitable, Callable, ClassVar, Optional

from ..infrastructure.logger import get_logger
from ..models.state import UserConfirmation
from ..models.decision import Decision


class InteractionManager:
    """
    Bridge between user input and the rest of the system.

    Singleton: use InteractionManager.get_instance() to obtain the shared
    instance.  Direct instantiation via InteractionManager() is still
    supported for test harnesses that need an isolated instance, but in
    production code only one InteractionManager should exist per process
    because it owns the background stdin-reader thread and the three input
    queues.  Creating a second instance would start a second stdin thread,
    causing both instances to race for the same stdin lines.
    """

    # ── Singleton state ───────────────────────────────────────────────────────
    _instance: ClassVar[Optional["InteractionManager"]] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def get_instance(cls) -> "InteractionManager":
        """
        Return the process-wide singleton InteractionManager (thread-safe).

        Creates the instance on first call using double-checked locking.
        Subsequent calls return the same object without acquiring the lock
        (fast path: the check outside the lock is safe because Python
        reference assignment is atomic under the GIL).
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """
        Destroy the singleton instance.

        Intended for test teardown and :new session handling only.  After
        calling this, the next get_instance() call will create a fresh
        InteractionManager (and a new stdin-reader thread).  Do NOT call
        this in production code outside of the :new session flow.
        """
        with cls._instance_lock:
            cls._instance = None

    def __init__(self, config_path: Optional[str] = None):
        self.logger = get_logger()
        self.config_path = config_path

        # ── Optional UI delegate ──────────────────────────────────────────────
        # Set via set_ui().  When set, display_message / display_error and all
        # notify_* calls are routed to the UI object instead of printing to
        # stdout/stderr.  The UI object must implement the methods it wants to
        # handle; missing methods are silently skipped.
        self._ui: Optional[Any] = None

        # ── Input queues ──────────────────────────────────────────────────────
        self._user_message_queue: queue.Queue = queue.Queue()
        self._confirmation_queue: queue.Queue = queue.Queue()
        self._replan_queue: queue.Queue = queue.Queue()

        self._confirmation_active: bool = False

        # Lock protecting _confirmation_active.
        if not hasattr(self, "_confirmation_active_lock"):
            self._confirmation_active_lock = threading.Lock()

        # ── Pending confirmation state (UI path) ──────────────────────────────
        # When a UI is set, confirmation dialogs are shown as popups.
        # The question and callback are stored here so submit_confirmation_response()
        # can deliver the user's answer when they open the full UI to reply.
        self._pending_confirmation_question: Optional[str] = None
        self._pending_confirmation_callback: Optional[Callable[[str], None]] = None
        self._pending_confirmation_lock = threading.Lock()

        # ── Flags set by system commands ──────────────────────────────────────
        self._exit_requested: bool = False
        self._new_session_requested: bool = False

        # ── Optional status callback (set by FlowController) ─────────────────
        self._status_callback: Optional[Callable[[], str]] = None

        # ── Async message processor ───────────────────────────────────────────
        self._evaluate_callback: Optional[Callable[[str], Awaitable]] = None
        self._processor_running: bool = False
        self._message_processor_task: Optional[asyncio.Task] = None

        # Start background stdin reader (daemon — exits with the process).
        # In a fork()ed child process, thread startup may fail if the parent
        # had threads holding locks.  Catch and ignore — the background child
        # uses file-based IPC (MESSAGES_DIR) and does not need stdin input.
        self._stdin_thread = threading.Thread(
            target=self._stdin_reader, daemon=True, name="handq-stdin"
        )
        try:
            self._stdin_thread.start()
        except Exception:
            pass

        self.logger.info(
            "InteractionManager initialized successfully",
            component="InteractionManager",
        )

    # ── UI delegate ───────────────────────────────────────────────────────────

    def set_ui(self, ui: Any) -> None:
        """
        Set the UI delegate.

        All display_message / display_error calls and notify_* calls will be
        routed to the corresponding methods on *ui* when it is set.  Pass
        None to revert to the default print-to-stdout behaviour.
        """
        self._ui = ui

    def _ui_call(self, method: str, *args, **kwargs) -> None:
        """
        Call a method on the UI delegate if it exists, silently ignoring errors.

        This helper avoids repetitive hasattr / try-except boilerplate in every
        notify_* method.
        """
        if self._ui is None:
            return
        fn = getattr(self._ui, method, None)
        if fn is None:
            return
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            self.logger.debug(
                f"UI delegate {method} raised: {exc}",
                component="InteractionManager",
            )

    # ── Routing setup (called by FlowController after wiring) ────────────────

    def set_status_callback(self, callback: Callable[[], str]) -> None:
        """Register a callback that returns the current task status string."""
        self._status_callback = callback

    def set_evaluate_callback(
        self, callback: Optional[Callable[[str], Awaitable]]
    ) -> None:
        """
        Register (or clear) the async callback used by the message processor
        to evaluate user messages.

        Signature: async (msg: str) -> UserMessageEvaluation

        Set by FlowController before starting the processor for a new task;
        cleared (set to None) after the task ends.
        """
        self._evaluate_callback = callback

    # ── Async message processor (started/stopped per task by FlowController) ─

    async def start_message_processor(self) -> None:
        """
        Start the async message-processor loop as an asyncio.Task.

        Must be called from an async context (e.g. inside _plan_and_execute).
        Idempotent: calling while already running is a no-op.
        """
        if self._processor_running and self._message_processor_task is not None:
            return
        self._processor_running = True
        self._message_processor_task = asyncio.create_task(
            self._message_processor_loop(), name="handq-msg-processor"
        )
        self.logger.info(
            "Message processor started", component="InteractionManager"
        )

    async def stop_message_processor(self) -> None:
        """
        Stop the async message-processor loop and await its clean exit.

        Called by FlowController in the finally block of _plan_and_execute.
        """
        self._processor_running = False
        task = self._message_processor_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._message_processor_task = None
        self.logger.info(
            "Message processor stopped", component="InteractionManager"
        )

    async def _message_processor_loop(self) -> None:
        """
        Async loop: drain _user_message_queue, evaluate each message, route.

        Routing:
          • REPLAN intent  → put in _replan_queue for FlowController.
          • RESPOND_ONLY   → display response, done.
          • No callback    → generic acknowledgment + put in _replan_queue.
          • Evaluation error → fallback acknowledgment + put in _replan_queue.

        Sleeps 20 ms when the queue is empty to yield without busy-spinning.
        """
        while self._processor_running:
            try:
                msg = self._user_message_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue

            await self._process_one_message(msg)

    async def _process_one_message(self, msg: str) -> None:
        """Evaluate a single user message and route it."""
        callback = self._evaluate_callback
        if callback is None:
            self.logger.debug(
                f"Handq get user msg: {msg}", component="InteractionManager"
            )
            self._replan_queue.put(msg)
            return

        try:
            self._ui_call("show_receptionist_thinking")
            evaluation = await callback(msg)
            self._ui_call("clear_receptionist_thinking")
            self.display_receptionist_reply(evaluation.response_to_user)
            if evaluation.intent.value == "replan":
                # Use context_for_planner (which includes recent conversation
                # history) so the Planner has full context when the user
                # references prior RESPOND_ONLY answers.  Falls back to the
                # raw message when context_for_planner is empty.
                planner_msg = getattr(evaluation, "context_for_planner", "") or msg
                self._replan_queue.put(planner_msg)
                self.logger.info(
                    f"Message queued for replan: {msg[:60]}",
                    component="InteractionManager",
                )
            else:
                self.logger.info(
                    f"Message handled (respond_only): {msg[:60]}",
                    component="InteractionManager",
                )
        except Exception as exc:
            self.logger.error(
                f"Message evaluation failed: {exc}",
                component="InteractionManager",
            )
            self.display_message(
                "Message received — will be incorporated into the plan."
            )
            self._replan_queue.put(msg)

    # ── System command flags (polled by FlowController) ──────────────────────

    def is_exit_requested(self) -> bool:
        """Return True if the user has requested an exit."""
        return self._exit_requested

    def reset_exit_flag(self) -> None:
        """Clear the exit-requested flag."""
        self._exit_requested = False

    def consume_new_session_request(self) -> bool:
        """
        Return True (and reset the flag) if the user typed :new.

        Polled by handq_main.py after each session ends to determine whether
        to create a fresh FlowController for a new session or exit the process.
        """
        flag = self._new_session_requested
        self._new_session_requested = False
        return flag

    def clear_session_state(self, reset_new_session_flag: bool = True) -> None:
        """
        Clear per-session state between sessions.

        Drains all message queues and resets _confirmation_active and the
        pending confirmation state.

        Parameters
        ----------
        reset_new_session_flag : bool, default True
            When False, _new_session_requested is NOT reset.  Pass False when
            calling from _new_session() so handq_main.py can still read
            consume_new_session_request() == True after the session ends.
        """
        while True:
            try:
                self._user_message_queue.get_nowait()
            except queue.Empty:
                break

        while True:
            try:
                self._replan_queue.get_nowait()
            except queue.Empty:
                break

        while True:
            try:
                self._confirmation_queue.get_nowait()
            except queue.Empty:
                break

        if reset_new_session_flag:
            self._new_session_requested = False
        self._confirmation_active = False
        self._evaluate_callback = None

        with self._pending_confirmation_lock:
            self._pending_confirmation_question = None
            self._pending_confirmation_callback = None

    def is_confirmation_active(self) -> bool:
        """Return True while a confirmation dialog is active."""
        return self._confirmation_active

    # ── User-message queue (polled by FlowController) ────────────────────────

    def get_pending_user_message(self) -> Optional[str]:
        """
        Non-blocking: return the next pending REPLAN message, or None.

        Called by FlowController at the start of each planning cycle.
        """
        try:
            return self._replan_queue.get_nowait()
        except queue.Empty:
            return None

    # ── Programmatic injection (UI / test harness / API layer) ───────────────

    def inject_user_message(self, message: str) -> None:
        """
        Inject a user message programmatically.

        Follows the same routing as stdin: everything goes to
        _user_message_queue for processing by the message processor.
        """
        self._user_message_queue.put(message)
        self.logger.info(
            f"User message injected: {message[:60]}",
            component="InteractionManager",
        )

    def inject_task_goal(self, message: str) -> None:
        """
        Inject a task goal programmatically, bypassing Receptionist classification.

        Unlike inject_user_message(), this puts the message directly into
        _replan_queue so it is treated as a REPLAN (task) without going through
        classify_initial_goal().  Use this for programmatic task injection where
        the intent is known to be a task (e.g. the save-session goal).
        """
        self._replan_queue.put(message)
        self.logger.info(
            f"Task goal injected directly: {message[:60]}",
            component="InteractionManager",
        )

    # ── Background stdin reader ───────────────────────────────────────────────

    def _stdin_reader(self) -> None:
        """
        Daemon thread: reads stdin line-by-line and routes each non-empty line.

        Routing rules:
          • _confirmation_active=True → _confirmation_queue
          • otherwise                 → _user_message_queue
        """
        if sys.stdin.closed:
            self.logger.warning(
                "stdin is closed - stdin reader thread exiting",
                component="InteractionManager",
            )
            return

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                with self._confirmation_active_lock:
                    if self._confirmation_active:
                        # CLI path: route directly to the blocking confirmation reader.
                        self._confirmation_queue.put(line)
                    else:
                        self._user_message_queue.put(line)

            except UnicodeDecodeError:
                continue
            except OSError as exc:
                if exc.errno == 9:
                    self.logger.info(
                        "stdin is no longer available (bad file descriptor) - "
                        "stdin reader thread exiting",
                        component="InteractionManager",
                    )
                else:
                    self.logger.error(
                        f"stdin reader encountered an OS error: {exc}",
                        component="InteractionManager",
                    )
                break
            except Exception as exc:
                self.logger.error(
                    f"stdin reader encountered an error: {exc}",
                    component="InteractionManager",
                )
                break

    # ── Display helpers ───────────────────────────────────────────────────────

    def display_message(self, message: str) -> None:
        """
        Display a general message to the user.

        Delegates to self._ui.display_message() when a UI is set; falls back
        to print() in CLI / test contexts.
        """
        if self._ui is not None:
            self._ui_call("display_message", message)
        else:
            print(message, flush=True)

    def display_receptionist_reply(self, message: str) -> None:
        """
        Display a Receptionist reply to the user.

        Delegates to self._ui.display_receptionist_reply() when a UI is set
        (which uses a non-focus-stealing popup); falls back to display_message().
        """
        if self._ui is not None:
            fn = getattr(self._ui, "display_receptionist_reply", None)
            if fn is not None:
                try:
                    fn(message)
                    return
                except Exception:
                    pass
        self.display_message(message)

    def display_error(self, message: str) -> None:
        """
        Display an error message to the user.

        Delegates to self._ui.display_error() when a UI is set; falls back
        to printing to stderr in CLI / test contexts.
        """
        if self._ui is not None:
            self._ui_call("display_error", message)
        else:
            print(f"[ERROR] {message}", flush=True, file=sys.stderr)

    def display_progress(self, current: int, total: int) -> None:
        """
        Display a progress indicator to the user.

        Called by FlowController at the start of each planning cycle with
        the number of completed steps and the total number of planned steps.
        Delegates to self._ui.display_message() when a UI is set; falls back
        to print() in CLI / test contexts.
        """
        msg = f"\nProgress: {current}/{total} steps"
        if self._ui is not None:
            self._ui_call("display_progress_status", current, total)
        else:
            print(msg, flush=True)

    # ── Notification hooks ────────────────────────────────────────────────────

    def notify_state_changed(self, new_state: str) -> None:
        """
        Called when the FlowController or RuntimeAgent transitions to a new
        execution state (e.g. "thinking", "executing", "idle").

        Delegates to self._ui.show_state_changed(new_state) when a UI is set.
        """
        self._ui_call("show_state_changed", new_state)

    def notify_gep_countdown(self, remaining_secs: int, template_name: str) -> None:
        """
        Called each second during the GEP confirm window to update the
        status bar countdown.  Pass remaining_secs=-1 to clear.
        """
        self._ui_call("show_gep_countdown", remaining_secs, template_name)

    def notify_step_started(self, step_id: str, desc: str) -> None:
        """
        Called when a step begins executing.

        Delegates to self._ui.show_step_started(step_name, goal) when a UI
        is set.
        """
        self._ui_call("show_step_started", step_id, desc)

    def notify_step_completed(
        self,
        step_id: str,
        step_desc: str,
        success: bool = True,
    ) -> None:
        """
        Called when a step finishes.

        Delegates to self._ui.show_step_completed(step_name, result_summary)
        when a UI is set.
        """
        self._ui_call("show_step_completed", step_id, step_desc)

    def notify_step_confidence(self, confidence: float) -> None:
        """Called after planner evaluates a completed step's confidence."""
        self._ui_call("notify_step_confidence", confidence)

    def notify_reasoning(self, text: str, token_count: int = 0) -> None:
        """
        Called when the planner produces reasoning text.

        Delegates to self._ui.show_reasoning(text) when a UI is set.
        """
        self._ui_call("show_reasoning", text)

    def notify_task_completed(self, summary: str) -> None:
        """
        Called when the planner determines the task is complete.

        Delegates to self._ui.show_task_completed(summary) and
        self._ui.show_completion(summary) when a UI is set.
        """
        self._ui_call("show_task_completed", summary)

    def notify_decision_made(
        self, iteration: int, reasoning: str, token_count: int = 0
    ) -> None:
        """Called when the agent makes a decision (before tool execution)."""
        self._ui_call("notify_decision_made", iteration, reasoning, token_count)

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[Any],
        output: Optional[Any],
    ) -> None:
        """Called before and after each tool execution."""
        self._ui_call("notify_tool_execution_started", iteration, tool_name, params, output)

    # ── Confirmation flows ────────────────────────────────────────────────────

    def _read_confirmation_input(self) -> str:
        """
        Read one line of input for a confirmation dialog (CLI path only).

        INTENTIONALLY BLOCKING — do not convert to async.
        Blocks the calling thread until the user types a response.
        Must only be called when _confirmation_active is True and _ui is None.
        """
        return self._confirmation_queue.get()

    def _deliver_confirmation_response(self, response: str) -> None:
        """
        Deliver a response to the pending confirmation callback (UI path).

        Called from the stdin reader when _confirmation_active is True and
        a UI is set.  Also callable directly from the UI input handler via
        submit_confirmation_response().
        """
        with self._pending_confirmation_lock:
            callback = self._pending_confirmation_callback
            self._pending_confirmation_question = None
            self._pending_confirmation_callback = None

        if callback is not None:
            try:
                callback(response)
            except Exception as exc:
                self.logger.error(
                    f"Confirmation callback failed: {exc}",
                    component="InteractionManager",
                )

        with self._confirmation_active_lock:
            self._confirmation_active = False

    def submit_confirmation_response(self, response: str) -> None:
        """
        Public entry point for the UI to deliver a confirmation response.

        When the user types a response in the full UI input box and a
        confirmation is pending, the UI calls this method to route the
        response to the waiting callback.

        If no confirmation is pending the response is treated as a regular
        user message and routed to _user_message_queue.
        """
        with self._pending_confirmation_lock:
            has_pending = self._pending_confirmation_callback is not None

        if has_pending:
            self._deliver_confirmation_response(response)
        else:
            self._user_message_queue.put(response)

    def _resolve_confirmation(self, choice: str) -> UserConfirmation:
        """Map a raw user string to a UserConfirmation value."""
        if choice.lower() in ("y", "yes"):
            return UserConfirmation.yes()
        elif choice.lower() in ("n", "no"):
            return UserConfirmation.no()
        else:
            return UserConfirmation.with_message(choice)

    def request_confirmation(
        self, question: str, callback: Optional[Callable[[str], None]] = None
    ) -> UserConfirmation:
        """
        Generic confirmation request (CLI blocking path).

        Parameters
        ----------
        question : str
            The question to present to the user.
        callback : callable, optional
            Ignored on the CLI path (kept for API compatibility).
        """
        with self._confirmation_active_lock:
            self._confirmation_active = True
        try:
            print(f"\n[HandQ] {question}")
            print("  [yes] Approve  |  [no] Reject  |  [other] Provide guidance")
            print("Your choice: ", end="", flush=True)
            choice = self._read_confirmation_input()
            return self._resolve_confirmation(choice)
        finally:
            with self._confirmation_active_lock:
                self._confirmation_active = False

    def confirm_action(
        self, question: str, callback: Optional[Callable[[str], None]] = None
    ) -> UserConfirmation:
        """Alias for request_confirmation() — kept for API compatibility."""
        return self.request_confirmation(question, callback=callback)

    def request_secret_input(self, prompt: str) -> str:
        """
        Request a single secret string from the user (e.g. a password).

        Unlike request_confirmation(), this method:
          - Does NOT repeat the prompt for confirmation (one-shot input)
          - On the CLI path: uses getpass so the input is hidden (not echoed)
          - On the UI path: delegates to ui.request_secret_input() if available,
            otherwise falls back to displaying the prompt and reading from
            _confirmation_queue (UI layer is responsible for masking input)

        The method is intentionally BLOCKING — it must be called from a
        thread (e.g. via run_in_executor) to avoid blocking the event loop.

        Returns the raw string entered by the user (may be empty if the
        user pressed Enter without typing).
        """
        if self._ui is not None:
            # UI path: prefer a dedicated secret-input method on the UI delegate
            # (e.g. a modal password dialog with *** masking).  Fall back to
            # blocking on _confirmation_queue if the UI does not implement it.
            fn = getattr(self._ui, "request_secret_input", None)
            if fn is not None:
                self.logger.debug(
                    "request_secret_input: delegating to UI delegate",
                    component="InteractionManager",
                )
                try:
                    # NOTE: fn may be async (e.g. _BackgroundUI.request_secret_input).
                    # Callers that invoke this method via run_in_executor must check
                    # asyncio.iscoroutine(result) and await it on the event loop.
                    return fn(prompt)
                except Exception as exc:
                    self.logger.debug(
                        f"UI request_secret_input raised: {exc}",
                        component="InteractionManager",
                    )
            # Fallback: show prompt in message stream, block on confirmation queue.
            # Also print to stderr so the prompt is visible in headless/log contexts —
            # display_message() routes to the UI's normal message stream which may
            # not be visible to regression/background observers.
            self.logger.warning(
                f"request_secret_input: UI has no request_secret_input — "
                f"blocking on confirmation queue. Prompt: {prompt[:80]}",
                component="InteractionManager",
            )
            print(
                f"\n[HandQ] SECRET INPUT REQUIRED — respond in the UI:\n{prompt}",
                file=sys.stderr, flush=True,
            )
            self.display_message(prompt)
            with self._confirmation_active_lock:
                self._confirmation_active = True
            try:
                return self._confirmation_queue.get()
            finally:
                with self._confirmation_active_lock:
                    self._confirmation_active = False
        else:
            # CLI path: use getpass for hidden input (no echo).
            import getpass as _getpass
            print(f"\n{prompt}", flush=True)
            try:
                return _getpass.getpass("  Password: ")
            except (EOFError, KeyboardInterrupt):
                return ""

    def request_risk_confirmation(
        self, decision: Decision, risk_description: str
    ) -> UserConfirmation:
        """
        Request user confirmation for a high-risk operation.

        When a UI delegate is set (background child path), delegates to
        ui.request_risk_confirmation() which uses file-based IPC.
        Otherwise uses the CLI blocking path.

        Returns UserConfirmation based on user input:
          yes / y  → approved
          no  / n  → rejected
          anything else → UserConfirmation.with_message (guidance)
        """
        if self._ui is not None:
            fn = getattr(self._ui, "request_risk_confirmation", None)
            if fn is not None:
                with self._confirmation_active_lock:
                    self._confirmation_active = True
                try:
                    return fn(decision, risk_description)
                finally:
                    with self._confirmation_active_lock:
                        self._confirmation_active = False

        with self._confirmation_active_lock:
            self._confirmation_active = True
        try:
            print(f"\n{'='*60}")
            print("WARNING: HIGH-RISK OPERATION")
            print(f"{'='*60}")
            print(risk_description)
            print(f"{'='*60}")
            print("\nOptions:")
            print("  [yes] Approve and execute")
            print("  [no]  Reject this operation")
            print("  [any other input] Provide guidance")
            print("Your choice: ", end="", flush=True)

            choice = self._read_confirmation_input()

            if choice.lower() in ("y", "yes"):
                self.logger.info(
                    "User approved high-risk operation",
                    component="InteractionManager",
                )
                return UserConfirmation.yes()
            elif choice.lower() in ("n", "no"):
                self.logger.info(
                    "User rejected high-risk operation",
                    component="InteractionManager",
                )
                return UserConfirmation.no()
            else:
                self.logger.info(
                    f"User provided guidance for risk confirmation: {choice}",
                    component="InteractionManager",
                )
                return UserConfirmation.with_message(choice)
        finally:
            with self._confirmation_active_lock:
                self._confirmation_active = False

    def request_tool_confirmation(
        self, tool_name: str, decision: Decision
    ) -> UserConfirmation:
        """
        Request user confirmation for a tool execution.

        When a UI delegate is set (background child path), delegates to
        ui.request_tool_confirmation() which uses file-based IPC.
        Otherwise uses the CLI blocking path.

        Returns UserConfirmation based on user input:
          yes / y  → approved
          no  / n  → rejected
          anything else → new instruction (UserConfirmation.with_message)
        """
        if self._ui is not None:
            fn = getattr(self._ui, "request_tool_confirmation", None)
            if fn is not None:
                with self._confirmation_active_lock:
                    self._confirmation_active = True
                try:
                    return fn(tool_name, decision)
                finally:
                    with self._confirmation_active_lock:
                        self._confirmation_active = False

        with self._confirmation_active_lock:
            self._confirmation_active = True
        try:
            print(f"\n{'='*60}")
            print(f"TOOL EXECUTION CONFIRMATION: {tool_name}")
            print(f"{'='*60}")
            print(f"Tool:       {tool_name}")
            print(f"Parameters: {decision.parameters}")
            print(f"Reasoning:  {decision.reasoning}")
            print(f"{'='*60}")
            print("\nOptions:")
            print("  [yes] Execute")
            print("  [no]  Skip this operation")
            print("  [any other input] Provide new instruction")
            print("Your choice: ", end="", flush=True)

            choice = self._read_confirmation_input()

            if choice.lower() in ("y", "yes"):
                self.logger.info(
                    f"User approved {tool_name} execution",
                    component="InteractionManager",
                )
                return UserConfirmation.yes()
            elif choice.lower() in ("n", "no"):
                self.logger.info(
                    f"User rejected {tool_name} execution",
                    component="InteractionManager",
                )
                return UserConfirmation.no()
            else:
                self.logger.info(
                    f"User provided new instruction: {choice}",
                    component="InteractionManager",
                )
                return UserConfirmation.with_message(choice)
        finally:
            with self._confirmation_active_lock:
                self._confirmation_active = False