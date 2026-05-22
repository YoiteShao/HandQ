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
from .base_tool import BaseTool, ToolResult
from .file_state import FileState


# Hard file-size limit for the 100000-char large-file truncation path
_MAX_FILE_CHARS = 100_000
_TRUNCATE_LINES = 200
_MAX_PDF_PAGES_PER_REQUEST = 20

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

    # Try PyPDF2
    try:
        import PyPDF2
        with open(path_obj, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            total_pages = len(reader.pages)

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
                page = reader.pages[i]
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
    except ImportError:
        pass
    except Exception as e:
        # PyPDF2 failed for non-import reason — try next library
        pass

    # Try pdfplumber
    try:
        import pdfplumber
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
    except ImportError:
        pass
    except Exception:
        pass

    # Try pymupdf (fitz)
    try:
        import fitz  # pymupdf
        doc = fitz.open(str(path_obj))
        total_pages = doc.page_count

        if end_page is None:
            end_page = min(total_pages, start_page + _MAX_PDF_PAGES_PER_REQUEST)
        end_page = min(end_page, total_pages)

        if total_pages > _MAX_PDF_PAGES_PER_REQUEST and pages is None:
            doc.close()
            return {
                "success": False,
                "error": (
                    f"PDF has {total_pages} pages (max {_MAX_PDF_PAGES_PER_REQUEST} per request). "
                    f"Provide the 'pages' parameter (e.g., pages='1-10')."
                )
            }

        text_parts = []
        for i in range(start_page, end_page):
            page = doc[i]
            text = page.get_text() or ""
            text_parts.append(f"--- Page {i + 1} ---\n{text}")
        doc.close()

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
    except ImportError:
        pass
    except Exception:
        pass

    # No PDF library available
    return {
        "success": False,
        "error": (
            "Cannot read PDF: no supported PDF library installed. "
            "Install one of: PyPDF2, pdfplumber, or pymupdf (fitz). "
            f"Example: pip install PyPDF2"
        )
    }


class ReadTool(BaseTool):
    """Read one or more files or directories."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self):
        super().__init__("read")

    def _read_single_path(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        pages: Optional[str] = None,
    ) -> dict:
        """Read a single file or directory; return a result dict."""
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

        # Encoding fallback: utf-8 → latin-1 → hex summary
        content = None
        encoding_used = None
        for enc in ("utf-8", "latin-1"):
            try:
                with open(path_obj, "r", encoding=enc) as f:
                    content = f.read()
                encoding_used = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is None:
            # Binary fallback: hex summary
            try:
                raw = _read_bytes_sample(path_obj, 256)
                hex_preview = raw.hex(" ", 1)
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
            except OSError as e:
                return {"success": False, "path": path, "error": str(e)}

        all_lines = content.splitlines(keepends=True)
        total_lines = len(all_lines)

        # --- Line range selection ---
        if start_line is not None or end_line is not None:
            sl = (start_line - 1) if start_line is not None and start_line >= 1 else 0
            el = end_line if end_line else total_lines
            selected = all_lines[sl:el]
            numbered = _add_line_numbers(selected, start=sl + 1)
            with open(path_obj, "rb") as _fh:
                _raw = _fh.read()
            FileState.get_instance().record_read(path, hashlib.sha256(_raw).hexdigest())
            return {
                "success": True,
                "output": {
                    "type": "file",
                    "path": str(path_obj.absolute()),
                    "content": numbered,
                    "size": file_size,
                    "total_lines": total_lines,
                    "start_line": sl + 1,
                    "end_line": min(el, total_lines),
                    "encoding": encoding_used,
                }
            }

        # --- Large file truncation ---
        if len(content) > _MAX_FILE_CHARS:
            first_200 = all_lines[:_TRUNCATE_LINES]
            numbered = _add_line_numbers(first_200, start=1)
            notice = (
                f"File truncated: showing lines 1-{len(first_200)} of {total_lines} total lines. "
                f"Use start_line/end_line to read other sections."
            )
            with open(path_obj, "rb") as _fh:
                _raw = _fh.read()
            FileState.get_instance().record_read(path, hashlib.sha256(_raw).hexdigest())
            return {
                "success": True,
                "output": {
                    "type": "file",
                    "path": str(path_obj.absolute()),
                    "content": numbered,
                    "size": file_size,
                    "total_lines": total_lines,
                    "truncated": True,
                    "notice": notice,
                    "encoding": encoding_used,
                }
            }

        # --- Normal full read ---
        numbered = _add_line_numbers(all_lines, start=1)
        with open(path_obj, "rb") as _fh:
            _raw = _fh.read()
        FileState.get_instance().record_read(path, hashlib.sha256(_raw).hexdigest())
        return {
            "success": True,
            "output": {
                "type": "file",
                "path": str(path_obj.absolute()),
                "content": numbered,
                "size": file_size,
                "total_lines": total_lines,
                "encoding": encoding_used,
            }
        }

    async def execute(
        self,
        path: Union[str, List[str], None] = None,
        paths: Union[List[str], None] = None,
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
            start_line: Optional 1-indexed first line to return (inclusive).
            end_line:   Optional 1-indexed last line to return (inclusive).
            pages:      For PDF files: page range (e.g., "1-5", "3", "10-20").
                        Required for PDFs with more than 20 pages.

        Returns:
            ToolResult:
                - Single path: result returned directly.
                - Multiple paths: output is
                  {"results": [...], "total": n, "succeeded": n, "failed": n}
        """
        start_time = time.time()

        try:
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
                    tool_parameters={"path": path, "paths": paths, "start_line": start_line, "end_line": end_line}
                )

            # Single path: return result directly
            if len(all_paths) == 1:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._read_single_path(
                        all_paths[0], start_line=start_line, end_line=end_line,
                        pages=pages,
                    ),
                )
                if result["success"]:
                    return ToolResult(
                        success=True,
                        output=result["output"],
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters={"path": path, "paths": paths, "start_line": start_line, "end_line": end_line}
                    )
                else:
                    return ToolResult(
                        success=False,
                        output=None,
                        error=result["error"],
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters={"path": path, "paths": paths, "start_line": start_line, "end_line": end_line}
                    )

            # Multiple paths: start_line/end_line are not supported (ambiguous across files)
            if start_line is not None or end_line is not None:
                return ToolResult(
                    success=False,
                    output=None,
                    error="start_line/end_line are not supported for multi-path reads. Read each file individually.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"path": path, "paths": paths, "start_line": start_line, "end_line": end_line}
                )

            loop = asyncio.get_event_loop()
            results = []
            for p in all_paths:
                r = await loop.run_in_executor(
                    None,
                    lambda p=p: self._read_single_path(
                        p, start_line=start_line, end_line=end_line,
                        pages=pages,
                    ),
                )
                if r["success"]:
                    results.append({
                        "path": p,
                        "success": True,
                        "data": r["output"]
                    })
                else:
                    results.append({
                        "path": p,
                        "success": False,
                        "error": r.get("error", "Unknown error")
                    })

            succeeded = sum(1 for r in results if r["success"])
            failed = len(results) - succeeded

            return ToolResult(
                success=failed == 0,  # True only if all paths succeeded
                output={
                    "results": results,
                    "total": len(results),
                    "succeeded": succeeded,
                    "failed": failed
                },
                error=None if failed == 0 else f"{failed} path(s) failed to read",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"path": path, "paths": paths, "start_line": start_line, "end_line": end_line}
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Read fail: {str(e)}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"path": path, "paths": paths, "start_line": start_line, "end_line": end_line}
            )
