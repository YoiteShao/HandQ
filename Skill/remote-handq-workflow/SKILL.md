---
name: remote-handq-workflow
description: When delegating a complex, multi-step task to a remote Linux HandQ agent that must plan and execute autonomously on its own side (not just run known commands)
origin: bundled
allowed-tools: [remote_handq]
---
# Remote HandQ delegation

Use `remote_handq` when the REMOTE side must reason/plan/branch on its own — not
when you already know the exact commands (that's the `ssh` tool). Reading this
skill activates the `remote_handq` tool. Pass `ssh_target="user@host"` on your
first call to a new machine — the tool establishes SSH credentials itself and
returns `credentials_file`; pass that exact path on subsequent calls to the
same host.

## Actions
`discover`, `ensure_installed`, `submit_goal`, `send_message`, `get_status`, `get_result`,
`get_confirmation`, `answer_confirmation`, `new_session`, `interrupt`, `exit_handq`.

## Workflow
1. `discover()` — locate a reachable remote HandQ daemon on the target host before anything else if you're unsure one is running.
2. `ensure_installed()` — deploy/upgrade handq_linux on the remote from the configured share path; call this if `discover` shows none installed or an outdated version.
3. `submit_goal(goal=...)` → returns a `message_id`.
4. `get_status(wait_timeout=N)` → poll until the remote task settles.
5. `get_result(message_id=...)` → fetch the reply for that message_id.

`submit_goal` / `send_message` return a `message_id`; pass it to `get_result`.
Use `send_message` (not a fresh `submit_goal`) to continue the SAME running
remote session — e.g. answering a remote-side clarifying question.
`interrupt` stops the remote task's current work without ending the session;
`exit_handq` shuts the remote daemon down entirely — only call it when you
are certain no further delegation to that host is needed this session.

## Confirmations
If `get_status` reports `pending_confirmation` (a risk/tool approval or an
ask_human prompt), the remote task is BLOCKED until you answer:
- tool/risk approval → `answer_confirmation(confirmation_id=..., decision=yes|no|message)`
- secret/text prompt → `answer_confirmation(confirmation_id=..., value=...)`

It auto-refuses after a timeout, so answer promptly.

## Reading a fresh session
Start with a clean slate via `new_session` when a prior session's state on the
remote could contaminate your result (old replies still in the reply dir).

## When it looks stuck
If `get_status` reports `daemon_alive: true` but the whole state is empty, that
is a read-path failure, not "nothing happening" — surface it, don't keep polling.
