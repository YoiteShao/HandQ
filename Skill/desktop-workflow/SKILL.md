---
name: desktop-workflow
description: Windows native app automation. Claim desktop_snapshot and desktop_find_and_click TOGETHER with any action tool (click_at/type_text/hotkey) — skipping them forces slow screenshot+OCR guessing for the rest of the task. Covers action hierarchy, takeover rules, safety constraints, and a concrete rule for acting on OCR/snapshot text that matches your goal instead of hunting for an icon.
enabled: true
standing: false
origin: bundled
allowed-tools: [desktop_screenshot, desktop_snapshot, desktop_click_at, desktop_find_and_click, desktop_find_element, desktop_type_text, desktop_hotkey, desktop_key_press, desktop_drag, desktop_scroll, desktop_list_windows, desktop_hover_at]
---
# Desktop Automation Workflow

## Claim Checklist (read this BEFORE calling claim_tool)

Claim ALL of these together on your first claim_tool call for desktop work —
not just the ones that sound like "actions":

  claim_tool(names=["desktop_snapshot", "desktop_find_and_click",
                     "desktop_click_at", "desktop_type_text",
                     "desktop_hotkey", "desktop_list_windows"])

`desktop_snapshot` and `desktop_find_and_click` don't sound like they "do"
anything, but skipping them locks you onto the slow screenshot+OCR fallback
for the ENTIRE rest of the task.

Caveat: some apps (many Electron/Chromium-based ones) do not expose their
content to UIA at all — snapshot returns only 1-2 elements (the outer window
shell). If snapshot comes back nearly empty, that's a real signal — don't
retry it expecting a different answer. Before falling back to screenshot+OCR,
check whether the app can be started with (or is already running with) a
`--remote-debugging-port=<port>` flag — if so, prefer `browser_attach`
(see browser-workflow skill) over screenshot+OCR: it drives the same DOM
deterministically instead of guessing from pixels, and it is not limited to
actual browser windows. Only move to screenshot+OCR as primary when no
debug port is available.

## MECHANICAL RULE — before you decide "I need to find X icon", re-read the
## OCR/snapshot text you already have

This is a concrete trigger-action rule, not a general principle: apply it
literally, every time, BEFORE reasoning about what icon might exist.

**TRIGGER:** Your goal mentions a noun or verb (e.g. "settings", "provider",
"configure", "temperature", "preferences", "save", any app-specific term from
the user's instruction). You are about to call a tool whose purpose is
"look for an icon" (desktop_hover_at on an unlabeled icon, another
screenshot hoping to spot a gear symbol).

**ACTION:** STOP. Re-read the text_regions/OCR results and the snapshot
summary you ALREADY received in this conversation. Check every text string
for a partial word-match against your goal's nouns/verbs (e.g. goal says
"configure the AI provider" or even just relates to app settings generally,
and OCR already found "ConfigureProvider" or "Configure Provider" — that is
a match, click it now). A partial or approximate match beats no match. If
you find ANY match you have not yet clicked, click it via desktop_click_at
using its OCR-reported (x, y) THIS TURN — do not take another screenshot
first, you already have the coordinates.

**Why this matters:** settings/config UIs are usually gated behind ONE entry
point (a button, not necessarily a gear icon), and that entry point's label
routinely already appears in OCR/snapshot text you captured turns ago. Going
back to hunt for an icon you haven't found in 3+ tries, while a textual match
sits unused in a previous tool result, is the single most expensive mistake
in desktop navigation — each extra screenshot/hover costs a full turn for
zero new information, while the textual match was already sitting there.

**Do not talk yourself out of the match.** A real failure mode: seeing a
button like "Configure Provider" and reasoning "but I need Settings, not
provider config" — then going back to hunting for a gear icon instead of
clicking it. This reasoning is backwards. Apps that gate AI/model config
behind a "Configure Provider" (or "Connect a provider" / "Add API key" /
similar) button almost always put ALL related settings — including chat
generation parameters like temperature — in that SAME settings surface,
because from the app's perspective "configure your provider" and "configure
how chat behaves" are the same settings area, just different tabs/sections
within it. Treat ANY button whose label overlaps your goal's domain
(provider/model/chat/AI/config/settings/preferences) as your best candidate
and CLICK IT, then look at what tabs/sections exist inside. Do not require
an exact label match before clicking — that standard is too strict for how
real UIs are labeled, and it is exactly what causes stalling.

**After clicking:** you may land on a general config screen, not the exact
sub-setting yet (e.g. "Configure Provider" opens AI-provider setup, not Chat
Temperature directly) — that is fine and expected. Re-apply the same
MECHANICAL RULE on the NEW screen's OCR/snapshot text against your goal's
remaining nouns. Chained clicks through intermediate screens is normal
navigation, not a wrong turn — only backtrack if a click produces NO visual
change at all (compare before/after) or you land somewhere with no textual
lead at all.

