# -*- coding: utf-8 -*-
"""TeamsWebBridge — harvest access tokens from Microsoft Teams Web's
MSAL.js localStorage cache.

Why
===
We tried using Teams Web's refresh token through Microsoft's OAuth
token endpoint and got AADSTS700084: SPA refresh tokens have a hard
24-hour ceiling and Microsoft refuses to extend them via that path.
SPAs are designed to renew via cookie-bound silent iframe flows that
we can't replicate from outside the browser.

So we take the other side of MSAL.js's contract: it has *already*
minted access tokens on our behalf and stashed them in localStorage.
Those ATs are valid for ~1 hour; we read them out and use them
directly. When they expire we re-run this bridge — Teams Web's
session cookie outlives the AT (~weeks), so MSAL silently renews and
we read the new AT.

Concretely
----------
1. Reuse ``%USERPROFILE%/HandQ/browser_profile/`` (browser_tool's
   persistent context — already SSO'd to teams.microsoft.com).
2. Open teams.microsoft.com in Edge.
3. Wait for MSAL.js to publish at least one Graph access-token
   credential to localStorage. If none appears within a short window,
   navigate to /calendarv2 to nudge MSAL into requesting one.
4. Read all localStorage entries; structurally pick the AccessToken
   credentials (by ``credentialType`` field, NOT by key name).
5. Decode each token's JWT payload to learn its true audience and
   expiry; keep the one whose ``aud`` is ``graph.microsoft.com`` (and
   any other audiences the caller asked for).
6. Persist into ``teams_auth`` and return.

Concurrency / lifecycle
-----------------------
This module owns no global state. Each call launches a Playwright
context, extracts, closes the context. The caller (teams_tool's auth
gate) holds ``_teams_auth_lock`` so no two extractions run in
parallel — the persistent profile is locked while the context is
open and a second simultaneous launch would fail.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

from .logger import get_logger
from .teams_api import GRAPH_RESOURCE, get_teams_auth


_BROWSER_PROFILE_DIR = Path(os.path.expanduser("~")) / "HandQ" / "browser_profile"

# How long we'll wait for MSAL to publish a Graph AT after page load.
# Warm session: ~3 s. Cold session needing SSO redirect: 30-60 s.
# Fresh MFA / FIDO prompt: indefinite — the user is in control.
_MSAL_WAIT_TIMEOUT_S = 90
_MSAL_WAIT_TIMEOUT_INTERACTIVE_S = 300  # browser visible, user interacting
_RT_POLL_INTERVAL_S = 1.0

# After teams.microsoft.com loads, MSAL may have an AT for chatsvcagg
# but not for graph.microsoft.com (it acquires the latter only when
# Teams Web itself needs to call Graph — calendar / people search /
# files preview). To force MSAL to request a Graph AT, navigate to
# the calendar route which triggers a Graph permission request.
_CALENDAR_NUDGE_URL = "https://teams.microsoft.com/v2/?ucwa.anonymous=false#/calendarv2"

# When MSAL has cached a refresh_token_expired error in
# ``tmp.auth.v1.*`` entries it short-circuits subsequent acquireToken
# calls without retrying. This JS clears those error caches and
# forces a hard reload so MSAL has to ask Microsoft fresh (which then
# triggers user-visible sign-in when the SPA RT is genuinely dead).
_FORCE_REAUTH_JS = """
(() => {
    let cleared = 0;
    for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.indexOf('tmp.auth.v1.') === 0) {
            try { localStorage.removeItem(k); cleared++; } catch (_) {}
        }
    }
    return cleared;
})()
"""

# Routes we navigate during bootstrap, in order. Each route nudges
# Teams Web's MSAL to acquire a token for a different audience:
#
#   /calendarv2     → graph.microsoft.com (Calendars.ReadWrite scope)
#   /conversations  → chatsvcagg.teams.microsoft.com (chat threads)
#                     + presence.teams.microsoft.com (online indicators
#                     are rendered next to each chat in the list)
#   (homepage)      → already covered above by the initial goto
#
# Visiting these in turn after the initial page load makes MSAL fan
# out and request the full audience set we'll need. Without this,
# a user whose Teams Web session never opened the chat tab would have
# no presence token in their cache, and ``get_presence`` would 401.
_NUDGE_ROUTES = (
    "https://teams.microsoft.com/v2/?ucwa.anonymous=false#/calendarv2",
    "https://teams.microsoft.com/v2/?ucwa.anonymous=false#/conversations",
)

# The audiences we want to harvest in addition to Graph. Teams' own
# internal API and presence service get their own ATs that we can
# reuse for the read_chat / presence actions later.
_EXTRA_AUDIENCES = (
    "https://chatsvcagg.teams.microsoft.com",
    "https://presence.teams.microsoft.com",
)


class BootstrapError(RuntimeError):
    """Raised when extraction fails. ``code`` describes the cause:

      * ``playwright_missing``  — playwright not installed
      * ``profile_locked``      — browser_profile already in use
      * ``profile_missing``     — browser_profile does not exist
      * ``no_graph_token``      — Teams Web didn't acquire a Graph AT
                                  (e.g. user not signed in, or session
                                  cookie expired requiring fresh MFA)
      * ``parse_failed``        — found no AccessToken credentials at
                                  all (MSAL.js storage shape may have
                                  changed)
    """

    def __init__(self, message: str, code: str, **diag: Any) -> None:
        super().__init__(message)
        self.code = code
        self.diag = diag


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    """Best-effort JWT payload decode."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:
        return {}


