"""
handq_win.py — Foreground Windows runner for HandQ.

Runs entirely in the foreground (no background process, no tmux, no state
files).  Input/output go directly to the console.  Suitable as a temporary
CLI on Windows until a proper GUI is available.

Real-time input/feedback on Windows is provided by an explicit asyncio +
prompt_toolkit pipeline:

  • prompt_toolkit.PromptSession.prompt_async() drives line input on the
    asyncio loop (uses console-handle waits on Windows; no busy thread).
  • prompt_toolkit.patch_stdout.patch_stdout(raw=True) wraps the whole
    runtime so every print() / logger write from the planner is rendered
    above the prompt without destroying the user's in-progress line.
  • InteractionManager's blocking sys.stdin.readline() daemon thread is
    short-circuited from the runner side (sys.stdin is replaced with a
    closed sentinel before the singleton is constructed) so it exits via
    the `sys.stdin.closed` guard at the top of _stdin_reader().  All
    user lines and confirmation responses are then injected explicitly
    through im.inject_user_message() / im.submit_confirmation_response().

A pure-stdlib msvcrt fallback is provided behind --no-prompt-toolkit for
environments where prompt_toolkit cannot be installed.

Usage:
    python handq_win.py --config myconfig.yaml
    handq_win.exe    --config myconfig.yaml
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import argparse
import logging as _logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Bootstrap: make src/ importable when run from the repo root or from a
# handq.dist/ directory (Nuitka standalone layout).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ---------------------------------------------------------------------------
# Windows event-loop policy + ANSI enabling.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    # Proactor loop is the Python 3.8+ default on Windows but we assert it
    # explicitly so subprocesses launched by FlowController keep working
    # even on alternate Python builds.
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass
    # Flip the console into ANSI-processing mode (Windows 10+).  os.system("")
    # is the cheapest way to enable ENABLE_VIRTUAL_TERMINAL_PROCESSING without
    # adding a colorama dependency.  Failure is non-fatal — we just lose
    # colour and \r-redraw fidelity.
    try:
        os.system("")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Console UI — output flows through prompt_toolkit's patch_stdout, so plain
# print() / logger writes are safe; the prompt is redrawn automatically.
# ---------------------------------------------------------------------------

class _ConsoleUI:
    """Minimal UI adapter that prints to the console.

    Re-prompting after planner output is the input loop's job (via
    patch_stdout), so display_* methods only emit content — they never
    write a fresh "  Message: " prompt themselves.
    """

    def __init__(self) -> None:
        # Updated by _run() once the prompt_toolkit session exists, so the
        # countdown can refresh the bottom toolbar instead of using bare \r.
        self._pt_session = None  # type: ignore[assignment]
        self._countdown_text: str = ""

    # ── output ────────────────────────────────────────────────────────────
    def display_message(self, message: str) -> None:
        print(f"\n{message}", flush=True)

    def display_receptionist_reply(self, message: str) -> None:
        print(f"\n💬  HandQ: {message}", flush=True)

    def display_error(self, message: str) -> None:
        print(f"\n[ERROR] {message}", file=sys.stderr, flush=True)

    def show_task_completed(self, summary: str) -> None:
        print(f"\n✅  Task complete: {summary}", flush=True)

    def show_state_changed(self, state: str) -> None:
        pass

    def show_step_started(self, step_name: str, goal: str) -> None:
        print(f"\n  ▶  {step_name}", flush=True)

    def show_step_completed(self, step_name: str, result_summary: str) -> None:
        print(f"  ✓  {step_name}", flush=True)

    def show_reasoning(self, text: str) -> None:
        pass

    def show_receptionist_thinking(self) -> None:
        print("\n  💬  Receptionist thinking...", flush=True)

    def clear_receptionist_thinking(self) -> None:
        pass  # reply follows immediately

    def notify_decision_made(self, iteration: int, reasoning: str,
                             token_count: int = 0) -> None:
        if reasoning:
            snippet = reasoning[:120].replace("\n", " ")
            print(f"\n  💬 [{iteration}] {snippet}", flush=True)

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[dict],
        output: Optional[dict],
    ) -> None:
        if not tool_name or not params:
            return  # only show before-execution events
        if tool_name in ("bash", "execute_command", "run_command"):
            cmd = str(params.get("command", ""))[:80]
            print(f"  ⊙ [{iteration}] $ {cmd}", flush=True)
        elif tool_name in ("read", "write", "edit"):
            path = str(params.get("path", params.get("file_path", "")))
            fname = path.replace("\\", "/").rsplit("/", 1)[-1][:40]
            print(f"  ⊙ [{iteration}] {tool_name}: {fname}", flush=True)
        else:
            print(f"  ⊙ [{iteration}] {tool_name}", flush=True)

    def display_progress_status(self, current: int, total: int) -> None:
        print(f"\n  ≡  {current}/{total} steps", flush=True)

    def notify_step_confidence(self, confidence: float) -> None:
        pass

    def show_gep_countdown(self, remaining_secs: int, template_name: str) -> None:
        # Route countdown updates through the prompt_toolkit bottom toolbar
        # when available; otherwise fall back to a single-line print so we
        # don't clobber an in-progress prompt with bare \r.
        if remaining_secs >= 0:
            self._countdown_text = f"⏳  {template_name or 'GEP'} in {remaining_secs}s"
        else:
            self._countdown_text = ""
        if self._pt_session is not None:
            try:
                # Force the prompt to redraw so the toolbar reflects the
                # new countdown text.
                app = self._pt_session.app
                if app is not None and app.is_running:
                    app.invalidate()
                return
            except Exception:
                pass
        # Fallback: plain print (only used when prompt_toolkit is disabled).
        if remaining_secs >= 0:
            print(f"  ⏳  {template_name or 'GEP'} in {remaining_secs}s", flush=True)
        else:
            print(flush=True)

    # ── confirmations ─────────────────────────────────────────────────────
    def request_confirmation(self, question: str, callback=None) -> None:
        """Display a confirmation prompt header.

        The actual yes/no answer is collected by the input loop (which
        checks im.is_confirmation_active() each iteration) and forwarded
        via im.submit_confirmation_response().  This method only prints
        the question so the user sees what they're confirming.
        """
        print(f"\n[HandQ] {question}", flush=True)
        print("  [yes] Approve  |  [no] Reject  |  [other] Provide guidance",
              flush=True)


# ---------------------------------------------------------------------------
# Role assignment (mirrors handq.py logic)
# ---------------------------------------------------------------------------

_PLANNER_MIN_VERSION = (4, 5)


def _model_version(model_str: str):
    """Extract (major, minor) from 'anthropic::claude-X-Y-...'."""
    import re
    m = re.search(r"claude-(\d+)[.\-](\d+)", model_str)
    if m:
        return int(m.group(1)), int(m.group(2))
    return (0, 0)


def _assign_roles(all_models: list) -> dict:
    """Assign model lists to agent/planner/receptionist/from_data roles."""
    capable = [m for m in all_models if _model_version(m) >= _PLANNER_MIN_VERSION]
    if not capable:
        import warnings
        warnings.warn(
            "No planner-capable models (Claude 4-5+) found. "
            "All roles will use the full model list.",
            UserWarning,
        )
        return dict(agent=all_models, planner=all_models,
                    receptionist=all_models, from_data=all_models)

    n = len(capable)
    opus_n = sum(1 for m in capable if "opus" in m)

    if opus_n:
        recep_skip = min(opus_n,     n - 1)
        fdata_skip = min(opus_n + 2, n - 1)
    else:
        recep_skip = min(1, n - 1)
        fdata_skip = min(2, n - 1)

    return dict(
        agent=all_models,
        planner=capable,
        receptionist=capable[recep_skip:],
        from_data=capable[fdata_skip:],
    )


# ---------------------------------------------------------------------------
# Session builder (mirrors handq.py _build_session, Windows foreground only)
# ---------------------------------------------------------------------------

def _build_session(config_path: str, session_id: str, cwd: str):
    import yaml
    from src.infrastructure.logger import initialize_logger, LogLevel
    from src.controller.flow_controller import FlowController
    from src.infrastructure.anthropic_streaming_service import AnthropicStreamingService

    cfg_path = Path(config_path)
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        raise RuntimeError(f"Cannot load config {config_path}: {exc}") from exc

    llm_cfg        = config.get("llm", {})
    api_key_val    = llm_cfg.get("API_KEY") or ""
    max_tokens     = llm_cfg.get("max_tokens", None)
    all_models     = llm_cfg.get("models", [])
    log_level_str  = config.get("session", {}).get("log_level", "INFO")
    threshold      = float(config.get("session", {}).get(
        "step_verification_threshold",
        FlowController.DEFAULT_STEP_VERIFICATION_THRESHOLD,
    ))
    workspace_base = config.get("session", {}).get("workspace_base", ".workspace")
    venv_path      = config.get("session", {}).get("venv_path")

    session_dir = Path(cwd) / workspace_base / session_id
    (session_dir / "logs").mkdir(parents=True, exist_ok=True)
    (session_dir / "executions_logs").mkdir(parents=True, exist_ok=True)

    initialize_logger(
        name="HandQ",
        level=LogLevel[log_level_str.upper()],
        log_file=f"handq_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        log_dir=str(session_dir / "logs"),
    )

    def _make_service(model: str, max_retries: int) -> "AnthropicStreamingService":
        return AnthropicStreamingService(
            model=model,
            api_key=api_key_val,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )

    all_llm_services = [_make_service(m, max_retries=3) for m in all_models]
    roles = _assign_roles(all_models)
    svc_map = {m: svc for m, svc in zip(all_models, all_llm_services)}

    agent_services        = all_llm_services
    receptionist_services = [svc_map[m] for m in roles["receptionist"]]
    from_data_services    = [svc_map[m] for m in roles["from_data"]]
    planner_services      = [_make_service(m, max_retries=50) for m in roles["planner"]]

    flow = FlowController(
        agent_llm_services=agent_services,
        planner_llm_services=planner_services,
        receptionist_llm_services=receptionist_services,
        from_data_llm_services=from_data_services,
        working_directory=cwd,
        storage_directory=str(session_dir),
        step_verification_threshold=threshold,
        venv_path=venv_path,
        config_path=config_path,
    )

    return flow, all_llm_services, str(session_dir)


# ---------------------------------------------------------------------------
# Stdin reader neutralization
# ---------------------------------------------------------------------------

class _ClosedStdin(io.StringIO):
    """A pre-closed StringIO that quacks like sys.stdin.

    InteractionManager._stdin_reader() guards on `sys.stdin.closed` and
    returns immediately when True.  We swap sys.stdin out for one of these
    *before* the singleton is constructed, so the daemon reader thread
    exits on its first iteration and we own the input pipeline.
    """

    def __init__(self) -> None:
        super().__init__()
        self.close()

    @property
    def closed(self) -> bool:  # type: ignore[override]
        return True


def _neutralize_interaction_manager_stdin() -> None:
    """Replace sys.stdin with a closed sentinel for the duration of the run.

    We keep the real stdin file descriptor available via sys.__stdin__
    (Python preserves it) so prompt_toolkit / msvcrt can still read the
    console handle directly — they don't go through sys.stdin.
    """
    sys.stdin = _ClosedStdin()


# ---------------------------------------------------------------------------
# Logger silencing — make sure planner log lines do not collide with the
# input prompt.  We keep file logging untouched; only console handlers are
# downgraded.
# ---------------------------------------------------------------------------

def _quiet_console_loggers() -> None:
    for name in ("HandQ", "root", ""):
        logger = _logging.getLogger(name) if name else _logging.getLogger()
        for h in list(logger.handlers):
            stream = getattr(h, "stream", None)
            if stream in (sys.stdout, sys.stderr):
                # Only show ERRORs on the console; everything else stays in
                # the log file.
                h.setLevel(_logging.ERROR)


# ---------------------------------------------------------------------------
# Async input loop (prompt_toolkit path)
# ---------------------------------------------------------------------------

async def _input_loop_pt(session, im, ui: _ConsoleUI) -> None:
    """Prompt the user repeatedly and route each line into InteractionManager.

    Uses prompt_toolkit.PromptSession.prompt_async, so the call yields to
    the asyncio loop while waiting for a keystroke; planner tasks keep
    running concurrently.  A bottom toolbar reflects the current GEP
    countdown if one is active.
    """
    while True:
        try:
            line = await session.prompt_async(
                "  Message: ",
                bottom_toolbar=lambda: ui._countdown_text or "",
            )
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D / Ctrl-C at the prompt: stop the input loop, let the
            # outer _run() decide whether to shut the flow down too.
            return

        if line is None:
            return
        line = line.strip()
        if not line:
            continue

        # Route confirmation responses separately so the planner's blocking
        # confirmation path receives them via the documented public API.
        if im.is_confirmation_active():
            try:
                im.submit_confirmation_response(line)
            except Exception:
                # As a safety net, fall through to inject_user_message so
                # the line is not silently dropped.
                im.inject_user_message(line)
        else:
            im.inject_user_message(line)


# ---------------------------------------------------------------------------
# Async input loop (msvcrt fallback — used when --no-prompt-toolkit is set
# or prompt_toolkit cannot be imported).
# ---------------------------------------------------------------------------

async def _input_loop_msvcrt(im, ui: _ConsoleUI) -> None:
    """Pure-stdlib Windows fallback using msvcrt.

    A tiny background thread polls msvcrt.kbhit()/getwch() and posts
    completed lines into an asyncio.Queue via call_soon_threadsafe.
    Lacks history / paste mode but never blocks the asyncio loop and
    cooperates with Ctrl-C cleanly.
    """
    import threading

    loop = asyncio.get_running_loop()
    q: "asyncio.Queue[str]" = asyncio.Queue()
    stop_event = threading.Event()

    def reader() -> None:
        try:
            import msvcrt  # type: ignore[import-not-found]
        except ImportError:
            # Non-Windows fallback: use blocking sys.__stdin__ readline in a
            # thread.  Cooked-mode line buffering on the terminal still gives
            # us per-Enter delivery.
            real_stdin = sys.__stdin__
            while not stop_event.is_set():
                try:
                    line = real_stdin.readline()
                except Exception:
                    break
                if not line:
                    break
                loop.call_soon_threadsafe(q.put_nowait, line.rstrip("\r\n"))
            return

        buf: List[str] = []
        while not stop_event.is_set():
            if not msvcrt.kbhit():
                stop_event.wait(0.05)
                continue
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                msvcrt.putwch("\n")
                line = "".join(buf)
                buf.clear()
                loop.call_soon_threadsafe(q.put_nowait, line)
            elif ch == "\x03":  # Ctrl-C
                loop.call_soon_threadsafe(q.put_nowait, "\x03")
                break
            elif ch == "\b":  # backspace
                if buf:
                    buf.pop()
                    msvcrt.putwch("\b")
                    msvcrt.putwch(" ")
                    msvcrt.putwch("\b")
            else:
                buf.append(ch)
                msvcrt.putwch(ch)

    t = threading.Thread(target=reader, name="handq-win-input", daemon=True)
    t.start()

    sys.stdout.write("\n  Message: ")
    sys.stdout.flush()
    try:
        while True:
            line = await q.get()
            if line == "\x03":
                raise KeyboardInterrupt
            line = line.strip()
            if line:
                if im.is_confirmation_active():
                    try:
                        im.submit_confirmation_response(line)
                    except Exception:
                        im.inject_user_message(line)
                else:
                    im.inject_user_message(line)
            sys.stdout.write("  Message: ")
            sys.stdout.flush()
    finally:
        stop_event.set()


# ---------------------------------------------------------------------------
# Async main
# ---------------------------------------------------------------------------

async def _run(config_path: str, cwd: str, use_prompt_toolkit: bool = True) -> None:
    session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S")

    print("HandQ  ─  loading session...", flush=True)

    # ── Set up prompt_toolkit BEFORE neutralizing stdin ──────────────────
    # prompt_toolkit detects whether sys.stdin is a TTY at construction
    # time.  If we replace sys.stdin with _ClosedStdin first, it sees a
    # non-TTY / closed stream and prompt_async() raises EOFError on the
    # very first call, immediately completing the input task and triggering
    # an unwanted shutdown.  Building the session here (while sys.stdin is
    # still the real console handle) avoids that race.
    pt_session = None
    patch_ctx = None
    if use_prompt_toolkit:
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.patch_stdout import patch_stdout

            history_path = Path(cwd) / ".handq_history"
            try:
                pt_session = PromptSession(history=FileHistory(str(history_path)))
            except Exception:
                pt_session = PromptSession()
            patch_ctx = patch_stdout(raw=True)
            patch_ctx.__enter__()
        except Exception as exc:
            print(f"[WARN] prompt_toolkit unavailable ({exc}); "
                  "falling back to msvcrt input.", file=sys.stderr, flush=True)
            pt_session = None
            patch_ctx = None

    # Neutralize InteractionManager's blocking stdin reader BEFORE the
    # singleton is constructed.  After this, the daemon thread sees
    # sys.stdin.closed and exits cleanly via the guard at the top of
    # _stdin_reader().  All input is then driven by us via inject_*.
    _neutralize_interaction_manager_stdin()

    from src.controller.interaction_manager import InteractionManager
    im = InteractionManager.get_instance()

    ui = _ConsoleUI()
    im.set_ui(ui)
    if pt_session is not None:
        ui._pt_session = pt_session

    try:
        flow, services, session_dir = _build_session(config_path, session_id, cwd)
    except Exception as exc:
        print(f"[ERROR] Failed to build session: {exc}", file=sys.stderr)
        return

    _quiet_console_loggers()

    print(f"\nHandQ  |  session: {session_id}")
    print(f"        storage: {session_dir}")
    print("Type your message and press Enter.  Ctrl-C to exit.")

    # ── Launch flow + input loops concurrently ──────────────────────────
    flow_task = asyncio.create_task(flow.start_idle_session(), name="handq-flow")
    if pt_session is not None:
        input_task = asyncio.create_task(
            _input_loop_pt(pt_session, im, ui), name="handq-input"
        )
    else:
        input_task = asyncio.create_task(
            _input_loop_msvcrt(im, ui), name="handq-input"
        )

    try:
        done, pending = await asyncio.wait(
            {flow_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Surface any non-CancelledError exceptions from the completed task
        # (without re-raising — we still want to run shutdown cleanly).
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc and not isinstance(exc, (KeyboardInterrupt,
                                            asyncio.CancelledError)):
                print(f"[ERROR] {t.get_name()} crashed: {exc}",
                      file=sys.stderr, flush=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        print("\nShutting down...", flush=True)
        for t in (flow_task, input_task):
            if not t.done():
                t.cancel()
        # Wait briefly for cancellation to land.
        try:
            await asyncio.wait_for(
                asyncio.gather(flow_task, input_task, return_exceptions=True),
                timeout=5.0,
            )
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

        # Tear down prompt_toolkit's stdout patch if we installed one.
        if patch_ctx is not None:
            try:
                patch_ctx.__exit__(None, None, None)
            except Exception:
                pass
        if pt_session is not None:
            try:
                app = pt_session.app
                if app is not None and app.is_running:
                    app.exit()
            except Exception:
                pass

        for svc in services:
            try:
                await svc.close()
            except Exception:
                pass
        print("HandQ exited.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HandQ — foreground Windows runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to handq_config.yaml",
    )
    parser.add_argument(
        "--no-prompt-toolkit", action="store_true",
        help="Disable prompt_toolkit and use the msvcrt/stdin fallback "
             "(no history, no paste mode).",
    )
    args = parser.parse_args()

    config_path = str(Path(args.config).resolve())
    if not Path(config_path).exists():
        print(f"[ERROR] Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    cwd = str(Path.cwd())

    try:
        asyncio.run(_run(
            config_path, cwd,
            use_prompt_toolkit=not args.no_prompt_toolkit,
        ))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
