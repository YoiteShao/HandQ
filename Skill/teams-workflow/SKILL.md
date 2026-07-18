---
name: teams-workflow
description: MS Teams Graph API automation — capability matrix, browser fallbacks, key invariants
enabled: true
standing: false
origin: bundled
allowed-tools: [teams]
---
# Teams (Microsoft Graph) Workflow

The 'teams' tool covers Microsoft Teams via Graph API + Teams internal API.
It runs SILENTLY — the user's Teams desktop app is unaffected, no UI is stolen.

## First Call Behavior

The first call (and any after token expires, ~1 hour) opens an Edge window
briefly (~3-5s) to harvest the Graph access token from teams.microsoft.com SSO.
If bootstrap returns 'profile_locked', the browser tool is using the same profile
— finish the browser step first, then retry.

DO NOT try to find/read the token cache yourself — the tool owns it.

## Capability Matrix

### Calendar / Meetings (✅ direct)
- `list_calendar_events` [start_after, end_before, top] — today's meetings, next meeting
- `get_event` event_id — event details
- `create_meeting` subject, start, end, attendees, online — schedule a meeting
- `respond_event` event_id, response — accept/decline/tentative
- `find_meeting_times` attendees, duration_minutes — find free slots

### Chat (✅ mixed backend)
- `list_chats` [top] — list my chats / recent conversations
- `read_chat` chat_id [top] — read messages
- `send_chat` chat_id, message_html — send a message (discover chat_id with list_chats first)

### Teams / Channels (✅ direct)
- `list_teams` / `list_channels` team_id — teams and channels
- `read_channel` team_id, channel_id [top] — channel messages
- `send_channel` team_id, channel_id, message_html — post to channel

### People (✅ direct)
- `find_person` query — returns display_name, title, department, emails[]

### Presence (✅ read only)
- `get_presence` [user_id] — check online status (cannot WRITE presence)

### Files / OneDrive (✅ direct)
- `search_files` query — find files
- `list_recent_files` [top] — recent files

### Tasks / Microsoft To Do (✅ direct)
- `list_tasks` [top] — list tasks
- `create_task` title [due_date] — add a task

## Browser Fallbacks (when teams tool can't)

| User ask | Route |
|---|---|
| 'join the meeting' | teams.list_calendar_events → browser.navigate(join_url) |
| 'set status to Busy/DND' | browser: teams.microsoft.com → click avatar → pick status |
| 'play meeting recording' | teams.list_calendar_events → browser.navigate(web_link) → Recording tab |
| 'show activity feed' | browser: teams.microsoft.com/_#/activity |

## Impossible (don't attempt)

- Join active call audio/video (no API for live media)
- Change Teams settings/theme/notifications
- Read meeting recordings/transcripts (admin scope)
- Drive Teams desktop app via desktop_tool

## Key Invariants

- `top` capped at 50 per call — paginate with multiple calls
- `message_html` is HTML (Teams' native format); 32 KB cap per message
- send_* / create_meeting / respond_event are NOT undoable — verify identifiers first
- For send_chat to NEW person: start chat in Teams Web first (API can only post to existing chats)
- Rate limit: ~12k req / 10 min / app. Tool surfaces 429 with Retry-After.
- 401 mid-task: bootstrap auto-runs on next call (~3-5s). Just retry once.
