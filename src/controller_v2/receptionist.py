"""
ReceptionistMixin — Skills prompt block rendering + post-LLM activation.

Skills state lives in `SharedCheckList._active_skills` (append-only, session-level).
This mixin's only job is to:
  - render the [Available Skills] prompt block (read from checklist)
  - activate skills the LLM lists in `skills_needed` into the checklist
    (write to checklist scope)

@-mention scanning lives in `mention_preprocessing` (ingress preprocessing).
The receptionist no longer owns ANY skill state — it is purely a renderer + activator.

Usage:
  class Orchestrator(ReceptionistMixin):
      def __init__(self, ...):
          # No _init_receptionist() needed — checklist owns the state.
          ...
"""
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set

if TYPE_CHECKING:
    from .shared_checklist import SharedCheckList


class ReceptionistMixin:
    """Receptionist capabilities mixed into Orchestrator.

    Expects the host class to provide:
      - self._checklist: SharedCheckList  (owns the active_skills set)
      - self._on_reply_to_user: Optional[Callable[[str], Any]]
      - self.logger
    """

    if TYPE_CHECKING:
        _checklist: "SharedCheckList"

    # ── Public API (used by Orchestrator) ────────────────────────────────────

    def _activate_skills_in_checklist(
        self,
        parsed: Dict,
        prescan: Optional[Set[str]] = None,
    ) -> List[str]:
        """Activate skills into the checklist's session scope.

        Pipeline:
          1. Merge LLM-declared `skills_needed` with user `prescan` (the
             @-mention set extracted at ingress).
          2. Filter against SkillRegistry (drop unknown names defensively).
          3. Call `checklist.activate_skills(valid)` — this is where the
             session-level write actually lands. The checklist's
             on_skills_changed callbacks fire synchronously, which causes
             PersistentAgent to inject the new skill bodies into observation.
          4. Notify UI via _on_reply_to_user.
          5. Log.

        `prescan` comes from `mention_preprocessing.preprocess_mentions` at
        message ingress; it is the user's explicit @-mentions that we keep
        even if the LLM forgets to list them.

        Returns the names that were newly activated this turn (empty list
        when all named skills were already active or none were valid).
        """
        merged = self._merge_activated_skills(parsed, prescan)
        if not merged:
            return []

        # Filter against registry — drop unknown names defensively.
        try:
            from ..infrastructure.skills import SkillRegistry
            registry = SkillRegistry.get()
        except Exception:
            return []

        valid = [n for n in merged if registry.has(n)]
        if not valid:
            return []

        new = self._checklist.activate_skills(valid)
        if not new:
            return []

        on_reply = getattr(self, '_on_reply_to_user', None)
        if on_reply:
            try:
                on_reply(f"Skills activated: {', '.join(new)}")
            except Exception:
                pass

        logger = getattr(self, 'logger', None)
        if logger:
            logger.info(
                f"Skills activated: {new}",
                component="Orchestrator",
            )
        return new

    def _build_skills_section(self) -> str:
        """Render the [Available Skills] prompt block from checklist state.

        All installed skills are listed with their description. Skills that
        are already in checklist.active_skills are tagged with "(active)" so
        the planner LLM doesn't redundantly request re-activation.

        Returns "" when no skills are installed.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            registry = SkillRegistry.get()
        except Exception:
            return ""
        all_names = registry.names()
        if not all_names:
            return ""

        active_set = {n for n in self._checklist.active_skills if registry.has(n)}

        lines: List[str] = ["[Available Skills — additional methodologies]"]
        for name in sorted(all_names):
            entry = registry.get_skill(name)
            if entry is None:
                continue
            tag = " (active)" if name in active_set else ""
            lines.append(f"  - {entry.name}{tag}: {entry.description}")

        lines.append(
            "\nRules:\n"
            "  - Names tagged (active) are already on for this session.\n"
            "  - When the user's request semantically matches a non-active skill "
            "(same task type, same domain), list the skill name in `skills_needed`.\n"
            "  - Re-listing already-active names is safe — the system handles dedup.\n"
            "  - Never invent skill names. Only names that appear above are valid.\n"
            "  - When in doubt, leave `skills_needed` empty."
        )

        return "\n".join(lines).rstrip() + "\n"

    # ── Internal ─────────────────────────────────────────────────────────────

    def _merge_activated_skills(
        self,
        parsed: Dict,
        prescan_skills: Optional[Set[str]] = None,
    ) -> List[str]:
        """Merge LLM-suggested + prescan @-mentions, filter against registry.

        Returns sorted list of valid skill names for the post-LLM commit step.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            registry = SkillRegistry.get()
            registry_has = registry.has
        except Exception:
            registry_has = lambda _n: False  # noqa: E731

        skills_raw = parsed.get("skills_needed") or []
        if not isinstance(skills_raw, list):
            skills_raw = [skills_raw]
        llm_skills = {str(s).strip() for s in skills_raw if str(s).strip()}

        unknown = {s for s in llm_skills if not registry_has(s)}
        if unknown:
            logger = getattr(self, "logger", None)
            if logger:
                logger.warning(
                    f"LLM returned unknown skill names {sorted(unknown)} — dropping",
                    component="Orchestrator",
                )
            llm_skills -= unknown

        return sorted((prescan_skills or set()) | llm_skills)
