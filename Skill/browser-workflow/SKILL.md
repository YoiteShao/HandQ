---
name: browser-workflow
description: Chromium browser automation — persistent profile, workflow steps, login recovery. READ THIS FIRST if the task implies an already-logged-in/already-open browser (SSO session, "my existing tab", etc.) — your FIRST call to browser_launch or browser_attach picks a mode for the rest of the session and cannot be undone mid-task, so decide before that first call, not after.
enabled: true
standing: false
origin: bundled
allowed-tools: [browser_launch, browser_navigate, browser_click, browser_type, browser_extract, browser_snapshot, browser_screenshot, browser_wait_for, browser_close_tab, browser_request_user_login, browser_attach, browser_new_tab, browser_vision_query, browser_video_context, browser_fetch_json]
---
# Browser Automation Workflow

## Step 0: launch vs. attach — decide THIS FIRST

Two ways to get a browser session, and your first call to either one locks
in that mode for the rest of the HandQ session (see below for why there's
no undo):

- **`browser_launch`** — a separate, HandQ-owned Chromium profile. Cookies/
  login persist ACROSS HandQ sessions (you login once, ever, per site), but
  it starts unauthenticated relative to whatever the user has open in their
  own everyday browser right now. Default choice for a fresh task with no
  reference to an existing window.
- **`browser_attach`** — connects to ANY process exposing a Chrome DevTools
  Protocol endpoint, not only the user's Chrome/Edge. Two distinct triggers:
  1. **User's real browser** — the task's own wording tells you the target
     depends on a session that's ALREADY logged in / already open in the
     user's real browser — this can be phrased many ways, not just a fixed
     Chinese idiom: '刚才/正在/我现在打开的/接着我那个', "I'm already logged
     into it", "use my existing session", "don't start a fresh one", a site
     that's SSO-gated and the user says they're already authenticated, etc.
     Read the INTENT (does this task depend on state that only exists in a
     browser window the user already has open?), not a fixed keyword list.
  2. **A local desktop app that is Chromium-based under the hood.** Signal:
     `desktop_snapshot`/UIA comes back nearly empty for it (see
     desktop-workflow's Electron caveat), AND it can be started with — or is
     already running with — a `--remote-debugging-port=<port>` flag. In this
     case: launch/relaunch the app yourself with that flag (via `shell`),
     make sure `browser.cdp_port` matches (or pass a `browser_credentials_file`
     pointing at it), then `browser_attach` instead of the app's normal
     launch path. Do NOT hand-write a websocket/CDP client script for this —
     `browser_navigate`/`browser_snapshot`/`browser_click`/`browser_type`
     already speak CDP through Playwright and handle selector waiting,
     retries, and dropdown/tree interaction that raw `Runtime.evaluate` calls
     don't. If `attach_browser` errors because `browser.attach_enabled` is
     off, tell the user it needs to be set to `true` in `handq_config.yaml`
     rather than falling back to a hand-rolled CDP script.

  Requires user approval (high-risk); in attach mode `browser_new_tab` opens
  tabs in the background.

  `browser_launch` always spawns a SEPARATE, HandQ-owned Chromium/Edge
  process — it can never reach another program's already-running window or
  its in-memory state (open dialogs, a selected dropdown value, a loaded
  app bundle behind a custom `file://`/`app://` scheme). If the task's
  target is a process already running on this machine, `browser_attach` is
  the only one of the two that can reach it — trying `browser_launch`
  against it will look like it "worked" (a new window opens) while actually
  landing on an unrelated, unauthenticated instance.

**Why there's no undo:** `browser_launch` and `browser_attach` are mutually
exclusive for the lifetime of one HandQ session. Once either mode is
active, calling the other errors with "a '<mode>' session is already
active" — and no atomic tool closes/resets an active session from inside a
task (not `browser_close_tab`, which only closes one tab and refuses to
close the last one). If you realize mid-task you picked the wrong one, do
NOT keep retrying the other mode — it will keep failing identically. Stop
and tell the user directly which mode you're stuck in and that a new HandQ
session is needed to reset it. That's a real, correctly-diagnosed
limitation, not a mistake to route around by guessing more tool calls.

Each step below is its OWN tool — there is no single 'browser' tool with an
`action` parameter to switch between them (that API was split into separate
atomic tools). `claim_tool` every one you need up front, including whichever
of `browser_launch`/`browser_attach` you picked above — claiming only the
action tools you think you'll need (e.g. navigate/click) without also
claiming the session-starting tool means step 1 below will fail with "No
browser session" every time you retry it.

Chromium (Edge by default) runs with a persistent profile — cookies and
login state survive across HandQ sessions, the user logs in once per site.
The window launches off-screen to keep the user's desktop undisturbed.

## Workflow

1. `browser_launch` (or `browser_attach` — see Step 0 above) — idempotent,
   safe to call repeatedly. One of the two MUST be called before any other
   browser_* tool works; there is no implicit auto-launch.
2. `browser_navigate(url='https://...')` — result includes `page_state` (open
   dialog + toast text); inspect before deciding next call.
3. `browser_snapshot` — preferred entry point for figuring out a page.
   Returns every interactable element with a suggested selector + any open
   modal. Use BEFORE guessing selectors.
4. `browser_extract` — read content (mode='text' default; mode='list' with
   selector + limit for enumerating matches).
5. `browser_click` / `browser_type` — interact. Both echo `page_state` after
   the action — read it instead of running a follow-up extract.
6. `browser_wait_for` — for selectors / URL patterns when needed.
7. Login wall → `browser_request_user_login` so user can log in manually.
   The agent NEVER reads or types passwords — `input[type=password]` is
   REFUSED server-side.
8. `browser_close_tab` — for tabs you no longer need.
9. `browser_vision_query` — image-level questions only (chart trend, canvas
   content, captcha). For "what's on the page" use snapshot; for text
   content use extract.
10. `browser_video_context` — read an active `<video>`'s title/description/
    captions via textTracks. Use this for "what is this video about", not
    per-frame vision.
11. `browser_fetch_json` — call a REST API as the logged-in user (reuses
    cookies/SSO) without leaving the DOM in an unexpected state.

## Key Points

- Profile path is returned in `browser_launch`'s result — no need to know it
  in advance.
- Snapshot is preferred over repeated extract probes.
- `page_state` after click/type tells you what changed — don't re-extract
  just to check.
- If a browser_* call fails with "No browser session", you forgot to claim
  or call `browser_launch`/`browser_attach` — claim it now and call it,
  don't retry the same failing call.

