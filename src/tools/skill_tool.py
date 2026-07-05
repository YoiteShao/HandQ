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
"""
import time

from .base_tool import BaseTool, ToolResult


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

        return ToolResult(
            success=True,
            output={
                "name": entry.name,
                "description": entry.description,
                "body": entry.body.rstrip(),
            },
            execution_time=time.time() - start,
            tool_name=self.name,
            tool_parameters=params,
        )
