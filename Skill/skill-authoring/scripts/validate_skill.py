#!/usr/bin/env python3
"""Validate a HandQ skill directory before handing it to a user.

Answers the only question that matters for a generated skill: *will HandQ
actually load this, and will it still work on someone else's machine?*

The load check calls HandQ's real ``_load_skill_file`` rather than
reimplementing its rules. That matters more than it looks: nine separate
authoring mistakes cause a skill to be discarded with no user-visible error
(unquoted colon in the description, a BOM, a space in the directory name, ...),
and a validator that reimplemented those checks would drift out of agreement
with the loader precisely when it was most needed. Here, ``loads: yes`` means
the loader said yes.

Everything past the load check is portability: a skill that loads on the
machine that generated it can still be dead on arrival after being copied or
shared. Those checks are original to this script because nothing in the product
performs them.

Usage:
    python validate_skill.py <skill-dir> [more-skill-dirs...]
    python validate_skill.py --bundled <skill-dir>    # product-shipped skill

By default a skill is validated as a *shareable artifact*: something the user
will copy into their Skill root or send to another HandQ user. Pass ``--bundled``
when checking a skill that ships with HandQ itself, where ``origin: bundled``
is correct rather than a defect.

Exit status: 0 when every skill loads and has no fatal portability defect,
1 otherwise. Warnings alone do not fail the run.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

# Tools that are always present, so listing them in allowed-tools is a no-op.
CORE_TOOLS = {
    "shell", "bash", "read", "write", "edit", "glob", "grep", "todo_write",
    "read_skill", "claim_tool", "release_tool", "wait_interval", "spawn_agent",
    "notebook_edit", "fan_out_agents",
}
# Registered on Windows HandQ only — a skill listing these is not portable to
# the Linux daemon, which does not register them at all.
WINDOWS_ONLY_TOOLS = {"desktop", "browser", "email", "teams"}
KNOWN_ON_DEMAND = {
    "ssh", "session", "web_search", "desktop", "browser", "email", "teams",
    "ask_human", "remote_handq", "schedule_task",
}

# A drive-letter or UNC path in a skill body is almost always this run's
# absolute path leaking into what should be a portable artifact.
ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[A-Za-z0-9._-]+\\)")
HOME_PATH_RE = re.compile(r"(?:/home/|/Users/|/root/)[A-Za-z0-9._-]+")
# Path portion following the placeholder, stopped at whitespace or the
# punctuation that normally closes an inline code span or sentence.
SKILL_DIR_REF_RE = re.compile(r"\$\{SKILL_DIR\}([^\s`'\"),\]]*)")


def find_handq_root() -> Path | None:
    """Locate the HandQ install/repo root that owns ``src/infrastructure/skills.py``.

    Searched in order: ``HANDQ_ROOT``, then every ancestor of this script, then
    every ancestor of the cwd. The ancestor walk matters because this skill can
    legitimately live in two places — bundled at ``<root>/Skill/skill-authoring``
    (root is three levels up) or copied into ``%USERPROFILE%\\HandQ\\Skill``
    (where there is no ``src`` above it, and the cwd is the only remaining clue).
    """
    marker = Path("src") / "infrastructure" / "skills.py"
    candidates: list[Path] = []
    env_root = os.environ.get("HANDQ_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    candidates.extend(Path.cwd().resolve().parents)
    candidates.append(Path.cwd().resolve())
    for cand in candidates:
        try:
            if (cand / marker).is_file():
                return cand
        except OSError:
            continue
    return None


class _WarnCapture(logging.Handler):
    """Collect the loader's own warnings so we can report its exact reason.

    The loader explains every rejection to the ``handq.skills`` logger and then
    returns None. Without this, a dropped skill would be reported as "did not
    load" with no cause — which is the same unhelpful experience the user gets
    in production, and the thing this script exists to fix.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


