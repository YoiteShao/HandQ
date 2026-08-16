"""The pairing string — one line a human can copy between two machines.

``handq://10.239.170.152:55079/3f9a…c1?name=lab-win-02``

With TLS deliberately out of scope for this phase, pairing reduces to moving a
host, a port and a bearer token across the gap. That fits on one line, so it is
one line: the controlled machine shows it, the operator pastes it into the controlling machine,
done. The design doc's ``pairing.py`` (6-digit code → HMAC-derived long-term
token) is not implemented, and would have been dead weight here — a short code
only earns its keep when the thing it protects must survive brute force, which
is the case for a 6-digit code and not for a 256-bit token that is copied whole.

``handq://`` is not registered as an OS URL scheme; it is a marker that makes the
string obviously-a-HandQ-address in a chat window, and gives the parser
something to reject unambiguously.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

SCHEME = "handq"

#: Tokens are ``secrets.token_urlsafe`` output. Accept a generous character set
#: but insist on a length floor — a short token in a pairing string is far more
#: likely to be a truncated paste than a deliberate choice, and failing loudly
#: beats failing at auth time on the other machine.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")

MIN_TOKEN_LENGTH = 16


class AddressError(ValueError):
    """Raised for a pairing string that cannot be parsed or is implausible."""


@dataclass(frozen=True)
class ControlAddress:
    """A parsed pairing string."""

    host: str
    port: int
    token: str
    name: str = ""

    @property
    def endpoint(self) -> str:
        """``host:port``, for logs and UI labels. Never includes the token."""
        return f"{self.host}:{self.port}"

    def display(self) -> str:
        """Human label that is safe to log or show in a list — no token."""
        return f"{self.name} ({self.endpoint})" if self.name else self.endpoint

    def to_string(self) -> str:
        return format_address(self.host, self.port, self.token, self.name)


def format_address(host: str, port: int, token: str, name: str = "") -> str:
    """Build the pairing string shown on the controlled machine."""
    # An IPv6 literal must be bracketed or the ``host:port`` split is ambiguous.
    host_part = f"[{host}]" if ":" in host else host
    base = f"{SCHEME}://{host_part}:{int(port)}/{token}"
    if name:
        return f"{base}?name={urllib.parse.quote(name, safe='')}"
    return base


def parse_address(text: str) -> ControlAddress:
    """Parse a pairing string.

    Tolerant of what a paste actually looks like: surrounding whitespace, a
    trailing period from prose, and a missing scheme (``host:port/token`` alone
    is accepted, because that is what people type from memory). Intolerant of
    anything ambiguous — a missing token or a non-numeric port raises rather
    than being guessed at, since a wrong guess surfaces much later as an
    inscrutable auth failure on the other machine.
    """
    raw = (text or "").strip().strip(".,;")
    if not raw:
        raise AddressError("pairing string is empty")

    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        if scheme.lower() != SCHEME:
            raise AddressError(
                f"unsupported scheme {scheme!r} — expected {SCHEME}://"
            )
    else:
        rest = raw

    # Split the optional query off first so a ``?name=`` containing ``/`` can
    # never be mistaken for part of the token.
    rest, _, query = rest.partition("?")

    authority, sep, token = rest.partition("/")
    if not sep or not token:
        raise AddressError(
            "pairing string is missing the token — expected "
            f"{SCHEME}://host:port/token"
        )
    token = token.strip("/")

    host, port = _split_authority(authority)

    if not _TOKEN_RE.match(token):
        raise AddressError(
            f"token looks malformed or truncated ({len(token)} chars; "
            f"expected at least {MIN_TOKEN_LENGTH} url-safe characters)"
        )

    name = ""
    if query:
        parsed = urllib.parse.parse_qs(query)
        name = (parsed.get("name") or [""])[0].strip()

    return ControlAddress(host=host, port=port, token=token, name=name)


def _split_authority(authority: str) -> tuple[str, int]:
    authority = authority.strip()
    if not authority:
        raise AddressError("pairing string is missing host:port")

    if authority.startswith("["):  # IPv6 literal
        close = authority.find("]")
        if close < 0:
            raise AddressError("unterminated IPv6 literal in pairing string")
        host = authority[1:close]
        remainder = authority[close + 1 :]
        if not remainder.startswith(":"):
            raise AddressError("pairing string is missing the port")
        port_text = remainder[1:]
    else:
        host, sep, port_text = authority.rpartition(":")
        if not sep:
            raise AddressError("pairing string is missing the port")

    host = host.strip()
    if not host:
        raise AddressError("pairing string is missing the host")

    try:
        port = int(port_text)
    except ValueError:
        raise AddressError(f"port {port_text!r} is not a number") from None
    if not (1 <= port <= 65535):
        raise AddressError(f"port {port} is out of range")

    return host, port


def guess_lan_ip() -> str:
    """Best-effort "which of my addresses would a peer actually reach me on".

    Opens a UDP socket toward a public address and reads back the local end. No
    packet is sent — this is purely asking the OS routing table which interface
    it would pick. Copied from ``verify_fleet_scheduling/remote_probe_server.py``,
    where it was validated against the real machine pool; ``gethostbyname`` was
    the obvious alternative and returns ``127.0.0.1`` on plenty of Linux hosts.
    """
    import socket

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def local_endpoints(port: int) -> list[str]:
    """Every plausible ``host:port`` a peer might use, best first.

    Shown on the controlled machine so an operator on a multi-homed box (VPN + LAN,
    a common shape in the target machine pool) can pick the reachable one
    instead of guessing.
    """
    import socket

    seen: list[str] = []

    def add(host: Optional[str]) -> None:
        if not host or host.startswith("127."):
            return
        endpoint = f"{host}:{port}"
        if endpoint not in seen:
            seen.append(endpoint)

    add(guess_lan_ip())
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    if not seen:
        seen.append(f"127.0.0.1:{port}")
    return seen
