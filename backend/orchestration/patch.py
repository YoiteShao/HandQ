"""Compatibility stub — re-exports from extensions/patch.py."""
from ..extensions.patch import *  # noqa: F401,F403
from ..extensions.patch import (
    Patch, Conflict, PatchStager, ApprovalGate,
    make_stage_patch_tool, apply_patches,
)
