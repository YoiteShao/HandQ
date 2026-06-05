"""
Edit Tool - Edit a file using a temp-file swap for atomicity.
"""
import asyncio
import os
import time
import hashlib
import tempfile
import difflib
from pathlib import Path
from .base_tool import BaseTool, ToolResult
from .file_state import FileState


def _walk_for_suggestions(target_name: str, search_root: str) -> list:
    """Pure-sync helper used by edit_tool when the target file does not exist.
    Walks the filesystem to find files with similar names; bounded to
    _MAX_WALK_FILES = 500 to avoid wedging on huge trees. Pulled out to a
    module-level helper so it can be run inside loop.run_in_executor.
    """
    suggestions = []
    try:
        all_files = []
        _MAX_WALK_FILES = 500
        for dirpath, _dirnames, filenames in os.walk(search_root):
            for fname in filenames:
                all_files.append(os.path.join(dirpath, fname))
                if len(all_files) >= _MAX_WALK_FILES:
                    break
            if len(all_files) >= _MAX_WALK_FILES:
                break
        if search_root != "." and len(all_files) < _MAX_WALK_FILES:
            for dirpath, _dirnames, filenames in os.walk("."):
                for fname in filenames:
                    fpath = os.path.join(dirpath, fname)
                    if fpath not in all_files:
                        all_files.append(fpath)
                        if len(all_files) >= _MAX_WALK_FILES:
                            break
                if len(all_files) >= _MAX_WALK_FILES:
                    break
        all_names = [os.path.basename(f) for f in all_files]
        close_names = difflib.get_close_matches(target_name, all_names, n=5, cutoff=0.6)
        for close_name in close_names:
            for fpath in all_files:
                if os.path.basename(fpath) == close_name:
                    suggestions.append(fpath)
                    break
    except OSError:
        pass
    return suggestions


def _swap_file_contents(parent_dir: Path, target_basename: str,
                       target_path: str, content: str) -> None:
    """Atomically replace target with *content*. Pure sync — runs in executor."""
    temp_fd, temp_path = tempfile.mkstemp(
        dir=parent_dir,
        prefix=f".{target_basename}.",
        suffix=".tmp",
    )
    fd_closed = False
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8', newline='') as temp_file:
            fd_closed = True  # os.fdopen takes ownership
            temp_file.write(content)
        os.replace(temp_path, target_path)
    except Exception:
        if not fd_closed:
            try:
                os.close(temp_fd)
            except Exception:
                pass
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise


def _read_text_safe(path: Path) -> "Optional[str]":
    """Read text content, returning None on any I/O error. Sync — runs in executor."""
    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            return f.read()
    except OSError:
        return None


