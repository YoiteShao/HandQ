"""
Read Tool - Read file or directory contents; supports multiple paths in one call.
"""
import asyncio
import hashlib
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Union, List, Optional

import pdfplumber  # type: ignore[import-not-found]

from .base_tool import BaseTool, ToolResult
from .file_state import FileState


# Hard file-size limit for the 100000-char large-file truncation path
_MAX_FILE_CHARS = 50_000
_TRUNCATE_LINES = 200
_MAX_PDF_PAGES_PER_REQUEST = 20

# Default page size when neither offset/limit nor start_line/end_line is given.
# The caller paginates by passing offset/limit explicitly when a wider window
# is needed. Mirrors the Claude Code Read-tool defaults.
_DEFAULT_LIMIT = 2000

# Multi-path aggregate cap: when a single read() call returns several files,
# we soft-cap the total rendered content. The file that crosses the threshold
# is included in full; every subsequent path is returned as a `file_skipped`
# stub (path + size only, no I/O). The agent can re-read interesting paths
# individually for full content.
_MAX_MULTI_TOTAL_CHARS = 40_000

# Known binary file extensions — checked before reading
_BINARY_EXTENSIONS = frozenset([
    ".exe", ".bin", ".so", ".dylib", ".dll", ".obj", ".o", ".a",
    ".pyc", ".pyd", ".class", ".jar", ".zip", ".tar", ".gz",
    ".bz2", ".xz", ".7z", ".rar", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".bmp", ".ico", ".tiff", ".mp3", ".mp4",
    ".avi", ".mov", ".mkv", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".db", ".sqlite", ".wasm",
])


def _add_line_numbers(lines: list, start: int = 1) -> str:
    """Prefix each line with its 1-based line number, right-aligned to 4 digits."""
    end = start + len(lines) - 1
    width = max(4, len(str(end)))
    parts = []
    for i, line in enumerate(lines):
        parts.append(f"{start + i:>{width}}\u2192 {line}")
    return "".join(parts)


