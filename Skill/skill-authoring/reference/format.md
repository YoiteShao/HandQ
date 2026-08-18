# HandQ SKILL.md Format Reference

Everything a skill needs to load, be enabled, and survive being copied to
another machine. Claims here were verified by running HandQ's actual loader
(`_load_skill_file` in `src/infrastructure/skills.py`) against each case, not
inferred from the YAML spec.

---

## 1. Where skills are discovered

Two roots are merge-scanned at boot, both flat:

| Root | Path | Purpose |
|---|---|---|
| bundled | `<install_dir>/Skill` | ships with HandQ; read in place, never copied |
| user | `%USERPROFILE%\HandQ\Skill` (Windows), `<install_dir>/Skill` (POSIX) | everything the user installs or authors |

`HANDQ_SKILLS_DIR` overrides the user root. A user skill **shadows** a bundled
one of the same name, silently and by design — that is how a shipped recipe gets
overridden. It also means a name collision quietly replaces something, so a
generated skill must not reuse an existing name.

Only **immediate subdirectories** of a root are skill candidates. Nesting is not
scanned: `Skill/a/b/SKILL.md` is invisible. Directories starting with `.` are
skipped.

---

## 2. Frontmatter fields

The file must open with a `---` block. Everything after the closing `---` is the
body.

| Key | Required | Semantics |
|---|---|---|
| `name` | no | **Advisory only.** The directory name is canonical. A mismatch is recorded as a warning and the directory wins. Set it to the directory name or omit it. |
| `description` | **yes** | Missing or empty ⇒ the entire skill is discarded. Always quote it (§3). This is the only part of a skill that is permanently resident in every role's context. |
| `enabled` | no | **Fails open** — missing or unrecognised means enabled. Disabled by exactly: `false`, `0`, `no`, `off`, `disabled`, `none`, empty. |
| `standing` | no | **Fails closed** — missing or unrecognised means NOT standing. Standing by exactly: `true`, `1`, `yes`, `on`, `enabled`. A standing skill's body is injected into every role unconditionally, with no attribution. |
| `origin` | no | Only the exact tokens `auto` and `bundled` are recognised; anything else, including absent, means `user`. See §6. |
| `allowed-tools` | no | On-demand tools to activate when the skill is read. Inline list `[a, b]` or comma string `"a, b"`. `allowed_tools` also accepted. |
| `process-hints` | no | Mapping only (§8). |

The asymmetry between `enabled` and `standing` is deliberate: a typo must never
hide a skill, and must never make one unconditionally always-on.

`standing: true` also **forces** `enabled: true` at load time — the two cannot
disagree.

---

## 3. Verified failure table

Loader run against each case. "DROPPED" means the file exists on disk and the
skill does not exist at all: absent from the menu, absent from the panel,
`read_skill` refuses it, one log warning nobody reads.

| Written | Result |
|---|---|
| `description: Flash device: full meta` | **DROPPED** — unquoted colon-space is invalid YAML |
| `description: "Flash device: full meta"` | ok |
| `description: Check build #42 status` | loads, description **silently truncated** to `Check build` |
| `description:` spanning two unindented lines | **DROPPED** |
| UTF-8 BOM before `---` | **DROPPED** |
| blank line before `---` | **DROPPED** |
| a Markdown heading before `---` | **DROPPED** |
| no `description` key | **DROPPED** |
| `description:` with empty value | **DROPPED** |
| no frontmatter at all | **DROPPED** |
| directory named `my skill` | **DROPPED** |
| directory named `my.skill` | **DROPPED** |
| tab used for YAML indentation | **DROPPED** |
| CRLF line endings | ok |
| directory named `明日香` | ok — the name pattern is Unicode-aware |
| `enabled: False` / `enabled: no` | ok, correctly disabled |

Directory name must match `^[\w\-]{1,64}$`: letters, digits, underscore, hyphen.
No spaces, no dots, no slashes, 64 characters maximum.

File name must be exactly `SKILL.md`. Linux HandQ is case-sensitive, so
`Skill.md` loads on Windows and vanishes on Linux — the worst possible failure
mode for a shared skill.

**The single most valuable rule in this document: always quote the
description.** It costs two characters and removes the two most likely fatal
mistakes at once.

---

## 4. The SKILL_DIR placeholder

Spelled exactly, with the braces:

```
${SKILL_DIR}
```

When `read_skill` returns a skill body, every literal occurrence is replaced with
the absolute path of that skill's own directory, resolved on the machine doing
the reading. This is the only correct way to reference a companion file — it is
what makes a skill portable across machines and install layouts.

The substitution is a plain string replace with no escape mechanism. There is no
way to show the placeholder as literal text inside a SKILL.md body; that is why
these examples live in this companion file, which is read with the `read` tool
and therefore not substituted.

### Two blind spots

1. **Standing skills are not substituted.** A `standing: true` skill's body is
   injected verbatim; only `read_skill` substitutes. A standing skill therefore
   cannot reference companion files at all — do not combine `standing: true`
   with companion files.
2. **Only the body is substituted.** A placeholder in `description` or any other
   frontmatter field stays literal.

### Always use the full placeholder path for scripts

The agent's shell cwd is the task workspace, never the skill's directory. A
relative path does not resolve:

```bash
# WRONG — cwd is the task workspace
python scripts/probe.py

# RIGHT
python ${SKILL_DIR}/scripts/probe.py
```

Forward slashes after the placeholder are fine on Windows.

---

## 5. Companion files

Anything under the skill's directory travels with it. Two conventions:

- `reference/*.md` — bulky material the body points at instead of inlining: a
  long exact command sequence, a large config template, a parameter table, DOM
  selectors. The body is re-read on every future use, so keeping it a map rather
  than a manual is a real token saving.
