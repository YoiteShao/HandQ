"""
Write Tool - Write content to a file.
"""
import asyncio
import hashlib
import os
import re
import time
import difflib
import tempfile
from pathlib import Path
from .base_tool import BaseTool, ToolResult
from .file_state import FileState

# Secret detection patterns
_SECRET_PATTERNS = [
    (re.compile(r'AKIA[0-9A-Z]{16}'), "AWS Access Key"),
    (re.compile(r"api_key\s*=\s*['\"][^'\"]{20,}['\"]", re.IGNORECASE), "API Key"),
    (re.compile(r"password\s*=\s*['\"][^'\"]{8,}['\"]", re.IGNORECASE), "Password"),
]


def _detect_secrets(content: str) -> list[str]:
    """Return list of warning strings for any secrets found in content."""
    warnings = []
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(content):
            warnings.append(label)
    return warnings


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + os.replace to prevent partial writes."""
    parent = path.parent
    temp_fd, temp_path = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


class WriteTool(BaseTool):
    """Write content to a file."""

    is_read_only = False
    is_concurrency_safe = False

    def __init__(self, ctx=None):
        super().__init__("write", ctx=ctx)

    async def execute(self, path: str, content: str, append: bool = False, **kwargs) -> ToolResult:
        """Write *content* to *path*, creating parent directories as needed.

        Args:
            path:    Destination file path.
            content: Text to write.
            append:  When True, append to an existing file instead of
                     overwriting it.  Use this to write long content in
                     multiple chunks: first call with append=False (or omit)
                     to create/overwrite the file, then subsequent calls with
                     append=True to add more content.
        """
        start_time = time.time()
        # All blocking file I/O in this method goes through this loop's
        # default executor so the asyncio loop stays alive while we wait
        # on slow filesystems (network shares, AV-scanned files,
        # OneDrive online-only files). The executor thread cannot be
        # killed on Windows; if the surrounding asyncio task is
        # cancelled (new_session), the thread finishes its read on its
        # own and the bridge's generation tag isolates the result.
        loop = asyncio.get_event_loop()

        try:
            self.validate_params(["path", "content"], {"path": path, "content": content})

            # Resolve relative paths against the per-session workspace, not the
            # process cwd (no longer mutated via os.chdir — see concurrency work).
            # FileState keys, file I/O, and the displayed .absolute() path all
            # derive from this single resolved value.
            path = self.resolve_in_workspace(path)

            path_obj = Path(path)

            # Auto-create parent directories
            parent_dir = path_obj.parent
            os.makedirs(parent_dir, exist_ok=True)

            file_exists = path_obj.exists()

            # Staleness check: block write if file has not been read or changed since last read.
            # For append mode use check_stale_and_read so the existing content comes from the
            # same open() call as the hash comparison — no TOCTOU window between check and read.
            # For overwrite mode check_stale suffices (the write does not depend on old content).
            existing_content_from_check: str = ""
            if file_exists:
                if append:
                    fs = self.ctx.file_state if self.ctx is not None else FileState.get_instance()
                    stale, reason, _read_content = await loop.run_in_executor(
                        None,
                        lambda: fs.check_stale_and_read(path),
                    )
                    if stale:
                        return ToolResult(
                            success=False,
                            output=None,
                            error=f"Stale file: {reason}. Re-read the file before writing.",
                            execution_time=time.time() - start_time,
                            tool_name=self.name,
                            tool_parameters={"path": path, "content": content, "append": append},
                        )
                    existing_content_from_check = _read_content or ""
                else:
                    fs = self.ctx.file_state if self.ctx is not None else FileState.get_instance()
                    stale, reason = await loop.run_in_executor(
                        None,
                        lambda: fs.check_stale(path),
                    )
                    if stale:
                        return ToolResult(
                            success=False,
                            output=None,
                            error=f"Stale file: {reason}. Re-read the file before writing.",
                            execution_time=time.time() - start_time,
                            tool_name=self.name,
                            tool_parameters={"path": path, "content": content, "append": append},
                        )

            # Read old content for diff (only when overwriting an existing file)
            old_content = None
            if file_exists and not append:
                def _read_old() -> "Optional[str]":
                    try:
                        with open(path_obj, 'r', encoding='utf-8', newline='') as f:
                            return f.read()
                    except Exception:
                        return None
                old_content = await loop.run_in_executor(None, _read_old)

            # Write
            if append:
                # Use the content already read during the staleness check — no second open().
                combined_content = existing_content_from_check + content
                await loop.run_in_executor(
                    None, lambda: _atomic_write(path_obj, combined_content),
                )
                action = "appended"
                final_content = combined_content
            else:
                await loop.run_in_executor(
                    None, lambda: _atomic_write(path_obj, content),
                )
                action = "wrote"
                final_content = content

            lines_written = final_content.count('\n') + (1 if final_content and not final_content.endswith('\n') else 0)

            # Update FileState so subsequent edits in the same session don't trigger stale warning
            new_hash = hashlib.sha256(final_content.encode("utf-8")).hexdigest()
            fs = self.ctx.file_state if self.ctx is not None else FileState.get_instance()
            fs.record_read(path, new_hash)

            # Build observation
            observation: dict = {
                "path": str(path_obj.absolute()),
                "size": len(final_content),
                "lines_written": lines_written,
                "message": f"Successfully {action} {len(content)} characters ({lines_written} lines total)",
            }

            # Diff display on overwrite
            if old_content is not None:
                diff_lines = list(difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{path_obj.name}",
                    tofile=f"b/{path_obj.name}",
                    lineterm="",
                ))
                if diff_lines:
                    diff_text = "".join(diff_lines)
                    if len(diff_text) > 5_000:
                        diff_text = diff_text[:5_000] + "\n... (diff truncated at 5000 chars; full write applied)"
                    observation["diff"] = diff_text
                else:
                    observation["diff"] = "(no changes)"

            # Secret detection (non-blocking warning)
            secret_hits = _detect_secrets(content)
            if secret_hits:
                observation["warnings"] = (
                    "WARNING: Possible secret(s) detected in written content: "
                    + ", ".join(secret_hits)
                )

            return ToolResult(
                success=True,
                output=observation,
                execution_time=time.time() - start_time,
                lines_written=lines_written,
                diff_output=observation.get("diff"),
                tool_name=self.name,
                tool_parameters={"path": path, "content": content, "append": append},
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Write failed: {str(e)}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"path": path, "content": content, "append": append},
            )
