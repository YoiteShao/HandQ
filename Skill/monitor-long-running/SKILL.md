---
name: monitor-long-running
description: When an item must observe a long-running process (build, stress test, deploy, pipeline) over time and react to completion/error/hang
origin: bundled
---
# Monitoring a long-running process

When the task is to watch a process over an extended period and act on how it
ends, run this cycle instead of free-form polling.

## Launch: use shell's own background mechanism, not a hand-rolled one
Every `shell` call runs in a FRESH process — nothing survives between calls
(no shell vars, no `Start-Job`/`Get-Job` PSJob objects, no PowerShell `&`
background operator). A job you background with PowerShell's own primitives
disappears the instant that call's process exits; the next check's `Get-Job`
will report `NotFound` even though the real work is still running fine.

Launch with `shell(command=..., run_in_background=true)` instead — it returns
a `task_id` immediately and the OS process is tracked for you across every
subsequent call in this session, no PID file or state file needed for
liveness. Then EITHER:
- poll it yourself: `shell(task_id=...)` returns `status` (running/done/
  killed) + `elapsed_seconds`, and stdout/stderr once done; or
- do other work and wait — a completed background task is injected into your
  next turn automatically as a tool observation, no polling call needed.
To stop it: `shell(task_id=..., command="kill")`.

## Per cycle (once you also need periodic classification/branching, e.g. a
hang timeout the OS-level status alone doesn't capture)
1. Read your state file (or initialize it on the first cycle).
2. Check status via `shell(task_id=...)` — do NOT re-derive liveness from
   `Get-Job`/`Get-Process`/log growth; those are unreliable across the
   fresh-process boundary (see above) and log growth specifically is
   unreliable whenever the child buffers stdout (default for a plain
   `print()` redirected to a file, not a TTY) — the file can sit at 0 bytes
   the whole run and jump to full size only at exit, which looks identical
   to "hung" until the last check.
3. Classify: HEALTHY / ERROR / HUNG / COMPLETE, using the task_id status as
   the primary signal.
4. Branch:
   - COMPLETE → collect results, finish the item.
   - ERROR → capture diagnostics (stdout/stderr from the task_id result),
     notify, finish.
   - HUNG → diagnose, `shell(task_id=..., command="kill")`, capture, notify,
     finish.
   - HEALTHY → update state file, `wait_interval(N)`, next cycle.

## State file (JSON in the working dir, e.g. `.monitor_state.json`)
Only needed for cross-cycle bookkeeping the task_id doesn't already give you
(e.g. consecutive-stale count for a HUNG heuristic). Track `check_count`,
`started_at`; do NOT re-track liveness/pid here — that's what `task_id`
already answers. Read at cycle start, write at cycle end.

## Anti-drift
- Each cycle is observe → classify → branch. No exploration in between.
- Do NOT try to "fix" the monitored process unless the task says to.
- "HUNG" = a concrete signal (e.g. `task_id` still `running` well past the
  process's expected duration, or output genuinely unchanged for 3
  consecutive checks on a channel you've confirmed isn't buffered), not a
  guess.
- If a check reports COMPLETE almost immediately, that means the process
  really did finish fast — trust it and move to the COMPLETE branch. Don't
  re-launch a fresh instance to "properly" observe an in-progress state;
  a process finishing faster than expected is not a bug in your monitoring.

