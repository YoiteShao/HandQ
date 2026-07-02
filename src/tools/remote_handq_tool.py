# -*- coding: utf-8 -*-
"""
Remote HandQ Tool — drive a remote Linux HandQ daemon over SSH (Windows side).

This is the Win→Linux control channel for the "one Windows HandQ controls many
Linux HandQ" design. Each Linux box runs ``handq_linux`` as a resident
``FlowControllerV2`` daemon (setsid-detached, no tmux/systemd). This tool speaks
that daemon's file-IPC protocol so the agent only deals in high-level actions —
submit a goal, read status, fetch the reply, interrupt, start a fresh session.

File IPC layout (mirrors ``handq_linux.py``), under ``~/.handq/<user>@<host>/``:
  state.json            daemon writes coarse status + latest_tool + checklist
  messages/<id>.txt     inbound goal / follow-up (we write here)
  commands/<id>.json    inbound new_session / interrupt (we write here)
  reply/<id>.txt        outbound reply, keyed by the message id we chose
  handq.pid             daemon liveness (pid + kill -0)

Wake model: if the daemon is not alive we launch it detached over SSH with
``nohup setsid <launch> --_daemon`` — it then survives Windows power / network
loss and is resumable. ``<launch>`` is discovered on the remote, in priority:
  1. the ``handq_linux`` command installed by ``handq_setup.sh`` (on PATH or in
     ``~/.local/bin``) — the canonical entry; it injects the per-host config;
  2. a standalone Nuitka binary (``handq_linux.dist/handq_linux.bin``) sitting
     un-installed in a common dir — auto-loads its dist-root config;
  3. a source checkout (``python3 <root>/handq_linux.py``), preferring a
     ``.venv``/``venv`` interpreter co-located with the script.

Prerequisite: the user has put HandQ on the remote — normally by copying the
built dist package (``handq_linux.dist/`` + ``handq_config.yaml`` + setup) and
running ``handq_setup.sh`` to install the ``handq_linux`` command; a source
checkout (``handq_linux.py`` next to ``src/``) also works. This tool never
deploys software; it only communicates and wakes the daemon.

Reuses the SSH connection pool + credential infrastructure from ssh_tool.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import posixpath
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .base_tool import BaseTool, ToolResult
from .ssh_tool import (
    _connect,
    _default_pool,
    _exec_command,
    _load_credentials,
)


# ── per-host discovery cache ─────────────────────────────────────────────────
# Keyed by "hostname:port" → resolved launch metadata from _discover().
_discovery_cache: Dict[str, Dict[str, str]] = {}

POLL_INTERVAL = 1.0  # seconds between reply/state polls when waiting


def _host_key(creds: Dict[str, Any]) -> str:
    return f"{creds['hostname']}:{creds.get('port', 22)}"


def _exec(creds: Dict[str, Any], command: str, timeout: float = 15.0) -> Tuple[str, str, int]:
    """Run one command on the remote via a pooled connection. (stdout, stderr, rc)."""
    with _connect(creds, _default_pool) as (client, _):
        return _exec_command(client, command, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery — where is handq_linux and how do we invoke it
# ─────────────────────────────────────────────────────────────────────────────
_PROBE = r"""
U=$(whoami); H=$(hostname -s 2>/dev/null || hostname); HM="$HOME"
echo "USER=$U"; echo "HOST=$H"
D="$HM/.handq/${U}@${H}"
echo "HANDQDIR=$D"
[ -d "$D" ] && echo "DIREXISTS=1" || echo "DIREXISTS=0"
PF="$D/handq.pid"
if [ -f "$PF" ]; then
  P=$(cat "$PF" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo "ALIVE=1"; else echo "ALIVE=0"; fi
else
  echo "ALIVE=0"
fi
# 1. Installed dispatcher (handq_setup.sh): canonical entry, on PATH or in
#    ~/.local/bin (the latter is usually NOT on a non-interactive SSH PATH).
#    It forwards "$@" and injects the per-host --config itself.
BIN=$(command -v handq_linux 2>/dev/null || true)
[ -z "$BIN" ] && [ -x "$HM/.local/bin/handq_linux" ] && BIN="$HM/.local/bin/handq_linux"
# 2/3. Un-installed copies in a common dir: a standalone Nuitka binary
#      (handq_linux.dist/handq_linux.bin auto-loads its dist-root config) or a
#      source checkout (handq_linux.py next to src/).
SBIN=""; SCRIPT=""
for r in "$HM/handq" "$HM/HandQ" "$HM" "$HM/.local/share/handq"; do
  [ -z "$SBIN" ] && [ -x "$r/handq_linux.dist/handq_linux.bin" ] && SBIN="$r/handq_linux.dist/handq_linux.bin"
  [ -z "$SBIN" ] && [ -x "$r/handq_linux.bin" ] && SBIN="$r/handq_linux.bin"
  [ -z "$SCRIPT" ] && [ -f "$r/handq_linux.py" ] && SCRIPT="$r/handq_linux.py"
done
PY=""
if [ -n "$SCRIPT" ]; then
  ROOT=$(dirname "$SCRIPT")
  for c in "$ROOT/.venv/bin/python3" "$ROOT/.venv/bin/python" "$ROOT/venv/bin/python3" "$ROOT/venv/bin/python"; do
    [ -x "$c" ] && { PY="$c"; break; }
  done
  [ -z "$PY" ] && PY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)
