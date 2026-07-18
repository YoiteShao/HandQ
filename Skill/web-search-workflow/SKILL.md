---
name: web-search-workflow
description: Enterprise cross-source search (Confluence/Jira/SharePoint/orbit) — sources, login recovery, ranking-vs-reading split
enabled: true
standing: false
origin: bundled
allowed-tools: [web_search]
---
# Enterprise Web Search Workflow

Searches Qualcomm internal sources via the authenticated browser session —
cookies/SSO are reused from the persistent browser profile, so the user logs
in once per source and HandQ inherits the cookie afterward.

## When to use

- The request names Confluence/Jira/SharePoint/orbit/intranet, or asks to
  find internal docs/wiki/ticket content.
- Any cross-source enterprise search.

## When NOT to use

- Public web search (Google/DuckDuckGo) — use `browser` navigate + extract.
- You already know the URL — use `browser` navigate.
- Reading one specific Confluence page / Jira ticket you can already name —
  use `browser` navigate + extract.
- Email/calendar lookup — use `email`.

## Ranking vs. reading

`web_search` returns snippet-truncated hits (~300 chars) — it is for ranking
which result to open, not for reading full documents. Pick a hit and call
`browser.navigate` to read it; auto-fetching full bodies through this tool
is out of scope. `browser.launch_browser` is idempotent — call it first if
a session may not be open yet.

## Login recovery

If a result's error reads `'<source> requires login (status=401|403|3xx)'`:
`browser.navigate` to the source's base URL, then
`browser.request_user_login(reason='auth <source>', success_url_pattern='<base_url>')`.
After the user approves, retry the same search call. Cookies persist across
HandQ sessions, so this is normally a once-per-source dance until server-side
expiry.

## Sources

- **confluence** — qualcomm-confluence.atlassian.net (Atlassian Cloud REST).
  Query accepts CQL (`text ~ "..."`, `space=ENG AND ...`) or plain text
  (auto-wrapped in `text~`).
- **jira** — jira-dc.qualcomm.com (Jira Data Center REST). Query accepts JQL
  (`project = ANDR AND text ~ "..."`) or plain text (auto-wrapped in `text~`).
- **sharepoint** — qualcomm.sharepoint.com (SharePoint Online Search REST).
  Plain free-text; KQL keywords (`filetype:pdf`, `author:"..."`) also work.
- **orbit** — intranet portal, DOM-extract fallback (no JSON API). Selector
  tunable via `web_search.sources.orbit.result_selector` in config if the
  portal markup shifts.

Default `limit` 10, hard cap 25 (clamped from `web_search.max_limit` in config).
