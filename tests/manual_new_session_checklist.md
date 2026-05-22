# Manual smoke-test checklist — `new_session` cancellation

Use this after running `python tests/test_tool_cancellation.py` succeeds.
The unit tests prove the primitives work in isolation; this checklist
proves the end-to-end UI path delivers on the user-visible promise.

## 0. Setup

```
cd electron
npm start
```

Open the bridge log in a separate terminal so you can watch it in real time:

```
# Path is printed in the Electron app's console at boot:
#   [MAIN] app ready { log_dir: "...\\logs\\<TS>", ... }
# It's the latest <TS> directory under either:
#   <repo>/logs/<TS>/handq-bridge.log   (dev mode)
#   %LOCALAPPDATA%\HandQ\logs\<TS>\handq-bridge.log   (packaged)
Get-Content -Wait <path>\handq-bridge.log
```

## Test 1 — Click New on a fresh session (cold path)

**Steps**

1. Type `hello` in the composer, press Enter.
2. Wait for the assistant to reply.
3. Click **New** in the shortcut bar.

**Expected log lines (in order)**

```
new_session sequence begin; id=... old_gen=0 new_gen=1
new_session: cooperative drain ok (X.XX ms)        ← typically <50ms
new_session: FileState cleared
new_session: SSH pool flushed (0 clients closed)
new_session sequence complete (Y.YY ms; new_gen=1)
```

**Expected UI**

- Conversation pane clears.
- Pill returns to `idle`.
- Composer focused.

**Pass criteria**

- `new_session sequence complete` line appears within ~1s of the click.
- `cooperative drain ok` (NOT `escalating to cancel`) — clean shutdown.
- `new_gen=1` matches what the renderer expects (you can see this in the
  Electron debug log via Ctrl+Shift+L).

## Test 2 — Click New mid-task (the wedge case)

**Steps**

1. Send a goal that runs for a while, e.g.:
   `请运行 ping baidu.com -n 50 这个命令` (or any long bash that streams output).
2. Wait until you see streaming output / status updates flowing.
3. Click **New** mid-stream.

**Expected log lines**

```
new_session sequence begin; id=... old_gen=N new_gen=N+1
new_session: cooperative drain ok (X.XX ms)        ← here X should be a few hundred ms,
                                                     because the bash tool's
                                                     _kill_process_tree path runs
                                                     (Windows: taskkill /F /T;
                                                      Linux: SIGTERM→SIGKILL of pgroup)
new_session: FileState cleared
new_session: SSH pool flushed (0 clients closed)
new_session sequence complete (Y.YY ms; new_gen=N+1)
```

**Expected UI**

- Pill briefly shows the current tool state, then transitions away.
- Conversation clears.
- Crucially: **no further status events from the old goal appear in the new
  conversation**. Open Ctrl+Shift+L; you should see entries like
  `gateGen drop` (or the events simply not arriving in onTokenStream/onStatus
  handlers, depending on logging verbosity).

**Pass criteria**

- `new_session sequence complete` within ~3s.
- Bridge log shows the bash subprocess being killed BEFORE
  `cooperative drain ok` (Windows: `[bridge]` lines mentioning taskkill;
  Linux: SIGTERM/SIGKILL).
- Send a new message immediately after — it gets a fresh response with
  no leakage from the old conversation.

## Test 3 — Click New during an SSH operation

Requires a credentials file pointing at a reachable host (or use a
deliberately unreachable IP to test the retry-backoff wedge).

**Steps**

1. Send a goal that uses SSH:
   - Reachable host: `ssh into example.com and run sleep 30`
   - Unreachable host (worst case wedge): point credentials at
     `203.0.113.1` (TEST-NET-3, RFC 5737, will time out)
2. Wait for SSH activity in the pill (e.g. `⊙ ssh: connect`).
3. Click **New**.

**Expected log lines**

For reachable host (long running sleep):

```
new_session sequence begin
ssh: exec-poll aborted          ← interruptible_sleep returned True
new_session: cooperative drain ok
new_session: SSH pool flushed (1 clients closed)    ← the in-flight client was registered
                                                      and force_closed
```

For unreachable host (in retry backoff):

```
new_session sequence begin
ssh: connect-retry aborted      ← interruptible_sleep in _new_client retry loop
new_session: cooperative drain ok
```

**Pass criteria**

- The unreachable-host case (retry-backoff wedge) is the killer test.
  Pre-refactor it would have wedged for `_BASE_DELAY * 2^N` seconds (up
  to ~32s on attempt 5). Post-refactor it must abort within ~50ms.
- `cooperative drain ok` appears with elapsed time well under the
  pre-refactor backoff.

## Test 4 — Stress test: rapid New clicks

**Steps**

1. Type a goal, send.
2. Click **New** 10 times in quick succession (every ~200ms).

**Expected log lines**

```
new_session sequence begin; old_gen=N new_gen=N+1
new_session sequence complete (... new_gen=N+1)
new_session sequence begin; old_gen=N+1 new_gen=N+2
new_session sequence complete (... new_gen=N+2)
... (10 such pairs)
```

**Pass criteria**

- All 10 cycles complete; no exception logs.
- `old_gen` of cycle N+1 always matches `new_gen` of cycle N (monotonic).
- Final `new_gen` in the bridge matches `currentGen` in the renderer
  (visible via Ctrl+Shift+L).

## Test 5 — File I/O on a slow filesystem

**Steps**

1. (Optional, hardest) Mount or use a slow network share / OneDrive
   online-only file. If you don't have one handy, skip — the unit test
   `test_read_tool_does_not_freeze_event_loop` already covers the
   essential property.
2. Send a goal: `read the file at <slow-path>`.
3. **While the read is in flight**, watch the bridge log. Status events
   (`step_started`, `decision_made`, etc. from later steps) MUST keep
   ticking — the bridge event loop is not frozen.

**Pass criteria**

- Event loop ticks visible during the slow read (any log line with a
  timestamp progressing).
- If you click New mid-read, the bridge accepts it within ~1s (the
  read's executor thread becomes an orphan that finishes on its own
  schedule; the pool flush + FileState reset happen anyway).

## What "failing" looks like

- `cooperative drain expired after 2.5s; escalating to cancel` —
  acceptable on rare wedges, but if it happens every time, the
  cooperative interrupt path is broken for that tool.
- `flow_task did not drain after cancel within 1.5s; leaving as
  background` — the orphan accepted path. With generation tags this is
  still safe (no new-flow contamination) but indicates a tool that
  isn't honoring the interrupt; investigate which tool was active
  before the click.
- New-flow conversation showing tokens from the old goal — the
  generation filter isn't wired correctly. Check that `evt.gen` is
  present on incoming envelopes (Ctrl+Shift+L → look for `gen:` in the
  log).

## Cross-platform note

The unit tests run on both Windows and Linux. The end-to-end tests
behave identically on both because the cancellation primitives use
only `asyncio + threading` — no platform branches. Where platform
divergence exists (subprocess kill in `bash_tool`, SO_LINGER on close)
it's confined to existing well-tested code paths that haven't changed
in this refactor.
