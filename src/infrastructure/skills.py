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

import base64
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
# with HandQ itself and scanned in place from the install-dir ``Skill/`` root
# (see _scan_two_roots) — invisible to the panel and immutable via the
# skill_* IPC surface (see SkillRegistry.list_all / _bundled_immutable_result).
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
      process_hints:   ``{process_name_lowercased: hint}`` — app-specific
                   quirks that need to be re-surfaced every time that process
                   is the acting target, not just once when the skill is
                   read. ``read_skill`` puts the whole skill body in context
                   ONE time; by turn 200 of a long task that text has scrolled
                   far outside the model's effective attention (confirmed
                   live 2026-07-26: the alpaca-workflow skill already
                   documented "TAC's none_detected is expected", read at
                   turn 1, but the agent still spent ~40 minutes rediscovering
                   it near turn 200). desktop_tool.py's click handlers look up
                   the foreground process here on EVERY none_detected result
                   and append any match to that turn's ``effect_hint`` — so
                   the knowledge reappears exactly when it's relevant, however
                   deep into the task that is. Empty for skills with no
                   process-specific behavior to pin.
      mtime:       ``SKILL.md``'s filesystem modification time (epoch
                   seconds), used only for panel display ("uploaded on ...");
                   not part of the skill's semantics.
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
    process_hints: Dict[str, str] = field(default_factory=dict)
    mtime: float = 0.0