def _normalize_audience(aud_raw: Any) -> str:
    """Normalize a JWT ``aud`` claim to a comparable resource URI.

    The audience may arrive as a bare hostname (``graph.microsoft.com``)
    or with a scheme (``https://graph.microsoft.com``). We canonicalize
    to ``https://<host>`` so cache keys match.
    """
    if not aud_raw:
        return ""
    aud = str(aud_raw).rstrip("/")
    if "://" in aud:
        return aud
    return f"https://{aud}"


def parse_msal_access_tokens(
    storage: Dict[str, str],
    *,
    wanted: Iterable[str] = (GRAPH_RESOURCE, *_EXTRA_AUDIENCES),
) -> Dict[str, Dict[str, Any]]:
    """Extract usable access tokens by JSON shape, not by key pattern.

    Walks every localStorage value. Picks values where ``credentialType``
    is ``AccessToken`` and JWT decoding shows ``aud`` matches one of
    ``wanted``. Keeps the latest non-expired AT per audience (latest
    ``exp``).

    Returns ``{audience: {access_token, expires_at, scopes, account, username}, ...}``.
    Empty dict when no tokens of any wanted audience were found.
    """
    wanted_set: Set[str] = {_normalize_audience(a) for a in wanted}
    by_aud: Dict[str, Dict[str, Any]] = {}
    now = int(time.time())

    for raw in storage.values():
        try:
            v = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(v, dict):
            continue
        if v.get("credentialType") != "AccessToken":
            continue
        secret = v.get("secret") or ""
        if not secret:
            continue

        payload = _decode_jwt_payload(secret)
        if not payload:
            continue
        aud = _normalize_audience(payload.get("aud"))
        if aud not in wanted_set:
            continue

        exp = int(payload.get("exp") or 0)
        if exp <= now:
            continue  # already expired

        prev = by_aud.get(aud)
        if prev and prev.get("expires_at", 0) >= exp:
            continue  # we already have a fresher one for this audience

        scp = payload.get("scp") or ""
        upn = (
            payload.get("upn")
            or payload.get("preferred_username")
            or payload.get("unique_name")
            or ""
        )
        # MSAL also stores homeAccountId on the credential record itself;
        # fall back to the JWT's oid+tid.
        account = v.get("homeAccountId") or ""
        if not account and payload.get("oid") and payload.get("tid"):
            account = f"{payload['oid']}.{payload['tid']}"

        by_aud[aud] = {
            "access_token": secret,
            "expires_at":   exp,
            "scopes":       scp,
            "account":      account,
            "username":     upn,
        }

    return by_aud


def parse_identity(storage: Dict[str, str]) -> Dict[str, str]:
    """Pull client_id / tenant_id / account / username from any MSAL
    credential entry — same shape across AT / IdToken / Account.

    Returns the first match; missing fields default to "".
    """
    out = {"client_id": "", "tenant_id": "", "account": "", "username": ""}
    for raw in storage.values():
        try:
            v = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(v, dict):
            continue
        ct = v.get("credentialType") or ""
        # Account record (no credentialType but has homeAccountId + realm + username)
        if not ct and v.get("homeAccountId") and v.get("realm"):
            out["account"]   = out["account"]   or v.get("homeAccountId") or ""
            out["tenant_id"] = out["tenant_id"] or v.get("realm")         or ""
            out["username"]  = out["username"]  or v.get("username")      or ""
            continue
        # AT / IdToken / RefreshToken record carries clientId.
        if ct in ("AccessToken", "IdToken", "RefreshToken"):
            out["client_id"] = out["client_id"] or v.get("clientId")      or ""
            out["account"]   = out["account"]   or v.get("homeAccountId") or ""

    # Fallback for tenant_id from "<oid>.<tid>" account string.
    if not out["tenant_id"] and out["account"] and "." in out["account"]:
        out["tenant_id"] = out["account"].split(".", 1)[1]
    return out


