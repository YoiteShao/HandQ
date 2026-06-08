"""Programmatic tool — the MICRO granularity (ARCHITECTURE.md §2, §9 step 1).

The biggest speed win per unit effort: collapse many tool round-trips within a
single agent turn into one bounded local loop. Instead of the model issuing
read → (turn) → grep → (turn) → read → (turn) → edit, it emits one small program
that calls helper primitives locally and returns a single consolidated result.
This is Yansu's most impactful missing tool.

SANDBOX (KEEP_REBUILD.md open question #3): a restricted-Python ``eval`` is NOT
a real sandbox. The safe-by-construction option is a Starlark binding. Until
that dependency is decided, this scaffold exposes a *fixed helper surface* and
refuses arbitrary code — the helpers are the only thing callable, and they are
individually permission-checked exactly as the standalone tools are.

Helper surface (mirrors the standalone tools, bounded): read, grep, glob,
shell, write, web_fetch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .checkpoint import CheckpointManager

# Each helper is an async callable dispatched to the corresponding standalone
# tool, so permissions / interrupts / logging stay identical to direct use.
Helper = Callable[..., Awaitable[Any]]

# Helpers that change the filesystem and carry a ``path`` kwarg we can snapshot
# before running, so a failed program can be rolled back exactly (§8.7).
DEFAULT_MUTATING_HELPERS = frozenset({"write", "edit"})


@dataclass
class ProgrammaticResult:
    ok: bool
    steps: int
    output: str = ""
    error: Optional[str] = None
    log: list[str] = field(default_factory=list)
    # True when a failed program's filesystem writes were rolled back via the
    # checkpoint manager (only possible when one was supplied).
    rolled_back: bool = False


class ProgrammaticTool:
    """Runs a bounded sequence of helper calls in one turn.

    NOTE: this is a scaffold. The execution model (Starlark vs. a constrained
    instruction list) is deliberately left as a single ``run`` seam so the
    sandbox decision can be made without touching callers. ``max_steps`` bounds
    the loop so a runaway program cannot spin.

    When a ``CheckpointManager`` is supplied, every mutating helper step
    (``mutating_helpers``, default write/edit) is snapshotted before it runs, and
    a fail-fast abort rolls every snapshot from this program back — so a partial
    program never leaves half-written files behind.
    """

    name = "programmatic"

    def __init__(
        self,
        *,
        helpers: dict[str, Helper],
        max_steps: int = 50,
        checkpoint_manager: Optional[CheckpointManager] = None,
        mutating_helpers: frozenset[str] = DEFAULT_MUTATING_HELPERS,
    ) -> None:
        self._helpers = helpers
        self._max_steps = max_steps
        self._checkpoints = checkpoint_manager
        self._mutating = mutating_helpers

    def available_helpers(self) -> list[str]:
        return sorted(self._helpers)

    async def call_helper(self, name: str, /, **kwargs: Any) -> Any:
        helper = self._helpers.get(name)
        if helper is None:
            raise KeyError(f"unknown helper {name!r}; available: {self.available_helpers()}")
        return await helper(**kwargs)

    async def run(self, program: list[tuple[str, dict[str, Any]]]) -> ProgrammaticResult:
        """Execute a list of ``(helper_name, kwargs)`` steps, fail-fast.

        This explicit instruction-list form is the safe interim execution model
        (no arbitrary eval). A Starlark binding, if adopted, slots in here
        behind the same signature.

        With a checkpoint manager set, a mutating step is snapshotted before it
        runs and any failure rolls the whole program's writes back.
        """
        log: list[str] = []
        last: Any = None
        first_cp: Optional[str] = None  # earliest checkpoint this program took

        def fail(steps: int, error: str) -> ProgrammaticResult:
            rolled = False
            if self._checkpoints is not None and first_cp is not None:
                self._checkpoints.rollback_to(first_cp)
                rolled = True
                log.append(f"rolled back to {first_cp}")
            return ProgrammaticResult(ok=False, steps=steps, error=error, log=log,
                                      rolled_back=rolled)

        for i, (helper_name, kwargs) in enumerate(program):
            if i >= self._max_steps:
                return fail(i, "max_steps exceeded")
            if (self._checkpoints is not None and helper_name in self._mutating
                    and "path" in kwargs):
                cp = self._checkpoints.capture([kwargs["path"]], label=helper_name)
                if first_cp is None:
                    first_cp = cp
            try:
                last = await self.call_helper(helper_name, **kwargs)
                log.append(f"{helper_name}({', '.join(kwargs)}) ok")
            except Exception as exc:
                log.append(f"{helper_name} raised {exc!r}")
                return fail(i + 1, str(exc))
        return ProgrammaticResult(
            ok=True, steps=len(program), output=str(last) if last is not None else "", log=log
        )
