---
name: web-search-workflow
description: Enterprise cross-source search (Confluence/Jira/SharePoint/orbit) + YOUR-AI-ENDPOINT synthesised-answer assistant — sources, login recovery, ranking-vs-reading split
enabled: true
standing: false
origin: bundled
allowed-tools: [web_search]
---
# Enterprise Web Search Workflow

Searches COMPANY internal sources via the authenticated browser session —
cookies/SSO are reused from the persistent browser profile, so the user logs
in once per source and HandQ inherits the cookie afterward.

## When to use

- The request names Confluence/Jira/SharePoint/orbit/intranet, or asks to
  find internal docs/wiki/ticket content.
- Any cross-source enterprise search.
- The request asks YOUR-AI-ENDPOINT something ("ask YOUR-AI-ENDPOINT X", "what does YOUR-AI-ENDPOINT say
  about X"), or wants a synthesised answer from internal knowledge rather than
  a list of documents to open → `source=YOUR-AI-ENDPOINT`.

## When NOT to use

- Public web search (Google/DuckDuckGo) — use `browser` navigate + extract.
- You already know the URL — use `browser` navigate.
- Reading one specific Confluence page / Jira ticket you can already name —
  use `browser` navigate + extract.
- Email/calendar lookup — use `email`.

## Ranking vs. reading

For confluence/jira/sharepoint/orbit, `web_search` returns snippet-truncated
hits (~300 chars) — it is for ranking which result to open, not for reading
full documents. Pick a hit and call `browser.navigate` to read it; auto-fetching
full bodies through this tool is out of scope. `browser.launch_browser` is
idempotent — call it first if a session may not be open yet.

**YOUR-AI-ENDPOINT is the exception — it is for reading, not ranking.** It returns ONE
hit whose `snippet` is YOUR-AI-ENDPOINT's full, untruncated answer (markdown, possibly
long), optionally followed by the source documents it cited. Read `hits[0]`
directly; don't navigate to open it. YOUR-AI-ENDPOINT ignores `limit`/`offset`.

## Login recovery

If a result's error reads `'<source> requires login (status=401|403|3xx)'`:
`browser.navigate` to the source's base URL, then
`browser.request_user_login(reason='auth <source>', success_url_pattern='<base_url>')`.
After the user approves, retry the same search call. Cookies persist across
HandQ sessions, so this is normally a once-per-source dance until server-side
expiry.

## Sources

- **confluence** — COMPANY-confluence.atlassian.net (Atlassian Cloud REST).
  Query accepts CQL (`text ~ "..."`, `space=ENG AND ...`) or plain text
  (auto-wrapped in `text~`).
- **jira** — jira-dc.COMPANY.com (Jira Data Center REST). Query accepts JQL
  (`project = ANDR AND text ~ "..."`) or plain text (auto-wrapped in `text~`).
- **sharepoint** — COMPANY.sharepoint.com (SharePoint Online Search REST).
  Plain free-text; KQL keywords (`filetype:pdf`, `author:"..."`) also work.
- **orbit** — intranet portal, DOM-extract fallback (no JSON API). Selector
  tunable via `web_search.sources.orbit.result_selector` in config if the
  portal markup shifts.
- **YOUR-AI-ENDPOINT** — the YOUR-AI-ENDPOINT-chat assistant (YOUR-AI-ENDPOINT-chat.COMPANY.com). Returns a
  synthesised, cited answer to a natural-language question, RAG-searching
  COMPANY internal knowledge first. Auth is two tokens harvested from the live
  session (not cookies); needs a launched browser session like orbit. Query is
  plain natural language ("What is the X release schedule?"). Ignores `limit`.

Default `limit` 10, hard cap 25 (clamped from `web_search.max_limit` in config).
Applies to the ranking sources; YOUR-AI-ENDPOINT always returns one answer.
