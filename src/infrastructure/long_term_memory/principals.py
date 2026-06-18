"""Principals populator — feed the entity graph (ent_principals / aliases /
sightings) from existing HandQ data sources.

Three sources are scanned at LongTermMemory init + on demand:

1. **SSH host registry** — ``~/.ssh/handq_<host>.yaml`` files describe each
   machine the user SSHs to. Each becomes a ``Principal(kind=machine,
   host_kind='ssh')``.
2. **Git author** — ``git config user.email`` identifies the user.
   Their own email becomes a ``Principal(kind=person)``. Future post-commit
   hooks add other authors as ``person`` principals.
3. **Project working directory** — the bridge's working_directory is
   tagged as a ``Principal(kind=project)``. Future hooks can add
   project_root entries from session contexts.

This module is intentionally minimal — it produces a baseline graph at
boot. The SemanticExtractor (Phase 5) can extend it opportunistically
when it extracts new entities from observation events.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from .models import HostKind, PrincipalKind

_logger = logging.getLogger("handq.ltm.principals")


async def populate_baseline(store, *, working_directory: Optional[str] = None) -> int:
    """Run a one-shot baseline population. Returns number of principals added."""
    added = 0
    added += await _populate_from_ssh_registry(store)
    added += await _populate_self_person(store)
    if working_directory:
        added += await _populate_project(store, working_directory)
    _logger.info("principals baseline populated: %d entries", added)
    return added


async def _populate_from_ssh_registry(store) -> int:
    """Scan ~/.ssh/handq_*.yaml — each is one machine principal."""
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.is_dir():
        return 0
    count = 0
    try:
        files = list(ssh_dir.glob("handq_*.yaml"))
    except Exception:
        return 0
    for fpath in files:
        try:
            data = await asyncio.to_thread(_read_yaml, fpath)
        except Exception:
            _logger.debug("read SSH yaml failed: %s", fpath, exc_info=True)
            continue
        if not data:
            continue
        host = data.get("hostname") or ""
        user = data.get("username") or ""
        if not host:
            continue
        try:
            pid = await store.upsert_principal(
                kind=PrincipalKind.MACHINE.value,
                canonical_name=host,
                display_name=host,
                host_kind=HostKind.SSH.value,
                os="linux",  # SSH targets in HandQ are typically Linux dev hosts
                description=f"SSH host (user={user})" if user else "SSH host",
            )
            if user:
                await store.add_principal_alias(pid, f"{user}@{host}")
            count += 1
        except Exception:
            _logger.exception("upsert SSH principal failed: %s", host)
    return count


async def _populate_self_person(store) -> int:
    """Add the user as a person principal from git config user.email."""
    try:
        email = await asyncio.to_thread(_git_user_email)
    except Exception:
        return 0
    if not email:
        return 0
    canonical = email.split("@")[0]
    try:
        pid = await store.upsert_principal(
            kind=PrincipalKind.PERSON.value,
            canonical_name=canonical,
            display_name=canonical,
            email=email,
            description="Local user (from git config user.email)",
        )
        await store.add_principal_alias(pid, email)
        return 1
    except Exception:
        _logger.exception("upsert self principal failed: %s", email)
        return 0


async def _populate_project(store, working_directory: str) -> int:
    """Add the bridge's working_directory as a project principal."""
    if not working_directory:
        return 0
    wd = Path(working_directory)
    name = wd.name or str(wd)
    try:
        await store.upsert_principal(
            kind=PrincipalKind.PROJECT.value,
            canonical_name=name,
            display_name=name,
            project_root=str(wd.resolve()) if wd.exists() else str(wd),
            description="Bridge working directory at boot",
        )
        return 1
    except Exception:
        _logger.exception("upsert project principal failed: %s", working_directory)
        return 0


def _read_yaml(path: Path) -> Optional[dict]:
    try:
        import yaml
    except ImportError:
        return _read_yaml_dumb(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_yaml_dumb(path: Path) -> Optional[dict]:
    """Minimal YAML reader for `key: value` files (no nesting)."""
    try:
        out: dict = {}
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ":" in ln and not ln.lstrip().startswith("#"):
                k, _, v = ln.partition(":")
                out[k.strip()] = v.strip().strip("'\"")
        return out
    except Exception:
        return None


def _git_user_email() -> Optional[str]:
    """Return ``git config user.email`` or None."""
    try:
        res = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, timeout=2.0,
        )
        if res.returncode == 0:
            out = (res.stdout or "").strip()
            return out or None
    except Exception:
        pass
    return None


# Lightweight @-mention parser for SemanticExtractor entity extraction.
# The (?<!\w) lookbehind rejects an @ that immediately follows a word char —
# that is an email local-part boundary (bob@example.com), not a handle — so
# domains never surface as spurious person principals.
_MENTION_RE = re.compile(r"(?<!\w)@([\w.\-]{2,64})")


def extract_mentions(text: str) -> List[str]:
    """Return all @-prefixed handles in *text*.

    Used by future SemanticExtractor passes to opportunistically add
    person principals when user messages mention colleagues by handle.
    """
    if not text:
        return []
    return list(set(_MENTION_RE.findall(text)))
