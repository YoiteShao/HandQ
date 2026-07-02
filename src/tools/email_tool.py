# -*- coding: utf-8 -*-
"""Email tool — read local Outlook mail via win32com COM automation.

Architecture
------------
Outlook's ``Outlook.Application`` COM interface is STA (Single-Threaded
Apartment).  All COM calls are serialised through a one-worker
``ThreadPoolExecutor`` whose sole thread is COM-initialised via
``pythoncom.CoInitialize()`` in its initializer.  The async ``execute``
method dispatches to ``_action_*()`` helpers that each call
``loop.run_in_executor(_outlook_executor, _sync_fn, params)`` and
``await`` the result.  An ``asyncio.Lock`` serialises concurrent tool
invocations.

Critical invariant: sync functions that run inside the executor MUST
convert every COM object to a plain Python type before returning.
CDispatch handles cannot safely cross thread boundaries.

DO NOT call ``app.Quit()`` — that closes the user's Outlook window.
"""
from __future__ import annotations

import atexit
import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base_tool import BaseTool, ToolResult
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.logger import get_logger


# ── Outlook COM class / folder constants ──────────────────────────────────────
_OL_MAIL_CLASS  = 43   # olMail
_OL_FOLDER_INBOX   = 6
_OL_FOLDER_SENT    = 5
_OL_FOLDER_DRAFTS  = 16
_OL_FOLDER_OUTBOX  = 4
_OL_FOLDER_DELETED = 3
_OL_FOLDER_JUNK    = 23

_WELL_KNOWN_FOLDERS: Dict[str, int] = {
    "inbox":         _OL_FOLDER_INBOX,
    "sent items":    _OL_FOLDER_SENT,
    "sent mail":     _OL_FOLDER_SENT,
    "drafts":        _OL_FOLDER_DRAFTS,
    "outbox":        _OL_FOLDER_OUTBOX,
    "deleted items": _OL_FOLDER_DELETED,
    "trash":         _OL_FOLDER_DELETED,
    "junk":          _OL_FOLDER_JUNK,
    "junk email":    _OL_FOLDER_JUNK,
    "spam":          _OL_FOLDER_JUNK,
}

_RECIPIENT_TO  = 1
_RECIPIENT_CC  = 2
_RECIPIENT_BCC = 3


# ── Single-threaded COM executor ───────────────────────────────────────────────
def _init_com() -> None:
    # STA: Outlook's COM collections (Folders, Items) are not thread-safe
    # from an MTA apartment — CoInitialize gives us STA per the ProgID spec.
    import pythoncom  # type: ignore[import-untyped]
    pythoncom.CoInitialize()


_outlook_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="outlook_com",
    initializer=_init_com,
)
_outlook_lock = asyncio.Lock()
_outlook_app = None   # lazy COM handle, created on first action


def _get_app():
    """Return Outlook.Application CDispatch. Runs INSIDE the executor thread.

    Late-binding only (``Dispatch``, not ``gencache.EnsureDispatch``): every
    method call goes through ``IDispatch::Invoke`` so we never touch the
    ``%TEMP%\\gen_py\\`` cache. That sidesteps the recurring "module
    'win32com.gen_py.<CLSID>' has no attribute 'CLSIDToPackageMap'" failure
    that surfaces when Office upgrades the Outlook typelib past what the
    on-disk makepy stub was generated against, or when prior runs left a
    half-written cache. All Outlook constants we use (``olMail=43``,
    ``olFolderInbox=6``, recipient types) are hard-coded above, so there is
    no ``win32com.client.constants`` dependency to lose.
    """
    global _outlook_app
    if _outlook_app is not None:
        return _outlook_app
    from win32com.client import Dispatch  # type: ignore[import-untyped]
    _outlook_app = Dispatch("Outlook.Application")
    return _outlook_app


def is_outlook_app_ready() -> bool:
    """True if the Outlook.Application handle is already cached.

    Read-safe from any thread — only inspects a module-level reference. Used
    by ``EmailContextProvider.prepare`` to skip the 5s smoke-test round-trip
    on every step's prepare after the first successful one in this process.
    """
    return _outlook_app is not None


def _shutdown() -> None:
    try:
        import pythoncom  # type: ignore[import-untyped]
        _outlook_executor.submit(pythoncom.CoUninitialize).result(timeout=2)
    except Exception:
        pass
    _outlook_executor.shutdown(wait=False)


atexit.register(_shutdown)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _to_iso(pywintime) -> Optional[str]:
    """Convert pywintypes.datetime (subclass of datetime) to ISO string."""
    if pywintime is None:
        return None
    try:
        return pywintime.isoformat()
    except AttributeError:
        return str(pywintime)


def _to_python_datetime(pywintime) -> Optional[datetime]:
    """Extract a tz-naive local Python datetime from a pywintypes.datetime."""
    if pywintime is None:
        return None
    try:
        return datetime(
            pywintime.year, pywintime.month, pywintime.day,
            pywintime.hour, pywintime.minute, pywintime.second,
        )
    except (AttributeError, ValueError):
        return None


def _recipients_of_type(item, rtype: int) -> List[str]:
    """Return SMTP addresses from item.Recipients filtered by Type constant."""
    result: List[str] = []
    try:
        for r in item.Recipients:
            try:
                if r.Type == rtype:
                    addr = str(r.Address or "")
                    if addr:
                        result.append(addr)
            except Exception:
                pass
    except Exception:
        pass
    return result


def _resolve_folder(namespace, folder_path: str):
    """Map a folder name or slash-separated path to a Folder COM object.

    Handles well-known shortcuts ("Inbox", "Sent Items", …) and nested
    paths ("Inbox/Project-Alpha").  All comparisons are case-insensitive.
    """
    parts = folder_path.strip("/\\").split("/")
    top_lower = parts[0].strip().lower()

    if len(parts) == 1 and top_lower in _WELL_KNOWN_FOLDERS:
        return namespace.GetDefaultFolder(_WELL_KNOWN_FOLDERS[top_lower])

    # Navigate from the store root (parent of the default Inbox)
    inbox = namespace.GetDefaultFolder(_OL_FOLDER_INBOX)
    current = inbox.Parent

    for part in parts:
        part_lower = part.strip().lower()
        found = None
        try:
            for sub in current.Folders:
                try:
                    if sub.Name.lower() == part_lower:
                        found = sub
                        break
                except Exception:
                    pass
        except Exception:
            pass
        if found is None:
            raise ValueError(
                f"Folder {folder_path!r} not found "
                f"(no child named {part!r} under "
                f"{getattr(current, 'Name', '?')!r})"
            )
        current = found
    return current


def _walk_folders(folder, max_depth: int = 4):
    """Yield ``folder`` then every descendant folder, depth-first.

    Bounded by ``max_depth`` to defuse pathological / cyclic stores. The
    default of 4 levels covers any realistic Outlook profile (rule-routed
    sub-folders rarely nest deeper); raise via ``email.max_recursion_depth``
    in handq_config.yaml when needed. Errors while iterating a sub-folder
    are swallowed so one bad branch doesn't abort the whole walk.
    """
    yield folder
    if max_depth <= 0:
        return
    try:
        sub_folders = folder.Folders
    except Exception:
        return
    try:
        for sub in sub_folders:
            try:
                yield from _walk_folders(sub, max_depth - 1)
            except Exception:
                continue
    except Exception:
        return


