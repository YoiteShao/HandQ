---
name: skill-authoring
description: "Package a finished task into a portable HandQ skill directory the user can copy in or share with other HandQ users. Read this BEFORE writing any SKILL.md — nine separate frontmatter/encoding mistakes make a skill silently unloadable with no error the user will ever see, and this skill ships a validator that catches all of them."
enabled: true
standing: false
origin: bundled
---
# Authoring a HandQ Skill

Turn a task that just finished successfully into a reusable skill. You have the
whole trajectory in context right now — what actually worked, which step stalled,
which values were specific to this run. That context is gone next session, so
capture it while you have it.

## The deliverable is a DIRECTORY, not a file

    <skill-name>/
      SKILL.md            ← required; frontmatter + body
      reference/*.md      ← optional; bulky material the body points at
      scripts/*.py        ← optional; helper scripts the body invokes

Build it **in the task workspace** (your shell cwd), not in the user's Skill
root. You cannot install it yourself: there is no `skill_reload` IPC, so a
directly-written skill stays invisible to the running process no matter where you
put it. Installation is the user's action, and it needs a restart either way.

The layout above is exactly what HandQ's own skill-sharing transport produces
(`export_skill_files` in `src/infrastructure/skills.py`), so a hand-copied skill
and a machine-pushed one are indistinguishable. Do not invent a different shape.

## Workflow

1. **Name it.** Lowercase, hyphenated, describes the KIND of task, not this
   run's specifics: `flash-meta-build`, not `flash-sa8797-build-194`. The
   DIRECTORY name is the skill's canonical identity — frontmatter `name` is
   advisory and loses any disagreement.
2. **Write `SKILL.md`.** Read the format spec first (see Reference below). The
   frontmatter rules are unforgiving and fail silently.
3. **Split bulk out.** A full helper script, a long exact command sequence, or a
   large config template goes in a companion file that the body points at — not
   inline. The body is re-read on every future use; keep it a map, not a manual.
4. **Validate.** Run the validator (see Reference). It calls HandQ's real loader,
   so `loads: yes` means the skill will actually load — not that it looks right.
   Do not report success on an unvalidated skill.
5. **Report and instruct.** Give the user the absolute path, then the two install
   steps: copy the WHOLE directory into their Skill root, then restart HandQ.
   Say both. A user who copies only `SKILL.md` gets a skill whose scripts are
   all dangling, and a user who does not restart concludes it did not work.

## Fatal frontmatter mistakes

Each of these makes the skill load as *nothing*. No error dialog, no panel
entry, no menu line — one log warning the user will never look at. Verified
against the real loader:

- An unquoted `description` containing a colon-then-space. `description: Flash
  device: full meta` is invalid YAML and kills the whole file. **Always quote the
  description**, whether or not you think it needs it.
- A `#` in an unquoted description. It starts a YAML comment: the skill loads
  but its description is silently truncated at that point — and the description
  is the only part of a skill that is always resident in every role's context.
- A UTF-8 BOM, a blank line, or anything else before the opening `---`. The
  frontmatter must start at byte zero.
- A missing or empty `description`.
- A space or a dot in the directory name.
- A tab used for YAML indentation.

CRLF line endings are fine. Non-ASCII directory names load, but keep names ASCII
anyway so they survive being typed, pasted, and shared.

## Portability — this artifact will leave this machine

- **Never write an absolute path** into the body or a script. Reference every
  companion file through the SKILL_DIR placeholder, which is resolved to the
  skill's own directory at read time on whatever machine it lands on. The exact
  spelling is in the format reference; get it from there rather than guessing.
- **Omit `origin`.** Missing means user-owned, which is what a shared skill must
  be: visible and editable in the recipient's control panel, and protected from
  being overwritten by the memory system's auto-miner. Writing `origin: bundled`
  makes the skill invisible and immutable in the panel — the user could never
  enable, edit, or delete it.
- **Only list `allowed-tools` that exist on the target.** The activation path does
  not validate names: an unknown tool is reported as successfully activated and
  then simply is not there. `ssh`, `session`, `web_search` are broadly available;
  `desktop`, `browser`, `email`, `teams` are Windows-only and are not registered
  on Linux HandQ. Omit the key entirely for a recipe that only needs core tools
  (`shell`, `read`, `write`, `edit`, `glob`, `grep`, `todo_write` are always on —
  listing them does nothing).
- **Declare what the machine must already have.** A script needing `pyserial`, a
  reachable build server, or an installed toolchain belongs in a Prerequisites
  section. The recipient's machine is not yours.

## Write for a reader who has forgotten everything

- `description` states the TRIGGER, not the feature list. It is the one line
  every role sees every turn, so it has to answer "is this my situation?".
- Per step, record the ONE non-obvious fact that would otherwise cost a
  rediscovery. Skip what any competent agent would do anyway.
- Include a **Cannot Do** section. It stops a future agent from forcing this
  recipe onto an adjacent task it does not actually cover.
- Parameterize. Paths, hosts, IDs, and build numbers from this run become
  placeholders; if a value cannot be generalized, the task is probably not worth
  saving as a skill.

## Reference

- `${SKILL_DIR}/reference/format.md` — complete frontmatter field reference, the
  full verified failure table, the exact SKILL_DIR spelling and its two
  substitution blind spots, companion-file and process-hints conventions, and a
  worked end-to-end example. **Read this before writing frontmatter.**
- `${SKILL_DIR}/scripts/validate_skill.py` — run as
  `python <that path> <skill-dir>`. Reports whether the skill loads, whether its
  description survived intact, whether its companion references resolve, and
  whether anything in it will break on another machine.

## Cannot Do

- Install or activate the skill yourself. No IPC reloads the registry; the user
  copies the directory and restarts. Say so explicitly instead of implying the
  skill is live.
- Rely on the control panel's Import button for a skill with companion files. It
  copies `SKILL.md` alone and leaves `scripts/` and `reference/` behind, silently
  producing dangling references.
- Overwrite an existing skill of the same name. Pick a distinct name and let the
  user reconcile; a name collision in the user's root silently shadows whatever
  was there.
- Turn a single command or a one-line convention into a skill. If the whole
  procedure fits in one step, it is a note, not a skill.

## Maintenance note for editors of this skill

Every literal `${SKILL_DIR}` in THIS file's body is substituted with this
skill's own directory when the file is read. That is correct for the two
pointers above, and it is why every *illustrative* example lives in
`reference/format.md` instead — companion files are read with the `read` tool,
which does no substitution, so the placeholder survives as text. Do not inline
an example from that file into this one: it will be silently rewritten into a
path pointing at this skill, and the agent will copy that path into the skill it
generates.
