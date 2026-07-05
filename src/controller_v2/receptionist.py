"""
ReceptionistMixin — progressive-disclosure skill prompt rendering.

The receptionist is a single-turn generator (INTENT stage): it classifies the
user message and writes a chat reply, but it cannot fetch a skill body
mid-turn the way the agent can. So it receives only the *awareness* layer of
the progressive-disclosure model:

  - the [Available Skills] menu (name + description of every enabled non-standing
    skill), for reference only — actually reading/executing a skill is the
    agent's job;
  - standing skill bodies, injected as transparent prompt text that defines the
    receptionist's communication style and shapes ``response_to_user``.

Both are rendered LIVE from the SkillRegistry singleton, so panel toggles
(enable / standing / CRUD) take effect immediately. The receptionist owns NO
skill state and performs NO activation — that concept is gone. @-mention
normalization still lives in ``mention_preprocessing`` (ingress preprocessing),
but the normalized text simply rides along in the user message; the agent
decides whether to ``read_skill`` it.

Usage:
  class Orchestrator(ReceptionistMixin):
      def __init__(self, ...):
          ...
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .shared_checklist import SharedCheckList


class ReceptionistMixin:
    """Receptionist skill-awareness rendering mixed into Orchestrator.

    Expects the host class to provide:
      - self.logger  (optional)
    """

    if TYPE_CHECKING:
        _checklist: "SharedCheckList"

    # ── Public API (used by Orchestrator) ────────────────────────────────────

    def _build_skills_menu_block(self) -> str:
        """Render the [Available Skills] menu for the receptionist.

        The menu (name + description of every enabled non-standing skill) is
        rendered live from the SkillRegistry. Standing skills are excluded
        (their bodies are already injected as transparent prompt text).
        Wrapped with an instruction that this is for the receptionist's
        reference only — pulling a skill body and executing it is the agent's
        job, not the receptionist's.

        Returns "" when no enabled skills exist or on any error.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            reg = SkillRegistry.get()
            menu = reg.render_menu_block(exclude=reg.standing_names())
        except Exception:
            return ""
        if not menu:
            return ""
        return (
            menu
            + "\n\nThese skills are available for reference only — they inform "
            "how you talk about what the system can do. Actually reading a "
            "skill's instructions and executing it is the agent's job, not "
            "yours; do not attempt to apply them here."
        )

    def _build_standing_block(self) -> str:
        """Render standing skill bodies as transparent style instructions.

        Standing skills define the receptionist's communication style and must
        be applied to ``response_to_user``. They do NOT change task/chat
        classification — style only. The body is plain prompt text with no
        skill attribution — the receptionist just follows the instructions.

        Returns "" when no enabled+standing skills exist or on any error.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            standing = SkillRegistry.get().render_standing_block()
        except Exception:
            return ""
        if not standing:
            return ""
        return (
            standing
            + "\n\nThe instructions above define your communication style. "
            "Apply them to `response_to_user`. They do NOT change your task/chat "
            "classification — style only."
        )
