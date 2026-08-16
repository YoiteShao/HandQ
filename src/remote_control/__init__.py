"""Direct control channel — one HandQ driving another over plain TCP.

The shape, in one paragraph: every HandQ that is willing to be driven runs a
:class:`~src.remote_control.server.RemoteControlServer` bound to
``0.0.0.0:<port>``. A controlling HandQ opens ONE TCP connection per remote
machine and multiplexes any number of sessions over it. On the controlled side each
remote session gets a :class:`~src.remote_control.network_delegate.NetworkUIDelegate`
installed as its ``InteractionManager`` delegate; on the controlling side a
:class:`~src.remote_control.session_bridge.RemoteSessionBridge` occupies the
``stdio_bridge._flows[sid]`` slot and replays every frame onto the local
``_StdioUI``. The local chat UI therefore renders a remote session with exactly
the code path it uses for a local one.

Two properties this package exists to guarantee, both of which shaped the
protocol more than anything else:

* **No content loss across a disconnect.** Every fire-and-forget UI event is
  assigned a monotonic per-session ``seq`` and retained in an
  :class:`~src.remote_control.event_log.EventLog`. ``attach_session`` carries
  ``since_seq``, so a reconnecting controller is sent precisely the events it
  missed. When the requested seq has already aged out of the ring the server
  says so explicitly (``gap=True``) rather than silently starting from now.

* **A disconnect never disturbs the controlled agent.** Pending confirmations are
  *parked indefinitely* — the future is neither cancelled nor defaulted. It
  survives in the session and is re-offered to whoever attaches next. The
  agent is simply blocked on the human, which is what it would be locally.

Modules
-------
``protocol``          frame-type constants + builders (wire uses ``"t"``)
``event_log``         per-session seq'd ring buffer + replay/gap logic
``network_delegate``  controlled-side ``UIDelegate`` → ``agent_event`` frames
``server``           controlled-side listener, session registry, heartbeat, auth
``client``           controlling-side connection, auto-reconnect, session multiplex
``session_bridge``   controlling-side stand-in for ``FlowControllerV2``
``registry``         on-disk record of paired targets
``address``          ``handq://host:port/token`` pairing-string codec
"""
from __future__ import annotations

from . import protocol
from .address import ControlAddress, format_address, parse_address
from .event_log import EventLog, ReplayResult

__all__ = [
    "ControlAddress",
    "EventLog",
    "ReplayResult",
    "format_address",
    "parse_address",
    "protocol",
]
