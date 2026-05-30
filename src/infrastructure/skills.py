"""
Skill — lightweight prompt assets discovered at boot.

A skill is a single SKILL.md file inside %USERPROFILE%\\HandQ\\Skill\\<name>\\
(Windows) or <install_dir>/Skill/<name>/ (POSIX) that ships professional
guidance to the receptionist / planner / agent on demand.

Layout follows Anthropic's Claude Code convention so files are portable:

    Skill/
      security-review/
        SKILL.md          ← frontmatter + body
      verify/
        SKILL.md

SKILL.md format:

    ---
    name: security-review
    description: 简短描述 — receptionist looks at this to decide activation
    ---
    （markdown body — full methodology / instructions; injected into planner
    & agent context only when this skill is in the active set）

The registry is a process-level singleton built once at boot
(:meth:`SkillRegistry.init`).  ``SkillRegistry.get()`` returns an empty
registry if init was skipped, mirroring :class:`LongTermMemory`'s
``_NullLongTermMemory`` fallback so test code never has to wrap calls
in ``try``/``except``.

Failure policy: every parse error is logged and the offending file is
skipped — a single broken skill MUST NOT block boot.

The registry is intentionally read-only after init — there is no hot
reload, no save API. Skills are installed by dropping a directory into
the Skill root; users restart the bridge to pick up changes. (A
``reload()`` shim is exposed for future IPC integration but not wired
yet.)
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

_logger = logging.getLogger("handq.skills")

_SKILLS_SUBDIR = "Skill"
_SKILL_FILE = "SKILL.md"
# Same constraint applied at parse time AND in the receptionist's @-prescan
# so a name that loads here can always be resolved by an explicit mention.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
# Front-matter splitter: '---\n<yaml>\n---\n<body>'. DOTALL so the yaml
# block can span multiple lines.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL
)


@dataclass
class SkillEntry:
    """A single loaded skill.

    Fields:
      name:        canonical identifier (frontmatter ``name`` if valid,
                   otherwise directory name; receptionist / planner refer
                   to skills by this).
      description: short description shown in the L0 menu.
      body:        full skill body (everything after the closing ``---``).
                   Empty when the file has no body or only whitespace.
      source_path: absolute path of the SKILL.md file (for logs / debug).
      problems:    human-readable warnings encountered at load time
                   (collisions, name/dir mismatch, empty body, …). The
                   skill is still usable; problems are advisory.
    """

    name: str
    description: str
    body: str
    source_path: str
    problems: List[str] = field(default_factory=list)


class SkillRegistry:
    """Process-level skill registry.

    Mirrors :class:`LongTermMemory` lifecycle:
      * :meth:`init` is called once at boot; it scans the Skill root.
      * :meth:`get` returns the singleton (or an empty fallback before
        init has run, so tests / standalone tools can call freely).
    """

    _instance: Optional["SkillRegistry"] = None

    def __init__(self, root: Path, entries: Dict[str, SkillEntry]) -> None:
        self._root = root
        self._entries: Dict[str, SkillEntry] = entries

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    def init(cls, root: Optional[Path] = None) -> "SkillRegistry":
        """Scan the Skill root and build the singleton.

        ``root`` lets tests point at an isolated directory; production
        boot calls ``init()`` with no argument and falls back to
        :func:`_default_skills_root`. Re-init replaces the singleton —
        useful for tests but not exposed via IPC.
        """
        scan_root = root if root is not None else _default_skills_root()
        try:
            scan_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Don't let a permission error kill boot — proceed with an
            # empty registry. The user can fix the directory later.
            _logger.exception(
                "SkillRegistry.init: could not create %s; using empty registry",
                scan_root,
            )
            cls._instance = cls(scan_root, {})
            return cls._instance

        entries = _scan_skill_root(scan_root)
        cls._instance = cls(scan_root, entries)
        _logger.info(
            "SkillRegistry initialised: root=%s, %d skill(s) loaded%s",
            scan_root,
            len(entries),
            f" (with warnings: {sum(1 for e in entries.values() if e.problems)})"
            if any(e.problems for e in entries.values())
            else "",
        )
        return cls._instance

    @classmethod
    def get(cls) -> "SkillRegistry":
        if cls._instance is None:
            _logger.debug("SkillRegistry.get() before init; returning empty instance")
            return cls(_default_skills_root(), {})
        return cls._instance

    def reload(self) -> None:
        """Re-scan the Skill root.

        Not wired to any IPC handler yet — exposed so a future
        ``/skill-reload`` command can refresh without restarting the
        bridge.
        """
        self._entries = _scan_skill_root(self._root)
        _logger.info(
            "SkillRegistry reloaded: %d skill(s) under %s",
            len(self._entries),
            self._root,
        )

    # ── Read API ────────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        return name in self._entries

    def names(self) -> List[str]:
        return sorted(self._entries.keys())

    def get_skill(self, name: str) -> Optional[SkillEntry]:
        return self._entries.get(name)

    def list_summary(self) -> List[Dict[str, str]]:
        """L0 menu data — one dict per skill with name + description."""
        return [
            {"name": e.name, "description": e.description}
            for e in sorted(self._entries.values(), key=lambda x: x.name)
        ]

    # ── Prompt block builders ───────────────────────────────────────────────

    def render_active_block(self, names: Iterable[str]) -> str:
        """Render full bodies of *names* as one [Active Skills] block.

        Skills not in the registry are silently skipped (the FlowController
        already filters; this is defence-in-depth so a stale name from an
        older Plan can never crash). Skills with empty bodies are also
        skipped — an empty body is a no-op and would only confuse the
        downstream LLM by suggesting there is content to follow.
        """
        wanted = [n for n in names if n in self._entries]
        if not wanted:
            return ""
        lines: List[str] = ["[Active Skills]"]
        any_rendered = False
        for n in sorted(set(wanted)):
            entry = self._entries[n]
            if not entry.body.strip():
                continue
            lines.append(f'<skill name="{entry.name}" description="{entry.description}">')
            lines.append(entry.body.rstrip())
            lines.append("</skill>")
            any_rendered = True
        if not any_rendered:
            return ""
        return "\n".join(lines)

    def render_menu_block(self, exclude: Iterable[str] = ()) -> str:
        """Render the L0 menu of skills NOT in *exclude*.

        Receptionist sees this menu to decide whether to add a skill to
        ``activated_skills``. Planner sees it to decide whether to add a
        skill to ``skills_to_activate``.
        """
        excluded = set(exclude)
        visible = [
            e for e in sorted(self._entries.values(), key=lambda x: x.name)
            if e.name not in excluded
        ]
        if not visible:
            return ""
        lines = ["[Available Skills]"]
        for e in visible:
            lines.append(f"  - {e.name}: {e.description}")
        return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────────────


def _user_handq_root() -> Path:
    """%USERPROFILE%\\HandQ on Windows, ~/HandQ otherwise.

    Mirrors ``bridge_main._user_handq_root()`` and
    ``gep_template._user_handq_root()`` — single source of truth for the
    user-owned data root per ARCHITECTURE.md §1.5.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