- `scripts/*.py` — helper scripts the body invokes. A script the agent runs
  through the placeholder path is never read into context at all, which is the
  main reason to prefer a script over a pasted code block.

A script must be **runnable as-is**, which pulls against being reusable. Resolve
that with real CLI arguments and sane defaults, not by hardcoding this run's
values:

```bash
# Portable: caller supplies what varies
python ${SKILL_DIR}/scripts/flash.py --meta "<build-id>" --storage ufs

# Not portable: frozen to the run it was extracted from
python ${SKILL_DIR}/scripts/flash.py    # \\some-server\share\BUILD_194 baked in
```

If a procedure cannot be parameterized this way, keep it as documented steps in
the body. A script that only works for one input is worse than prose, because it
looks reusable.

Scripts execute under whatever `python` is on the user's PATH — **not** HandQ's
own interpreter, which in a packaged build is compiled and exposes no `python`
command. Declare interpreter and package requirements in a Prerequisites
section.

---

## 6. `origin` and who may modify the skill

| Value | Panel visibility | Auto-miner may overwrite |
|---|---|---|
| absent / `user` | visible, editable, deletable | no |
| `auto` | visible, editable | yes — refreshed by the memory system |
| `bundled` | **invisible and immutable** | no |

A generated, shareable skill must **omit `origin`**. Writing `bundled` produces a
skill the recipient can see in the menu but cannot find in their control panel,
cannot disable, and cannot delete. Writing `auto` marks it as machine-owned and
invites the memory system to rewrite its contents later.

---

## 7. `allowed-tools`

Lists on-demand tools that get activated when the skill is read, so a future run
does not have to discover and claim them.

Always-on core tools — listing them accomplishes nothing:

```
shell (bash), read, write, edit, glob, grep, todo_write, read_skill,
wait_interval, spawn_agent
```

On-demand tools worth listing: `ssh`, `session`, `web_search`, `desktop`,
`browser`, `email`, `teams`, `ask_human`.

**Availability is not validated.** An unrecognised name is reported as
successfully activated and then does not exist. `desktop`, `browser`, `email`,
and `teams` are not registered on Linux HandQ, so a skill listing them is
Windows-only in practice. Note the platform requirement in the body when it
matters.

Omit the key entirely when the recipe needs nothing on-demand.

---

## 8. `process-hints`

A mapping from process name to a short fact, re-surfaced every time that process
is the acting target of a desktop click that detected no effect:

```yaml
process-hints:
  tac.exe: "none_detected is expected here — the window repaints without a UIA event. Verify via serial output, not the screenshot."
```

Keys are lowercased at load. Values must be single-line strings; a mapping is the
only accepted shape — a list or a bare string yields no hints.

This exists because a skill body enters context once, when it is read. In a long
task that text scrolls out of the model's effective attention, and knowledge that
was present at turn 1 gets rediscovered the hard way around turn 200. Process
hints reappear at the moment they are relevant, however deep the task has gone.
Use them for per-application quirks, not for general guidance.

---

## 9. Worked example

```
flash-meta-build/
  SKILL.md
  reference/parameters.md
  scripts/flash.py
```

`SKILL.md`:

```markdown
---
name: flash-meta-build
description: "Flash a full meta build to an attached COMPANY automotive device: EDL entry, Firehose load, then verification. Read before any flashing request — entering the wrong EDL mode wastes a full power cycle."
allowed-tools: [shell]
---
# Flash a Meta Build

Drive a complete meta build onto a device already wired to a TAC board.

## Prerequisites

- TAC board attached; device on the USB data channel
- `fh_loader` and `QSaharaServer` on PATH (QPM: `qpm-cli --install qfil --silent`)
- Python 3 with `pyserial`
- Meta build path reachable (UNC share or local); this skill does not fetch builds

## Steps

1. Verify the environment before touching the device — a failure here is cheap,
   a failure mid-flash costs a recovery cycle:

       python ${SKILL_DIR}/scripts/flash.py --check

2. Enter EDL. SS and MD EDL are different modes reached by different sequences;
   picking the wrong one appears to succeed and then stalls at Sahara. The
   per-chip table is in `${SKILL_DIR}/reference/parameters.md`.

3. Flash, passing the build and storage type:

       python ${SKILL_DIR}/scripts/flash.py --meta "<build-id>" --storage <ufs|nvme>

4. Verify by reading the booted device's firmware version, not by the flasher's
   exit code — the loader reports success on a partially provisioned device.

## Cannot Do

- Operate a device with no TAC board: power and mode cannot be controlled
- Fetch or build meta images; the path must already exist
- Flash a single partition — use fastboot for that
```

Note what the example does: quotes the description even though it contains a
colon, omits `origin`, uses the placeholder for both the script and the reference
file, parameterizes the build ID and storage type, records one non-obvious fact
per step, and states its limits.

---

## 10. Installation and sharing

There is no `skill_reload` IPC. A directly written skill is invisible to the
running process regardless of where it was written, so installation is always a
user action:

1. Copy the **entire directory** into the Skill root
   (`%USERPROFILE%\HandQ\Skill\` on Windows).
2. Restart HandQ.

Both steps are required, and both must be stated. Copying `SKILL.md` alone
produces a skill whose every placeholder reference dangles; skipping the restart
looks identical to the skill not working.

The control panel's Import button is **not** a substitute for a skill with
companion files: it reads the single `SKILL.md`, re-renders it into the user root,
and leaves `scripts/` and `reference/` behind. Use it only for a single-file
skill.

Sharing with another HandQ user is the same operation — send the directory, they
copy and restart. This is the manual equivalent of HandQ's own transport
(`export_skill_files` / `receive_skill_push`), which moves exactly this directory
as a file bundle, so the two are interchangeable.
