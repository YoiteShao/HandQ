#!/usr/bin/env python3
"""
HandQ Mesh 通信 demo — 独立脚本，不依赖 HandQ 任何模块。

依赖:
    pip install websockets

房主（启动 relay）:
    python mesh_demo.py host
    python mesh_demo.py host --port 9000

房客（加入房间）:
    python mesh_demo.py join ws://192.168.1.10:8765 --name PC2
    python mesh_demo.py join ws://192.168.1.10:8765 --name Linux1
"""
import argparse
import asyncio
import json
import socket
import sys

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("缺少依赖，请先运行: pip install websockets")
    sys.exit(1)


# ─── Relay (房主) ─────────────────────────────────────────────────────────────

_peers: dict[str, "websockets.ServerConnection"] = {}


async def _broadcast(message: str, exclude: str = "") -> None:
    for name, ws in list(_peers.items()):
        if name == exclude:
            continue
        try:
            await ws.send(message)
        except Exception:
            pass


async def _relay_handler(ws) -> None:
    peer_name = f"peer-{id(ws)}"
    try:
        # 第一条消息必须是 join
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != "join":
            await ws.close(1002, "expected join first")
            return

        peer_name = str(msg.get("peer", peer_name))
        _peers[peer_name] = ws

        print(f"[relay] + {peer_name} 加入  (当前 {len(_peers)} 人: {list(_peers.keys())})")

        # 通知房间内其他人
        await _broadcast(
            json.dumps({"type": "sys", "text": f"{peer_name} 加入了房间"}),
            exclude=peer_name,
        )
        # 欢迎新成员
        await ws.send(json.dumps({
            "type": "sys",
            "text": f"欢迎 {peer_name}！当前房间成员: {list(_peers.keys())}",
        }))

        # 转发消息循环
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg["from"] = peer_name  # relay 盖戳发送者
            fwd = json.dumps(msg, ensure_ascii=False)
            if msg.get("type") == "msg":
                print(f"[relay] {peer_name}: {msg.get('text', '')}")
            await _broadcast(fwd, exclude=peer_name)

    except (ConnectionClosed, asyncio.TimeoutError):
        pass
    finally:
        _peers.pop(peer_name, None)
        print(f"[relay] - {peer_name} 离开  (当前 {len(_peers)} 人)")
        await _broadcast(
            json.dumps({"type": "sys", "text": f"{peer_name} 离开了房间"}),
            exclude=peer_name,
        )


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def run_host(port: int) -> None:
    ip = _get_local_ip()
    url = f"ws://{ip}:{port}"
    print()
    print("=" * 50)
    print("  HandQ Mesh Relay — 房主模式")
    print("=" * 50)
    print(f"  本机 IP : {ip}")
    print(f"  端口    : {port}")
    print(f"  连接地址: {url}")
    print()
    print("  把上面的地址发给房客，让他们运行:")
    print(f"  python mesh_demo.py join {url} --name 你的名字")
    print("=" * 50)
    print()

    async with websockets.serve(_relay_handler, "0.0.0.0", port):
        print(f"[relay] 监听 0.0.0.0:{port} ... (Ctrl+C 退出)")
        await asyncio.Future()  # 永久运行


# ─── Client (房客) ────────────────────────────────────────────────────────────

async def run_join(url: str, name: str) -> None:
    print(f"[client] 正在连接 {url}，身份: {name} ...")
    try:
        async with websockets.connect(url) as ws:
            # 发送 join
            await ws.send(json.dumps({"type": "join", "peer": name}))
            print(f"[client] 已加入房间。输入消息后按 Enter 发送，Ctrl+C 退出。\n")

            async def _recv_loop():
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg["type"] == "sys":
                        print(f"\n[系统] {msg['text']}")
                    elif msg["type"] == "msg":
                        sender = msg.get("from", "?")
                        text = msg.get("text", "")
                        print(f"\n{sender}: {text}")
                    print("> ", end="", flush=True)

            recv_task = asyncio.create_task(_recv_loop())

            loop = asyncio.get_event_loop()
            try:
                while True:
                    print("> ", end="", flush=True)
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        break
                    text = line.strip()
                    if text:
                        await ws.send(json.dumps({"type": "msg", "text": text}))
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass

    except OSError as e:
        print(f"[client] 连接失败: {e}")
        print("请确认:")
        print("  1. 房主已经启动 (python mesh_demo.py host)")
        print("  2. IP 和端口正确")
        print("  3. 防火墙没有阻挡该端口")
        sys.exit(1)

    print("[client] 已断开连接")


# ─── Entry ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HandQ Mesh 通信 demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  房主:  python mesh_demo.py host
  房客:  python mesh_demo.py join ws://192.168.1.10:8765 --name PC2
        """,
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    h = sub.add_parser("host", help="启动 relay 服务器（房主）")
    h.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")

    j = sub.add_parser("join", help="加入房间（房客）")
    j.add_argument("url", help="relay 地址，例如 ws://192.168.1.10:8765")
    j.add_argument("--name", required=True, help="你在房间里的名字")

    args = parser.parse_args()

    try:
        if args.mode == "host":
            asyncio.run(run_host(args.port))
        else:
            asyncio.run(run_join(args.url, args.name))
    except KeyboardInterrupt:
        print("\n再见！")


if __name__ == "__main__":
    main()
