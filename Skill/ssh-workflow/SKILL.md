---
name: ssh-workflow
description: SSH remote execution — pooled connections, long-running job pattern, log inspection
enabled: true
standing: false
origin: bundled
allowed-tools: [ssh]
---
# SSH Remote Execution Workflow

Pass `credentials_file` (a local YAML/JSON path from `ssh_setup`) — never
hostname/username/password directly. Only the path enters LLM context.
First call to a new host: pass `ssh_target="user@host"` instead — the tool
establishes credentials itself and returns `credentials_file` to reuse.

## Recommended pattern for a long-running remote job

1. `exec` — verify environment, run anything under ~30s. Returns `login_shell`.
2. `run_script` — upload a script and launch it as a detached background job
   that survives SSH disconnect.
3. `wait_done` — a single connection blocks until the job finishes (preferred
   over polling: set `timeout` to expected duration + buffer). Alternative:
   `job_status` polled every 30-60s when interleaving other work — `log_tail`
   is omitted while `status="running"`, use `tail_log` to peek at live output.
4. `tail_log` / `fetch_log` — inspect output on success, or page through a
   large log to debug a failure (`start_line`/`end_line`).
5. `safe_exit` — always call when done: kills tracked jobs, removes pid files.

`run_script` covers the common case (write + launch in one call). For finer
control: `write_file` alone just uploads without launching; `exec_bg` launches
an already-present remote command/script as a detached job without an upload
step.

## Actions

`exec` | `exec_bg` | `job_status` | `wait_done` | `tail_log` | `fetch_log` |
`write_file` | `run_script` | `safe_exit`

## Connection management

All actions to one host share a single pooled TCP connection — first action
pays the handshake, subsequent actions reuse it. Auto-reconnects on transport
death with exponential backoff; a 30s keepalive prevents NAT/firewall from
dropping an idle connection.

## Job directory (quota-aware)

`run_script`/`exec_bg` job artifacts default to `~/handq_jobs`, but the home
directory on some hosts (e.g. NetApp NFS mounts) has a small per-user quota
unrelated to the filesystem's actual free space. The tool auto-probes
`~/handq_jobs`, then `/tmp`, then `/local/mnt/workspace` and uses the first
one that accepts a real write, caching the result per host — you don't need
to handle this yourself. `run_script` also accepts an optional `job_base_dir`
to force a specific location.

## Shell compatibility

Built-in actions (`exec_bg`, `job_status`, `safe_exit`) wrap commands in
`bash -c` regardless of the remote login shell. For `exec` on a non-bash host,
wrap yourself: `command='bash -c "your_command"'`.

## When NOT to use ssh

- A local command — use `shell`.
- A single short remote command with no log/job tracking — use `shell` with
  `ssh host 'cmd'`.
- Live UI streaming of remote output — use `live_shell_open` (`open(command='ssh user@host')`,
  `-tt` auto-prepended).
