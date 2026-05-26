"""
Configuration Manager — Unified YAML loader
Loads and manages all system configuration from a single YAML file.
"""
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class ConfigManager:
    """Configuration manager — loads all settings from handq_config.yaml."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the configuration manager.

        The config file is loaded lazily on first access so that the
        ConfigManager can be constructed before the process working directory
        has been set to the project root.

        Path resolution when ``config_path`` is omitted (the common case for
        tools that build their own ConfigManager): consume ``HANDQ_CONFIG``
        from the environment — bridge_main.py resolves the absolute path on
        boot and writes it there, per ARCHITECTURE.md §1. Fall back to the
        cwd-relative ``handq_config.yaml`` only if the env var is unset (e.g.
        unit tests that import ConfigManager standalone).

        Args:
            config_path: Explicit path to handq_config.yaml. ``None`` defers
                to ``HANDQ_CONFIG`` env, then to the cwd-relative default.
        """
        if config_path is None:
            config_path = os.environ.get("HANDQ_CONFIG") or "handq_config.yaml"
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Internal loader
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load the config file if it has not been loaded yet (lazy init)."""
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        """Read handq_config.yaml from disk and cache the result."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
            self._loaded = True
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"YAML parse error in {self.config_path}: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_config(self) -> Dict[str, Any]:
        """Return the full configuration dictionary."""
        self._ensure_loaded()
        return self._config

    def get_section(self, section: str) -> Dict[str, Any]:
        """
        Return a top-level section of the configuration.

        Args:
            section: Top-level key name (e.g. 'llm', 'session', 'ui').

        Returns:
            Section dict, or an empty dict if the section does not exist.
        """
        self._ensure_loaded()
        return self._config.get(section, {})

    def reload_config(self, _name: Optional[str] = None) -> Dict[str, Any]:
        """
        Reload configuration from disk (hot reload).

        The optional ``_name`` parameter is accepted for backward compatibility
        but is ignored — there is only one config file.

        Returns:
            Updated full configuration dictionary.
        """
        self._loaded = False  # force re-read on next access
        self._ensure_loaded()
        return self._config

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_high_risk_config(self) -> Dict[str, Any]:
        """
        Return the high-risk commands configuration section.

        Returns:
            Dict with keys: always_dangerous_keywords, always_dangerous_patterns,
            high_risk_keywords, custom_patterns, whitelist.
        """
        self._ensure_loaded()
        return self._config.get("high_risk_commands", {})

    def get_interaction_switches_config(self) -> Dict[str, Any]:
        """
        Return the interaction switches configuration section.

        Returns:
            Dict keyed by switch name (tool_write / tool_edit / tool_bash /
            high_risk), each containing ``auto_approve`` and ``description``.
        """
        self._ensure_loaded()
        return self._config.get("interaction_switches", {})

    def is_auto_approve_enabled(self, switch_name: str) -> bool:
        """
        Check whether auto-approve is enabled for a specific interaction switch.

        Args:
            switch_name: One of tool_write / tool_edit / tool_shell / tool_bash / high_risk.

        Returns:
            True if auto_approve is set to true for that switch.
        """
        # get_interaction_switches_config already calls _ensure_loaded
        switches = self.get_interaction_switches_config()
        result = switches.get(switch_name, {}).get("auto_approve", False)
        # Fallback: tool_shell checks tool_bash for backward compatibility
        if not result and switch_name == "tool_shell":
            result = switches.get("tool_bash", {}).get("auto_approve", False)
        return result
