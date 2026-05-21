"""
Risk Guard - High-risk operation detection and interception
Responsible for detecting and intercepting high-risk operations to ensure system safety
Checks are performed in the RuntimeAgent's act phase

Optimization strategy
---------------------
Operations are split into two categories:

1. Always-dangerous (always_dangerous_keywords / always_dangerous_patterns):
   System-level operations (shutdown, kill, mkfs, etc.) that have no file-path
   context and must always be confirmed by the user.

2. Path-based risk (high_risk_keywords / custom_patterns):
   File operations (rm, delete, truncate, chmod, …) that are automatically
   approved when ALL referenced paths resolve to inside the working directory,
   and require confirmation only when a path is outside the working directory.

Additionally, write/edit tool calls whose target path is inside the working
directory are auto-approved by RuntimeAgent (via is_path_within_working_dir).
"""
import re
from pathlib import Path
from typing import List, Optional

from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.logger import get_logger
from ..models.decision import Decision


class RiskGuard:
    """Risk Guard - Detects and intercepts high-risk operations"""

    def __init__(self, config_manager: ConfigManager, working_directory: str = "."):
        """
        Initialize Risk Guard.

        Args:
            config_manager:    Configuration manager instance.
            working_directory: The agent's working directory.  Operations whose
                               file paths all resolve to inside this directory
                               are automatically approved.
        """
        self.config_manager = config_manager
        self.logger = get_logger()

        # Resolve to an absolute path once; used for all subsequent comparisons.
        self.working_directory: Path = Path(working_directory).resolve()

        self._load_risk_config()

        self.logger.info(
            f"RiskGuard initialized. Working directory: {self.working_directory}",
            component="RiskGuard",
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def set_working_directory(self, working_directory: str) -> None:
        """
        Update the working directory (e.g. when the agent changes context).

        Args:
            working_directory: New working directory path.
        """
        self.working_directory = Path(working_directory).resolve()
        self.logger.info(
            f"RiskGuard working directory updated: {self.working_directory}",
            component="RiskGuard",
        )

    def is_path_within_working_dir(self, path_str: str) -> bool:
        """
        Public helper: check whether a single file path is inside the working
        directory.  Used by RuntimeAgent to auto-approve write/edit tool calls.

        Args:
            path_str: File path to check (absolute or relative).

        Returns:
            True if the path resolves to inside the working directory.
        """
        return self._is_within_working_directory(path_str)

    # ------------------------------------------------------------------
    # Core risk detection
    # ------------------------------------------------------------------

    def is_high_risk(self, decision: Decision) -> bool:
        """
        Determine whether a decision is a high-risk operation that requires
        user confirmation.

        Decision logic for bash commands
        ---------------------------------
        1. Whitelist check  → if matched, always safe (return False).
        2. Always-dangerous → system-level ops (shutdown, kill, mkfs …);
                              return True unconditionally.
        3. Path-based risk  → file ops (rm, delete, truncate …);
                              return False if ALL paths are inside the working
                              directory, True otherwise.

        Args:
            decision: Agent's decision.

        Returns:
            True  → high-risk, user confirmation required.
            False → safe to proceed automatically.
        """
        # Only bash commands are inspected for risk.
        if decision.tool_name != "bash":
            return False

        command: str = (decision.parameters or {}).get("command", "")
        if not command:
            return False

        # ── 1. Whitelist ──────────────────────────────────────────────
        if self._is_whitelisted(command):
            self.logger.debug(
                f"Command is whitelisted: {command}",
                component="RiskGuard",
            )
            return False

        # ── 2. Always-dangerous (system-level, no path mitigation) ────
        if self._is_always_dangerous(command):
            self.logger.warning(
                f"Always-dangerous operation detected (confirmation required): "
                f"{command[:120]}",
                component="RiskGuard",
            )
            return True

        # ── 3. Path-based risk ────────────────────────────────────────
        if not (self._contains_risk_keyword(command) or self._matches_risk_pattern(command)):
            return False

        # Risk keyword/pattern matched — check whether all paths are safe.
        if self._all_paths_within_working_dir(command):
            self.logger.info(
                f"Path-based risk auto-approved "
                f"(all paths inside working dir '{self.working_directory}'): "
                f"{command[:120]}",
                component="RiskGuard",
            )
            return False

        self.logger.warning(
            f"High-risk operation detected (path outside working dir): {command[:120]}",
            component="RiskGuard",
        )
        return True

    # ------------------------------------------------------------------
    # Configuration loading
    # ------------------------------------------------------------------

    def _load_risk_config(self) -> None:
        """Load high-risk command configuration from config manager."""
        try:
            config = self.config_manager.get_high_risk_config()

            # System-level: always require confirmation regardless of paths.
            self.always_dangerous_keywords: List[str] = config.get(
                "always_dangerous_keywords", []
            )
            self.always_dangerous_patterns: List[str] = config.get(
                "always_dangerous_patterns", []
            )

            # Path-based: auto-approved when all paths are inside working dir.
            self.high_risk_keywords: List[str] = config.get("high_risk_keywords", [])
            self.custom_patterns: List[str] = config.get("custom_patterns", [])
            self.whitelist: List[str] = config.get("whitelist", [])

            # Pre-compile regex patterns for performance.
            self.compiled_always_dangerous_patterns = [
                re.compile(p, re.IGNORECASE) for p in self.always_dangerous_patterns
            ]
            self.compiled_patterns = [
                re.compile(p, re.IGNORECASE) for p in self.custom_patterns
            ]

            self.logger.debug(
                f"Loaded risk config: "
                f"{len(self.always_dangerous_keywords)} always-dangerous keywords, "
                f"{len(self.always_dangerous_patterns)} always-dangerous patterns, "
                f"{len(self.high_risk_keywords)} path-based keywords, "
                f"{len(self.custom_patterns)} path-based patterns, "
                f"{len(self.whitelist)} whitelist items",
                component="RiskGuard",
            )
        except Exception as e:
            self.logger.error(f"Failed to load risk config: {e}", component="RiskGuard")
            # Minimal safe defaults.
            self.always_dangerous_keywords = ["shutdown", "reboot", "kill", "mkfs", "dd"]
            self.always_dangerous_patterns = []
            self.compiled_always_dangerous_patterns = []
            self.high_risk_keywords = ["rm", "delete", "format", "truncate"]
            self.custom_patterns = []
            self.compiled_patterns = []
            self.whitelist = []

    # ------------------------------------------------------------------
    # Always-dangerous checks
    # ------------------------------------------------------------------

    def _is_always_dangerous(self, command: str) -> bool:
        """
        Return True if the command matches any always-dangerous keyword or
        pattern (system-level operations that cannot be mitigated by path
        checking).
        """
        command_lower = command.lower()

        for keyword in self.always_dangerous_keywords:
            pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
            if re.search(pattern, command_lower):
                return True

        for compiled in self.compiled_always_dangerous_patterns:
            if compiled.search(command):
                return True

        return False

    # ------------------------------------------------------------------
    # Path-based risk checks
    # ------------------------------------------------------------------

    def _all_paths_within_working_dir(self, command: str) -> bool:
        """
        Return True only when we can positively confirm that every file path
        referenced in the command resolves to inside the working directory.

        Conservative rules
        ------------------
        • Home-directory reference  (~/ or trailing ~)  → False
        • Parent-directory traversal (../)              → False
        • Absolute paths present → check each one; any outside → False
        • No absolute paths, no traversal, no home ref  → True
          (relative paths / globs are relative to the working directory)
        • No paths found at all (e.g. bare `kill 1234`) → False
          (handled upstream by always_dangerous_keywords, but kept as safety net)
        """
        # ── Home directory reference ──────────────────────────────────
        if re.search(r'(?:^|\s|[\'"])~[/\s\'"]|(?:^|\s|[\'"])~$', command):
            self.logger.debug(
                f"Home-dir reference detected, treating as outside working dir: "
                f"{command[:80]}",
                component="RiskGuard",
            )
            return False

        # ── Parent-directory traversal ────────────────────────────────
        if re.search(r"\.\.[/\\]", command):
            self.logger.debug(
                f"Parent-dir traversal detected, treating as outside working dir: "
                f"{command[:80]}",
                component="RiskGuard",
            )
            return False

        # ── Absolute paths ────────────────────────────────────────────
        # Match tokens that start with / (preceded by whitespace or quote).
        abs_paths = re.findall(r'(?:^|\s|[\'"])(/[^\s;|&<>\'\"\\]+)', command)

        if abs_paths:
            for raw in abs_paths:
                path_str = raw.strip("'\"").rstrip("/")
                if not self._is_within_working_directory(path_str):
                    self.logger.debug(
                        f"Absolute path outside working dir: {path_str}",
                        component="RiskGuard",
                    )
                    return False
            # Every absolute path is inside the working directory.
            return True

        # ── No absolute paths ─────────────────────────────────────────
        # All remaining path references are relative (./file, *.py, filename …)
        # and therefore relative to the working directory → safe.
        return True

    def _is_within_working_directory(self, path_str: str) -> bool:
        """
        Check whether a single path resolves to inside the working directory.

        Args:
            path_str: Path string (absolute or relative).

        Returns:
            True if the resolved path is inside self.working_directory.
        """
        try:
            path = Path(path_str.strip("'\""))
            if not path.is_absolute():
                path = self.working_directory / path
            resolved = path.resolve()
            try:
                resolved.relative_to(self.working_directory)
                return True
            except ValueError:
                return False
        except Exception as exc:
            self.logger.debug(
                f"Path resolution error for '{path_str}': {exc}",
                component="RiskGuard",
            )
            return False

    # ------------------------------------------------------------------
    # Whitelist / keyword / pattern helpers
    # ------------------------------------------------------------------

    def _is_whitelisted(self, command: str) -> bool:
        """Return True if the command contains any whitelist entry."""
        for item in self.whitelist:
            if item in command:
                return True
        return False

    def _contains_risk_keyword(self, command: str) -> bool:
        """Return True if the command contains any path-based risk keyword."""
        command_lower = command.lower()
        for keyword in self.high_risk_keywords:
            pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
            if re.search(pattern, command_lower):
                return True
        return False

    def _matches_risk_pattern(self, command: str) -> bool:
        """Return True if the command matches any path-based risk pattern."""
        for compiled in self.compiled_patterns:
            if compiled.search(command):
                return True
        return False

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_risk_description(self, decision: Decision) -> str:
        """
        Build a concise human-readable description of why a decision was flagged.

        Shows only:
          1. The bash command — truncated to 500 chars max, but always keeping
             50 chars of context around the first triggering keyword so the user
             can see exactly what triggered the alert.
          2. The triggering keyword(s).

        Args:
            decision: Agent's decision.

        Returns:
            Two-line risk description string.
        """
        command: str = (decision.parameters or {}).get("command", "")
        command_lower = command.lower()

        # ── Collect all triggering keywords ───────────────────────────────
        triggered_keywords: List[str] = []

        for keyword in self.always_dangerous_keywords:
            kw_pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
            if re.search(kw_pattern, command_lower):
                triggered_keywords.append(keyword)

        for i, compiled in enumerate(self.compiled_always_dangerous_patterns):
            if compiled.search(command):
                triggered_keywords.append(self.always_dangerous_patterns[i])

        for keyword in self.high_risk_keywords:
            kw_pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
            if re.search(kw_pattern, command_lower):
                triggered_keywords.append(keyword)

        for i, compiled in enumerate(self.compiled_patterns):
            if compiled.search(command):
                triggered_keywords.append(self.custom_patterns[i])

        # ── Smart command truncation ───────────────────────────────────────
        max_len = 500
        context = 50  # chars to keep before/after the keyword

        if len(command) <= max_len:
            display_command = command
        else:
            # Locate the first triggering keyword in the original command
            kw_pos: Optional[int] = None
            kw_len = 0
            for kw in triggered_keywords:
                m = re.search(r"\b" + re.escape(kw.lower()) + r"\b", command_lower)
                if m:
                    kw_pos = m.start()
                    kw_len = m.end() - m.start()
                    break

            if kw_pos is not None:
                start = max(0, kw_pos - context)
                end   = min(len(command), kw_pos + kw_len + context)
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < len(command) else ""
                display_command = prefix + command[start:end] + suffix
            else:
                display_command = command[:max_len] + "..."

        # ── Format output ─────────────────────────────────────────────────
        keyword_str = ", ".join(triggered_keywords) if triggered_keywords else "(pattern match)"
        return (
            f"Command : {display_command}\n"
            f"Keyword : {keyword_str}"
        )

    # ------------------------------------------------------------------
    # Runtime management
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        """Reload configuration from disk (hot reload)."""
        self.logger.debug("Reloading risk configuration", component="RiskGuard")
        self.config_manager.reload_config()
        self._load_risk_config()

    def add_to_whitelist(self, pattern: str) -> None:
        """
        Dynamically add an entry to the in-memory whitelist.

        Args:
            pattern: Substring that, if present in a command, marks it safe.
        """
        if pattern not in self.whitelist:
            self.whitelist.append(pattern)
            self.logger.info(f"Added to whitelist: {pattern}", component="RiskGuard")
