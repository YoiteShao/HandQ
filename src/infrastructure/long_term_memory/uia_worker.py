"""UIA worker — PowerShell subprocess providing structured accessibility text.

The single OCR-channel observation pipeline is brittle on GUI apps where
text on screen is rendered as bitmap or pixel-aligned glyphs. Windows UI
Automation (UIA) exposes a *structured* alternative: control trees with
``Name`` / ``AutomationId`` / ``Value`` fields, addressable URLs in
browsers, file paths in Explorer / VS Code, current prompt in terminals.

This worker spawns a long-lived ``powershell.exe`` child running
``scripts/uia_query.ps1`` and communicates over its stdin/stdout. JSON
one-object-per-line. The protocol:

    >>> {"req": "query", "hwnd": <int>, "depth_limit": 4}
    <<< {"ax_text": "...", "parsed_json": {"url": "..."},
         "top_window_titles": ["...", ...]}

Lifecycle
---------
- Lazy spawn on first query. Stays alive for the bridge's lifetime.
- If the PowerShell child crashes (or times out 3 consecutive times),
  we kill + respawn (up to 5 attempts with 1s backoff). Failure to start
  permanently means UIA returns ``None`` from ``query()`` and the caller
  (PersonalityMonitor) silently degrades to OCR-only.

PersonalityMonitor integration
------------------------------
Each snapshot capture optionally calls ``uia_worker.query(hwnd=...)`` to
populate ``obs_snapshots.ax_text`` and ``obs_snapshots.parsed_json``.
The call is wrapped in ``asyncio.wait_for(..., timeout=2.0)`` so a slow
UIA tree never blocks the snapshot path; a timeout returns ``None``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

_logger = logging.getLogger("handq.ltm.uia_worker")

_SCRIPT_PATH = Path(__file__).parent / "scripts" / "uia_query.ps1"

QUERY_TIMEOUT_SECONDS: float = 2.0
RESPAWN_MAX_ATTEMPTS: int = 5
RESPAWN_BACKOFF_SECONDS: float = 1.0


class UIAWorker:
    """Long-lived PowerShell subprocess for UI Automation queries.

    Single-flight: only one in-flight query at a time. Use a global
    asyncio.Lock to serialize concurrent callers — UIA queries are
    relatively cheap (<200ms typical) so this won't dominate even under
    light contention.
    """

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._respawn_count = 0
        self._disabled = False
        # Only initialize on Windows
        if os.name != "nt":
            _logger.info("UIA worker disabled (non-Windows host)")
            self._disabled = True

    async def query(
        self, hwnd: int, *, depth_limit: int = 4,
    ) -> Optional[dict]:
        """Ask the worker for ax_text + parsed_json + top_window_titles.

        Returns ``None`` on any failure (no subprocess, timeout, parse
        error). The caller treats None as "UIA unavailable" and proceeds
        without structured signals.
        """
        if self._disabled or not hwnd:
            return None
        async with self._lock:
            for _attempt in range(2):
                if not await self._ensure_running():
                    return None
                try:
                    result = await asyncio.wait_for(
                        self._send_recv({
                            "req": "query",
                            "hwnd": int(hwnd),
                            "depth_limit": int(depth_limit),
                        }),
                        timeout=QUERY_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    _logger.warning("UIA query timeout; restarting worker")
                    await self._terminate()
                    continue
                except Exception:
                    _logger.exception("UIA query failed")
                    await self._terminate()
                    continue
                # A healthy round-trip proves the worker is alive, so clear
                # the consecutive-spawn-failure tally. Without this the count
                # only ever rises (every _spawn increments it) and a few
                # transient early failures over the bridge's lifetime would
                # permanently self-disable an otherwise-working worker.
                if result is not None:
                    self._respawn_count = 0
                return result
        return None

    async def _ensure_running(self) -> bool:
        if self._proc and self._proc.returncode is None:
            return True
        if self._respawn_count >= RESPAWN_MAX_ATTEMPTS:
            if not self._disabled:
                _logger.error(
                    "UIA worker disabled after %d failed spawns",
                    RESPAWN_MAX_ATTEMPTS,
                )
                self._disabled = True
            return False
        try:
            return await self._spawn()
        except Exception:
            _logger.exception("UIA worker spawn failed")
            self._respawn_count += 1
            await asyncio.sleep(RESPAWN_BACKOFF_SECONDS)
            return False

    async def _spawn(self) -> bool:
        if not _SCRIPT_PATH.exists():
            _logger.error("UIA script not found at %s", _SCRIPT_PATH)
            self._disabled = True
            return False
        # Find a usable PowerShell executable. Prefer pwsh.exe (PowerShell 7),
        # fall back to powershell.exe.
        ps_cmd = "pwsh.exe" if _which("pwsh.exe") else "powershell.exe"
        self._proc = await asyncio.create_subprocess_exec(
            ps_cmd, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(_SCRIPT_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._respawn_count += 1
        # Wait for "ready" signal
        try:
            ready_line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=5.0,
            )
            if not ready_line:
                _logger.warning("UIA worker did not signal ready")
                await self._terminate()
                return False
            try:
                ready = json.loads(ready_line.decode("utf-8", errors="ignore").strip())
                if not (isinstance(ready, dict) and ready.get("ready")):
                    _logger.warning("UIA worker bad ready signal: %r", ready_line)
                    await self._terminate()
                    return False
            except (json.JSONDecodeError, TypeError, ValueError):
                _logger.warning("UIA worker non-JSON ready: %r", ready_line)
                await self._terminate()
                return False
            _logger.info("UIA worker spawned (pid=%s)", self._proc.pid)
            return True
        except asyncio.TimeoutError:
            _logger.warning("UIA worker spawn-ready timeout")
            await self._terminate()
            return False

    async def _send_recv(self, msg: dict) -> Optional[dict]:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            return None
        payload = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        await self._proc.stdin.drain()
        line = await self._proc.stdout.readline()
        if not line:
            return None
        try:
            data = json.loads(line.decode("utf-8", errors="ignore").strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("error"):
            _logger.debug("UIA worker error response: %s", data["error"])
            return None
        return data

    async def _terminate(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.returncode is None:
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass
        self._proc = None

    async def shutdown(self) -> None:
        await self._terminate()


def _which(cmd: str) -> Optional[str]:
    """Cross-process find of cmd on PATH (Win32 only here)."""
    import shutil
    return shutil.which(cmd)


# Singleton — UIA worker is one per process.
_GLOBAL: Optional[UIAWorker] = None


def get_uia_worker() -> UIAWorker:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = UIAWorker()
    return _GLOBAL
