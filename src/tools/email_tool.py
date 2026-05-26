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
from datetime import datetime
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
    """Return Outlook.Application CDispatch. Runs INSIDE the executor thread."""
    global _outlook_app
    if _outlook_app is not None:
        return _outlook_app
    from win32com.client import Dispatch, gencache  # type: ignore[import-untyped]
    try:
        # Early binding — ~1–3s on first call (writes %TEMP%\gen_py\...);
        # instant on subsequent calls.  Falls back gracefully under Nuitka
        # standalone where gen_py path resolution may differ (doc §13).
        _outlook_app = gencache.EnsureDispatch("Outlook.Application")
    except Exception:
        _outlook_app = Dispatch("Outlook.Application")
    return _outlook_app


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


def _walk_folders(folder, max_depth: int = 10):
    """Yield ``folder`` then every descendant folder, depth-first.

    Bounded by ``max_depth`` to defuse pathological / cyclic stores. Errors
    while iterating a sub-folder are swallowed so one bad branch doesn't
    abort the whole walk.
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


def _mail_item_to_summary(item, preview_chars: int) -> Dict[str, Any]:
    """Return list_messages / search dict for a MailItem. All plain Python."""
    try:
        folder_path = str(item.Parent.FolderPath)
    except Exception:
        folder_path = ""
    return {
        "entry_id": str(item.EntryID),
        "subject": str(item.Subject or ""),
        "sender_name": str(item.SenderName or ""),
        "sender_email": str(item.SenderEmailAddress or ""),
        "received_at": _to_iso(item.ReceivedTime),
        "is_read": not bool(item.UnRead),
        "has_attachments": bool(item.Attachments.Count > 0),
        "body_preview": str(item.Body or "")[:preview_chars],
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
            for att in item.Attachments:
                try:
                    attachments.append({
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


def _build_dasl_prefilter(
    since_dt: Optional[datetime],
    unread_only: bool,
) -> Optional[str]:
    """Build a DASL @SQL filter for the index-side pre-filters of list_messages.

    DASL (vs. Jet syntax) is locale-invariant — Jet's date format depends on
    Windows regional settings, which would break ``[ReceivedTime] >= '...'``
    on non-en-US machines. Returns None when no pre-filter applies.
    """
    parts: List[str] = []
    if since_dt is not None:
        parts.append(
            f"\"urn:schemas:httpmail:datereceived\" >= "
            f"'{since_dt.strftime('%Y/%m/%d %H:%M:%S')}'"
        )
    if unread_only:
        parts.append("\"urn:schemas:httpmail:read\" = 0")
    if not parts:
        return None
    return "@SQL=" + " AND ".join(parts)


def _count_folder_prefilter(folder, dasl_prefilter: Optional[str]) -> int:
    """Index-side count of items in ``folder`` matching the DASL prefilter.

    Returns -1 when the count couldn't be obtained — the caller should treat
    that as "unknown" and disable the aggregated total estimate.
    """
    try:
        if dasl_prefilter is None:
            return int(folder.Items.Count)
        return int(folder.Items.Restrict(dasl_prefilter).Count)
    except Exception:
        return -1


def _scan_folder_messages(
    folder,
    *,
    since_dt: Optional[datetime],
    sender_filter: str,
    subject_filter: str,
    unread_only: bool,
    preview_chars: int,
    cap: int,
) -> tuple:
    """Per-folder message scan honoring all list_messages filters.

    Returns ``(summaries, hit_cap)`` — ``hit_cap`` is True iff iteration
    stopped because the cap was reached (i.e. there were more matching
    candidates we didn't materialise). Callers use it to drive the
    response-level ``truncated`` flag.
    """
    try:
        items = folder.Items
    except Exception:
        return [], False

    if unread_only:
        try:
            items = items.Restrict("[Unread] = True")
        except Exception:
            pass

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

        # Items are sorted newest-first → break as soon as we cross the floor
        if since_dt:
            rt = _to_python_datetime(item.ReceivedTime)
            if rt is None:
                continue
            if rt < since_dt:
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
            out.append(_mail_item_to_summary(item, preview_chars))
        except Exception:
            pass

    return out, hit_cap


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

    since_dt: Optional[datetime] = None
    if params.get("since"):
        try:
            since_dt = datetime.fromisoformat(str(params["since"]).replace("Z", ""))
        except (ValueError, TypeError):
            pass

    sender_filter = (params.get("sender_contains") or "").lower()
    subject_filter = (params.get("subject_contains") or "").lower()
    unread_only = bool(params.get("unread_only"))
    limit = min(int(params.get("limit") or 50), 200)
    preview_chars = int(params.get("_preview_chars") or 500)

    # list_messages defaults to recursive=true: enterprise mailboxes commonly
    # route incoming mail into Inbox sub-folders via rules, and a single-folder
    # scan of "Inbox" silently misses those.
    recursive_param = params.get("recursive")
    recursive = True if recursive_param is None else bool(recursive_param)

    folders_to_scan = list(_walk_folders(root_folder)) if recursive else [root_folder]
    if blacklist:
        folders_to_scan = [
            f for f in folders_to_scan
            if not _is_folder_blacklisted(getattr(f, "Name", "") or "", blacklist)
        ]

    # When no Python-side post-filters apply, total_estimated is exact (it
    # equals the index-side count). Otherwise leave it None — Outlook can't
    # cheaply count items that match a substring on SenderName/Subject.
    post_filters_active = bool(sender_filter or subject_filter)
    dasl_prefilter = _build_dasl_prefilter(since_dt, unread_only)
    total_estimated: Optional[int] = 0 if not post_filters_active else None

    merged: List[Dict[str, Any]] = []
    any_folder_hit_cap = False
    for fld in folders_to_scan:
        try:
            results, hit_cap = _scan_folder_messages(
                fld,
                since_dt=since_dt,
                sender_filter=sender_filter,
                subject_filter=subject_filter,
                unread_only=unread_only,
                preview_chars=preview_chars,
                cap=limit,
            )
        except Exception:
            continue
        merged.extend(results)
        if hit_cap:
            any_folder_hit_cap = True
        if total_estimated is not None:
            count_in_folder = _count_folder_prefilter(fld, dasl_prefilter)
            if count_in_folder < 0:
                total_estimated = None  # one failed count poisons the sum
            else:
                total_estimated += count_in_folder

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
            # 'substring': exact LIKE — body LIKE falls through to a row scan
            # if WDS isn't current; slower but matches across word boundaries.
            q_esc = _escape_dasl_like(query)
            dasl_parts.append(
                f"(\"urn:schemas:httpmail:subject\" LIKE '%{q_esc}%' "
                f"OR \"urn:schemas:httpmail:textdescription\" LIKE '%{q_esc}%')"
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

    if params.get("since"):
        try:
            since_dt = datetime.fromisoformat(str(params["since"]).replace("Z", ""))
            dasl_parts.append(
                f"\"urn:schemas:httpmail:datereceived\" >= "
                f"'{since_dt.strftime('%Y/%m/%d %H:%M:%S')}'"
            )
        except (ValueError, TypeError):
            pass

    dasl = "@SQL=" + " AND ".join(dasl_parts)

    limit = min(int(params.get("limit") or 20), 100)
    preview_chars = int(params.get("_preview_chars") or 500)

    # search defaults to recursive=true for the same reason as list_messages:
    # auto-routed mail is the common case, and DASL Restrict per-folder is
    # cheap (Outlook indexes subject + datereceived).
    recursive_param = params.get("recursive")
    recursive = True if recursive_param is None else bool(recursive_param)

    folders_to_scan = list(_walk_folders(root_folder)) if recursive else [root_folder]
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
        try:
            items = fld.Items.Restrict(dasl)
            try:
                folder_total = int(items.Count)
            except Exception:
                folder_total = -1
        except Exception:
            items = fld.Items   # fallback: iterate all in this folder
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
                merged.append(_mail_item_to_summary(item, preview_chars))
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

    target_name: str = str(params["attachment_name"])
    sandbox: str = str(params["_sandbox"])
    output_dir: Optional[str] = params.get("output_dir")

    # Find attachment by name (case-insensitive)
    found = None
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

    save_path = _safe_attachment_path(target_name, output_dir, sandbox)
    found.SaveAsFile(str(save_path))
    return {"path": str(save_path), "size": int(found.Size), "content_type": ""}


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
                    "Email action. One of: list_folders, list_messages, "
                    "read_message, search, mark_read, mark_unread, "
                    "download_attachment."
                ),
                "enum": [
                    "list_folders",
                    "list_messages",
                    "read_message",
                    "search",
                    "mark_read",
                    "mark_unread",
                    "download_attachment",
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
                    "[list_messages / search] Also scan all sub-folders of "
                    "`folder` (depth-first, up to 10 levels). Default: true. "
                    "Enterprise mailboxes often route incoming mail into "
                    "Inbox sub-folders via rules — querying 'Inbox' alone "
                    "with recursive=false will miss those. Each result row "
                    "includes a `folder` field showing where it actually "
                    "lives. Set false to scan only the named folder."
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
                    "after this date. ISO format: '2026-01-15' or "
                    "'2026-01-15T09:00:00'."
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
                    "[search] Search text matched against subject + body. "
                    "Wildcard chars (% _ [ ]) and quotes are escaped — your "
                    "query is always treated literally. Match semantics are "
                    "controlled by `match_mode`."
                ),
            },
            "match_mode": {
                "type": "string",
                "enum": ["phrase", "substring"],
                "description": (
                    "[search] How `query` matches subject and body.\n"
                    "  'phrase' (default): hits the Windows Search content "
                    "index via DASL ci_phrasematch. Fast on large mailboxes; "
                    "matches at word boundaries ('fail' matches 'fail' but "
                    "NOT 'failed').\n"
                    "  'substring': falls back to LIKE '%query%' — exact "
                    "substring match across word boundaries (matches both "
                    "'fail' and 'failed' for query='fail'), but body LIKE "
                    "can be slow when WDS isn't current. Use this when you "
                    "need partial-word hits or when 'phrase' returns "
                    "unexpectedly empty (WDS still indexing)."
                ),
            },
            "attachment_name": {
                "type": "string",
                "description": (
                    "[download_attachment] Exact attachment file name as "
                    "returned by read_message (e.g. 'report.pdf')."
                ),
            },
            "output_dir": {
                "type": "string",
                "description": (
                    "[download_attachment] Absolute path to save the file. "
                    "Must be within config.email.attachment_sandbox. "
                    "Omit to use the default sandbox directory."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        super().__init__("email")
        self.logger = get_logger()

    async def execute(self, **kwargs) -> ToolResult:
        start = time.time()
        params = dict(kwargs)
        action = (kwargs.get("action") or "").strip().lower()
        if not action:
            return self._fail(params, start, "email requires 'action' parameter.")

        dispatch = {
            "list_folders":        self._action_list_folders,
            "list_messages":       self._action_list_messages,
            "read_message":        self._action_read_message,
            "search":              self._action_search,
            "mark_read":           self._action_mark_read,
            "mark_unread":         self._action_mark_unread,
            "download_attachment": self._action_download_attachment,
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
        if not kwargs.get("query"):
            return self._fail(params, start, "search requires 'query'.")
        email_cfg = ConfigManager().get_section("email")
        run_params = dict(kwargs)
        run_params["_preview_chars"] = int(email_cfg.get("body_preview_chars", 500))
        run_params["_folder_blacklist"] = email_cfg.get("folder_blacklist") or []
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(_outlook_executor, _sync_search, run_params)
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
        if not kwargs.get("attachment_name"):
            return self._fail(params, start, "download_attachment requires 'attachment_name'.")
        email_cfg = ConfigManager().get_section("email")
        sandbox_raw = email_cfg.get(
            "attachment_sandbox",
            r"%USERPROFILE%\HandQ\email_attachments",
        )
        run_params = dict(kwargs)
        run_params["_sandbox"] = os.path.expandvars(sandbox_raw)
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                _outlook_executor, _sync_download_attachment, run_params
            )
        except Exception as exc:
            return self._fail(params, start, f"download_attachment: {exc}")
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