fi
echo "BIN=$BIN"; echo "SBIN=$SBIN"; echo "SCRIPT=$SCRIPT"; echo "PY=$PY"
"""


def _discover(creds: Dict[str, Any], *, force: bool = False) -> Dict[str, str]:
    """Probe the remote once for handq_dir + launch invocation. Cached per host.

    Returns {handq_dir, launch, remote_user, remote_host}. ``launch`` is the
    command prefix to invoke handq_linux, resolved in priority order:
      1. installed ``handq_linux`` dispatcher (injects the per-host --config),
      2. a standalone Nuitka binary (auto-loads its dist-root config),
      3. ``<python> <handq_linux.py>`` from a source checkout.
    Raises RuntimeError if handq_linux can't be found on the remote.
    """
    hk = _host_key(creds)
    if not force and hk in _discovery_cache:
        return _discovery_cache[hk]

    stdout, _, _ = _exec(creds, f"bash -c {_shq(_PROBE)}", timeout=15.0)
    fields: Dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v

    handq_dir = fields.get("HANDQDIR", "")
    binary = fields.get("BIN", "")
    standalone = fields.get("SBIN", "")
    script = fields.get("SCRIPT", "")
    py = fields.get("PY", "python3") or "python3"

    if binary:
        launch = _shq(binary)
    elif standalone:
        launch = _shq(standalone)
    elif script:
        launch = f"{_shq(py)} {_shq(script)}"
    else:
        raise RuntimeError(
            f"handq_linux not found on {creds['hostname']}. Copy the built dist "
            f"package (handq_linux.dist/ + handq_config.yaml + handq_setup.sh) to "
            f"the remote and run 'bash handq_setup.sh' to install the handq_linux "
            f"command, or place a source checkout (handq_linux.py next to src/) in "
            f"~/handq/, then retry."
        )

    info = {
        "handq_dir": handq_dir,
        "launch": launch,
        "remote_user": fields.get("USER", ""),
        "remote_host": fields.get("HOST", ""),
        "alive": fields.get("ALIVE", "0"),
        "dir_exists": fields.get("DIREXISTS", "0"),
    }
    _discovery_cache[hk] = info
    return info


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in a remote bash command."""
    return "'" + s.replace("'", "'\\''") + "'"