def _format_dir_listing(path_obj: Path) -> str:
    """Return an ls -la style listing for a directory."""
    lines = [f"total {sum(1 for _ in path_obj.iterdir())}"]
    try:
        entries = sorted(path_obj.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    except PermissionError:
        return "Permission denied"
    for entry in entries:
        try:
            st = entry.stat()
            mode = stat.filemode(st.st_mode)
            nlink = st.st_nlink
            size = st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%b %d %H:%M")
            name = entry.name + ("/" if entry.is_dir() else "")
            lines.append(f"{mode} {nlink:>3} {size:>10}  {mtime}  {name}")
        except (PermissionError, OSError):
            lines.append(f"{'?':10}  {'?':>10}  {'?':>12}  {entry.name}")
    return "\n".join(lines)


def _read_bytes_sample(path_obj: Path, n: int = 512) -> bytes:
    with open(path_obj, "rb") as f:
        return f.read(n)


def _make_skipped_stub(path_str: str) -> dict:
    """Return a `file_skipped` stub for the multi-path aggregate-cap path.

    *path_str* must already be workspace-resolved (absolute): this helper has
    no ``ctx`` of its own, so ``.exists()``/``.stat()``/``.absolute()`` below
    would otherwise resolve against the process cwd — wrong now that the bridge
    no longer os.chdir's into the workspace. The caller passes
    ``resolve_in_workspace(p)``.

    No content is read — just stat() for size if reachable. Used to keep the
    aggregate response bounded while still telling the agent which paths were
    requested but not rendered.
    """
    path_obj = Path(path_str)
    size: Optional[int] = None
    try:
        if path_obj.exists():
            size = path_obj.stat().st_size
    except OSError:
        pass
    return {
        "type": "file_skipped",
        "path": str(path_obj.absolute()),
        "size": size,
        "skipped_for_aggregate": True,
        "notice": (
            "Skipped: aggregate cap reached for this multi-path read. "
            "Re-read this path individually for full content."
        ),
    }


def _is_binary(path_obj: Path) -> bool:
    """Return True if file is likely binary (by extension or null bytes in first 512 bytes)."""
    if path_obj.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    try:
        sample = _read_bytes_sample(path_obj)
        return b"\x00" in sample
    except OSError:
        return False


def _read_pdf(path_obj: Path, pages: Optional[str] = None) -> dict:
    """Read a PDF file and extract text content.

    Tries available PDF libraries in order:
      1. PyPDF2 (most common)
      2. pdfplumber (better text extraction)
      3. pymupdf / fitz (fastest)

    Args:
        path_obj: Path to the PDF file.
        pages:    Optional page range string (e.g., "1-5", "3", "10-20").
                  If None, reads all pages (max 20).

    Returns:
        dict with success/output or success=False/error.
    """
    # Parse page range
    start_page = 0
    end_page = None  # None means "all"

    if pages:
        try:
            if "-" in pages:
                parts = pages.split("-", 1)
                start_page = int(parts[0]) - 1  # 0-indexed
                end_page = int(parts[1])  # exclusive upper bound (1-indexed end → exclusive)
            else:
                start_page = int(pages) - 1
                end_page = start_page + 1
        except (ValueError, IndexError):
            return {"success": False, "error": f"Invalid pages format: '{pages}'. Use '1-5', '3', or '10-20'."}

    # pdfplumber — single canonical PDF parser. Imported eagerly at module load
    # so missing dep fails at startup rather than at first PDF read.
    try:
        with pdfplumber.open(path_obj) as pdf:
            total_pages = len(pdf.pages)

            if end_page is None:
                end_page = min(total_pages, start_page + _MAX_PDF_PAGES_PER_REQUEST)
            end_page = min(end_page, total_pages)

            if total_pages > _MAX_PDF_PAGES_PER_REQUEST and pages is None:
                return {
                    "success": False,
                    "error": (
                        f"PDF has {total_pages} pages (max {_MAX_PDF_PAGES_PER_REQUEST} per request). "
                        f"Provide the 'pages' parameter (e.g., pages='1-10')."
                    )
                }

            text_parts = []
            for i in range(start_page, end_page):
                page = pdf.pages[i]
                text = page.extract_text() or ""
                text_parts.append(f"--- Page {i + 1} ---\n{text}")

            content = "\n\n".join(text_parts)
            return {
                "success": True,
                "output": {
                    "type": "pdf",
                    "path": str(path_obj.absolute()),
                    "content": content,
                    "total_pages": total_pages,
                    "pages_read": f"{start_page + 1}-{end_page}",
                    "size": path_obj.stat().st_size,
                }
            }
    except Exception as exc:
        return {
            "success": False,
            "error": f"PDF read failed via pdfplumber: {exc}",
        }


class ReadTool(BaseTool):
    """Read one or more files or directories."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx=None):
        super().__init__("read", ctx=ctx)

    def _read_single_path(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        pages: Optional[str] = None,
    ) -> dict:
        """Read a single file or directory; return a result dict.

        Pagination semantics for files (priority order):
          1. Explicit offset/limit                — preferred
          2. Legacy start_line/end_line aliases   — kept for backward-compat
          3. Default                              — offset=1, limit=_DEFAULT_LIMIT
        """
        # Resolve relative paths against the per-session workspace (not process
        # cwd) so the FileState read record is keyed identically to the absolute
        # path a later write/edit will check against.
        path = self.resolve_in_workspace(path)
        path_obj = Path(path)

        if not path_obj.exists():
            return {
                "success": False,
                "path": path,
                "error": f"Path does not exist: {path}"
            }

        # --- Directory ---
        if path_obj.is_dir():
            listing = _format_dir_listing(path_obj)
            items = []
            try:
                for item in sorted(path_obj.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                    try:
                        st = item.stat()
                        items.append({
                            "name": item.name,
                            "type": "directory" if item.is_dir() else "file",
                            "path": str(item.absolute()),
                            "size": st.st_size,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        })
                    except OSError:
                        items.append({
                            "name": item.name,
                            "type": "directory" if item.is_dir() else "file",
                            "path": str(item.absolute()),
                        })
            except PermissionError:
                pass

            return {
                "success": True,
                "output": {
                    "type": "directory",
                    "path": str(path_obj.absolute()),
                    "listing": listing,
                    "items": items,
                    "count": len(items),
                }
            }

        # --- File ---
        if not path_obj.is_file():
            return {
                "success": False,
                "path": path,
                "error": f"Unsupported path type: {path}"
            }

        file_size = path_obj.stat().st_size

        # --- PDF files: special handling ---
        if path_obj.suffix.lower() == ".pdf":
            return _read_pdf(path_obj, pages=pages)

        # Binary detection (extension + null bytes)
        if _is_binary(path_obj):
            return {
                "success": True,
                "output": {
                    "type": "binary_file",
                    "path": str(path_obj.absolute()),
                    "size": file_size,
                    "message": f"Binary file detected ({file_size} bytes). Cannot display as text.",
                }
            }

        # Read raw bytes once, then decode. Avoids a second read for SHA
        # later (which both wastes IO and opens a TOCTOU window where the
        # file changes between the read and the hash, leaving FileState
        # with a hash that doesn't match the bytes the agent saw).
        try:
            with open(path_obj, "rb") as _fh:
                _raw_bytes = _fh.read()
        except OSError as e:
            return {"success": False, "path": path, "error": str(e)}

        content = None
        encoding_used = None
        for enc in ("utf-8", "latin-1"):
            try:
                content = _raw_bytes.decode(enc)
                encoding_used = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is None:
            # Binary fallback: hex summary
            hex_preview = _raw_bytes[:256].hex(" ", 1)
            return {
                "success": True,
                "output": {
                    "type": "binary_file",
                    "path": str(path_obj.absolute()),
                    "size": file_size,
                    "message": (
                        f"Binary file detected ({file_size} bytes). Cannot display as text.\n"
                        f"First 256 bytes (hex): {hex_preview}"
                    ),
                }
            }

        all_lines = content.splitlines(keepends=True)
        total_lines = len(all_lines)

        # --- Resolve effective (offset, limit) ---
        if offset is not None or limit is not None:
            eff_offset = offset if (offset is not None and offset >= 1) else 1
            eff_limit = limit if (limit is not None and limit >= 1) else _DEFAULT_LIMIT
        elif start_line is not None or end_line is not None:
            eff_offset = start_line if (start_line is not None and start_line >= 1) else 1
            eff_end = end_line if (end_line is not None and end_line >= eff_offset) else total_lines
            eff_limit = max(1, eff_end - eff_offset + 1)
        else:
            eff_offset = 1
            eff_limit = _DEFAULT_LIMIT

        sl = eff_offset - 1                  # 0-based start
        el = sl + eff_limit                  # exclusive end
        selected = all_lines[sl:el]

        # --- Char safety cap on selected range ---
        # Even with a small line limit, pathologically long lines can blow up
        # the response. Trim further to fit _MAX_FILE_CHARS and flag it.
        char_truncated = False
        if sum(len(l) for l in selected) > _MAX_FILE_CHARS:
            char_truncated = True
            running = 0
            cut_at = 0
            for i, line in enumerate(selected):
                if running + len(line) > _MAX_FILE_CHARS:
                    cut_at = i
                    break
                running += len(line)
            selected = selected[:cut_at]

        lines_returned = len(selected)
        last_returned = sl + lines_returned  # 1-based last line index actually returned
        more_after = last_returned < total_lines or char_truncated

        numbered = _add_line_numbers(selected, start=sl + 1)
        fs = self.ctx.file_state if self.ctx is not None else FileState.get_instance()
        fs.record_read(path, hashlib.sha256(_raw_bytes).hexdigest())

        output: dict = {
            "type": "file",
            "path": str(path_obj.absolute()),
            "content": numbered,
            "size": file_size,
            "total_lines": total_lines,
            "offset": eff_offset,
            "limit": eff_limit,
            "lines_returned": lines_returned,
            "encoding": encoding_used,
        }
        if more_after:
            output["truncated"] = True
            if char_truncated:
                output["notice"] = (
                    f"Range exceeded {_MAX_FILE_CHARS}-char safety cap: returned lines "
                    f"{eff_offset}-{last_returned} of {total_lines} total. "
                    f"Pass a smaller `limit` or a different `offset` to read more."
                )
            else:
                next_offset = last_returned + 1
                output["notice"] = (
                    f"Showing lines {eff_offset}-{last_returned} of {total_lines} total. "
                    f"Pass offset={next_offset} (with same or different limit) to read the next chunk."
                )
        return {"success": True, "output": output}

    async def execute(
        self,
        path: Union[str, List[str], None] = None,
        paths: Union[List[str], None] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        pages: Optional[str] = None,
        **kwargs
    ) -> ToolResult:
        """
        Read one or more files or directories.

        Args:
            path:       A single path (str) or list of paths; combined with *paths*.
            paths:      Additional list of paths; duplicates across *path* and *paths* are ignored.
            offset:     1-indexed first line to return (inclusive). Default 1.
            limit:      Number of lines to return starting at *offset*. Default
                        _DEFAULT_LIMIT (2000); pass a larger limit explicitly to
                        get more, or paginate with offset+limit.
            start_line: Legacy alias — first line (inclusive). Prefer `offset`.
            end_line:   Legacy alias — last line (inclusive). Prefer `limit`.
            pages:      For PDF files: page range (e.g., "1-5", "3", "10-20").
                        Required for PDFs with more than 20 pages.

        Returns:
            ToolResult:
                - Single path: result returned directly.
                - Multiple paths: output is
                  {"results": [...], "total": n, "succeeded": n, "failed": n}
        """
        start_time = time.time()
        tp = {
            "path": path, "paths": paths,
            "offset": offset, "limit": limit,
            "start_line": start_line, "end_line": end_line,
        }

        try:
            # Reject mixing the new and legacy parameter pairs in the same call —
            # otherwise their priority rule becomes hidden state for the caller.
            if (offset is not None or limit is not None) and (
                start_line is not None or end_line is not None
            ):
                return ToolResult(
                    success=False,
                    output=None,
                    error="Use either offset/limit OR start_line/end_line, not both.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=tp,
                )

            # Collect all paths from both 'path' and 'paths', deduplicating while preserving order
            all_paths: List[str] = []
            seen = set()

            def _add(p: str):
                if p not in seen:
                    seen.add(p)
                    all_paths.append(p)

            if path is not None:
                if isinstance(path, list):
                    for p in path:
                        _add(str(p))
                else:
                    _add(str(path))

            if paths is not None:
                for p in paths:
                    _add(str(p))

            if not all_paths:
                return ToolResult(
                    success=False,
                    output=None,
                    error="No path provided. Please supply 'path' or 'paths'.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=tp,
                )

            # Single path: return result directly
            if len(all_paths) == 1:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._read_single_path(
                        all_paths[0],
                        offset=offset, limit=limit,
                        start_line=start_line, end_line=end_line,
                        pages=pages,
                    ),
                )
                if result["success"]:
                    return ToolResult(
                        success=True,
                        output=result["output"],
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters=tp,
                    )
                else:
                    return ToolResult(
                        success=False,
                        output=None,
                        error=result["error"],
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters=tp,
                    )

            # Multiple paths: explicit ranges are ambiguous across files, reject them.
            # Each file still gets the default _DEFAULT_LIMIT cap inside _read_single_path.
            if (
                offset is not None or limit is not None
                or start_line is not None or end_line is not None
            ):
                return ToolResult(
                    success=False,
                    output=None,
                    error="offset/limit/start_line/end_line are not supported for multi-path reads. Read each file individually.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=tp,
                )

            loop = asyncio.get_event_loop()
            results = []
            running_total = 0
            aggregate_cap_hit_at: Optional[int] = None  # 1-based file index where cap kicked in
            for idx, p in enumerate(all_paths):
                # Once the cap has been crossed on a previous iteration, skip
                # the I/O entirely and return a stub for the remaining paths.
                if aggregate_cap_hit_at is not None:
                    results.append({
                        "path": p,
                        "success": True,
                        "data": _make_skipped_stub(self.resolve_in_workspace(p)),
                    })
                    continue

                r = await loop.run_in_executor(
                    None,
                    lambda p=p: self._read_single_path(p, pages=pages),
                )
                if r["success"]:
                    data = r["output"]
                    results.append({
                        "path": p,
                        "success": True,
                        "data": data,
                    })
                    if isinstance(data, dict):
                        # `content` (file/pdf) and `listing` (directory) are the
                        # main contributors to message-history weight.
                        running_total += len(data.get("content") or "")
                        running_total += len(data.get("listing") or "")
                    if running_total > _MAX_MULTI_TOTAL_CHARS:
                        # Mark cap hit; the NEXT file (idx + 1, 1-based: idx + 2) is the first skipped.
                        aggregate_cap_hit_at = idx + 2
                else:
                    results.append({
                        "path": p,
                        "success": False,
                        "error": r.get("error", "Unknown error")
                    })

            succeeded = sum(1 for r in results if r["success"])
            failed = len(results) - succeeded

            output_payload: dict = {
                "results": results,
                "total": len(results),
                "succeeded": succeeded,
                "failed": failed,
            }
            if aggregate_cap_hit_at is not None and aggregate_cap_hit_at <= len(all_paths):
                skipped = len(all_paths) - aggregate_cap_hit_at + 1
                output_payload["aggregate_truncated"] = True
                output_payload["aggregate_truncation_start"] = aggregate_cap_hit_at
                output_payload["notice"] = (
                    f"Multi-path aggregate cap reached: total content exceeded "
                    f"{_MAX_MULTI_TOTAL_CHARS} chars after file #{aggregate_cap_hit_at - 1}. "
                    f"{skipped} remaining path(s) were skipped (file_skipped stubs). "
                    f"Re-read individual paths for full content."
                )

            return ToolResult(
                success=failed == 0,  # True only if all paths succeeded
                output=output_payload,
                error=None if failed == 0 else f"{failed} path(s) failed to read",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters=tp,
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Read fail: {str(e)}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters=tp,
            )
