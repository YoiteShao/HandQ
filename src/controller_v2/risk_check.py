"""
V2 risk checking — stateless functions operating on (tool_name, parameters).

Ported from src/agent/risk_guard.py. No Decision dependency.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.logger import get_logger

_logger = get_logger()


def _load_config(config_manager: ConfigManager) -> dict:
    try:
        return config_manager.get_high_risk_config()
    except Exception:
        return {
            "always_dangerous_keywords": ["shutdown", "reboot", "kill", "mkfs", "dd"],
            "always_dangerous_patterns": [],
            "high_risk_keywords": ["rm", "delete", "format", "truncate"],
            "custom_patterns": [],
            "whitelist": [],
        }


def is_path_within_working_dir(path_str: str, working_directory: str) -> bool:
    """Check whether a path resolves to inside the working directory."""
    try:
        wd = Path(working_directory).resolve()
        p = Path(path_str.strip("'\""))
        if not p.is_absolute():
            p = wd / p
        resolved = p.resolve()
        resolved.relative_to(wd)
        return True
    except (ValueError, OSError):
        return False


def is_high_risk(
    tool_name: str,
    parameters: Dict[str, Any],
    working_directory: str,
    config_manager: ConfigManager,
) -> bool:
    """Determine if a tool call is high-risk and requires user confirmation."""
    if tool_name == "browser":
        if parameters.get("action") == "attach_browser":
            _logger.warning(
                "browser attach_browser action detected (confirmation required)",
                component="RiskCheck",
            )
            return True
        return False

    if tool_name not in ("bash", "shell"):
        return False

    command: str = parameters.get("command", "")
    if not command:
        return False

    config = _load_config(config_manager)
    whitelist: List[str] = config.get("whitelist", [])
    always_kw: List[str] = config.get("always_dangerous_keywords", [])
    always_pat: List[str] = config.get("always_dangerous_patterns", [])
    risk_kw: List[str] = config.get("high_risk_keywords", [])
    custom_pat: List[str] = config.get("custom_patterns", [])

    # 1. Whitelist
    for item in whitelist:
        if item in command:
            return False

    # 2. Always-dangerous
    command_lower = command.lower()
    for keyword in always_kw:
        pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
        if re.search(pattern, command_lower):
            _logger.warning(
                f"Always-dangerous operation detected: {command[:120]}",
                component="RiskCheck",
            )
            return True

    for pat in always_pat:
        try:
            if re.search(pat, command, re.IGNORECASE):
                return True
        except re.error:
            pass

    # 3. Path-based risk
    has_risk_keyword = False
    for keyword in risk_kw:
        kw_pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
        if re.search(kw_pattern, command_lower):
            has_risk_keyword = True
            break

    if not has_risk_keyword:
        for pat in custom_pat:
            try:
                if re.search(pat, command, re.IGNORECASE):
                    has_risk_keyword = True
                    break
            except re.error:
                pass

    if not has_risk_keyword:
        return False

    if _all_paths_within_working_dir(command, working_directory):
        _logger.info(
            f"Path-based risk auto-approved (all paths inside working dir): {command[:120]}",
            component="RiskCheck",
        )
        return False

    _logger.warning(
        f"High-risk operation detected (path outside working dir): {command[:120]}",
        component="RiskCheck",
    )
    return True


def get_risk_description(
    tool_name: str,
    parameters: Dict[str, Any],
    working_directory: str,
    config_manager: ConfigManager,
) -> str:
    """Build a human-readable description of why a call was flagged."""
    if tool_name == "browser":
        if parameters.get("action") == "attach_browser":
            creds_file = parameters.get("browser_credentials_file") or "(none — using config defaults)"
            return (
                "Agent wants to ATTACH to your running Chrome / Edge browser.\n"
                "\n"
                "What this means:\n"
                "  - HandQ will see ALL your currently open tabs and their content.\n"
                "  - HandQ can open new tabs and operate on existing tabs.\n"
                "  - HandQ uses your real cookies / login state.\n"
                "\n"
                f"Credentials file: {creds_file}\n"
                "\n"
                "Approve only if you started Chrome with --remote-debugging-port=9222\n"
                "and intend to share its tabs with the agent for THIS task."
            )

    command: str = parameters.get("command", "")
    command_lower = command.lower()

    config = _load_config(config_manager)
    always_kw: List[str] = config.get("always_dangerous_keywords", [])
    always_pat: List[str] = config.get("always_dangerous_patterns", [])
    risk_kw: List[str] = config.get("high_risk_keywords", [])
    custom_pat: List[str] = config.get("custom_patterns", [])

    triggered: List[str] = []
    for keyword in always_kw:
        kw_pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
        if re.search(kw_pattern, command_lower):
            triggered.append(keyword)
    for i, pat in enumerate(always_pat):
        try:
            if re.search(pat, command, re.IGNORECASE):
                triggered.append(pat)
        except re.error:
            pass
    for keyword in risk_kw:
        kw_pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
        if re.search(kw_pattern, command_lower):
            triggered.append(keyword)
    for pat in custom_pat:
        try:
            if re.search(pat, command, re.IGNORECASE):
                triggered.append(pat)
        except re.error:
            pass

    # Smart truncation around first keyword
    max_len = 500
    context = 50
    if len(command) <= max_len:
        display_command = command
    else:
        kw_pos: Optional[int] = None
        kw_len = 0
        for kw in triggered:
            m = re.search(r"\b" + re.escape(kw.lower()) + r"\b", command_lower)
            if m:
                kw_pos = m.start()
                kw_len = m.end() - m.start()
                break
        if kw_pos is not None:
            start = max(0, kw_pos - context)
            end = min(len(command), kw_pos + kw_len + context)
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(command) else ""
            display_command = prefix + command[start:end] + suffix
        else:
            display_command = command[:max_len] + "..."

    keyword_str = ", ".join(triggered) if triggered else "(pattern match)"
    return f"Command : {display_command}\nKeyword : {keyword_str}"


def _all_paths_within_working_dir(command: str, working_directory: str) -> bool:
    """Return True only when all referenced paths resolve inside working_directory."""
    # Home-directory reference
    if re.search(r'(?:^|\s|[\'"])~[/\s\'"]|(?:^|\s|[\'"])~$', command):
        return False

    # Parent-directory traversal
    if re.search(r"\.\.[/\\]", command):
        return False

    # Absolute paths
    abs_paths = re.findall(r'(?:^|\s|[\'"])(/[^\s;|&<>\'\"\\]+)', command)
    abs_paths += re.findall(r'(?:^|\s|[\'"])([A-Za-z]:[\\\/][^\s;|&<>\'\"]*)', command)

    if abs_paths:
        for raw in abs_paths:
            path_str = raw.strip("'\"").rstrip("/")
            if not is_path_within_working_dir(path_str, working_directory):
                return False
        return True

    return True