# ─────────────────────────────────────────────────────────────────────────────
#  Remote file primitives (base64 transport — safe for any goal text)
# ─────────────────────────────────────────────────────────────────────────────
def _write_remote_file(creds: Dict[str, Any], remote_path: str, content: str) -> None:
    """Atomically write *content* to *remote_path* (write .tmp then mv).

    Content is base64-encoded on this side and decoded remotely, so arbitrary
    text (quotes, newlines, unicode) transfers without shell-escaping hazards.
    """
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    d = posixpath.dirname(remote_path)
    tmp = remote_path + ".tmp"
    _inner = (
        f'mkdir -p {_shq(d)} && printf %s {_shq(b64)}'
        f' | base64 -d > {_shq(tmp)} && mv {_shq(tmp)} {_shq(remote_path)}'
    )
    cmd = f"bash -c {_shq(_inner)}"
    _, stderr, rc = _exec(creds, cmd, timeout=15.0)
    if rc != 0:
        raise RuntimeError(f"Failed to write {remote_path}: {stderr.strip()}")


def _read_remote_file(creds: Dict[str, Any], remote_path: str) -> Optional[str]:
    """Return file contents, or None if missing/empty."""
    cmd = f"cat {_shq(remote_path)} 2>/dev/null"
    stdout, _, _ = _exec(creds, cmd, timeout=15.0)
    return stdout if stdout.strip() else None


def _read_state(creds: Dict[str, Any], handq_dir: str) -> Dict[str, Any]:
    raw = _read_remote_file(creds, posixpath.join(handq_dir, "state.json"))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _daemon_alive(creds: Dict[str, Any], handq_dir: str) -> bool:
    pf = posixpath.join(handq_dir, "handq.pid")
    _inner = (
        f'P=$(cat {_shq(pf)} 2>/dev/null); '
        '[ -n "$P" ] && kill -0 "$P" 2>/dev/null && echo ALIVE || echo DEAD'
    )
    cmd = f"bash -c {_shq(_inner)}"
    stdout, _, _ = _exec(creds, cmd, timeout=10.0)
    return stdout.strip() == "ALIVE"


def _wake_daemon(creds: Dict[str, Any], info: Dict[str, str], config_path: str = "") -> bool:
    """Launch the daemon detached and wait for its pid file. Returns aliveness."""
    handq_dir = info["handq_dir"]
    launch = info["launch"]
    cfg = f" --config {_shq(config_path)}" if config_path else ""
    # nohup + setsid: detached from the SSH session's process group so it
    # survives the connection closing (and Windows power/network loss).
    wake = f"nohup setsid {launch} --_daemon{cfg} >/dev/null 2>&1 </dev/null & echo WOKE"
    _exec(creds, f"bash -c {_shq(wake)}", timeout=20.0)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if _daemon_alive(creds, handq_dir):
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _ensure_daemon(creds: Dict[str, Any], info: Dict[str, str], config_path: str = "") -> None:
    if _daemon_alive(creds, info["handq_dir"]):
        return
    if not _wake_daemon(creds, info, config_path):
        raise RuntimeError(
            f"Failed to wake remote HandQ daemon on {creds['hostname']}. "
            f"Launch tried: {info['launch']} --_daemon (see ~/.handq/.../daemon.log)."
        )


def _new_msg_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


