"""
status_tui.py — HandQ status display

Renders a rich-formatted snapshot of the current task execution state.
Reads state.json to find the active session, then parses the execution log
to display: latest plan goal, completed steps, current running step details,
and the latest agent iteration (think, tool, params, output).

Only meaningful in state2 (running) or state3 (completed); shows IDLE otherwise.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from rich.padding import Padding
from rich.table import Table
import rich.box


def run_status_tui(
    workspace_path: str = "",
    handq_dir: str = "",
    state: Optional[dict] = None,
) -> int:
    """
    Print a rich-formatted status snapshot of the current HandQ session.

    Args:
        workspace_path: Session workspace directory from state.json
                        (e.g. /path/to/.workspace/session_XXX).
                        Used to locate the execution log.
        handq_dir:      Path to the .handq/<user>@<host>/ directory.
                        Used only to check for confirmation_request.json.
                        Must be provided by the caller (handq.py knows HANDQ_DIR
                        correctly in both source and Nuitka compiled modes).
        state:          Already-parsed state dict from state.json.
                        Passed directly so this function never needs to
                        re-read state.json using __file__ (which breaks in
                        Nuitka compiled binaries).

    Returns 0 always (display-only, no error conditions).
    """
    from ..infrastructure.execution_recorder import ExecutionRecorder

    console = Console()

    if state is None:
        state = {}

    # ── Derive handq state from the provided state dict ───────────────────────
    hs = _get_handq_state(state, handq_dir)

    console.print()
    console.print(Rule("[bold cyan]HandQ Status[/bold cyan]", style="cyan"))
    console.print()

    if hs in (0, 1):
        console.print("  [dim]IDLE — no active task[/dim]")
        console.print()
        return 0

    session_id = state.get("session_id", "unknown")
    task_status = state.get("task_status", "")

    if hs == 2:
        status_label = "[bold yellow]RUNNING[/bold yellow]"
    elif hs == 3:
        status_label = "[bold green]COMPLETED[/bold green]"
    elif hs == 4:
        status_label = "[bold red]AWAITING CONFIRMATION[/bold red]"
    else:
        status_label = "[dim]UNKNOWN[/dim]"

    console.print(f"  {status_label}  Session: [dim]{session_id}[/dim]")
    console.print()

    # ── Find execution log ────────────────────────────────────────────────────
    log_path = _find_session_log(workspace_path)

    if not log_path:
        console.print("  [dim]No execution log found for this session.[/dim]")
        console.print()
        return 0

    # ── Parse log ─────────────────────────────────────────────────────────────
    data = ExecutionRecorder.parse_log(log_path)

    # ── Latest plan goal ──────────────────────────────────────────────────────
    plans = data.get("plans", [])
    latest_plan = plans[-1] if plans else None
    plan_goal = data.get("goal", "") or (latest_plan or {}).get("goal", "")
    is_replan = len(plans) > 1

    if plan_goal:
        goal_label = "[bold]Goal[/bold] (replan)" if is_replan else "[bold]Goal[/bold]"
        console.print(f"  {goal_label}: {plan_goal}")
        console.print()

    # ── Steps ─────────────────────────────────────────────────────────────────
    steps = data.get("steps", [])
    completed_steps = [s for s in steps if s["status"] in ("success", "failed")]
    running_steps = [s for s in steps if s["status"] == "running"]

    if completed_steps:
        table = Table(
            show_header=True,
            header_style="bold",
            box=rich.box.SIMPLE_HEAVY,
            padding=(0, 1),
        )
        table.add_column("#", style="dim", width=3, no_wrap=True)
        table.add_column("Description")
        table.add_column("Status", width=6, no_wrap=True)
        table.add_column("Conf", width=6, no_wrap=True)

        for i, s in enumerate(completed_steps, 1):
            icon = "✅" if s["status"] == "success" else "❌"
            desc = s.get("description") or s.get("goal") or s.get("step_id", "")
            conf = s.get("confidence")
            if conf is None:
                conf_cell = Text("  —  ", style="dim")
            elif conf >= 0.7:
                conf_cell = Text(f"{conf:.2f}", style="green")
            elif conf >= 0.5:
                conf_cell = Text(f"{conf:.2f}", style="yellow")
            else:
                conf_cell = Text(f"{conf:.2f}", style="red")
            table.add_row(str(i), desc, icon, conf_cell)

        console.print(Padding(table, (0, 2)))

        conf_values = [s["confidence"] for s in completed_steps if s.get("confidence") is not None]
        if conf_values:
            chars = "▁▂▃▄▅▆▇█"
            spark = "".join(chars[min(7, int(v * 8))] for v in conf_values)
            avg = sum(conf_values) / len(conf_values)
            console.print(f"  Confidence trend: [cyan]{spark}[/cyan]  avg: [bold]{avg:.2f}[/bold]")
        console.print()

    # ── Current running step ──────────────────────────────────────────────────
    if running_steps:
        cur = running_steps[-1]
        _render_running_step(console, cur)
    elif hs == 2:
        # State2 but no running step: the planner is between steps.
        # Distinguish initial planning (no steps yet) from inter-step replanning.
        next_steps = (latest_plan or {}).get("next_steps", [])
        label = "Deduce …" if completed_steps else "Planning…"
        console.print(f"  [bold yellow]{label}[/bold yellow]")
        console.print()
        for i, ns in enumerate(next_steps):
            sid = ns.get("step_id", "")
            desc = ns.get("description", "")
            prefix = "▶" if i == 0 else " "
            console.print(f"  {prefix} [dim]{sid}:[/dim] {desc}")
        if next_steps:
            console.print()
    elif hs == 3:
        # Task completed — show completion reason
        completion = state.get("completion_reason", "") or data.get("completion", "")
        if completion:
            console.print("  [bold green]Task Complete[/bold green]")
            console.print(f"  {completion}")
            console.print()

    return 0


def _render_running_step(console: Console, step: dict) -> None:
    """Render the currently running step with full details."""
    console.print("  [bold]Current Step[/bold]: [yellow]RUNNING[/yellow]")

    goal = step.get("goal", "")
    if goal:
        console.print(f"    [dim]Goal:[/dim]      {goal}")

    reasoning = step.get("reasoning", "")
    if reasoning:
        # Show first 200 chars of reasoning
        short = reasoning[:200].replace("\n", " ").strip()
        if len(reasoning) > 200:
            short += "…"
        console.print(f"    [dim]Reasoning:[/dim] {short}")

    started_at = step.get("started_at", "")
    if started_at:
        console.print(f"    [dim]Started:[/dim]   {started_at}")

    iters = step.get("iterations", 0)
    console.print(f"    [dim]Iterations:[/dim] {iters}")

    console.print()

    # ── Latest iteration ──────────────────────────────────────────────────────
    li = step.get("latest_iteration")
    if li:
        console.print("  [bold]Latest Action[/bold]:")

        tool = li.get("tool", "")
        if tool:
            console.print(f"    [dim]Tool:[/dim]   [cyan]{tool}[/cyan]")

        params = li.get("params", "")
        if params:
            # params is a multi-line string "  key: value\n  key: value"
            param_lines = [ln.strip() for ln in params.splitlines() if ln.strip()]
            if param_lines:
                console.print("    [dim]Params:[/dim]")
                for pl in param_lines[:5]:  # show at most 5 param lines
                    console.print(f"      {pl}")

        think = li.get("think", "")
        if think:
            short = think[:300].replace("\n", " ").strip()
            if len(think) > 300:
                short += "…"
            console.print(f"    [dim]Think:[/dim]  {short}")

        output = li.get("output", "")
        if output:
            short = output[:400].replace("\n", " ").strip()
            if len(output) > 400:
                short += "…"
            status_color = "red" if li.get("status") == "err" else "default"
            console.print(f"    [dim]Output:[/dim] [{status_color}]{short}[/{status_color}]")

        console.print()


def _find_session_log(workspace_path: str) -> Optional[str]:
    """
    Find the most recent execution log for the current session.

    Uses the session-specific workspace_path from state.json.
    """
    if workspace_path:
        exec_logs_dir = Path(workspace_path) / "executions_logs"
        if exec_logs_dir.exists():
            candidates = sorted(
                exec_logs_dir.glob("plan_*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                return str(candidates[0])
    return None


def _get_handq_state(state: dict, handq_dir: str = "") -> int:
    """Derive handq state integer from state dict."""
    if not state or not state.get("handq_active", False):
        return 0
    task_status = state.get("task_status", "")
    if task_status == "running":
        if handq_dir and (Path(handq_dir) / "confirmation_request.json").exists():
            return 4
        return 2
    if task_status == "completed":
        return 3
    return 1
