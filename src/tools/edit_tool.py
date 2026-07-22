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
from typing import Optional
from .base_tool import BaseTool, ToolResult
from .file_state import FileState


def _walk_for_suggestions(
    target_name: str, search_root: str, workspace_root: Optional[str] = None,
) -> list:
    """Pure-sync helper used by edit_tool when the target file does not exist.
    Walks the filesystem to find files with similar names; bounded to
    _MAX_WALK_FILES = 500 to avoid wedging on huge trees. Pulled out to a
    module-level helper so it can be run inside loop.run_in_executor.

    The optional broadening walk uses *workspace_root* (the per-session
    working directory), never the process cwd: the bridge no longer
    os.chdir's into the workspace, so a bare ``os.walk(".")`` here would
    scan the install/launch dir and surface unrelated filenames as
    suggestions. When no workspace is known (ctx=None test fixtures) the
    broadening walk is skipped so suggestions never depend on process cwd.
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
        # Broaden the search to the workspace root when the target's own
        # directory under-fills — but only if it's a different tree than the
        # one we just walked, and only when a workspace is known.
        if (workspace_root and len(all_files) < _MAX_WALK_FILES
                and os.path.realpath(workspace_root) != os.path.realpath(search_root)):
            for dirpath, _dirnames, filenames in os.walk(workspace_root):
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

    def __init__(self, ctx=None):
        super().__init__("edit", ctx=ctx)

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

            if not old_content:
                return ToolResult(
                    success=False,
                    output=None,
                    error="old_content must not be empty",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "old_content": old_content, "new_content": new_content},
                )

            # Resolve relative paths against the per-session workspace, not the
            # process cwd. Downstream Path(path), FileState staleness check, and
            # the displayed .absolute() path all use this single resolved value.
            path = self.resolve_in_workspace(path)

            path_obj = Path(path)

            # (5) Similar file suggestion: if target file doesn't exist, suggest alternatives.
            # os.walk on a deep tree can take seconds; off-load to executor.
            if not path_obj.exists():
                target_name = path_obj.name
                search_root = str(path_obj.parent) if path_obj.parent != Path(".") else "."
                workspace_root = (
                    self.ctx.working_directory if (self.ctx and self.ctx.working_directory) else None
                )
                suggestions = await loop.run_in_executor(
                    None,
                    lambda: _walk_for_suggestions(target_name, search_root, workspace_root),
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
            fs = self.ctx.file_state if self.ctx is not None else FileState.get_instance()
            stale, reason, content = await loop.run_in_executor(
                None,
                lambda: fs.check_stale_and_read(path),
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
            used_normalized = False
            working_content = content
            working_old_content = old_content

            if match_count == 0:
                # CRLF friction retry: old_content may be \n-only while the
                # file on disk uses \r\n (or vice versa). Normalize both
                # sides once and recount before falling back to the fuzzy
                # match failure below. Adopting the normalized count even
                # when it's >1 lets the existing "must be unique" check
                # further down produce an accurate error instead of a
                # misleading "not found" for a match that actually exists
                # (just ambiguously).
                normalized_content = content.replace("\r\n", "\n")
                normalized_old = old_content.replace("\r\n", "\n")
                if normalized_content != content or normalized_old != old_content:
                    normalized_count = normalized_content.count(normalized_old)
                    if normalized_count >= 1:
                        match_count = normalized_count
                        used_normalized = True
                        working_content = normalized_content
                        working_old_content = normalized_old

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
                new_file_content = working_content.replace(working_old_content, new_content)
            else:
                new_file_content = working_content.replace(working_old_content, new_content, 1)

            if used_normalized and content != working_content:
                # The original file used CRLF line endings and old_content was
                # matched only after normalizing both to \n — restore CRLF in
                # the written result so the file's line-ending style is
                # preserved. Normalize any \r\n already present in
                # new_file_content (e.g. if new_content itself contained \r\n)
                # before reintroducing it, so this is idempotent rather than
                # doubling up into \r\r\n.
                new_file_content = new_file_content.replace("\r\n", "\n").replace("\n", "\r\n")

            # Checkpoint the PRE-edit state for undo (RewindStore, Tier-1.3).
            # Placed here — after all validation early-returns (not-found,
            # stale, no-match, ambiguous) — so we only snapshot when a mutation
            # is actually about to happen. First-write-wins in the store keeps
            # the earliest pre-item content. No-op without a session store.
            rewind = getattr(self.ctx, "rewind_store", None) if self.ctx else None
            if rewind is not None:
                await loop.run_in_executor(None, lambda: rewind.capture_before(path))

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
            if len(diff_preview) > 5_000:
                diff_preview = diff_preview[:5_000] + "\n... (diff truncated at 5000 chars; full edit applied)"

            # Update FileState so subsequent edits in the same session don't trigger stale warning
            new_hash = hashlib.sha256(new_file_content.encode("utf-8")).hexdigest()
            fs = self.ctx.file_state if self.ctx is not None else FileState.get_instance()
            fs.record_read(path, new_hash)

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

            # Live file-touch event → session sidebar (nebula + change list).
            self.emit_file_touch(str(path_obj.absolute()), "edit")

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
