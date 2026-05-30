"""HandQ git post-commit hook.

This file is installed into ``<repo>/.git/hooks/post-commit`` (either
manually or via Settings → Personalization → "Learn from commits in"
on Save, which diff-syncs the configured repo list against existing
hooks). It runs in the git
hook environment — short-lived, no HandQ runtime — and writes a fresh
candidate row into ``%USERPROFILE%/HandQ/personality/memory.db`` for
the bridge's DreamWorker to triage on its next cycle.

Why self-contained
------------------
The bridge owns stdio and is busy serving Electron — a git hook can't
push messages into it. Instead we open SQLite directly from the hook
process. WAL + ``BEGIN IMMEDIATE`` + ``busy_timeout=10000`` make
concurrent writes safe: if the bridge is mid-write, our INSERT waits
up to 10 seconds for the lock; the hook exits cleanly either way.

We also keep imports minimal (stdlib only) so the hook runs even when
HandQ's full dependency tree isn't on PATH (frozen builds, fresh
checkouts before pip install, etc.).

Usage
-----
The hook is invoked by git as ``post-commit`` with no arguments. It
reads HEAD info via ``git`` CLI, formats a candidate identical to
``candidates.submit_post_commit``'s shape, and exits.

Failure handling
----------------
If anything goes wrong (memory.db missing, schema mismatch, git CLI
not on PATH, anything else), the hook logs to stderr and exits 0.
``post-commit`` exit codes are advisory; we never fail the commit
because of LTM problems.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path


# ── Paths ────────────────────────────────────────────────────────────


def _user_handq_root() -> Path:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


def _memory_db_path() -> Path:
    # Mirrors src/infrastructure/long_term_memory/_constants.py
    # PERSONALITY_DATA_DIR = "personality" and bridge_main._run_with_long_term_memory
    # which constructs db_path = personality_root / "memory.db".
    return _user_handq_root() / "personality" / "memory.db"


# ── Git helpers ──────────────────────────────────────────────────────


def _git(*args: str) -> str:
    """Run ``git <args>`` in the current directory; return stdout text.

    Returns "" on any error so the rest of the hook can still produce a
    minimal candidate.
    """
    try:
        out = subprocess.check_output(
            ("git",) + args,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            text=True,
        )
        return out.strip()
    except Exception:
        return ""


def _read_commit() -> dict:
    sha = _git("rev-parse", "HEAD") or "unknown"
    msg = _git("log", "-1", "--pretty=%B") or ""
    author_email = _git("log", "-1", "--pretty=%ae") or ""
    config_email = _git("config", "user.email") or ""
    diff_stat = _git("diff-tree", "--stat", "--no-color", "HEAD") or ""
    # author_is_self heuristic: same email between the commit and the
    # current git config. Picks up squash commits that already had
    # someone else's authorship preserved.
    author_is_self = bool(author_email and config_email and
                          author_email == config_email)
    return {
        "sha": sha,
        "msg": msg,
        "author_email": author_email,
        "author_is_self": author_is_self,
        "diff_stat": diff_stat,
    }


# ── Candidate insert (mirrors candidates.submit_post_commit) ─────────


def _insert_candidate(commit: dict) -> None:
    db = _memory_db_path()
    if not db.exists():
        # Bridge has never run on this machine — no LTM yet. Silent.
        return

    tag = "[SELF]" if commit["author_is_self"] else "[OTHER]"
    raw_text = (
        f"# Git commit {commit['sha'][:8]}\n"
        f"{tag} {commit['msg']}\n\n"
        f"# Diff stat\n{commit['diff_stat'][:500]}"
    )
    cid = str(uuid.uuid4())
    now = int(time.time())
    metadata = json.dumps({
        "author_is_self": commit["author_is_self"],
        "author_email": commit["author_email"],
    }, ensure_ascii=False)

    # Open the SAME database the bridge uses. WAL + busy_timeout means
    # concurrent INSERT with an active bridge serializes safely; we wait
    # up to 10s for any held write lock before giving up.
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO memory_candidates "
            "(id, source, source_ref, raw_text, hint, metadata, status, "
            " retry_count, created_at, updated_at) "
            "VALUES (?, 'post_commit', ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (
                cid, commit["sha"], raw_text,
                "Commit just landed. Look for project conventions or user habits.",
                metadata, now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── Entry point ──────────────────────────────────────────────────────


def main() -> int:
    try:
        commit = _read_commit()
        if not commit["sha"] or commit["sha"] == "unknown":
            print("handq post-commit: no HEAD; skipping", file=sys.stderr)
            return 0
        _insert_candidate(commit)
    except Exception as exc:
        # Never fail the commit because of LTM. Log and exit 0.
        print(f"handq post-commit: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
