"""On-disk record of paired remote targets.

Follows the storage split ``infrastructure/ssh_setup.py`` already established for
SSH credentials: non-secret metadata in a chmod-600 file under the user's HandQ
root, the secret in the OS keyring. Here the secret is the bearer token, and the
keyring service name is ``handq-remote-<target_id>`` — the same shape as
``handq-<safe_hostname>`` used for SSH passwords, so both show up together in
Credential Manager / Secret Service.

The fallback path matters more than usual: a headless Linux controlled machine may have
no D-Bus Secret Service, which is exactly why ``keyrings.alt`` is already a
dependency. When even that fails the token is written into the metadata file,
which is chmod 600 and no worse than what ``state.json`` on the Linux side
already holds. That degradation is logged rather than silent.

Also tracked per target: the sessions we have driven, with the ``seq`` each one
was last caught up to. That is what lets a controller close a tab, come back
tomorrow, and re-attach to a still-running remote session without losing the
transcript between then and now.

**These session records are a credential cache, not an inventory.** A record
exists so we can present ``(session_id, capability, since_seq)`` and re-attach;
it is NOT evidence the session still exists, because the controlled machine can destroy
one while we are offline and unable to hear about it. Existence is answered by
the server (``protocol.LIST_SESSIONS`` / ``auth_ok``'s session list) and the
records are reconciled against that answer — see :meth:`reconcile_sessions` and
``hub.refresh_sessions``. The inverse rule matters just as much: a record is
never dropped as a *heuristic*. An earlier version pruned records for sessions
that had only ever been chat, on the theory that there was no work to resume.
That orphaned a live ``FlowControllerV2`` plus workspace on the other machine,
reachable by nothing, holding one of its session slots — and, because the flag
it keyed on was learned from a replayable one-shot event, it also deleted real
tasks that had been re-adopted. Records now go away for exactly one reason: the
session is gone.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type

from .address import ControlAddress

logger = logging.getLogger("handq.remote_control.registry")

_KEYRING_SERVICE_PREFIX = "handq-remote"
_KEYRING_USERNAME = "token"


def _known_fields(cls: Type[Any], data: Any) -> Dict[str, Any]:
    """Keep only the keys ``cls`` actually declares.

    Guards the load path against a file written by a *newer* HandQ: an unknown
    key used to reach the dataclass constructor as an unexpected keyword, raise
    ``TypeError``, and take the whole surrounding target record down with it in
    the caller's ``except`` — meaning a downgrade did not just lose the new
    field, it silently lost the machine's pairing and forced the user to walk
    back to the other machine. Dropping the key we cannot represent is the
    correct amount of damage.
    """
    if not isinstance(data, dict):
        return {}
    allowed = {f.name for f in dataclass_fields(cls)}
    return {k: v for k, v in data.items() if k in allowed}


def _user_handq_root() -> Path:
    """``%USERPROFILE%\\HandQ`` / ``~/HandQ`` — same root stdio_bridge uses for
    session history, so everything user-visible lives in one place."""
    return Path(os.path.expanduser("~")) / "HandQ"


def default_registry_path() -> Path:
    return _user_handq_root() / "remote_targets.json"


@dataclass
class RemoteSessionRecord:
    """A remote session this controller has driven."""

    session_id: str
    capability: str
    title: str = ""
    since_seq: int = 0
    #: Last value the controlled side reported for this session's ``is_task``. Purely
    #: for the panel's chip badge, and only a cache — the live value comes from
    #: the session descriptor whenever we are connected. It is persisted so a
    #: chip for an offline target can still say what it was, rather than
    #: silently downgrading every session to "chat" while the link is down.
    is_task: bool = False
    updated_at: float = field(default_factory=time.time)


@dataclass
class RemoteTarget:
    """A paired remote machine."""

    target_id: str
    name: str
    host: str
    port: int
    platform: str = ""
    #: Only populated when the keyring is unavailable; otherwise the token lives
    #: in the keyring and this stays empty.
    token_fallback: str = ""
    last_connected: float = 0.0
    sessions: List[RemoteSessionRecord] = field(default_factory=list)
    #: The ``user@host`` used to SSH-bootstrap this target, when it was paired
    #: that way (Linux auto-pair). Not a secret — the SSH password lives in the
    #: keyring, this is just the address — and stored so a deferred upgrade can
    #: re-run the bootstrap without asking the operator to re-type it. Empty for
    #: manually-pasted (Windows) pairings, which never carry an upgrade anyway.
    ssh_target: str = ""
    #: A newer package is on the share but wasn't installed because the machine
    #: was busy. ``{}`` normally, else ``{from, to, reason}``. Persisted so the
    #: panel keeps showing the "upgrade available" banner across restarts until
    #: it is actually applied (which clears it). Kept a plain dict, not a nested
    #: dataclass, so the ``_known_fields`` load guard can pass it through
    #: untouched and an older HandQ downgrade just ignores an unknown key.
    upgrade_pending: Dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def display(self) -> str:
        return f"{self.name} ({self.endpoint})" if self.name else self.endpoint

    def to_public_dict(self) -> Dict[str, Any]:
        """Shape sent to the renderer. Never includes the token."""
        return {
            "target_id": self.target_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "platform": self.platform,
            "last_connected": self.last_connected,
            "ssh_target": self.ssh_target,
            "upgrade_pending": dict(self.upgrade_pending or {}),
            "sessions": [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "since_seq": s.since_seq,
                    "is_task": s.is_task,
                    "updated_at": s.updated_at,
                }
                for s in self.sessions
            ],
        }

    def find_session(self, session_id: str) -> Optional[RemoteSessionRecord]:
        for record in self.sessions:
            if record.session_id == session_id:
                return record
        return None


class RemoteTargetRegistry:
    """Load / mutate / persist the paired-target list.

    Every mutator writes through immediately. The alternative (flush on exit) is
    the kind of thing that works until the process is killed, and losing a
    pairing means the operator has to walk back to the other machine.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_registry_path()
        self._lock = threading.Lock()
        self._targets: Dict[str, RemoteTarget] = {}
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "remote_control: could not read %s; starting with no targets",
                self.path, exc_info=True,
            )
            return
        for item in raw.get("targets") or []:
            try:
                sessions = [
                    RemoteSessionRecord(**_known_fields(RemoteSessionRecord, s))
                    for s in (item.pop("sessions", None) or [])
                ]
                target = RemoteTarget(
                    **_known_fields(RemoteTarget, item), sessions=sessions
                )
                self._targets[target.target_id] = target
            except Exception:
                logger.warning(
                    "remote_control: skipping malformed target record", exc_info=True
                )

    def _save_locked(self) -> None:
        payload = {
            "version": 1,
            "targets": [
                {**asdict(t), "sessions": [asdict(s) for s in t.sessions]}
                for t in self._targets.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        # Best effort — a no-op on Windows, where the ACL on the user profile
        # already restricts this, but correct and cheap on Linux.
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    # ── Token storage ────────────────────────────────────────────────────────

    @staticmethod
    def _keyring_service(target_id: str) -> str:
        return f"{_KEYRING_SERVICE_PREFIX}-{target_id}"

    def _store_token(self, target: RemoteTarget, token: str) -> None:
        try:
            import keyring

            keyring.set_password(
                self._keyring_service(target.target_id), _KEYRING_USERNAME, token
            )
            target.token_fallback = ""
            return
        except Exception:
            logger.warning(
                "remote_control: OS keyring unavailable; storing the token for %s "
                "in %s instead (file is chmod 600)",
                target.display(), self.path,
                exc_info=True,
            )
        target.token_fallback = token

    def token_for(self, target: RemoteTarget) -> str:
        if target.token_fallback:
            return target.token_fallback
        try:
            import keyring

            return keyring.get_password(
                self._keyring_service(target.target_id), _KEYRING_USERNAME
            ) or ""
        except Exception:
            logger.warning(
                "remote_control: could not read the token for %s from the keyring",
                target.display(), exc_info=True,
            )
            return ""

    def _forget_token(self, target: RemoteTarget) -> None:
        try:
            import keyring

            keyring.delete_password(
                self._keyring_service(target.target_id), _KEYRING_USERNAME
            )
        except Exception:
            # Nothing stored, or no keyring. Either way there is nothing to do.
            pass

    # ── Queries ──────────────────────────────────────────────────────────────

    def list_targets(self) -> List[RemoteTarget]:
        with self._lock:
            return sorted(
                self._targets.values(), key=lambda t: t.last_connected, reverse=True
            )

    def get(self, target_id: str) -> Optional[RemoteTarget]:
        with self._lock:
            return self._targets.get(target_id)

    def find_by_endpoint(self, host: str, port: int) -> Optional[RemoteTarget]:
        with self._lock:
            for target in self._targets.values():
                if target.host == host and target.port == int(port):
                    return target
        return None

    def address_for(self, target_id: str) -> Optional[ControlAddress]:
        target = self.get(target_id)
        if target is None:
            return None
        token = self.token_for(target)
        if not token:
            return None
        return ControlAddress(
            host=target.host, port=target.port, token=token, name=target.name
        )

    # ── Mutators ─────────────────────────────────────────────────────────────

    def pair(self, address: ControlAddress, name: str = "") -> RemoteTarget:
        """Record a pairing string. Re-pairing the same endpoint updates in place.

        Updating in place rather than appending is what makes re-pairing after a
        controlled-side restart (new dynamic port, new token) a no-friction operation:
        the operator pastes the fresh address and the session records attached to
        that target are preserved. Records for a *different* endpoint are left
        alone, so two machines never merge.
        """
        with self._lock:
            existing = None
            for target in self._targets.values():
                if target.host == address.host and target.port == address.port:
                    existing = target
                    break
            if existing is None:
                # Match on name too, so a target whose dynamic port moved is
                # updated instead of duplicated.
                label = (name or address.name).strip()
                if label:
                    for target in self._targets.values():
                        if target.name == label:
                            existing = target
                            break

            if existing is not None:
                existing.host = address.host
                existing.port = address.port
                if name or address.name:
                    existing.name = (name or address.name).strip()
                target = existing
            else:
                target = RemoteTarget(
                    target_id=secrets.token_hex(6),
                    name=(name or address.name).strip() or address.endpoint,
                    host=address.host,
                    port=address.port,
                )
                self._targets[target.target_id] = target

            self._store_token(target, address.token)
            self._save_locked()
            return target

    def forget(self, target_id: str) -> bool:
        with self._lock:
            target = self._targets.pop(target_id, None)
            if target is None:
                return False
            self._forget_token(target)
            self._save_locked()
            return True

    def mark_connected(self, target_id: str, platform: str = "") -> None:
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return
            target.last_connected = time.time()
            if platform:
                target.platform = platform
            self._save_locked()

    def set_upgrade_pending(
        self, target_id: str, upgrade: Optional[Dict[str, Any]],
        *, ssh_target: Optional[str] = None,
    ) -> None:
        """Record (or clear) a deferred upgrade for a target.

        Called on every Linux re-pair: set to ``{from, to, reason}`` when a newer
        package was found but the machine was too busy to install it, or to
        ``{}`` to clear a previously-pending upgrade that has since been applied.
        Always written (even when clearing) so a stale banner cannot outlive the
        upgrade it referred to.

        ``ssh_target`` (when given non-empty) is stored alongside, so the panel's
        "upgrade now" can re-run the SSH bootstrap without re-prompting for the
        address. Passing "" leaves the stored value untouched.
        """
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return
            target.upgrade_pending = dict(upgrade or {})
            if ssh_target:
                target.ssh_target = ssh_target
            self._save_locked()

    def remember_session(
        self,
        target_id: str,
        session_id: str,
        capability: str,
        title: str = "",
        since_seq: int = 0,
        is_task: Optional[bool] = None,
    ) -> None:
        """Record (or refresh) what we know about one remote session.

        Called on every seq checkpoint and on tab close. Unconditional by
        design: see the module docstring on why records are never pruned
        heuristically. ``title`` and ``is_task`` are only *upgraded* — an empty
        title or a ``None`` flag from a caller that does not know leaves the
        stored value alone, so a checkpoint mid-session cannot blank out a name
        the open call already established.
        """
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return
            record = target.find_session(session_id)
            if record is None:
                record = RemoteSessionRecord(
                    session_id=session_id, capability=capability, title=title
                )
                target.sessions.append(record)
            record.capability = capability or record.capability
            if title:
                record.title = title
            if is_task is not None:
                record.is_task = bool(is_task)
            record.since_seq = max(record.since_seq, int(since_seq))
            record.updated_at = time.time()
            self._save_locked()

    def update_session_seq(self, target_id: str, session_id: str, since_seq: int) -> None:
        """Checkpoint how far a session has been consumed.

        Called on a cadence rather than per event — a disk write per streamed
        reply token would be absurd — so a crash can cost a few seconds of
        replay position. The consequence of an under-reported seq is a short
        duplicate replay on reattach, not loss, which is the right direction to
        be wrong in.
        """
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return
            record = target.find_session(session_id)
            if record is None:
                return
            if int(since_seq) <= record.since_seq:
                return
            record.since_seq = int(since_seq)
            record.updated_at = time.time()
            self._save_locked()

    def forget_session(self, target_id: str, session_id: str) -> None:
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return
            target.sessions = [
                s for s in target.sessions if s.session_id != session_id
            ]
            self._save_locked()

    def reconcile_sessions(
        self, target_id: str, live_session_ids: Set[str]
    ) -> List[str]:
        """Drop records for sessions the controlled side no longer has. Returns the ids
        dropped.

        The only sanctioned way a record disappears without us asking for it.
        ``live_session_ids`` must come from a *successful* query of the server
        (``auth_ok``'s list or ``protocol.LIST_SESSIONS``) — never from a
        timeout, a disconnect, or an empty default, because "the server told me
        it has nothing" and "I could not reach the server" produce the same
        empty set and only the first one licenses deleting anything. Callers
        enforce that by not calling this at all on failure; a session on a
        machine we cannot currently reach keeps its record and its chip.
        """
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return []
            keep, dropped = [], []
            for record in target.sessions:
                if record.session_id in live_session_ids:
                    keep.append(record)
                else:
                    dropped.append(record.session_id)
            if not dropped:
                return []
            target.sessions = keep
            self._save_locked()
        logger.info(
            "remote_control: dropped %d stale session record(s) for %s: %s",
            len(dropped), target_id, ", ".join(dropped),
        )
        return dropped
