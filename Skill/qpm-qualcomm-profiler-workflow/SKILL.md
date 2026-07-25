---
name: qpm-qualcomm-profiler-workflow
description: Installing/downloading anything through QPM (Qualcomm Package Manager 3) or fetching Qualcomm Profiler specifically — QPM3 is a windowless Electron app invisible to desktop UIA, has a fast CLI shortcut most tasks should use instead of the GUI, and its underlying QIK package manager has a version-downgrade trap that is expensive to recover from.
origin: bundled
---
# QPM3 / Qualcomm Profiler Workflow

Distilled from three live runs (39min GUI-only success, ~64min GUI-then-CLI
success, 61min GUI-then-QIK-conflict success). The single most expensive
mistake across all three was NOT trying the CLI first.

## 0. Try the CLI FIRST — it is usually 40x faster than the GUI

Before touching the GUI, check for `qpm-cli.exe` (a SEPARATE, older product
from `qpm3-cli.exe` — do not confuse them):

- **`qpm-cli.exe`** — `C:\Program Files (x86)\Qualcomm\QPM-CLI\<ver>\qpm-cli.exe`
  (multiple version dirs coexist, e.g. `1.0.128.10`, `.11`, `.12`; use the
  highest). **This is the one that can actually install things.** Key flags
  (`--help`): `--product-list`, `--info <product>`, `--download-only <product>`,
  `--install <product> [-v <version>] [--silent]`, `--uninstall <product>`.
  Product naming: `qualcomm_profiler` = public/non-QC-only channel,
  `qualcomm_profiler.internal` = QC-only channel — this is how "non-QC only"
  is expressed on the CLI (no separate flag).
  ```
  qpm-cli.exe --install qualcomm_profiler -v 2.26.5.6 --silent
  ```
  Verified: completes in under a minute, vs 20-60+ min for the GUI path below.

- **`qpm3-cli.exe`** — `C:\Program Files (x86)\Qualcomm\QPM3\<ver>\qpm3-cli.exe`
  (ships alongside the GUI). Its subcommands are `login, logout,
  build-download, diff-build-download, build-ship, diff-build-ship,
  list-tags, list-crs, get-parent-info, get-folder-paths` — **this is a
  build-shipping tool, it CANNOT install/download end-user packages.** Do
  not waste time here; two prior runs each independently rediscovered this
  before moving on.

Only fall through to the GUI/CDP workflow below if `qpm-cli.exe` doesn't
exist, doesn't support the target product, or the user explicitly asked to
use the QPM application/GUI specifically.

## 1. If GUI is required: it MUST be driven via CDP, never desktop/UIA

**QPM3 is a windowless Electron/Chromium app.** `EnumChildWindows` on its
hwnd returns zero children — there is no native Win32 control tree for
mouse/keyboard events or UIA to attach to. `desktop_click_at` /
`desktop_snapshot` etc. will deliver clicks that are silently swallowed or
report `effect: navigated` on a mere selection-highlight change (see §3).
**Do not attempt pyautogui / SendInput / UIA on this app.** Go straight to
Chrome DevTools Protocol:

```powershell
# Kill any existing instance first — a stray one without the debug flag
# will not have the port open.
Get-Process QualcommPackageManager3 -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Process "C:\Program Files (x86)\Qualcomm\QPM3\<ver>\QualcommPackageManager3.exe" `
  -ArgumentList "--remote-debugging-port=9222"
# poll until the port is listening, then:
```
```python
import json, urllib.request
pages = json.loads(urllib.request.urlopen("http://localhost:9222/json").read())
# pages[0]["webSocketDebuggerUrl"] -> ws://localhost:9222/devtools/page/<id>
```
Drive it with `websocket-client` (`pip install websocket-client` if
missing) sending `Runtime.evaluate` (JS), `Input.dispatchMouseEvent`,
`Input.insertText`. Avoid `Page.captureScreenshot` — it has timed out and
killed the automation script in more than one run; poll
`document.body.innerText` instead to check state.

## 2. Navigation map (Angular hash routes inside the Electron app)

The app URL is `file:///.../app.asar/dist/QPM/#/main/<section>` — Angular
hash routing, not HTTP.

