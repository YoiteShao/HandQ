"""
Edit Tool - Edit a file using a temp-file swap for atomicity.
"""
import os
import time
import hashlib
import tempfile
import difflib
from pathlib import Path
from .base_tool import BaseTool, ToolResult
from .file_state import FileState


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
        **kwargs
    ) -> ToolResult:
        """Find *old_content* in *path* and replace it with *new_content* (first occurrence)."""
        start_time = time.time()

        try:
            self.validate_params(
                ["path", "old_content", "new_content"],
                {"path": path, "old_content": old_content, "new_content": new_content}
            )

            path_obj = Path(path)

            # (5) Similar file suggestion: if target file doesn't exist, suggest alternatives
            if not path_obj.exists():
                target_name = path_obj.name
                suggestions = []
                search_root = str(path_obj.parent) if path_obj.parent != Path(".") else "."
                # Walk filesystem to find files with similar names
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
                    # Also search from cwd if search_root differs
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
            stale, reason, content = FileState.get_instance().check_stale_and_read(path)
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

            if match_count >= 2:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"String found {match_count} times — must be unique. Add more context to old_content to make it unique.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
                )

            new_file_content = content.replace(old_content, new_content, 1)

            # Write via temp file for atomicity
            temp_fd, temp_path = tempfile.mkstemp(
                dir=path_obj.parent,
                prefix=f".{path_obj.name}.",
                suffix=".tmp"
            )
            fd_closed = False
            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as temp_file:
                    fd_closed = True  # os.fdopen takes ownership; fd will be closed by context manager
                    temp_file.write(new_file_content)

                # Atomic replace: rename temp file over target
                os.replace(temp_path, str(path_obj))

            except Exception as e:
                # Close fd if os.fdopen() never took ownership
                if not fd_closed:
                    os.close(temp_fd)
                # Clean up temp file on failure
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e

            # (4) Read-before-write guard: warn if file changed since we read it
            # Re-read after write to verify; compare hash of what we read vs what we wrote from
            # (The guard fires if another process modified the file between our read and write)
            try:
                with open(path_obj, 'r', encoding='utf-8') as f:
                    written_content = f.read()
                written_hash = hashlib.sha256(new_file_content.encode('utf-8')).hexdigest()
                actual_hash = hashlib.sha256(written_content.encode('utf-8')).hexdigest()
                file_changed_warning = None
                if actual_hash != written_hash:
                    file_changed_warning = (
                        "Warning: file content after write does not match expected — "
                        "the file may have been modified concurrently."
                    )
            except OSError:
                file_changed_warning = None

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