async def _read_storage(page) -> Dict[str, str]:
    return await page.evaluate("""() => {
        const out = {};
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            out[k] = localStorage.getItem(k);
        }
        return out;
    }""")


async def _wait_for_graph_token(
    page, timeout_s: int = _MSAL_WAIT_TIMEOUT_S,
) -> Dict[str, str]:
    """Poll localStorage until at least one non-expired Graph AT shows
    up, or timeout. Returns the final localStorage snapshot regardless
    so the caller can still try to extract whatever's there.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    storage: Dict[str, str] = {}
    while asyncio.get_event_loop().time() < deadline:
        try:
            storage = await _read_storage(page)
        except Exception:
            storage = {}
        graph = parse_msal_access_tokens(storage, wanted=[GRAPH_RESOURCE]).get(GRAPH_RESOURCE)
        if graph:
            return storage
        await asyncio.sleep(_RT_POLL_INTERVAL_S)
    return storage


async def bootstrap_from_teams_web(
    *,
    headless: bool = False,
    profile_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Open Teams Web in browser_profile and harvest access tokens.

    Caller responsibilities:
      * hold ``_teams_auth_lock`` so two extractions cannot collide;
      * inform the user a browser is about to open (best-effort).

    Side effects: writes the harvested tokens into ``teams_auth``'s
    on-disk cache via ``install_bootstrap``.

    Raises BootstrapError on any failure.
    """
    logger = get_logger()
    profile = profile_dir or _BROWSER_PROFILE_DIR

    if not profile.exists():
        raise BootstrapError(
            f"browser_profile does not exist at {profile}. "
            "Use the browser tool at least once to create it.",
            code="profile_missing",
        )

    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BootstrapError(
            "playwright is not installed. Run: pip install playwright "
            "&& playwright install chromium",
            code="playwright_missing",
        ) from exc

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="msedge",
                headless=headless,
                # browser_tool deliberately hides its window off-screen
                # for unobtrusive automation. The profile remembers that
                # position, so a fresh launch on the same profile would
                # also pop off-screen — invisible to the user who needs
                # to complete sign-in. Force a centred, on-screen
                # placement here. We use a 1200x900 window roughly
                # centred for typical 1920x1080 / 2560x1440 displays.
                args=[
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--window-position=200,100",
                    "--window-size=1200,900",
                ],
            )
        except Exception as exc:
            raise BootstrapError(
                f"could not launch Edge with browser_profile: {exc}. "
                "If browser_tool is currently running, close it first.",
                code="profile_locked",
            ) from exc

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            # Pull the window to the foreground so the user actually
            # sees the sign-in prompt. Best-effort — some platforms
            # ignore this, but Windows/Edge respects it.
            try:
                await page.bring_to_front()
            except Exception:
                pass

            try:
                await page.goto(
                    "https://teams.microsoft.com",
                    wait_until="load",
                    timeout=60_000,
                )
            except Exception as exc:
                logger.warning(
                    f"teams.microsoft.com nav warning (non-fatal): {exc}",
                    component="TeamsWebBridge",
                )

            # First wait — common case: Graph AT shows up within 5-10 s
            # of page load if the user has visited Calendar / Files
            # recently. We give it a short window.
            storage = await _wait_for_graph_token(page, timeout_s=15)
            graph_present = bool(
                parse_msal_access_tokens(storage, wanted=[GRAPH_RESOURCE]).get(GRAPH_RESOURCE)
            )

            # Walk through the nudge routes — each forces MSAL to mint
            # a different audience token. We do this even when the
            # Graph token already showed up, because we also want
            # presence + chatsvcagg tokens populated for read_chat /
            # get_presence to work without a second bootstrap.
            for route in _NUDGE_ROUTES:
                # Skip /calendarv2 if Graph AT is already in storage
                # to save 5-10s on warm sessions.
                if route.endswith("calendarv2") and graph_present:
                    continue
                logger.info(
                    f"teams web bridge: nudging via {route.split('#')[-1]}",
                    component="TeamsWebBridge",
                )
                try:
                    await page.goto(route, wait_until="load", timeout=30_000)
                except Exception as exc:
                    logger.warning(
                        f"nudge nav warning ({route}): {exc}",
                        component="TeamsWebBridge",
                    )
                # Short wait — only to let MSAL kick off acquisitions;
                # we'll do the real polling below.
                await asyncio.sleep(5)

            # Final wait — give MSAL a fair window to publish all
            # audiences. Per-token expiries vary; we re-poll the
            # localStorage view rather than wait for any single key.
            storage = await _wait_for_graph_token(page, timeout_s=30)
            graph_present = bool(
                parse_msal_access_tokens(storage, wanted=[GRAPH_RESOURCE]).get(GRAPH_RESOURCE)
            )

            # Still nothing — MSAL has likely cached a
            # 'refresh_token_expired' error and won't retry until the
            # user signs in again. Clear the error cache, hard-reload,
            # and wait a long time for the user to complete MFA in the
            # visible browser window.
            if not graph_present:
                logger.warning(
                    "*** TEAMS BOOTSTRAP NEEDS USER ATTENTION ***\n"
                    "    Your Microsoft Teams session has expired and MSAL "
                    "cannot renew silently. An Edge window has opened on "
                    "teams.microsoft.com — please complete sign-in (and MFA "
                    "if prompted) there. The bootstrap will continue "
                    "automatically once Teams loads. Waits up to 5 minutes.",
                    component="TeamsWebBridge",
                )
                try:
                    cleared = await page.evaluate(_FORCE_REAUTH_JS)
                    logger.info(
                        f"cleared {cleared} stale MSAL error entries",
                        component="TeamsWebBridge",
                    )
                except Exception as exc:
                    logger.warning(
                        f"could not clear MSAL error cache: {exc}",
                        component="TeamsWebBridge",
                    )
                # Hard reload to force MSAL to actually re-attempt auth
                # rather than serve a cached app shell.
                try:
                    await page.goto(
                        "https://teams.microsoft.com/v2/",
                        wait_until="load",
                        timeout=30_000,
                    )
                except Exception as exc:
                    logger.warning(
                        f"reload warning (non-fatal): {exc}",
                        component="TeamsWebBridge",
                    )
                storage = await _wait_for_graph_token(
                    page, timeout_s=_MSAL_WAIT_TIMEOUT_INTERACTIVE_S,
                )

            tokens = parse_msal_access_tokens(storage)
            identity = parse_identity(storage)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    if not tokens:
        raise BootstrapError(
            "MSAL.js localStorage contained no usable AccessToken "
            "credentials. The page may not have finished signing in, "
            "or MSAL.js's storage shape changed (re-run "
            "scripts/verify_teams_token_storage.py to inspect).",
            code="parse_failed",
        )
    if GRAPH_RESOURCE not in tokens:
        raise BootstrapError(
            "Teams Web did not acquire a Microsoft Graph access token "
            f"within {_MSAL_WAIT_TIMEOUT_S}s. The user may need to "
            "sign in to teams.microsoft.com manually first, or the "
            "tenant policy may be blocking Graph access.",
            code="no_graph_token",
            audiences_seen=list(tokens.keys()),
        )

    # Borrow identity fields from the Graph AT first (it has the freshest
    # JWT-derived UPN), fall back to the storage scan.
    g = tokens[GRAPH_RESOURCE]
    creds = {
        "client_id": identity["client_id"],
        "tenant_id": identity["tenant_id"],
        "account":   g.get("account") or identity["account"],
        "username":  g.get("username") or identity["username"],
        "tokens":    tokens,
    }

    auth = get_teams_auth()
    auth.install_bootstrap(creds)

    logger.info(
        f"TeamsWebBridge: bootstrap succeeded for "
        f"{creds.get('username') or creds.get('account')} "
        f"(audiences: {sorted(tokens.keys())})",
        component="TeamsWebBridge",
    )
    return creds


# Backwards-compat alias: the previous version exposed
# ``extract_refresh_token`` from this module. Keep the name working so
# any in-flight call sites don't 500 mid-rollout.
extract_refresh_token = bootstrap_from_teams_web