def raw_description_line(text: str) -> str | None:
    """The verbatim ``description:`` line from the frontmatter, if present."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    for line in block.splitlines():
        if line.strip().lower().startswith("description:"):
            return line
    return None


# Characters that make an unquoted YAML scalar fragile. A plain prose
# description (words, commas, em-dashes, parentheses) is safe unquoted, so
# warning about it would be noise on every well-formed skill in the product.
# These are the ones that either bite now or bite after the next small edit.
RISKY_UNQUOTED_CHARS = set(":#[]{}&*!|>%@`")


def check_skill(
    skill_dir: Path, load_skill_file, name_pattern, *, bundled_ok: bool = False
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one skill directory.

    ``bundled_ok`` marks the skill as product-shipped, where ``origin: bundled``
    is the intended value rather than a portability defect.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not skill_dir.is_dir():
        return [f"not a directory: {skill_dir}"], []

    dir_name = skill_dir.name
    if not name_pattern.match(dir_name):
        errors.append(
            f"directory name {dir_name!r} does not match ^[\\w\\-]{{1,64}}$ — "
            "no spaces, dots, or slashes; the skill will never be scanned"
        )

    # Exact-case filename: this loads on Windows and vanishes on Linux, which is
    # the worst failure mode for a skill that is going to be shared.
    # Checked by listing the directory rather than with is_file(), because NTFS
    # is case-insensitive — `(dir / "SKILL.md").is_file()` is True even when the
    # file on disk is named `Skill.md`, so the naive check silently never fires
    # on the platform where the mistake is made.
    on_disk = {p.name for p in skill_dir.iterdir() if p.is_file()}
    skill_md = skill_dir / "SKILL.md"
    if "SKILL.md" not in on_disk:
        variants = sorted(n for n in on_disk if n.lower() == "skill.md")
        if variants:
            errors.append(
                f"file is named {variants[0]!r}, must be exactly 'SKILL.md' — "
                "loads on Windows, invisible on Linux HandQ, so a shared skill "
                "breaks for half its recipients"
            )
        else:
            errors.append("no SKILL.md in the skill directory")
        return errors, warnings

    raw_bytes = skill_md.read_bytes()
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        errors.append(
            "SKILL.md starts with a UTF-8 BOM — the frontmatter must begin at "
            "byte zero; rewrite the file without a BOM"
        )
    text = skill_md.read_text(encoding="utf-8-sig")
    if not text.startswith("---"):
        first = text.split("\n", 1)[0][:40]
        errors.append(
            f"content before the opening '---' (found {first!r}) — frontmatter "
            "must be the very first thing in the file"
        )

    # ── The load check: the loader's own verdict, not a reimplementation ──
    capture = _WarnCapture()
    logger = logging.getLogger("handq.skills")
    logger.addHandler(capture)
    prev_level, prev_prop = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        entry = load_skill_file(skill_md, dir_name=dir_name)
    except Exception as exc:
        entry = None
        capture.messages.append(f"loader raised {type(exc).__name__}: {exc}")
    finally:
        logger.removeHandler(capture)
        logger.setLevel(prev_level)
        logger.propagate = prev_prop

    if entry is None:
        reason = capture.messages[-1] if capture.messages else "no reason logged"
        errors.append(f"DOES NOT LOAD — {reason}")
        return errors, warnings

    for problem in entry.problems:
        warnings.append(f"loader problem: {problem}")

    # ── Description integrity ──
    # A '#' in an unquoted scalar starts a YAML comment, so the description
    # loads truncated. It parses fine, which is why this needs its own check.
    raw_line = raw_description_line(text)
    if raw_line:
        raw_value = raw_line.split(":", 1)[1].strip()
        quoted = len(raw_value) >= 2 and raw_value[0] in "\"'" and raw_value[-1] == raw_value[0]
        if not quoted:
            if "#" in raw_value:
                errors.append(
                    "description contains '#' while unquoted — YAML treats it as "
                    f"a comment and the description is truncated to "
                    f"{entry.description!r}; wrap the description in double quotes"
                )
            elif len(entry.description) < len(raw_value.rstrip()):
                warnings.append(
                    f"description parsed shorter than written ({entry.description!r}) "
                    "— quote it to be safe"
                )
            elif RISKY_UNQUOTED_CHARS & set(raw_value):
                risky = "".join(sorted(RISKY_UNQUOTED_CHARS & set(raw_value)))
                warnings.append(
                    f"description is unquoted and contains {risky!r} — it parses "
                    "today, but YAML-special characters here are one edit away from "
                    "silently killing the whole skill; wrap it in double quotes"
                )

    # ── Portability ──
    if entry.origin == "bundled" and not bundled_ok:
        errors.append(
            "origin: bundled — the recipient will see this in the menu but not "
            "in their control panel, and cannot enable, edit, or delete it; omit "
            "the origin key entirely for a shareable skill "
            "(pass --bundled if this skill ships with HandQ)"
        )
    elif entry.origin == "auto":
        warnings.append(
            "origin: auto marks the skill as machine-owned; the memory system "
            "may overwrite its contents later. Omit origin for a user-owned skill."
        )

    if entry.standing:
        companions = [
            p for p in skill_dir.rglob("*") if p.is_file() and p.name != "SKILL.md"
        ]
        if companions:
            errors.append(
                "standing: true combined with companion files — a standing body is "
                "injected without ${SKILL_DIR} substitution, so every companion "
                "reference stays literal and unusable"
            )
        if "${SKILL_DIR}" in entry.body:
            errors.append(
                "standing: true and the body uses ${SKILL_DIR} — standing bodies "
                "are never substituted; the placeholder reaches the model as text"
            )

    # Dangling companion references: mechanically checkable, definitely broken.
    for match in SKILL_DIR_REF_RE.finditer(entry.body):
        rel = match.group(1).strip().lstrip("/\\").rstrip(".,;:")
        if not rel:
            continue
        if not (skill_dir / rel).exists():
            errors.append(f"${{SKILL_DIR}}/{rel} referenced in the body does not exist")

    for match in ABS_PATH_RE.finditer(entry.body):
        warnings.append(
            f"absolute path starting {match.group(0)} appears in the body — it will "
            "not exist on another machine; reference companion files via "
            "${SKILL_DIR} and turn machine-specific values into placeholders"
        )
        break
    if HOME_PATH_RE.search(entry.body):
        warnings.append(
            "a user home path appears in the body — replace it with a placeholder "
            "or ${SKILL_DIR}"
        )

    for tool in entry.allowed_tools:
        if tool in CORE_TOOLS:
            warnings.append(
                f"allowed-tools lists {tool!r}, which is always on — listing it "
                "does nothing; drop it"
            )
        elif tool in WINDOWS_ONLY_TOOLS:
            warnings.append(
                f"allowed-tools lists {tool!r}, which is not registered on Linux "
                "HandQ — note the Windows requirement in the body"
            )
        elif tool not in KNOWN_ON_DEMAND:
            warnings.append(
                f"allowed-tools lists unrecognised tool {tool!r} — activation does "
                "not validate names, so it will be reported as activated and then "
                "not exist. Verify the spelling."
            )

    if "python scripts/" in entry.body or "python reference/" in entry.body:
        errors.append(
            "a bare relative script path in the body — the shell cwd is the task "
            "workspace, not the skill directory; use ${SKILL_DIR}/scripts/..."
        )

    if not entry.enabled:
        warnings.append(
            "enabled: false — the skill loads but stays invisible until the user "
            "turns it on in the control panel. Intended only if you want that review step."
        )

    return errors, warnings


def main(argv: list[str]) -> int:
    args = argv[1:]
    bundled_ok = False
    if "--bundled" in args:
        bundled_ok = True
        args = [a for a in args if a != "--bundled"]
    targets = [Path(a).resolve() for a in args]
    if not targets:
        print(
            "usage: python validate_skill.py [--bundled] <skill-dir> [more-skill-dirs...]",
            file=sys.stderr,
        )
        return 2

    root = find_handq_root()
    if root is None:
        print(
            "ERROR: could not locate HandQ's src/ (looked at HANDQ_ROOT, this "
            "script's ancestors, and the cwd's ancestors).\n"
            "Run this from inside a HandQ checkout/install, or set HANDQ_ROOT.",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(root))
    try:
        from src.infrastructure.skills import _load_skill_file, _NAME_PATTERN
    except Exception as exc:
        print(f"ERROR: cannot import HandQ's skill loader from {root}: {exc}", file=sys.stderr)
        return 2

    failed = 0
    for target in targets:
        errors, warnings = check_skill(
            target, _load_skill_file, _NAME_PATTERN, bundled_ok=bundled_ok
        )
        status = "FAIL" if errors else ("ok (with warnings)" if warnings else "ok")
        print(f"\n=== {target.name} — {status} ===")
        print(f"    {target}")
        for e in errors:
            print(f"  [ERROR]   {e}")
        for w in warnings:
            print(f"  [warning] {w}")
        if not errors and not warnings:
            print("  loads, description intact, references resolve, portable.")
        if errors:
            failed += 1

    print()
    if failed:
        print(f"{failed} of {len(targets)} skill(s) will not load or are not portable.")
        print("Do not tell the user the skill is ready until this reports ok.")
        return 1
    print(f"all {len(targets)} skill(s) validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