def _install_dir() -> Path:
    """Directory next to the bridge entry point.

    Same algorithm as ``bridge_main._INSTALL_DIR`` — falls back to the
    repo root in dev mode (this file's grandparent's grandparent).
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return Path(__file__).parent.parent.parent.resolve()


def _default_skills_root() -> Path:
    """Where to scan for skills when ``init()`` was called without a root.

    Resolution order:
      1. ``HANDQ_SKILLS_DIR`` env var (tests / portable mode)
      2. Per-platform default — Windows uses %USERPROFILE%\\HandQ\\Skill\\,
         POSIX uses <install_dir>/Skill/. Mirrors gep_template's policy.
    """
    override = os.environ.get("HANDQ_SKILLS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return _user_handq_root() / _SKILLS_SUBDIR
    return _install_dir() / _SKILLS_SUBDIR


def _scan_skill_root(root: Path) -> Dict[str, SkillEntry]:
    """Walk *root* and return a {name: SkillEntry} map.

    Each immediate subdirectory is a skill candidate; we read its
    ``SKILL.md`` and parse the frontmatter. Failures are logged and
    skipped so one bad skill cannot block the rest.

    Name resolution rule: when frontmatter ``name`` differs from the
    directory name, the directory name wins — that's the value the
    receptionist's @-mention regex will match. The discrepancy is
    recorded as a problem on the entry.
    """
    entries: Dict[str, SkillEntry] = {}
    if not root.is_dir():
        return entries

    try:
        candidate_dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name,
        )
    except OSError:
        _logger.exception("SkillRegistry: could not list %s", root)
        return entries

    for skill_dir in candidate_dirs:
        skill_md = skill_dir / _SKILL_FILE
        if not skill_md.is_file():
            _logger.warning(
                "SkillRegistry: skipping %s — no %s",
                skill_dir.name,
                _SKILL_FILE,
            )
            continue
        try:
            entry = _load_skill_file(skill_md, dir_name=skill_dir.name)
        except Exception:
            _logger.exception(
                "SkillRegistry: failed to load %s — skipping",
                skill_md,
            )
            continue
        if entry is None:
            continue
        if entry.name in entries:
            # Sorted dir scan means the first one wins; tag the loser.
            entry.problems.append(
                f"name '{entry.name}' collides with already-loaded skill at "
                f"{entries[entry.name].source_path}; this entry is ignored"
            )
            _logger.warning(
                "SkillRegistry: collision on name '%s' at %s — keeping first, ignoring this one",
                entry.name,
                entry.source_path,
            )
            continue
        entries[entry.name] = entry

    return entries


def _load_skill_file(skill_md: Path, *, dir_name: str) -> Optional[SkillEntry]:
    """Parse one SKILL.md. Returns None when the file is unusable."""
    text = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        _logger.warning(
            "SkillRegistry: %s — no frontmatter (`---` block missing); skipping",
            skill_md,
        )
        return None
    fm_raw, body = match.group(1), match.group(2)
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as exc:
        _logger.warning(
            "SkillRegistry: %s — frontmatter YAML parse failed (%s); skipping",
            skill_md,
            exc,
        )
        return None
    if not isinstance(fm, dict):
        _logger.warning(
            "SkillRegistry: %s — frontmatter is not a mapping; skipping",
            skill_md,
        )
        return None

    fm_name = str(fm.get("name", "") or "").strip()
    description = str(fm.get("description", "") or "").strip()

    problems: List[str] = []

    # Resolve name: directory name is canonical, frontmatter is advisory.
    # Reject altogether if neither yields a valid identifier.
    canonical_name = dir_name
    if not _NAME_PATTERN.match(canonical_name):
        _logger.warning(
            "SkillRegistry: %s — directory name '%s' is not a valid skill identifier "
            "(must match %s); skipping",
            skill_md,
            canonical_name,
            _NAME_PATTERN.pattern,
        )
        return None
    if fm_name and fm_name != canonical_name:
        problems.append(
            f"frontmatter name '{fm_name}' does not match directory '{canonical_name}'; "
            "using directory name"
        )

    if not description:
        _logger.warning(
            "SkillRegistry: %s — frontmatter is missing 'description'; skipping",
            skill_md,
        )
        return None

    body_clean = body.strip()
    if not body_clean:
        problems.append("empty body — skill activation will be a no-op")

    entry = SkillEntry(
        name=canonical_name,
        description=description,
        body=body,
        source_path=str(skill_md.resolve()),
        problems=problems,
    )
    return entry
