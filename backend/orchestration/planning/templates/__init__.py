"""JSON-based template loading and workflow construction.

Templates are JSON files conforming to the DAGDraft schema plus a ``meta``
section. This replaces the old Python template factories with a data-driven
approach: non-developers can author templates by writing JSON, no code needed.

The loader scans one or more directories for ``*.json`` files, indexes them
by ``meta.id`` (or filename stem as fallback), and builds Workflows on demand
by substituting ``{goal}`` into sub_goal strings and delegating to DAGDraft.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..dag_draft import DAGDraft, DEFAULT_BUILDERS, build as dag_draft_build
from ..workflow import Workflow
from ....engine.executor import ExecutorProtocol


class TemplateLoader:
    """Load templates from JSON files in directories, build Workflows on demand.

    Supports multiple directories (builtin + user-supplied) scanned in order.
    Templates are indexed by meta.id; a later directory overrides an earlier one
    with the same id.
    """

    def __init__(
        self,
        *dirs: Path,
        builders: Optional[dict[str, Any]] = None,
    ) -> None:
        self._dirs = list(dirs)
        self._builders = builders or DEFAULT_BUILDERS
        self._cache: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """(Re)scan all template dirs. Later dirs override earlier ones."""
        self._cache.clear()
        for d in self._dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.json")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                meta = doc.get("meta", {})
                tid = meta.get("id", p.stem)
                self._cache[tid] = doc

    def has(self, template_id: str) -> bool:
        return template_id in self._cache

    def build(
        self,
        template_id: str,
        *,
        goal: str,
        executor: ExecutorProtocol,
    ) -> Workflow:
        """Build a Workflow from a JSON template, substituting {goal}."""
        doc = self._cache[template_id]
        text = json.dumps(doc, ensure_ascii=False)
        text = text.replace("{goal}", goal)
        spec = json.loads(text)
        spec.pop("meta", None)
        draft = DAGDraft.from_dict(spec)
        return dag_draft_build(draft, executor=executor, builders=self._builders)

    def list_ids(self) -> list[str]:
        return list(self._cache.keys())

    def get_meta(self, template_id: str) -> dict[str, Any]:
        """Return the meta section of a template, or empty dict."""
        doc = self._cache.get(template_id)
        if doc is None:
            return {}
        return dict(doc.get("meta", {}))
