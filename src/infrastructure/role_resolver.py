"""LLM-pool resolution shared between the bridge and auxiliary callers.

Two pools, expressed in YAML under the ``llm:`` key:

  llm:
    API_KEY: ...
    available_models:       # full pool of model identifiers
      - <model-1>
      - <model-2>
      - ...
    agent_models:           # checked subset — used by the coordinator + agent
      - <model-1>
      - <model-2>
    helper_models:          # checked subset — background/cheap tasks
      - <model-X>

``agent_models`` is the priority-ordered fallback chain that
``FlowControllerV2`` hands to ``call_with_fallback``.
``helper_models`` is what the scheduler inferer + LTM triage / reranker /
retriage use for cheap classification work.

Legacy shapes (flat ``models`` list, ``roles`` dict) are still supported for
backward compatibility but are migrated to the new shape on the next Settings
save in the Electron UI.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

logger = logging.getLogger("HandQ")


def resolve_models_and_helper(llm_cfg: dict) -> Tuple[List[str], List[str]]:
    """Return ``(models, helper_models)`` from an ``llm:`` config block.

    Resolution order:

    0. **New schema**: ``llm.available_models`` + ``llm.agent_models`` +
       ``llm.helper_models`` — subsets of the pool selected via checkboxes.
    1. **Legacy modern shape**: ``llm.models`` + ``llm.helper_models``.
    2. **Legacy roles shape**: ``llm.roles.{agent,planner,...}`` → derived.
    3. **Neither**: both lists empty.
    """
    if not isinstance(llm_cfg, dict):
        return [], []

    # (0) New schema — available_models pool with checked subsets.
    available = llm_cfg.get("available_models")
    if isinstance(available, list) and available:
        pool = [str(m) for m in available if m]
        agent_raw = llm_cfg.get("agent_models")
        if isinstance(agent_raw, list) and agent_raw:
            agent = [str(m) for m in agent_raw if m]
        else:
            agent = [pool[0]]
        helper_raw = llm_cfg.get("helper_models")
        if isinstance(helper_raw, list) and helper_raw:
            helper = [str(m) for m in helper_raw if m]
        else:
            helper = pool[-1:]
        return agent, helper

    # (1) Legacy modern shape — flat `models` + optional `helper_models`.
    models_raw = llm_cfg.get("models")
    helper_raw = llm_cfg.get("helper_models")
    roles_raw = llm_cfg.get("roles")

    if isinstance(models_raw, list) and models_raw:
        models = [str(m) for m in models_raw if m]
        if isinstance(helper_raw, list) and helper_raw:
            helper = [str(m) for m in helper_raw if m]
            return models, helper
        if not isinstance(roles_raw, dict):
            logger.info(
                "llm.helper_models missing — defaulting helper pool to "
                "last entry of llm.models (%s)", models[-1],
            )
            return models, [models[-1]]

    # (2) Legacy roles shape — migrate to two pools.
    if isinstance(roles_raw, dict):
        def _list_of(key: str) -> List[str]:
            v = roles_raw.get(key)
            return [str(m) for m in v if m] if isinstance(v, list) else []

        seen: set = set()
        models: List[str] = []
        for key in ("agent", "planner", "receptionist"):
            for m in _list_of(key):
                if m not in seen:
                    seen.add(m)
                    models.append(m)

        helper = _list_of("helper") or _list_of("from_data")

        if models or helper:
            logger.info(
                "Legacy llm.roles detected — deriving (models, helper_models). "
                "Config will be migrated to new schema on next Save in the UI.",
            )
        if not models and isinstance(models_raw, list) and models_raw:
            models = [str(m) for m in models_raw if m]
        return models, helper

    # (3) Neither — empty.
    return [], []


def used_legacy_roles(llm_cfg: dict) -> bool:
    """True when ``llm.roles`` is still present (would be migrated to the
    flat ``models`` / ``helper_models`` shape on next Save). Cheap predicate
    for the Settings UI to flag stale configs."""
    return isinstance(llm_cfg, dict) and "roles" in llm_cfg
