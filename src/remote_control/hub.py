"""Controlling side — one object owning every paired target, connection and bridge.

``stdio_bridge`` talks to this and nothing else on the controlling side. It holds the
:class:`~src.remote_control.registry.RemoteTargetRegistry`, at most one
:class:`~src.remote_control.client.RemoteControlClient` per target (connections
are shared across that target's sessions), and the
:class:`~src.remote_control.session_bridge.RemoteSessionBridge` for each local
session tab.

The seq checkpoint is throttled here rather than in the bridge: every replayed or
live event advances a session's seq, and writing the registry file per event would
mean a disk write per streamed token. Falling behind costs a short duplicate
replay after a crash, never lost content, which is the correct direction to be
imprecise in.

**One rule governs every registry write in this file**: a session record is
created or refreshed unconditionally, and removed only when the controlled side has
told us the session is gone (``session_closed``, or absence from a successful
``refresh_sessions`` query). There is no heuristic. The previous design kept a
record only for sessions the Coordinator had classified as a real task, which
broke in both directions — it deleted the record of a finished task the moment
its re-adopted tab was closed a second time (the flag was learned from a
one-shot event that a later ``since_seq`` never replays), and for chat-only
sessions it abandoned a live ``FlowControllerV2`` and workspace on the other
machine with nothing left able to reach or close it. Both controlled hosts treat every
session identically, so the controller does too.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

from .address import AddressError, ControlAddress, parse_address
from .client import RemoteControlClient, RemoteControlError
from .registry import RemoteTarget, RemoteTargetRegistry
from .session_bridge import RemoteSessionBridge

logger = logging.getLogger("handq.remote_control.hub")

#: Minimum seconds between registry writes for the same session's seq.
SEQ_CHECKPOINT_INTERVAL = 5.0

#: How long ``refresh_sessions`` waits for a server's session list. Short: it
#: runs on the panel's refresh path, and a stale chip list is a much smaller
#: problem than a panel that takes ten seconds to open.
SESSION_REFRESH_TIMEOUT = 4.0


class RemoteControlHub:
    """Owns the controlling-side state for remote sessions."""

    def __init__(
        self,
        *,
        emit: Callable[[Dict[str, Any], Optional[str]], None],
        ui_factory: Callable[[str], Any],
        client_name: str = "",
        registry: Optional[RemoteTargetRegistry] = None,
        on_bridge_released: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str, str, str], None]] = None,
    ) -> None:
        #: ``_emit`` from stdio_bridge — ``(envelope, session_id)``.
        self._emit = emit
        #: ``_get_or_create_ui`` from stdio_bridge — local sid → ``_StdioUI``.
        self._ui_factory = ui_factory
        self._client_name = client_name
        self.registry = registry or RemoteTargetRegistry()
        #: Optional Connect-panel log sink — ``(source, message, level)``. Used
        #: for connect-time config sync so "why did it (not) restart?" is
        #: answerable without SSHing in. None → those lines are dropped.
        self._on_log = on_log
        #: Called with a local sid whose bridge this hub tore down on its own
        #: initiative (i.e. not via the tab-close path that already knows).
        #: stdio_bridge uses it to drop ``_flows[sid]``; without it that slot
        #: keeps a destroyed bridge, ``_ensure_any_flow`` short-circuits on it,
        #: and the tab accepts typing that goes nowhere.
        self._on_bridge_released = on_bridge_released

        self._clients: Dict[str, RemoteControlClient] = {}
        #: Targets with a connect attempt in flight. Exists because
        #: ``_clients[tid]`` is deliberately not written until ``connect()``
        #: returns (see :meth:`ensure_client`), which left ``list_targets`` with
        #: no way to tell "never connected" from "connecting right now" — both
        #: reported ``offline``. Boot-time auto-reconnect makes that window
        #: seconds long per machine, so the panel showed a disconnected card,
        #: offered "Forget" as the only action, and then flipped to connected
        #: under the operator's hands.
        self._connecting: Set[str] = set()
        self._bridges: Dict[str, RemoteSessionBridge] = {}
        #: local sid → pending binding, set by ``remote_bind`` before the first
        #: ``request`` so ``_ensure_flow`` knows to build a bridge.
        self._bindings: Dict[str, Dict[str, Any]] = {}
        self._seq_last_write: Dict[str, float] = {}
        #: target_id → newest session descriptors from that server, as returned
        #: by ``RemoteSession.describe()``. Merged into :meth:`list_targets` so
        #: the panel can show live state (running / parked confirmation) rather
        #: than only what we last persisted.
        self._live_sessions: Dict[str, List[Dict[str, Any]]] = {}


    # ── Pairing ──────────────────────────────────────────────────────────────

    def pair(self, pairing_string: str, name: str = "") -> RemoteTarget:
        """Record a pairing string. Raises :class:`AddressError` if unparseable."""
        address = parse_address(pairing_string)
        target = self.registry.pair(address, name=name)
        logger.info("remote_control: paired target %s", target.display())
        # A re-pair may carry a new port/token for a target we hold a live (and
        # now stale) connection to. Drop it so the next use reconnects with the
        # fresh address instead of failing against the old one.
        self._drop_client(target.target_id)
        return target

    async def probe(self, pairing_string: str) -> Dict[str, Any]:
        """Connect, authenticate, disconnect. Used by the pairing dialog so the
        operator learns immediately whether the address works, rather than at
        first use."""
        address = parse_address(pairing_string)
        client = RemoteControlClient(
            address, client_name=self._client_name, auto_reconnect=False
        )
        try:
            await client.connect()
            return {
                "ok": True,
                "server_name": client.server_name,
                "platform": client.server_platform,
                "endpoint": address.endpoint,
                "sessions": client.remote_sessions,
            }
        finally:
            await client.close()

    def forget(self, target_id: str) -> bool:
        self._drop_client(target_id)
        return self.registry.forget(target_id)

    async def pair_linux_over_ssh(
        self,
        *,
        ssh_target: str = "",
        credentials_file: str = "",
        name: str = "",
        install: bool = True,
        interaction_manager: Optional[Any] = None,
        force: bool = False,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> RemoteTarget:
        """Fetch a Linux host's control address over SSH, then pair with it.

        The one place SSH still touches the interactive path, and only to move an
        address: install/start ``handq_linux`` if needed, read the port and token
        it published into ``state.json``, record the pairing. Every subsequent
        interaction is a direct connection.

        ``force=False`` (default): if bringing the direct channel up requires
        restarting the daemon to upgrade it while a task or remote session is
        running there, the upgrade is DEFERRED rather than interrupting the work.
        The pairing still succeeds against the current (working) build, and the
        deferred upgrade is recorded on the target (``upgrade_pending``) so the
        panel can offer it once the machine drains. Pass ``force=True`` only
        after the operator has been shown that and chosen to interrupt.

        ``on_log`` receives operator-facing upgrade-decision lines (share scan,
        versions, deploy / defer), for the Connect panel's log.
        """
        from .linux_bootstrap import resolve_linux_address

        result = await resolve_linux_address(
            ssh_target=ssh_target,
            credentials_file=credentials_file,
            interaction_manager=interaction_manager,
            install=install,
            name=name,
            force=force,
            on_log=on_log,
        )
        address = result.address
        target = self.registry.pair(address, name=name or address.name)
        target.platform = target.platform or "linux"
        # Record (or clear) a deferred upgrade so list_targets can surface a
        # banner, and remember the SSH target so the panel's "upgrade now" can
        # re-bootstrap without re-prompting. Set even when the upgrade dict is
        # empty so a previously-pending upgrade that has since been applied
        # stops showing.
        self.registry.set_upgrade_pending(
            target.target_id, result.upgrade_pending, ssh_target=ssh_target,
        )
        target = self.registry.get(target.target_id) or target
        logger.info(
            "remote_control: paired Linux target %s over SSH%s", target.display(),
            " (upgrade pending)" if result.upgrade_pending else "",
        )
        self._drop_client(target.target_id)
        return target

    def list_targets(self) -> List[Dict[str, Any]]:
        """Every paired machine, with connection state and session chips.

        Each session entry is the persisted record (which is what carries the
        capability, so it is what determines whether a chip can be acted on)
        merged with the controlled side's own newest descriptor for that id when we
        have one. The merge is what lets a chip say "still running" or "waiting
        on you" instead of only "this existed when I last closed its tab".
        """
        out = []
        for target in self.registry.list_targets():
            item = target.to_public_dict()
            client = self._clients.get(target.target_id)
            if client is not None and client.connected:
                item["state"] = "connected"
            elif client is not None:
                item["state"] = "connecting"
            elif target.target_id in self._connecting:
                # A first connect is in flight. Checked after ``_clients``
                # because a reconnecting client is the more specific answer,
                # and before the offline fallback because "we are trying right
                # now" is not the same claim as "there is nothing here" — the
                # panel renders a destructive Forget button for the latter.
                item["state"] = "connecting"
            else:
                item["state"] = "offline"
            # Legacy field kept for backward compat with old panel
            item["connected"] = bool(client and client.connected)
            item["server_name"] = client.server_name if client else ""
            item["sessions"] = self._merge_session_views(
                target.target_id, item.get("sessions") or []
            )
            out.append(item)
        return out

    def _merge_session_views(
        self, target_id: str, records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Fold the controlled side's live descriptors into our persisted records.

        Newest activity first, so a panel that shows every session (there is no
        pruning any more) still puts the one the user cares about at the front.
        Sessions the server reports but we hold no capability for are appended
        as ``controllable: False`` — they are real and worth showing (they are
        occupying a slot on that machine, and one of them may be sitting on a
        parked confirmation), but we cannot honestly offer to open or close
        them, so the panel disables those buttons rather than failing on click.
        """
        live = {
            str(d.get("session_id") or ""): d
            for d in (self._live_sessions.get(target_id) or [])
            if isinstance(d, dict) and d.get("session_id")
        }
        merged: List[Dict[str, Any]] = []
        for record in records:
            sid = str(record.get("session_id") or "")
            entry = dict(record)
            entry["controllable"] = True
            descriptor = live.pop(sid, None)
            if descriptor is not None:
                entry["alive"] = True
                entry["state"] = descriptor.get("state") or ""
                entry["attached"] = bool(descriptor.get("attached"))
                entry["pending_confirms"] = int(
                    descriptor.get("pending_confirms") or 0
                )
                entry["is_task"] = bool(descriptor.get("is_task"))
                entry["last_activity_at"] = float(
                    descriptor.get("last_activity_at") or 0.0
                )
                # The server's title is authoritative — it was set from the goal
                # at open time and we may never have learned it (a record first
                # written by a seq checkpoint has no title of its own).
                if descriptor.get("title"):
                    entry["title"] = descriptor["title"]
            else:
                # No live view: either we are offline, or the refresh failed.
                # Deliberately NOT "alive: False" — that would be a claim we
                # cannot support, and the panel would grey out a chip for a
                # session that is running perfectly well behind a dropped link.
                entry["alive"] = None
                entry["pending_confirms"] = 0
                entry["attached"] = False
            merged.append(entry)

        for sid, descriptor in live.items():
            merged.append({
                "session_id": sid,
                "title": descriptor.get("title") or "",
                "since_seq": int(descriptor.get("cur_seq") or 0),
                "is_task": bool(descriptor.get("is_task")),
                "updated_at": float(descriptor.get("last_activity_at") or 0.0),
                "last_activity_at": float(descriptor.get("last_activity_at") or 0.0),
                "state": descriptor.get("state") or "",
                "attached": bool(descriptor.get("attached")),
                "pending_confirms": int(descriptor.get("pending_confirms") or 0),
                "alive": True,
                "controllable": False,
            })

        merged.sort(
            key=lambda e: max(
                float(e.get("last_activity_at") or 0.0),
                float(e.get("updated_at") or 0.0),
            ),
            reverse=True,
        )
        return merged

    async def refresh_sessions(self, target_id: str) -> List[Dict[str, Any]]:
        """Re-ask one server what sessions it has, then reconcile our records.

        The only place a record is dropped without the server volunteering a
        ``session_closed``. Silent on failure by design: a timeout or a dropped
        link produces the same empty list as "this server hosts nothing", and
        reconciling against that would delete every record we hold for a machine
        that is merely unreachable. So we reconcile only on a genuine answer,
        and leave the last known chips in place otherwise.
        """
        client = self._clients.get(target_id)
        if client is None or not client.connected:
            return []
        try:
            live = await client.list_remote_sessions(
                timeout=SESSION_REFRESH_TIMEOUT
            )
        except (RemoteControlError, asyncio.TimeoutError) as exc:
            logger.info(
                "remote_control: session refresh for %s failed: %s", target_id, exc
            )
            return []
        except Exception:
            logger.warning(
                "remote_control: session refresh for %s raised",
                target_id, exc_info=True,
            )
            return []

        self._live_sessions[target_id] = live
        live_ids: Set[str] = {
            str(d.get("session_id") or "")
            for d in live
            if isinstance(d, dict) and d.get("session_id")
        }
        # Never reconcile away a session a local tab is actively driving. Such a
        # session must exist (we are exchanging events with it); its absence
        # from this list would mean the list is the thing that is wrong.
        for bridge in self._bridges.values():
            if bridge._target_id == target_id and bridge.remote_session_id:
                live_ids.add(bridge.remote_session_id)
        dropped = self.registry.reconcile_sessions(target_id, live_ids)
        # Refresh the title/is_task cache for the survivors, so an offline chip
        # later still shows a real name. Only for sessions we already hold a
        # record (and therefore a capability) for — remember_session must not
        # mint a record with an empty capability, which would be a chip we can
        # neither open nor close.
        for descriptor in live:
            if not isinstance(descriptor, dict):
                continue
            sid = str(descriptor.get("session_id") or "")
            if not sid or not self._has_record(target_id, sid):
                continue
            self.registry.remember_session(
                target_id,
                sid,
                "",  # never overwrite a stored capability with nothing
                title=str(descriptor.get("title") or ""),
                is_task=bool(descriptor.get("is_task")),
            )
        if dropped:
            self._broadcast_targets(target_id, "sessions_reconciled")
        # A target with a deferred upgrade whose sessions have now all drained is
        # safe to upgrade. Announce it (once) so the panel can nudge — but never
        # act automatically: the upgrade bounces the daemon and re-mints the
        # port+token, which must be an operator's explicit choice, not something
        # that happens while they aren't looking.
        target = self.registry.get(target_id)
        if (target is not None and target.upgrade_pending
                and not live and not self._driving_any(target_id)):
            self._broadcast_targets(target_id, "upgrade_ready")
        return live

    def _driving_any(self, target_id: str) -> bool:
        """Is a local tab currently driving any session on this target."""
        return any(
            b._target_id == target_id and b.remote_session_id
            for b in self._bridges.values()
        )

    def _has_record(self, target_id: str, session_id: str) -> bool:
        target = self.registry.get(target_id)
        return (target.find_session(session_id) is not None) if target else False

    def _broadcast_targets(self, target_id: str, state: str, detail: str = "") -> None:
        """Push the refreshed machine list so the panel updates without polling."""
        self._emit(
            {
                "type": "status",
                "kind": "remote_target_state",
                "target_id": target_id,
                "state": state,
                "detail": detail,
                "targets": self.list_targets(),
            },
            None,
        )

    # ── Connections ──────────────────────────────────────────────────────────

    async def ensure_client(self, target_id: str) -> RemoteControlClient:
        """Return a connected client for ``target_id``, connecting if needed."""
        client = self._clients.get(target_id)
        if client is not None and client.connected:
            return client
        if client is not None:
            # Exists but disconnected: its supervisor is already retrying. Give
            # it no special treatment — surface the state and let the caller
            # decide, rather than opening a second connection alongside it.
            raise RemoteControlError(
                f"Connection to {client.address.display()} has not recovered yet, automatically reconnecting"
            )

        address = self.registry.address_for(target_id)
        if address is None:
            target = self.registry.get(target_id)
            if target is None:
                raise RemoteControlError(f"Remote target {target_id} not found")
            raise RemoteControlError(
                f"Pairing token for {target.display()} was lost (could not be read from the system credential store) — please re-pair"
            )

        # Before connecting to a Linux target, re-assert this controller's llm.*
        # config over it (credentials + model pool). The controller's yaml is the
        # authoritative source for a headless daemon; doing it on every connect is
        # what makes fixing a key here heal the remote without waiting for a
        # deploy. A restart it triggers can mint a new port/token, so pick up the
        # fresh address before building the client. Never fatal: a sync that fails
        # (SSH down, etc.) must not block a connect that might still work against
        # the existing config.
        address = await self._maybe_sync_linux_config(target_id, address)

        # Announce the attempt before the socket work starts, so the panel shows
        # "connecting" for the whole window rather than "not connected" followed
        # by a sudden flip. The connect itself can take
        # ``CONNECT_TIMEOUT_SEC + AUTH_TIMEOUT_SEC``, which is long enough for an
        # operator to act on the wrong state.
        self._connecting.add(target_id)
        self._broadcast_targets(target_id, "connecting")
        client = RemoteControlClient(address, client_name=self._client_name)
        client.on_orphan_session_closed = (
            lambda sid, reason, tid=target_id: self._on_orphan_session_closed(
                tid, sid, reason
            )
        )
        try:
            await client.connect()
        except BaseException:
            self._connecting.discard(target_id)
            self._broadcast_targets(target_id, "offline")
            raise
        self._connecting.discard(target_id)
        # Registered AFTER connect() on purpose. The listener's whole job is to
        # broadcast a target list, and until this client is in ``_clients`` that
        # list would report the machine we just connected to as "offline" — the
        # first and most confusing moment to be wrong. The connect itself is
        # announced explicitly below, once the state is consistent.
        self._clients[target_id] = client
        client.on_state_change(
            lambda state, detail, tid=target_id: self._on_client_state(tid, state, detail)
        )
        self.registry.mark_connected(target_id, client.server_platform)
        # ``auth_ok`` already carried this server's live session list; use it to
        # reconcile immediately rather than waiting for the panel's next refresh,
        # so a session its operator killed while we were away stops being
        # offered the moment we are back in touch.
        self._adopt_live_sessions(target_id, client.remote_sessions)
        self._broadcast_targets(target_id, "connected", client.server_name)
        return client

    async def push_skills_to(
        self, target_id: str, skill_names: List[str]
    ) -> List[Dict[str, Any]]:
        """Mirror each named local user-authored skill onto ``target_id``.

        Reads files from the local user Skill root via
        ``SkillRegistry.export_skill_files``, which itself rejects a bundled
        or unknown name with ``ValueError`` — that is the re-check that keeps
        a stale or hand-crafted IPC payload from pushing a bundled skill even
        though the picker UI already excludes those (bundled skills are
        shipped/immutable and identical across machines already).
        """
        from ..infrastructure.skills import SkillRegistry

        registry = SkillRegistry.get()
        payload: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []
        for name in skill_names:
            try:
                files = registry.export_skill_files(name)
            except Exception as exc:
                results.append({"name": name, "ok": False, "error": str(exc)})
                continue
            payload.append({"name": name, "files": files})

        if payload:
            client = await self.ensure_client(target_id)
            pushed = await client.push_skills(payload)
            results.extend(pushed)
        return results

    async def list_skills_on(self, target_id: str) -> List[Dict[str, Any]]:
        """What skills ``target_id`` already has — bundled and
        user/uploaded alike. Lets ``RemoteControlError`` propagate: this is
        only reachable from an already-open picker for a connected target,
        and the renderer treats it as best-effort (see ``connect-panel.js``).
        """
        client = await self.ensure_client(target_id)
        return await client.list_remote_skills()

    async def _maybe_sync_linux_config(
        self, target_id: str, address: "ControlAddress"
    ) -> "ControlAddress":
        """Push llm.* to a Linux target before connecting; return the address to
        use (possibly a fresh one if the daemon was restarted).

        Only for Linux targets paired over SSH (we need an ``ssh_target`` to reach
        the config). Any failure is logged and swallowed — the connect proceeds
        against the existing config, because a machine whose key was already fine
        should still be reachable when the controller can't SSH in to re-assert it.
        """
        target = self.registry.get(target_id)
        if target is None:
            return address
        platform = str(getattr(target, "platform", "") or "").lower()
        ssh_target = str(getattr(target, "ssh_target", "") or "")
        if not platform.startswith("linux") or not ssh_target:
            return address

        def _log(msg: str) -> None:
            if self._on_log is not None:
                try:
                    self._on_log("config-sync", msg, "info")
                except Exception:
                    pass

        try:
            from .linux_bootstrap import sync_linux_llm_config

            result = await sync_linux_llm_config(
                ssh_target=ssh_target,
                name=target.name,
                on_log=_log,
            )
        except Exception as exc:
            logger.info("remote_control: llm config sync to %s failed: %s",
                        target_id, exc)
            if self._on_log is not None:
                try:
                    self._on_log(
                        "config-sync",
                        f"Failed to sync llm config to {target.display()} (will connect with existing config): {exc}",
                        "warn",
                    )
                except Exception:
                    pass
            return address

        if result.address is not None:
            # The sync learned a live port/token — either because it bounced
            # the daemon itself, or because it simply asked a live daemon what
            # it's actually listening on right now (see linux_bootstrap's
            # no-diff+alive branch). Either way the registry's cached address
            # may be stale (dynamic port moved for a reason unrelated to this
            # sync — crash, manual restart, host reboot) and must not be
            # trusted over what the daemon just reported. Persist it so both
            # this connect and the saved record use the fresh address.
            self.registry.pair(result.address, name=target.name)
            return result.address
        if result.changed and not result.restarted and result.pending:
            # Deferred: the write happened but the busy daemon still runs the old
            # config. Surface it like a pending upgrade so the panel can say so.
            self.registry.set_upgrade_pending(
                target_id, dict(result.pending), ssh_target=ssh_target,
            )
        return address

    def _adopt_live_sessions(
        self, target_id: str, descriptors: Any
    ) -> None:
        """Reconcile against a session list we already have in hand.

        Split from :meth:`refresh_sessions` because ``auth_ok`` delivers the
        same payload without a round trip, and because this path is reached
        from a state-change callback that must not await.
        """
        if not isinstance(descriptors, list):
            return
        self._live_sessions[target_id] = descriptors
        live_ids: Set[str] = {
            str(d.get("session_id") or "")
            for d in descriptors
            if isinstance(d, dict) and d.get("session_id")
        }
        for bridge in self._bridges.values():
            if bridge._target_id == target_id and bridge.remote_session_id:
                live_ids.add(bridge.remote_session_id)
        self.registry.reconcile_sessions(target_id, live_ids)

    def _on_orphan_session_closed(
        self, target_id: str, session_id: str, reason: str
    ) -> None:
        """The controlled side destroyed a session no local tab was driving.

        Its record is the only trace left, and it now points at nothing, so drop
        it and push the refreshed list. Reached for exactly the case the panel
        used to get wrong: you closed the tab yesterday, the other operator
        closed the session from their dashboard today, and the chip stayed until
        you clicked it and got an error.
        """
        logger.info(
            "remote_control: %s on %s closed remotely (%s); dropping its record",
            session_id, target_id, reason or "no reason given",
        )
        self.registry.forget_session(target_id, session_id)
        self._live_sessions[target_id] = [
            d for d in (self._live_sessions.get(target_id) or [])
            if not (isinstance(d, dict) and d.get("session_id") == session_id)
        ]
        self._broadcast_targets(target_id, "session_gone", reason)

    async def close_client(self, target_id: str) -> None:
        """Passive disconnect: drop the socket, keep everything else.

        The non-destructive half of the pair (:meth:`release_target` is the other).
        Nothing is released, no session is destroyed, the pairing stays, and the
        controlled side parks its sessions as it would for any network drop — so
        re-connecting picks up exactly where we left off. Exists so "stop talking
        to that machine for now" and "I am done with that machine" are two
        different operations instead of one word that quietly means the
        destructive one.

        Also the way to hand a controlled machine back: it serves one controller at a
        time, so holding an idle connection is what keeps somebody else out.
        """
        client = self._clients.pop(target_id, None)
        if client is not None:
            await client.close()
        self._live_sessions.pop(target_id, None)
        self._broadcast_targets(target_id, "offline")

    async def release_target(self, target_id: str) -> Dict[str, Any]:
        """End the serving relationship with one server — the ONE destructive action.

        A controlled machine is a server: it does not lose state because we stopped
        visiting. Closing a tab, closing HandQ, losing the network — all of that is
        absence, handled by parking (:meth:`close_client`). This method is the only
        thing in the client that deliberately destroys work on another machine, so
        every caller must have confirmed with the operator first.

        What it means is genuinely platform-dependent, and the two are not papered
        over:

        * **Linux** — the daemon exists only to be driven, so ``release_server``
          destroys its sessions and the process exits. Its port and token die with
          it, which makes the pairing record worthless, so the pairing is
          forgotten and the panel card goes away. Re-use means re-pairing over SSH.
        * **Anything else (a Windows controlled machine)** — that machine is also somebody's
          workstation and its owner is the one who put it into server mode. We
          destroy the sessions we were driving and disconnect; it keeps listening,
          its address and token stay valid, so the pairing is KEPT and the card
          stays connectable.

        Returns ``{"confirmed", "forgot", "warning"}`` rather than raising on an
        unacknowledged release. The ack is genuinely unreliable *by design* on the
        platform where this matters most: ``disconnect_client()`` writes the
        acknowledging frame and then the host hook exits the Linux process, so the
        socket can die while ``release()`` is still waiting on it
        (``client._on_connection_lost`` fails the waiter). Gating the forget on
        that ack meant the expected case — a daemon that did exactly as asked —
        left a card behind whose only remaining action was "Forget", which is the
        redundant second click this exists to remove. So the local bookkeeping
        follows the operator's decision, and an unconfirmed release is reported as
        a warning instead of a failure.

        Note the asymmetry with a *session-level* close, which still does require
        an ack (``close_remote_session_by_id``): a session record carries the
        capability needed to re-adopt a live session, so dropping it on an
        unconfirmed close can orphan real work. A target record only carries an
        address and a token, and re-pairing regenerates both.

        Order matters. ``client.release()`` runs FIRST, while the bridges are still
        registered as its sinks, so each open tab is told
        ``on_session_closed(released_by_client)`` and renders as closed. Tearing the
        bridges down first — which is what used to happen — detached them from the
        client, so release() found no sinks to notify and the tabs sat there
        looking live.
        """
        target = self.registry.get(target_id)
        is_linux = str(getattr(target, "platform", "") or "").lower().startswith("linux")
        label = target.display() if target is not None else target_id

        client = self._clients.pop(target_id, None)
        confirmed = True
        if client is not None:
            try:
                confirmed = bool(await client.release())
            except Exception as exc:
                logger.warning("remote_control: release of %s raised: %s",
                               target_id, exc)
                confirmed = False

        for local_sid, bridge in list(self._bridges.items()):
            if bridge._target_id != target_id:
                continue
            self._bridges.pop(local_sid, None)
            self._bindings.pop(local_sid, None)
            try:
                await bridge.destroy()
            except Exception:
                logger.debug("remote_control: bridge teardown on release failed",
                             exc_info=True)
            # Tell stdio_bridge to free the ``_flows`` slot. Skipping this left a
            # destroyed bridge in it, and ``_ensure_any_flow`` returns early on
            # any occupied slot — so the tab kept accepting messages that the
            # closed bridge dropped on the floor.
            self._release_bridge_slot(local_sid)

        self._live_sessions.pop(target_id, None)

        if is_linux:
            self.registry.forget(target_id)
            self._broadcast_targets(target_id, "released")
        else:
            self._broadcast_targets(target_id, "offline", "released")

        warning = ""
        if not confirmed:
            warning = (
                f"{label} did not confirm it destroyed its session."
                + (
                    "For a Linux daemon this is usually normal — it exits right after sending the confirmation frame. "
                    "If that machine is actually still alive, sessions on it may still be running, "
                    "you can see them after re-pairing."
                    if is_linux else
                    "Sessions on that machine may still be running; reconnect to view and handle them."
                )
            )
            logger.info("remote_control: release of %s unconfirmed: %s",
                        target_id, warning)
        return {"confirmed": confirmed, "forgot": is_linux, "warning": warning}

    def _release_bridge_slot(self, local_session_id: str) -> None:
        if self._on_bridge_released is None:
            return
        try:
            self._on_bridge_released(local_session_id)
        except Exception:
            logger.debug("remote_control: on_bridge_released failed", exc_info=True)


    async def exit_client(self) -> None:
        """Leave client mode — a purely LOCAL action.

        Disconnects every socket and nothing more: no session is destroyed, no
        pairing is forgotten, every remote keeps running exactly as it was. It is
        :meth:`close_client` applied to each connected target, so the next time
        this HandQ connects it finds its sessions where it left them.

        This used to release each target (destroying all their sessions and
        deleting their pairings), which made "stop being a client" mean "reach
        across the network and tear down every machine I was using". A controlled machine
        is a server; a client leaving is not an instruction to it. Destroying
        anything over there now requires :meth:`release_target`, per target,
        with its own confirmation.
        """
        for target_id in list(self._clients.keys()):
            try:
                await self.close_client(target_id)
            except Exception:
                logger.warning("remote_control: exit_client: disconnecting %s failed",
                               target_id, exc_info=True)

    def _drop_client(self, target_id: str) -> None:
        client = self._clients.pop(target_id, None)
        self._live_sessions.pop(target_id, None)
        if client is None:
            return
        try:
            asyncio.ensure_future(client.close())
        except RuntimeError:
            # No running loop (shutdown). The socket dies with the process.
            pass

    def _on_client_state(self, target_id: str, state: str, detail: str) -> None:
        """Broadcast connection state so the renderer can update the target list.

        Unstamped by design: this is machine-level, not session-level, news. The
        per-session card state comes from the bridge's own status envelopes.

        A reconnect also re-reconciles: ``client.remote_sessions`` was just
        refreshed by the handshake, so this is the earliest honest moment to
        drop chips for sessions that died while we were away.
        """
        if state == "connected":
            client = self._clients.get(target_id)
            if client is not None:
                self._adopt_live_sessions(target_id, client.remote_sessions)
        self._broadcast_targets(target_id, state, detail)

    # ── Session binding ──────────────────────────────────────────────────────

    def bind(
        self,
        local_session_id: str,
        target_id: str,
        remote_session_id: str = "",
        capability: str = "",
        since_seq: int = 0,
    ) -> None:
        """Declare that a local tab is backed by ``target_id``.

        Sent by the renderer before the first ``request`` for that tab.
        ``remote_session_id`` is supplied only when re-adopting a session that
        outlived its tab, in which case the capability comes from the registry.

        ``since_seq`` is carried through for the record and the panel only. It no
        longer decides how much gets replayed: a re-adopt opens a blank tab, so
        :meth:`RemoteSessionBridge.start` always asks for the full tail
        regardless of what is stored here.
        """
        title = ""
        if remote_session_id:
            target = self.registry.get(target_id)
            record = target.find_session(remote_session_id) if target else None
            if record is not None:
                title = record.title
                if not capability:
                    capability = record.capability
                    since_seq = max(since_seq, record.since_seq)
        self._bindings[local_session_id] = {
            "target_id": target_id,
            "remote_session_id": remote_session_id,
            "capability": capability,
            "since_seq": int(since_seq),
            "title": title,
        }


    def is_remote(self, local_session_id: str) -> bool:
        return (
            local_session_id in self._bindings
            or local_session_id in self._bridges
        )

    def bridge_for(self, local_session_id: str) -> Optional[RemoteSessionBridge]:
        return self._bridges.get(local_session_id)

    async def create_bridge(self, local_session_id: str) -> RemoteSessionBridge:
        """Build the bridge that will occupy ``stdio_bridge._flows[sid]``."""
        existing = self._bridges.get(local_session_id)
        if existing is not None:
            return existing

        binding = self._bindings.get(local_session_id)
        if binding is None:
            raise RemoteControlError(
                f"Session {local_session_id} is not bound to any remote target"
            )

        target_id = str(binding["target_id"])
        client = await self.ensure_client(target_id)
        bridge = RemoteSessionBridge(
            local_session_id=local_session_id,
            target_id=target_id,
            client=client,
            local_ui=self._ui_factory(local_session_id),
            loop=asyncio.get_running_loop(),
            emit=self._emit,
            on_seq_advanced=self._checkpoint_seq,
            on_session_gone=self._forget_session_record,
            remote_session_id=str(binding.get("remote_session_id") or ""),
            remote_capability=str(binding.get("capability") or ""),
            since_seq=int(binding.get("since_seq") or 0),
            title=str(binding.get("title") or ""),
        )
        self._bridges[local_session_id] = bridge
        return bridge

    def _forget_session_record(self, target_id: str, remote_session_id: str) -> None:
        """Drop a re-adopt record whose controlled session no longer exists."""
        self.registry.forget_session(target_id, remote_session_id)
        self._live_sessions[target_id] = [
            d for d in (self._live_sessions.get(target_id) or [])
            if not (isinstance(d, dict) and d.get("session_id") == remote_session_id)
        ]
        # Push the refreshed machine list so the dead chip disappears live.
        self._broadcast_targets(target_id, "session_gone")

    async def release_bridge(self, local_session_id: str) -> None:
        """Local tab closed. Detaches without killing the remote session.

        Always writes the record — the whole point of the tab/session asymmetry
        is that the controlled agent keeps working, so there is always something to come
        back to. There is no "was this worth remembering?" test: see the module
        docstring for the two ways the previous heuristic got that wrong.
        """
        self._bindings.pop(local_session_id, None)
        bridge = self._bridges.pop(local_session_id, None)
        if bridge is None:
            return
        # Persist the final seq before letting go, so a later re-adopt resumes
        # from the right place instead of replaying from the ring's oldest event.
        if bridge.remote_session_id:
            self.registry.remember_session(
                bridge._target_id,
                bridge.remote_session_id,
                bridge.remote_capability,
                title=bridge.title,
                since_seq=bridge._since_seq,
            )
        await bridge.destroy()
        self._broadcast_targets(bridge._target_id, "session_detached")


    async def close_remote_session(self, local_session_id: str) -> None:
        """Terminate the controlled-side session too, then release locally.

        Remote first, local bookkeeping second — ``bridge.close_remote()`` waits
        for the controlled side to confirm and raises otherwise, and on a raise the tab
        and the record both stay, because the session over there is still
        running. This ordering used to be reversed (forget, then ask), which is
        how a failed close produced a live session with no record of it.
        """
        bridge = self._bridges.get(local_session_id)
        if bridge is None:
            self._bindings.pop(local_session_id, None)
            return
        target_id = bridge._target_id
        remote_sid = bridge.remote_session_id
        await bridge.close_remote()
        if remote_sid:
            self.registry.forget_session(target_id, remote_sid)
            self._live_sessions[target_id] = [
                d for d in (self._live_sessions.get(target_id) or [])
                if not (isinstance(d, dict)
                        and d.get("session_id") == remote_sid)
            ]
        self._bindings.pop(local_session_id, None)
        self._bridges.pop(local_session_id, None)
        if target_id:
            self._broadcast_targets(target_id, "session_closed")


    async def close_remote_session_by_id(
        self, target_id: str, remote_session_id: str, *, force: bool = False
    ) -> None:
        """Terminate a controlled-side session that has no open local tab.

        The panel's session chips are *remembered* sessions
        (``remote_targets.json``), which usually have no live bridge — the tab
        was closed, only the re-adopt record remains. Closing one from there
        means opening a short-lived connection, telling the controlled side to close
        that session, and dropping the record. If a live tab happens to be
        driving it, defer to :meth:`close_remote_session` so the bridge is
        cleaned up too rather than leaving a half-dead tab.

        ``force=True`` is for a chip with no held capability at all (an
        orphan): the controlled side accepts an auth-token-only close in that case,
        so this is passed straight through to :meth:`RemoteControlClient.close_remote_session`.

        Raises if the controlled side did not confirm the destruction, and leaves the
        record in place when it does. The record used to be dropped from a
        ``finally``, unconditionally — so a close over a link that had just
        dropped, or without a capability, removed the only handle we had on a
        session that was still running. It then reappeared on the next connect as
        a greyed "not controllable" chip: present, alive, and unclosable. Keeping
        the record on failure means the operator can simply click × again.
        """
        # If a local tab is currently driving this remote session, close it the
        # full way (tab + remote) instead of going behind the bridge's back.
        for local_sid, bridge in list(self._bridges.items()):
            if bridge.remote_session_id == remote_session_id:
                await self.close_remote_session(local_sid)
                return

        target = self.registry.get(target_id)
        record = target.find_session(remote_session_id) if target else None
        capability = record.capability if record else ""
        client = await self.ensure_client(target_id)
        await client.close_remote_session(
            remote_session_id, capability, force=force
        )
        # Confirmed gone on the controlled side — only now is the local record wrong.
        self.registry.forget_session(target_id, remote_session_id)
        self._live_sessions[target_id] = [
            d for d in (self._live_sessions.get(target_id) or [])
            if not (isinstance(d, dict)
                    and d.get("session_id") == remote_session_id)
        ]
        self._broadcast_targets(target_id, "session_closed")

    def _checkpoint_seq(self, target_id: str, session_id: str, seq: int) -> None:
        """Throttled write-through of a session's replay position.

        Also the place a brand-new session first gets a record, so that a crash
        or a hard kill between "the remote session exists" and "the tab was
        closed cleanly" still leaves something to re-adopt.
        """
        key = f"{target_id}/{session_id}"
        now = time.monotonic()
        last = self._seq_last_write.get(key, 0.0)
        if now - last < SEQ_CHECKPOINT_INTERVAL:
            return
        self._seq_last_write[key] = now
        target = self.registry.get(target_id)
        if target is None:
            return
        if target.find_session(session_id) is None:
            bridge = next(
                (
                    b for b in self._bridges.values()
                    if b.remote_session_id == session_id
                ),
                None,
            )
            self.registry.remember_session(
                target_id,
                session_id,
                bridge.remote_capability if bridge else "",
                title=bridge.title if bridge else "",
                since_seq=seq,
            )
            return
        self.registry.update_session_seq(target_id, session_id, seq)


    # ── Shutdown ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """App shutdown. Every remote session parks and keeps its record.

        Closing HandQ is not releasing anything — the controlled agents keep working and
        we want to find them again next launch, so this writes a record for
        every bridge rather than judging which ones were "worth it".
        """
        for local_sid, bridge in list(self._bridges.items()):
            if bridge.remote_session_id:
                self.registry.remember_session(
                    bridge._target_id,
                    bridge.remote_session_id,
                    bridge.remote_capability,
                    title=bridge.title,
                    since_seq=bridge._since_seq,
                )
            try:
                await bridge.destroy()
            except Exception:
                logger.debug("remote_control: bridge teardown failed", exc_info=True)
        self._bridges.clear()
        self._bindings.clear()
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        self._connecting.clear()
        self._live_sessions.clear()
        self._seq_last_write.clear()

