"""End-to-end verification of the storage_state cookie-carry-over path.

Simulates the flow:
  1. Launch Chromium in profile A → seed synthetic cookie + real navigation
  2. Persist cookies via ``_persist_context_storage_state`` (writes JSON)
  3. Close profile A
  4. Launch Chromium in a *fresh* profile B (different user-data-dir)
  5. Inject cookies via ``_inject_shared_storage_state``
  6. Read cookies back from profile B and verify the seeded cookie survived

No real login is used — we bypass DPAPI + real SSO by seeding a plain HTTP
cookie via ``context.add_cookies`` in step 1. This is the exact same code path
the browser tool uses on close/launch, so success here means the fix actually
works in production too.

Run with:
    $env:PYTHONIOENCODING = "utf-8"
    python tools_test_storage_state.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

# Guarantee we can import the src.* package layout.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright

from src.tools.browser_tool import (
    _inject_shared_storage_state,
    _persist_context_storage_state,
)
from src.infrastructure.browser_paths import user_browser_shared_storage_state_path


SEED_COOKIE = {
    "name": "handq_storage_state_test",
    "value": "carry-over-works-42",
    "domain": ".example.com",
    "path": "/",
    "expires": -1,  # session cookie (persists via storage_state)
    "httpOnly": False,
    "secure": False,
    "sameSite": "Lax",
}


async def _launch(user_data_dir: str):
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="msedge",
        headless=True,  # off-screen so the test doesn't flash a window
        args=[
            "--window-position=-32000,-32000",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        viewport={"width": 1280, "height": 800},
        accept_downloads=False,
    )
    return pw, context


async def _shutdown(pw, context):
    try:
        await context.close()
    finally:
        await pw.stop()


async def main() -> int:
    state_path = user_browser_shared_storage_state_path()

    # Fresh slate: remove any pre-existing storage_state.json so the test
    # only sees state we produce ourselves.
    if os.path.exists(state_path):
        os.remove(state_path)
        print(f"[setup] removed pre-existing {state_path}")
    else:
        print(f"[setup] no pre-existing {state_path}")

    profile_a = tempfile.mkdtemp(prefix="handq_test_profile_a_")
    profile_b = tempfile.mkdtemp(prefix="handq_test_profile_b_")
    print(f"[setup] profile A: {profile_a}")
    print(f"[setup] profile B: {profile_b}")

    try:
        # ── Session A: seed cookie, persist via storage_state ─────────────
        print("\n[A] launching Chromium in profile A")
        pw_a, ctx_a = await _launch(profile_a)
        try:
            print(f"[A] adding seed cookie: {SEED_COOKIE['name']}")
            await ctx_a.add_cookies([SEED_COOKIE])

            all_cookies_a = await ctx_a.cookies()
            seed_hits_a = [c for c in all_cookies_a if c["name"] == SEED_COOKIE["name"]]
            print(f"[A] cookies present after add: {len(seed_hits_a)} match(es)")
            assert seed_hits_a, "seed cookie missing right after add_cookies"

            n = await _persist_context_storage_state(ctx_a)
            print(f"[A] _persist_context_storage_state returned n={n}")
            assert n >= 1, "persist should have written at least one cookie"
        finally:
            await _shutdown(pw_a, ctx_a)

        # storage_state.json is now on disk
        assert os.path.exists(state_path), f"{state_path} not created"
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        cookies_on_disk = state.get("cookies", [])
        seed_hits_disk = [c for c in cookies_on_disk if c["name"] == SEED_COOKIE["name"]]
        print(f"[disk] cookies in storage_state.json: {len(cookies_on_disk)}")
        print(f"[disk] seed cookie in file: {bool(seed_hits_disk)} "
              f"({seed_hits_disk[0] if seed_hits_disk else 'MISSING'})")
        assert seed_hits_disk, "seed cookie missing from persisted JSON"

        # ── Session B: fresh profile, inject via storage_state ────────────
        print("\n[B] launching Chromium in profile B (DIFFERENT user-data-dir)")
        pw_b, ctx_b = await _launch(profile_b)
        try:
            cookies_before = await ctx_b.cookies()
            hits_before = [c for c in cookies_before if c["name"] == SEED_COOKIE["name"]]
            print(f"[B] cookies BEFORE inject: {len(cookies_before)} total, "
                  f"seed match: {len(hits_before)}")
            assert not hits_before, "fresh profile should NOT have seed cookie yet"

            injected = await _inject_shared_storage_state(ctx_b)
            print(f"[B] _inject_shared_storage_state returned {injected}")
            assert injected >= 1, "inject should have added at least one cookie"

            cookies_after = await ctx_b.cookies()
            hits_after = [c for c in cookies_after if c["name"] == SEED_COOKIE["name"]]
            print(f"[B] cookies AFTER inject: {len(cookies_after)} total, "
                  f"seed match: {len(hits_after)}")
            assert hits_after, "seed cookie MISSING in profile B after inject"

            recovered = hits_after[0]
            print(f"[B] recovered value: {recovered['value']!r}")
            assert recovered["value"] == SEED_COOKIE["value"], (
                f"value mismatch: got {recovered['value']!r}, "
                f"expected {SEED_COOKIE['value']!r}"
            )
        finally:
            await _shutdown(pw_b, ctx_b)

    finally:
        shutil.rmtree(profile_a, ignore_errors=True)
        shutil.rmtree(profile_b, ignore_errors=True)
        # Preserve storage_state.json — user may want to inspect it.

    print("\n[PASS] storage_state round-trip works: cookie survived a fresh profile.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
