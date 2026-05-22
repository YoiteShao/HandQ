"""Role-to-model resolution shared between CLI (handq.py / handq_win.py) and bridge (stdio_bridge.py).

External (YAML, UI) role names: planner / receptionist / agent / helper.
Internal (FlowController, Planner, RuntimeAgent) role name for `helper` is `from_data`,
preserved here to avoid touching every call site under src/.

Two config shapes are accepted by `resolve_role_models`:

  Modern (preferred):
    llm:
      roles:
        planner:      [...]
        receptionist: [...]
        agent:        [...]
        helper:       [...]

  Legacy (auto-derived in memory; written back as `roles` on next Save in the UI):
    llm:
      models: [...]
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List

logger = logging.getLogger("HandQ")

# Claude 4-5 and above are planner-capable. Below that the planner JSON-schema
# adherence is too unreliable to be the default.
PLANNER_MIN_VERSION = (4, 5)

# Internal role names. `helper` (UI/YAML) maps to `from_data` (Python).
_EXTERNAL_TO_INTERNAL = {
    "planner":      "planner",
    "receptionist": "receptionist",
    "agent":        "agent",
    "helper":       "from_data",
}
_INTERNAL_ROLES = ("planner", "receptionist", "agent", "from_data")


def model_version(model_str: str) -> tuple:
    """Return (major, minor) version tuple from a model string.

    Examples:
        "anthropic::claude-4-6-sonnet:1M" -> (4, 6)
        "anthropic::claude-4-5-haiku:thinking" -> (4, 5)
        "anthropic::claude-4-sonnet"       -> (4, 0)
        "anthropic::claude-3-7-sonnet"     -> (3, 7)
    """
    name = model_str.split("::")[-1].split(":")[0]
    if m := re.search(r"claude-(\d+)-(\d+)-", name):
        return int(m.group(1)), int(m.group(2))
    if m := re.search(r"claude-(\d+)-[a-z]", name):
        return int(m.group(1)), 0
    return (0, 0)


def assign_roles(all_models: List[str]) -> Dict[str, List[str]]:
    """Distribute one master priority list into the four internal roles.

    Used as the legacy auto-assignment when a config file still carries the
    old `llm.models` shape, and as the JS-mirrored heuristic behind the UI's
    "Distribute to roles" button.

    Two schemes based on whether opus models are present:
      Opus scheme  — receptionist skips all opus models; from_data skips opus + top-2 sonnet.
      Sonnet scheme — receptionist skips index 0; from_data skips indices 0-1.

    Returns dict with internal keys: agent, planner, receptionist, from_data.
    """
    capable = [m for m in all_models if model_version(m) >= PLANNER_MIN_VERSION]
    if not capable:
        import warnings
        warnings.warn(
            "No planner-capable models (Claude 4-5+) found. "
            "All roles will use the full model list.",
            UserWarning,
        )
        return dict(
            agent=list(all_models),
            planner=list(all_models),
            receptionist=list(all_models),
            from_data=list(all_models),
        )

    n = len(capable)
    opus_n = sum(1 for m in capable if "opus" in m)

    if opus_n:
        recep_skip = min(opus_n,     n - 1)
        fdata_skip = min(opus_n + 2, n - 1)
    else:
        recep_skip = min(1, n - 1)
        fdata_skip = min(2, n - 1)

    return dict(
        agent=list(all_models),
        planner=list(capable),
        receptionist=list(capable[recep_skip:]),
        from_data=list(capable[fdata_skip:]),
    )


def resolve_role_models(llm_cfg: dict) -> Dict[str, List[str]]:
    """Return per-role model lists (internal keys) from an `llm:` config block.

    Resolution order:
      1. `llm.roles` dict — used as-is. External `helper` key maps to internal
         `from_data`. Missing/empty role values yield `[]` for that role and
         emit a warning.
      2. Legacy `llm.models` list — passed through `assign_roles` to derive
         the four lists; emits a one-time INFO so operators know the YAML
         will be migrated on next Save in the Electron UI.
      3. Neither — all four roles return `[]`.
    """
    roles_cfg = llm_cfg.get("roles") if isinstance(llm_cfg, dict) else None
    if isinstance(roles_cfg, dict):
        out: Dict[str, List[str]] = {role: [] for role in _INTERNAL_ROLES}
        for ext_name, internal in _EXTERNAL_TO_INTERNAL.items():
            value = roles_cfg.get(ext_name)
            if isinstance(value, list) and value:
                out[internal] = [str(m) for m in value]
            else:
                logger.warning(
                    "llm.roles.%s missing or empty — that role will have no LLM service",
                    ext_name,
                )
        return out

    legacy_models = llm_cfg.get("models") if isinstance(llm_cfg, dict) else None
    if isinstance(legacy_models, list) and legacy_models:
        logger.info(
            "Legacy llm.models detected — deriving per-role lists via assign_roles "
            "(config will be migrated to llm.roles on next Save in the UI)"
        )
        return assign_roles([str(m) for m in legacy_models])

    return {role: [] for role in _INTERNAL_ROLES}


def used_legacy_models(llm_cfg: dict) -> bool:
    """Cheap predicate for cmd_models / status output to flag legacy configs."""
    if not isinstance(llm_cfg, dict):
        return False
    return ("roles" not in llm_cfg) and bool(llm_cfg.get("models"))