class RemoteHandQTool(BaseTool):
    """Drive a remote Linux HandQ daemon over SSH.

    Actions:
      discover      Locate handq_linux + report daemon state
      submit_goal   Wake daemon if needed + queue a goal (optionally wait for reply)
      send_message  Queue a follow-up message into the running task
      get_status    Read state.json (task_status, status_text, latest_tool, checklist)
      get_result    Fetch reply/<message_id>.txt for a previously submitted goal
      get_confirmation    Read a pending risk/tool/secret/ask_human request, if any
      answer_confirmation Answer a pending confirmation so the remote task resumes
      new_session   Tell the daemon to start a fresh session
      interrupt     Abort the in-flight task (clears the pending checklist tail)
      exit_handq    Stop the remote daemon
    """

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover", "submit_goal", "send_message", "get_status",
                    "get_result", "get_confirmation", "answer_confirmation",
                    "new_session", "interrupt", "exit_handq",
                ],
                "description": "Action to perform on the remote HandQ daemon.",
            },
            "credentials_file": {
                "type": "string",
                "description": "Path to SSH credentials YAML file (provided by setup).",
            },
            "goal": {
                "type": "string",
                "description": "[submit_goal] Task description to submit to the remote agent.",
            },
            "message_id": {
                "type": "string",
                "description": "[get_result] The id returned by submit_goal/send_message, used to fetch its reply.",
            },
            "confirmation_id": {
                "type": "string",
                "description": "[answer_confirmation] The id from get_confirmation/get_status's pending_confirmation, identifying which request to answer.",
            },
            "decision": {
                "type": "string",
                "enum": ["yes", "no", "message"],
                "description": "[answer_confirmation] For a tool/risk confirmation: 'yes' to approve, 'no' to refuse, 'message' to refuse with guidance (requires 'message').",
            },
            "value": {
                "type": "string",
                "description": "[answer_confirmation] For a secret/text (ask_human) confirmation: the secret or text value to supply.",
            },
            "reason": {
                "type": "string",
                "description": "[interrupt] Optional human-readable reason recorded for the interrupt.",
            },
            "message": {
                "type": "string",
                "description": "[send_message] Follow-up message to inject into the running task. [answer_confirmation] Guidance text when decision='message'.",
            },
            "config_path": {
                "type": "string",
                "description": "Remote config path passed as --config when waking the daemon (optional; the daemon auto-loads a co-located handq_config.yaml).",
            },
            "handq_dir": {
                "type": "string",
                "description": "Override the remote HANDQ_DIR if already known (skips part of discovery).",
            },
            "wait_timeout": {
                "type": "number",
                "description": "[submit_goal/send_message/get_status/get_result] Max seconds to wait (0=return immediately). Default: 0.",
            },
        },
        "required": ["action", "credentials_file"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None):
        super().__init__("remote_handq", ctx=ctx)

    async def execute(self, **params) -> ToolResult:
        # Inner handlers are blocking paramiko I/O — run off the event loop.
        return await asyncio.to_thread(self._execute_sync, **params)

    def _execute_sync(self, **params) -> ToolResult:
        action = params.get("action", "")
        creds_file = params.get("credentials_file", "")

        if not creds_file:
            return self._fail(params, "credentials_file is required.")
        # Anchor a relative credentials_file path to the per-session workspace
        # rather than the process cwd. _load_credentials only does
        # os.path.expanduser; without this resolve a bare "creds.yaml" would
        # be looked up under the install dir (process cwd is no longer the
        # session workspace — see concurrency work).
        resolved_creds_file = self.resolve_in_workspace(creds_file)
        try:
            creds = _load_credentials(resolved_creds_file)
        except Exception as e:
            return self._fail(params, f"Failed to load credentials: {e}")

        handler = {
            "discover": self._action_discover,
            "submit_goal": self._action_submit_goal,
            "send_message": self._action_send_message,
            "get_status": self._action_get_status,
            "get_result": self._action_get_result,
            "get_confirmation": self._action_get_confirmation,
            "answer_confirmation": self._action_answer_confirmation,
            "new_session": self._action_new_session,
            "interrupt": self._action_interrupt,
            "exit_handq": self._action_exit_handq,
        }.get(action)
        if not handler:
            return self._fail(
                params,
                f"Unknown action: {action}. Valid: discover, submit_goal, "
                f"send_message, get_status, get_result, get_confirmation, "
                f"answer_confirmation, new_session, interrupt, exit_handq",
            )
        try:
            return ToolResult(
                success=True, output=handler(creds, params),
                tool_name=self.name, tool_parameters=params,
            )
        except Exception as e:
            return self._fail(params, str(e))

    def _fail(self, params: Dict[str, Any], error: str) -> ToolResult:
        return ToolResult(
            success=False, output=None,
            tool_name=self.name, tool_parameters=params, error=error,
        )

    # ── discovery / dir resolution ───────────────────────────────────────────
    def _info(self, creds: Dict[str, Any], params: Dict[str, Any], *, force: bool = False) -> Dict[str, str]:
        info = _discover(creds, force=force)
        override = params.get("handq_dir", "")
        if override:
            info = dict(info)
            info["handq_dir"] = override
        return info

    # ── actions ────────────────────────────────────────────────────────────
    def _action_discover(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params, force=True)
        handq_dir = info["handq_dir"]
        alive = _daemon_alive(creds, handq_dir)
        state = _read_state(creds, handq_dir) if alive else {}
        return {
            "handq_dir": handq_dir,
            "launch": info["launch"],
            "remote_user": info.get("remote_user", ""),
            "remote_hostname": info.get("remote_host", ""),
            "daemon_alive": alive,
            "session_id": state.get("session_id", ""),
            "task_status": state.get("task_status", ""),
        }

    def _action_submit_goal(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        goal = params.get("goal", "")
        if not goal:
            raise ValueError("'goal' is required for submit_goal.")
        info = self._info(creds, params)
        _ensure_daemon(creds, info, params.get("config_path", ""))
        return self._post_and_maybe_wait(creds, info, goal, params, label="Goal")

    def _action_send_message(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        message = params.get("message", "")
        if not message:
            raise ValueError("'message' is required for send_message.")
        info = self._info(creds, params)
        if not _daemon_alive(creds, info["handq_dir"]):
            raise RuntimeError("Remote HandQ daemon is not running; use submit_goal to start a task first.")
        return self._post_and_maybe_wait(creds, info, message, params, label="Message")

    def _post_and_maybe_wait(
        self, creds: Dict[str, Any], info: Dict[str, str], text: str,
        params: Dict[str, Any], *, label: str,
    ) -> Dict[str, Any]:
        handq_dir = info["handq_dir"]
        msgid = _new_msg_id()
        _write_remote_file(creds, posixpath.join(handq_dir, "messages", f"{msgid}.txt"), text)
        out: Dict[str, Any] = {
            "message_id": msgid,
            "handq_dir": handq_dir,
            "note": f"{label} queued. Use get_result with this message_id to fetch the reply, "
                    f"or get_status to monitor progress.",
        }
        wait = float(params.get("wait_timeout", 0) or 0)
        if wait > 0:
            reply = self._poll_reply(creds, handq_dir, msgid, wait)
            if reply is not None:
                out["reply"] = reply
                out["note"] = f"{label} processed; reply attached."
            else:
                out["note"] = f"{label} queued; timed out after {int(wait)}s waiting for the reply (task may still be running)."
        return out

    def _action_get_status(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        wait = float(params.get("wait_timeout", 0) or 0)
        deadline = time.time() + wait
        while True:
            # A crashed daemon leaves a stale state.json (only graceful stop()
            # unlinks it), so cross-check liveness before trusting the status.
            alive = _daemon_alive(creds, handq_dir)
            state = _read_state(creds, handq_dir)
            status = self._format_status(state)
            status["daemon_alive"] = alive
            if not alive:
                status["handq_active"] = False
                status["note"] = "Daemon is not alive; state.json may be stale."
                return status
            # Surface a pending confirmation so the agent knows it must answer
            # before the task can make progress. A confirmation can only be
            # pending while a task runs (the agent blocks mid-item on the
            # response file), so skip the extra round-trip when idle.
            pending = (
                self._pending_confirmation(creds, handq_dir)
                if status.get("task_status") == "running" else None
            )
            if pending:
                status["pending_confirmation"] = pending
                status["note"] = (
                    "A confirmation is pending — call answer_confirmation with "
                    "this confirmation_id so the remote task can continue."
                )
                return status
            if wait <= 0 or status["task_status"] == "idle" or time.time() >= deadline:
                if wait > 0 and status["task_status"] == "running":
                    status["note"] = f"Timed out after {int(wait)}s — task still running."
                return status
            time.sleep(max(POLL_INTERVAL, 2.0))

    def _action_get_result(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        msgid = params.get("message_id", "")
        if not msgid:
            raise ValueError("'message_id' is required for get_result (the id returned by submit_goal/send_message).")
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        wait = float(params.get("wait_timeout", 0) or 0)
        reply = self._poll_reply(creds, handq_dir, msgid, wait) if wait > 0 else \
            _read_remote_file(creds, posixpath.join(handq_dir, "reply", f"{msgid}.txt"))
        state = _read_state(creds, handq_dir)
        # A dead daemon with no reply means the result will never arrive; tell
        # the agent so it doesn't keep polling a stale 'running' status.
        alive = _daemon_alive(creds, handq_dir)
        out: Dict[str, Any] = {
            "message_id": msgid,
            "found": reply is not None,
            "reply": reply or "",
            "task_status": state.get("task_status", ""),
            "session_id": state.get("session_id", ""),
            "daemon_alive": alive,
        }
        if not alive and reply is None:
            out["note"] = "Daemon is not alive and no reply was found; the task may have died with the daemon."
        return out

    def _action_get_confirmation(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        pending = self._pending_confirmation(creds, handq_dir)
        if not pending:
            return {"pending": False, "note": "No confirmation is pending on the remote daemon."}
        return {"pending": True, **pending}

    def _action_answer_confirmation(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        cid = params.get("confirmation_id", "")
        if not cid:
            raise ValueError("'confirmation_id' is required for answer_confirmation (from get_confirmation/get_status).")
        req = self._read_confirmation_request(creds, handq_dir)
        if not req:
            return {"success": False, "note": "No confirmation is pending (it may have already been answered or timed out)."}
        pending_id = req.get("id", "")
        if pending_id and pending_id != cid:
            return {
                "success": False,
                "note": f"confirmation_id mismatch: the pending request is {pending_id!r}, not {cid!r}. "
                        f"Call get_confirmation to read the current request.",
            }
        kind = req.get("kind", "")
        resp: Dict[str, Any] = {"id": cid}
        if kind in ("secret", "text"):
            value = params.get("value")
            if value is None:
                raise ValueError(f"'value' is required to answer a '{kind}' confirmation.")
            resp["value"] = str(value)
        else:  # tool / risk
            decision = str(params.get("decision", "")).strip().lower()
            if decision not in ("yes", "no", "message"):
                raise ValueError("'decision' must be 'yes', 'no', or 'message' for a tool/risk confirmation.")
            resp["decision"] = decision
            if decision == "message":
                msg = params.get("message", "")
                if not msg:
                    raise ValueError("'message' is required when decision='message'.")
                resp["message"] = str(msg)
        self._write_confirmation_response(creds, handq_dir, resp)
        return {
            "success": True, "confirmation_id": cid, "kind": kind,
            "note": "Answer delivered; the remote task will resume.",
        }

    def _action_new_session(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        _ensure_daemon(creds, info, params.get("config_path", ""))
        self._post_command(creds, info["handq_dir"], {"action": "new_session"})
        return {"success": True, "note": "Fresh session requested on the remote daemon."}

    def _action_interrupt(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        if not _daemon_alive(creds, info["handq_dir"]):
            return {"success": False, "note": "Daemon not running; nothing to interrupt."}
        self._post_command(creds, info["handq_dir"], {
            "action": "interrupt",
            "reason": params.get("reason", "remote interrupt"),
        })
        return {"success": True, "note": "Interrupt queued; the in-flight task will be aborted and its pending tail cleared."}

    def _action_exit_handq(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        hk = _host_key(creds)
        cmd = f"bash -c {_shq(info['launch'] + ' --exit 2>&1 || true')}"
        _exec(creds, cmd, timeout=20.0)
        _discovery_cache.pop(hk, None)
        return {"success": True, "note": "Remote HandQ daemon shutdown requested."}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _post_command(self, creds: Dict[str, Any], handq_dir: str, cmd: Dict[str, Any]) -> None:
        cid = _new_msg_id()
        _write_remote_file(
            creds,
            posixpath.join(handq_dir, "commands", f"{cid}.json"),
            json.dumps(cmd, ensure_ascii=False),
        )

    def _read_confirmation_request(self, creds: Dict[str, Any], handq_dir: str) -> Optional[Dict[str, Any]]:
        raw = _read_remote_file(creds, posixpath.join(handq_dir, "confirmation_request.json"))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _write_confirmation_response(self, creds: Dict[str, Any], handq_dir: str, resp: Dict[str, Any]) -> None:
        _write_remote_file(
            creds,
            posixpath.join(handq_dir, "confirmation_response.json"),
            json.dumps(resp, ensure_ascii=False),
        )

    def _pending_confirmation(self, creds: Dict[str, Any], handq_dir: str) -> Optional[Dict[str, Any]]:
        """Shape a pending confirmation_request.json for the host, or None."""
        req = self._read_confirmation_request(creds, handq_dir)
        if not req:
            return None
        kind = req.get("kind", "")
        shaped: Dict[str, Any] = {
            "confirmation_id": req.get("id", ""),
            "kind": kind,
        }
        if kind == "risk":
            shaped["description"] = req.get("description", "")
            shaped["how_to_answer"] = "answer_confirmation with decision=yes|no|message (message needs 'message')."
        elif kind == "tool":
            shaped["tool_name"] = req.get("tool_name", "")
            shaped["hint"] = req.get("hint", "")
            shaped["params"] = req.get("params")
            shaped["how_to_answer"] = "answer_confirmation with decision=yes|no|message (message needs 'message')."
        else:  # secret / text (ask_human)
            shaped["prompt"] = req.get("prompt", "")
            shaped["how_to_answer"] = "answer_confirmation with 'value' set to the requested input."
        return shaped

    def _poll_reply(self, creds: Dict[str, Any], handq_dir: str, msgid: str, timeout: float) -> Optional[str]:
        """Wait for the *settled* reply for ``msgid`` (None on timeout / death).

        The reply file passes through two states for a task (Fix 1): while the
        task runs it holds only the plan-ack placeholder, and the authoritative
        completion summary overwrites it once the task settles. We must not
        return the placeholder, so each poll reads ``state.json`` BEFORE the
        reply file and returns only when both hold:
          * ``task_status == "idle"`` — the task has settled, and
          * the reply file exists.

        Read-state-before-reply matters: the orchestrator emits the ``idle``
        state change immediately before writing the final reply
        (``_emit_completion_reply``: notify idle → on_reply_to_user). Reading
        state first, then crossing the SSH round-trip to read the reply, absorbs
        that sub-millisecond gap — by the time our ``cat reply`` lands, the
        final summary has been written. The empty-body case (no completed item)
        never fires the reply sink, so the placeholder under an ``idle`` status
        is the correct fallback there.
        """
        path = posixpath.join(handq_dir, "reply", f"{msgid}.txt")
        deadline = time.time() + timeout
        while True:
            state = _read_state(creds, handq_dir)
            task_status = state.get("task_status", "")
            reply = _read_remote_file(creds, path)
            if reply is not None and task_status == "idle":
                return reply
            # A crashed daemon will never settle; hand back whatever exists
            # (the placeholder, or None) so the caller can report accurately.
            if not _daemon_alive(creds, handq_dir):
                return reply
            if time.time() >= deadline:
                return None
            time.sleep(max(POLL_INTERVAL, 2.0))

    @staticmethod
    def _format_status(state: Dict[str, Any]) -> Dict[str, Any]:
        """Shape state.json into the host-facing status (latest_tool + checklist)."""
        return {
            "handq_active": state.get("handq_active", False),
            "task_status": state.get("task_status", ""),
            "status_text": state.get("status_text", ""),
            "session_id": state.get("session_id", ""),
            "latest_tool": state.get("latest_tool"),
            "checklist": state.get("checklist", ""),
            "completed": state.get("completed", 0),
            "total": state.get("total", 0),
            "last_updated": state.get("last_updated", ""),
        }