class SkillRegistry:
    """Process-level skill registry.

    Mirrors :class:`LongTermMemory` lifecycle:
      * :meth:`init` is called once at boot; it scans the Skill root.
      * :meth:`get` returns the singleton (or an empty fallback before
        init has run, so tests / standalone tools can call freely).
    """

    _instance: Optional["SkillRegistry"] = None

    def __init__(
        self,
        user_root: Path,
        entries: Dict[str, SkillEntry],
        *,
        bundled_root: Optional[Path] = None,
    ) -> None:
        # Two roots, merged into one flat registry:
        #   * bundled_root — product-shipped recipes next to the bridge exe
        #     (``<install_dir>/Skill``). Read directly, never copied. The
        #     authoritative version of every ``origin: bundled`` skill.
        #   * user_root — the user's own skills (``%USERPROFILE%\HandQ\Skill``).
        #     All panel writes (create/edit/enable/delete) land here.
        # A user skill shadows a bundled one of the same name (see
        # ``_scan_two_roots``) — that's how a user disables/overrides a shipped
        # recipe without the product ever mutating the bundled copy.
        self._user_root = user_root
        self._bundled_root = bundled_root
        self._entries: Dict[str, SkillEntry] = entries

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    def init(
        cls,
        user_root: Optional[Path] = None,
        *,
        bundled_root: Optional[Path] = None,
    ) -> "SkillRegistry":
        """Scan the bundled + user Skill roots and build the singleton.

        ``user_root`` lets tests point at an isolated directory; production
        boot calls ``init()`` with no argument and falls back to
        :func:`_default_skills_root` (``%USERPROFILE%\\HandQ\\Skill``).
        ``bundled_root`` defaults to :func:`_bundled_skills_dir` (the shipped
        ``<install_dir>/Skill``); pass it explicitly in tests. Bundled skills
        are read straight from their install location — never copied into the
        user root — so a read-only or partially-written user dir can no longer
        make a shipped skill vanish. Re-init replaces the singleton — useful
        for tests but not exposed via IPC.
        """
        scan_user_root = user_root if user_root is not None else _default_skills_root()
        scan_bundled_root = (
            bundled_root if bundled_root is not None else _bundled_skills_dir()
        )
        try:
            scan_user_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Don't let a permission error kill boot — the user root may be
            # uncreatable, but bundled skills still load from the install dir.
            # Proceed with whatever the bundled scan yields.
            _logger.exception(
                "SkillRegistry.init: could not create %s; loading bundled only",
                scan_user_root,
            )
            entries = _scan_two_roots(scan_bundled_root, scan_user_root)
            cls._instance = cls(
                scan_user_root, entries, bundled_root=scan_bundled_root
            )
            return cls._instance

        entries = _scan_two_roots(scan_bundled_root, scan_user_root)
        cls._instance = cls(
            scan_user_root, entries, bundled_root=scan_bundled_root
        )
        _logger.info(
            "SkillRegistry initialised: bundled=%s user=%s, %d skill(s) loaded%s",
            scan_bundled_root,
            scan_user_root,
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
            return cls(
                _default_skills_root(), {}, bundled_root=_bundled_skills_dir()
            )
        return cls._instance

    def reload(self) -> None:
        """Re-scan both Skill roots.

        The panel's CRUD paths update entries in place (``_refresh_entry``) and
        don't need this; it stays for the "something changed the files out of
        band" case (triage direct-writes an auto-generated skill, manual edits,
        a future ``/skill-reload``). Triage runs in-process and calls the write
        API directly, so its new skills are visible without an explicit reload.
        """
        self._entries = _scan_two_roots(self._bundled_root, self._user_root)
        _logger.info(
            "SkillRegistry reloaded: %d skill(s) (bundled=%s user=%s)",
            len(self._entries),
            self._bundled_root,
            self._user_root,
        )

    @property
    def user_root(self) -> Path:
        """The user-authored Skill root this instance scans/writes.

        Exposed read-only for callers (remote skill push) that need to walk a
        skill's files on disk directly rather than going through the
        per-field write API — bundled skills are never a legitimate source
        here, so there is no equivalent accessor for ``_bundled_root``.
        """
        return self._user_root

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

    def hints_for_process(self, process_name: str) -> List[Tuple[str, str]]:
        """``[(skill_name, hint), ...]`` across every ENABLED skill whose
        ``process_hints`` has a case-insensitive key match for *process_name*.

        Agent-facing discovery (same enabled-only rule as :meth:`get_skill` /
        :meth:`render_menu_block`) — a disabled skill's hints must not
        re-surface just because desktop_tool happens to be looking at a
        process name it mentions. Called on every none_detected click result
        (see desktop_tool.py's ``_process_hint_suffix``) so this needs to
        stay cheap: a flat scan over already-loaded, already-lowercased dict
        keys, no I/O. Returns ``[]`` for an empty/unknown process name or no
        match — never raises, since a lookup miss is the common case.
        """
        proc = (process_name or "").strip().lower()
        if not proc:
            return []
        hits: List[Tuple[str, str]] = []
        for e in sorted(self._entries.values(), key=lambda x: x.name):
            if not e.enabled:
                continue
            hint = e.process_hints.get(proc)
            if hint:
                hits.append((e.name, hint))
        return hits

    # ── Mutation / panel API (Skill control panel) ───────────────────────────
    #
    # These see the FULL set (enabled + disabled) and write SKILL.md files
    # under the Skill root, refreshing the affected in-memory entry. They are
    # synchronous (file I/O); the IPC layer wraps them in asyncio.to_thread.
    # Each returns a JSON-serializable {"ok": bool, ...} the bridge forwards
    # verbatim. Name mutations are constrained to _NAME_PATTERN, so a crafted
    # name can never escape the Skill root.

    def list_all(self, *, include_bundled: bool = False) -> List[Dict[str, object]]:
        """Full inventory for the panel — enabled AND disabled skills.

        Excludes ``origin: bundled`` entries by default: product-shipped
        skills are not part of the user's own inventory, so they never
        appear in the panel and can't be discovered there to
        enable/disable/edit/delete. The Agent-facing side
        (render_menu_block / render_standing_block) is a SEPARATE code path
        and still surfaces bundled skills normally — this method only gates
        what the human-facing panel can see.

        ``include_bundled=True`` lifts that gate — used by the remote-control
        skill-list query, whose whole point is showing what a target already
        has, built-ins included.
        """
        return [
            {
                "name": e.name,
                "description": e.description,
                "enabled": e.enabled,
                "standing": e.standing,
                "origin": e.origin,
                "allowed_tools": list(e.allowed_tools),
                "process_hints": dict(e.process_hints),
                "body": e.body.strip(),
                "source_path": e.source_path,
                "problems": list(e.problems),
                "mtime": e.mtime,
            }
            for e in sorted(self._entries.values(), key=lambda x: x.name)
            if include_bundled or e.origin != SKILL_ORIGIN_BUNDLED
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
                      process_hints: Optional[Dict[str, str]] = None,
                      ) -> Dict[str, object]:
        name = (name or "").strip()
        description = (description or "").strip()
        if not _NAME_PATTERN.match(name):
            return {"ok": False, "reason": "invalid_name", "name": name}
        if not description:
            return {"ok": False, "reason": "missing_description"}
        if name in self._entries or (self._user_root / name).exists():
            return {"ok": False, "reason": "exists", "name": name}
        skill_md = self._user_root / name / _SKILL_FILE
        allowed_list = _coerce_str_list(list(allowed_tools) if allowed_tools else None)
        hints_dict = _coerce_process_hints(process_hints or {})
        try:
            skill_md.parent.mkdir(parents=True, exist_ok=True)
            skill_md.write_text(
                _render_skill_md(name, description, body or "", enabled=enabled,
                                 standing=standing, origin=origin,
                                 allowed_tools=allowed_list,
                                 process_hints=hints_dict),
                encoding="utf-8",
            )
        except OSError as exc:
            _logger.exception("SkillRegistry.create_skill write failed name=%s", name)
            return {"ok": False, "reason": "write_failed", "error": str(exc)}
        if not self._refresh_entry(skill_md, name):
            # The directory did not exist before this call (checked above), so
            # removing it is safe and leaves no unloadable debris for the next
            # scan to trip over.
            shutil.rmtree(skill_md.parent, ignore_errors=True)
            return {
                "ok": False,
                "reason": "unloadable_after_write",
                "name": name,
                "error": "rendered SKILL.md did not parse; nothing was created",
            }
        return {"ok": True, "name": name}

    def update_skill(
        self, name: str, *, new_name: Optional[str] = None,
        description: Optional[str] = None, body: Optional[str] = None,
        standing: Optional[bool] = None, origin: Optional[str] = None,
        allowed_tools: Optional[Iterable[str]] = None,
        process_hints: Optional[Dict[str, str]] = None,
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
                if new_name in self._entries or (self._user_root / new_name).exists():
                    return {"ok": False, "reason": "exists", "name": new_name}
                final_name = new_name
        final_desc = entry.description if description is None else description.strip()
        if not final_desc:
            return {"ok": False, "reason": "missing_description"}
        final_body = entry.body if body is None else body
        final_standing = entry.standing if standing is None else standing
        final_origin = entry.origin if origin is None else origin
        # allowed_tools / process_hints: None means "preserve"; an explicit
        # value (even empty) replaces. This lets callers add / change / clear
        # the grant or the hints independently.
        final_allowed = (
            entry.allowed_tools if allowed_tools is None
            else _coerce_str_list(list(allowed_tools))
        )
        final_hints = (
            entry.process_hints if process_hints is None
            else _coerce_process_hints(process_hints)
        )
        old_dir = Path(entry.source_path).parent
        new_md = self._user_root / final_name / _SKILL_FILE
        try:
            new_md.parent.mkdir(parents=True, exist_ok=True)
            new_md.write_text(
                _render_skill_md(final_name, final_desc, final_body,
                                 enabled=entry.enabled, standing=final_standing,
                                 origin=final_origin,
                                 allowed_tools=final_allowed,
                                 process_hints=final_hints),
                encoding="utf-8",
            )
            if final_name != name and old_dir.exists() and old_dir != new_md.parent:
                shutil.rmtree(old_dir, ignore_errors=True)
        except OSError as exc:
            _logger.exception("SkillRegistry.update_skill write failed name=%s", name)
            return {"ok": False, "reason": "write_failed", "error": str(exc)}
        if final_name != name:
            self._entries.pop(name, None)
        if not self._refresh_entry(new_md, final_name):
            # No rollback is possible here: on a rename the old directory was
            # already removed above. Say exactly where the file is and that the
            # body survived, so the user can repair it rather than assume the
            # edit was rejected.
            return {
                "ok": False,
                "reason": "unloadable_after_write",
                "name": final_name,
                "error": (
                    f"rendered SKILL.md did not parse; the file at {new_md} has an "
                    "intact body but will not load until its frontmatter is fixed"
                ),
            }
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

    def export_skill_files(self, name: str) -> List[Dict[str, str]]:
        """Read every file under a user-authored skill's own directory for
        transfer to another machine (remote skill push's source side).

        Returns ``[{path, content_b64}]`` where ``path`` is POSIX-style and
        relative to the skill's directory (``SKILL.md``, ``scripts/foo.py``,
        …). Raises ``ValueError`` for a name this registry doesn't recognise
        as user-owned — bundled skills are shipped identically to every
        machine already and are never a legitimate push source, so callers
        must reject them before this is reached (see ``push_skills_to`` in
        ``remote_control/hub.py``); this is the second gate, not the first.
        """
        entry = self._entries.get(name)
        if entry is None:
            raise ValueError(f"unknown skill {name!r}")
        if entry.origin == SKILL_ORIGIN_BUNDLED:
            raise ValueError(f"skill {name!r} is bundled, not user-owned")
        skill_dir = Path(entry.source_path).parent
        files: List[Dict[str, str]] = []
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(skill_dir).as_posix()
            content = path.read_bytes()
            files.append({
                "path": rel,
                "content_b64": base64.b64encode(content).decode("ascii"),
            })
        return files

    def _mirror_files_into(
        self, name: str, entries: List[Tuple[str, bytes]]
    ) -> Dict[str, object]:
        """Traversal-safe full mirror of ``(relative_path, content)`` pairs into
        ``self._user_root / name``.

        Full replace, not a diff: the target directory is removed and recreated
        from exactly *entries*, so a file present in the old copy but absent from
        *entries* is deleted. Every relative path is validated against traversal
        (``..`` segments, absolute paths) BEFORE anything is written — a
        malformed or hostile payload must never write outside the skill's own
        directory. The single home of that security check, shared by
        :meth:`receive_skill_push` (bytes from a remote push) and
        :meth:`import_skill` (bytes read off a local skill folder). Callers own
        the follow-up (``reload`` / ``_refresh_entry``); this only touches disk.
        """
        if not _NAME_PATTERN.match(name or ""):
            return {"ok": False, "name": name, "error": "invalid_name"}
        target_dir = (self._user_root / name).resolve()
        try:
            target_dir.relative_to(self._user_root.resolve())
        except ValueError:
            return {"ok": False, "name": name, "error": "invalid_name"}
        # Validate every path before writing anything — reject the whole mirror
        # if any single path would escape, rather than write a partial tree.
        for rel, _content in entries:
            rel = str(rel or "")
            if not rel or rel.startswith("/") or rel.startswith("\\"):
                return {"ok": False, "name": name, "error": f"invalid_path:{rel}"}
            candidate = (target_dir / rel).resolve()
            try:
                candidate.relative_to(target_dir)
            except ValueError:
                return {"ok": False, "name": name, "error": f"invalid_path:{rel}"}
        try:
            shutil.rmtree(target_dir, ignore_errors=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in entries:
                dest = target_dir / str(rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        except (OSError, ValueError) as exc:
            _logger.exception(
                "SkillRegistry._mirror_files_into write failed name=%s", name
            )
            return {"ok": False, "name": name, "error": str(exc)}
        return {"ok": True, "name": name}

    def receive_skill_push(
        self, name: str, files: List[Dict[str, str]]
    ) -> Dict[str, object]:
        """Mirror a pushed skill's files into this machine's user Skill root,
        replacing whatever currently exists under ``name`` (the receiving
        side of remote skill upload).

        Full mirror, not a diff: the target directory is removed and
        recreated from exactly the given ``files``, so a file present on the
        old copy but absent from ``files`` is deleted. Every ``path`` is
        checked against traversal (``..`` segments, absolute paths) before
        anything is written — a malformed or hostile payload must not be able
        to write outside the skill's own directory (see :meth:`_mirror_files_into`).
        """
        try:
            entries: List[Tuple[str, bytes]] = [
                (
                    str(f.get("path") or ""),
                    base64.b64decode(str(f.get("content_b64") or "")),
                )
                for f in files
            ]
        except (ValueError, TypeError) as exc:
            return {"ok": False, "name": name, "error": str(exc)}
        result = self._mirror_files_into(name, entries)
        if result.get("ok"):
            self.reload()
        return result

    def import_skill(self, src_path: str) -> Dict[str, object]:
        """Import a user-authored skill FOLDER from an arbitrary path.

        A skill is a directory (``SKILL.md`` plus siblings like ``scripts/`` and
        ``reference/``), so import copies the WHOLE folder — not just the
        markdown. The old file-only import silently dropped every sibling, so an
        imported multi-file recipe landed broken (its ``${SKILL_DIR}/scripts/...``
        references pointed at files that were never copied) while the panel still
        reported success.

        Accepts either the skill directory itself, or (leniently) a path to its
        ``SKILL.md`` — in which case its parent directory is imported. The
        directory's ``SKILL.md`` must parse and carry a ``description``.

        Semantics preserved from the file-only version: the name is derived from
        frontmatter ``name`` (else the source dir name), slugified; a
        user-picked skill is an explicit install so it lands ``enabled=True`` and
        ``origin=user`` regardless of source flags (off limits to the
        auto-miner); re-importing an existing name replaces it in place (full
        mirror — a sibling removed at the source is pruned here too); and a name
        colliding with an ``origin: bundled`` skill is refused.
        """
        path = Path(src_path)
        # Resolve to the skill directory: a folder is used directly; a SKILL.md
        # file resolves to its parent (lenient back-compat with the old picker).
        if path.is_dir():
            src_dir = path
        elif path.is_file() and path.name == _SKILL_FILE:
            src_dir = path.parent
        else:
            return {"ok": False, "reason": "not_a_skill_dir", "path": src_path}

        skill_md = src_dir / _SKILL_FILE
        if not skill_md.is_file():
            return {"ok": False, "reason": "no_skill_md", "path": str(src_dir)}
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.exception("SkillRegistry.import_skill read failed path=%s", skill_md)
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
        process_hints = _coerce_process_hints(
            fm.get("process-hints", fm.get("process_hints"))
        )
        raw_name = str(fm.get("name", "") or "").strip() or src_dir.name
        name = slugify_skill_name(raw_name)

        # Bundled-name guard: a user skill of the same name would shadow a
        # shipped one, so refuse — mirrors create/update/delete's protection.
        existing = self._entries.get(name)
        if existing is not None and existing.origin == SKILL_ORIGIN_BUNDLED:
            return self._bundled_immutable_result(name)

        # Collect the whole source tree as (posix-relative-path, bytes). SKILL.md
        # is collected too but its content is overwritten below with the
        # normalized render, so we skip reading it here and inject the render.
        try:
            collected: List[Tuple[str, bytes]] = []
            for p in sorted(src_dir.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(src_dir).as_posix()
                if rel == _SKILL_FILE:
                    continue  # replaced by the normalized render below
                collected.append((rel, p.read_bytes()))
        except OSError as exc:
            _logger.exception("SkillRegistry.import_skill collect failed dir=%s", src_dir)
            return {"ok": False, "reason": "read_failed", "error": str(exc)}

        # Normalize the SKILL.md to import's ownership rules (enabled + user
        # origin) while preserving parsed standing / allowed-tools / hints /
        # description / body. Rendered here, not read raw, so an imported skill
        # can never carry an origin: auto/bundled marker into the user root.
        normalized_md = _render_skill_md(
            name, description, body, enabled=True, standing=standing,
            origin=SKILL_ORIGIN_USER, allowed_tools=allowed_tools,
            process_hints=process_hints,
        )
        collected.append((_SKILL_FILE, normalized_md.encode("utf-8")))

        result = self._mirror_files_into(name, collected)
        if not result.get("ok"):
            # _mirror_files_into failures use the {ok, name, error} shape; map
            # to import's {ok, reason, ...} vocabulary for the panel.
            return {
                "ok": False,
                "reason": "write_failed",
                "name": name,
                "error": str(result.get("error", "")),
            }

        target_md = (self._user_root / name / _SKILL_FILE)
        if not self._refresh_entry(target_md, name):
            return {
                "ok": False,
                "reason": "unloadable_after_write",
                "name": name,
                "error": (
                    f"mirrored SKILL.md at {target_md} did not parse; the files "
                    "were copied but the skill will not load until its "
                    "frontmatter is fixed"
                ),
            }
        return {"ok": True, "name": name, "files": len(collected)}

    def _refresh_entry(self, skill_md: Path, dir_name: str) -> bool:
        """Re-parse one SKILL.md and replace its in-memory entry.

        Keeps the registry consistent with what a fresh boot scan would load
        (parsed ``enabled`` / description, recorded problems) without paying
        for a full rescan.

        Returns True when the file parsed and the entry is now registered.
        Callers MUST check it: a False means what was just written to disk is
        not loadable, so the skill does not exist for any agent-facing surface
        and will not exist after a restart either. Reporting success in that
        case is how a render defect turns into silent data loss (see
        ``_render_skill_md``'s docstring for the instance that motivated this).
        """
        try:
            entry = _load_skill_file(skill_md, dir_name=dir_name)
        except Exception:
            _logger.exception("SkillRegistry._refresh_entry: reload %s failed", skill_md)
            return False
        if entry is not None:
            self._entries[entry.name] = entry
            return True
        return False


# ── Helpers ────────────────────────────────────────────────────────────────


def _yaml_frontmatter_lines(mapping: Dict[str, object]) -> List[str]:
    """Serialize *mapping* into frontmatter lines using PyYAML's own emitter.

    Every caller-supplied string in the frontmatter MUST go through here rather
    than through f-string interpolation, so the emitter decides on quoting. A
    value containing a colon-then-space, a ``#``, a leading ``[``/``{``/``*``,
    or a trailing colon is valid text and invalid bare YAML; interpolating it
    produces a file that ``_load_skill_file`` discards whole.

    Always dump a MAPPING, never a bare scalar: ``yaml.safe_dump('a b c')``
    returns ``'a b c\\n...\\n'`` — with a document-end marker that would corrupt
    the frontmatter block on the very next parse.

    ``width`` is raised past PyYAML's 80-column default so a long value stays on
    one line. Folding would round-trip correctly (callers collapse whitespace to
    single spaces first) but these files are meant to be hand-editable, and a
    wrapped description reads badly.
    """
    text = yaml.safe_dump(
        mapping,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=4096,
    )
    return text.rstrip("\n").splitlines()


def _render_skill_md(name: str, description: str, body: str, *,
                     enabled: bool = True, standing: bool = False,
                     origin: str = SKILL_ORIGIN_USER,
                     allowed_tools: Optional[List[str]] = None,
                     process_hints: Optional[Dict[str, str]] = None) -> str:
    """Serialize a canonical SKILL.md.

    Only writes ``enabled: false`` when the skill is disabled — an enabled
    skill keeps minimal frontmatter (missing key == enabled), so hand-authored
    files stay clean. Similarly only writes ``standing: true`` when the skill
    is standing (missing key == not standing) and ``origin: auto``/``origin:
    bundled`` only for triage-minted / product-shipped skills (missing key ==
    user-owned, the protective default). ``allowed-tools`` is written only
    when non-empty (missing == none), same for ``process-hints`` (rendered as
    a YAML mapping block — hint text is a full sentence, too long for the
    inline-list style ``allowed-tools`` uses). ``description`` is flattened to a
    single line and then serialized by :func:`_yaml_frontmatter_lines`; each
    process-hint value is flattened and serialized the same way.

    Flattening ALONE is not enough, which is what this function previously got
    wrong: it emitted ``f"description: {desc_line}"`` directly, so a description
    containing a colon-then-space — ``"Flash device: full meta"``, the most
    natural way to write one — became a second key/value separator and made the
    frontmatter unparseable. ``_load_skill_file`` then discarded the entire
    skill while ``create_skill`` still returned ``ok: True``, so the panel and
    the memory system both reported success on a skill that no longer existed.
    Never interpolate a caller-supplied string into YAML; let the emitter quote.
    """
    desc_line = " ".join(str(description).split())
    lines = ["---"]
    # name goes through the emitter too. ``_NAME_PATTERN`` already rules out the
    # characters that would break bare YAML, so this is belt-and-braces — but it
    # keeps the "nothing is interpolated into frontmatter" invariant literally
    # true, instead of true-by-coincidence-of-another-validator.
    lines.extend(_yaml_frontmatter_lines({"name": name, "description": desc_line}))
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
    if process_hints:
        flat_hints = {
            proc: " ".join(str(hint).split())
            for proc, hint in process_hints.items()
        }
        lines.extend(_yaml_frontmatter_lines({"process-hints": flat_hints}))
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


def _scan_two_roots(
    bundled_root: Optional[Path], user_root: Path
) -> Dict[str, SkillEntry]:
    """Merge-scan the bundled + user Skill roots into one ``{name: entry}`` map.

    Bundled skills (shipped next to the bridge exe) are scanned first, then
    user skills are layered on top: a user skill of the same name SHADOWS the
    bundled one. That is the whole override mechanism — a user disables or
    rewrites a shipped recipe by dropping a same-named skill in their own root;
    the product never mutates the bundled copy, so it can't be corrupted by a
    read-only or half-written user dir (the failure this design replaces).

    ``bundled_root`` is Optional because :func:`_bundled_skills_dir` returns
    None when no shipped ``Skill/`` exists (a build without bundled recipes).
    When the two roots resolve to the SAME directory (dev mode: install dir ==
    repo AND the user root points there) we scan only once — otherwise every
    skill would collide with itself and log a spurious warning.
    """
    entries: Dict[str, SkillEntry] = {}
    same_root = False
    if bundled_root is not None:
        try:
            same_root = (
                user_root.exists()
                and bundled_root.exists()
                and bundled_root.resolve() == user_root.resolve()
            )
        except OSError:
            same_root = False
        entries = _scan_skill_root(bundled_root)
    if same_root:
        # Single physical dir behind both roots — the bundled scan already
        # covers everything; scanning user_root again would self-collide.
        return entries
    user_entries = _scan_skill_root(user_root)
    # User shadows bundled on name collision — explicit override, no warning
    # (the within-root "first wins" collision rule in _scan_skill_root does
    # not apply across roots; this is intentional layering, not a clash).
    for name, entry in user_entries.items():
        entries[name] = entry
    return entries



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
    # process-hints: {process.exe: "hint text"} — see SkillEntry docstring.
    process_hints = _coerce_process_hints(
        fm.get("process-hints", fm.get("process_hints"))
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
        process_hints=process_hints,
        mtime=skill_md.stat().st_mtime,
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


def _coerce_process_hints(raw: object) -> Dict[str, str]:
    """Interpret the frontmatter ``process-hints`` value.

    Only a YAML mapping is meaningful here (``{process.exe: "hint text"}``) —
    unlike ``allowed-tools``, there's no sensible flat-list or scalar
    shorthand for a name→text mapping. Missing / null / non-mapping → ``{}``
    (fails empty, mirrors ``_coerce_str_list``'s empty-list default — a
    skill with no process hints is just a skill with no process hints, not
    an error). Keys are lowercased so lookup at click time
    (``foreground process_name`` is already lowercased by callers) doesn't
    depend on how the hand-authored YAML capitalized ``TAC.exe`` vs
    ``tac.exe``. Non-string values are skipped individually rather than
    invalidating the whole mapping — one bad entry shouldn't cost every
    other hint in the skill.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        proc = str(key).strip().lower()
        hint = str(value).strip() if value is not None else ""
        if proc and hint:
            out[proc] = hint
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
