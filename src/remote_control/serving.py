"""Controlled-side configuration and identity.

Shared by both hosts — the Electron bridge on Windows and the ``handq_linux.py``
daemon — because the decisions here (what to bind, on what port, how many
sessions) are host-independent even though the two build their sessions very
differently.

**This section says HOW to serve, never WHETHER.** There is no ``serve`` flag.
Windows enters server mode only when the operator presses "As Server" in the
Connect panel; the Linux daemon always listens, because being driven is the only
reason it exists. A persisted "yes, serve" used to live here and start a listener
at boot, alongside a settings-panel checkbox that wrote it — so the same question
had two answers, and the boot path opened the machine up with a token nobody had
been shown. Both are gone. Nothing about being restarted or upgraded puts a
machine into server mode.

**The token is always ephemeral**, minted fresh on every ``resolve_token()`` call
and never written anywhere. Persisting it would buy nothing for the case people
assume it helps: a *client* disconnect does not restart the controlled process, so the
token in memory is still valid and reconnect works untouched. And a controlled *restart*
destroys every session with it (the event log and the ``FlowControllerV2`` are
both in memory), so there is nothing left to re-attach to — a fresh pairing is
required regardless. A token at rest in a file, never rotating, is therefore pure
exposure with no matching benefit. The opt-in ``persist_token`` that used to
exist was for staying paired across reboots on a machine pinned to a fixed port,
which stopped being coherent once serving became a deliberate per-run act.

The token is deliberately NOT a config field. A secret in ``handq_config.yaml``
would be edited by hand, round-tripped through the ``config_set`` IPC, and echoed
into logs by anything that dumps the config — several chances to leak something
that grants agent sessions on this machine.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("handq.remote_control.serving")

TOKEN_BYTES = 32


@dataclass
class RemoteControlConfig:
    """The ``remote_control:`` section of ``handq_config.yaml``.

    Advanced, yaml-only knobs. None of these appear in the settings UI: every
    Connect-related decision a normal user makes belongs in the Connect panel,
    and a checkbox in Settings that quietly duplicated a panel action is exactly
    the ambiguity this file's docstring describes. What is left is either
    genuinely deployment-shaped (a headless Linux daemon's bind address and
    session cap) or a security switch that should require deliberately editing a
    file on the controlled machine.
    """

    bind: str = "0.0.0.0"
    #: 0 = ask the OS for a free port. Default because it is the mode already
    #: validated in the real machine pool and because two HandQ instances on one
    #: host then never collide. Pin it only if a firewall rule needs a fixed
    #: number; pairing carries the port either way.
    port: int = 0
    max_sessions: int = 16
    #: Let a remote operator answer this machine's `request_secret_input`
    #: prompts (in practice: a first-time SSH password for a third host). OFF by
    #: default because without TLS that password crosses the network in
    #: cleartext. See NetworkUIDelegate.request_secret_input for the refusal
    #: path and the out-of-band alternative it points the operator at.
    allow_remote_secret_prompt: bool = False

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "RemoteControlConfig":
        section = (config or {}).get("remote_control")
        if not isinstance(section, dict):
            return cls()

        def _bool(key: str, default: bool) -> bool:
            value = section.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        def _int(key: str, default: int) -> int:
            try:
                return int(section.get(key, default))
            except (TypeError, ValueError):
                return default

        # Keys this version no longer honours are ignored rather than rejected —
        # an old config file must not stop HandQ from starting. They are logged
        # once so a puzzled operator finds out why the setting stopped mattering
        # instead of assuming it still works.
        for retired, why in (
            ("serve", "serving is now started only from the Connect panel"),
            ("persist_token", "the control token is always ephemeral"),
            ("mirror_locally", "the controlled side never creates a mirror tab"),
        ):
            if retired in section:
                logger.info(
                    "remote_control: ignoring retired config key %r — %s",
                    retired, why,
                )

        return cls(
            bind=str(section.get("bind") or "0.0.0.0"),
            port=_int("port", 0),
            max_sessions=max(1, _int("max_sessions", 16)),
            allow_remote_secret_prompt=_bool("allow_remote_secret_prompt", False),
        )

    def resolve_token(self) -> str:
        """A fresh control token for this run. See the module docstring on why
        there is no persisted alternative."""
        return secrets.token_urlsafe(TOKEN_BYTES)