def _is_folder_blacklisted(name_or_path: str, blacklist) -> bool:
    """Case-insensitive deny-list check for folder access.

    Matches ``name_or_path`` against any blacklist entry as either the full
    string OR its trailing slash-segment, both lowercased and trimmed. So
    a blacklist of ``["HR"]`` blocks both an agent-passed
    ``folder='Inbox/HR'`` and a recursive walk's encounter of any folder
    literally named "HR" anywhere in the tree.

    Empty / None blacklist → always False (no restriction).
    """
    if not blacklist:
        return False
    s = (name_or_path or "").strip().lower()
    if not s:
        return False
    leaf = s.rsplit("/", 1)[-1].strip()
    for entry in blacklist:
        e = str(entry).strip().lower()
        if e and (e == s or e == leaf):
            return True
    return False


def _escape_dasl_like(s: str) -> str:
    """Escape user input for safe inclusion inside a DASL LIKE '%...%'.

    DASL LIKE follows Jet/Access syntax: ``%`` and ``_`` are wildcards;
    ``[`` opens a character class; ``'`` ends a string literal. To match
    these as literals we wrap each in ``[ ]`` (Jet's standard literal-char
    trick) and double the quote.

    Order is load-bearing: ``[`` must be remapped BEFORE we introduce new
    ``[`` chars via the ``%`` / ``_`` escapes, otherwise we'd re-escape
    our own escapes.
    """
    return (s
        .replace("'", "''")
        .replace("[", "[[]")
        .replace("%", "[%]")
        .replace("_", "[_]"))


def _escape_dasl_string(s: str) -> str:
    """Escape for a plain DASL string literal (no LIKE wildcard semantics).

    Used by ``ci_phrasematch`` operands where ``%`` / ``_`` / ``[`` are
    treated literally. Only ``'`` needs doubling.
    """
    return s.replace("'", "''")


def _folder_to_dict(folder, display_name: str) -> Dict[str, Any]:
    """Materialise a Folder COM object to a plain dict."""
    return {
        "name": display_name,
        "full_path": str(getattr(folder, "FolderPath", "") or ""),
        "item_count": int(folder.Items.Count),
        "unread_count": int(folder.UnReadItemCount),
    }


def _mail_item_to_summary(
    item, preview_chars: int, folder_path: str, include_body_preview: bool = True,
) -> Dict[str, Any]:
    """Return list_messages / search dict for a MailItem. All plain Python.

    ``folder_path`` is read once per folder by the caller and threaded through —
    avoids a per-item ``item.Parent.FolderPath`` round-trip (saves 2 COM calls
    per matched item).

    ``include_body_preview=False`` skips the ``item.Body`` property read, which
    is the single most expensive COM access per item (Body materialises the
    full body text — 50-150ms per HTML mail). Set False when the caller only
    needs metadata (subject / sender / date); body_preview is returned as ""
    in that case so the response shape stays stable.
    """
    if include_body_preview:
        body_preview = str(item.Body or "")[:preview_chars]
    else:
        body_preview = ""
    return {
        "entry_id": str(item.EntryID),
        "subject": str(item.Subject or ""),
        "sender_name": str(item.SenderName or ""),
        "sender_email": str(item.SenderEmailAddress or ""),
        "received_at": _to_iso(item.ReceivedTime),
        "is_read": not bool(item.UnRead),
        "has_attachments": bool(item.Attachments.Count > 0),
        "body_preview": body_preview,
        "folder": folder_path,
    }


def _mail_item_to_full(
    item,
    include_full_body: bool,
    include_attachments_meta: bool,
) -> Dict[str, Any]:
    """Return read_message dict. All values are plain Python types."""
    attachments: List[Dict[str, Any]] = []
    if include_attachments_meta:
        try:
            # 1-based enumerate aligns with Outlook's Attachments.Item(N)
            # COM accessor — the 'index' is the only unambiguous handle
            # when several attachments share a FileName (forwarded-mail
            # chains commonly do).
            for i, att in enumerate(item.Attachments, start=1):
                try:
                    attachments.append({
                        "index": i,
                        "name": str(att.FileName or ""),
                        "size": int(att.Size),
                        "content_type": "",   # MAPI property accessor needed for MIME type
                    })
                except Exception:
                    pass
        except Exception:
            pass

    body_text = str(item.Body or "")
    try:
        folder_path = str(item.Parent.FolderPath)
    except Exception:
        folder_path = ""

    return {
        "entry_id": str(item.EntryID),
        "subject": str(item.Subject or ""),
        "sender_name": str(item.SenderName or ""),
        "sender_email": str(item.SenderEmailAddress or ""),
        "to": _recipients_of_type(item, _RECIPIENT_TO),
        "cc": _recipients_of_type(item, _RECIPIENT_CC),
        "bcc": _recipients_of_type(item, _RECIPIENT_BCC),
        "received_at": _to_iso(item.ReceivedTime),
        "sent_at": _to_iso(item.SentOn),
        "is_read": not bool(item.UnRead),
        "body_preview": body_text[:500],
        "body": body_text if include_full_body else None,
        "body_html": str(item.HTMLBody or "") if include_full_body else None,
        "folder": folder_path,
        "attachments": attachments,
        "conversation_topic": str(item.ConversationTopic or ""),
    }


