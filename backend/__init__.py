"""HandQ backend v2.

A rebuilt agent backend that treats the executor as a commodity (single
self-planning loop by default) and invests above it. See docs/ARCHITECTURE.md.

This package REUSES the worthwhile parts of ``src/`` (LLM client, tool
registry, RuntimeAgent, memory) and replaces the FlowController per-step
planner with a router + deterministic workflow layer.
"""

__version__ = "2.0.0-dev"
