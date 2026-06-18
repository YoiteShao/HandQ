"""
NotebookEdit Tool - Edit Jupyter notebook (.ipynb) cells.

Supports replace, insert, and delete operations on notebook cells.
.ipynb files are JSON documents; this tool manipulates them directly.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional
from .base_tool import BaseTool, ToolResult


def _load_notebook(path: Path) -> dict:
    """Load and parse a .ipynb file. Raises on error."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_notebook(path: Path, nb: dict) -> None:
    """Write notebook dict back to disk as formatted JSON."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _get_cells(nb: dict) -> list:
    """Get the cells list from a notebook dict."""
    return nb.get("cells", [])


def _make_cell(cell_type: str, source: str) -> dict:
    """Create a new notebook cell with standard structure."""
    cell = {
        "cell_type": cell_type,
        "metadata": {},
        "source": source.splitlines(keepends=True),
    }
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


class NotebookEditTool(BaseTool):
    """Edit Jupyter notebook (.ipynb) cells: replace, insert, or delete."""

    is_read_only = False
    is_concurrency_safe = False

    def __init__(self, ctx=None):
        super().__init__("notebook_edit", ctx=ctx)

    async def execute(
        self,
        notebook_path: str,
        new_source: str = "",
        cell_number: int = 0,
        cell_type: Optional[str] = None,
        edit_mode: str = "replace",
        **kwargs
    ) -> ToolResult:
        """Edit a Jupyter notebook cell.

        Args:
            notebook_path: Path to the .ipynb file (must exist).
            new_source:    New cell content. Ignored for delete mode.
            cell_number:   0-indexed cell number to operate on.
            cell_type:     'code' or 'markdown'. Required for insert;
                           defaults to existing cell type for replace.
            edit_mode:     'replace' (default), 'insert', or 'delete'.
        """
        start_time = time.time()
        params = {
            "notebook_path": notebook_path,
            "new_source": new_source,
            "cell_number": cell_number,
            "cell_type": cell_type,
            "edit_mode": edit_mode,
        }

        try:
            self.validate_params(["notebook_path"], params)

            if edit_mode not in ("replace", "insert", "delete"):
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Invalid edit_mode: '{edit_mode}'. Must be 'replace', 'insert', or 'delete'.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            path_obj = Path(notebook_path)
            if not path_obj.exists():
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Notebook not found: {notebook_path}",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            if path_obj.suffix.lower() != ".ipynb":
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Not a notebook file (must be .ipynb): {notebook_path}",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            loop = asyncio.get_event_loop()

            # Load notebook
            try:
                nb = await loop.run_in_executor(None, lambda: _load_notebook(path_obj))
            except (json.JSONDecodeError, OSError) as e:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Failed to parse notebook: {e}",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            cells = _get_cells(nb)
            total_cells = len(cells)

            # --- DELETE mode ---
            if edit_mode == "delete":
                if cell_number < 0 or cell_number >= total_cells:
                    return ToolResult(
                        success=False,
                        output=None,
                        error=f"cell_number {cell_number} out of range (notebook has {total_cells} cells, 0-indexed).",
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters=params,
                    )
                deleted_type = cells[cell_number].get("cell_type", "unknown")
                del cells[cell_number]
                await loop.run_in_executor(None, lambda: _save_notebook(path_obj, nb))
                return ToolResult(
                    success=True,
                    output={
                        "action": "deleted",
                        "cell_number": cell_number,
                        "cell_type": deleted_type,
                        "total_cells": len(cells),
                        "path": str(path_obj.absolute()),
                    },
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            # --- INSERT mode ---
            if edit_mode == "insert":
                if cell_type is None:
                    return ToolResult(
                        success=False,
                        output=None,
                        error="cell_type is required for insert mode ('code' or 'markdown').",
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters=params,
                    )
                if cell_type not in ("code", "markdown"):
                    return ToolResult(
                        success=False,
                        output=None,
                        error=f"Invalid cell_type: '{cell_type}'. Must be 'code' or 'markdown'.",
                        execution_time=time.time() - start_time,
                        tool_name=self.name,
                        tool_parameters=params,
                    )
                # Insert at position (0 = beginning, total_cells = end)
                insert_at = max(0, min(cell_number, total_cells))
                new_cell = _make_cell(cell_type, new_source)
                cells.insert(insert_at, new_cell)
                await loop.run_in_executor(None, lambda: _save_notebook(path_obj, nb))
                return ToolResult(
                    success=True,
                    output={
                        "action": "inserted",
                        "cell_number": insert_at,
                        "cell_type": cell_type,
                        "total_cells": len(cells),
                        "path": str(path_obj.absolute()),
                    },
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            # --- REPLACE mode (default) ---
            if cell_number < 0 or cell_number >= total_cells:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"cell_number {cell_number} out of range (notebook has {total_cells} cells, 0-indexed).",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            target_cell = cells[cell_number]
            old_type = target_cell.get("cell_type", "code")
            new_type = cell_type if cell_type else old_type

            if new_type not in ("code", "markdown"):
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Invalid cell_type: '{new_type}'. Must be 'code' or 'markdown'.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters=params,
                )

            # Replace content
            target_cell["source"] = new_source.splitlines(keepends=True)
            target_cell["cell_type"] = new_type
            if new_type == "code":
                target_cell.setdefault("execution_count", None)
                target_cell.setdefault("outputs", [])
                # Clear outputs on edit (stale outputs are misleading)
                target_cell["outputs"] = []
                target_cell["execution_count"] = None
            else:
                # Markdown cells don't have outputs/execution_count
                target_cell.pop("outputs", None)
                target_cell.pop("execution_count", None)

            await loop.run_in_executor(None, lambda: _save_notebook(path_obj, nb))
            return ToolResult(
                success=True,
                output={
                    "action": "replaced",
                    "cell_number": cell_number,
                    "cell_type": new_type,
                    "total_cells": total_cells,
                    "path": str(path_obj.absolute()),
                },
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters=params,
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"NotebookEdit failed: {e}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters=params,
            )