- `#/main/home` — landing page
- `#/main/software` — chip/SPF picker; Qualcomm Profiler is NOT here
- `#/main/tools/find` — the tools list/search page you actually want.
  **Jumping straight to this exact hash via `location.hash = ...` works and
  is more reliable than clicking through menus.** (Jumping to the shallower
  `#/main/tools` instead returns "Access Denied" — always use the deeper
  `/find` path.)
- `#/main/tools/details/<ToolName>` — a tool's detail/install page
- Appending `?version=X.Y.Z` to the details URL does NOT preselect a
  version in the dropdown — don't rely on it.

**The top nav "Tools" label is a two-level menu, not a direct link** —
clicking the top-bar span does not navigate; you must open the menu and
click the (separately-rendered, same-looking) submenu item.

## 3. The `<tr>` row is (usually) not clickable — click its toggler instead

Clicking a tree-table row's bounding-box center (JS `.click()` or a
synthesized mouse event) frequently does NOTHING to the URL/content, or
worse, reports `content_changed: true` / `effect: navigated` from a mere
row-selection highlight — this is a real pixel change, just not the one you
wanted. The actual expand control is a small **PrimeNG
`p-treetable-toggler`** button (chevron icon, ~14px, `data-pc-section=
"rowtoggler"`) nested INSIDE the row, not the row itself. Query for that
element specifically and click IT. After expanding, the child rows
themselves ARE directly clickable (no toggler needed one level down).

**Text match note:** the rendered label is `"Qualcomm® Profiler"` (with the
registered-trademark glyph) — a plain-ASCII `"Qualcomm Profiler"` string
match will spuriously report "not found" against real page text.

**Channel selection:** there is no separate "non-QC only" checkbox/filter.
Searching "Qualcomm Profiler" and expanding the tree yields two child rows —
one suffixed `"(QC Only)"` and one without. **Pick the one WITHOUT the
suffix for "non-QC only."**

## 4. Version dropdown — try multiple click strategies, one WILL fail

The version `<p-dropdown>`'s behavior is inconsistent across runs: clicking
the inner `.p-dropdown-label` span opened it in one run; in another, that
AND clicking the outer div AND the trigger arrow AND keyboard ArrowDown all
failed silently (`options: NONE`). The only fallback that has worked when
simple clicks fail is dispatching a synthesized DOM event sequence via JS on
the dropdown element (not a plain `.click()`). Try label-click first, fall
back to synthetic-event dispatch — don't give up after one method fails.

**⚠️ HIGH-COST TRAP: confirm the dropdown actually shows your target version
selected BEFORE clicking Install.** If the dropdown never opened and you
click Install anyway, QPM silently installs whatever version was already
showing (usually the newest) — see §5 for how expensive this mistake is to
undo. Always re-read the visible version text right before clicking Install.

