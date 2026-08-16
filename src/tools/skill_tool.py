"""
read_skill — pull the full body of an available skill on demand.

This is the agent-facing half of the progressive-disclosure skill model
(mirrors Claude Code's Skills). The [Available Skills] menu that every role
sees lists only ``name: description`` pairs — cheap to keep resident. When the
current task matches one of those skills, the agent calls ``read_skill(name)``
to load its full instructions, exactly like it would ``read`` a file it needs.

Standing skills are injected transparently into the prompt and do NOT appear
in the menu — their bodies are already in context as plain instructions.

Resolution goes through :meth:`SkillRegistry.get_skill`, which is enabled-only —
a disabled or unknown name returns ``success=False`` with a hint pointing back
at the menu, never a body.

``${SKILL_DIR}`` substitution: mirrors Claude Code's ``${CLAUDE_SKILL_DIR}``.
A skill's own directory is resolved at read-time (not baked into the file, so
it works across machines/install layouts) and any literal ``${SKILL_DIR}`` in
the body is replaced with that absolute path — letting a skill ship a companion
script (e.g. ``cdp_lib.py`` next to its ``SKILL.md``) and reference it with
``python ${SKILL_DIR}/cdp_lib.py`` instead of embedding the whole script inline
or hardcoding a path that only exists on one machine.
"""
import time
from pathlib import Path

from .base_tool import BaseTool, ToolResult


def _substitute_skill_dir(body: str, source_path: str) -> str:
    """Replace literal ``${SKILL_DIR}`` in *body* with the skill's own
    directory (parent of its ``SKILL.md``, i.e. *source_path*), resolved
    fresh at read-time so it's correct on whatever machine/install layout
    this is. Skills without the placeholder are returned unchanged — this
    is a no-op string replace, not a template engine, so a skill with no
    companion files pays no cost.
    """
    if "${SKILL_DIR}" not in body:
        return body
    skill_dir = str(Path(source_path).resolve().parent)
    return body.replace("${SKILL_DIR}", skill_dir)


class ReadSkillTool(BaseTool):
    """Load the full instructions of an available skill by name."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx=None):
        super().__init__("read_skill", ctx=ctx)

    async def execute(self, name: str = "", **kwargs) -> ToolResult:
        """Return the body of the enabled skill *name*.

        Args:
            name: skill identifier as shown in the [Available Skills] menu.

        Returns:
            ToolResult(success=True, output={name, description, body}) on a hit;
            success=False with a menu hint for an unknown / disabled name.
        """
        start = time.time()
        params = {"name": name}
        skill_name = (name or "").strip()

        if not skill_name:
            return ToolResult(
                success=False,
                output=None,
                error="read_skill requires a 'name'; pick one from the [Available Skills] menu.",
                execution_time=time.time() - start,
                tool_name=self.name,
                tool_parameters=params,
            )

        try:
            from ..infrastructure.skills import SkillRegistry
            entry = SkillRegistry.get().get_skill(skill_name)
        except Exception as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"read_skill failed to resolve '{skill_name}': {exc}",
                execution_time=time.time() - start,
                tool_name=self.name,
                tool_parameters=params,
            )

        if entry is None:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"unknown or disabled skill '{skill_name}'; "
                    "pick a name from the [Available Skills] menu."
                ),
                execution_time=time.time() - start,
                tool_name=self.name,
                tool_parameters=params,
            )

        # Skill-driven tool activation: a skill is a recipe PLUS the on-demand
        # tools that recipe needs. Reading the skill activates those tools in one
        # step (mirrors Claude Code's `allowed-tools`), so the agent does not
        # have to separately reason about which tools to claim. Activation goes
        # through the task channel's append-only bus. Fire-and-forget: a
        # failure here must not block returning the body (the agent can still
        # claim_tool manually).
        activated: list = []
        if entry.allowed_tools:
            task_channel = getattr(self.ctx, "_task_channel", None) if self.ctx else None
            if task_channel is not None:
                try:
                    activated = list(task_channel.activate_tools(entry.allowed_tools))
                except Exception:
                    activated = []

        output = {
            "name": entry.name,
            "description": entry.description,
            "body": _substitute_skill_dir(entry.body.rstrip(), entry.source_path),
        }
        if entry.allowed_tools:
            # Report what the skill grants so the agent knows these tools are
            # (or will be, next turn) available without a claim_tool round-trip.
            output["tools_activated"] = activated
            output["allowed_tools"] = list(entry.allowed_tools)

        return ToolResult(
            success=True,
            output=output,
            execution_time=time.time() - start,
            tool_name=self.name,
            tool_parameters=params,
        )