class EditTool(BaseTool):
    """Edit file content (find-and-replace) using a temp-file swap for atomicity."""

    is_read_only = False
    is_concurrency_safe = False

    def __init__(self):
        super().__init__("edit")

    async def execute(
        self,
        path: str,
        old_content: str,
        new_content: str,
        replace_all: bool = False,
        **kwargs
    ) -> ToolResult:
        """Find *old_content* in *path* and replace with *new_content*.

        When *replace_all* is False (default), replaces the first unique
        occurrence only — rejects if multiple matches exist.
        When *replace_all* is True, replaces ALL occurrences.
        """
        start_time = time.time()
        # All blocking file I/O routes through this loop's default executor
        # so the asyncio loop stays alive on slow filesystems. Each
        # individual call is a self-contained sync block that can run on
        # an executor thread without coordination with the main thread.
        loop = asyncio.get_event_loop()

        try:
            self.validate_params(
                ["path", "old_content", "new_content"],
                {"path": path, "old_content": old_content, "new_content": new_content, "replace_all": replace_all}
            )

            path_obj = Path(path)

            # (5) Similar file suggestion: if target file doesn't exist, suggest alternatives.
            # os.walk on a deep tree can take seconds; off-load to executor.
            if not path_obj.exists():
                target_name = path_obj.name
                search_root = str(path_obj.parent) if path_obj.parent != Path(".") else "."
                suggestions = await loop.run_in_executor(
                    None, lambda: _walk_for_suggestions(target_name, search_root),
                )

                error_msg = f"File not found: {path}"
                if suggestions:
                    error_msg += "\nDid you mean one of these?\n" + "\n".join(f"  {s}" for s in suggestions)
                return ToolResult(
                    success=False,
                    output=None,
                    error=error_msg,
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
                )

            # Staleness check + read in one atomic open() call — eliminates TOCTOU window
            stale, reason, content = await loop.run_in_executor(
                None,
                lambda: FileState.get_instance().check_stale_and_read(path),
            )
            if stale:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Stale file: {reason}. Re-read the file before editing.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
                )

            # (1) Unique match validation
            match_count = content.count(old_content)
            if match_count == 0:
                # (3) Fuzzy match hint: suggest closest actual string in the file
                # Split content into chunks roughly the size of old_content for comparison
                chunk_size = max(len(old_content), 20)
                # Build candidate substrings by sliding window (sampled to avoid huge files)
                candidates = []
                step = max(1, chunk_size // 4)
                max_candidates = 500
                for i in range(0, max(0, len(content) - chunk_size + 1), step):
                    candidates.append(content[i:i + chunk_size])
                    if len(candidates) >= max_candidates:
                        break
                # Also try line-based candidates for multi-line old_content
                lines = content.splitlines()
                old_lines = old_content.splitlines()
                if len(old_lines) > 1 and len(lines) >= len(old_lines):
                    for i in range(len(lines) - len(old_lines) + 1):
                        candidates.append("\n".join(lines[i:i + len(old_lines)]))

                fuzzy_hint = ""
                if candidates:
                    close = difflib.get_close_matches(old_content, candidates, n=1, cutoff=0.6)
                    if close:
                        fuzzy_hint = f"\nClosest match found in file:\n---\n{close[0]}\n---"

                error_msg = "String not found in file"
                if fuzzy_hint:
                    error_msg += fuzzy_hint
                return ToolResult(
                    success=False,
                    output=None,
                    error=error_msg,
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
                )

            if match_count >= 2 and not replace_all:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"String found {match_count} times — must be unique. Add more context to old_content to make it unique, or set replace_all=true to replace all occurrences.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
                )

            if replace_all:
                new_file_content = content.replace(old_content, new_content)
            else:
                new_file_content = content.replace(old_content, new_content, 1)

            # Write via temp file for atomicity. The temp-file dance is
            # synchronous and can stall on slow filesystems → executor.
            await loop.run_in_executor(
                None,
                lambda: _swap_file_contents(
                    path_obj.parent, path_obj.name, str(path_obj), new_file_content,
                ),
            )

            # (4) Read-before-write guard: re-read and verify the on-disk content
            # matches what we intended to write. Off-loaded to executor for the
            # same reason as the original read.
            written_content = await loop.run_in_executor(
                None, lambda: _read_text_safe(path_obj),
            )
            file_changed_warning = None
            if written_content is not None:
                written_hash = hashlib.sha256(new_file_content.encode('utf-8')).hexdigest()
                actual_hash = hashlib.sha256(written_content.encode('utf-8')).hexdigest()
                if actual_hash != written_hash:
                    file_changed_warning = (
                        "Warning: file content after write does not match expected — "
                        "the file may have been modified concurrently."
                    )

            # (2) Diff preview: generate unified diff of what changed
            old_lines = content.splitlines(keepends=True)
            new_lines = new_file_content.splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path_obj.name}",
                tofile=f"b/{path_obj.name}",
                lineterm=""
            ))
            diff_preview = "".join(diff_lines) if diff_lines else "(no diff — content identical)"

            # Update FileState so subsequent edits in the same session don't trigger stale warning
            new_hash = hashlib.sha256(new_file_content.encode("utf-8")).hexdigest()
            FileState.get_instance().record_read(path, new_hash)

            output = {
                "path": str(path_obj.absolute()),
                "old_size": len(content),
                "new_size": len(new_file_content),
                "message": "File edited successfully",
                "diff": diff_preview,
            }
            if replace_all:
                output["replacements_made"] = match_count
            if file_changed_warning:
                output["warning"] = file_changed_warning

            return ToolResult(
                success=True,
                output=output,
                execution_time=time.time() - start_time,
                diff_output=diff_preview,
                tool_name=self.name,
                tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Edit failed: {str(e)}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
            )