**A click that opens a NEW top-level window** (settings/preferences panels
are often a separate OS window, not a panel inside the one you clicked from)
invalidates any `hwnd` you were passing to `desktop_screenshot`/
`desktop_click_at` — that hwnd still points at the OLD window and will keep
showing you its stale, unchanging content forever, making it LOOK like your
click did nothing or the new window "closed itself". If a screenshot with an
old hwnd shows the exact same content it showed before your last click
(down to identical OCR text), do NOT conclude the click failed or re-click
the same thing — instead call `desktop_list_windows` (or screenshot with
`region="foreground"`/`"fullscreen"`, no hwnd) to find the NEW window's own
hwnd, and target that one from here on.

## ROW-LEVEL CONTROLS ARE OFTEN AN ICON, NOT THE ROW ITSELF

Clicking a list/table ROW (its text label, its highlighted background)
frequently only SELECTS it — a highlight-color change that shows up as
`content_changed: true` / `effect: navigated` even though nothing you
actually wanted happened (no detail panel, no expand). This is a common,
easy-to-miss trap: the effect signal is being honest about a REAL pixel
change, but that change is the selection highlight, not your goal.

**The actual action is frequently a separate, unlabeled icon-only control**
next to or inside the row: a chevron/arrow (expand/collapse), a plus/gear/
three-dot overflow menu, a checkbox — none of which have OCR-able text, so
they will NEVER show up as a text match no matter how carefully you re-read
the OCR/snapshot output. This is the mirror-image case of the MECHANICAL
RULE above (which is about preferring a TEXT label over hunting for an
icon) — here there IS no text label, because the control genuinely is just
a glyph.

**TRIGGER:** you clicked a row (or its visible text) and got `effect:
navigated`/`content_changed: true`, but re-checking the OCR/snapshot after
shows the same layout, no new panel, no expanded content — i.e. the "change"
was cosmetic (selection/hover), not the navigation you wanted. You've now
tried this 2+ times, possibly at slightly different x/y within the same row.

**ACTION:** stop retrying coordinates inside the row. Call
`desktop_snapshot` (or `desktop_find_element` if snapshot came back empty
for this app) and look specifically for a SEPARATE small control at the
row's edges — most commonly a narrow zone at the far LEFT or far RIGHT of
the row (chevron/arrow/caret), or directly on top of an icon-shaped glyph
the OCR ignored (icons have no text, so they're invisible to OCR — you have
to reason about their probable position, e.g. "before the label" or "at the
row's right edge", not search OCR text for them). If snapshot lists an
element there with a generic role (Button, TreeItem with an expand
affordance), click ITS coordinates, not the row's.

## Tool Choice Hierarchy

Re-check before each desktop action:
- ✅ Use desktop ONLY for NATIVE Windows apps (Notepad, Excel, File Explorer, Settings, Task Manager)
- ❌ Web pages / URLs → browser (DOM is deterministic, ~10x faster)
- ❌ File read/write/edit → read / write / edit
- ❌ Run command / script → shell
- ❌ Search files → glob / grep
- ❌ Remote machine → ssh

Even if you can SEE a target on screen, prefer the specialised tool — desktop is the LAST resort.

## Action Hierarchy

Check top-down before each action:
1. **hotkey / key_press** — fastest, no vision dependency. If you know the shortcut (Ctrl+S, F5, Alt+F4), USE IT.
2. **find_and_click** on a UIA-named control — robust to layout shifts.
3. **snapshot → click_at(x, y)** — structured listing of every interactable control. Cached per hwnd.
4. **screenshot+OCR / vision_fallback** — last resort. Slow (~700ms-5s).

## Typical Workflow

1. `shell(...)` — launch target app if not already open.
2. `desktop_list_windows` — confirm the app is foregrounded.
3. If you know the hotkey → fire `desktop_hotkey(...)` directly.
4. `desktop_snapshot` — for unfamiliar UIs. Cached until state changes. DO THIS
   before reaching for screenshot+OCR. If it comes back nearly empty (UIA not
   supported — see Caveat above), stop retrying it and move to screenshot+OCR.
5. `desktop_find_and_click` — when target is visual-only or textual.
6. `desktop_screenshot` with `with_ocr=true` — only when #3-5 aren't enough.
7. Before EVERY further observation call, apply the MECHANICAL RULE above.
8. `desktop_type_text` / `desktop_drag` / `desktop_scroll` — drive specifics. type_text capped at 4000 chars.

## Takeover + Revocation

- On approval: rainbow border + watermark appears (user knows agent is driving)
- User can hit **Ctrl+Shift+C** to REVOKE control
- After revocation: click_at / type_text / drag / scroll / hotkey need RE-APPROVAL
- Read-only actions (screenshot / list_windows) keep working

## Sensitive Window Refusal (HARD)

Banking / password manager / wallet app foreground is refused outright. Cannot bypass.

## Password Guard

NEVER type a password. If a field looks like a password prompt, do not type into it — ask the user.
