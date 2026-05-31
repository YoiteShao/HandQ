# -*- coding: utf-8 -*-
"""
Remote HandQ Context Provider — setup credentials and discover HANDQ_DIR
before the agent executes a step that delegates to a remote Linux HandQ.

Activation: Planner declares "remote_handq" in step.tools_required and sets
step.ssh_target to "user@hostname".

Delegates credential establishment to the existing SSHSetupManager so the
same key/keyring/password flow is reused.  Adds HANDQ_DIR discovery on
top — a single SSH exec that locates the remote .handq/ directory.
"""
from __future__ import annotations

import getpass
import re
from typing import TYPE_CHECKING, Optional

from .step_context_provider import StepContextProvider
from .logger import get_logger

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


_USER_AT_RE = re.compile(r"(\w[\w.-]*)@([\w.-]+)")


class RemoteHandQContextProvider(StepContextProvider):
    """
    StepContextProvider for remote HandQ task delegation.

    Activation: Planner declares "remote_handq" in step.tools_required.
    FlowController invokes prepare() based purely on the declaration.

    Responsibility:
      1. Establish SSH credentials for the target host (via SSHSetupManager)
      2. Discover HANDQ_DIR on the remote host
      3. Inject a context hint so the agent knows credentials_file and handq_dir
    """

    def __init__(self) -> None:
        from .ssh_setup import SSHSetupManager
        self._manager = SSHSetupManager()
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "remote_handq"

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        """
        Establish SSH credentials and discover remote HANDQ_DIR.

        Progressive disclosure:
          - First time a hostname is seen: full hint (workflow + creds + handq_dir)
          - Subsequent steps for same host: brief hint (creds + handq_dir only)
        """
        self.logger.info(
            f"RemoteHandQContextProvider.prepare() for step {step.step_id!r}",
            component="RemoteHandQProvider",
        )

        hostname, username = self._extract_host_user(step)
        if not hostname:
            self.logger.warning(
                f"RemoteHandQContextProvider: could not extract hostname from "
                f"step {step.step_id!r} — skipping context injection.",
                component="RemoteHandQProvider",
            )
            return None

        # Per-hostname cache check
        cached = memory.get_remote_handq_context(hostname)
        if cached:
            self.logger.debug(
                f"Using cached remote_handq context for {hostname}",
                component="RemoteHandQProvider",
            )
            return _build_brief_hint(cached["creds_file"], cached["handq_dir"])

        # Establish SSH credentials via the shared SSHSetupManager
        self.logger.info(
            f"Establishing SSH credentials for {username or '?'}@{hostname}",
            component="RemoteHandQProvider",
        )
        try:
            from .ssh_setup import SSHSetupError
            result = await self._manager.ensure_ssh_ready(
                hostname=hostname,
                username=username or "",
                interaction_manager=interaction_manager,
            )
        except Exception as exc:
            self.logger.error(
                f"SSH setup failed for {username or '?'}@{hostname}: {exc}",
                component="RemoteHandQProvider",
            )
            return (
                f"[Remote HandQ Setup Error]\n"
                f"Failed to establish SSH credentials: {exc}\n"
                f"Verify hostname, username, and connectivity, then retry."
            )

        creds_file = result.creds_file

        # Discover HANDQ_DIR on the remote host
        handq_dir = ""
        try:
            from ..tools.remote_handq_tool import _load_credentials, _discover_handq_dir
            creds = _load_credentials(creds_file)
            handq_dir, _, _, _ = _discover_handq_dir(creds)
        except Exception as disc_exc:
            self.logger.warning(
                f"HANDQ_DIR discovery failed for {hostname}: {disc_exc}. "
                f"Agent will need to call discover action manually.",
                component="RemoteHandQProvider",
            )

        # Build and cache the full hint
        hint = _build_full_hint(creds_file, handq_dir)
        memory.set_remote_handq_context(hostname, creds_file, handq_dir, hint)

        return hint

    def _extract_host_user(self, step: "Step") -> tuple:
        """Extract (hostname, username) from step — same logic as SSHContextProvider."""
        ssh_target = getattr(step, "ssh_target", "") or ""
        if ssh_target.strip():
            m = _USER_AT_RE.match(ssh_target.strip())
            if m:
                return m.group(2), m.group(1)
            return ssh_target.strip(), getpass.getuser()

        text = f"{getattr(step, 'goal', '')} {step.description}"
        m = _USER_AT_RE.search(text)
        if m:
            return m.group(2), m.group(1)
        return "", ""

    def planner_description(self) -> str:
        return (
            "`remote_handq` | Delegate a complex task to a remote Linux HandQ "
            "agent that plans and executes autonomously — use when remote "
            "work requires reasoning/multi-step planning, not just running "
            "known commands | Set `ssh_target`; intelligence runs on remote side"
        )

    def planner_routing_rule(self) -> str:
        return (
            "Remote task needs planning/reasoning (not a known command)  "
            "→ `tools_required: [\"remote_handq\"]` + set `ssh_target`"
        )

    def planner_antipatterns(self) -> list:
        return [
            '`["remote_handq"]` when you already know the exact commands → use `["ssh"]` tool',
            '`["remote_handq"]` for single command or known script → use `["ssh"]` exec/run_script',
            '`["ssh", "remote_handq"]` together in one step → pick one: delegate OR drive',
        ]


def _build_full_hint(creds_file: str, handq_dir: str) -> str:
    """Full context hint for first activation of a remote HandQ host."""
    dir_line = f" | handq_dir={handq_dir}" if handq_dir else ""
    return (
        f"[Remote HandQ ready] credentials_file={creds_file}{dir_line}\n"
        f"Actions: discover, submit_goal, get_status, send_message, get_result, exit_handq\n"
        f"Workflow: submit_goal(goal=...) → get_status(wait_timeout=N) → get_result\n"
        f"Pass credentials_file in every remote_handq tool call."
        + (f"\nPass handq_dir={handq_dir} to skip re-discovery." if handq_dir else "")
    )


def _build_brief_hint(creds_file: str, handq_dir: str) -> str:
    """Brief hint for subsequent activations (same host, already cached)."""
    dir_part = f" | handq_dir={handq_dir}" if handq_dir else ""
    return f"[Remote HandQ] credentials_file={creds_file}{dir_part}"
