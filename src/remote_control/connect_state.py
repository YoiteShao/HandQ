"""``~/HandQ/connect_state.json`` — the last-picked Connect panel role.

Small on-purpose: everything else the v6 design doc §8 asks for
(``servers[]`` with ``sessions[]`` and ``since_seq``) is already carried by
``remote_targets.json`` (see :mod:`registry`). The ONE datum that file
doesn't cover is which role the user was in when they last closed HandQ —
"was I a Server, or a Client?" — so :class:`ConnectState` isolates just that.

Why we care about the role at all: on next startup the Connect panel wants
to open on the LAST role's dashboard rather than the neutral role-selection
page, so a user who was serving five minutes ago doesn't have to click
through the tile again. It is a UX hint, not a functional signal — v6
explicitly mints a fresh token every time you click "As Server", so we do
NOT auto-start the server just because ``role == "server"`` was persisted.

File shape::

    {
        "role": "client" | "server" | null,
        "updated_at": <unix timestamp>
    }

0600 on Linux (best-effort — matches remote_targets.json), plain file on
Windows (ACL on the user profile already restricts it).
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


VALID_ROLES = frozenset({"client", "server"})


def _default_path() -> Path:
    return Path(os.path.expanduser("~")) / "HandQ" / "connect_state.json"


@dataclass
class ConnectState:
    """One-line role tracker for the v6 Connect panel.

    Thread-safety: writes are best-effort atomic (temp file + os.replace);
    reads are lockless because the file is single-writer (this bridge) and a
    torn read from another process would only lose "did I last serve?" UX
    hint, never any real state.
    """

    role: Optional[str] = None
    updated_at: float = 0.0
    #: Path override for tests. Never used in production.
    path: Optional[Path] = None

    def _resolved_path(self) -> Path:
        return self.path or _default_path()

    # ── I/O ─────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ConnectState":
        p = path or _default_path()
        if not p.exists():
            return cls(path=path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "connect_state: could not read %s; treating as no prior role",
                p, exc_info=True,
            )
            return cls(path=path)
        role = data.get("role")
        if role not in VALID_ROLES:
            role = None
        updated_at = float(data.get("updated_at") or 0.0)
        return cls(role=role, updated_at=updated_at, path=path)

    def save(self) -> None:
        p = self._resolved_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "role": self.role if self.role in VALID_ROLES else None,
                "updated_at": self.updated_at or time.time(),
            }
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, p)
            # Best effort — a no-op on Windows (ACL already restricts) and
            # cheap+correct on Linux.
            try:
                p.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
        except Exception:
            logger.warning(
                "connect_state: could not persist role=%r to %s",
                self.role, p, exc_info=True,
            )

    # ── Mutators ────────────────────────────────────────────────────────

    def set_role(self, role: Optional[str]) -> None:
        """Set the current role and persist. ``None`` clears it (e.g. after
        Exit Server / Exit Client)."""
        if role is not None and role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        self.role = role
        self.updated_at = time.time()
        self.save()
