"""
Grep Tool - Search file contents by regex pattern.

Supports:
  - Three output modes: files_only, content, count
  - Separate -A (after), -B (before), -C (context) line parameters
  - Multiline mode for cross-line pattern matching
  - head_limit + offset for result pagination
"""
import asyncio
import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from .base_tool import BaseTool, ToolResult
from .read_tool import _BINARY_EXTENSIONS


_MAX_SEARCH_FILE_SIZE = 1_048_576  # 1 MB
_MAX_OUTPUT_CHARS = 15_000         # 15 KB output cap
_DEFAULT_HEAD_LIMIT = 100

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".eggs",
})


def _should_skip_dir(name: str) -> bool:
    if name in _SKIP_DIRS:
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def _is_binary_ext(filepath: str) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    return ext in _BINARY_EXTENSIONS


def _read_file_text(filepath: str) -> Optional[str]:
    """Read file as text with encoding fallback. Returns None on failure."""
    for enc in ("utf-8", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return None
    return None


def _read_file_lines(filepath: str) -> Optional[List[str]]:
    """Read file lines with encoding fallback. Returns None on failure."""
    for enc in ("utf-8", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.readlines()
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return None
    return None


def _grep_sync(
    pattern: str,
    search_dir: str,
    include: Optional[str],
    output_mode: str,
    context_before: int,
    context_after: int,
    case_insensitive: bool,
    multiline: bool,
    head_limit: int,
    offset: int,
) -> Dict[str, Any]:
    """Synchronous grep implementation — runs in executor thread."""

    # Compile regex
    flags = 0
    if case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL | re.MULTILINE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"Invalid regex pattern: {e}"}

    search_path = Path(search_dir).resolve()

    # Single file mode
    if search_path.is_file():
        files_to_search = [str(search_path)]
        base_dir = str(search_path.parent)
    elif search_path.is_dir():
        files_to_search = None  # will use os.walk
        base_dir = str(search_path)
    else:
        return {"error": f"Search path does not exist: {search_dir}"}

    # Collect ALL results first (pre-pagination), then apply offset + head_limit
    all_matches: List[Any] = []
    total_matches = 0
    files_matched = 0
    output_chars = 0
    hit_output_limit = False

    def _process_file_multiline(filepath: str, rel_path: str) -> bool:
        """Multiline search: match against full file content."""
        nonlocal total_matches, files_matched, output_chars, hit_output_limit

        text = _read_file_text(filepath)
        if text is None:
            return True

        found = list(regex.finditer(text))
        if not found:
            return True

        files_matched += 1
        total_matches += len(found)

        if output_mode == "files_only":
            all_matches.append(rel_path)

        elif output_mode == "count":
            all_matches.append({"file": rel_path, "count": len(found)})

        elif output_mode == "content":
            lines = text.splitlines(keepends=True)
            # Map char offset → line number
            line_offsets: List[int] = []
            pos = 0
            for ln in lines:
                line_offsets.append(pos)
                pos += len(ln)

            def _offset_to_line(char_offset: int) -> int:
                """Binary search for the line containing char_offset."""
                lo, hi = 0, len(line_offsets) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if line_offsets[mid] <= char_offset:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                return hi  # 0-indexed line number

            for m in found:
                line_idx = _offset_to_line(m.start())
                entry: Dict[str, Any] = {
                    "file": rel_path,
                    "line": line_idx + 1,
                    "content": lines[line_idx].rstrip("\n\r") if line_idx < len(lines) else m.group()[:200],
                }
                if context_before > 0:
                    start = max(0, line_idx - context_before)
                    entry["before"] = [l.rstrip("\n\r") for l in lines[start:line_idx]]
                if context_after > 0:
                    end = min(len(lines), line_idx + context_after + 1)
                    entry["after"] = [l.rstrip("\n\r") for l in lines[line_idx + 1:end]]
                all_matches.append(entry)

                output_chars += len(str(entry))
                if output_chars >= _MAX_OUTPUT_CHARS:
                    hit_output_limit = True
                    return False

        return True

    def _process_file_linewise(filepath: str, rel_path: str) -> bool:
        """Standard line-by-line search."""
        nonlocal total_matches, files_matched, output_chars, hit_output_limit

        lines = _read_file_lines(filepath)
        if lines is None:
            return True

        file_match_count = 0
        file_content_matches: List[Dict[str, Any]] = []

        for i, line in enumerate(lines):
            if regex.search(line):
                file_match_count += 1
                total_matches += 1

                if output_mode == "content":
                    entry: Dict[str, Any] = {
                        "file": rel_path,
                        "line": i + 1,
                        "content": line.rstrip("\n\r"),
                    }
                    if context_before > 0:
                        start = max(0, i - context_before)
                        entry["before"] = [l.rstrip("\n\r") for l in lines[start:i]]
                    if context_after > 0:
                        end = min(len(lines), i + context_after + 1)
                        entry["after"] = [l.rstrip("\n\r") for l in lines[i + 1:end]]
                    file_content_matches.append(entry)

                    output_chars += len(str(entry))
                    if output_chars >= _MAX_OUTPUT_CHARS:
                        hit_output_limit = True
                        return False

        if file_match_count > 0:
            files_matched += 1
            if output_mode == "files_only":
                all_matches.append(rel_path)
            elif output_mode == "content":
                all_matches.extend(file_content_matches)
            elif output_mode == "count":
                all_matches.append({"file": rel_path, "count": file_match_count})

        return True

    def _process_file(filepath: str) -> bool:
        """Route to multiline or linewise processing."""
        if _is_binary_ext(filepath):
            return True
        try:
            size = os.path.getsize(filepath)
        except OSError:
            return True
        if size > _MAX_SEARCH_FILE_SIZE:
            return True

        try:
            rel_path = os.path.relpath(filepath, base_dir)
        except ValueError:
            rel_path = filepath
        rel_path = rel_path.replace("\\", "/")

        if multiline:
            return _process_file_multiline(filepath, rel_path)
        else:
            return _process_file_linewise(filepath, rel_path)

    # Process files
    if files_to_search is not None:
        for fp in files_to_search:
            if not _process_file(fp):
                break
    else:
        for dirpath, dirnames, filenames in os.walk(str(search_path)):
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

            for fname in sorted(filenames):
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                filepath = os.path.join(dirpath, fname)
                if not _process_file(filepath):
                    break
            else:
                continue
            break

    # Apply offset + head_limit pagination
    total_before_pagination = len(all_matches)
    paginated = all_matches[offset:offset + head_limit] if head_limit > 0 else all_matches[offset:]

    # Build result
    result: Dict[str, Any] = {
        "matches": paginated,
        "count": len(paginated),
    }

    if output_mode == "count":
        result["total_matches"] = total_matches
        result["files_matched"] = files_matched

    truncated = False
    if hit_output_limit:
        truncated = True
    if total_before_pagination > offset + len(paginated):
        truncated = True

    if truncated:
        result["truncated"] = True
        result["total_before_pagination"] = total_before_pagination
        result["notice"] = (
            f"Results truncated. Showing {len(paginated)} entries "
            f"(offset={offset}, head_limit={head_limit}, "
            f"total_available={total_before_pagination}). "
            "Use offset/head_limit to page through results, or narrow with 'include'."
        )

    return result


class GrepTool(BaseTool):
    """Search file contents by regex pattern."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx=None):
        super().__init__("grep", ctx=ctx)

    async def execute(
        self,
        pattern: str,
        path: Optional[str] = None,
        include: Optional[str] = None,
        output_mode: str = "files_only",
        context_before: Optional[int] = None,
        context_after: Optional[int] = None,
        context_lines: Optional[int] = None,
        case_insensitive: bool = False,
        multiline: bool = False,
        head_limit: int = _DEFAULT_HEAD_LIMIT,
        offset: int = 0,
        **kwargs
    ) -> ToolResult:
        """Search file contents by regex pattern.

        Args:
            pattern:          Python regex pattern to search for.
            path:             File or directory to search. Defaults to '.'.
            include:          Glob pattern to filter files (e.g., '*.py').
            output_mode:      'files_only' (default), 'content', or 'count'.
            context_before:   Lines of context BEFORE each match (content mode).
            context_after:    Lines of context AFTER each match (content mode).
            context_lines:    Shorthand: sets both context_before and context_after.
            case_insensitive: If True, search case-insensitively.
            multiline:        If True, pattern can span multiple lines (re.DOTALL).
            head_limit:       Max entries to return (default 250). 0 = unlimited.
            offset:           Skip first N entries before applying head_limit.
        """
        start_time = time.time()

        try:
            self.validate_params(["pattern"], {"pattern": pattern})

            if output_mode not in ("files_only", "content", "count"):
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Invalid output_mode: '{output_mode}'. Must be 'files_only', 'content', or 'count'.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"pattern": pattern, "path": path, "include": include, "output_mode": output_mode},
                )

            # Resolve context lines: -C is shorthand for both -B and -A
            _ctx_before = context_before if context_before is not None else (context_lines or 0)
            _ctx_after = context_after if context_after is not None else (context_lines or 0)

            # Default search base is the per-session workspace, not the process
            # cwd (no longer mutated via os.chdir — see concurrency work). An
            # explicit relative path is resolved against the workspace too, so
            # search never depends on the process cwd.
            if path:
                search_dir = self.resolve_in_workspace(path)
            else:
                search_dir = self.ctx.working_directory if (self.ctx and self.ctx.working_directory) else "."

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: _grep_sync(
                    pattern, search_dir, include, output_mode,
                    _ctx_before, _ctx_after, case_insensitive,
                    multiline, head_limit, offset,
                ),
            )

            if "error" in result:
                return ToolResult(
                    success=False,
                    output=None,
                    error=result["error"],
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"pattern": pattern, "path": path, "include": include, "output_mode": output_mode},
                )

            # Sidebar hits: one file_touch(kind='hit') per DISTINCT file the
            # scan actually matched. Result shapes vary by output_mode
            # (files_with_matches/count use a `matches` list; content mode
            # groups per-file too) — walk every dict-like entry and pull the
            # `file` key. Deduped so a file matched 20 times is one hit, not
            # 20 orbs of noise. Bounded because a pathological pattern can
            # match thousands of files and each event is a stdout write.
            try:
                matches = result.get("matches") if isinstance(result, dict) else None
                if isinstance(matches, list):
                    seen: set = set()
                    _root = self.ctx.working_directory if (
                        self.ctx and self.ctx.working_directory) else None
                    for m in matches[:150]:
                        rel = None
                        if isinstance(m, dict):
                            rel = m.get("file") or m.get("path")
                        elif isinstance(m, str):
                            rel = m
                        if not rel or rel in seen:
                            continue
                        seen.add(rel)
                        abs_path = rel if os.path.isabs(rel) else (
                            os.path.join(_root, rel) if _root else rel
                        )
                        self.emit_file_touch(abs_path, "hit")
            except Exception:
                pass

            return ToolResult(
                success=True,
                output=result,
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"pattern": pattern, "path": path, "include": include, "output_mode": output_mode},
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Grep failed: {e}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"pattern": pattern, "path": path, "include": include, "output_mode": output_mode},
            )
