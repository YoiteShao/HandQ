# -*- coding: utf-8 -*-
"""One-off perf bench for email + web_search common scenarios.

Usage (from project root):
    python bench_perf.py                  # email only
    python bench_perf.py --with-browser   # also launch Edge and bench web_search
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("HANDQ_CONFIG", str(HERE / "handq_config.yaml"))


def fmt_secs(s: float) -> str:
    if s >= 1.0:
        return f"{s:7.2f}s"
    return f"{int(s * 1000):5d}ms"


async def time_call(label, coro_factory):
    t0 = time.perf_counter()
    result = await coro_factory()
    dt = time.perf_counter() - t0
    ok = getattr(result, "success", None)
    out = getattr(result, "output", None) or {}
    bits = []
    if isinstance(out, dict):
        for k in ("count", "folders_scanned", "truncated", "total_estimated"):
            if k in out:
                bits.append(f"{k}={out[k]}")
        if out.get("cached"):
            bits.append("CACHED")
    suffix = f"  [{', '.join(bits)}]" if bits else ""
    print(f"  {label:62s} {fmt_secs(dt):>10s}  ok={ok}{suffix}")
    if not ok:
        err = getattr(result, "error", "") or ""
        print(f"    -> {err[:160]}")
    return result, dt


async def bench_email():
    print("\n=== email ===")
    from src.tools.email_tool import EmailTool
    tool = EmailTool()

    today = datetime.now().date().isoformat()
    week_ago = (datetime.now() - timedelta(days=7)).date().isoformat()
    month_ago = (datetime.now() - timedelta(days=30)).date().isoformat()

    await time_call(
        "list_folders (cold; first COM dispatch + gen_py)",
        lambda: tool.execute(action="list_folders"),
    )
    await time_call(
        "list_folders (warm)",
        lambda: tool.execute(action="list_folders"),
    )

    await time_call(
        "list_messages Inbox limit=20 recursive=true (no since)",
        lambda: tool.execute(action="list_messages", folder="Inbox", limit=20),
    )
    await time_call(
        "list_messages Inbox limit=20 recursive=false",
        lambda: tool.execute(action="list_messages", folder="Inbox", recursive=False, limit=20),
    )

    await time_call(
        f"list_messages Inbox since={today} limit=50 (DASL push-down)",
        lambda: tool.execute(action="list_messages", folder="Inbox", since=today, limit=50),
    )
    await time_call(
        f"list_messages Inbox since={today} unread_only=true",
        lambda: tool.execute(
            action="list_messages", folder="Inbox", since=today, unread_only=True, limit=50,
        ),
    )
    await time_call(
        f"list_messages Inbox since={week_ago} limit=50",
        lambda: tool.execute(action="list_messages", folder="Inbox", since=week_ago, limit=50),
    )
    await time_call(
        f"list_messages Inbox since={month_ago} limit=100",
        lambda: tool.execute(action="list_messages", folder="Inbox", since=month_ago, limit=100),
    )
    await time_call(
        f"list_messages Inbox since={month_ago} limit=100 NO_PREVIEW",
        lambda: tool.execute(
            action="list_messages", folder="Inbox", since=month_ago, limit=100,
            include_body_preview=False,
        ),
    )

    await time_call(
        f"list_messages Inbox since={week_ago} limit=50 NO_PREVIEW",
        lambda: tool.execute(
            action="list_messages", folder="Inbox", since=week_ago, limit=50,
            include_body_preview=False,
        ),
    )

    await time_call(
        "list_messages Inbox limit=20 recursive=true NO_PREVIEW",
        lambda: tool.execute(
            action="list_messages", folder="Inbox", limit=20,
            include_body_preview=False,
        ),
    )

    await time_call(
        "search Inbox query='meeting' phrase limit=10",
        lambda: tool.execute(action="search", folder="Inbox", query="meeting", limit=10),
    )
    await time_call(
        "search Inbox query='meeting' phrase limit=10 (warm idx)",
        lambda: tool.execute(action="search", folder="Inbox", query="meeting", limit=10),
    )
    await time_call(
        "search Inbox query='review' phrase limit=10",
        lambda: tool.execute(action="search", folder="Inbox", query="review", limit=10),
    )
    await time_call(
        "search Inbox query='review' phrase limit=10 NO_PREVIEW",
        lambda: tool.execute(
            action="search", folder="Inbox", query="review", limit=10,
            include_body_preview=False,
        ),
    )


async def bench_web_search(with_browser: bool):
    print("\n=== web_search ===")
    from src.tools.web_search_tool import WebSearchTool
    tool = WebSearchTool()

    if not with_browser:
        await time_call(
            "confluence q='roadmap' (no session - fast-fail)",
            lambda: tool.execute(source="confluence", query="roadmap", limit=5),
        )
        print("  (skipping rest — pass --with-browser to launch Edge and bench)")
        return

    from src.tools.browser_tool import BrowserTool
    bt = BrowserTool()
    print("  launch_browser (opens Edge) ...")
    t0 = time.perf_counter()
    r = await bt.execute(action="launch_browser")
    print(f"  launch_browser: {fmt_secs(time.perf_counter() - t0)}  ok={r.success}")
    if not r.success:
        print(f"    -> {(r.error or '')[:200]}")
        return

    queries = {
        "confluence": "roadmap",
        "jira": "power",
        "sharepoint": "design",
    }
    for src, q in queries.items():
        await time_call(
            f"{src:10s} q={q!r:10s} (cold)",
            lambda s=src, qq=q: tool.execute(source=s, query=qq, limit=5),
        )
        await time_call(
            f"{src:10s} q={q!r:10s} (cache hit)",
            lambda s=src, qq=q: tool.execute(source=s, query=qq, limit=5),
        )

    await time_call(
        "orbit      q='infra'    (DOM extract)",
        lambda: tool.execute(source="orbit", query="infra", limit=5),
    )


async def main():
    with_browser = "--with-browser" in sys.argv
    await bench_email()
    await bench_web_search(with_browser=with_browser)


if __name__ == "__main__":
    asyncio.run(main())
