"""
Skill — lightweight prompt assets discovered at boot.

A skill is a single SKILL.md file inside %USERPROFILE%\\HandQ\\Skill\\<name>\\
(Windows) or <install_dir>/Skill/<name>/ (POSIX) that ships professional
guidance to the orchestrator / agent on demand.

Layout follows Anthropic's Claude Code convention so files are portable:

    Skill/
      security-review/
        SKILL.md          ← frontmatter + body
      verify/
        SKILL.md

SKILL.md format:

    ---
    name: security-review
    description: 简短描述 — shown in the [Available Skills] menu every role sees
    enabled: true          ← optional; omit or `true` = active, `false` = off
    standing: false        ← optional; `true` = body transparently injected into
                             all roles as plain prompt text (persona / methodology);
                             omit or anything unrecognised = not standing
    ---
    （markdown body — full methodology / instructions. Non-standing skills are
    read on demand by the agent via the `read_skill` tool; standing skills are
    injected transparently as prompt text — the agent cannot tell they came
    from a "skill".）

The optional ``enabled`` frontmatter flag is the single knob behind the
Skill control panel: disabling a skill keeps its file in place (so it is
still editable/re-enablable) but hides it from every agent-facing surface
(menu, body injection). A missing flag means enabled, so every pre-existing
skill keeps working untouched.

The optional ``standing`` flag marks a skill as "always in effect": its body
is rendered transparently into every role's context (no task trigger, no
@mention, no attribution markers). Use it for persona / speaking-habit /
general-methodology skills where enable ⇒ immediately live. Non-standing
skills only appear in the menu; the agent pulls their full body on demand
via ``read_skill``. ``standing`` fails closed — a missing or unrecognised
value means not-standing.

INVARIANT: standing implies enabled. A standing skill that is somehow marked
``enabled: false`` (e.g. hand-edited) gets force-enabled at load time. The
panel enforces this bidirectionally: disabling clears standing, enabling
standing forces enable.

The registry is a process-level singleton built once at boot
(:meth:`SkillRegistry.init`).  ``SkillRegistry.get()`` returns an empty
registry if init was skipped, mirroring :class:`LongTermMemory`'s
``_NullLongTermMemory`` fallback so test code never has to wrap calls
in ``try``/``except``.

Failure policy: every parse error is logged and the offending file is
skipped — a single broken skill MUST NOT block boot.

The registry supports live mutation via the Skill control panel: enable
/disable (a frontmatter flag), create, edit and delete all write the
SKILL.md on disk and refresh the in-memory entry. ``reload()`` re-scans
the whole root and is invoked after out-of-band changes (e.g. triage
direct-writes an auto-generated skill). Agent-facing discovery reads
(``names`` / ``get_skill`` / ``render_menu_block`` / ``render_standing_block``)
only ever expose *enabled* skills; ``has`` is a neutral existence probe over
the full set (enabled + disabled), used for diagnostics and tests. The panel
uses ``list_all`` / ``get_any`` to see everything including disabled ones.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

_logger = logging.getLogger("handq.skills")

_SKILLS_SUBDIR = "Skill"
_SKILL_FILE = "SKILL.md"
# Applied at parse time to the canonical (directory) name — that's the value
# the menu lists and that ``read_skill`` matches on.
_NAME_PATTERN = re.compile(r"^[\w\-]{1,64}$", re.UNICODE)
# Front-matter splitter: '---\n<yaml>\n---\n<body>'. DOTALL so the yaml
# block can span multiple lines.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL
)

# ASCII-only: non-ASCII scripts (e.g. CJK) are stripped so the slug falls back
# cleanly rather than yielding a directory name that can't be @-mentioned.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Origin marks who owns a skill's *content*, gating the auto-miner's write path.
# ``auto`` = minted by triage from a recurring task; ``bundled`` = shipped
# with HandQ itself and seeded into the user's Skill root by
# seed_bundled_skills() — invisible to the panel and immutable via the
# skill_* IPC surface (see SkillRegistry.list_all / _reject_if_bundled).
# Anything else (missing key, ``user``, hand-created / imported / panel-
# edited) = user-owned and OFF LIMITS to the auto-miner. Origin fails *safe
# to user*: an ambiguous value must never green-light a clobber (see
# ``_coerce_origin``) and must never silently hide a skill the user created.
SKILL_ORIGIN_AUTO = "auto"
SKILL_ORIGIN_USER = "user"
SKILL_ORIGIN_BUNDLED = "bundled"


def slugify_skill_name(text: str, *, fallback: str = "skill") -> str:
    """Turn a free-text skill title into a ``_NAME_PATTERN``-valid slug.

    Lowercase → collapse every non-ASCII-alphanumeric run to a single ``-`` →
    trim leading/trailing ``-`` → clip to 64 chars (the pattern's ceiling).
    Returns ``fallback`` when nothing usable survives (e.g. a CJK-only title),
    so callers always get a directory-safe identifier.
    """
    slug = _SLUG_STRIP_RE.sub("-", (text or "").strip().lower()).strip("-")
    slug = slug[:64].rstrip("-")
    return slug if slug and _NAME_PATTERN.match(slug) else fallback


@dataclass
class SkillEntry:
    """A single loaded skill.

    Fields:
      name:        canonical identifier (frontmatter ``name`` if valid,
                   otherwise directory name; the orchestrator and agent refer
                   to skills by this).
      description: short description shown in the L0 menu.
      body:        full skill body (everything after the closing ``---``).
                   Empty when the file has no body or only whitespace.
      source_path: absolute path of the SKILL.md file (for logs / debug).
      problems:    human-readable warnings encountered at load time
                   (collisions, name/dir mismatch, empty body, …). The
                   skill is still usable; problems are advisory.
      enabled:     whether the skill is active. Disabled skills stay on
                   disk (editable / re-enablable) but are hidden from every
                   agent-facing surface. A missing ``enabled`` frontmatter
                   key means enabled, so legacy skills keep working.
      standing:    whether the skill is "always in effect". A standing skill's
                   body is injected unconditionally into every role
                   (orchestrator / agent) — used for persona /
                   speaking-habit / general-methodology skills that should
                   apply without a task trigger. Fail-closed: a missing or
                   unrecognised frontmatter value means False.
      origin:      who owns the skill's content — ``auto`` (minted by triage
                   from a recurring task) or ``user`` (hand-created, imported,
                   or panel-edited). Gates the auto-miner: it may refresh an
                   ``auto`` skill in place but must never overwrite a ``user``
                   one. Fail-safe: any non-``auto`` value resolves to ``user``,
                   so an ambiguous marker protects rather than exposes content.
      allowed_tools:   on-demand tool names this skill's recipe uses. When the
                   agent pulls the body via ``read_skill``, these are activated
                   (loaded + available next turn) in one step — the skill is a
                   recipe + its tool grant, mirroring Claude Code's
                   ``allowed-tools`` frontmatter. Empty for pure-guidance skills.
                   Every provider today is 1:1 with a tool, so activating a
                   tool ALSO runs its provider's session-once setup via the
                   ``on_tools_changed`` bus — no separate ``activates_providers``
                   field is needed.
    """

    name: str
    description: str
    body: str
    source_path: str
    problems: List[str] = field(default_factory=list)
    enabled: bool = True
    standing: bool = False
    origin: str = SKILL_ORIGIN_USER
    allowed_tools: List[str] = field(default_factory=list)


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

        The panel's CRUD paths update entries in place (``_refresh_entry``) and
        don't need this; it stays for the "something changed the files out of
        band" case (triage direct-writes an auto-generated skill, manual edits,
        a future ``/skill-reload``). Triage runs in-process and calls the write
        API directly, so its new skills are visible without an explicit reload.
        """
        self._entries = _scan_skill_root(self._root)
        _logger.info(
            "SkillRegistry reloaded: %d skill(s) under %s",
            len(self._entries),
            self._root,
        )

    # ── Read API ────────────────────────────────────────────────────────────
    #
    # Two tiers of visibility for a disabled skill:
    #   * Discovery surfaces (``names`` / ``render_menu_block`` /
    #     ``render_standing_block`` / ``get_skill``) list ONLY enabled skills —
    #     no agent-facing surface (menu, standing bodies, read_skill) ever
    #     exposes a disabled one.
    #   * Existence probe (``has``) sees the FULL set (enabled + disabled). It
    #     injects nothing and gates nothing — it's a neutral "did this name
    #     load" check for diagnostics and tests.
    # Panel-facing reads (``list_all`` / ``get_any``) also see the full set.

    def has(self, name: str) -> bool:
        # Existence, not enabled-state — see the two-tier note above.
        return name in self._entries

    def names(self) -> List[str]:
        return sorted(n for n, e in self._entries.items() if e.enabled)

    def debug_roster(self) -> List[str]:
        """One-line-per-skill summary for boot diagnostics — the FULL set
        (bundled + user, enabled + disabled), unlike ``list_all()`` which
        hides bundled entries (that's a panel-visibility rule, not a
        diagnostic one). Exists because the 2026-07-24 desktop-workflow
        outage had no log line anywhere that answered "what skills did this
        boot actually find" — every downstream symptom (empty [Available
        Skills] menu, read_skill never called, reverse-push silently
        skipped) had to be reverse-engineered from session traces instead of
        read directly off one boot log line.
        """
        return [
            f"{e.name} (origin={e.origin}, enabled={e.enabled}, standing={e.standing})"
            for e in sorted(self._entries.values(), key=lambda x: x.name)
        ]

    def get_skill(self, name: str) -> Optional[SkillEntry]:
        entry = self._entries.get(name)
        if entry is None or not entry.enabled:
            return None
        return entry

    # ── Prompt block builders ───────────────────────────────────────────────

    def render_menu_block(self, exclude: Iterable[str] = ()) -> str:
        """Render the [Available Skills] menu of enabled skills NOT in *exclude*.

        Every role sees this awareness menu (name + description only). It is
        reference material, not an activation surface: the agent pulls a full
        body on demand via ``read_skill``; the orchestrator only reasons
        about what exists. ``exclude`` lets a caller drop skills already shown
        in full (e.g. standing bodies) so they aren't listed twice.
        """
        excluded = set(exclude)
        visible = [
            e for e in sorted(self._entries.values(), key=lambda x: x.name)
            if e.name not in excluded and e.enabled
        ]
        if not visible:
            return ""
        lines = ["[Available Skills]"]
        for e in visible:
            lines.append(f"  - {e.name}: {e.description}")
        return "\n".join(lines)

    def render_standing_block(self) -> str:
        """Render full bodies of enabled+standing skills as transparent prompt text.

        Standing skills are injected as plain instructions — the agent sees no
        markers or attribution. They become indistinguishable from the base
        system prompt, which is the point: standing skills define persona /
        methodology / style that should be unconditionally applied without the
        agent reasoning about "skills". Disabled skills are excluded even if
        flagged standing. Empty bodies are skipped. Returns ``""`` when nothing
        qualifies.
        """
        standing = [
            e for e in sorted(self._entries.values(), key=lambda x: x.name)
            if e.enabled and e.standing
        ]
        if not standing:
            return ""
        parts: List[str] = []
        for e in standing:
            if not e.body.strip():
                continue
            parts.append(e.body.rstrip())
        if not parts:
            return ""
        return "\n\n".join(parts)

    def standing_names(self) -> List[str]:
        """Names of enabled+standing skills (sorted). Test/introspection aid."""
        return sorted(
            e.name for e in self._entries.values() if e.enabled and e.standing
        )

    # ── Mutation / panel API (Skill control panel) ───────────────────────────
    #
    # These see the FULL set (enabled + disabled) and write SKILL.md files
    # under the Skill root, refreshing the affected in-memory entry. They are
    # synchronous (file I/O); the IPC layer wraps them in asyncio.to_thread.
    # Each returns a JSON-serializable {"ok": bool, ...} the bridge forwards
    # verbatim. Name mutations are constrained to _NAME_PATTERN, so a crafted
    # name can never escape the Skill root.

    def list_all(self) -> List[Dict[str, object]]:
        """Full inventory for the panel — enabled AND disabled skills.

        Excludes ``origin: bundled`` entries: product-shipped skills are not
        part of the user's own inventory, so they never appear in the panel
        and can't be discovered there to enable/disable/edit/delete. The
        Agent-facing side (render_menu_block / render_standing_block) is a
        SEPARATE code path and still surfaces bundled skills normally — this
        method only gates what the human-facing panel can see.
        """
        return [
            {
                "name": e.name,
                "description": e.description,
                "enabled": e.enabled,
                "standing": e.standing,
                "origin": e.origin,
                "allowed_tools": list(e.allowed_tools),
                "body": e.body.strip(),
                "source_path": e.source_path,
                "problems": list(e.problems),
            }
            for e in sorted(self._entries.values(), key=lambda x: x.name)
            if e.origin != SKILL_ORIGIN_BUNDLED
        ]

    def get_any(self, name: str) -> Optional[SkillEntry]:
        """Fetch an entry regardless of enabled state (panel / write use)."""
        return self._entries.get(name)

    @staticmethod
    def _bundled_immutable_result(name: str) -> Dict[str, object]:
        return {"ok": False, "reason": "bundled_immutable", "name": name}

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, object]:
        entry = self._entries.get(name)
        if entry is None:
            return {"ok": False, "reason": "not_found", "name": name}
        if entry.origin == SKILL_ORIGIN_BUNDLED:
            return self._bundled_immutable_result(name)
        path = Path(entry.source_path)
        try:
            text = path.read_text(encoding="utf-8")
            text = _set_frontmatter_enabled(text, enabled)
            # Invariant: standing implies enabled. Disabling clears standing.
            if not enabled and entry.standing:
                text = _set_frontmatter_standing(text, False)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            _logger.exception("SkillRegistry.set_enabled write failed name=%s", name)
            return {"ok": False, "reason": "write_failed", "error": str(exc)}
        entry.enabled = enabled
        if not enabled and entry.standing:
            entry.standing = False
        return {"ok": True, "name": name, "enabled": enabled, "standing": entry.standing}

    def set_standing(self, name: str, standing: bool) -> Dict[str, object]:
        entry = self._entries.get(name)
        if entry is None:
            return {"ok": False, "reason": "not_found", "name": name}
        if entry.origin == SKILL_ORIGIN_BUNDLED:
            return self._bundled_immutable_result(name)
        path = Path(entry.source_path)
        try:
            text = path.read_text(encoding="utf-8")
            text = _set_frontmatter_standing(text, standing)
            # Invariant: standing implies enabled. Enabling standing forces enable.
            if standing and not entry.enabled:
                text = _set_frontmatter_enabled(text, True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            _logger.exception("SkillRegistry.set_standing write failed name=%s", name)
            return {"ok": False, "reason": "write_failed", "error": str(exc)}
        entry.standing = standing
        if standing and not entry.enabled:
            entry.enabled = True
        return {"ok": True, "name": name, "standing": standing, "enabled": entry.enabled}

    def create_skill(self, name: str, description: str, body: str,
                      *, enabled: bool = True, standing: bool = False,
                      origin: str = SKILL_ORIGIN_USER,
                      allowed_tools: Optional[Iterable[str]] = None,
                      ) -> Dict[str, object]:
        name = (name or "").strip()
        description = (description or "").strip()
        if not _NAME_PATTERN.match(name):
            return {"ok": False, "reason": "invalid_name", "name": name}
        if not description:
            return {"ok": False, "reason": "missing_description"}
        if name in self._entries or (self._root / name).exists():
            return {"ok": False, "reason": "exists", "name": name}
        skill_md = self._root / name / _SKILL_FILE
        allowed_list = _coerce_str_list(list(allowed_tools) if allowed_tools else None)
        try:
            skill_md.parent.mkdir(parents=True, exist_ok=True)
            skill_md.write_text(
                _render_skill_md(name, description, body or "", enabled=enabled,
                                 standing=standing, origin=origin,
                                 allowed_tools=allowed_list),
                encoding="utf-8",
            )
        except OSError as exc:
            _logger.exception("SkillRegistry.create_skill write failed name=%s", name)
            return {"ok": False, "reason": "write_failed", "error": str(exc)}
        self._refresh_entry(skill_md, name)
        return {"ok": True, "name": name}

    def update_skill(
        self, name: str, *, new_name: Optional[str] = None,
        description: Optional[str] = None, body: Optional[str] = None,
        standing: Optional[bool] = None, origin: Optional[str] = None,
        allowed_tools: Optional[Iterable[str]] = None,
    ) -> Dict[str, object]:
        entry = self._entries.get(name)
        if entry is None:
            return {"ok": False, "reason": "not_found", "name": name}
        if entry.origin == SKILL_ORIGIN_BUNDLED:
            return self._bundled_immutable_result(name)
        final_name = name
        if new_name is not None:
            new_name = new_name.strip()
            if new_name and new_name != name:
                if not _NAME_PATTERN.match(new_name):
                    return {"ok": False, "reason": "invalid_name", "name": new_name}
                if new_name in self._entries or (self._root / new_name).exists():
                    return {"ok": False, "reason": "exists", "name": new_name}
                final_name = new_name
        final_desc = entry.description if description is None else description.strip()
        if not final_desc:
            return {"ok": False, "reason": "missing_description"}
        final_body = entry.body if body is None else body
        final_standing = entry.standing if standing is None else standing
        final_origin = entry.origin if origin is None else origin
        # allowed_tools: None means "preserve"; an explicit list (even empty)
        # replaces. This lets callers add / change / clear the grant.
        final_allowed = (
            entry.allowed_tools if allowed_tools is None
            else _coerce_str_list(list(allowed_tools))
        )
        old_dir = Path(entry.source_path).parent
        new_md = self._root / final_name / _SKILL_FILE
        try:
            new_md.parent.mkdir(parents=True, exist_ok=True)
            new_md.write_text(
                _render_skill_md(final_name, final_desc, final_body,
                                 enabled=entry.enabled, standing=final_standing,
                                 origin=final_origin,
                                 allowed_tools=final_allowed),
                encoding="utf-8",
            )
            if final_name != name and old_dir.exists() and old_dir != new_md.parent:
                shutil.rmtree(old_dir, ignore_errors=True)
        except OSError as exc:
            _logger.exception("SkillRegistry.update_skill write failed name=%s", name)
            return {"ok": False, "reason": "write_failed", "error": str(exc)}
        if final_name != name:
            self._entries.pop(name, None)
        self._refresh_entry(new_md, final_name)
        return {"ok": True, "name": final_name}

    def delete_skill(self, name: str) -> Dict[str, object]:
        entry = self._entries.get(name)
        if entry is None:
            return {"ok": False, "reason": "not_found", "name": name}
        if entry.origin == SKILL_ORIGIN_BUNDLED:
            return self._bundled_immutable_result(name)
        try:
            shutil.rmtree(Path(entry.source_path).parent, ignore_errors=True)
        except OSError as exc:
            _logger.exception("SkillRegistry.delete_skill failed name=%s", name)
            return {"ok": False, "reason": "delete_failed", "error": str(exc)}
        self._entries.pop(name, None)
        return {"ok": True, "name": name}

    def import_skill(self, src_path: str) -> Dict[str, object]:
        """Import a user-supplied SKILL.md from an arbitrary path.

        Parses the file's frontmatter, derives a directory-safe name (frontmatter
        ``name`` preferred, then the source's parent directory, slugified either
        way), and writes it under the Skill root via ``create_skill`` /
        ``update_skill``. A user-picked file is an explicit install, so it lands
        ``enabled=True`` and ``origin=user`` regardless of any flags in the
        source — an imported skill is user-owned and off limits to the
        auto-miner. Re-importing an existing name overwrites description + body
        in place (and claims ownership).
        """
        path = Path(src_path)
        if not path.is_file():
            return {"ok": False, "reason": "not_found", "path": src_path}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.exception("SkillRegistry.import_skill read failed path=%s", src_path)
            return {"ok": False, "reason": "read_failed", "error": str(exc)}
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {"ok": False, "reason": "no_frontmatter"}
        fm_raw, body = match.group(1), match.group(2)
        try:
            fm = yaml.safe_load(fm_raw) or {}
        except yaml.YAMLError as exc:
            return {"ok": False, "reason": "bad_frontmatter", "error": str(exc)}
        if not isinstance(fm, dict):
            return {"ok": False, "reason": "bad_frontmatter"}
        description = str(fm.get("description", "") or "").strip()
        if not description:
            return {"ok": False, "reason": "missing_description"}
        standing = _coerce_standing(fm.get("standing"))
        # Round-trip allowed-tools from source so an imported recipe skill
        # keeps its tool grant. Silently dropping was the audit gap.
        allowed_tools = _coerce_str_list(
            fm.get("allowed-tools", fm.get("allowed_tools"))
        )
        raw_name = str(fm.get("name", "") or "").strip() or path.parent.name
        name = slugify_skill_name(raw_name)
        if name in self._entries:
            return self.update_skill(name, description=description, body=body,
                                     standing=standing, origin=SKILL_ORIGIN_USER,
                                     allowed_tools=allowed_tools)
        return self.create_skill(name, description, body, enabled=True,
                                 standing=standing, origin=SKILL_ORIGIN_USER,
                                 allowed_tools=allowed_tools)

    def _refresh_entry(self, skill_md: Path, dir_name: str) -> None:
        """Re-parse one SKILL.md and replace its in-memory entry.

        Keeps the registry consistent with what a fresh boot scan would load
        (parsed ``enabled`` / description, recorded problems) without paying
        for a full rescan.
        """
        try:
            entry = _load_skill_file(skill_md, dir_name=dir_name)
        except Exception:
            _logger.exception("SkillRegistry._refresh_entry: reload %s failed", skill_md)
            return
        if entry is not None:
            self._entries[entry.name] = entry


# ── Helpers ────────────────────────────────────────────────────────────────


def _render_skill_md(name: str, description: str, body: str, *,
                     enabled: bool = True, standing: bool = False,
                     origin: str = SKILL_ORIGIN_USER,
                     allowed_tools: Optional[List[str]] = None) -> str:
    """Serialize a canonical SKILL.md.

    Only writes ``enabled: false`` when the skill is disabled — an enabled
    skill keeps minimal frontmatter (missing key == enabled), so hand-authored
    files stay clean. Similarly only writes ``standing: true`` when the skill
    is standing (missing key == not standing) and ``origin: auto``/``origin:
    bundled`` only for triage-minted / product-shipped skills (missing key ==
    user-owned, the protective default). ``allowed-tools`` is written only
    when non-empty (missing == none). ``description`` is flattened to a
    single line so it can't break the YAML block.
    """
    desc_line = " ".join(str(description).split())
    lines = ["---", f"name: {name}", f"description: {desc_line}"]
    if not enabled:
        lines.append("enabled: false")
    if standing:
        lines.append("standing: true")
    if origin == SKILL_ORIGIN_AUTO:
        lines.append("origin: auto")
    elif origin == SKILL_ORIGIN_BUNDLED:
        lines.append("origin: bundled")
    if allowed_tools:
        lines.append("allowed-tools: [" + ", ".join(allowed_tools) + "]")
    lines.append("---")
    lines.append("")
    lines.append(str(body).strip())
    return "\n".join(lines).rstrip() + "\n"


def _set_frontmatter_standing(text: str, standing: bool) -> str:
    """Return *text* with its frontmatter ``standing`` flag set in place.

    Standing writes ``standing: true``; non-standing drops the key (missing ==
    not standing, keeps files minimal). All other frontmatter keys and the body
    are preserved verbatim. If the file lacks frontmatter we prepend a minimal
    block rather than lose the flag.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        header = "standing: true\n" if standing else ""
        return f"---\n{header}---\n\n{text.lstrip()}"
    fm_raw, body = match.group(1), match.group(2)
    kept = [
        ln for ln in fm_raw.splitlines()
        if ln.strip().lower().split(":", 1)[0].strip() != "standing"
    ]
    if standing:
        kept.append("standing: true")
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def _set_frontmatter_enabled(text: str, enabled: bool) -> str:
    """Return *text* with its frontmatter ``enabled`` flag set in place.

    Enabling drops the key (missing == enabled, keeps files minimal); disabling
    writes ``enabled: false``. All other frontmatter keys and the body are
    preserved verbatim. If the file lacks frontmatter we prepend a minimal
    block rather than lose the flag.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        header = "" if enabled else "enabled: false\n"
        return f"---\n{header}---\n\n{text.lstrip()}"
    fm_raw, body = match.group(1), match.group(2)
    kept = [
        ln for ln in fm_raw.splitlines()
        if ln.strip().lower().split(":", 1)[0].strip() != "enabled"
    ]
    if not enabled:
        kept.append("enabled: false")
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def _user_handq_root() -> Path:
    """%USERPROFILE%\\HandQ on Windows, ~/HandQ otherwise.

    Mirrors ``bridge_main._user_handq_root()`` — single source of truth for
    the user-owned data root per ARCHITECTURE.md §1.5.
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
         POSIX uses <install_dir>/Skill/.
    """
    override = os.environ.get("HANDQ_SKILLS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return _user_handq_root() / _SKILLS_SUBDIR
    return _install_dir() / _SKILLS_SUBDIR


def _bundled_skills_dir() -> Optional[Path]:
    """Repo/installed ``Skill/`` directory shipping product recipe skills.

    Dev mode: ``<repo>/Skill``. Frozen build: ``<install_dir>/Skill``. Returns
    None if it does not exist (a build without bundled recipes is fine).
    """
    d = _install_dir() / _SKILLS_SUBDIR
    return d if d.is_dir() else None


def _rmtree_with_retry(path: Path, *, attempts: int = 3, delay_s: float = 0.1) -> None:
    """``shutil.rmtree`` that also clears the Windows read-only bit on failure.

    Root cause this exists for: ``shutil.copytree`` (used by
    ``seed_bundled_skills`` to seed/refresh a target) preserves the SOURCE
    directory's permission bits via ``copystat`` — and the repo's ``Skill/``
    directories can themselves be read-only (e.g. certain git checkouts /
    editors mark checked-out dirs `0o555`). The copy inherits that: its files
    are writable, but the DIRECTORY ENTRY itself is not, so a later
    ``rmdir`` on it raises ``PermissionError: Access is denied`` even though
    every file inside deletes cleanly (confirmed: `os.remove` on the file
    succeeds, only `os.rmdir` on the now-empty dir fails). ``onexc`` chmod's
    the offending path to writable and retries the SAME operation — this is
    the standard fix documented for `shutil.rmtree` on Windows. A short
    sleep+retry wrapper around the whole call remains as a second-line
    defense for the separate (rarer) case of a transient AV/indexer handle;
    re-raises the last error if it still hasn't cleared after all attempts.
    """
    def _on_rm_error(func, target_path, exc_info):
        try:
            os.chmod(target_path, stat.S_IWRITE)
            func(target_path)
        except OSError:
            raise exc_info[1]

    last_exc: Optional[OSError] = None
    for i in range(attempts):
        try:
            shutil.rmtree(path, onexc=_on_rm_error)
            return
        except OSError as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(delay_s)
    if last_exc is not None:
        raise last_exc


def _dir_contents_equal(a: Path, b: Path) -> bool:
    """True iff *a* and *b* contain the same relative file paths with
    byte-identical content (recursive). Used to skip a bundled-skill refresh
    when the repo source hasn't actually changed since it was last seeded —
    metadata (mtime, permission bits) is deliberately ignored, only content
    and the set of files matter. Any read error is treated as "not equal"
    (fail toward refreshing, never toward silently skipping a real change).
    """
    try:
        a_files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
        b_files = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    except OSError:
        return False
    if a_files != b_files:
        return False
    try:
        return all(
            (a / rel).read_bytes() == (b / rel).read_bytes()
            for rel in a_files
        )
    except OSError:
        return False


def seed_bundled_skills(dest_root: Optional[Path] = None) -> int:
    """Copy shipped recipe skills into the user skill root, refreshing any
    that are still ``origin: bundled`` AND whose content actually differs
    from the shipped source (see ``_dir_contents_equal`` — an unconditional
    rewrite on every boot, even when nothing shipped, would burn disk I/O
    and the Windows-lock retry path for no reason on the overwhelmingly
    common case where the repo content hasn't changed since last boot).

    Product-authored recipe skills (monitor-long-running, remote-handq-workflow,
    …) live in the repo/installed ``Skill/`` dir. The registry only scans user
    data (``%USERPROFILE%\\HandQ\\Skill``), so on boot we copy any bundled skill
    the user does not already have, AND overwrite any existing target that is
    still ``origin: bundled`` — the repo is the single source of truth for
    bundled content, so shipping an improved recipe (new guidance, a fixed
    caveat) reaches already-installed users on their next boot instead of
    being stuck forever at whatever version first got seeded. Returns the
    number of skills seeded or refreshed.

    Bundled skills have NO supported edit path — the panel/IPC surface
    rejects every write (set_enabled/update_skill/delete_skill/import_skill)
    for ``origin: bundled`` (see SkillRegistry._bundled_immutable_result).
    The only way a target's content diverges from the shipped source is a
    user hand-editing the file directly, outside any supported flow — and
    even then, unless they also strip/change the ``origin:`` line (which
    they have no product-facing reason to know exists), this refresh will
    overwrite that edit on the next boot. This is intentional: origin is the
    single knob that means "authoritative content lives in the repo, not
    here" — once True, it stays true.

    A target whose ``origin`` is NOT ``bundled`` (a hand-created/imported
    user skill with a colliding name, or one where the origin line was
    itself edited away) is left untouched — seeding never overwrites those.

    When the bundled dir and the user dir resolve to the SAME path (dev mode
    where the install dir is the repo AND the user root points there), seeding
    is a no-op.

    Historical-file backfill: a target that predates ``origin:`` entirely
    (no frontmatter key at all) is compared byte-for-byte against the shipped
    source (ignoring the ``origin:`` line) — an exact match proves the user
    never touched the file, so it's safe to stamp ``origin: bundled`` onto it
    AND refresh its content in the same pass. A target with no ``origin:``
    key that differs from the shipped source keeps reading as user-owned,
    visible and editable in the panel, exactly as before.
    """
    src = _bundled_skills_dir()
    if src is None:
        return 0
    dest = dest_root if dest_root is not None else _default_skills_root()
    try:
        if src.resolve() == dest.resolve():
            return 0
    except OSError:
        pass
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        _logger.exception("seed_bundled_skills: cannot create %s", dest)
        return 0
    seeded = 0
    try:
        candidates = [p for p in src.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return 0
    for skill_dir in candidates:
        src_md = skill_dir / _SKILL_FILE
        if not src_md.is_file():
            continue
        target = dest / skill_dir.name
        target_md = target / _SKILL_FILE
        if target.exists():
            existing_origin = _read_origin(target_md)
            if existing_origin is None:
                # No `origin:` key at all — predates the field. Backfill it
                # (and refresh content in the same pass) only if untouched;
                # otherwise leave the user-owned file exactly as-is.
                _backfill_bundled_origin_if_untouched(src_md, target_md)
                continue
            if existing_origin != SKILL_ORIGIN_BUNDLED:
                continue  # hand-created/imported user skill with this name
            # Still bundled — the repo is authoritative, but skip the
            # rmtree+copytree dance entirely when nothing actually changed.
            # Without this check, EVERY boot rewrites EVERY bundled skill's
            # files unconditionally, even when the repo content is byte-for-
            # byte identical to last boot — pure wasted disk I/O plus a
            # needless run through the Windows-lock retry path on every
            # single startup, for the (usual) case where nothing shipped.
            if _dir_contents_equal(skill_dir, target):
                continue
            # Copy to a sibling temp dir FIRST, then swap — an rmtree()
            # immediately followed by copytree() to the same path can race a
            # transient Windows lock on the just-deleted directory (see
            # _rmtree_with_retry). Copy-then-swap also means we never have a
            # moment where the target is both gone and being recreated at
            # the same path.
            tmp_target = dest / f".{skill_dir.name}.refresh-tmp"
            try:
                if tmp_target.exists():
                    _rmtree_with_retry(tmp_target)
                shutil.copytree(skill_dir, tmp_target)
                _rmtree_with_retry(target)
                tmp_target.rename(target)
                seeded += 1
            except OSError:
                _logger.exception(
                    "seed_bundled_skills: failed to refresh %s", skill_dir.name
                )
                if tmp_target.exists():
                    try:
                        _rmtree_with_retry(tmp_target)
                    except OSError:
                        pass
            continue
        try:
            shutil.copytree(skill_dir, target)
            seeded += 1
        except OSError:
            _logger.exception(
                "seed_bundled_skills: failed to copy %s", skill_dir.name
            )
    if seeded:
        _logger.info("seed_bundled_skills: seeded/refreshed %d bundled recipe skill(s)", seeded)
    return seeded


def _read_origin(skill_md: Path) -> Optional[str]:
    """Best-effort read of the raw ``origin:`` frontmatter value.

    Returns ``None`` when the file is unreadable, has no frontmatter, or the
    ``origin`` key is absent — the caller distinguishes "absent" (historical
    pre-origin file, eligible for backfill) from "present but not bundled"
    (leave alone). Does NOT use ``_coerce_origin`` — that fails safe to
    ``"user"`` for anything unrecognised, which would collapse the "absent"
    and "present-but-different" cases this function needs to tell apart.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict) or "origin" not in fm:
        return None
    raw = fm.get("origin")
    return str(raw).strip() if raw is not None else None


_ORIGIN_LINE_RE = re.compile(r"(?m)^origin:\s*\S+\s*\n?")


def _backfill_bundled_origin_if_untouched(src_md: Path, target_md: Path) -> None:
    """Stamp ``origin: bundled`` onto *target_md* iff it is byte-identical to
    *src_md* apart from the ``origin:`` frontmatter line itself.

    Pre-existing installs seeded a bundled skill before ``origin: bundled``
    was added to the shipped files — this repairs those files in place, once,
    the first time this runs after upgrading. Any read/write failure is
    swallowed (best-effort — worst case the file just stays user-owned).
    """
    if not target_md.is_file():
        return
    try:
        src_text = src_md.read_text(encoding="utf-8")
        target_text = target_md.read_text(encoding="utf-8")
    except OSError:
        return
    if _ORIGIN_LINE_RE.sub("", src_text) != _ORIGIN_LINE_RE.sub("", target_text):
        return  # user has touched this file — leave it as user-owned
    if _ORIGIN_LINE_RE.search(target_text):
        return  # already stamped (or stamped with a non-bundled value; leave it)
    try:
        target_md.write_text(src_text, encoding="utf-8")
    except OSError:
        _logger.exception(
            "seed_bundled_skills: failed to backfill origin for %s", target_md
        )


def _scan_skill_root(root: Path) -> Dict[str, SkillEntry]:
    """Walk *root* and return a {name: SkillEntry} map.

    Each immediate subdirectory is a skill candidate; we read its
    ``SKILL.md`` and parse the frontmatter. Failures are logged and
    skipped so one bad skill cannot block the rest.

    Name resolution rule: when frontmatter ``name`` differs from the
    directory name, the directory name wins — that's the canonical value the
    menu lists and that ``read_skill`` matches on. The discrepancy is
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
    enabled = _coerce_enabled(fm.get("enabled"))
    standing = _coerce_standing(fm.get("standing"))
    origin = _coerce_origin(fm.get("origin"))
    # allowed-tools accepts either an inline list or a comma-separated string
    # (Claude Code allows both). Hyphen and underscore spellings are both
    # honored so hand-authored files aren't brittle.
    allowed_tools = _coerce_str_list(
        fm.get("allowed-tools", fm.get("allowed_tools"))
    )

    # Invariant: standing implies enabled. A standing skill that is somehow
    # disabled (only reachable by hand-editing the file — the panel can't
    # produce this combo) is silently force-enabled at load time. No warning:
    # the state is self-correcting and not user-reachable, so flagging it is
    # just noise.
    if standing and not enabled:
        enabled = True

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
        problems.append("empty body — read_skill would return nothing useful")

    entry = SkillEntry(
        name=canonical_name,
        description=description,
        body=body,
        source_path=str(skill_md.resolve()),
        problems=problems,
        enabled=enabled,
        standing=standing,
        origin=origin,
        allowed_tools=allowed_tools,
    )
    return entry


def _coerce_str_list(raw: object) -> List[str]:
    """Interpret a frontmatter value that should be a list of short strings.

    Accepts an inline YAML list (``[a, b]``), a comma-separated string
    (``"a, b"``), or a single scalar. Returns a de-duplicated, order-preserving
    list of non-empty trimmed strings. Missing / null → empty list.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw]
    else:
        items = [str(raw).strip()]
    out: List[str] = []
    seen = set()
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _coerce_enabled(raw: object) -> bool:
    """Interpret the frontmatter ``enabled`` value.

    Missing/``null`` → True (legacy skills stay on). PyYAML already turns
    ``true/false/yes/no/on/off`` into ``bool``; we additionally treat the
    common string/int spellings of "off" as disabled so a hand-edited file
    behaves intuitively. Anything else is enabled — we fail *open*, never
    silently hiding a skill because of an odd value.
    """
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return str(raw).strip().lower() not in {
        "false", "0", "no", "off", "disabled", "none", "",
    }


def _coerce_standing(raw: object) -> bool:
    """Interpret the frontmatter ``standing`` value — fail *closed*.

    Missing/``null``/unrecognised → False (a skill is standing only when it
    explicitly opts in). PyYAML already turns ``true/false/yes/no/on/off`` into
    ``bool``; we additionally treat the common string/int spellings of "on" as
    standing. This is the mirror image of :func:`_coerce_enabled`: enabled fails
    open (default active), standing fails closed (default not always-on), so an
    odd value can never silently make a skill always-in-effect.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return str(raw).strip().lower() in {
        "true", "1", "yes", "on", "enabled",
    }


def _coerce_origin(raw: object) -> str:
    """Interpret the frontmatter ``origin`` value — fail *safe to user-owned*.

    Only the exact tokens ``auto`` and ``bundled`` (case-insensitive) mark a
    skill as triage-minted / product-shipped respectively; everything else —
    missing, ``user``, or an unrecognised value — resolves to ``user``. This
    is deliberately the protective default: origin gates both whether the
    auto-miner may overwrite a skill's content AND whether the panel may see
    or modify it at all, so an ambiguous marker MUST mean "hands off, fully
    visible" (user-owned), never "safe to clobber" or "hide from the user".
    A parse hiccup can never accidentally make the user's own skill
    disappear from their panel or become uneditable.
    """
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token == SKILL_ORIGIN_AUTO:
            return SKILL_ORIGIN_AUTO
        if token == SKILL_ORIGIN_BUNDLED:
            return SKILL_ORIGIN_BUNDLED
    return SKILL_ORIGIN_USER