After Install: a terms dialog ("Please review the terms below... I Accept /
Cancel") must be accepted to proceed. Poll page text for `"Install Summary
Install succeeded"` **combined with the target version number** — a leftover
status banner from a PREVIOUS install attempt can still say "succeeded" and
cause a false positive if you only check for the phrase.

## 5. If the wrong version got installed: the QIK downgrade trap

QPM3's installs are managed by a separate service, QIK (`C:\ProgramData\
Qualcomm\qik\`), which enforces a downgrade lock **via the Windows registry**
(`HKLM:\SOFTWARE\Qualcomm\QIK\Components\<component-GUID>`), independent of
the files on disk. If you need to replace an already-installed newer version
with an older target version:

- A plain silent installer run (`<installer>.exe /S`) for the older version
  returns **exit code 48 = DowngradeError**.
- Uninstalling first (`/S /uninstall`, returns exit 254 = success) and
  deleting `C:\ProgramData\Qualcomm\qik\components\qikcomponent_<GUID>.dat`
  and restarting the `QIKService3` service is **NOT enough** — the registry
  lock is untouched by any of that, and the downgrade error persists.
- **The actual fix**: `qikv3.exe INSTALL <package> -force` on the individual
  cached component package bypasses the top-level installer's downgrade
  check. You may need to do this per-component (Qualcomm Profiler is 5
  separate QIK components: `.Core`, `_API`, `_CLI`, `_QCP`,
  `_Utility_App`) — if the older version's package for one component was
  already deleted by a failed uninstall attempt, that one component may be
  stuck on the wrong version while the rest succeed. This is an acceptable
  outcome if the actual functional check (e.g. `qprof -ca` succeeding on
  device) passes — QPM's own GUI status text can legitimately disagree with
  on-disk reality after this kind of recovery; don't chase GUI-status
  consistency once the functional target is met.

**The cheapest fix is prevention: get §4's confirm-before-Install step right
and you never need this section.**

## 6. InstallerLE.exe (device-side Profiler API installer)

Path: `C:\Program Files (x86)\Qualcomm\Shared\QualcommProfiler\API\
target-le\InstallerLE.exe`. Internally shells out to bare `adb` (no `-s`
flag) for several steps (`adb shell "exit"`, `adb root`, `adb wait-for-
device`, remount). **With more than one device attached, these bare calls
fail with `"adb.exe: more than one device/emulator"` — and passing `-s
<serial>` to InstallerLE.exe itself does NOT fix this**, because it doesn't
propagate to the internal bare calls. Fix: set the environment variable
before running it —
```powershell
$env:ANDROID_SERIAL = "9f0e75d7"
& "C:\...\target-le\InstallerLE.exe"
```
A `qprof: not found` line during its pre-install cleanup step is expected
noise on a device that never had qprof installed — not a failure signal.

## 7. Device-side environment + capability check

Three env vars, then `qprof -ca`:
```
QMONITOR_BACKEND_LIB_PATH=/var/QualcommProfiler/libs/backends
PATH=$PATH:/data/shared/QualcommProfiler/bins
LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/var/QualcommProfiler/libs
```
**Do not inline this into a PowerShell string** — PowerShell expands
`$PATH`/`$LD_LIBRARY_PATH` itself before adb ever sees them, producing
`"Variable reference is not valid"` errors (hit in every run that tried it
inline, regardless of quoting style). Write the full `adb -s <serial> shell
"export ...; export ...; export ...; qprof -ca"` command as a literal string
inside a `.py` file and run that file — this is the only reliable path.

`qprof -ca` (exit 0 on success) prints a capability table: `Friendly Name |
Capability | Streaming Rate(s) | Sampling Rate(s) | Available Metric(s)`,
one row per subsystem (CPU, DDR, GPU, NPU0-3, HPASS0-2, Memory, Network,
Process, Thermal, Thread, ...). Seeing this table with the expected
subsystems is the functional pass/fail signal for the whole task —
harmless startup noise (`drm fe debug is not enabled`, `gbm_create_device
... backend name is: ki-umd`) can be ignored.

## Priority checklist (read top-to-bottom before starting)

1. Does `qpm-cli.exe --install <product> -v <version> --silent` cover the
   whole task? If yes, stop here — do not open the GUI at all.
2. If GUI is required: CDP only, never desktop/UIA (§1).
3. Before clicking Install, visually confirm the version dropdown shows your
   TARGET version, not whatever was pre-selected (§4) — this single check
   avoids the most expensive failure mode in this whole workflow (§5).
4. Multiple ADB devices → set `$env:ANDROID_SERIAL` before InstallerLE.exe
   or any bare adb call (§6).
5. Any adb shell command using `$PATH`/`$LD_LIBRARY_PATH` → write it to a
   `.py` file, never inline in PowerShell (§7).
