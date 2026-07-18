---
name: email-workflow
description: Outlook MAPI email automation — workflow, search modes, performance tips, concurrency limits
enabled: true
standing: false
origin: bundled
allowed-tools: [email]
---
# Email (Outlook MAPI) Workflow

The 'email' tool reads local Outlook mail via win32com COM.
It reuses your MAPI profile — no extra credentials needed.

## Workflow

1. `action='status'` — quick MAPI connectivity + default-account check before anything else if you're unsure Outlook is reachable.
2. `action='list_folders'` — see folder names + unread counts (top-level only)
3. `action='list_messages'` folder='Inbox' [unread_only=true] [limit=20]
   - Returns entry_id + subject + sender + 500-char body_preview
   - Defaults to `recursive=true`: scans Inbox AND every sub-folder (4 levels deep)
4. `action='read_message'` entry_id='...' [include_full_body=true]
   - ONLY needed for full body, to/cc/bcc, or attachment metadata
   - search/list_messages already have everything else — DO NOT re-fetch
5. `action='search'` query='keyword' [folder] [sender_contains] [match_mode] [since] [limit]
   - Index-backed search on subject + body
   - Returns same dict as list_messages (entry_id/subject/sender/body_preview/folder)
6. `action='mark_read'` / `action='mark_unread'` entry_id='...' — flip read state without opening the message.
7. `action='download_attachment'` entry_id='...' attachment_name='file.pdf'
8. `action='download_all_attachments'` entry_id='...' [output_dir] — grab every attachment on a message in one call instead of looping download_attachment per file.

## match_mode (critical choice)

- **'phrase'** (default): WDS index. Sub-second on any folder size. Word-level matching.
- **'substring'**: LIKE '%query%' fallback. Fine on <1000 msgs. Can take MINUTES on large folders (6+ min on 17k msgs). Use ONLY when phrase returned empty AND you're confident the keyword is a sub-word fragment.

## Performance Tips

- Set `include_body_preview=false` when you only need metadata (limit≥50 → ~30-40% faster)
- Combine `sender_contains` with `query` for 'topic FROM person' — both are index-side DASL filters
- `recursive=true` is the default and usually correct (enterprise users have Inbox sub-folders)

## Truncation Handling

- `truncated=true` means more matches exist than returned
- DO NOT silently summarise — tell the user the real total and offer to narrow the query

## Concurrency

Email is COM-STA-serialised — every action runs on a single dedicated thread.
Parallel email dispatches give ZERO speedup. Call sequentially.

## Scope

- Outlook stays open — the tool never calls app.Quit()
- Write actions (compose_draft / send) are NOT in scope yet
- Attachments are sandboxed; paths outside the sandbox are refused
