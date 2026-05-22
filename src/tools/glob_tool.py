"""
Glob Tool - Fast file pattern matching.
"""
import asyncio
import os
import time
from pathlib import Path, PurePosixPath
from typing import List, Optional
from .base_tool import BaseTool, ToolResult


_MAX_RESULTS = 200

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".eggs", "*.egg-info",
})


def _should_skip_dir(name: str) -> bool:
    """Return True if directory name should be excluded from results."""
    if name in _SKIP_DIRS:
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def _glob_sync(pattern: str, search_dir: str, skip_default_dirs: bool = True) -> dict:
    """Synchronous glob implementation — runs in executor thread.

    Returns a dict with matches (relative paths as forward-slash strings),
    count, and truncated flag.
    """
    search_path = Path(search_dir).resolve()

    if not search_path.exists():
        return {"error": f"Search path does not exist: {search_dir}"}
    if not search_path.is_dir():
        return {"error": f"Search path is not a directory: {search_dir}"}

    # Collect all matches, filtering out skip-dirs
    raw_matches: List[tuple] = []  # (relative_path_str, mtime)
    try:
        for match in search_path.glob(pattern):
            # Skip if any parent component is in the skip list
            try:
                rel = match.relative_to(search_path)
            except ValueError:
                continue

            parts = rel.parts
            if skip_default_dirs and any(_should_skip_dir(p) for p in parts[:-1]):
                continue
            # Skip directories themselves (glob can match dirs)
            if match.is_dir():
                continue

            try:
                mtime = match.stat().st_mtime
            except OSError:
                mtime = 0.0

            # Normalize to forward slashes for cross-platform consistency
            rel_str = "/".join(parts)
            raw_matches.append((rel_str, mtime))

            # Collect up to 5x limit for sorting, then early-terminate
            if len(raw_matches) >= _MAX_RESULTS * 5:
                break

    except OSError as e:
        return {"error": f"Glob failed: {e}"}

    # Sort by modification time (most recent first)
    raw_matches.sort(key=lambda x: x[1], reverse=True)

    truncated = len(raw_matches) > _MAX_RESULTS
    matches = [m[0] for m in raw_matches[:_MAX_RESULTS]]

    return {
        "matches": matches,
        "count": len(matches),
        "total_found": len(raw_matches),
        "truncated": truncated,
    }


class GlobTool(BaseTool):
    """Fast file pattern matching — find files by glob pattern."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self):
        super().__init__("glob")

    async def execute(
        self,
        pattern: str,
        path: Optional[str] = None,
        skip_default_dirs: bool = True,
        **kwargs
    ) -> ToolResult:
        """Find files matching a glob pattern.

        Args:
            pattern:           Glob pattern (e.g., '**/*.py', 'src/**/*.ts').
            path:              Directory to search in. Defaults to current directory.
            skip_default_dirs: If True (default), skip .git, node_modules, __pycache__,
                               etc. Set to False to include all directories.

        Returns:
            ToolResult with matches sorted by modification time (newest first).
        """
        start_time = time.time()

        try:
            self.validate_params(["pattern"], {"pattern": pattern})

            search_dir = path or "."

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: _glob_sync(pattern, search_dir, skip_default_dirs),
            )

            if "error" in result:
                return ToolResult(
                    success=False,
                    output=None,
                    error=result["error"],
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"pattern": pattern, "path": path, "skip_default_dirs": skip_default_dirs},
                )

            output = {
                "pattern": pattern,
                "search_path": str(Path(search_dir).resolve()),
                "matches": result["matches"],
                "count": result["count"],
            }
            if result["truncated"]:
                output["truncated"] = True
                output["notice"] = (
                    f"Results truncated: showing {_MAX_RESULTS} of "
                    f"{result['total_found']} matches. Use a more specific pattern."
                )

            return ToolResult(
                success=True,
                output=output,
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"pattern": pattern, "path": path, "skip_default_dirs": skip_default_dirs},
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Glob failed: {e}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"pattern": pattern, "path": path, "skip_default_dirs": skip_default_dirs},
            )
