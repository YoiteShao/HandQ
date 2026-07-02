"""End-to-end demo drive: spawn owner + member subprocesses, script commands
into the owner, capture the transcript, and print it back so a reviewer can
see /burn / /state / /reset behavior end-to-end in one go.

This is a manual runbook, not a pytest fixture — run it directly:
    python scripts/chatroom_demo_drive.py

It exists because the interactive REPL is hard to demonstrate in a written
review; automated stdout capture makes the behavior reproducible.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 stdout so downstream prints don't blow up on Windows cp1252 when
# rendering child-process transcripts that contain non-ASCII (guardrail
# notices, presence events, etc.).
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO = REPO_ROOT / "chatroom_demo.py"


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _print_section(label: str, lines: list[str]) -> None:
    print(f"\n===== {label} =====")
    for line in lines:
        stripped = _strip_ansi(line).rstrip()
        if stripped:
            print(f"  {stripped}")


def run() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    owner = subprocess.Popen(
        [sys.executable, "-u", str(DEMO),
         "--name", "owner", "host",
         "--room", "demo", "--bind", "127.0.0.1", "--port", "48622",
         "--no-announce"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT), env=env, text=True, encoding="utf-8",
        errors="replace",
    )
    time.sleep(1.2)  # let owner bind

    member = subprocess.Popen(
        [sys.executable, "-u", str(DEMO),
         "--name", "member", "join",
         "--host", "127.0.0.1", "--port", "48622", "--room", "demo"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT), env=env, text=True, encoding="utf-8",
        errors="replace",
    )
    time.sleep(1.0)  # let member connect

    def _tell(proc: subprocess.Popen, cmd: str) -> None:
        assert proc.stdin is not None
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()

    try:
        # Scene 1: normal human chat, both directions.
        _tell(owner, "hi everyone (owner speaking as USER)")
        time.sleep(0.3)
        _tell(member, "hi back (member USER)")
        time.sleep(0.3)

        # Scene 2: directed TASK from owner to member.
        _tell(owner, "/task @member run the tests")
        time.sleep(0.3)

        # Scene 3: BURN — force R2 pair cooldown by mixing owner and member handq.
        _tell(owner, "/burn 3")
        time.sleep(0.2)
        _tell(member, "/handq chime-in from member agent")
        time.sleep(0.6)

        # Scene 4: subsequent handq is now blocked.
        _tell(owner, "/handq trying again after cooldown")
        time.sleep(0.4)

        # Scene 5: /state shows the current counters + active cooldown.
        _tell(owner, "/state")
        time.sleep(0.3)

        # Scene 6: /reset clears, next handq goes through.
        _tell(owner, "/reset")
        time.sleep(0.2)
        _tell(owner, "/handq speaking freely after reset")
        time.sleep(0.4)

        # Scene 7: /state again to see counters restart.
        _tell(owner, "/state")
        time.sleep(0.3)

    finally:
        for p in (owner, member):
            try:
                _tell(p, "/quit")
            except (BrokenPipeError, OSError):
                pass
        time.sleep(0.5)
        for p in (owner, member):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    p.kill()

    owner_out = owner.stdout.read() if owner.stdout else ""
    member_out = member.stdout.read() if member.stdout else ""

    _print_section("OWNER transcript", owner_out.splitlines())
    _print_section("MEMBER transcript", member_out.splitlines())
    return 0


if __name__ == "__main__":
    sys.exit(run())