def _safe_attachment_path(
    name: str,
    output_dir: Optional[str],
    sandbox: str,
) -> Path:
    """Return a validated absolute path for saving an attachment.

    Rejects names with directory-traversal components and output_dir values
    outside the attachment sandbox.
    """
    safe_name = Path(name).name
    if not safe_name or safe_name in (".", ".."):
        raise ValueError(
            "attachment_name is empty or stripped to a directory separator "
            "after sanitisation — possible path traversal attempt"
        )

    sandbox_resolved = Path(os.path.expandvars(sandbox)).resolve()
    target_dir = Path(output_dir).resolve() if output_dir else sandbox_resolved

    is_under_sandbox = (
        target_dir == sandbox_resolved
        or sandbox_resolved in target_dir.parents
    )
    if not is_under_sandbox:
        raise ValueError(
            f"output_dir {target_dir} is outside the attachment sandbox "
            f"({sandbox_resolved}). "
            "Set config.email.attachment_sandbox or use a path under it."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / safe_name


# ── Sync action functions (all run inside _outlook_executor thread) ────────────
def _sync_list_folders(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")

    parent_path: Optional[str] = params.get("parent")
    if parent_path:
        root = _resolve_folder(namespace, parent_path)
    else:
        root = namespace.GetDefaultFolder(_OL_FOLDER_INBOX).Parent  # store root

    folders: List[Dict[str, Any]] = []

    # Always surface the well-known default folders at top level
    if not parent_path:
        for const, display in (
            (_OL_FOLDER_INBOX,   "Inbox"),
            (_OL_FOLDER_SENT,    "Sent Items"),
            (_OL_FOLDER_DRAFTS,  "Drafts"),
            (_OL_FOLDER_OUTBOX,  "Outbox"),
            (_OL_FOLDER_DELETED, "Deleted Items"),
            (_OL_FOLDER_JUNK,    "Junk Email"),
        ):
            try:
                folders.append(_folder_to_dict(namespace.GetDefaultFolder(const), display))
            except Exception:
                pass

    if params.get("recursive"):
        try:
            for sub in root.Folders:
                try:
                    folders.append(_folder_to_dict(sub, sub.Name))
                except Exception:
                    pass
        except Exception:
            pass

    return {"folders": folders}


def _sync_status(params: Dict[str, Any]) -> Dict[str, Any]:
    """Zero-friction readiness probe — open Outlook, resolve Inbox, count
    today's messages.

    Returns a small payload the LLM can use as a "the tool works" signal
    before issuing a full ``list_messages`` call. Counting is bounded so
    a 50k-message inbox does not stall the call: iterate newest-first and
    stop once we walk past today's start.
    """
    app = _get_app()
    namespace = app.GetNamespace("MAPI")
    inbox = _resolve_folder(namespace, "Inbox")

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = 0
    cap = 500  # newest-first walk; an inbox with >500 messages today is implausible
    items = inbox.Items
    try:
        items.Sort("[ReceivedTime]", True)  # newest first
    except Exception:
        pass
    seen = 0
    for item in items:
        seen += 1
        if seen > cap:
            break
        try:
            received = item.ReceivedTime
            if received is None:
                continue
            received_dt = datetime(
                received.year, received.month, received.day,
                received.hour, received.minute, received.second,
            )
        except Exception:
            continue
        if received_dt < today_start:
            break
        today_count += 1

    return {
        "outlook_ready": True,
        "default_folder": "Inbox",
        "today_start": today_start.isoformat(),
        "today_count": today_count,
        "next_step": (
            "Call action='list_messages' with since='today' to retrieve "
            "today's messages."
        ),
    }


def _build_dasl_prefilter(
    since_dt: Optional[datetime],
    unread_only: bool,
    sender_q: str = "",
    subject_q: str = "",
) -> Optional[str]:
    """Build a DASL @SQL filter for the index-side pre-filters of list_messages.

    DASL (vs. Jet syntax) is locale-invariant — Jet's date format depends on
    Windows regional settings, which would break ``[ReceivedTime] >= '...'``
    on non-en-US machines. Returns None when no pre-filter applies.

    ``sender_q`` / ``subject_q`` push the LIKE match for sender_contains /
    subject_contains down to the DASL index. Previously these ran as
    Python-side post-filters, which meant per-item COM round-trips for
    SenderName / SenderEmailAddress / Subject on every mail in scope —
    on the 20260609-122314 run that took 13m34s for one Inbox-recursive
    sweep over a 927-mail mailbox. Pushing to DASL on the indexed
    fromname / fromemail / subject fields turns the same query into a
    sub-second lookup.
    """
    parts: List[str] = []
    if since_dt is not None:
        parts.append(
            f"\"urn:schemas:httpmail:datereceived\" >= "
            f"'{since_dt.strftime('%Y/%m/%d %H:%M:%S')}'"
        )
    if unread_only:
        parts.append("\"urn:schemas:httpmail:read\" = 0")
    if sender_q:
        s_esc = _escape_dasl_like(sender_q)
        parts.append(
            f"(\"urn:schemas:httpmail:fromname\" LIKE '%{s_esc}%' "
            f"OR \"urn:schemas:httpmail:fromemail\" LIKE '%{s_esc}%')"
        )
    if subject_q:
        sub_esc = _escape_dasl_like(subject_q)
        parts.append(
            f"\"urn:schemas:httpmail:subject\" LIKE '%{sub_esc}%'"
        )
    if not parts:
        return None
    return "@SQL=" + " AND ".join(parts)


def _scan_folder_messages(
    items,
    *,
    folder_path: str,
    since_dt: Optional[datetime],
    sender_filter: str,
    subject_filter: str,
    unread_only: bool,
    preview_chars: int,
    include_body_preview: bool,
    cap: int,
    fallback_python_filter: bool,
) -> tuple:
    """Per-folder message scan honoring all list_messages filters.

    ``items`` is the already-Restricted collection from the caller (so the
    DASL prefilter for since + unread is applied index-side). When the
    Restrict failed and the caller passed the unfiltered collection, set
    ``fallback_python_filter=True`` to re-enable the Python-side since /
    unread guard inside the loop.

    ``folder_path`` is read once by the caller from the folder handle and
    threaded through to ``_mail_item_to_summary`` — avoids per-item
    ``item.Parent.FolderPath`` round-trips.

    Returns ``(summaries, hit_cap)`` — ``hit_cap`` is True iff iteration
    stopped because the cap was reached (i.e. there were more matching
    candidates we didn't materialise). Callers use it to drive the
    response-level ``truncated`` flag.
    """
    try:
        items.Sort("[ReceivedTime]", True)  # newest first
    except Exception:
        pass

    out: List[Dict[str, Any]] = []
    hit_cap = False
    for item in items:
        if len(out) >= cap:
            hit_cap = True
            break
        try:
            if item.Class != _OL_MAIL_CLASS:
                continue
        except Exception:
            continue

        if fallback_python_filter:
            # DASL Restrict failed — re-apply ALL filters in Python:
            # since, unread, sender, subject. Each costs a COM round-trip
            # per item (SenderName / SenderEmailAddress / Subject), so
            # this path is the slow one; the DASL prefilter is the fast
            # path and should be preferred whenever Outlook accepts it.
            if unread_only:
                try:
                    if not bool(item.UnRead):
                        continue
                except Exception:
                    continue
            if since_dt:
                rt = _to_python_datetime(item.ReceivedTime)
                if rt is None:
                    continue
                if rt < since_dt:
                    # Items are sorted newest-first → break on first miss
                    break
            if sender_filter:
                try:
                    sn = (item.SenderName or "").lower()
                    se = (item.SenderEmailAddress or "").lower()
                    if sender_filter not in sn and sender_filter not in se:
                        continue
                except Exception:
                    continue
            if subject_filter:
                try:
                    if subject_filter not in (item.Subject or "").lower():
                        continue
                except Exception:
                    continue

        try:
            out.append(_mail_item_to_summary(
                item, preview_chars, folder_path, include_body_preview,
            ))
        except Exception:
            pass

    return out, hit_cap


def _parse_since(value: Any) -> Optional[datetime]:
    """Resolve ``since`` to a datetime; accepts ISO strings and magic words.

    Magic values (case-insensitive):
      - ``today`` / ``now`` — start of today, local time
      - ``yesterday`` — start of yesterday
      - ``24h`` / ``1d`` / ``last_24h`` — 24 hours ago
      - ``this_week`` / ``week`` — start of the current ISO week (Monday)

    Returns ``None`` for empty / unparseable input so callers can apply their
    own default. The magic-word handling exists so the LLM does not have to
    compute ISO timestamps for the common "emails since today" case — which
    was the failure mode in session 20260618-234346 where the agent skipped
    the email tool entirely rather than synthesise an ISO string.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    low = s.lower()
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if low in ("today", "now"):
        return midnight
    if low == "yesterday":
        return midnight - timedelta(days=1)
    if low in ("24h", "1d", "last_24h"):
        return now - timedelta(hours=24)
    if low in ("this_week", "week"):
        return midnight - timedelta(days=now.weekday())
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except (ValueError, TypeError):
        return None


def _sync_list_messages(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")

    folder_name: str = params.get("folder") or "Inbox"

    # Folder blacklist check. Applies to the agent-passed root and to every
    # sub-folder visited during the recursive walk. Empty/null blacklist
    # disables the gate entirely (no restriction).
    blacklist = params.get("_folder_blacklist") or []
    if _is_folder_blacklisted(folder_name, blacklist):
        raise ValueError(
            f"Folder {folder_name!r} is in email.folder_blacklist; "
            f"access denied."
        )

    root_folder = _resolve_folder(namespace, folder_name)

    since_dt: Optional[datetime] = _parse_since(params.get("since"))

    sender_filter = (params.get("sender_contains") or "").lower()
    subject_filter = (params.get("subject_contains") or "").lower()
    unread_only = bool(params.get("unread_only"))
    limit = min(int(params.get("limit") or 50), 200)
    preview_chars = int(params.get("_preview_chars") or 500)
    max_depth = int(params.get("_max_depth") or 4)
    # Default True for backward compatibility — agents can opt out via the
    # public include_body_preview param when only metadata is needed (saves
    # ~50-150ms per item by skipping the Body COM materialisation).
    include_body_preview = bool(
        True if params.get("include_body_preview") is None
        else params["include_body_preview"]
    )

    # list_messages defaults to recursive=true: enterprise mailboxes commonly
    # route incoming mail into Inbox sub-folders via rules, and a single-folder
    # scan of "Inbox" silently misses those.
    recursive_param = params.get("recursive")
    recursive = True if recursive_param is None else bool(recursive_param)

    folders_to_scan = (
        list(_walk_folders(root_folder, max_depth=max_depth))
        if recursive else [root_folder]
    )
    if blacklist:
        folders_to_scan = [
            f for f in folders_to_scan
            if not _is_folder_blacklisted(getattr(f, "Name", "") or "", blacklist)
        ]

    # All four filters (since, unread, sender, subject) are pushed to
    # DASL when possible — that turns 1000-mail recursive scans from
    # 13-minute Python-side enumerations into sub-second indexed lookups.
    # The Python-side filters in _scan_folder_messages only run when
    # Restrict raises (fallback_python_filter), so total_estimated is
    # exact whenever DASL succeeds and None only on the fallback path.
    dasl_prefilter = _build_dasl_prefilter(
        since_dt,
        unread_only,
        sender_q=sender_filter,
        subject_q=subject_filter,
    )
    total_estimated: Optional[int] = 0

    merged: List[Dict[str, Any]] = []
    any_folder_hit_cap = False
    for fld in folders_to_scan:
        # E4: skip empty folders before paying for Restrict + Sort.
        try:
            if int(fld.Items.Count) == 0:
                continue
        except Exception:
            pass

        try:
            folder_path = str(getattr(fld, "FolderPath", "") or "")
        except Exception:
            folder_path = ""

        # E1+E2: apply DASL prefilter once and share the Restricted collection
        # for both count and scan. Falls back to the unfiltered Items if
        # Restrict raises — _scan_folder_messages re-applies since/unread in
        # Python in that case.
        try:
            items = fld.Items
        except Exception:
            continue

        fallback_python_filter = False
        if dasl_prefilter:
            try:
                items = items.Restrict(dasl_prefilter)
            except Exception:
                fallback_python_filter = True

        if total_estimated is not None:
            if fallback_python_filter:
                # Restrict failed → items.Count would over-count (it includes
                # items that don't match the prefilter). Disable total tracking.
                total_estimated = None
            else:
                try:
                    folder_total = int(items.Count)
                except Exception:
                    folder_total = -1
                if folder_total < 0:
                    total_estimated = None  # one failed count poisons the sum
                else:
                    total_estimated += folder_total

        try:
            results, hit_cap = _scan_folder_messages(
                items,
                folder_path=folder_path,
                since_dt=since_dt,
                sender_filter=sender_filter,
                subject_filter=subject_filter,
                unread_only=unread_only,
                preview_chars=preview_chars,
                include_body_preview=include_body_preview,
                cap=limit,
                fallback_python_filter=fallback_python_filter,
            )
        except Exception:
            continue
        merged.extend(results)
        if hit_cap:
            any_folder_hit_cap = True

    # ISO-8601 strings sort lexicographically as dates → desc by received_at
    merged.sort(key=lambda m: m.get("received_at") or "", reverse=True)
    pre_limit_count = len(merged)
    merged = merged[:limit]

    truncated = (
        any_folder_hit_cap
        or pre_limit_count > limit
        or (total_estimated is not None and total_estimated > len(merged))
    )

    return {
        "count": len(merged),
        "messages": merged,
        "folders_scanned": len(folders_to_scan),
        "recursive": recursive,
        "truncated": truncated,
        "total_estimated": total_estimated,
    }


def _sync_read_message(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")
    item = namespace.GetItemFromID(params["entry_id"])
    if item.Class != _OL_MAIL_CLASS:
        raise ValueError(
            f"Entry ID {params['entry_id']!r} is not a mail item "
            f"(class={item.Class})"
        )
    return _mail_item_to_full(
        item,
        include_full_body=bool(params.get("include_full_body")),
        include_attachments_meta=params.get("include_attachments_meta", True),
    )


def _sync_search(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")

    folder_name: str = params.get("folder") or "Inbox"

    blacklist = params.get("_folder_blacklist") or []
    if _is_folder_blacklisted(folder_name, blacklist):
        raise ValueError(
            f"Folder {folder_name!r} is in email.folder_blacklist; "
            f"access denied."
        )

    root_folder = _resolve_folder(namespace, folder_name)

    query = str(params.get("query") or "")
    sender_q = (params.get("sender_contains") or "").strip()
    match_mode = str(params.get("match_mode") or "phrase").lower()
    if match_mode not in ("phrase", "substring"):
        match_mode = "phrase"

    dasl_parts: List[str] = []

    if query:
        if match_mode == "phrase":
            # ci_phrasematch hits the Windows Search content index — much
            # faster than a LIKE scan on body text. Word-level matching:
            # 'fail' matches the word 'fail' but not 'failed'. Switch to
            # match_mode='substring' when literal partial-word match is
            # needed, or when WDS isn't current and ci_phrasematch returns
            # empty.
            q_esc = _escape_dasl_string(query)
            dasl_parts.append(
                f"(\"urn:schemas:httpmail:subject\" ci_phrasematch '{q_esc}' "
                f"OR \"urn:schemas:httpmail:textdescription\" ci_phrasematch '{q_esc}')"
            )
        else:
            # 'substring': LIKE on subject only. Body LIKE
            # ('textdescription LIKE %x%') is a row scan that opens every
            # message and materialises its body sequentially on Outlook's
            # STA UI thread — that path was observed in the 20260609-111806
            # incident to hang Outlook for 4+ minutes with no way to
            # interrupt. To match against body, use match_mode='phrase'
            # (ci_phrasematch hits the Windows Search content index) or
            # add sender_contains to keep the candidate set small.
            q_esc = _escape_dasl_like(query)
            dasl_parts.append(
                f"\"urn:schemas:httpmail:subject\" LIKE '%{q_esc}%'"
            )

    if sender_q:
        # Sender always uses LIKE: email addresses tokenise badly under
        # ci_phrasematch (alice@example.com → 'alice', 'example', 'com'),
        # and sender fields are short + indexed so LIKE is cheap regardless.
        s_esc = _escape_dasl_like(sender_q)
        dasl_parts.append(
            f"(\"urn:schemas:httpmail:fromname\" LIKE '%{s_esc}%' "
            f"OR \"urn:schemas:httpmail:fromemail\" LIKE '%{s_esc}%')"
        )

    # `since` defaults to 365 days back when not supplied. Without a date
    # lower bound the DASL query enumerates the full mail history; on a
    # 925-mail Inbox plus archive subtree that materialises as tens of
    # seconds of synchronous COM round-trips on Outlook's UI thread —
    # the symptom that surfaced in the 20260608-162233 run as "Outlook
    # is not responding". 365d covers the vast majority of "find recent
    # mail from X" queries; an agent that needs older mail must pass
    # `since` explicitly.
    since_dt: Optional[datetime] = _parse_since(params.get("since"))
    if since_dt is None:
        since_dt = datetime.now() - timedelta(days=365)
    dasl_parts.append(
        f"\"urn:schemas:httpmail:datereceived\" >= "
        f"'{since_dt.strftime('%Y/%m/%d %H:%M:%S')}'"
    )

    dasl = "@SQL=" + " AND ".join(dasl_parts)

    limit = min(int(params.get("limit") or 20), 100)
    preview_chars = int(params.get("_preview_chars") or 500)
    max_depth = int(params.get("_max_depth") or 4)
    include_body_preview = bool(
        True if params.get("include_body_preview") is None
        else params["include_body_preview"]
    )

    # search defaults to recursive=False after the 20260608 incident.
    # The previous default (True) walked every sub-folder under the
    # resolved root, and on enterprise mailboxes with rule-routed
    # sub-folders that meant ~30+ folders × Restrict + Sort + Count,
    # all synchronous on Outlook's UI thread, giving a multi-minute
    # "Outlook not responding" UI hang. Agents that genuinely need to
    # scan sub-folders must pass recursive=True explicitly — the
    # folder-context already advertises common sub-folder paths so the
    # agent typically knows which leaf to target.
    recursive_param = params.get("recursive")
    recursive = False if recursive_param is None else bool(recursive_param)

    folders_to_scan = (
        list(_walk_folders(root_folder, max_depth=max_depth))
        if recursive else [root_folder]
    )
    if blacklist:
        folders_to_scan = [
            f for f in folders_to_scan
            if not _is_folder_blacklisted(getattr(f, "Name", "") or "", blacklist)
        ]

    # search has no Python-side post-filters — every match has to pass the
    # DASL @SQL filter, so per-folder .Count is exact and the running sum
    # is a faithful total_estimated.
    total_estimated: Optional[int] = 0
    merged: List[Dict[str, Any]] = []
    any_folder_hit_cap = False
    for fld in folders_to_scan:
        # Global early-stop: each folder's items are Sort'ed by
        # ReceivedTime desc, and per-folder contribution is capped at
        # `limit`. Once we have 2×limit candidates the global top-`limit`
        # is effectively locked in (worst case a later folder displaces
        # the tail of the window). The 2× is a slop margin — exact
        # correctness would require each folder's max ReceivedTime up
        # front, which itself is an RPC. This trade keeps Outlook
        # responsive on mailboxes with many sub-folders.
        if len(merged) >= limit * 2:
            any_folder_hit_cap = True
            break

        # E4: skip empty folders before the index round-trip.
        try:
            if int(fld.Items.Count) == 0:
                continue
        except Exception:
            pass

        try:
            folder_path = str(getattr(fld, "FolderPath", "") or "")
        except Exception:
            folder_path = ""

        # Restrict failure is fatal. The prior fallback (items = fld.Items)
        # silently degraded to enumerating every mail in the folder, which
        # is what produced the multi-minute Outlook hang when the Windows
        # Search index was unavailable. Surface the error so the agent
        # can pick another tactic (restart WSearch, narrow scope, or
        # switch to list_messages with subject_contains).
        try:
            items = fld.Items.Restrict(dasl)
        except Exception as exc:
            raise RuntimeError(
                f"DASL Restrict failed on {folder_path or 'folder'}: {exc}. "
                f"Likely cause: Windows Search service stopped or index is "
                f"rebuilding. Check 'Get-Service WSearch' or wait for "
                f"indexing to catch up."
            ) from exc

        try:
            folder_total = int(items.Count)
        except Exception:
            folder_total = -1

        if total_estimated is not None:
            if folder_total < 0:
                total_estimated = None
            else:
                total_estimated += folder_total

        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass

        per_folder_count = 0
        for item in items:
            if per_folder_count >= limit:
                any_folder_hit_cap = True
                break
            try:
                if item.Class != _OL_MAIL_CLASS:
                    continue
                merged.append(_mail_item_to_summary(
                    item, preview_chars, folder_path, include_body_preview,
                ))
                per_folder_count += 1
            except Exception:
                pass

    merged.sort(key=lambda m: m.get("received_at") or "", reverse=True)
    pre_limit_count = len(merged)
    merged = merged[:limit]

    truncated = (
        any_folder_hit_cap
        or pre_limit_count > limit
        or (total_estimated is not None and total_estimated > len(merged))
    )

    return {
        "count": len(merged),
        "messages": merged,
        "folders_scanned": len(folders_to_scan),
        "recursive": recursive,
        "truncated": truncated,
        "total_estimated": total_estimated,
    }


def _sync_mark_read(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")
    item = namespace.GetItemFromID(params["entry_id"])
    was_read = not bool(item.UnRead)
    item.UnRead = False
    item.Save()
    return {"entry_id": str(item.EntryID), "was_read": was_read, "is_read": True}


def _sync_mark_unread(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")
    item = namespace.GetItemFromID(params["entry_id"])
    was_read = not bool(item.UnRead)
    item.UnRead = True
    item.Save()
    return {"entry_id": str(item.EntryID), "was_read": was_read, "is_read": False}


def _sync_download_attachment(params: Dict[str, Any]) -> Dict[str, Any]:
    app = _get_app()
    namespace = app.GetNamespace("MAPI")
    item = namespace.GetItemFromID(params["entry_id"])
    if item.Class != _OL_MAIL_CLASS:
        raise ValueError("Entry ID is not a mail item")

    target_index_raw = params.get("attachment_index")
    target_name: Optional[str] = params.get("attachment_name")
    save_as: Optional[str] = params.get("save_as")
    sandbox: str = str(params["_sandbox"])
    output_dir: Optional[str] = params.get("output_dir")

    found = None
    if target_index_raw is not None:
        # Index-based lookup is the unambiguous path. The 'index' field
        # on read_message's attachments list maps directly to Outlook's
        # 1-based Attachments.Item(N) — needed when several attachments
        # share a FileName (the forwarded-.msg chain case that surfaced
        # in the 20260609-125255 run, where 5 same-named .msg files all
        # collapsed onto Attachments.Item(1) under name-based lookup).
        try:
            target_index = int(target_index_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"attachment_index must be an integer, got {target_index_raw!r}"
            ) from exc
        try:
            found = item.Attachments.Item(target_index)
        except Exception as exc:
            count = 0
            try:
                count = int(item.Attachments.Count)
            except Exception:
                pass
            raise ValueError(
                f"attachment_index={target_index} not found "
                f"(message has {count} attachment(s)): {exc}"
            ) from exc
        # When both index and name are passed, sanity-check they agree.
        # Guards against stale read_message snapshots pointing at a
        # message that has since been edited.
        if target_name:
            try:
                actual = str(found.FileName or "")
                if actual.lower() != target_name.lower():
                    raise ValueError(
                        f"attachment_index={target_index} resolves to "
                        f"{actual!r}, not {target_name!r}. The message "
                        f"may have changed since the read_message call."
                    )
            except AttributeError:
                pass
    elif target_name:
        # Name-based fallback: returns the FIRST attachment whose name
        # matches case-insensitively. Ambiguous when multiple share a
        # name — caller should switch to attachment_index for that.
        for att in item.Attachments:
            try:
                if att.FileName.lower() == target_name.lower():
                    found = att
                    break
            except Exception:
                pass
        if found is None:
            available = []
            for att in item.Attachments:
                try:
                    available.append(att.FileName)
                except Exception:
                    pass
            raise ValueError(
                f"Attachment {target_name!r} not found. "
                f"Available attachments: {available!r}"
            )
    else:
        raise ValueError(
            "download_attachment requires either 'attachment_index' "
            "(preferred — unambiguous) or 'attachment_name'."
        )

    # Resolve save filename: explicit save_as wins; else use the
    # attachment's own FileName. Path components are stripped inside
    # _safe_attachment_path so passing a directory-traversal name fails
    # safely there.
    save_name = save_as or str(found.FileName or "attachment")
    save_path = _safe_attachment_path(save_name, output_dir, sandbox)
    found.SaveAsFile(str(save_path))
    return {"path": str(save_path), "size": int(found.Size), "content_type": ""}


def _sync_download_all_attachments(params: Dict[str, Any]) -> Dict[str, Any]:
    """Save every attachment of a message to output_dir, deduping same names.

    Solves the forwarded-mail chain case: a single message may carry N
    attachments that all share a FileName (e.g. five forwarded
    ``滴滴出行电子发票及行程报销单.msg`` files in the 20260609-125255 run).
    Single-shot ``download_attachment`` collapses them all onto the first
    via name-based lookup; this action saves each one with a Windows
    Explorer-style ``" (1)"`` / ``" (2)"`` suffix when names collide.

    Returns ``{"count": N, "saved": [{index, name, original_name, path,
    size}, ...]}``. Per-attachment errors don't abort the whole batch —
    failed entries appear with ``"error": "..."`` and no ``path``.

    ``extension_filter`` (optional) restricts which attachments to save
    by file extension — useful for "give me just the PDFs" without
    pulling embedded ``.msg`` containers or signature images.
    """
    app = _get_app()
    namespace = app.GetNamespace("MAPI")
    item = namespace.GetItemFromID(params["entry_id"])
    if item.Class != _OL_MAIL_CLASS:
        raise ValueError("Entry ID is not a mail item")

    sandbox: str = str(params["_sandbox"])
    output_dir: Optional[str] = params.get("output_dir")

    raw_filter = params.get("extension_filter")
    extension_filter: Optional[set] = None
    if raw_filter:
        extension_filter = set()
        for e in raw_filter:
            e = str(e).strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            extension_filter.add(e)

    saved: List[Dict[str, Any]] = []
    used_names: Dict[str, int] = {}

    for i, att in enumerate(item.Attachments, start=1):
        try:
            original_name = str(att.FileName or f"attachment_{i}")
        except Exception:
            original_name = f"attachment_{i}"

        if extension_filter is not None:
            ext = Path(original_name).suffix.lower()
            if ext not in extension_filter:
                continue

        # Dedup against the names we've ALREADY saved this call:
        #   foo.pdf  → foo.pdf
        #   foo.pdf  → foo (1).pdf
        #   foo.pdf  → foo (2).pdf
        # Doesn't read the destination directory — same-call collisions
        # are the case the forwarded-chain bug needs solved, and reading
        # the directory would race with concurrent writers.
        key = original_name.lower()
        n = used_names.get(key, 0)
        if n == 0:
            save_name = original_name
        else:
            stem = Path(original_name).stem
            ext = Path(original_name).suffix
            save_name = f"{stem} ({n}){ext}"
        used_names[key] = n + 1

        try:
            save_path = _safe_attachment_path(save_name, output_dir, sandbox)
            att.SaveAsFile(str(save_path))
            saved.append({
                "index": i,
                "name": save_name,
                "original_name": original_name,
                "path": str(save_path),
                "size": int(att.Size),
            })
        except Exception as exc:
            saved.append({
                "index": i,
                "name": save_name,
                "original_name": original_name,
                "error": str(exc),
            })

    return {"count": len(saved), "saved": saved}


# ── EmailTool ──────────────────────────────────────────────────────────────────
class EmailTool(BaseTool):
    """Read Outlook email via win32com COM automation (STA, single-worker executor).

    Action dispatch mirrors desktop_tool / web_search_tool: execute() routes
    to _action_<name>() which awaits the sync worker via run_in_executor.
    """

    is_read_only = True          # read path only; flip to False when write path lands
    is_concurrency_safe = False  # serialised by _outlook_lock

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Email action. Start with 'status' if you have not used "
                    "this tool yet — it confirms Outlook is reachable and "
                    "returns today's inbox count, with no required parameters. "
                    "One of: status, list_folders, list_messages, "
                    "read_message, search, mark_read, mark_unread, "
                    "download_attachment, download_all_attachments."
                ),
                "enum": [
                    "status",
                    "list_folders",
                    "list_messages",
                    "read_message",
                    "search",
                    "mark_read",
                    "mark_unread",
                    "download_attachment",
                    "download_all_attachments",
                ],
            },
            "folder": {
                "type": "string",
                "description": (
                    "[list_messages / search] Folder name or path. "
                    "Default: 'Inbox'. Shortcuts: 'Inbox', 'Sent Items', "
                    "'Drafts', 'Outbox', 'Deleted Items', 'Junk Email'. "
                    "Sub-folder: 'Inbox/Project-X'."
                ),
            },
            "recursive": {
                "type": "boolean",
                "description": (
                    "[list_folders] Also list sub-folders one level deep. "
                    "Default: false.\n"
                    "[list_messages] Also scan all sub-folders of `folder` "
                    "(depth-first, up to 4 levels). Default: true. "
                    "Enterprise mailboxes often route incoming mail into "
                    "Inbox sub-folders via rules — list_messages on 'Inbox' "
                    "alone with recursive=false will miss those.\n"
                    "[search] Default: false. search is DASL-Restrict-based "
                    "and synchronous on Outlook's UI thread; recursing into "
                    "30+ rule-routed sub-folders has been observed to make "
                    "Outlook unresponsive for minutes. Pass recursive=true "
                    "explicitly when you need to scan a sub-tree, after "
                    "narrowing `folder` to the smallest reasonable root "
                    "(e.g. 'Inbox/Project-X', not the whole mailbox)."
                ),
            },
            "parent": {
                "type": "string",
                "description": (
                    "[list_folders] Parent folder path to list children of. "
                    "Default: store root (shows all top-level folders)."
                ),
            },
            "unread_only": {
                "type": "boolean",
                "description": (
                    "[list_messages] Return only unread messages. Default: false."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "[list_messages / search] Max messages to return. "
                    "Default: 50 (list_messages), 20 (search). "
                    "Hard cap: 200 (list), 100 (search)."
                ),
            },
            "since": {
                "type": "string",
                "description": (
                    "[list_messages / search] Only messages received on or "
                    "after this point in time. Accepts magic words "
                    "(case-insensitive): 'today' / 'now' (start of today), "
                    "'yesterday', '24h' / '1d' (24 hours ago), 'this_week' "
                    "(start of current ISO week). Or ISO format: "
                    "'2026-01-15' / '2026-01-15T09:00:00'. Prefer the magic "
                    "words for common cases — they are local-time relative "
                    "and do not require you to compute timestamps."
                ),
            },
            "sender_contains": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on sender name and email "
                    "address.\n"
                    "[list_messages] Python post-filter — slower at scale but "
                    "always exact substring.\n"
                    "[search] Index-side DASL LIKE filter — cheap; pair with "
                    "`query` to find 'topic FROM person'."
                ),
            },
            "subject_contains": {
                "type": "string",
                "description": (
                    "[list_messages] Case-insensitive substring matched "
                    "against message subject."
                ),
            },
            "entry_id": {
                "type": "string",
                "description": (
                    "[read_message / mark_read / mark_unread / "
                    "download_attachment] "
                    "Persistent message ID from list_messages or search output."
                ),
            },
            "include_full_body": {
                "type": "boolean",
                "description": (
                    "[read_message] Include the full message body. "
                    "Default: false (500-char preview only). "
                    "Only set true when you need the complete content — "
                    "full bodies can be large and consume LLM context budget."
                ),
            },
            "include_body_preview": {
                "type": "boolean",
                "description": (
                    "[list_messages / search] Include the 500-char body_preview "
                    "field. Default: true.\n"
                    "Set false when you only need metadata (subject + sender + "
                    "date) — skipping the Body COM materialisation gives "
                    "roughly 30-40% speedup on bulk listings (measured on a "
                    "67k-mail Inbox: limit=100 → 11s→8s, limit=50 → 5s→3s, "
                    "limit=20 → 15% saving). When false, body_preview is "
                    "returned as an empty string so the response shape is "
                    "unchanged. Recommended for limit >= 50 or when the agent "
                    "is counting / categorising messages without reading content."
                ),
            },
            "include_attachments_meta": {
                "type": "boolean",
                "description": (
                    "[read_message] Include attachment names and sizes. "
                    "Default: true."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "[search] Search text. Match scope depends on "
                    "`match_mode`: 'phrase' matches subject + body via the "
                    "Windows Search index; 'substring' matches subject "
                    "only. Wildcard chars (% _ [ ]) and quotes are "
                    "escaped — your query is always treated literally. "
                    "Optional when sender_contains is provided — for "
                    "'latest mail from <person>' queries, sender-only "
                    "search is the fastest path (skips body scanning "
                    "entirely)."
                ),
            },
            "match_mode": {
                "type": "string",
                "enum": ["phrase", "substring"],
                "description": (
                    "[search] How `query` matches.\n"
                    "  'phrase' (default): subject + body via DASL "
                    "ci_phrasematch — hits the Windows Search content "
                    "index. Fast on large mailboxes; matches at word "
                    "boundaries ('fail' matches 'fail' but NOT 'failed'). "
                    "For CJK queries, requires the relevant Windows "
                    "indexer language pack to be installed.\n"
                    "  'substring': **subject only**, via LIKE '%query%'. "
                    "Body LIKE was removed because it forces a row-scan "
                    "that materialises every message body on Outlook's UI "
                    "thread, hanging Outlook for minutes on enterprise "
                    "mailboxes. To search inside body content, use "
                    "match_mode='phrase' or narrow scope with "
                    "sender_contains."
                ),
            },
            "attachment_name": {
                "type": "string",
                "description": (
                    "[download_attachment] Attachment file name as "
                    "returned in the 'name' field of read_message's "
                    "attachments list. NOTE: when several attachments "
                    "share a name (forwarded .msg chains commonly do), "
                    "name-based lookup ALWAYS hits the first one — use "
                    "attachment_index to disambiguate or "
                    "action='download_all_attachments' for the whole set."
                ),
            },
            "attachment_index": {
                "type": "integer",
                "description": (
                    "[download_attachment] 1-based index from the "
                    "'index' field of read_message's attachments list. "
                    "Unambiguous; the right way to pick a specific "
                    "attachment when several share a filename. When both "
                    "attachment_index and attachment_name are passed they "
                    "must agree (cross-check guards against stale "
                    "read_message snapshots)."
                ),
            },
            "save_as": {
                "type": "string",
                "description": (
                    "[download_attachment] Optional output filename (no "
                    "path components — those are stripped). Defaults to "
                    "the attachment's original FileName. Use this when "
                    "downloading several same-named attachments "
                    "individually so each one gets a distinct path."
                ),
            },
            "extension_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[download_all_attachments] Optional list of file "
                    "extensions to include (case-insensitive, leading "
                    "dot optional). Example: ['.pdf'] saves only PDFs "
                    "and skips embedded .msg / images / signatures. "
                    "Omit to save every attachment."
                ),
            },
            "output_dir": {
                "type": "string",
                "description": (
                    "[download_attachment / download_all_attachments] "
                    "Absolute path to save into. Must be within "
                    "config.email.attachment_sandbox. Omit to use the "
                    "default sandbox directory. "
                    "download_all_attachments dedups same-named files "
                    "with ' (1)', ' (2)' suffixes (matches Windows "
                    "Explorer's collision pattern)."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None) -> None:
        super().__init__("email", ctx=ctx)
        self.logger = get_logger()

    async def execute(self, **kwargs) -> ToolResult:
        start = time.time()
        params = dict(kwargs)
        action = (kwargs.get("action") or "").strip().lower()
        if not action:
            return self._fail(params, start, "email requires 'action' parameter.")

        dispatch = {
            "status":                   self._action_status,
            "list_folders":             self._action_list_folders,
            "list_messages":            self._action_list_messages,
            "read_message":             self._action_read_message,
            "search":                   self._action_search,
            "mark_read":                self._action_mark_read,
            "mark_unread":              self._action_mark_unread,
            "download_attachment":      self._action_download_attachment,
            "download_all_attachments": self._action_download_all_attachments,
        }
        handler = dispatch.get(action)
        if handler is None:
            return self._fail(
                params, start,
                f"Unknown email action {action!r}. "
                f"Valid actions: {sorted(dispatch)!r}"
            )

        async with _outlook_lock:
            return await handler(params, start, **kwargs)

    # ── Action handlers ───────────────────────────────────────────────────────

    async def _action_status(self, params, start, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(_outlook_executor, _sync_status, kwargs)
        except Exception as exc:
            return self._fail(params, start, f"status: {exc}")
        return self._ok(params, start, data)

    async def _action_list_folders(self, params, start, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(_outlook_executor, _sync_list_folders, kwargs)
        except Exception as exc:
            return self._fail(params, start, f"list_folders: {exc}")
        return self._ok(params, start, data)

    async def _action_list_messages(self, params, start, **kwargs):
        email_cfg = ConfigManager().get_section("email")
        run_params = dict(kwargs)
        run_params["_preview_chars"] = int(email_cfg.get("body_preview_chars", 500))
        run_params["_folder_blacklist"] = email_cfg.get("folder_blacklist") or []
        run_params["_max_depth"] = int(email_cfg.get("max_recursion_depth", 4))
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                _outlook_executor, _sync_list_messages, run_params
            )
        except Exception as exc:
            return self._fail(params, start, f"list_messages: {exc}")
        return self._ok(params, start, data)

    async def _action_read_message(self, params, start, **kwargs):
        if not kwargs.get("entry_id"):
            return self._fail(params, start, "read_message requires 'entry_id'.")
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(_outlook_executor, _sync_read_message, kwargs)
        except Exception as exc:
            return self._fail(params, start, f"read_message: {exc}")
        return self._ok(params, start, data)

    async def _action_search(self, params, start, **kwargs):
        if not kwargs.get("query") and not kwargs.get("sender_contains"):
            return self._fail(
                params, start,
                "search requires 'query' or 'sender_contains' (at least "
                "one). For 'latest mail from <person>' queries, "
                "sender_contains alone is the fastest path — it hits "
                "indexed sender fields and skips body scanning entirely."
            )
        email_cfg = ConfigManager().get_section("email")
        run_params = dict(kwargs)
        run_params["_preview_chars"] = int(email_cfg.get("body_preview_chars", 500))
        run_params["_folder_blacklist"] = email_cfg.get("folder_blacklist") or []
        run_params["_max_depth"] = int(email_cfg.get("max_recursion_depth", 4))
        # Hard wall-clock timeout. The executor is STA + max_workers=1, so
        # a hung sync call cannot be cancelled mid-flight (Python can't
        # interrupt a blocked COM round-trip). wait_for at least frees the
        # awaiter so the agent gets a TimeoutError and can switch tactics
        # instead of waiting indefinitely; the user's "stop" message also
        # stops being blocked behind the dead future. Default 30s is far
        # above the 99p of healthy queries (~3s) — a hit means the query
        # landed on a row-scan path and Outlook is likely already
        # unresponsive. Override per-call via the `timeout` kwarg or
        # globally via email.search_timeout_seconds in handq_config.yaml.
        timeout_s = float(
            kwargs.get("timeout")
            or email_cfg.get("search_timeout_seconds")
            or 30
        )
        loop = asyncio.get_running_loop()
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(_outlook_executor, _sync_search, run_params),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return self._fail(
                params, start,
                f"search timed out after {timeout_s:.0f}s. The query "
                f"likely fell into a row-scan path (recursive=True over a "
                f"wide tree without sender filter, or a CJK substring "
                f"query the index can't accelerate). Recover by one of: "
                f"(1) add sender_contains, (2) recursive=False with a "
                f"specific folder, (3) match_mode='phrase' so "
                f"ci_phrasematch handles body matching via the index."
            )
        except Exception as exc:
            return self._fail(params, start, f"search: {exc}")
        return self._ok(params, start, data)

    async def _action_mark_read(self, params, start, **kwargs):
        if not kwargs.get("entry_id"):
            return self._fail(params, start, "mark_read requires 'entry_id'.")
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(_outlook_executor, _sync_mark_read, kwargs)
        except Exception as exc:
            return self._fail(params, start, f"mark_read: {exc}")
        return self._ok(params, start, data)

    async def _action_mark_unread(self, params, start, **kwargs):
        if not kwargs.get("entry_id"):
            return self._fail(params, start, "mark_unread requires 'entry_id'.")
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(_outlook_executor, _sync_mark_unread, kwargs)
        except Exception as exc:
            return self._fail(params, start, f"mark_unread: {exc}")
        return self._ok(params, start, data)

    async def _action_download_attachment(self, params, start, **kwargs):
        if not kwargs.get("entry_id"):
            return self._fail(params, start, "download_attachment requires 'entry_id'.")
        if not kwargs.get("attachment_name") and kwargs.get("attachment_index") is None:
            return self._fail(
                params, start,
                "download_attachment requires 'attachment_index' "
                "(preferred — use the 'index' field from read_message) "
                "or 'attachment_name'. When the message has multiple "
                "same-named attachments (common in forwarded .msg "
                "chains), name-based lookup collapses them all onto the "
                "first — switch to attachment_index to disambiguate, or "
                "use action='download_all_attachments' to grab the whole "
                "set in one call."
            )
        email_cfg = ConfigManager().get_section("email")
        sandbox_raw = email_cfg.get(
            "attachment_sandbox",
            r"%USERPROFILE%\HandQ\email_attachments",
        )
        run_params = dict(kwargs)
        run_params["_sandbox"] = os.path.expandvars(sandbox_raw)
        # Anchor a relative output_dir to the per-session workspace before the
        # sync helper's _safe_attachment_path runs Path(output_dir).resolve()
        # (which otherwise resolves against the process cwd — no longer the
        # session workspace; see concurrency work).
        _out_dir = run_params.get("output_dir")
        if _out_dir:
            run_params["output_dir"] = self.resolve_in_workspace(_out_dir)
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                _outlook_executor, _sync_download_attachment, run_params
            )
        except Exception as exc:
            return self._fail(params, start, f"download_attachment: {exc}")
        return self._ok(params, start, data)

    async def _action_download_all_attachments(self, params, start, **kwargs):
        if not kwargs.get("entry_id"):
            return self._fail(
                params, start,
                "download_all_attachments requires 'entry_id'."
            )
        email_cfg = ConfigManager().get_section("email")
        sandbox_raw = email_cfg.get(
            "attachment_sandbox",
            r"%USERPROFILE%\HandQ\email_attachments",
        )
        run_params = dict(kwargs)
        run_params["_sandbox"] = os.path.expandvars(sandbox_raw)
        _out_dir = run_params.get("output_dir")
        if _out_dir:
            run_params["output_dir"] = self.resolve_in_workspace(_out_dir)
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                _outlook_executor, _sync_download_all_attachments, run_params
            )
        except Exception as exc:
            return self._fail(params, start, f"download_all_attachments: {exc}")
        return self._ok(params, start, data)

    # ── Shared result builders ────────────────────────────────────────────────

    def _ok(self, params, start, data) -> ToolResult:
        return ToolResult(
            success=True,
            output=data,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    def _fail(self, params, start, msg: str) -> ToolResult:
        return ToolResult(
            success=False,
            output=None,
            tool_name=self.name,
            tool_parameters=params,
            error=msg,
            execution_time=time.time() - start,
        )
