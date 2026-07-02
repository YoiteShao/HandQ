"""Standalone demo/harness for the LAN chat room (src/infrastructure/chatroom).

NOT wired into the bridge — this is a manual test driver so you can watch two
or more HandQ-style nodes talk across the LAN (or on one box via multiple
terminals) AND see the guardrails fire in real time.

Owner (host the room):
    python chatroom_demo.py host --room dev --name win-pc-1

Member (join by address):
    python chatroom_demo.py join --host 192.168.1.5 --port 48611 --name win-pc-2

Member (auto-discover a room via UDP beacon):
    python chatroom_demo.py join --discover --name win-pc-2

Add ``--auto-agent`` on any node to make its "HandQ" auto-execute directed
TASKs and reply with a RESULT. Combined with ``/burn`` on another node you
can watch R2 (pair cooldown) and R3 (room budget) trip on the wire.

Interactive commands (type at the prompt):
    <text>              broadcast chat as the user
    @name <text>        chat aimed at a node (still just chat)
    /task @name <text>  send a TASK to a node's HandQ (chat's sender is USER)
    /handq <text>       speak as this node's HandQ agent (kind=handq)
    /result @name <t>   speak as HandQ replying with a RESULT
    /burn N             fire N handq CHATs quickly (trigger R2/R3 on purpose)
    /state              show local guardrail counters (owner only, others empty)
    /reset              clear all guardrail state (owner only)
    /roster             show who's in the room
    /me                 show my identity
    /help               show this help
    /quit               leave and exit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

# Make 'src' importable when run from the repo root.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.infrastructure.chatroom import (  # noqa: E402
    ChatRoomError,
    ChatRoomService,
    IncomingMessage,
    MessageIntent,
    NodeIdentity,
    NodeRole,
    SenderKind,
)
from src.infrastructure.chatroom._constants import (  # noqa: E402
    BUDGET_MAX_HANDQ_MSGS,
    BUDGET_WINDOW_SEC,
    ECHO_COOLDOWN_SEC,
    ECHO_MAX_PAIR_MSGS,
    ECHO_WINDOW_SEC,
)
from src.infrastructure.chatroom.router import parse_mentions  # noqa: E402

# ANSI colors (best-effort — Windows Terminal / VT-enabled cmd support them).
_C_RESET = "\x1b[0m"
_C_DIM = "\x1b[2m"
_C_RED = "\x1b[31m"
_C_YELLOW = "\x1b[33m"
_C_GREEN = "\x1b[32m"
_C_CYAN = "\x1b[36m"
_C_BOLD = "\x1b[1m"


def _kind_glyph(inc: IncomingMessage) -> str:
    """Render one incoming message as a single terminal line.

    SYSTEM notices are colored (yellow = normal, red = a "dropped" block).
    Directed TASKs to me are highlighted so the operator can see at a glance
    which lines the receiver's orchestrator would classify as actionable.
    """
    m = inc.message
    c = inc.classification
    who = f"{m.sender_kind.value}:{m.sender_display}"
    if m.sender_is_owner:
        who += "*"  # owner marker
    target = ("@" + ",".join(m.mentions)) if m.mentions else "@all"
    seq = m.seq if m.seq is not None else "?"

    tags = []
    if c.is_self:
        tags.append("SELF")
    if m.intent is MessageIntent.TASK:
        tags.append("TASK->me" if c.directed_to_me and not c.is_self else "TASK")
    elif m.intent is MessageIntent.RESULT:
        tags.append("RESULT")
    elif m.intent is MessageIntent.SYSTEM:
        # A SYSTEM notice carrying "dropped" is a hard block (R2/R3 hit);
        # anything else is informational (a fresh cooldown announcement,
        # future presence hooks, etc.).
        tags.append("SYSTEM")
    elif c.directed_to_me:
        tags.append("to-me")
    elif c.is_broadcast:
        tags.append("broadcast")
    tag = ("  [" + " ".join(tags) + "]") if tags else ""

    line = f"#{seq} {who} {target}: {m.body}{tag}"

    if m.sender_kind is SenderKind.SYSTEM:
        color = _C_RED if "dropped" in m.body else _C_YELLOW
        return f"{color}{line}{_C_RESET}"
    if m.intent is MessageIntent.TASK and c.directed_to_me and not c.is_self:
        return f"{_C_CYAN}{line}{_C_RESET}"
    if c.is_self:
        return f"{_C_DIM}{line}{_C_RESET}"
    return line


class DemoApp:
    def __init__(self, identity: NodeIdentity, auto_agent: bool) -> None:
        self.auto_agent = auto_agent
        self.svc = ChatRoomService(
            identity=identity,
            on_message=self._on_message,
            on_roster=self._on_roster,
            on_presence=self._on_presence,
            on_state=self._on_state,
        )

    # ── Callbacks ─────────────────────────────────────────────────────────

    async def _on_message(self, inc: IncomingMessage) -> None:
        print("\n  " + _kind_glyph(inc))
        # Auto-agent mock: execute any directed TASK and reply with a RESULT.
        # In the real system this decision lives in the orchestrator, using
        # its native chat-vs-task classifier plus sender_kind context; the
        # chatroom itself no longer decides.
        if (
            self.auto_agent
            and not inc.classification.is_self
            and inc.classification.directed_to_me
            and inc.message.intent is MessageIntent.TASK
        ):
            sender = inc.message.sender_display
            print(f"  [auto-agent] executing task from {sender} ...")
            await asyncio.sleep(0.2)
            await self.svc.send(
                f"@{sender} done: '{inc.message.body}' (simulated)",
                sender_kind=SenderKind.HANDQ,
                intent=MessageIntent.RESULT,
            )
        self._prompt()

    def _on_roster(self, roster) -> None:  # list[Participant]
        names = ", ".join(
            f"{p.node.display_name}{'*' if p.node.is_owner else ''}" for p in roster
        )
        print(f"\n  {_C_DIM}[roster] ({len(roster)}) {names}{_C_RESET}")
        self._prompt()

    def _on_presence(self, event: str, node: NodeIdentity) -> None:
        print(f"\n  {_C_DIM}[presence] {node.display_name} {event}{_C_RESET}")

    def _on_state(self, state: str, info: dict) -> None:
        print(f"\n  {_C_DIM}[state] {state} {info}{_C_RESET}")

    def _prompt(self) -> None:
        print("> ", end="", flush=True)

    # ── Interactive loop ──────────────────────────────────────────────────

    async def repl(self) -> None:
        loop = asyncio.get_running_loop()
        self._print_help_short()
        self._prompt()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF (Ctrl-Z / Ctrl-D)
                break
            text = line.rstrip("\n")
            if not text.strip():
                self._prompt()
                continue
            if not await self._handle_command(text):
                break
        await self.svc.shutdown()

    async def _handle_command(self, text: str) -> bool:
        try:
            if text in ("/quit", "/exit"):
                return False
            if text == "/help":
                print(__doc__)
            elif text == "/me":
                print("  " + str(self.svc.identity.to_dict()))
            elif text == "/roster":
                self._on_roster(self.svc.roster())
                return True
            elif text == "/state":
                self._print_guardrail_state()
            elif text == "/reset":
                self._reset_guardrails()
            elif text.startswith("/burn"):
                await self._burn(text)
            elif text.startswith("/task "):
                body = text[len("/task "):]
                await self.svc.send(
                    body, sender_kind=SenderKind.USER, intent=MessageIntent.TASK,
                    mentions=parse_mentions(body),
                )
            elif text.startswith("/handq "):
                await self.svc.send(text[len("/handq "):], sender_kind=SenderKind.HANDQ)
            elif text.startswith("/result "):
                body = text[len("/result "):]
                await self.svc.send(
                    body, sender_kind=SenderKind.HANDQ, intent=MessageIntent.RESULT,
                )
            else:
                await self.svc.send(text, sender_kind=SenderKind.USER)
        except ChatRoomError as exc:
            print(f"  {_C_RED}[error] {exc}{_C_RESET}")
        self._prompt()
        return True

    # ── Guardrail commands ─────────────────────────────────────────────────

    async def _burn(self, text: str) -> None:
        """``/burn N`` — send N handq CHATs quickly to trigger R2/R3.

        Great for demos: on Owner A run ``/burn 5``. The first 3 land, then
        a pair cooldown notice, then a run of "dropped" notices until R2
        expires — all visible as color-coded SYSTEM lines in the transcript.
        Combine with a HandQ member replying (auto-agent) to see the pair
        threshold hit even faster.
        """
        parts = text.split()
        try:
            n = int(parts[1]) if len(parts) > 1 else ECHO_MAX_PAIR_MSGS + 1
        except ValueError:
            print(f"  {_C_RED}[error] usage: /burn N{_C_RESET}")
            return
        n = max(1, min(n, 200))  # sanity clamp
        print(f"  {_C_DIM}[burn] firing {n} handq CHATs ...{_C_RESET}")
        for i in range(n):
            try:
                await self.svc.send(
                    f"burn-{i+1}/{n}", sender_kind=SenderKind.HANDQ,
                )
            except ChatRoomError as exc:
                print(f"  {_C_RED}[burn:{i+1}] {exc}{_C_RESET}")
                return
            # No sleep — we WANT to be inside the ECHO_WINDOW.

    def _print_guardrail_state(self) -> None:
        """Show what R2/R3 counters currently look like on THIS node.

        Only meaningful on the owner (guardrails are owner-enforced). On a
        member the counters are always empty by design — the message tells
        the operator that clearly instead of pretending it's a bug.
        """
        role = self.svc.role
        if role is not NodeRole.OWNER:
            print(
                f"  {_C_DIM}[state] this node is {role.value if role else 'idle'}"
                f" — guardrails live on the owner; local counters are empty"
                f" by design.{_C_RESET}"
            )
            return
        now = time.time()
        window_start = now - ECHO_WINDOW_SEC
        budget_start = now - BUDGET_WINDOW_SEC
        # Per-node handq counts within echo window (R2 signal).
        per_node: dict[str, int] = {}
        for nid, ts in self.svc._handq_msg_log:
            if ts >= window_start:
                per_node[nid] = per_node.get(nid, 0) + 1
        # Room-wide handq count within budget window (R3 signal).
        budget_count = sum(1 for _, ts in self.svc._handq_msg_log if ts >= budget_start)
        active_cooldowns = [
            (pair, t - now)
            for pair, t in self.svc._pair_cooldown_until.items()
            if t > now
        ]
        print(f"  {_C_BOLD}[state] owner guardrail counters{_C_RESET}")
        print(
            f"    R2 · echo window = {ECHO_WINDOW_SEC:.0f}s, "
            f"per-pair threshold = {ECHO_MAX_PAIR_MSGS} combined, "
            f"cooldown = {ECHO_COOLDOWN_SEC:.0f}s"
        )
        if per_node:
            for nid, count in per_node.items():
                display = self._display_for(nid)
                print(f"      handq[{display}] = {count} in last window")
        else:
            print("      (no handq messages in echo window)")
        if active_cooldowns:
            for pair, remain in active_cooldowns:
                da = self._display_for(pair[0])
                db = self._display_for(pair[1])
                print(
                    f"      {_C_YELLOW}COOLING: {da} <-> {db}, "
                    f"{remain:.1f}s left{_C_RESET}"
                )
        else:
            print("      (no active pair cooldowns)")
        print(
            f"    R3 · budget window = {BUDGET_WINDOW_SEC:.0f}s, "
            f"quota = {BUDGET_MAX_HANDQ_MSGS} handq msgs"
        )
        colored = (
            _C_RED if budget_count >= BUDGET_MAX_HANDQ_MSGS
            else _C_YELLOW if budget_count >= BUDGET_MAX_HANDQ_MSGS * 0.8
            else _C_GREEN
        )
        print(
            f"      {colored}handq usage: "
            f"{budget_count}/{BUDGET_MAX_HANDQ_MSGS}{_C_RESET}"
        )

    def _reset_guardrails(self) -> None:
        if self.svc.role is not NodeRole.OWNER:
            print(
                f"  {_C_RED}[error] /reset only works on the owner "
                f"(this node is {self.svc.role.value if self.svc.role else 'idle'}){_C_RESET}"
            )
            return
        self.svc.reset_guardrails()
        print(f"  {_C_GREEN}[reset] pair cooldowns and budget cleared{_C_RESET}")

    def _display_for(self, node_id: str) -> str:
        """Look up a display name for *node_id* from the local roster."""
        for p in self.svc.roster():
            if p.node.node_id == node_id:
                return p.node.display_name
        return node_id[:8]

    def _print_help_short(self) -> None:
        print(
            "Commands: <text>=chat  /task @n <t>  /handq <t>  /result @n <t>  "
            "/burn N  /state  /reset  /roster  /me  /help  /quit"
        )


async def _amain(args: argparse.Namespace) -> None:
    identity = NodeIdentity.local(display_name=args.name, user_name=args.user)
    app = DemoApp(identity, auto_agent=args.auto_agent)

    if args.cmd == "discover":
        from src.infrastructure.chatroom import discovery
        beacons = await discovery.discover(timeout=args.timeout)
        if not beacons:
            print("no rooms found")
        for b in beacons:
            print(f"  room={b.room!r} owner={b.owner!r} at {b.host}:{b.tcp_port}")
        return

    try:
        if args.cmd == "host":
            info = await app.svc.host(
                room=args.room, bind_host=args.bind, tcp_port=args.port,
                announce=not args.no_announce,
            )
            print(f"Hosting room {info.room!r} on {info.host}:{info.port}")
            print(f"Others join with: --host {info.host} --port {info.port}")
        else:  # join
            if args.discover:
                info = await app.svc.join(room=args.room, discover_timeout=args.timeout)
            else:
                info = await app.svc.join(host=args.host, port=args.port, room=args.room)
            print(f"Joined room {info.room!r} at {info.host}:{info.port}")
    except ChatRoomError as exc:
        print(f"failed: {exc}")
        return

    await app.repl()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="HandQ LAN chat-room demo")
    p.add_argument("--name", default=None, help="display name (default: hostname)")
    p.add_argument("--user", default=None, help="user name (default: env USERNAME)")
    p.add_argument("--auto-agent", action="store_true",
                   help="auto-execute directed TASKs and reply with a RESULT")
    sub = p.add_subparsers(dest="cmd", required=True)

    ph = sub.add_parser("host", help="host a room (become the owner)")
    ph.add_argument("--room", default="dev")
    ph.add_argument("--bind", default="0.0.0.0")
    ph.add_argument("--port", type=int, default=48611)
    ph.add_argument("--no-announce", action="store_true",
                    help="don't broadcast a UDP discovery beacon")

    pj = sub.add_parser("join", help="join a room (become a member)")
    pj.add_argument("--host", default=None)
    pj.add_argument("--port", type=int, default=48611)
    pj.add_argument("--room", default=None)
    pj.add_argument("--discover", action="store_true", help="find a room via UDP beacon")
    pj.add_argument("--timeout", type=float, default=3.0)

    pd = sub.add_parser("discover", help="list rooms on the LAN and exit")
    pd.add_argument("--timeout", type=float, default=3.0)
    return p


def main() -> None:
    args = _build_parser().parse_args()
    if not hasattr(args, "timeout"):
        args.timeout = 3.0
    # Best-effort: enable ANSI on Windows consoles that support it.
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
