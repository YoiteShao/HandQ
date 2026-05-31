# -*- coding: utf-8 -*-
"""
Remote HandQ Tool — delegate tasks to a remote Linux HandQ instance over SSH.

Provides high-level actions that abstract the Linux HandQ's file-based IPC
protocol (state.json, messages/ queue, execution_logs/).  The agent never
needs to know the IPC details — it just submits goals and collects results.

Reuses the SSH connection pool and credential infrastructure from ssh_tool.py.

Prerequisites: Linux HandQ must be pre-installed on the remote host by the
user (bash handq_setup.sh).  This tool handles communication only — it can
start an idle HandQ (handq --new) but never deploys software.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Tuple

from .base_tool import BaseTool, ToolResult
from .cancellation import run_with_abort_handle
from .ssh_tool import (
    _connect,
    _exec_command,
    _load_credentials,
    _PARAMIKO_AVAILABLE,
)


# ── HANDQ_DIR discovery cache ────────────────────────────────────────────────
# Keyed by "hostname:port" — populated by _discover_handq_dir().
_handq_dir_cache: Dict[str, str] = {}
_handq_bin_cache: Dict[str, str] = {}


def _host_key(creds: Dict[str, Any]) -> str:
    return f"{creds['hostname']}:{creds.get('port', 22)}"


def _discover_handq_dir(creds: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """
    Discover HANDQ_DIR, username, hostname, and handq binary on the remote.

    Returns: (handq_dir, remote_user, remote_hostname, handq_binary)
    Raises RuntimeError if HandQ is not installed.
    """
    hk = _host_key(creds)
    cached_dir = _handq_dir_cache.get(hk)
    cached_bin = _handq_bin_cache.get(hk, "handq")

    if cached_dir:
        return cached_dir, "", "", cached_bin

    probe_cmd = (
        'bash -c \''
        'U=$(whoami); H=$(hostname -s); '
        'D="$HOME/.handq/${U}@${H}"; '
        'echo "USER=$U"; echo "HOST=$H"; echo "HOME=$HOME"; '
        'if [ -d "$D" ]; then echo "DIR=$D"; else echo "MISSING=$D"; fi; '
        'B=$(which handq 2>/dev/null || '
        '  ls "$HOME/.local/bin/handq" 2>/dev/null || '
        '  ls "$HOME/.local/share/handq/handq.bin" 2>/dev/null || '
        '  ls "$HOME/handq" 2>/dev/null || '
        '  echo ""); '
        'echo "BIN=$B"'
        '\''
    )

    with _connect(creds) as (client, _):
        stdout, stderr, rc = _exec_command(client, probe_cmd, timeout=10.0)

    remote_user = ""
    remote_host = ""
    handq_dir = ""
    handq_bin = ""
    missing_dir = ""

    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("USER="):
            remote_user = line[5:]
        elif line.startswith("HOST="):
            remote_host = line[5:]
        elif line.startswith("DIR="):
            handq_dir = line[4:]
        elif line.startswith("MISSING="):
            missing_dir = line[8:]
        elif line.startswith("BIN=") and line[4:].strip():
            handq_bin = line[4:].strip()

    if not handq_bin:
        raise RuntimeError(
            f"HandQ is not installed on the remote host ({creds['hostname']}). "
            f"The user must run 'bash handq_setup.sh --config <config_path>' "
            f"on the Linux machine first."
        )

    # HANDQ_DIR not found → initialize by running handq --new
    if not handq_dir:
        with _connect(creds) as (client, _):
            _exec_command(client, (
                f'bash -c \'export PATH="$HOME/.local/bin:$PATH"; '
                f'{handq_bin} --new 2>/dev/null; sleep 2\''
            ), timeout=25.0)

        # Re-probe for HANDQ_DIR
        with _connect(creds) as (client, _):
            stdout2, _, _ = _exec_command(client, (
                f'bash -c \'U=$(whoami); H=$(hostname -s); '
                f'D="$HOME/.handq/${{U}}@${{H}}"; '
                f'test -d "$D" && echo "DIR=$D"\''
            ), timeout=10.0)
        for line in stdout2.splitlines():
            if line.strip().startswith("DIR="):
                handq_dir = line.strip()[4:]
                break

    if not handq_dir:
        raise RuntimeError(
            f"Remote HandQ directory not found. "
            f"Expected: {missing_dir or '~/.handq/<user>@<host>'}. "
            f"Try running '{handq_bin} --new' on the remote host."
        )

    _handq_dir_cache[hk] = handq_dir
    _handq_bin_cache[hk] = handq_bin
    return handq_dir, remote_user, remote_host, handq_bin


def _read_remote_state(creds: Dict[str, Any], handq_dir: str) -> Dict[str, Any]:
    """Read and parse state.json from the remote HandQ instance."""
    cmd = f'cat "{handq_dir}/state.json" 2>/dev/null || echo "{{}}"'
    with _connect(creds) as (client, _):
        stdout, _, _ = _exec_command(client, cmd, timeout=10.0)
    try:
        return json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}


def _is_remote_handq_alive(creds: Dict[str, Any], handq_dir: str) -> bool:
    """Check if the remote HandQ process is alive (PID file + kill -0)."""
    cmd = (
        f'bash -c \''
        f'PF="{handq_dir}/handq.pid"; '
        f'if [ -f "$PF" ]; then '
        f'  PID=$(cat "$PF" 2>/dev/null); '
        f'  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then '
        f'    echo "ALIVE"; '
        f'  else echo "DEAD"; fi; '
        f'else echo "NOPID"; fi'
        f'\''
    )
    with _connect(creds) as (client, _):
        stdout, _, _ = _exec_command(client, cmd, timeout=10.0)
    return stdout.strip() == "ALIVE"


class RemoteHandQTool(BaseTool):
    """
    Delegate tasks to a remote Linux HandQ agent over SSH.

    Actions:
      discover      Locate HANDQ_DIR and binary on the remote host
      submit_goal   Ensure HandQ running + submit a goal
      get_status    Read state.json (optionally poll until completion)
      send_message  Inject a message into the running remote task
      get_result    Read completion output + execution log tail
      exit_handq    Shutdown the remote HandQ instance
    """

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover", "submit_goal", "get_status",
                    "send_message", "get_result", "exit_handq",
                ],
                "description": "Action to perform on the remote HandQ instance.",
            },
            "credentials_file": {
                "type": "string",
                "description": "Path to SSH credentials YAML file (provided by setup).",
            },
            "goal": {
                "type": "string",
                "description": "[submit_goal] Task description to submit to the remote agent.",
            },
            "message": {
                "type": "string",
                "description": "[send_message] Message to inject into the running task.",
            },
            "handq_dir": {
                "type": "string",
                "description": "Override HANDQ_DIR path if already known from discover.",
            },
            "wait_timeout": {
                "type": "number",
                "description": "[get_status/get_result] Max seconds to wait for completion (0=immediate). Default: 0.",
            },
            "poll_interval": {
                "type": "number",
                "description": "[get_status] Poll interval in seconds when waiting. Default: 5.",
            },
            "tail_lines": {
                "type": "integer",
                "description": "[get_result] Number of execution log lines to return. Default: 100.",
            },
        },
        "required": ["action", "credentials_file"],
        "additionalProperties": False,
    }

    def __init__(self):
        super().__init__("remote_handq")

    def execute(self, **params) -> ToolResult:
        if not _PARAMIKO_AVAILABLE:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=params,
                error="paramiko is not installed. Run: pip install paramiko",
            )

        action = params.get("action", "")
        creds_file = params.get("credentials_file", "")

        if not creds_file:
            return ToolResult(
                success=False, output=None,
                tool_name=self.name, tool_parameters=params,
                error="credentials_file is required.",
            )

        try:
            creds = _load_credentials(creds_file)
        except Exception as e:
            return ToolResult(
                success=False, output=None,
                tool_name=self.name, tool_parameters=params,
                error=f"Failed to load credentials: {e}",
            )

        handler = {
            "discover": self._action_discover,
            "submit_goal": self._action_submit_goal,
            "get_status": self._action_get_status,
            "send_message": self._action_send_message,
            "get_result": self._action_get_result,
            "exit_handq": self._action_exit_handq,
        }.get(action)

        if not handler:
            return ToolResult(
                success=False, output=None,
                tool_name=self.name, tool_parameters=params,
                error=f"Unknown action: {action}. Valid: discover, submit_goal, get_status, send_message, get_result, exit_handq",
            )

        try:
            result = handler(creds, params)
            return ToolResult(
                success=True,
                output=result,
                tool_name=self.name,
                tool_parameters=params,
            )
        except Exception as e:
            return ToolResult(
                success=False, output=None,
                tool_name=self.name, tool_parameters=params,
                error=str(e),
            )

    # ── Actions ───────────────────────────────────────────────────────────────

    def _resolve_handq_dir(self, creds: Dict[str, Any], params: Dict[str, Any]) -> str:
        """Get handq_dir from params override or discovery cache."""
        explicit = params.get("handq_dir", "")
        if explicit:
            return explicit
        hk = _host_key(creds)
        cached = _handq_dir_cache.get(hk)
        if cached:
            return cached
        handq_dir, _, _, _ = _discover_handq_dir(creds)
        return handq_dir

    def _action_discover(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Locate HANDQ_DIR and report remote HandQ state."""
        # Force fresh discovery (clear cache for this host)
        hk = _host_key(creds)
        _handq_dir_cache.pop(hk, None)
        _handq_bin_cache.pop(hk, None)

        handq_dir, remote_user, remote_host, handq_bin = _discover_handq_dir(creds)
        state = _read_remote_state(creds, handq_dir)
        alive = _is_remote_handq_alive(creds, handq_dir)

        return {
            "handq_dir": handq_dir,
            "remote_user": remote_user,
            "remote_hostname": remote_host,
            "handq_binary": handq_bin,
            "handq_active": state.get("handq_active", False),
            "task_status": state.get("task_status", ""),
            "session_id": state.get("session_id", ""),
            "process_alive": alive,
        }

    def _action_submit_goal(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a goal to the remote HandQ. Starts HandQ if not running."""
        goal = params.get("goal", "")
        if not goal:
            raise ValueError("'goal' parameter is required for submit_goal action.")

        handq_dir = self._resolve_handq_dir(creds, params)
        hk = _host_key(creds)
        handq_bin = _handq_bin_cache.get(hk, "handq")

        # Check if HandQ is running
        alive = _is_remote_handq_alive(creds, handq_dir)

        if not alive:
            # Start HandQ with --new to create a fresh session
            start_cmd = f'bash -c \'nohup {handq_bin} --new > /dev/null 2>&1; sleep 2\''
            with _connect(creds) as (client, _):
                _exec_command(client, start_cmd, timeout=20.0)

            # Verify it started
            alive = _is_remote_handq_alive(creds, handq_dir)
            if not alive:
                raise RuntimeError(
                    f"Failed to start remote HandQ. Binary: {handq_bin}. "
                    f"Ensure 'handq' is in PATH on the remote host."
                )

        # Write goal as a message file (atomic: write .tmp then mv)
        ts = time.strftime("%Y%m%d_%H%M%S")
        msg_dir = f"{handq_dir}/messages"
        msg_file = f"{msg_dir}/{ts}_remote.txt"
        tmp_file = f"{msg_file}.tmp"

        escaped_goal = goal.replace("'", "'\\''")
        write_cmd = (
            f"bash -c 'mkdir -p \"{msg_dir}\" && "
            f"printf \"%s\" '\"'\"'{escaped_goal}'\"'\"' > \"{tmp_file}\" && "
            f"mv \"{tmp_file}\" \"{msg_file}\"'"
        )

        with _connect(creds) as (client, _):
            stdout, stderr, rc = _exec_command(client, write_cmd, timeout=10.0)

        if rc != 0:
            raise RuntimeError(f"Failed to write goal message: {stderr}")

        # Brief wait for the monitor loop to pick it up (polls every 200ms)
        time.sleep(1.5)

        # Read state to confirm
        state = _read_remote_state(creds, handq_dir)
        return {
            "handq_dir": handq_dir,
            "session_id": state.get("session_id", ""),
            "task_status": state.get("task_status", ""),
            "note": "Goal submitted. Use get_status to monitor progress.",
        }

    def _action_get_status(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Read remote HandQ state. Optionally poll until completion."""
        handq_dir = self._resolve_handq_dir(creds, params)
        wait_timeout = float(params.get("wait_timeout", 0))
        poll_interval = float(params.get("poll_interval", 5))

        if wait_timeout <= 0:
            # Immediate read
            state = _read_remote_state(creds, handq_dir)
            return self._format_status(state)

        # Blocking poll via remote bash loop (single SSH connection)
        deadline = int(wait_timeout)
        interval = max(2, int(poll_interval))
        poll_cmd = (
            f'bash -c \''
            f'DEADLINE=$(($(date +%s) + {deadline})); '
            f'while [ $(date +%s) -lt $DEADLINE ]; do '
            f'  STATE=$(cat "{handq_dir}/state.json" 2>/dev/null); '
            f'  STATUS=$(echo "$STATE" | python3 -c "import sys,json; '
            f'    d=json.load(sys.stdin); print(d.get(\'task_status\',\'\'))" 2>/dev/null); '
            f'  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "" ]; then '
            f'    echo "$STATE"; exit 0; '
            f'  fi; '
            f'  sleep {interval}; '
            f'done; '
            f'cat "{handq_dir}/state.json" 2>/dev/null'
            f'\''
        )

        with _connect(creds) as (client, _):
            stdout, _, _ = _exec_command(client, poll_cmd, timeout=wait_timeout + 10)

        try:
            state = json.loads(stdout.strip() or "{}")
        except json.JSONDecodeError:
            state = {}

        result = self._format_status(state)
        if state.get("task_status") == "running":
            result["note"] = f"Timed out after {deadline}s — task still running."
        return result

    def _action_send_message(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Inject a message into the running remote HandQ task."""
        message = params.get("message", "")
        if not message:
            raise ValueError("'message' parameter is required for send_message action.")

        handq_dir = self._resolve_handq_dir(creds, params)
        msg_dir = f"{handq_dir}/messages"
        ts = time.strftime("%Y%m%d_%H%M%S")
        msg_file = f"{msg_dir}/{ts}_remote_msg.txt"
        tmp_file = f"{msg_file}.tmp"

        escaped_msg = message.replace("'", "'\\''")
        write_cmd = (
            f"bash -c 'mkdir -p \"{msg_dir}\" && "
            f"printf \"%s\" '\"'\"'{escaped_msg}'\"'\"' > \"{tmp_file}\" && "
            f"mv \"{tmp_file}\" \"{msg_file}\"'"
        )

        with _connect(creds) as (client, _):
            stdout, stderr, rc = _exec_command(client, write_cmd, timeout=10.0)

        if rc != 0:
            raise RuntimeError(f"Failed to send message: {stderr}")

        return {
            "success": True,
            "note": "Message delivered to remote HandQ message queue.",
        }

    def _action_get_result(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Read completion result and tail of execution log."""
        handq_dir = self._resolve_handq_dir(creds, params)
        tail_lines = int(params.get("tail_lines", 100))

        state = _read_remote_state(creds, handq_dir)
        task_status = state.get("task_status", "")
        completion_reason = state.get("completion_reason", "")
        session_id = state.get("session_id", "")
        workspace = state.get("workspace_path", "")

        # Find and tail the latest execution log
        log_tail = ""
        log_path = ""
        if workspace:
            log_dir = f"{workspace}/executions_logs"
            tail_cmd = (
                f'bash -c \''
                f'LOG=$(ls -t "{log_dir}/"*.log 2>/dev/null | head -1); '
                f'if [ -n "$LOG" ]; then '
                f'  echo "LOG_PATH=$LOG"; '
                f'  echo "---LOG_START---"; '
                f'  tail -n {tail_lines} "$LOG"; '
                f'fi'
                f'\''
            )
            with _connect(creds) as (client, _):
                stdout, _, _ = _exec_command(client, tail_cmd, timeout=15.0)

            for line in stdout.splitlines():
                if line.startswith("LOG_PATH="):
                    log_path = line[9:]
                    break

            if "---LOG_START---" in stdout:
                log_tail = stdout.split("---LOG_START---", 1)[1].strip()

        return {
            "task_status": task_status,
            "session_id": session_id,
            "completion_reason": completion_reason,
            "log_path": log_path,
            "log_tail": log_tail[:5000] if log_tail else "",
        }

    def _action_exit_handq(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Shutdown the remote HandQ instance."""
        hk = _host_key(creds)
        handq_bin = _handq_bin_cache.get(hk, "handq")
        handq_dir = self._resolve_handq_dir(creds, params)

        exit_cmd = f'bash -c \'{handq_bin} --exit 2>&1 || echo "EXIT_FAILED"\''
        with _connect(creds) as (client, _):
            stdout, _, rc = _exec_command(client, exit_cmd, timeout=15.0)

        # Clear cache for this host
        _handq_dir_cache.pop(hk, None)
        _handq_bin_cache.pop(hk, None)

        if "EXIT_FAILED" in stdout and rc != 0:
            raise RuntimeError(f"Failed to exit remote HandQ: {stdout}")

        return {
            "success": True,
            "note": "Remote HandQ instance shut down.",
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _format_status(state: Dict[str, Any]) -> Dict[str, Any]:
        """Format state.json into a clean status dict."""
        return {
            "handq_active": state.get("handq_active", False),
            "task_status": state.get("task_status", ""),
            "session_id": state.get("session_id", ""),
            "status_text": state.get("status_text", ""),
            "status_icon": state.get("status_icon", ""),
            "confidence_history": state.get("confidence_history", [])[-5:],
            "last_updated": state.get("last_updated", ""),
        }
