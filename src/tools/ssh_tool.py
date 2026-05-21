# -*- coding: utf-8 -*-
"""
Stateless SSH Tool — each action opens a fresh paramiko connection and closes it
when done.  No PTY, no session state, no pyte dependency.

Security model
--------------
  The LLM only ever sees ``credentials_file`` (a local file path).
  The actual hostname, username, password, and key_path are read from that file
  at runtime inside the tool and are NEVER passed through the LLM context.

Credentials file format (YAML or JSON)
---------------------------------------
  hostname: 192.168.1.100
  username: user
  key_path: ~/.ssh/id_rsa   # optional; tried before password
  password: secret          # optional; used when key auth fails
  keyring_service: myapp    # optional; fetch password from OS keyring instead
                            # (Windows Credential Manager / Linux Secret Service /
                            #  macOS Keychain).  Safer than storing password on disk.

Actions
-------
  exec        Run a short command; return stdout/stderr/exit_code.
  exec_bg     Launch a long-running command as a background process.
              Returns job_id, pid_file, log_path, exit_file.
  job_status  Poll a background job: running/done/unknown + log tail.
  tail_log    Read the last N lines of a remote log file (optional grep filter).
  fetch_log   Read a line-range slice of a remote log file (for paging large logs).
  write_file  Upload inline string content to a remote path via SFTP.
  run_script  write_file → exec_bg in one call (auto-detects OS).
  safe_exit   Kill all tracked background jobs and clean up pid files.

OS auto-detection
-----------------
  Each action automatically detects whether the remote host is Linux/macOS or
  Windows and generates the appropriate command set:
    Linux/macOS : bash / nohup / tail / wc / find / kill
    Windows     : PowerShell (Start-Process, Get-Process, Stop-Process, etc.)
  Detection is done once per connection via a single probe command.

Dependencies
------------
  pip install paramiko pyyaml
  pip install keyring   # optional; required only when keyring_service is used
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .base_tool import BaseTool, ToolResult

# ── Dependency checks ─────────────────────────────────────────────────────────

try:
    import paramiko as _paramiko
    _PARAMIKO_AVAILABLE = True
except ImportError:
    _PARAMIKO_AVAILABLE = False

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    import keyring as _keyring
    _KEYRING_AVAILABLE = True
except ImportError:
    _KEYRING_AVAILABLE = False


# ── Persistent connection pool ────────────────────────────────────────────────
# Reuses the same authenticated SSH Transport across all actions to the same
# host.  Eliminates repeated TCP+handshake+auth overhead and prevents the
# server's MaxStartups counter from being exhausted by our client.
#
# Pool lifecycle:
#   • On first action to a host: connect, authenticate, pool the SSHClient.
#   • On subsequent actions: verify transport.is_active(); if alive, reuse.
#   • If the transport has died (server reboot, network drop, idle timeout):
#     evict and reconnect transparently.
#   • Clients idle longer than _POOL_MAX_IDLE_SECS are evicted proactively
#     on the next access to avoid using a half-closed connection.
#
# The rate limiter is kept but only fires when a *new* TCP connection is
# actually established — pool reuses bypass it entirely (no new connection).

_conn_pool: Dict[str, Any] = {}           # host_key → SSHClient
_conn_pool_last_used: Dict[str, float] = {}  # host_key → monotonic time
_conn_pool_lock = threading.Lock()        # serialises pool access
_POOL_MAX_IDLE_SECS = 300                 # evict if idle longer than this

# Rate limiter — still used when a NEW connection must be opened.
_connect_timestamps: Dict[str, float] = {}
_connect_lock = threading.Lock()
_MIN_CONNECT_INTERVAL = 1.5   # seconds between NEW connections to the same host

# ── OS detection cache ────────────────────────────────────────────────────────
# The OS/shell probe (uname -s) is an extra round-trip inside every action.
# Cache the result per host so subsequent actions on the same host skip it.
# Thread-safe: protected by _os_cache_lock.

_os_cache: Dict[str, Tuple[str, str]] = {}   # "host:port" → (os_name, login_shell)
_os_cache_lock = threading.Lock()


def _rate_limit(host_key: str) -> None:
    """Block until the minimum inter-connection interval has elapsed."""
    with _connect_lock:
        last = _connect_timestamps.get(host_key, 0.0)
        wait = _MIN_CONNECT_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _connect_timestamps[host_key] = time.monotonic()


def _pool_get(host_key: str) -> Optional[Any]:
    """
    Return a live SSHClient from the pool, or None if unavailable.

    Evicts the entry if:
      - transport is no longer active (server closed / network drop)
      - the client has been idle longer than _POOL_MAX_IDLE_SECS
    """
    with _conn_pool_lock:
        client = _conn_pool.get(host_key)
        if client is None:
            return None

        # Idle-timeout eviction
        last_used = _conn_pool_last_used.get(host_key, 0.0)
        if time.monotonic() - last_used > _POOL_MAX_IDLE_SECS:
            _conn_pool.pop(host_key, None)
            _conn_pool_last_used.pop(host_key, None)
            try:
                client.close()
            except Exception:
                pass
            return None

        # Transport health check
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            _conn_pool.pop(host_key, None)
            _conn_pool_last_used.pop(host_key, None)
            try:
                client.close()
            except Exception:
                pass
            return None

        return client


def _pool_put(host_key: str, client: Any) -> None:
    """Store a successfully connected SSHClient in the pool."""
    with _conn_pool_lock:
        _conn_pool[host_key] = client
        _conn_pool_last_used[host_key] = time.monotonic()


def _pool_update_ts(host_key: str) -> None:
    """Touch the last-used timestamp of an already-pooled client."""
    with _conn_pool_lock:
        if host_key in _conn_pool:
            _conn_pool_last_used[host_key] = time.monotonic()


def _pool_evict(host_key: str) -> None:
    """Forcefully remove a client from the pool (e.g. after an error)."""
    with _conn_pool_lock:
        client = _conn_pool.pop(host_key, None)
        _conn_pool_last_used.pop(host_key, None)
    if client is not None:
        _linger_close(client)


def _linger_close(client: Any) -> None:
    """
    Close a paramiko SSHClient with SO_LINGER=0 so the OS sends TCP RST
    instead of FIN.  This prevents the socket from entering TIME_WAIT and
    immediately frees the port on both ends — no kernel-side 60–120 s wait.
    """
    try:
        transport = client.get_transport()
        if transport is not None:
            sock = getattr(transport, "sock", None)
            if sock is not None:
                try:
                    sock.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_LINGER,
                        struct.pack("ii", 1, 0),   # l_onoff=1, l_linger=0
                    )
                except OSError:
                    pass   # best-effort; fall through to normal close
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass


# ── OS detection ──────────────────────────────────────────────────────────────

def _detect_os_and_shell(client: Any) -> Tuple[str, str]:
    """
    Probe the remote host OS and login shell in a single round-trip.

    Returns (os_name, login_shell) where:
      os_name    : "linux" | "windows" | "cygwin"
      login_shell: "bash" | "tcsh" | "zsh" | "sh" | "powershell" | "unknown"

    "cygwin" means the SSH server is OpenSSH running under Cygwin on Windows.
    exec_bg on Cygwin must use Cygwin's own bash/nohup rather than PowerShell
    Start-Process, which cannot inherit Cygwin POSIX file descriptors.

    Defaults to ("linux", "unknown") on any error so that existing behaviour
    is preserved for unknown systems.
    """
    try:
        # $SHELL is set to the login shell path on Linux/macOS/Cygwin.
        # On Windows $SHELL is absent; 'ver' prints the Windows version.
        probe = "uname -s 2>/dev/null || ver; echo __SHELL__$SHELL"
        stdout, _, _ = _exec_command(client, probe, timeout=10.0)
        out_lower = stdout.strip().lower()

        # Pure Windows (no Cygwin): uname absent, 'ver' shows "Microsoft Windows"
        if ("windows" in out_lower or "microsoft" in out_lower) and "cygwin" not in out_lower:
            return "windows", "powershell"

        # Detect login shell from $SHELL
        login_shell = "unknown"
        if "__SHELL__" in stdout:
            shell_path = stdout.split("__SHELL__", 1)[-1].strip().split("\n")[0].lower()
            if "tcsh" in shell_path or "/csh" in shell_path:
                login_shell = "tcsh"
            elif "zsh" in shell_path:
                login_shell = "zsh"
            elif "bash" in shell_path:
                login_shell = "bash"
            elif shell_path.endswith("/sh") or shell_path == "sh":
                login_shell = "sh"

        # Cygwin: uname -s returns "CYGWIN_NT-..."
        if "cygwin" in out_lower:
            return "cygwin", login_shell

        return "linux", login_shell
    except Exception:
        return "linux", "unknown"


def _detect_os_and_shell_cached(client: Any, host_key: str) -> Tuple[str, str]:
    """
    Return (os_name, login_shell) for the remote host, using a module-level
    cache keyed by "hostname:port".  The probe round-trip is skipped on every
    call after the first for the same host.

    host_key must be the same string used by _rate_limit(), i.e. "host:port".
    """
    with _os_cache_lock:
        if host_key in _os_cache:
            return _os_cache[host_key]
    result = _detect_os_and_shell(client)
    with _os_cache_lock:
        _os_cache[host_key] = result
    return result


def _detect_os(client: Any) -> str:
    """Backward-compatible wrapper — returns just the OS name (no cache)."""
    return _detect_os_and_shell(client)[0]


def _detect_os_cached(client: Any, host_key: str) -> str:
    """Cached wrapper — returns just the OS name."""
    return _detect_os_and_shell_cached(client, host_key)[0]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_credentials(credentials_file: str) -> Dict[str, Any]:
    """
    Load SSH credentials from a local YAML or JSON file.

    Returns a dict with at least 'hostname' and 'username'.
    Optional keys: 'key_path', 'password', 'port', 'keyring_service'.

    If 'keyring_service' is set, the password is fetched from the OS keyring
    (Windows Credential Manager / Linux Secret Service / macOS Keychain) using
    the service name from 'keyring_service' and the username from 'username'.
    This keeps the password out of any file on disk.

    Raises FileNotFoundError / ValueError on bad input.
    """
    path = os.path.expanduser(credentials_file)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Credentials file not found: {path}")

    # Warn if file permissions are too open (Unix only)
    if hasattr(os, "stat"):
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:  # group or other can read/write
            import warnings
            warnings.warn(
                f"[SECURITY] Credentials file permissions are too open: {oct(mode)}. "
                f"Run: chmod 600 {path}",
                stacklevel=2,
            )

    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    # Try YAML first (superset of JSON), fall back to json.loads
    creds: Dict[str, Any] = {}
    if _YAML_AVAILABLE:
        creds = _yaml.safe_load(raw) or {}
    else:
        try:
            creds = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Cannot parse credentials file (pyyaml not installed, JSON parse failed): {exc}"
            ) from exc

    if not isinstance(creds, dict):
        raise ValueError("Credentials file must be a YAML/JSON mapping.")

    for required in ("hostname", "username"):
        if not creds.get(required):
            raise ValueError(f"Credentials file missing required field: '{required}'")

    # Resolve keyring_service → password
    keyring_service: Optional[str] = creds.get("keyring_service")
    if keyring_service and not creds.get("password"):
        if not _KEYRING_AVAILABLE:
            raise ImportError(
                "keyring_service is set but the 'keyring' package is not installed. "
                "Install with: pip install keyring"
            )
        username: str = creds["username"]

        # On headless Linux, the default backend may be fail.Keyring.
        # Auto-upgrade to EncryptedKeyring (keyrings.alt) if available.
        backend = _keyring.get_keyring()
        if "fail" in backend.__class__.__module__.lower():
            try:
                from keyrings.alt.file import EncryptedKeyring  # type: ignore
                _keyring.set_keyring(EncryptedKeyring())
            except ImportError:
                try:
                    from keyrings.alt.file import PlaintextKeyring  # type: ignore
                    _keyring.set_keyring(PlaintextKeyring())
                except ImportError:
                    raise ImportError(
                        "No usable keyring backend found. "
                        "On headless Linux, install: pip install keyrings.alt pycryptodome"
                    )

        secret = _keyring.get_password(keyring_service, username)
        if secret is None:
            raise ValueError(
                f"No password found in keyring for service='{keyring_service}' "
                f"username='{username}'. "
                f"Store it first with: python handq_keyring.py set {keyring_service} {username}"
            )
        creds = dict(creds)  # don't mutate the parsed dict
        creds["password"] = secret

    return creds


def _new_client(
    creds: Dict[str, Any],
    host_key: str,
) -> Any:
    """
    Open a fresh paramiko SSHClient, authenticate, configure keepalives,
    and return it.  Does NOT pool the result — callers handle pooling.

    Authentication order:
      1. key_path from credentials (if provided)
      2. Default keys in ~/.ssh/ (id_rsa / id_ed25519 / id_ecdsa / id_dsa)
      3. ssh-agent
      4. password from credentials (if provided)

    Retry policy:
      Transient network errors (OSError, SSHException including banner errors)
      are retried up to 2 times with exponential backoff (1.5 s then 3 s).
      Authentication failures are NOT retried.

    Rate-limits new connections to _MIN_CONNECT_INTERVAL per host so rapid
    sequential failures cannot flood the server's MaxStartups counter.
    """
    if not _PARAMIKO_AVAILABLE:
        raise ImportError("paramiko is required. Install with: pip install paramiko")

    hostname: str = creds["hostname"]
    username: str = creds["username"]
    port: int = int(creds.get("port", 22))
    key_path: Optional[str] = creds.get("key_path")
    password: Optional[str] = creds.get("password")

    if key_path:
        key_path = os.path.expanduser(key_path)

    # id_dsa intentionally excluded: modern cryptography library enforces strict
    # DSA q-parameter sizes (160/224/256 bits) and rejects legacy keys with
    # non-standard q, causing an unrecoverable ValueError during auth.
    # DSA is deprecated; use RSA / Ed25519 / ECDSA instead.
    default_keys = [
        os.path.expanduser(k)
        for k in ("~/.ssh/id_rsa", "~/.ssh/id_ed25519", "~/.ssh/id_ecdsa")
        if os.path.exists(os.path.expanduser(k))
    ]
    key_files = ([key_path] if key_path else []) + [
        k for k in default_keys if k != key_path
    ]

    _MAX_RETRIES = 2
    _BASE_DELAY  = 1.5   # seconds; doubled each retry (1.5 s, 3 s)
    last_error: Optional[Exception] = None
    connected = False
    client: Optional[Any] = None

    # Rate-limit only when actually opening a new TCP connection.
    _rate_limit(host_key)

    for _attempt in range(_MAX_RETRIES + 1):
        client = _paramiko.SSHClient()
        client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
        _is_auth_failure = False

        try:
            kw: Dict[str, Any] = dict(
                hostname=hostname, port=port, username=username,
                timeout=15, banner_timeout=30, allow_agent=True, look_for_keys=False,
            )
            if key_files:
                kw["key_filename"] = key_files
            client.connect(**kw)
            connected = True
        except _paramiko.AuthenticationException as exc:
            last_error = exc
            _is_auth_failure = True
        except (_paramiko.SSHException, OSError) as exc:
            last_error = exc

        if not connected and password:
            try:
                client.connect(
                    hostname=hostname, port=port, username=username,
                    timeout=15, banner_timeout=30, password=password,
                    allow_agent=False, look_for_keys=False,
                )
                connected = True
                _is_auth_failure = False
            except _paramiko.AuthenticationException as exc:
                last_error = exc
                _is_auth_failure = True
            except Exception as exc:
                last_error = exc

        if connected:
            last_error = None
            break

        _linger_close(client)
        client = None

        if _is_auth_failure:
            break

        if _attempt < _MAX_RETRIES:
            time.sleep(_BASE_DELAY * (2 ** _attempt))

    if not connected or client is None:
        raise ConnectionError(
            f"SSH connection failed for {username}@{hostname}:{port} "
            f"(tried {min(_attempt + 1, _MAX_RETRIES + 1)} time(s), "
            f"with up to {_MAX_RETRIES} retries). "
            f"Last error: {last_error}"
        )

    # SSH-level keepalive: sends MSG_IGNORE every 30 s to prevent NAT/firewall
    # from silently dropping the idle TCP session during long commands or waits.
    # TCP-level SO_KEEPALIVE adds a second layer of protection.
    try:
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
            sock = getattr(transport, "sock", None)
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass

    return client


@contextlib.contextmanager
def _connect(creds: Dict[str, Any]):
    """
    Context manager: obtain an authenticated SSHClient (from the pool when
    possible), yield it, then return it to the pool.

    Pool reuse
    ----------
    All actions to the same host share a single persistent SSH Transport.
    Only the very first action to a host (and recovery after a drop) opens
    a new TCP connection — subsequent actions reuse the existing transport
    via paramiko channels, paying zero TCP+handshake+auth overhead and
    exerting zero pressure on the server's MaxStartups counter.

    Transparent reconnect
    ---------------------
    If the pooled transport is detected as inactive before use (server
    reboot, network drop, idle-timeout eviction), the pool entry is
    discarded and a fresh connection is established automatically.

    If a command raises an exception that indicates the transport died
    *during* execution, the caller should call _pool_evict(host_key) to
    remove the stale entry before the next action retries.

    Connection management
    ---------------------
    On context exit the client is returned to the pool (last-used timestamp
    updated).  SO_LINGER=0 is only applied when the pool itself evicts a
    stale entry — never on a healthy return.
    """
    hostname: str = creds["hostname"]
    port: int = int(creds.get("port", 22))
    host_key = f"{hostname}:{port}"

    # Try pool first
    client = _pool_get(host_key)
    is_new = client is None

    if is_new:
        # _new_client() applies the rate limiter before opening TCP.
        client = _new_client(creds, host_key)
        _pool_put(host_key, client)

    try:
        yield client, host_key
        # Successful use — refresh the idle timer.
        _pool_update_ts(host_key)
    except Exception:
        # If the transport died during the action, evict so the next call
        # gets a fresh connection rather than a broken one.
        transport = client.get_transport() if client else None
        if transport is None or not transport.is_active():
            _pool_evict(host_key)
        raise


def _exec_command(
    client: Any,
    command: str,
    timeout: float = 30.0,
) -> Tuple[str, str, int]:
    """
    Execute *command* on the remote host via exec_command (no PTY).

    Returns: (stdout_text, stderr_text, exit_code)
    Raises: TimeoutError if the command does not finish within *timeout* seconds.
    """
    _, stdout_ch, stderr_ch = client.exec_command(command, timeout=timeout)
    channel = stdout_ch.channel
    # Per-recv timeout so a firewall-dropped idle connection raises socket.timeout
    # instead of blocking forever waiting for the final EOF/exit-status.
    channel.settimeout(timeout)

    stdout_buf: List[bytes] = []
    stderr_buf: List[bytes] = []
    deadline = time.monotonic() + timeout

    while True:
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError(
                f"Command timed out after {timeout}s: {command[:120]}"
            )
        if channel.recv_ready():
            stdout_buf.append(channel.recv(65536))
        if channel.recv_stderr_ready():
            stderr_buf.append(channel.recv_stderr(65536))
        if channel.exit_status_ready():
            # Drain any remaining bytes before reading exit status
            while channel.recv_ready():
                stdout_buf.append(channel.recv(65536))
            while channel.recv_stderr_ready():
                stderr_buf.append(channel.recv_stderr(65536))
            break
        time.sleep(0.05)

    stdout_text = b"".join(stdout_buf).decode("utf-8", errors="replace")
    stderr_text = b"".join(stderr_buf).decode("utf-8", errors="replace")
    exit_code = channel.recv_exit_status()
    return stdout_text, stderr_text, exit_code


# ── OS-specific background-job helpers ────────────────────────────────────────

def _exec_bg_linux(
    client: Any,
    command: str,
    job_id: str,
    log_path: str,
    pid_file: str,
    exit_file: str,
    workdir: Optional[str],
    timeout: float,
) -> Tuple[str, str, int]:
    """Launch a background job on Linux/macOS using nohup."""
    log_dir = log_path.rsplit("/", 1)[0] if "/" in log_path else "~/handq_jobs"
    if workdir:
        inner = f"cd {shlex.quote(workdir)} && {{ {command}; }}; echo $? > {exit_file}"
    else:
        inner = f"{{ {command}; }}; echo $? > {exit_file}"
    # Wrap the entire launch sequence in explicit `bash -c` so it executes
    # correctly regardless of the user's login shell (tcsh/csh use a different
    # variable-assignment syntax and reject `_PID=$!`).
    # paramiko exec_command sends commands via the login shell; wrapping in
    # bash -c bypasses that and guarantees POSIX behaviour.
    bash_body = (
        f"mkdir -p {log_dir} && "
        f"nohup bash -c {shlex.quote(inner)} > {log_path} 2>&1 & "
        f"_PID=$!; echo $_PID > {pid_file}; echo $_PID"
    )
    launch = f"bash -c {shlex.quote(bash_body)}"
    return _exec_command(client, launch, timeout=timeout)


def _exec_bg_cygwin(
    client: Any,
    command: str,
    job_id: str,
    log_path: str,
    pid_file: str,
    exit_file: str,
    workdir: Optional[str],
    timeout: float,
) -> Tuple[str, str, int]:
    """
    Launch a background job on a Windows host running Cygwin OpenSSH.

    PowerShell Start-Process cannot inherit Cygwin POSIX file descriptors, so
    stdout/stderr redirection via -RedirectStandardOutput silently produces
    empty log files.  Using Cygwin's own bash + nohup (identical to the Linux
    path) avoids the Windows HANDLE / POSIX fd mismatch entirely.
    """
    # Identical to _exec_bg_linux — Cygwin provides bash, nohup, mkdir, echo
    return _exec_bg_linux(
        client, command, job_id, log_path, pid_file, exit_file, workdir, timeout
    )


def _exec_bg_windows(
    client: Any,
    command: str,
    job_id: str,
    log_path: str,
    pid_file: str,
    exit_file: str,
    workdir: Optional[str],
    timeout: float,
) -> Tuple[str, str, int]:
    """
    Launch a background job on Windows using PowerShell Start-Process.

    The job runs detached; its PID is written to pid_file and its stdout/stderr
    are redirected to log_path.  When the process exits, its exit code is written
    to exit_file — matching the Linux nohup contract so job_status works uniformly.
    """
    job_dir = log_path.rsplit("/", 1)[0] if "/" in log_path else "~/handq_jobs"

    # Wrap the user command so we capture exit code into exit_file
    if workdir:
        inner_ps = (
            f"Set-Location -Path '{workdir}'; "
            f"{command}; "
            f"$LASTEXITCODE | Out-File -FilePath '{exit_file}' -Encoding ascii"
        )
    else:
        inner_ps = (
            f"{command}; "
            f"$LASTEXITCODE | Out-File -FilePath '{exit_file}' -Encoding ascii"
        )

    # Escape single quotes inside the inner script for embedding in outer PS string
    inner_escaped = inner_ps.replace("'", "\\'")

    launch = (
        f"powershell -Command \""
        f"New-Item -ItemType Directory -Force -Path '{job_dir}' | Out-Null; "
        f"$proc = Start-Process powershell "
        f"  -ArgumentList '-ExecutionPolicy','Bypass','-Command','{inner_escaped}' "
        f"  -RedirectStandardOutput '{log_path}' "
        f"  -RedirectStandardError '{log_path}.err' "
        f"  -PassThru -WindowStyle Hidden; "
        f"$proc.Id | Out-File -FilePath '{pid_file}' -Encoding ascii; "
        f"Write-Output $proc.Id"
        f"\""
    )
    return _exec_command(client, launch, timeout=timeout)


def _job_status_linux(
    client: Any,
    pid_file: str,
    log_path: str,
    exit_file: str,
    tail_lines: int,
    timeout: float,
) -> str:
    """Batch status check on Linux — returns the combined stdout string."""
    batch_body = (
        f"_PID=$(cat {pid_file} 2>/dev/null || echo ''); "
        f"if [ -n \"$_PID\" ] && kill -0 \"$_PID\" 2>/dev/null; then echo ALIVE; else echo DEAD; fi; "
        f"echo '---EXIT---'; cat {exit_file} 2>/dev/null || echo ''; "
        f"echo '---TAIL---'; tail -n {tail_lines} {log_path} 2>/dev/null || echo ''; "
        f"echo '---WC---'; wc -l < {log_path} 2>/dev/null || echo 0"
    )
    # Wrap in bash to work correctly when the login shell is tcsh/csh.
    batch = f"bash -c {shlex.quote(batch_body)}"
    stdout, _, _ = _exec_command(client, batch, timeout=timeout)
    return stdout


def _job_status_windows(
    client: Any,
    pid_file: str,
    log_path: str,
    exit_file: str,
    tail_lines: int,
    timeout: float,
) -> str:
    """
    Batch status check on Windows — returns a string in the same
    ---EXIT--- / ---TAIL--- / ---WC--- format as the Linux version so that
    the shared parser in _action_job_status works without modification.
    """
    batch = (
        f"powershell -Command \""
        f"$pid = (Get-Content '{pid_file}' -ErrorAction SilentlyContinue); "
        f"if ($pid -and (Get-Process -Id $pid -ErrorAction SilentlyContinue)) "
        f"  {{ Write-Output 'ALIVE' }} else {{ Write-Output 'DEAD' }}; "
        f"Write-Output '---EXIT---'; "
        f"Get-Content '{exit_file}' -ErrorAction SilentlyContinue; "
        f"Write-Output '---TAIL---'; "
        f"Get-Content '{log_path}' -ErrorAction SilentlyContinue | Select-Object -Last {tail_lines}; "
        f"Write-Output '---WC---'; "
        f"(Get-Content '{log_path}' -ErrorAction SilentlyContinue | Measure-Object -Line).Lines"
        f"\""
    )
    stdout, _, _ = _exec_command(client, batch, timeout=timeout)
    return stdout


def _tail_log_cmd_windows(log_path: str, lines: int, pattern: Optional[str]) -> str:
    """
    Build a PowerShell command that tails a log file on Windows.
    Returns a command string in the same ---WC--- format as the Linux version.
    """
    if pattern:
        # Select-String is PowerShell's grep equivalent
        ps = (
            f"$content = Get-Content '{log_path}' -ErrorAction SilentlyContinue | "
            f"Select-Object -Last {lines} | "
            f"Select-String -Pattern '{pattern}' | ForEach-Object {{ $_.Line }}; "
            f"$content; "
            f"Write-Output '---WC---'; "
            f"(Get-Content '{log_path}' -ErrorAction SilentlyContinue | Measure-Object -Line).Lines"
        )
    else:
        ps = (
            f"Get-Content '{log_path}' -ErrorAction SilentlyContinue | Select-Object -Last {lines}; "
            f"Write-Output '---WC---'; "
            f"(Get-Content '{log_path}' -ErrorAction SilentlyContinue | Measure-Object -Line).Lines"
        )
    return f"powershell -Command \"{ps}\""


# ── StatelessSSHTool ──────────────────────────────────────────────────────────

class StatelessSSHTool(BaseTool):
    """
    Stateless SSH tool.

    Every action opens a fresh SSH connection, executes the operation, and
    closes the connection immediately.  No session state is kept between calls.

    SECURITY: The LLM only passes ``credentials_file`` (a local file path).
    hostname, username, password, and key_path are read from that file inside
    the tool and never appear in LLM context.

    Actions
    -------
    exec
        Run a short command on the remote host.
        Args: credentials_file(str), command(str), timeout(float, default 30),
              workdir(str, optional)
        Returns: stdout, stderr, exit_code

    exec_bg
        Launch a long-running command as a nohup background process.
        Args: credentials_file(str), command(str),
              job_id(str, optional), log_path(str, optional),
              pid_file(str, optional), workdir(str, optional),
              timeout(float, default 30)
        Returns: job_id, pid_file, log_path, exit_file

    job_status
        Poll a background job launched by exec_bg or run_script.
        Args: credentials_file(str), pid_file(str), log_path(str),
              exit_file(str, optional), tail_lines(int, default 50),
              timeout(float, default 15)
        Returns: status("running"|"done"|"unknown"), exit_code,
                 total_lines, log_tail, error_summary

    tail_log
        Read the last N lines of a remote log file.
        Args: credentials_file(str), log_path(str),
              lines(int, default 100), pattern(str, optional),
              timeout(float, default 15)
        Returns: content, total_lines

    fetch_log
        Read a line-range slice of a remote log file (for paging large logs).
        Args: credentials_file(str), log_path(str),
              start_line(int, default 1), end_line(int, default start+199),
              timeout(float, default 15)
        Returns: content, start_line, end_line

    write_file
        Upload inline string content to a remote path via SFTP.
        Args: credentials_file(str), remote_path(str), content(str)
        Returns: remote_path, bytes_written

    run_script
        write_file → chmod +x → exec_bg in one call.
        Args: credentials_file(str), script_content(str),
              script_name(str, default 'handq_script.sh'),
              job_id(str, optional), workdir(str, optional),
              timeout_hint_seconds(int, optional)
        Returns: job_id, pid_file, log_path, exit_file, script_remote_path

    safe_exit
        Kill all nohup jobs tracked under ~/handq_jobs/ and remove pid files.
        Args: credentials_file(str), timeout(float, default 15)
        Returns: killed_count, message

    wait_done
        Block inside a SINGLE SSH connection until a background job finishes,
        then return the result.  Eliminates repeated job_status polling (each
        poll opens a new connection); the remote host runs a sleep loop instead.
        SSH keepalive packets are sent every 30 s to prevent NAT/firewall from
        silently dropping the idle connection during the wait.
        Args: credentials_file(str), pid_file(str), log_path(str),
              exit_file(str, optional), timeout(float, default 300),
              poll_interval(float, default 5), tail_lines(int, default 50)
        Returns: status("done"|"timeout"), exit_code, log_tail,
                 total_lines, error_summary, waited_seconds

    Connection management
    ---------------------
    All actions to the same host share a single persistent SSH Transport
    (connection pool).  Only the first action to a host opens a new TCP
    connection; subsequent actions reuse the existing transport, paying zero
    handshake/auth overhead and never touching the server's MaxStartups counter.

    1. Pool reuse    — one TCP connection per host; all actions share it.
    2. Auto-reconnect— if the transport dies (server reboot, network drop,
                       idle eviction after 300 s), the next action reconnects
                       transparently with retries and exponential backoff.
    3. Rate limiter  — enforces a minimum 1.5 s gap between NEW connections
                       (pool reuses bypass this entirely).
    4. SO_LINGER=0   — applied only when evicting a stale pool entry so the
                       local port is freed immediately without TIME_WAIT.
    5. Keepalive     — transport.set_keepalive(30) + SO_KEEPALIVE prevent NAT
                       from silently dropping the idle shared transport.

    OS auto-detection is cached per host after the first probe, so subsequent
    actions to the same host skip the uname round-trip entirely.
    """

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "exec", "exec_bg", "job_status",
                    "tail_log", "fetch_log",
                    "write_file", "run_script", "safe_exit",
                    "wait_done",
                ],
                "description": "SSH action to perform.",
            },
            "credentials_file": {
                "type": "string",
                "description": (
                    "Path to a local YAML or JSON file containing SSH credentials "
                    "(hostname, username, and optionally key_path / password / port). "
                    "The actual credential values are read by the tool at runtime and "
                    "never passed through the LLM."
                ),
            },
            # exec / exec_bg / run_script
            "command": {
                "type": "string",
                "description": (
                    "[exec / exec_bg] Shell command to run on the remote host. "
                    "For exec: use only for short commands expected to finish within "
                    "the timeout (default 30s); for anything longer use exec_bg or run_script."
                ),
            },
            "workdir": {
                "type": "string",
                "description": "[exec / exec_bg / run_script] Remote working directory (cd before running).",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds for the operation (default varies by action).",
            },
            # exec_bg / run_script
            "job_id": {
                "type": "string",
                "description": "[exec_bg / run_script] Stable identifier for the job (auto-generated if omitted).",
            },
            "log_path": {
                "type": "string",
                "description": "[exec_bg / job_status / tail_log / fetch_log] Remote path for the job log file.",
            },
            "pid_file": {
                "type": "string",
                "description": "[exec_bg / job_status] Remote path to the PID file.",
            },
            # job_status
            "exit_file": {
                "type": "string",
                "description": "[job_status / wait_done] Path to exit-code file (default: <log_path>.exit).",
            },
            "tail_lines": {
                "type": "integer",
                "description": "[job_status / wait_done] Number of log tail lines to return (default 50).",
            },
            "poll_interval": {
                "type": "number",
                "description": "[wait_done] Seconds between in-connection process checks (default 5).",
            },
            # tail_log
            "lines": {
                "type": "integer",
                "description": "[tail_log] Number of tail lines to read (default 100).",
            },
            "pattern": {
                "type": "string",
                "description": "[tail_log] grep -E pattern to filter lines.",
            },
            # fetch_log
            "start_line": {
                "type": "integer",
                "description": "[fetch_log] 1-based start line (default 1).",
            },
            "end_line": {
                "type": "integer",
                "description": "[fetch_log] 1-based end line inclusive (default start+199).",
            },
            # write_file
            "remote_path": {
                "type": "string",
                "description": "[write_file / run_script] Absolute remote path to write to.",
            },
            "content": {
                "type": "string",
                "description": "[write_file] Inline string content to upload.",
            },
            # run_script
            "script_content": {
                "type": "string",
                "description": "[run_script] Script text to upload and execute.",
            },
            "script_name": {
                "type": "string",
                "description": "[run_script] Filename for the remote script (default: 'handq_script.sh').",
            },
            "timeout_hint_seconds": {
                "type": "integer",
                "description": "[run_script] Advisory timeout hint in seconds (informational only).",
            },
        },
        "required": ["action", "credentials_file"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        super().__init__("ssh")

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute(
        self,
        action: str,
        credentials_file: str,
        **kwargs: Any,
    ) -> ToolResult:
        start_time = time.time()
        params: Dict[str, Any] = {
            "action": action,
            "credentials_file": credentials_file,
            **{k: v for k, v in kwargs.items() if k != "credentials_file"},
        }

        _DISPATCH = {
            "exec":        self._action_exec,
            "exec_bg":     self._action_exec_bg,
            "job_status":  self._action_job_status,
            "tail_log":    self._action_tail_log,
            "fetch_log":   self._action_fetch_log,
            "write_file":  self._action_write_file,
            "run_script":  self._action_run_script,
            "safe_exit":   self._action_safe_exit,
            "wait_done":   self._action_wait_done,
        }

        handler = _DISPATCH.get(action)
        if handler is None:
            return ToolResult(
                success=False, output=None,
                error=(
                    f"Unknown SSH action: '{action}'. "
                    f"Valid actions: {', '.join(_DISPATCH)}"
                ),
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        # Load credentials (raises on bad file — caught below)
        try:
            creds = _load_credentials(credentials_file)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=f"Failed to load credentials from '{credentials_file}': {exc}",
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        # Run blocking SSH work in a thread pool so we don't block the event loop
        loop = asyncio.get_event_loop()
        try:
            result: ToolResult = await loop.run_in_executor(
                None, lambda: handler(creds, start_time, params, **kwargs)
            )
        except Exception as exc:
            result = ToolResult(
                success=False, output=None,
                error=f"SSH action '{action}' raised an exception: {exc}",
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start_time,
            )
        return result

    # ── exec ──────────────────────────────────────────────────────────────────

    def _action_exec(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        command: str = kwargs.get("command", "")
        if not command:
            return ToolResult(
                success=False, output=None,
                error="exec requires 'command'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        workdir: Optional[str] = kwargs.get("workdir")
        timeout: float = float(kwargs.get("timeout", 30.0))

        try:
            with _connect(creds) as (client, host_key):
                remote_os, login_shell = _detect_os_and_shell_cached(client, host_key)
                if workdir:
                    if remote_os == "windows":
                        command = f"cd /d {workdir} && {command}"
                    else:
                        command = f"cd {shlex.quote(workdir)} && {command}"
                stdout, stderr, exit_code = _exec_command(client, command, timeout=timeout)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        success = (exit_code == 0)
        out: Dict[str, Any] = {
            "command":    command,
            "stdout":     stdout,
            "stderr":     stderr,
            "exit_code":  exit_code,
            "login_shell": login_shell,
        }
        # Warn when the login shell is not bash so the agent knows to wrap its
        # own commands.  Built-in actions (exec_bg / job_status / safe_exit)
        # already use 'bash -c' internally and are unaffected.
        if login_shell not in ("bash", "unknown", "powershell"):
            out["shell_warning"] = (
                f"Remote login shell is '{login_shell}' (not bash). "
                "Built-in SSH actions (exec_bg, job_status, safe_exit) wrap "
                "commands in 'bash -c' internally and work correctly. "
                "For action='exec' with your own commands, use: "
                "command='bash -c \"your_command_here\"'"
            )
        return ToolResult(
            success=success,
            output=out,
            error=stderr.strip() if not success else None,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
            exit_code=exit_code,
        )

    # ── exec_bg ───────────────────────────────────────────────────────────────

    def _action_exec_bg(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        command: str = kwargs.get("command", "")
        if not command:
            return ToolResult(
                success=False, output=None,
                error="exec_bg requires 'command'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        job_id: str = kwargs.get("job_id") or f"job_{int(time.time())}"
        log_path: str = kwargs.get("log_path") or f"~/handq_jobs/{job_id}.log"
        pid_file: str = kwargs.get("pid_file") or f"{log_path}.pid"
        exit_file: str = f"{log_path}.exit"
        workdir: Optional[str] = kwargs.get("workdir")
        timeout: float = float(kwargs.get("timeout", 30.0))

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)
                if remote_os == "windows":
                    stdout, stderr, exit_code = _exec_bg_windows(
                        client, command, job_id, log_path, pid_file, exit_file,
                        workdir, timeout,
                    )
                elif remote_os == "cygwin":
                    stdout, stderr, exit_code = _exec_bg_cygwin(
                        client, command, job_id, log_path, pid_file, exit_file,
                        workdir, timeout,
                    )
                else:
                    stdout, stderr, exit_code = _exec_bg_linux(
                        client, command, job_id, log_path, pid_file, exit_file,
                        workdir, timeout,
                    )
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        pid = stdout.strip().split()[-1] if stdout.strip() else ""

        # Validate that a numeric PID was actually captured.
        # If bash is not in $PATH on the remote, the nohup wrapper still exits 0
        # but writes nothing to the pid file, leaving pid empty or non-numeric.
        if not pid.isdigit():
            return ToolResult(
                success=False, output=None,
                error=(
                    f"exec_bg: background job launched but PID capture failed "
                    f"(stdout={stdout!r:.200}). "
                    "Likely cause: 'bash' is not in $PATH on the remote host, or "
                    "nohup is unavailable. "
                    "Fallback: use action='write_file' to upload a script, then "
                    "action='exec' with "
                    "command='nohup bash /tmp/script.sh > /tmp/out.log 2>&1 & echo $!' "
                    "to capture the PID manually."
                ),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        return ToolResult(
            success=True,
            output={
                "job_id":    job_id,
                "pid":       pid,
                "pid_file":  pid_file,
                "log_path":  log_path,
                "exit_file": exit_file,
                "command":   command,
                "note": (
                    "Job launched as nohup background process. "
                    "Use action='job_status' with pid_file and log_path to poll. "
                    "Use action='tail_log' to read recent output. "
                    "Use action='safe_exit' to clean up."
                ),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── job_status ────────────────────────────────────────────────────────────

    def _action_job_status(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        pid_file: str = kwargs.get("pid_file", "")
        log_path: str = kwargs.get("log_path", "")
        if not pid_file or not log_path:
            return ToolResult(
                success=False, output=None,
                error="job_status requires 'pid_file' and 'log_path'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        exit_file: str = kwargs.get("exit_file") or f"{log_path}.exit"
        tail_lines: int = int(kwargs.get("tail_lines", 50))
        timeout: float = float(kwargs.get("timeout", 15.0))

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)
                if remote_os == "windows":
                    stdout = _job_status_windows(client, pid_file, log_path, exit_file, tail_lines, timeout)
                else:
                    stdout = _job_status_linux(client, pid_file, log_path, exit_file, tail_lines, timeout)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        # Parse batch output
        sections = stdout.split("---EXIT---")
        alive_section = sections[0].strip() if sections else ""
        rest = sections[1] if len(sections) > 1 else ""

        exit_sections = rest.split("---TAIL---")
        exit_raw = exit_sections[0].strip() if exit_sections else ""
        rest2 = exit_sections[1] if len(exit_sections) > 1 else ""

        wc_sections = rest2.split("---WC---")
        tail_out = wc_sections[0].strip() if wc_sections else ""
        wc_raw = wc_sections[1].strip() if len(wc_sections) > 1 else "0"

        is_alive = "ALIVE" in alive_section

        exit_code: Optional[int] = None
        for token in exit_raw.split():
            if token.strip().lstrip("-").isdigit():
                exit_code = int(token.strip())
                break

        total_lines = 0
        for token in wc_raw.split():
            if token.strip().isdigit():
                total_lines = int(token.strip())
                break

        if is_alive:
            status = "running"
        elif exit_code is not None:
            status = "done"
        else:
            status = "unknown"

        # Error summary: only when the process has exited with a non-zero code.
        # Use word-boundary matching to avoid false positives from lines that
        # merely mention the words (e.g. "no errors found", "failed_count=0").
        import re as _re
        _ERROR_RE = _re.compile(r'\b(ERROR|FAILED|Traceback|Exception)\b', _re.IGNORECASE)
        error_summary: List[str] = []
        if status == "done" and exit_code is not None and exit_code != 0 and tail_out:
            for line in tail_out.splitlines():
                if _ERROR_RE.search(line):
                    error_summary.append(line)

        # Slim output when process is still running: omit log_tail to avoid
        # bloating the agent's observation history across many polling calls.
        # The full log_tail is only included once the process has exited.
        out_dict: Dict[str, Any] = {
            "status":        status,
            "exit_code":     exit_code,
            "process_alive": is_alive,
            "log_path":      log_path,
            "total_lines":   total_lines,
            "tail_lines":    tail_lines,
        }
        if status != "running":
            out_dict["log_tail"] = tail_out
            out_dict["error_summary"] = error_summary

        return ToolResult(
            success=True,
            output=out_dict,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── tail_log ──────────────────────────────────────────────────────────────

    def _action_tail_log(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        log_path: str = kwargs.get("log_path", "")
        if not log_path:
            return ToolResult(
                success=False, output=None,
                error="tail_log requires 'log_path'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        lines: int = int(kwargs.get("lines", 100))
        pattern: Optional[str] = kwargs.get("pattern")
        timeout: float = float(kwargs.get("timeout", 15.0))

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)
                if remote_os == "windows":
                    cmd = _tail_log_cmd_windows(log_path, lines, pattern)
                else:
                    if pattern:
                        cmd = (
                            f"tail -n {lines} {log_path} 2>/dev/null "
                            f"| grep -E {shlex.quote(pattern)} || true; "
                            f"echo '---WC---'; wc -l < {log_path} 2>/dev/null || echo 0"
                        )
                    else:
                        cmd = (
                            f"tail -n {lines} {log_path} 2>/dev/null || echo ''; "
                            f"echo '---WC---'; wc -l < {log_path} 2>/dev/null || echo 0"
                        )
                stdout, _, _ = _exec_command(client, cmd, timeout=timeout)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        parts = stdout.split("---WC---")
        content = parts[0].strip() if parts else ""
        wc_raw = parts[1].strip() if len(parts) > 1 else "0"

        total_lines = 0
        for token in wc_raw.split():
            if token.strip().isdigit():
                total_lines = int(token.strip())
                break

        return ToolResult(
            success=True,
            output={
                "log_path":    log_path,
                "lines":       lines,
                "pattern":     pattern,
                "total_lines": total_lines,
                "content":     content,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── fetch_log ─────────────────────────────────────────────────────────────

    def _action_fetch_log(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        log_path: str = kwargs.get("log_path", "")
        if not log_path:
            return ToolResult(
                success=False, output=None,
                error="fetch_log requires 'log_path'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        start_line: int = int(kwargs.get("start_line", 1))
        end_line: int = int(kwargs.get("end_line", start_line + 199))
        timeout: float = float(kwargs.get("timeout", 15.0))

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)
                if remote_os == "windows":
                    # PowerShell: Get-Content, select line range
                    cmd = (
                        f"powershell -Command \""
                        f"$lines = Get-Content -Path '{log_path}' -ErrorAction SilentlyContinue; "
                        f"if ($lines) {{ $lines[{start_line - 1}..{end_line - 1}] -join \\\"`n\\\" }} else {{ '' }}"
                        f"\""
                    )
                else:
                    cmd = f"sed -n '{start_line},{end_line}p' {log_path} 2>/dev/null || echo ''"
                stdout, _, _ = _exec_command(client, cmd, timeout=timeout)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        return ToolResult(
            success=True,
            output={
                "log_path":   log_path,
                "start_line": start_line,
                "end_line":   end_line,
                "content":    stdout.strip(),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── write_file ────────────────────────────────────────────────────────────

    def _action_write_file(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        remote_path: str = kwargs.get("remote_path", "")
        content: Optional[str] = kwargs.get("content")

        if not remote_path:
            return ToolResult(
                success=False, output=None,
                error="write_file requires 'remote_path'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )
        if content is None:
            return ToolResult(
                success=False, output=None,
                error="write_file requires 'content'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        data: bytes = content.encode("utf-8")

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)
                sftp = client.open_sftp()
                try:
                    remote_dir = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "."
                    if remote_dir and remote_dir not in (".", "~"):
                        if remote_os == "windows":
                            _exec_command(
                                client,
                                f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{remote_dir}' | Out-Null\"",
                                timeout=15.0,
                            )
                        else:
                            _exec_command(client, f"mkdir -p {shlex.quote(remote_dir)}", timeout=15.0)
                    with sftp.open(remote_path, "wb") as rf:
                        rf.write(data)
                finally:
                    sftp.close()
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        return ToolResult(
            success=True,
            output={
                "remote_path":   remote_path,
                "bytes_written": len(data),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── run_script ────────────────────────────────────────────────────────────

    def _action_run_script(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        script_content: str = kwargs.get("script_content", "")
        if not script_content:
            return ToolResult(
                success=False, output=None,
                error="run_script requires 'script_content'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        script_name: str = kwargs.get("script_name") or "handq_script.sh"
        job_id: str = kwargs.get("job_id") or f"job_{int(time.time())}"
        workdir: Optional[str] = kwargs.get("workdir")
        timeout_hint: Optional[int] = kwargs.get("timeout_hint_seconds")

        data: bytes = script_content.encode("utf-8")

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)

                if remote_os == "windows":
                    # Pure Windows (no Cygwin): use PowerShell Start-Process
                    win_script_name = (
                        script_name.replace(".sh", ".ps1")
                        if script_name.endswith(".sh") else script_name
                    )
                    job_dir = f"~/handq_jobs/{job_id}"
                    script_remote_path = f"{job_dir}/{win_script_name}"
                    log_path = f"{job_dir}/{job_id}.log"
                    pid_file = f"{log_path}.pid"
                    exit_file = f"{log_path}.exit"

                    # (a) mkdir
                    _exec_command(
                        client,
                        f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{job_dir}' | Out-Null\"",
                        timeout=15.0,
                    )
                    # (b) upload script via SFTP
                    sftp = client.open_sftp()
                    try:
                        with sftp.open(script_remote_path, "wb") as rf:
                            rf.write(data)
                    finally:
                        sftp.close()

                    # (c) launch via Start-Process (background, stdout→log, exit→exit_file)
                    stdout, _, _ = _exec_bg_windows(
                        client,
                        f"powershell -ExecutionPolicy Bypass -File '{script_remote_path}'",
                        job_id, log_path, pid_file, exit_file, workdir, 30.0,
                    )

                else:
                    # Linux or Cygwin: both support bash/nohup/mkdir natively.
                    # Cygwin must NOT use Start-Process — it cannot inherit Cygwin
                    # POSIX file descriptors, causing empty log files.
                    script_remote_path = f"~/handq_jobs/{job_id}/{script_name}"
                    log_path = f"~/handq_jobs/{job_id}/{job_id}.log"
                    pid_file = f"{log_path}.pid"
                    exit_file = f"{log_path}.exit"

                    # (a) mkdir + upload
                    _exec_command(client, f"mkdir -p ~/handq_jobs/{job_id}", timeout=15.0)
                    sftp = client.open_sftp()
                    try:
                        with sftp.open(script_remote_path, "wb") as rf:
                            rf.write(data)
                    finally:
                        sftp.close()

                    # (b) chmod +x
                    _exec_command(client, f"chmod +x {script_remote_path}", timeout=15.0)

                    # (c) exec_bg
                    stdout, _, _ = _exec_bg_linux(
                        client,
                        f"bash {script_remote_path}",
                        job_id, log_path, pid_file, exit_file, workdir, 30.0,
                    )

        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        pid = stdout.strip().split()[-1] if stdout.strip() else ""

        return ToolResult(
            success=True,
            output={
                "job_id":               job_id,
                "pid":                  pid,
                "pid_file":             pid_file,
                "log_path":             log_path,
                "exit_file":            exit_file,
                "script_remote_path":   script_remote_path,
                "timeout_hint_seconds": timeout_hint,
                "note": (
                    "Script uploaded, made executable, and launched as nohup background process. "
                    "Use action='job_status' with pid_file and log_path to poll progress. "
                    "Use action='tail_log' to read recent output. "
                    "Use action='safe_exit' to clean up when done."
                ),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── safe_exit ─────────────────────────────────────────────────────────────

    def _action_safe_exit(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        timeout: float = float(kwargs.get("timeout", 15.0))

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)
                if remote_os == "windows":
                    kill_all = (
                        "powershell -Command \""
                        "$pidFiles = Get-ChildItem -Path ~/handq_jobs -Recurse -Filter '*.pid' -ErrorAction SilentlyContinue; "
                        "foreach ($pf in $pidFiles) { "
                        "  $pid = (Get-Content $pf.FullName -ErrorAction SilentlyContinue); "
                        "  if ($pid) { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue }; "
                        "  Remove-Item $pf.FullName -Force -ErrorAction SilentlyContinue "
                        "}; "
                        "$remaining = (Get-ChildItem -Path ~/handq_jobs -Recurse -Filter '*.pid' -ErrorAction SilentlyContinue | Measure-Object).Count; "
                        "Write-Output $remaining"
                        "\""
                    )
                else:
                    kill_body = (
                        "find ~/handq_jobs -name '*.pid' 2>/dev/null "
                        "| while read pf; do "
                        "  _PID=$(cat \"$pf\" 2>/dev/null); "
                        "  if [ -n \"$_PID\" ] && kill -0 \"$_PID\" 2>/dev/null; then "
                        "    kill -s TERM -- -\"$_PID\" 2>/dev/null || kill -s TERM \"$_PID\" 2>/dev/null; "
                        "  fi; "
                        "  rm -f \"$pf\"; "
                        "done; "
                        "find ~/handq_jobs -name '*.pid' 2>/dev/null | wc -l"
                    )
                    # Wrap in bash to bypass tcsh/csh login shells.
                    kill_all = f"bash -c {shlex.quote(kill_body)}"
                stdout, _, _ = _exec_command(client, kill_all, timeout=timeout)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        remaining = stdout.strip().split()[-1] if stdout.strip() else "0"

        return ToolResult(
            success=True,
            output={
                "remaining_pid_files": int(remaining) if remaining.isdigit() else -1,
                "message": "All tracked nohup jobs terminated and pid files removed.",
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )

    # ── wait_done ─────────────────────────────────────────────────────────────

    def _action_wait_done(
        self,
        creds: Dict[str, Any],
        start_time: float,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        """
        Block inside a single SSH connection until a background job finishes,
        then return the result.

        This eliminates the need for the agent to repeatedly call job_status
        (each of which opens a new SSH connection).  A single bash loop runs
        on the remote host; Python simply waits for it to return.

        The remote loop looks like:
            while kill -0 $PID && [ $(date +%s) -lt $DEADLINE ]; do
                sleep <poll_interval>
            done
            # emit exit code, log tail, line count

        The SSH-level keepalive (set_keepalive=30) prevents NAT/firewalls from
        dropping the idle connection while the loop is sleeping.
        """
        pid_file: str = kwargs.get("pid_file", "")
        log_path: str = kwargs.get("log_path", "")
        if not pid_file or not log_path:
            return ToolResult(
                success=False, output=None,
                error="wait_done requires 'pid_file' and 'log_path'",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        exit_file: str    = kwargs.get("exit_file") or f"{log_path}.exit"
        timeout: float    = float(kwargs.get("timeout", 300.0))
        poll_interval: float = float(kwargs.get("poll_interval", 5.0))
        tail_lines: int   = int(kwargs.get("tail_lines", 50))

        # Give paramiko a bit more headroom than the bash timeout so it doesn't
        # cut off the final output read.
        exec_timeout = timeout + 15.0

        try:
            with _connect(creds) as (client, host_key):
                remote_os = _detect_os_cached(client, host_key)

                if remote_os == "windows":
                    # PowerShell polling loop
                    poll_secs = int(poll_interval)
                    deadline_ps = int(timeout)
                    batch = (
                        f"powershell -Command \""
                        f"$pid = (Get-Content '{pid_file}' -ErrorAction SilentlyContinue); "
                        f"$deadline = (Get-Date).AddSeconds({deadline_ps}); "
                        f"while ($pid -and (Get-Process -Id $pid -ErrorAction SilentlyContinue) "
                        f"       -and (Get-Date) -lt $deadline) "
                        f"  {{ Start-Sleep -Seconds {poll_secs} }}; "
                        f"if ($pid -and (Get-Process -Id $pid -ErrorAction SilentlyContinue)) "
                        f"  {{ Write-Output 'TIMEOUT' }} else {{ Write-Output 'DONE' }}; "
                        f"Write-Output '---EXIT---'; "
                        f"Get-Content '{exit_file}' -ErrorAction SilentlyContinue; "
                        f"Write-Output '---TAIL---'; "
                        f"Get-Content '{log_path}' -ErrorAction SilentlyContinue | Select-Object -Last {tail_lines}; "
                        f"Write-Output '---WC---'; "
                        f"(Get-Content '{log_path}' -ErrorAction SilentlyContinue | Measure-Object -Line).Lines"
                        f"\""
                    )
                else:
                    # Linux / Cygwin: bash polling loop
                    poll_secs = int(poll_interval)
                    deadline_secs = int(timeout)
                    batch_body = (
                        f"_PID=$(cat {pid_file} 2>/dev/null || echo ''); "
                        f"_DEADLINE=$(( $(date +%s) + {deadline_secs} )); "
                        f"while [ -n \"$_PID\" ] && kill -0 \"$_PID\" 2>/dev/null "
                        f"       && [ $(date +%s) -lt $_DEADLINE ]; do "
                        f"  sleep {poll_secs}; "
                        f"done; "
                        f"if [ -n \"$_PID\" ] && kill -0 \"$_PID\" 2>/dev/null; then "
                        f"  echo TIMEOUT; "
                        f"else "
                        f"  echo DONE; "
                        f"fi; "
                        f"echo '---EXIT---'; cat {exit_file} 2>/dev/null || echo ''; "
                        f"echo '---TAIL---'; tail -n {tail_lines} {log_path} 2>/dev/null || echo ''; "
                        f"echo '---WC---'; wc -l < {log_path} 2>/dev/null || echo 0"
                    )
                    batch = f"bash -c {shlex.quote(batch_body)}"

                stdout, _, _ = _exec_command(client, batch, timeout=exec_timeout)

        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=str(exc),
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start_time,
            )

        # Parse output (same format as job_status)
        timed_out = stdout.strip().startswith("TIMEOUT")

        sections = stdout.split("---EXIT---")
        rest = sections[1] if len(sections) > 1 else ""
        exit_sections = rest.split("---TAIL---")
        exit_raw = exit_sections[0].strip()
        rest2 = exit_sections[1] if len(exit_sections) > 1 else ""
        wc_sections = rest2.split("---WC---")
        tail_out = wc_sections[0].strip()
        wc_raw = wc_sections[1].strip() if len(wc_sections) > 1 else "0"

        exit_code: Optional[int] = None
        for token in exit_raw.split():
            if token.strip().lstrip("-").isdigit():
                exit_code = int(token.strip())
                break

        total_lines = 0
        for token in wc_raw.split():
            if token.strip().isdigit():
                total_lines = int(token.strip())
                break

        import re as _re
        _ERROR_RE = _re.compile(r'\b(ERROR|FAILED|Traceback|Exception)\b', _re.IGNORECASE)
        error_summary: List[str] = []
        if exit_code is not None and exit_code != 0 and tail_out:
            for line in tail_out.splitlines():
                if _ERROR_RE.search(line):
                    error_summary.append(line)

        status = "timeout" if timed_out else "done"

        return ToolResult(
            success=not timed_out,
            output={
                "status":        status,
                "exit_code":     exit_code,
                "log_path":      log_path,
                "total_lines":   total_lines,
                "tail_lines":    tail_lines,
                "log_tail":      tail_out,
                "error_summary": error_summary,
                "waited_seconds": round(time.time() - start_time, 1),
            },
            error="wait_done timed out — job is still running" if timed_out else None,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start_time,
        )
