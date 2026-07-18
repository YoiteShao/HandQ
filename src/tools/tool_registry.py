"""
Tool Registry - Centralized Tool Management
Provides a single source of truth for all tools, their metadata, and schemas
"""
import sys
from typing import TYPE_CHECKING, Dict, List, Type, Any, Optional
from .base_tool import BaseTool
from .read_tool import ReadTool
from .write_tool import WriteTool
from .edit_tool import EditTool
from .shell_tool import ShellTool
from .ssh_tool import StatelessSSHTool
# RemoteHandQTool is Windows-only — imported lazily inside `if _IS_WINDOWS:`
# to avoid loading a module that has Python-version-dependent syntax and is
# never used on Linux.
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .notebook_edit_tool import NotebookEditTool
from .browser_tool import BrowserTool
from .desktop_tool import DesktopTool
from .web_search_tool import WebSearchTool
from .email_tool import EmailTool
from .teams_tool import TeamsTool
from .ask_human_tool import AskHumanTool
from .wait_interval_tool import WaitIntervalTool
from .skill_tool import ReadSkillTool
from .spawn_agent_tool import SpawnAgentTool
from .fan_out_tool import FanOutAgentsTool
from .todo_write_tool import TodoWriteTool
from .self_extension_tool import ClaimToolTool, ReleaseToolTool
from .schedule_tool import (
    ScheduleCreateTool, ScheduleDeleteTool, ScheduleListTool,
)
from .schedule_wakeup_tool import ScheduleWakeupTool

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext

_IS_WINDOWS = sys.platform == "win32"


class ToolMetadata:
    """Tool metadata container"""

    def __init__(
        self,
        name: str,
        description: str,
        parameter_schema: Dict[str, Any],
        tool_class: Type[BaseTool],
        usage_guide: str = "",
        on_demand: bool = False,
    ):
        """
        Initialize tool metadata

        Args:
            name: Tool name (used as identifier)
            description: One-line summary of what the tool does
            parameter_schema: JSON schema for tool parameters
            tool_class: The tool class to instantiate
            usage_guide: Detailed usage guidance (when to use, when not to,
                         examples, strategy). Included verbatim in the system
                         prompt tools section.
        """
        self.name = name
        self.description = description
        self.parameter_schema = parameter_schema
        self.tool_class = tool_class
        self.usage_guide = usage_guide
        self.on_demand = on_demand

    def create_instance(self, ctx: Optional["SessionContext"] = None) -> BaseTool:
        """Create an instance of the tool, optionally injecting a SessionContext."""
        # Tool classes have their own __init__ that sets the name; many of them
        # also accept an optional ``ctx`` keyword. Try with ctx first, fall
        # back to no-args for tools whose ``__init__`` doesn't take it.
        try:
            return self.tool_class(ctx=ctx)  # type: ignore
        except TypeError:
            return self.tool_class()  # type: ignore


class ToolRegistry:
    """Central registry for all available tools"""

    # Tool name constants - use these instead of string literals
    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    SHELL = "shell"
    BASH = "bash"  # backward-compat alias for SHELL
    GLOB = "glob"
    GREP = "grep"
    NOTEBOOK_EDIT = "notebook_edit"
    SSH  = "ssh"
    REMOTE_HANDQ = "remote_handq"
    WEB_SEARCH = "web_search"
    EMAIL = "email"
    TEAMS = "teams"
    ASK_HUMAN = "ask_human"
    WAIT_INTERVAL = "wait_interval"
    READ_SKILL = "read_skill"
    SPAWN_AGENT = "spawn_agent"
    FAN_OUT_AGENTS = "fan_out_agents"
    TODO_WRITE = "todo_write"
    # Real structured tool_use for self-extension (replaces the old
    # embedded-JSON-in-reasoning convention — see self_extension_tool.py).
    # Always visible, like TODO_WRITE — never itself gated behind claim_tool.
    CLAIM_TOOL = "claim_tool"
    RELEASE_TOOL = "release_tool"
    # Agent-facing scheduling (Claude-Code CronCreate/List/Delete parity) +
    # dynamic self-paced loop wakeup (ScheduleWakeup parity). All on_demand.
    SCHEDULE_CREATE = "schedule_create"
    SCHEDULE_LIST = "schedule_list"
    SCHEDULE_DELETE = "schedule_delete"
    SCHEDULE_WAKEUP = "schedule_wakeup"
    # Live-shell family (Phase 2.1 split — no composite tool remains). Named
    # "live_shell", not "session", to avoid colliding with the unrelated
    # session_id/SessionContext/browser-session/bridge-session vocabulary
    # used throughout the rest of the codebase for a user's HandQ session —
    # this family is specifically about a long-lived INTERACTIVE SUBPROCESS
    # (adb shell, a Python REPL, a serial console), not a HandQ session.
    LIVE_SHELL_OPEN = "live_shell_open"
    LIVE_SHELL_EXEC = "live_shell_exec"
    LIVE_SHELL_WRITE = "live_shell_write"
    LIVE_SHELL_READ = "live_shell_read"
    LIVE_SHELL_LIST = "live_shell_list"
    LIVE_SHELL_CLOSE = "live_shell_close"

    _tools: Dict[str, ToolMetadata] = {}
    _initialized = False

    @classmethod
    def initialize(cls):
        """Initialize the tool registry with all available tools"""
        if cls._initialized:
            return

        # Register READ tool
        if _IS_WINDOWS:
            _read_usage_guide = """\
When to Use:
  - Examine file contents you haven't seen yet in this session
  - Read directory structure to understand project layout
  - Read a small set of related files together (pass as array) AFTER you have
    located them via 'glob' / 'grep'

When NOT to Use:
  - Discovery: do NOT pre-read N candidate files to figure out which is relevant.
    Use 'glob' to locate files by name/path and 'grep' to search content first;
    then 'read' only the specific file(s) you confirmed are interesting.
  - When you already have the file content from a previous read in this session
    (re-reading the same unchanged file wastes context budget)
  - When you only need to check if a file exists (use 'glob' or shell)
  - When you need to search for a pattern across files (use 'grep')

Pagination (single path):
  - Default: returns the first 2000 lines. If the file is larger, the result
    has truncated=True plus a notice telling you the next offset to pass.
  - 'offset' (1-based first line) and 'limit' (line count) page through large
    files explicitly. Prefer these over reading the whole file.
  - 'start_line' / 'end_line' are legacy aliases for offset/limit; do not mix
    them with offset/limit in the same call (the call will be rejected).

Multi-path mode (path is an array):
  - Each file is read with the default 2000-line cap; offset/limit/start_line/
    end_line are NOT accepted (ambiguous across files).
  - The total rendered content is soft-capped — once exceeded, remaining paths
    are returned as 'file_skipped' stubs with a re-read instruction.
  - Use this for batch reads of a SHORT list (≤5) of files you already know
    you need. For wider scans, prefer 'grep'/'glob' or read paths individually.

Strategy:
  - Locate first (glob/grep), then read the specific match
  - Context budget: each read result is appended to your conversation history
    and cannot be removed. Read what you need — avoid reading files you won't act on.
  - For huge files, page with offset/limit instead of asking for the whole thing

Examples:
  GOOD: {"path": "src/auth/login.py"}                          — single targeted read
  GOOD: {"path": "src/main.py", "offset": 1, "limit": 200}     — first 200 lines
  GOOD: {"path": "src/main.py", "offset": 2001, "limit": 500}  — page 2 of a big file
  GOOD: glob '**/*config.yaml' → read the one match
  BAD:  {"paths": ["src/a.py", "src/b.py", ..., "src/z.py"]}   — too many; grep first
  BAD:  Read the same file twice without changes in between
  BAD:  Read an entire 5000-line file when you only need one function
        → grep -n "def target" file.py to find the line, then read with offset/limit"""
        else:
            _read_usage_guide = """\
When to Use:
  - Examine file contents you haven't seen yet in this session
  - Read directory structure to understand project layout
  - Read a small set of related files together (pass as array) AFTER you have
    located them via 'glob' / 'grep'

When NOT to Use:
  - Discovery: do NOT pre-read N candidate files to figure out which is relevant.
    Use 'glob' to locate files by name/path and 'grep' to search content first;
    then 'read' only the specific file(s) you confirmed are interesting.
  - When you already have the file content from a previous read in this session
    (re-reading the same unchanged file wastes context budget)
  - When you only need to check if a file exists (use 'glob' or shell: test -f)
  - When you need to search for a pattern across files (use 'grep')

Pagination (single path):
  - Default: returns the first 2000 lines. If the file is larger, the result
    has truncated=True plus a notice telling you the next offset to pass.
  - 'offset' (1-based first line) and 'limit' (line count) page through large
    files explicitly. Prefer these over reading the whole file.
  - 'start_line' / 'end_line' are legacy aliases for offset/limit; do not mix
    them with offset/limit in the same call (the call will be rejected).

Multi-path mode (path is an array):
  - Each file is read with the default 2000-line cap; offset/limit/start_line/
    end_line are NOT accepted (ambiguous across files).
  - The total rendered content is soft-capped — once exceeded, remaining paths
    are returned as 'file_skipped' stubs with a re-read instruction.
  - Use this for batch reads of a SHORT list (≤5) of files you already know
    you need. For wider scans, prefer 'grep'/'glob' or read paths individually.

Strategy:
  - Locate first (glob/grep), then read the specific match
  - Context budget: each read result is appended to your conversation history
    and cannot be removed. Read what you need — avoid reading files you won't act on.
  - For huge files, page with offset/limit instead of asking for the whole thing

Examples:
  GOOD: {"path": "src/auth/login.py"}                          — single targeted read
  GOOD: {"path": "src/main.py", "offset": 1, "limit": 200}     — first 200 lines
  GOOD: {"path": "src/main.py", "offset": 2001, "limit": 500}  — page 2 of a big file
  GOOD: glob '**/*config.yaml' → read the one match
  BAD:  {"paths": ["src/a.py", "src/b.py", ..., "src/z.py"]}   — too many; grep first
  BAD:  Read the same file twice without changes in between
  BAD:  Read an entire 5000-line file when you only need one function
        → grep -n "def target" file.py to find the line, then read with offset/limit"""

        cls._tools[cls.READ] = ToolMetadata(
            name=cls.READ,
            description=(
                "Read one or more files or directories. "
                "Supports a single path, a list of paths, or both simultaneously. "
                "For a single path the result is returned directly; "
                "for multiple paths a summary with per-path results is returned. "
                "Supports PDF files (requires PyPDF2, pdfplumber, or pymupdf). "
                "Single-path reads default to the first 2000 lines; pass offset/limit "
                "to page through larger files. Multi-path reads have a per-file 2000-line "
                "cap and a soft total-content cap (remaining paths return file_skipped stubs)."
            ),
            usage_guide=_read_usage_guide,
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "oneOf": [
                            {
                                "type": "string",
                                "description": "A single file or directory path"
                            },
                            {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "A list of file or directory paths"
                            }
                        ],
                        "description": (
                            "One path (string) or multiple paths (array of strings) "
                            "to read. At least one of 'path' or 'paths' must be provided."
                        )
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional list of file or directory paths to read. "
                            "Can be used together with 'path'; duplicates are ignored."
                        )
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "1-based line number to start reading from (inclusive). "
                            "Default 1. Single-path only."
                        )
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Number of lines to return starting at offset. "
                            "Default 2000. Single-path only. Prefer this over reading "
                            "an entire large file in one call."
                        )
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Legacy alias for offset (1-based, inclusive). Prefer offset."
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Legacy alias used together with start_line (1-based, inclusive). Prefer offset+limit."
                    },
                    "pages": {
                        "type": "string",
                        "description": (
                            "Page range for PDF files (e.g., '1-5', '3', '10-20'). "
                            "Required for PDFs with more than 20 pages. "
                            "Maximum 20 pages per request."
                        )
                    }
                },
                "anyOf": [
                    {"required": ["path"]},
                    {"required": ["paths"]}
                ],
                "additionalProperties": False
            },
            tool_class=ReadTool
        )

        # Register WRITE tool
        cls._tools[cls.WRITE] = ToolMetadata(
            name=cls.WRITE,
            description=(
                "Write content to a file. "
                "Accepts 'path' (required), 'content' (required), and 'append' "
                "(optional bool, default false). "
                "ALL file content — every section, heading, and line — must be "
                "combined into the single 'content' string. "
                "Do NOT pass content sections as extra parameters; any parameter "
                "other than 'path', 'content', and 'append' is invalid and will "
                "cause an error. "
                "For very long content that cannot fit in one call, split it into "
                "chunks: first call with append=false (creates/overwrites the file), "
                "then subsequent calls with append=true to add more content."
            ),
            usage_guide="""\
When to Use:
  - Create a new file from scratch
  - Completely overwrite an existing file with new content
  - Write long content in chunks using append=true after the initial write

When NOT to Use:
  - Making small targeted changes to an existing file — use edit instead
    (edit is more precise, less error-prone, and preserves unchanged content)
  - When you only need to change a few lines or a single function

Strategy:
  - Combine ALL content into a single 'content' string — never split across parameters
  - For content > ~50 KB, split into chunks:
      1st call: append=false (creates/overwrites the file)
      Subsequent calls: append=true (adds to the file)
  - After writing, verify by reading back a key section to confirm correctness
  - Context budget: the 'content' string you pass is echoed back in the tool result.
    For very large writes, the echo consumes context. Use append chunking to limit
    the size of each individual call.

Examples:
  GOOD: Write a complete new Python module in one call
  GOOD: Write a 200-line report in 3 chunks of ~70 lines each using append=true
  BAD:  Use write to change 3 lines in a 500-line file — use edit instead
  BAD:  Pass content sections as separate parameters (only path/content/append are valid)""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to write"
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The complete file content as a single string. "
                            "Combine ALL sections, headings, and text here. "
                            "Do NOT split content across multiple parameters. "
                            "If the content is very long, use multiple write calls "
                            "with append=true to add content in chunks."
                        )
                    },
                    "append": {
                        "type": "boolean",
                        "description": (
                            "If true, append content to the existing file instead "
                            "of overwriting it. Use this to write long content in "
                            "multiple chunks: first call with append=false (or omit) "
                            "to create/overwrite the file, then subsequent calls with "
                            "append=true to add more content. Default: false."
                        )
                    }
                },
                "required": ["path", "content"],
                "additionalProperties": False
            },
            tool_class=WriteTool
        )

        # Register EDIT tool
        cls._tools[cls.EDIT] = ToolMetadata(
            name=cls.EDIT,
            description=(
                "Edit a file by replacing a specific section (find and replace). "
                "Replaces the FIRST occurrence of old_content with new_content. "
                "old_content must match the file exactly, including whitespace and indentation. "
                "Set replace_all=true to replace ALL occurrences (useful for renaming)."
            ),
            usage_guide="""\
When to Use:
  - Make targeted changes to specific parts of an existing file
  - Change a function, a few lines, or a section without rewriting the whole file
  - Fix a bug, update a value, or refactor a specific block
  - Rename a variable/function across an entire file (use replace_all=true)

When NOT to Use:
  - Creating a new file — use write instead
  - Changing more than ~50% of the file — write the whole file instead
  - When old_content appears multiple times and you only want to change one
    (edit replaces the FIRST match only; include enough context to be unique)

Strategy:
  - Include 3-5 lines of context around the change in old_content to ensure uniqueness
  - old_content must match EXACTLY: same whitespace, indentation, and line endings
  - For multiple changes to the same file, make separate edit calls in sequence
  - Use replace_all=true for renaming variables, changing repeated strings across the file
  - After editing, re-read the modified section to verify the change is correct
  - If the edit fails (old_content not found), re-read the file first to get the
    current exact content, then retry

Examples:
  GOOD: old_content includes the full function signature + 2 lines before/after
  GOOD: old_content is a unique 5-line block that appears exactly once
  GOOD: {"old_content": "old_name", "new_content": "new_name", "replace_all": true}
  BAD:  old_content is a single common line like "return None" (likely not unique)
  BAD:  old_content has different indentation than the actual file""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit"
                    },
                    "old_content": {
                        "type": "string",
                        "description": (
                            "The exact content to find and replace. "
                            "Must match the file exactly including whitespace. "
                            "Include 3-5 lines of context around the change "
                            "to ensure uniqueness. Replaces the FIRST match "
                            "(unless replace_all is true)."
                        )
                    },
                    "new_content": {
                        "type": "string",
                        "description": "New content to replace old_content with"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": (
                            "If true, replace ALL occurrences of old_content in the file "
                            "(useful for renaming variables or changing repeated strings). "
                            "Default: false (replace only the first unique match)."
                        )
                    }
                },
                "required": ["path", "old_content", "new_content"],
                "additionalProperties": False
            },
            tool_class=EditTool
        )

        # Register SHELL tool (replaces legacy BASH tool)
        _shell_description = (
            "Execute a shell command and return its stdout, stderr, and exit code. "
            "Supports foreground execution with timeout, background execution with task management, "
            "and persistent working directory across calls."
        )
        if _IS_WINDOWS:
            _shell_usage_guide = """\
Platform: Windows. Default shell: PowerShell 7+ (pwsh / powershell.exe).
Override: shell="cmd" (legacy batch), shell="bash" (Git Bash if installed).

Each call runs in a fresh process at the project root. Shell state (vars,
functions) does NOT persist between calls — chain with ';' if you need cd.
This also means PowerShell's OWN background primitives (`&`, `Start-Job`/
`Get-Job`, a bare `Start-Process` with no tracking) do NOT survive between
calls — the job object dies with the process that created it, so the next
call's `Get-Job` reports NotFound even though the real work is still running.
For anything that must be checked across multiple calls, use run_in_background
below instead of PowerShell's own job control.

WHEN TO USE
  - Run programs, scripts, tests, builds
  - Quick state checks (Get-ChildItem, Test-Path, Get-Process)
  - Long-running or backgrounded work you'll check on later →
    run_in_background=true (see BACKGROUND EXECUTION below) — NOT
    PowerShell's own `&`/Start-Job/Start-Process, which won't survive to
    your next call

WHEN NOT TO USE — use the listed alternative instead
  Read file contents          → 'read' tool (no escaping, no truncation issues)
  Find files by name          → 'glob' tool
  Search file contents        → 'grep' tool
  Edit a file                 → 'edit' / 'write' tool
  Interactive (Read-Host, Get-Credential, $Host.UI.PromptForChoice, pause,
    git rebase -i, vi, nano)  → these hang in non-interactive mode

PYTHON IS YOUR POWER TOOL — prefer it over chained PowerShell pipelines for
JSON/YAML/CSV processing, conditionals, loops, error handling, anything
beyond 2-3 piped commands. Easier to debug than PS object pipelines.
  e.g.  python -c "import json; d=json.load(open('a.json')); print(d['x'][0])"

MULTILINE `python -c "..."` IN POWERSHELL — the double-quoted arg is parsed
by PowerShell first (backticks, $var, embedded quotes), then by python. Two
safe patterns instead of hoping the quoting works:
  - Here-String: python -c @'
    import json
    print(json.load(open('a.json'))['x'])
    '@                       # single-quoted, literal; closing '@ AT COL 0
  - Temp file: write .py via the 'write' tool, then `python that_file.py`.
    The temp-file route also handles non-ASCII code correctly.
Symptom when you get this wrong:
  "python.exe: ScriptBlock should only be specified as a value ..." — that's
  PowerShell rejecting your string, not python.

POWERSHELL 7+ ESSENTIALS
  Pipeline    | passes objects (not text) — use Select-Object/Where-Object
  Variables   | $var = "x";  $env:NAME for env vars (NOT bash's $VAR)
  Strings     | "Hello $name", "Value: $($obj.Prop)"
  Null        | $null  (NOT /dev/null);  ?? coalesce, ?. null-conditional
  Chain       | cmd1 && cmd2  (stop on fail)  /  cmd1; cmd2  (always both)
  Heredoc     | @'..'@ literal, @"..."@ interpolated. Closing '@ AT COL 0
  Escape      | backtick (`), NOT backslash
  Confirm     | destructive cmdlets need -Confirm:$false to avoid prompt hang
  Registry    | HKLM:\\SOFTWARE\\... (PSDrive, NOT raw HKEY_LOCAL_MACHINE\\)

UNIX → POWERSHELL (top conversions; infer the rest)
  head -N file        → Get-Content file -TotalCount N
  tail -N file        → Get-Content file -Tail N
  rm -rf dir          → Remove-Item -Recurse -Force dir
  mkdir -p dir        → New-Item -ItemType Directory -Force dir
  which cmd           → (Get-Command cmd).Source
  2>/dev/null         → 2>$null
  VAR=x cmd           → $env:VAR='x'; cmd
  if [ -f x ]         → if (Test-Path x) { ... }
  `cmd` (backtick)    → $(cmd)

BACKGROUND EXECUTION
  run_in_background=true            → returns task_id; no timeout limit
  task_id="bg_X"                    → query status & captured output
  task_id="bg_X", command="kill"    → terminate task tree
  Use this — not `&`/Start-Job/bare Start-Process — for anything you'll
  check on in a LATER call; those don't survive past this call's process.
  A completed background task is also injected into your next turn
  automatically; you don't have to poll task_id at all if you're doing
  other work meanwhile.

OUTPUT
  - Truncated at 30,000 chars (10k head + 5k tail). Filter aggressively.
  - Background tasks capped at 30,000 bytes per stream.
  - Filter via | Select-Object -First N, | Select-String PATTERN.

SCOPE GUARD
  Search commands (grep, find, rg, Get-ChildItem -Recurse) MUST target '.'
  or a subdirectory. Searching drive root or %USERPROFILE% hangs on large
  trees → tool will warn but allow.

ESCALATION — claim the right tool instead of faking it on shell
  - Long-running remote batch (submit script → poll/wait → fetch logs)
    → claim_tool: ["ssh"] when the step targets a remote host
  - Persistent subprocess where EXACTLY ONE of these holds:
      (a) state must survive across commands (cwd, env, REPL, adb shell context)
      (b) watch streaming output AND inject commands concurrently
      (c) tty-bound device (serial console, minicom)
      (d) user explicitly asked to watch the process live
    → claim_tool: ["live_shell_open"] when you recognize the pattern
  - Web automation: visit URL, fill form, click, extract page, login flows
    → claim_tool: ["browser_launch"] when the step references a URL or web action
  - Native Windows app automation: Notepad, Excel, File Explorer, Settings,
    Task Manager, Office apps, third-party desktop software
    → claim_tool: ["desktop_screenshot"] when the step targets a native app or
       screen-level interaction
  If a step matches one of the above, claim the tool yourself (see
  [Available Tools — claim to activate] in the system prompt) — it
  activates the same turn you claim it, no extra round-trip needed. Don't
  thrash on shell trying to fake a capability another tool already has.

EXAMPLES
  GOOD: Get-ChildItem -Recurse -Filter *.py | Select-String 'def main'
  GOOD: python -m pytest tests/ -x -q
  GOOD: python -c "import json; d=json.load(open('a.json')); ..."
  GOOD: {"command": "npm test", "run_in_background": true}
  GOOD: {"command": "Get-ChildItem", "concurrent_safe": true}
  GOOD: {"task_id": "bg_1"} — check background task status
  GOOD: {"task_id": "bg_1", "command": "kill"} — kill background task
  BAD:  cat large_file.log         → use 'read' or Select-String
  BAD:  Read-Host "prompt"         → hangs (non-interactive)
  BAD:  grep -rn "x" src/          → wrong shell, use Select-String
  BAD:  awk + sed + grep chain     → write Python instead"""
        else:
            _shell_usage_guide = """\
Platform: Linux/macOS. Default shell: /bin/sh. Override with shell="bash"/"zsh".

Each call runs in a fresh process at the project root. Shell state (vars,
functions) does NOT persist between calls — chain with '&&' if you need cd.
This also means shell-native backgrounding (`cmd &`, `nohup`, `disown`) does
NOT survive between calls the way you might expect — the job is tied to the
shell process that launched it, which exits when this call returns. For
anything you'll check on in a LATER call, use run_in_background below.

WHEN TO USE
  - Run programs, scripts, tests, builds
  - Quick state checks (ls, ps, df, env, which)
  - Long-running or backgrounded work you'll check on later →
    run_in_background=true (see BACKGROUND EXECUTION below) — NOT `cmd &`/
    nohup, which won't survive to your next call

WHEN NOT TO USE — use the listed alternative instead
  Read file contents          → 'read' tool (no escaping, no truncation issues)
  Find files by name          → 'glob' tool
  Search file contents        → 'grep' tool
  Edit a file                 → 'edit' / 'write' tool
  Interactive (vi, nano, git rebase -i, prompts without --yes) → they hang

PYTHON IS YOUR POWER TOOL — prefer it over chained sed/awk/grep/cut when the
task needs JSON/YAML/CSV processing, conditionals, loops, error handling,
or more than 2-3 piped commands. Easier to debug, easier to extend.
  e.g.  python -c "import json; d=json.load(open('a.json')); print(d['x'][0])"

BACKGROUND EXECUTION
  run_in_background=true            → returns task_id; no timeout limit
  task_id="bg_X"                    → query status & captured output
  task_id="bg_X", command="kill"    → terminate task tree
  Use this — not `cmd &`/nohup/disown — for anything you'll check on in a
  LATER call; those don't survive past this call's process. A completed
  background task is also injected into your next turn automatically; you
  don't have to poll task_id at all if you're doing other work meanwhile.

OUTPUT
  - Truncated at 30,000 chars (10k head + 5k tail). Filter aggressively.
  - Background tasks capped at 30,000 bytes per stream.
  - Filter via | head -N, | tail -N, | grep PATTERN.

SCOPE GUARD
  Search commands (grep, find, rg) MUST target '.' or a subdirectory.
  Searching / or ~ hangs on large directory trees → tool will warn but allow.

ESCALATION — claim the right tool instead of faking it on shell
  Long-running remote batch (submit script → poll/wait → fetch logs)
  → claim_tool: ["ssh"] when the step targets a remote host

EXAMPLES
  GOOD: grep -rn "def process_batch" src/ | head -20
  GOOD: python -m pytest tests/ -x -q 2>&1 | tail -30
  GOOD: python -c "import json; d=json.load(open('a.json')); ..."
  GOOD: {"command": "npm test", "run_in_background": true}
  GOOD: {"command": "ls", "concurrent_safe": true}
  GOOD: {"task_id": "bg_1"} — check background task status
  GOOD: {"task_id": "bg_1", "command": "kill"} — kill background task
  BAD:  cat large_file.log                 → use 'read' or grep -n
  BAD:  find . -name "*.py" | xargs sed -i → write Python instead
  BAD:  git rebase -i HEAD~3               → interactive, hangs"""

        cls._tools[cls.SHELL] = ToolMetadata(
            name=cls.SHELL,
            description=_shell_description,
            usage_guide=_shell_usage_guide,
            parameter_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Shell command to execute. Required for new commands. "
                            "When used with task_id, set to 'kill' to terminate the task. "
                            + (
                                "Uses PowerShell syntax by default on Windows. "
                                "Specify shell=\"cmd\" or shell=\"bash\" for alternatives. "
                                if _IS_WINDOWS else
                                "Pipe through head/tail/grep to limit output size. "
                            )
                            + "IMPORTANT: search commands must be scoped to the working "
                            "directory ('.' or a subdirectory) — never to parent "
                            "directories or filesystem root."
                        )
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "If true, launch the command in the background and return "
                            "a task_id immediately. You will be notified when it completes. "
                            "Use for long-running commands (tests, builds, servers) — this "
                            "is the ONLY way to track a process across separate tool calls; "
                            "each call runs in a fresh shell process, so PowerShell's own "
                            "&/Start-Job or shell '&'/nohup do not survive to your next call "
                            "even though the underlying process keeps running. "
                            "Default: false."
                        )
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "ID of a background task to query or stop. "
                            "When provided without 'command', returns the task's current "
                            "status and output. When provided with command='kill', "
                            "terminates the background task."
                        )
                    },
                    "concurrent_safe": {
                        "type": "boolean",
                        "description": (
                            "ALWAYS set to true for read-only / observation-only commands "
                            "so they batch in parallel with other concurrent-safe tool calls "
                            "in the same response. Examples to mark true: "
                            "ls, find, grep, wc, cat, head, tail, which, type, "
                            "test -f / test -d, git status, git log, git diff, "
                            "python -c \"import …\" probes, --version checks, "
                            "PowerShell Get-ChildItem / Test-Path / Select-String. "
                            "Leave false (default) for anything that mutates state: "
                            "package installs, file moves, builds, deploys, tests "
                            "that write artifacts. When in doubt about safety, leave "
                            "it false. Multiple read-only checks in one turn should "
                            "all carry concurrent_safe=true so they finish in parallel "
                            "rather than one-after-the-other."
                        )
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Maximum seconds for foreground commands. "
                            "Default: 120. Maximum: 600 seconds (10 minutes)."
                        )
                    },
                    "shell": {
                        "type": "string",
                        "description": (
                            "Shell to use for execution. "
                            + (
                                "Options: \"powershell\"/\"pwsh\" (default), \"cmd\", \"bash\" (Git Bash). "
                                "Use \"cmd\" for legacy batch scripts; \"bash\" only if Git Bash is installed."
                                if _IS_WINDOWS else
                                "Options: \"sh\" (default), \"bash\", \"zsh\". "
                                "Usually omit to use the default."
                            )
                        )
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional human-readable description of what this command does. "
                            "Shown in progress displays. Useful for background tasks."
                        )
                    },
                },
                "required": [],
                "additionalProperties": False
            },
            tool_class=ShellTool
        )

        # Register GLOB tool
        _glob_usage_guide = """\
When to Use:
  - Find files by name or extension pattern across the project
  - Discover project structure (e.g., "**/*.py" to find all Python files)
  - Locate specific files before reading them

When NOT to Use:
  - When you need to search file CONTENTS — use grep instead
  - When you know the exact file path — use read directly
  - When listing a single directory — use read (which shows directory listing)

Strategy:
  - Start broad ("**/*.py") then narrow if too many results
  - Results sorted by modification time (newest first) — recently edited files appear first
  - Maximum 200 results returned; use a more specific pattern if truncated
  - Common noise directories (.git, node_modules, __pycache__, .venv) are skipped automatically

Examples:
  GOOD: {"pattern": "**/*.py"}                    — all Python files
  GOOD: {"pattern": "src/**/*.ts", "path": "."}   — TypeScript in src/
  GOOD: {"pattern": "**/test_*.py"}               — all test files
  BAD:  {"pattern": "*"} in a huge directory      — too broad, returns directories too"""

        cls._tools[cls.GLOB] = ToolMetadata(
            name=cls.GLOB,
            description=(
                "Fast file pattern matching. Find files by glob pattern "
                "(e.g. '**/*.py', 'src/**/*.ts'). "
                "Returns matching file paths sorted by modification time (newest first)."
            ),
            usage_guide=_glob_usage_guide,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern to match files against "
                            "(e.g. '**/*.py', 'src/**/*.ts', '*.md')"
                        )
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory to search in. Defaults to current working directory."
                        )
                    },
                    "skip_default_dirs": {
                        "type": "boolean",
                        "description": (
                            "If true (default), skip .git, node_modules, __pycache__, "
                            ".venv and other noise directories. Set to false to search "
                            "ALL directories including VCS internals."
                        )
                    }
                },
                "required": ["pattern"],
                "additionalProperties": False
            },
            tool_class=GlobTool
        )

        # Register READ_SKILL tool — the agent-facing half of progressive
        # disclosure. Always available (on_demand=False, like read/glob) so it
        # is present in every agent turn without a context provider. The
        # [Available Skills] menu lists names+descriptions; this loads the full
        # body of one on demand.
        cls._tools[cls.READ_SKILL] = ToolMetadata(
            name=cls.READ_SKILL,
            description=(
                "Load the full instructions of an available skill by name. "
                "The [Available Skills] menu lists what exists (name + one-line "
                "description); call this when the current task matches one of "
                "them to pull its complete methodology into context."
            ),
            usage_guide="""\
When to Use:
  - The current task matches a skill listed in the [Available Skills] menu and
    you want its full instructions before proceeding.
  - The user @-mentioned a skill by name — read it, then apply it.

When NOT to Use:
  - Speculatively reading every skill "just in case" — read only the one(s)
    relevant to the task at hand (menu descriptions tell you which).

Strategy:
  - Match the task to a menu entry by its description, then read that name.
  - The body is returned once and lives in your turn history like any tool
    result; you don't need to re-read it within the same task.

Examples:
  GOOD: {"name": "security-review"}   — task is "review this code for vulns"
  BAD:  {"name": "nonexistent"}       — not in the menu; returns an error""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill identifier exactly as shown in the "
                            "[Available Skills] menu."
                        )
                    }
                },
                "required": ["name"],
                "additionalProperties": False
            },
            tool_class=ReadSkillTool
        )

        # Register GREP tool
        _grep_usage_guide = """\
When to Use:
  - Search for a pattern (function name, variable, string) across files
  - Find where something is defined or used
  - Locate specific code patterns with regex
  - Cross-line pattern matching with multiline=true (e.g., class definition + method)

When NOT to Use:
  - When you know which file to look at — use read instead
  - When you need to find files by name — use glob instead

Strategy:
  - Use 'include' to narrow file types: {"include": "*.py"} for Python only
  - Default output_mode "files_only" is cheapest — use to locate, then read specific files
  - Use "content" mode with context_before/context_after when you need surrounding code
  - head_limit defaults to 250; use offset to page through large result sets
  - Binary files and files > 1 MB are skipped automatically
  - Use multiline=true for patterns that span multiple lines

Examples:
  GOOD: {"pattern": "def process_batch", "include": "*.py"}
  GOOD: {"pattern": "TODO|FIXME", "output_mode": "content", "context_after": 2}
  GOOD: {"pattern": "class.*Tool", "include": "*.py", "output_mode": "files_only"}
  GOOD: {"pattern": "class Foo.*?def bar", "multiline": true, "include": "*.py"}
  GOOD: {"pattern": "import", "include": "*.py", "offset": 50, "head_limit": 50}
  BAD:  {"pattern": "import"} without include — too many matches"""

        cls._tools[cls.GREP] = ToolMetadata(
            name=cls.GREP,
            description=(
                "Search file contents by regex pattern. "
                "Supports three output modes: 'files_only' (default, cheapest), "
                "'content' (matching lines with optional context), 'count' (match counts). "
                "Supports multiline matching and result pagination. "
                "Pure Python — no external tools required."
            ),
            usage_guide=_grep_usage_guide,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for (Python re syntax)"
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "File or directory to search in. "
                            "Defaults to current working directory."
                        )
                    },
                    "include": {
                        "type": "string",
                        "description": (
                            "Glob pattern to filter files "
                            "(e.g. '*.py', '*.js', '*.yaml')"
                        )
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_only", "content", "count"],
                        "description": (
                            "Output format: 'files_only' (default — list of matching file paths), "
                            "'content' (matching lines with context), "
                            "'count' (match counts per file)"
                        )
                    },
                    "context_before": {
                        "type": "integer",
                        "description": (
                            "Lines of context BEFORE each match (content mode only). "
                            "Equivalent to grep -B. Default: 0."
                        )
                    },
                    "context_after": {
                        "type": "integer",
                        "description": (
                            "Lines of context AFTER each match (content mode only). "
                            "Equivalent to grep -A. Default: 0."
                        )
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": (
                            "Shorthand: sets both context_before and context_after. "
                            "Equivalent to grep -C. Default: 0."
                        )
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "If true, search case-insensitively. Default: false."
                    },
                    "multiline": {
                        "type": "boolean",
                        "description": (
                            "If true, enable multiline mode where '.' matches newlines "
                            "and patterns can span across lines (re.DOTALL + re.MULTILINE). "
                            "Default: false."
                        )
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": (
                            "Maximum entries to return. Default: 250. "
                            "Set to 0 for unlimited (use sparingly)."
                        )
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Skip first N entries before applying head_limit. "
                            "Use with head_limit to page through results. Default: 0."
                        )
                    }
                },
                "required": ["pattern"],
                "additionalProperties": False
            },
            tool_class=GrepTool
        )

        # Register NOTEBOOK_EDIT tool
        cls._tools[cls.NOTEBOOK_EDIT] = ToolMetadata(
            name=cls.NOTEBOOK_EDIT,
            description=(
                "Edit Jupyter notebook (.ipynb) cells. "
                "Supports replace, insert, and delete operations. "
                "Cell numbering is 0-indexed."
            ),
            usage_guide="""\
When to Use:
  - Modify the source code or text of a specific notebook cell
  - Add new cells (code or markdown) to a notebook
  - Remove cells from a notebook

When NOT to Use:
  - When you need to read/view notebook contents — use read instead
  - When editing a .py file that isn't a notebook — use edit instead

Strategy:
  - Use edit_mode="replace" (default) to update an existing cell's content
  - Use edit_mode="insert" to add a new cell at a specific position
  - Use edit_mode="delete" to remove a cell
  - cell_number is 0-indexed (first cell = 0)
  - For insert: cell_type is required ('code' or 'markdown')
  - For replace: cell_type defaults to the existing cell's type

Examples:
  Replace cell 2: {"notebook_path": "nb.ipynb", "cell_number": 2, "new_source": "print('hello')"}
  Insert code cell at position 0: {"notebook_path": "nb.ipynb", "cell_number": 0, "edit_mode": "insert", "cell_type": "code", "new_source": "import numpy as np"}
  Delete cell 5: {"notebook_path": "nb.ipynb", "cell_number": 5, "edit_mode": "delete"}""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "notebook_path": {
                        "type": "string",
                        "description": "Path to the .ipynb notebook file"
                    },
                    "new_source": {
                        "type": "string",
                        "description": (
                            "New content for the cell. "
                            "Ignored for delete mode."
                        )
                    },
                    "cell_number": {
                        "type": "integer",
                        "description": (
                            "0-indexed cell number to operate on. "
                            "For insert: position to insert at."
                        )
                    },
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": (
                            "Cell type. Required for insert mode. "
                            "For replace, defaults to existing cell type."
                        )
                    },
                    "edit_mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": (
                            "Operation: 'replace' (default), 'insert', or 'delete'."
                        )
                    }
                },
                "required": ["notebook_path"],
                "additionalProperties": False
            },
            tool_class=NotebookEditTool
        )

        # Register SSH tool (on_demand=True: only enters the LLM tool list once the agent claims it)
        # Build the usage_guide as a string variable so we can drop the
        # session advisory line on Linux (session is Windows-only registered).
        _ssh_usage_guide = """\
SECURITY: Pass credentials_file (a local YAML/JSON path written by ssh_setup)
— NEVER hostname/username/password directly. Only the path passes through
LLM context.

WHEN TO USE
  Long-running remote batch jobs. You submit, the job survives disconnect,
  you poll/wait/fetch logs. NO interactive UI streaming.

WHEN NOT TO USE — use the listed alternative instead
  Local command                                   → 'shell' tool
  Single short remote cmd (no log/job tracking)   → 'shell' with: ssh host 'cmd'
  Need live UI streaming of remote output         → 'session' tool: open(command='ssh user@host')   (-tt auto-prepended)

ACTIONS (full param schemas in tool definition):
  exec | exec_bg | job_status | wait_done | tail_log | fetch_log
  write_file | run_script | safe_exit

Read the 'ssh-workflow' skill for the recommended action sequence, connection
pooling behavior, and shell-compatibility notes."""

        if not _IS_WINDOWS:
            # Drop the session advisory: session tool is not registered on Linux.
            # Linux users needing live remote streaming should use shell with
            # `ssh -tt host 'cmd | tee /tmp/log'` and poll the log via task_id.
            _ssh_usage_guide = _ssh_usage_guide.replace(
                "  Need live UI streaming of remote output         → 'session' tool: open(command='ssh user@host')   (-tt auto-prepended)\n",
                ""
            )

        cls._tools[cls.SSH] = ToolMetadata(
            name=cls.SSH,
            description=(
                "Stateless SSH tool. Each action opens a fresh connection and closes it "
                "when done — no persistent session state. "
                "SECURITY: hostname, username, password, and key_path are read from a local "
                "credentials file at runtime; only the file path is passed through the LLM."
            ),
            usage_guide=_ssh_usage_guide,
            parameter_schema=StatelessSSHTool().parameter_schema,
            tool_class=StatelessSSHTool,
            on_demand=True,
        )

        # Register REMOTE_HANDQ tool — delegate tasks to a remote Linux HandQ
        # agent over SSH. Windows-only: the Windows GUI delegates complex work
        # to Linux agents; Linux HandQ doesn't delegate to itself.
        if _IS_WINDOWS:
            from .remote_handq_tool import RemoteHandQTool
            _remote_handq_usage_guide = """\
WHEN TO USE
  - The remote task requires REASONING or PLANNING — not just a known command.
  - You cannot write out the full solution as a bash script in advance.
  - Complex multi-step work: "analyze this code", "fix all test failures",
    "investigate and resolve the build error".
  - Fire-and-forget: submit a goal and check back later.

WHEN NOT TO USE (use ssh tool instead)
  - You know the exact command(s) to run → ssh exec / run_script.
  - Single command, file copy, known script → ssh tool.
  - Real-time interactive session → session tool.
  - Never combine ["ssh", "remote_handq"] in one step.

KEY DISTINCTION: ssh vs remote_handq
  - ssh tool:        YOU drive the remote work step by step (intelligence here)
  - remote_handq:    REMOTE AGENT drives autonomously (intelligence there)
  - Rule of thumb:   "Can I write the bash commands?" → ssh.
                     "I need an agent to figure it out?" → remote_handq.

PREREQUISITES
  Remote Linux host must have HandQ installed by the user: copy the built dist
  package (handq_linux.dist/ + handq_config.yaml) and run handq_setup.sh to
  install the `handq_linux` command, or drop a source checkout (handq_linux.py
  next to src/) in ~/handq/. No service/systemd needed. submit_goal auto-wakes
  the resident daemon (handq_linux --_daemon) if it is not already running, but
  cannot deploy the files.

ACTIONS: submit_goal | get_status | get_result | send_message | new_session |
  interrupt | exit_handq

Read the 'remote-handq-workflow' skill for the recommended submit/poll/fetch
sequence and how to handle pending confirmations."""
            cls._tools[cls.REMOTE_HANDQ] = ToolMetadata(
                name=cls.REMOTE_HANDQ,
                description=(
                    "Delegate a task to a remote Linux HandQ agent over SSH. "
                    "Manages the full lifecycle: submit goal, monitor progress, "
                    "collect result. The remote agent plans and executes independently. "
                    "SECURITY: credentials read from local file only."
                ),
                usage_guide=_remote_handq_usage_guide,
                parameter_schema=RemoteHandQTool().parameter_schema,
                tool_class=RemoteHandQTool,
                on_demand=True,
            )

        # Register LIVE_SHELL tool family — interactive subprocess sessions
        # (adb shell, Python REPL, telnet, etc.). Windows-only: the
        # irreplaceable scenarios (adb dev, serial console, watch-and-inject)
        # are Windows-centric; Linux equivalents (tmux, expect, screen) are
        # mature enough that shell + ssh suffice. on_demand=True: enters the
        # LLM tool list once the agent claims a live_shell_* tool.
        if _IS_WINDOWS:
            from .session_tool import InteractiveSessionTool
            _live_shell_usage_guide = """\
WHEN TO USE — exactly ONE of (1)-(4) must hold; STATE WHICH IN REASONING:
  (1) State persistence across commands.
      Subsequent commands depend on accumulated process state that a fresh
      shell call would lose: cwd, env vars, REPL bindings, adb shell context,
      open file handles. shell spawns a new process per call → state lost.
      e.g.  open('adb shell') → exec('cd /data/local/tmp') → exec('ls') → exec('cat foo')

  (2) Watch streaming output AND inject commands concurrently.
      You read a continuous stream while sending input based on what you see.
      shell run_in_background lets you watch OR write — not both.
      e.g.  open('adb logcat') → read(timeout=5) → write('am force-stop X') → read

  (3) Tty-bound device interaction.
      Serial console, picocom, minicom — programs that genuinely require a
      pty and refuse to work with a piped stdin.
      e.g.  open('picocom -b 115200 /dev/ttyUSB0', prompt_pattern='> $')

  (4) User explicitly asked to watch / observe / 看着 / monitor live.
      The UI streams live_shell output in real time; only live_shell can do this.
      Trigger words: "看着", "演示", "watch", "observe", "monitor live".

WHEN NOT TO USE
  Anything not matching (1)-(4) → use shell or ssh. If you cannot name which
  scenario applies in your reasoning, you are using the wrong tool. Default
  to shell.
    Single command, even if remote     → shell with: ssh host 'cmd'
    Known multi-cmd sequence           → shell with: 'cmd1 && cmd2'
    Long-running ssh batch job         → ssh tool: run_script + wait_done
    Single ssh exec for stdout         → shell with: ssh host 'cmd'

ACTIONS (parameter details in schema):
  open    spawn subprocess; returns session_id; pass alias= to reuse a live one
  exec    send command, wait for completion (delimiter auto-injected for
          shells; prompt_pattern matched for REPLs)
  write   send raw stdin without waiting (use for y/n prompts, ^C, password)
  read    drain buffered output (timeout=N waits up to N seconds for new data);
          returns idle_seconds = time since last output (use for hang detection)
  list    list active sessions (includes idle_seconds per session)
  close   kill the subprocess tree

NOTES
  - Maximum 4 concurrent sessions; auto-killed on task completion.
  - SSH commands: 'ssh -tt' is auto-prepended for remote pty allocation.
    Credentials must be pre-established by ssh_setup (key auth or keyring) —
    no password injection happens here.
  - For streaming processes (logcat, tail -f), use write + read instead of exec.
  - HANG DETECTION: read and list return idle_seconds (time since last output).
    For monitoring: open the test command → loop (wait_interval + read) →
    if idle_seconds > threshold and status=alive → suspected hang.
    This is the PRIMARY liveness signal — faster and more reliable than screenshots.

EXAMPLES
  GOOD scenario (1): open('adb shell') → exec('cd /data') → exec('ls') → close
  GOOD scenario (2): open('adb logcat') → read(timeout=5) → write('q\\n')
  GOOD scenario (3): open('picocom -b 115200 /dev/ttyUSB0', prompt_pattern='> $')
  GOOD scenario (4): user said "watch it live" → open('ssh -tt user@host')
  GOOD monitoring:   open('adb shell run_test.sh') → wait_interval(60) → read →
        check idle_seconds → repeat until status=dead or idle_seconds > 300
  BAD:  open('adb shell') → exec('ls /data') → close
        ↑ single command, no state reuse → shell with: adb shell ls /data
  BAD:  open('ssh host') → exec('long_script.sh') → block waiting
        ↑ batch job → ssh tool: run_script + wait_done (survives disconnect)
"""
            from .session_tool import (
                SessionOpenTool, SessionExecTool, SessionWriteTool,
                SessionReadTool, SessionListTool, SessionCloseTool,
            )
            # ── live_shell_open ────────────────────────────────────────────
            # Carries the family's WHEN-TO-USE decision tree in its usage_guide,
            # since it's the entry point every live_shell workflow calls first.
            cls._tools[cls.LIVE_SHELL_OPEN] = ToolMetadata(
                name=cls.LIVE_SHELL_OPEN,
                description=(
                    "Spawn a long-lived interactive subprocess (adb shell, Python "
                    "REPL, serial console). Returns a session_id used by the other "
                    "live_shell_* tools. Use ONLY when shell+ssh cannot express the "
                    "scenario — see usage_guide for the 4 irreplaceable cases."
                ),
                usage_guide=_live_shell_usage_guide,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": (
                                "Program to launch as the session's long-lived "
                                "process (e.g., 'adb shell', 'python -i'). This "
                                "does NOT run a command inside an existing "
                                "session — to do that, call live_shell_exec "
                                "with the session_id instead. Ignored (the "
                                "existing session is reused as-is) when alias= "
                                "matches an already-open session."
                            ),
                        },
                        "alias": {
                            "type": "string",
                            "description": (
                                "If a live session with this alias exists, reuse "
                                "it instead of spawning a new one."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "Human-readable label for the session.",
                        },
                        "prompt_pattern": {
                            "type": "string",
                            "description": (
                                "Regex matching the REPL's prompt (e.g., '^>>> ' "
                                "for Python). Used by live_shell_exec to detect "
                                "completion. Not needed for shells."
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory for the subprocess.",
                        },
                        "merge_stderr": {
                            "type": "boolean",
                            "description": "Merge stderr into stdout (default true).",
                        },
                    },
                    "required": [],
                    "anyOf": [
                        {"required": ["command"]},
                        {"required": ["alias"]},
                    ],
                    "additionalProperties": False,
                },
                tool_class=SessionOpenTool,
                on_demand=True,
            )
            # ── live_shell_exec ────────────────────────────────────────────
            cls._tools[cls.LIVE_SHELL_EXEC] = ToolMetadata(
                name=cls.LIVE_SHELL_EXEC,
                description=(
                    "Send a command to an open session and wait for it to complete. "
                    "Delimiter is auto-injected for shells; prompt_pattern is matched "
                    "for REPLs (set at open time). Use for step-by-step interactive "
                    "workflows where each command's output informs the next."
                ),
                usage_guide="",
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session ID returned by live_shell_open.",
                        },
                        "command": {
                            "type": "string",
                            "description": "Command to send to stdin and wait for completion.",
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Max seconds to wait for completion (default 30, hard ceiling 600).",
                        },
                    },
                    "required": ["session_id", "command"],
                    "additionalProperties": False,
                },
                tool_class=SessionExecTool,
                on_demand=True,
            )
            # ── live_shell_write ───────────────────────────────────────────
            cls._tools[cls.LIVE_SHELL_WRITE] = ToolMetadata(
                name=cls.LIVE_SHELL_WRITE,
                description=(
                    "Write raw text to a session's stdin without waiting for "
                    "completion. Use for y/n prompts, ^C, password entry, or "
                    "streaming commands you'll read with live_shell_read."
                ),
                usage_guide="",
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session ID returned by live_shell_open.",
                        },
                        "input": {
                            "type": "string",
                            "description": "Raw text sent to stdin.",
                        },
                        "append_newline": {
                            "type": "boolean",
                            "description": "Append newline after input (default true).",
                        },
                    },
                    "required": ["session_id", "input"],
                    "additionalProperties": False,
                },
                tool_class=SessionWriteTool,
                on_demand=True,
            )
            # ── live_shell_read ────────────────────────────────────────────
            cls._tools[cls.LIVE_SHELL_READ] = ToolMetadata(
                name=cls.LIVE_SHELL_READ,
                description=(
                    "Drain buffered output from a session. Returns idle_seconds "
                    "(time since last output) — use for hang detection when the "
                    "output stream has fallen quiet."
                ),
                usage_guide="",
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session ID returned by live_shell_open.",
                        },
                        "timeout": {
                            "type": "number",
                            "description": (
                                "Seconds to wait for new data (default 0 = "
                                "immediate, drain what's already buffered). "
                                "Hard ceiling 600."
                            ),
                        },
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
                tool_class=SessionReadTool,
                on_demand=True,
            )
            # ── live_shell_list ────────────────────────────────────────────
            cls._tools[cls.LIVE_SHELL_LIST] = ToolMetadata(
                name=cls.LIVE_SHELL_LIST,
                description=(
                    "List all live sessions with their aliases, commands, and "
                    "idle_seconds. Pure enumeration — no state mutation."
                ),
                usage_guide="",
                parameter_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                tool_class=SessionListTool,
                on_demand=True,
            )
            # ── live_shell_close ───────────────────────────────────────────
            cls._tools[cls.LIVE_SHELL_CLOSE] = ToolMetadata(
                name=cls.LIVE_SHELL_CLOSE,
                description=(
                    "Terminate a session and release its subprocess. Max 4 "
                    "concurrent sessions — close ones you're done with."
                ),
                usage_guide="",
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session ID returned by live_shell_open.",
                        },
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
                tool_class=SessionCloseTool,
                on_demand=True,
            )

        # ── Atomic tool factory (Phase 2.1) ─────────────────────────────────
        # Register 28 atomic browser_* / desktop_* tools compactly. Each gets
        # its own name + terse description + narrow required-list; they SHARE
        # the composite's original ``properties`` dict so runtime param
        # validation (which is strict on unknown keys) still passes for
        # every param the underlying handler accepts. The family's full
        # usage_guide is attached to the family's entry-point tool only
        # (browser_launch, desktop_snapshot).
        def _register_atomic(
            name: str,
            *,
            tool_class: type,
            description: str,
            shared_properties: Dict[str, Any],
            usage_guide: str = "",
            required: Optional[List[str]] = None,
            optional_params: Optional[List[str]] = None,
            windows_only: bool = False,
        ) -> None:
            if windows_only and not _IS_WINDOWS:
                return
            # Confirmed bug (2026-07-14 live trace, recurrence found
            # 2026-07-16): shared_properties comes from the pre-split
            # composite tool's own parameter_schema, which lists every
            # property ANY atomic tool in the family reads. Originally only
            # 'action' was stripped — but every other property (e.g.
            # browser_extract's 'mode', browser_navigate's 'url') stayed
            # exposed on every OTHER atomic tool's schema too. A model
            # calling browser_launch(url=..., mode="fetch_json") passed
            # validation (both names are valid registry-wide), then
            # _action_launch_browser's **kwargs silently ignored them and
            # returned its normal reused/launched success shape — a
            # fake-success wrapped around a no-op, indistinguishable from a
            # working call except that the requested content never came
            # back. Live-observed 2026-07-16: claude-4-5-haiku burned 12
            # iterations re-trying browser_launch with different guessed
            # parameter combinations before giving up and reading the skill.
            # Fix: each atomic tool's schema is now scoped to exactly the
            # params ITS OWN handler reads (required ∪ optional_params) —
            # not the full family superset. A model-supplied parameter that
            # belongs to a sibling tool now fails _validate_tool_parameters's
            # "unexpected parameter" check instead of being silently dropped.
            _allowed = set(required or []) | set(optional_params or [])
            _properties = {k: v for k, v in shared_properties.items() if k in _allowed}
            cls._tools[name] = ToolMetadata(
                name=name,
                description=description,
                usage_guide=usage_guide,
                parameter_schema={
                    "type": "object",
                    "properties": _properties,
                    "required": required or [],
                },
                tool_class=tool_class,
                on_demand=True,
            )

        # Register BROWSER family — Windows-only. Phase 2.1: composite gone;
        # 16 atomic browser_* tools each register in their own right, sharing
        # BrowserTool.parameter_schema.properties so the strict param
        # validator still accepts every field the underlying handlers use.
        # The recipe/usage guidance (previously a giant composite usage_guide)
        # will move to a bundled ``browser-automation`` recipe skill so the
        # agent pulls it on demand via read_skill rather than paying prompt
        # bytes for it on every turn. TODO(Skill/browser-automation).
        if _IS_WINDOWS:
            from .browser_tool import (
                BrowserLaunchTool, BrowserAttachTool, BrowserNewTabTool,
                BrowserListTabsTool, BrowserNavigateTool, BrowserExtractTool,
                BrowserSnapshotTool, BrowserClickTool, BrowserTypeTool,
                BrowserWaitForTool, BrowserScreenshotTool,
                BrowserVisionQueryTool, BrowserVideoContextTool,
                BrowserFetchJsonTool, BrowserRequestUserLoginTool,
                BrowserCloseTabTool,
            )
            _browser_props = BrowserTool.parameter_schema.get("properties", {})
            for _name, _cls, _desc, _req, _opt in (
                ("browser_launch", BrowserLaunchTool,
                    "Start (or reuse) the persistent Chromium session. "
                    "Idempotent — safe to call repeatedly. Returns the first tab_id.",
                    [], []),
                ("browser_attach", BrowserAttachTool,
                    "Attach to the user's already-running Chrome (advanced; "
                    "requires config gate). Use ONLY when the task needs "
                    "existing browser state ('刚才/正在' in the user's request).",
                    [], ["browser_credentials_file"]),
                ("browser_new_tab", BrowserNewTabTool,
                    "Open a new tab. Optionally set background=true so the "
                    "user's focus is preserved (default in attach mode).",
                    [], ["url", "background", "timeout_ms"]),
                ("browser_list_tabs", BrowserListTabsTool,
                    "Enumerate every open tab (tab_id, url, title).",
                    [], []),
                ("browser_navigate", BrowserNavigateTool,
                    "Load a URL. Result auto-includes a `page_state` summary "
                    "of open dialogs + visible toasts.",
                    ["url"], ["tab_id", "timeout_ms", "wait_until"]),
                ("browser_extract", BrowserExtractTool,
                    "Read page content. mode ∈ text/html/attr/list "
                    "(selector required for attr/list).",
                    [], ["tab_id", "selector", "mode", "timeout_ms", "limit", "attribute"]),
                ("browser_snapshot", BrowserSnapshotTool,
                    "Enumerate every interactable element + open dialog on "
                    "the current page. Each item gets a suggested selector. "
                    "PREFERRED over speculative extract when you don't know the page.",
                    [], ["tab_id"]),
                ("browser_click", BrowserClickTool,
                    "Click a selector (CSS / text= / role= / xpath=). "
                    "Result returns `page_state` post-click so you see modal changes. "
                    "Don't guess a selector on an unfamiliar page — call "
                    "browser_snapshot first to get one.",
                    ["selector"], ["tab_id", "nth", "timeout_ms"]),
                ("browser_type", BrowserTypeTool,
                    "Type text into an element. Refused on input[type=password] — "
                    "use browser_request_user_login for login flows.",
                    ["selector", "text"], ["tab_id", "nth", "press_enter", "timeout_ms"]),
                ("browser_wait_for", BrowserWaitForTool,
                    "Block until a selector reaches a state OR the URL matches "
                    "a regex. Use after navigate/click when the next step "
                    "depends on the new page settling.",
                    [], ["tab_id", "selector", "url_pattern", "state", "timeout_ms"]),
                ("browser_screenshot", BrowserScreenshotTool,
                    "Capture the viewport (or a selector subtree) as PNG. "
                    "Auto-cleaned unless `path` is a session-dir absolute path.",
                    [], ["tab_id", "path", "selector", "full_page", "timeout_ms"]),
                ("browser_vision_query", BrowserVisionQueryTool,
                    "Send a viewport/selector screenshot to a small vision "
                    "LLM. USE ONLY for image-level questions (chart trend, "
                    "canvas content, captcha detection). For 'what's on the "
                    "page' use snapshot; for text content use extract.",
                    ["question"], ["tab_id", "selector", "full_page", "timeout_ms",
                                   "output_schema", "max_image_dim", "max_tokens"]),
                ("browser_video_context", BrowserVideoContextTool,
                    "Read the active <video>'s title/description/captions "
                    "via textTracks. THE right tool for 'what is this video "
                    "about' — no per-frame vision needed.",
                    [], ["tab_id", "selector", "max_cues", "seek_to_s", "pause"]),
                ("browser_fetch_json", BrowserFetchJsonTool,
                    "Fetch a JSON endpoint using the browser's cookies "
                    "(authenticated API calls without leaving Chromium).",
                    ["url"], ["method", "headers", "body", "same_origin", "timeout_ms"]),
                ("browser_request_user_login", BrowserRequestUserLoginTool,
                    "Hand off to the user for interactive login. The window "
                    "moves on-screen, user enters credentials in Chrome's "
                    "native UI, cookies persist across sessions.",
                    ["reason"], ["tab_id", "success_url_pattern"]),
                ("browser_close_tab", BrowserCloseTabTool,
                    "Close one tab by tab_id.",
                    ["tab_id"], []),
            ):
                _register_atomic(
                    _name, tool_class=_cls, description=_desc,
                    shared_properties=_browser_props, required=_req,
                    optional_params=_opt,
                )

        # Register DESKTOP family — Windows-only. Phase 2.1: composite gone;
        # 12 atomic desktop_* tools each register in their own right, sharing
        # DesktopTool.parameter_schema.properties so the strict param
        # validator still accepts every field the underlying handlers use.
        # The recipe/usage guidance (previously a giant composite usage_guide)
        # will move to a bundled ``desktop-automation`` recipe skill the
        # agent pulls on demand. TODO(Skill/desktop-automation).
        if _IS_WINDOWS:
            from .desktop_tool import (
                DesktopScreenshotTool, DesktopListWindowsTool,
                DesktopSnapshotTool, DesktopHoverAtTool,
                DesktopFindElementTool, DesktopFindAndClickTool,
                DesktopClickAtTool, DesktopTypeTextTool, DesktopDragTool,
                DesktopScrollTool, DesktopHotkeyTool, DesktopKeyPressTool,
            )
            _desktop_props = DesktopTool.parameter_schema.get("properties", {})
            # Safety facts a caller of these specific actions needs even
            # without reading Skill/desktop-workflow: they can be refused for
            # reasons outside the caller's control (hard window-content gate,
            # user-initiated revocation) — surfacing why avoids the agent
            # mistaking a safety refusal for a bug and retrying blindly.
            _desktop_control_safety_note = (
                "Hard refusal (cannot bypass): refused outright while a "
                "banking / password-manager / wallet app is foregrounded. "
                "The user can revoke control anytime with Ctrl+Shift+C — "
                "after that this action is refused until the user re-approves "
                "(desktop_screenshot / desktop_list_windows keep working)."
            )
            for _name, _cls, _desc, _req, _opt in (
                ("desktop_snapshot", DesktopSnapshotTool,
                    "Enumerate every interactable control on the foreground "
                    "window via Windows accessibility tree (UIA). Each element "
                    "gets role/text/x/y/selector — PREFERRED over "
                    "screenshot+OCR (~170 ms vs ~2.8 s).",
                    [], []),
                ("desktop_list_windows", DesktopListWindowsTool,
                    "List all top-level windows currently open + which is "
                    "foregrounded. Cheap discovery step.",
                    [], []),
                ("desktop_screenshot", DesktopScreenshotTool,
                    "Capture the active window or full screen as PNG. Reserve "
                    "for cases where you need actual pixels — snapshot covers "
                    "'what's on screen' faster.",
                    [], ["region", "monitor", "hwnd", "with_ocr", "path"]),
                ("desktop_find_element", DesktopFindElementTool,
                    "Locate a UI element by text or visual descriptor via OCR. "
                    "Slower than snapshot (~2.8 s) — try snapshot first.",
                    [], ["description", "region", "vision_fallback", "fuzzy_threshold"]),
                ("desktop_find_and_click", DesktopFindAndClickTool,
                    "Find an element by text/descriptor and click it in one "
                    "shot. When snapshot already gave you coordinates, use "
                    "desktop_click_at instead.",
                    [], ["description", "region", "vision_fallback", "fuzzy_threshold",
                         "button", "double", "use_uia_pattern"]),
                ("desktop_hover_at", DesktopHoverAtTool,
                    "Move cursor to (x, y) and OCR the tooltip that appears "
                    "~800 ms later. Use for iconless toolbar buttons snapshot "
                    "couldn't name.",
                    ["x", "y"], ["hover_seconds", "capture_after_hover"]),
                ("desktop_click_at", DesktopClickAtTool,
                    "Click at (x, y). Cheapest input action when you already "
                    "have coordinates from snapshot or find_element.",
                    ["x", "y"], ["button", "double", "use_uia_pattern"]),
                ("desktop_type_text", DesktopTypeTextTool,
                    "Type text into the foreground window's focused field. "
                    "Refused on sensitive windows (password managers, banking).",
                    ["text"], ["use_uia_pattern"]),
                ("desktop_drag", DesktopDragTool,
                    "Mouse drag from one point to another.",
                    ["from_x", "from_y", "to_x", "to_y"], ["duration"]),
                ("desktop_scroll", DesktopScrollTool,
                    "Scroll the foreground window by a given amount at a "
                    "given point.",
                    ["x", "y", "dy"], []),
                ("desktop_hotkey", DesktopHotkeyTool,
                    "Send a keyboard hotkey combo (e.g. Ctrl+S, Alt+F4).",
                    ["keys"], []),
                ("desktop_key_press", DesktopKeyPressTool,
                    "Press a single key by name (e.g. 'enter', 'esc', 'tab').",
                    ["key"], []),
            ):
                _register_atomic(
                    _name, tool_class=_cls, description=_desc,
                    shared_properties=_desktop_props, required=_req,
                    optional_params=_opt,
                    usage_guide=(
                        _desktop_control_safety_note
                        if _name in (
                            "desktop_click_at", "desktop_type_text",
                            "desktop_find_and_click", "desktop_hotkey",
                            "desktop_key_press", "desktop_drag", "desktop_scroll",
                        ) else ""
                    ),
                )


        # Register WEB_SEARCH tool. Windows-only — depends on browser_tool
        # whose Playwright session is Windows-tested. on_demand=True so it
        # only enters the LLM tool list once the agent claims it.
        if _IS_WINDOWS:
            cls._tools[cls.WEB_SEARCH] = ToolMetadata(
                name=cls.WEB_SEARCH,
                description=(
                    "Search across Qualcomm internal sources "
                    "(Confluence Cloud, Jira DC, SharePoint Online, orbit) "
                    "via the authenticated browser session. Cookies + SSO "
                    "are reused from the persistent browser profile so the "
                    "user logs in once per source and HandQ inherits the "
                    "cookie thereafter. Returns a list of normalised "
                    "(title, url, snippet, source, last_modified) hits — "
                    "use browser.navigate + extract to read full documents."
                ),
                usage_guide="""\
WHEN TO USE
  - Step text says "search Confluence/Jira/SharePoint/orbit/intranet for X"
  - Step text says "find internal docs / wiki page / ticket about X"
  - Anything that looks like cross-source enterprise search

WHEN NOT TO USE
  Public web search (Google / DuckDuckGo)        → browser navigate + extract
  Already know the URL                            → browser navigate
  Read a specific Confluence page / Jira ticket   → browser navigate + extract
  Email / calendar lookup                         → email tool

Results are ranking hits, not full documents (snippet-truncated to ~300
chars) — the agent picks which hit to open via browser.navigate. Default
limit 10, hard cap 25 (clamped from web_search.max_limit in handq_config.yaml).

Read the 'web-search-workflow' skill for the source list (confluence/jira/
sharepoint/orbit query syntax) and the login-recovery sequence.""",
                parameter_schema=WebSearchTool.parameter_schema,
                tool_class=WebSearchTool,
                on_demand=True,
            )

        # Register EMAIL tool. Windows-only — depends on pywin32 (win32com /
        # pythoncom) and a local Outlook MAPI profile. on_demand=True so it
        # only enters the LLM tool list once the agent claims it.
        if _IS_WINDOWS:
            cls._tools[cls.EMAIL] = ToolMetadata(
                name=cls.EMAIL,
                description=(
                    "Read Outlook email via local COM automation. "
                    "Reuses the user's MAPI profile — no extra credentials. "
                    "Actions: status, list_folders, list_messages, read_message, "
                    "search, mark_read, mark_unread, download_attachment, "
                    "download_all_attachments."
                ),
                usage_guide="""\
WHEN TO USE
  - Step says "read my email", "show inbox", "翻一下邮箱", "收件箱"
  - User asks who sent them message X, summary of unread, find attachment

WHEN NOT TO USE
  Web mail (Gmail, OWA via browser)   → browser tool
  IMAP/POP3 / Exchange EWS            → not supported here
  Calendar / contacts / tasks         → not in scope

KEY INVARIANTS
  - body_preview always 500 chars; include_full_body=true for full text
  - Outlook stays open — the tool never calls app.Quit()
  - No write actions (compose_draft / send) in this phase
  - output_dir outside sandbox → refused (path-traversal guard)

Read the 'email-workflow' skill for the recommended action sequence,
match_mode tradeoffs, and performance tips.""",
                parameter_schema=EmailTool.parameter_schema,
                tool_class=EmailTool,
                on_demand=True,
            )

        # Register TEAMS tool. Windows-only — registered alongside email so
        # the Linux agent never sees it (consistent with the desktop /
        # browser / email pattern). on_demand=True; enters the LLM tool list
        # once the agent claims it. Depends on httpx + playwright (both
        # already required); missing deps surface a clear "install X" message
        # from the tool's own first-call bootstrap.
        if _IS_WINDOWS:
            cls._tools[cls.TEAMS] = ToolMetadata(
                name=cls.TEAMS,
                description=(
                    "Microsoft Teams via Graph + Teams internal API. "
                    "Calendar / meetings, chats, channels, presence, "
                    "people, OneDrive files, Microsoft To Do — all "
                    "silent (does not steal mouse/keyboard or open the "
                    "Teams UI). First use harvests an access token from "
                    "the user's already-signed-in teams.microsoft.com "
                    "session via a brief Edge popup; thereafter every "
                    "call uses the cached token. Actions: "
                    "list_calendar_events, get_event, create_meeting, "
                    "respond_event, find_meeting_times, list_chats, "
                    "read_chat, send_chat, list_teams, list_channels, "
                    "read_channel, send_channel, find_person, "
                    "search_files, list_recent_files, list_tasks, "
                    "create_task, get_presence."
                ),
                usage_guide="""\
WHEN TO USE
  - Calendar: "today's meetings", "next meeting", "schedule with X"
  - Chats:    "list my chats", "read messages from X", "send X a message"
  - Channels: "list teams", "list channels in T", "post to #general"
  - People:   "find Zhang San", "Alice's email/title"
  - Presence: "am I shown busy", "is Bob online"
  - Files:    "find that PPT", "my recent files" (OneDrive backing Teams)
  - Tasks:    "what's due today", "add a task"

WHEN NOT TO USE / WHEN TO FALL BACK
  Set status / DND / status message  → browser (teams.microsoft.com avatar)
  Join an active meeting             → browser (use join_url from list_calendar_events)
  Watch a meeting recording          → browser (event web_link)
  Change Teams settings / theme      → tell user to do it in Teams Settings
  Live audio/video calling           → not supported (no API)
  Read local Outlook mail            → email tool (COM)
  Drive Teams desktop app via mouse  → desktop tool steals input; use browser

KEY INVARIANTS
  - top capped at 50 per call; paginate for older history
  - message_html: HTML or plain text, 32 KB cap per message
  - send_* / create_meeting / respond_event NOT undoable
  - 401 mid-task triggers automatic re-bootstrap (3-5s when cookie warm)
  - Bootstrap requires browser_profile to be free; close any running
    browser_tool action first if 'profile_locked' is reported
  - Do NOT shell-search the token cache file; the tool owns it

Read the 'teams-workflow' skill for the full capability matrix, browser
fallback routes, and the id-discovery-before-send pattern.""",
                parameter_schema=TeamsTool.parameter_schema,
                tool_class=TeamsTool,
                on_demand=True,
            )

        # Register ASK_HUMAN tool. Windows-only — relies on the GUI bridge to
        # render the modal and capture the reply. Linux/CLI runtimes use the
        # IM's stderr+stdin fallback, but the official surface is the Electron
        # UI. on_demand=True so it only enters the LLM tool list once the
        # agent claims it. Toggleable via the tool_ask_human interaction switch.
        if _IS_WINDOWS:
            cls._tools[cls.ASK_HUMAN] = ToolMetadata(
                name=cls.ASK_HUMAN,
                description=(
                    "Ask the user a single clarifying question via a modal "
                    "dialog and return their text reply. Use ONLY when the "
                    "task literally cannot proceed without information you "
                    "cannot derive from context — see usage_guide for the "
                    "restraint contract."
                ),
                usage_guide="""\
RESTRAINT CONTRACT (read before EVERY call)

WHEN TO USE
  - The task literally cannot proceed without information you (a) do not
    have, AND (b) cannot derive by reading the project, reasoning about it
    yourself, or making a sensible default choice that is easy
    to revert.
  - Examples that legitimately need ask_human:
      • The user said "deploy it" but never specified the target
        environment, and you cannot infer it from the repo.
      • The user said "send the email to the team" but the team's address
        is not in any artifact you can read.
      • A destructive operation needs a final go-ahead and there is no
        captured prior consent in this session.

WHEN NOT TO USE
  - To CONFIRM a choice the user already made — they made it; honour it.
  - To SECOND-GUESS your own plan — pick a sensible default and proceed;
    they can correct you afterwards.
  - To surface intermediate decisions you can make yourself (file names,
    function names, formatting choices, library choices, etc.).
  - To pick between cosmetic options ("which colour?", "which heading?").
  - To re-ask after the user dismissed the dialog — they declined; pick
    a default and continue.

PHRASING RULES
  - Pass exactly the literal sentence the user will read.
  - One short sentence. Answerable in one short sentence.
  - No "I need to ask…", no chain-of-thought, no options list, no
    multi-part questions.
  - Never include speculation, justification, or your reasoning — they
    interrupt the user too.

OUTPUT
  On success: ToolResult.output = the user's text reply (string).
  On user dismiss / empty reply: ToolResult.success=False with an error
  asking you to proceed with a default. DO NOT loop or re-ask.

EXAMPLES
  GOOD: {"question": "Which environment should I deploy to: staging or prod?"}
  GOOD: {"question": "What's the team's distribution address for the report?"}
  BAD:  {"question": "Should I name this file foo.py or bar.py?"}        ← decide yourself
  BAD:  {"question": "I'm going to add error handling, OK?"}              ← do it
  BAD:  {"question": "Let me know if you want me to continue."}           ← never
  BAD:  {"question": "The repo has X and Y. Should I use X or Y? Also,
                       what colour should the header be?"}                ← multi-part""",
                parameter_schema=AskHumanTool.parameter_schema,
                tool_class=AskHumanTool,
                on_demand=True,
            )

        # Register WAIT_INTERVAL tool — cross-platform, always available.
        # Lightweight async sleep for monitoring loops. The agent uses this
        # between observation cycles so the IterationAdvisor knows the pause
        # is intentional (not spinning).
        cls._tools[cls.WAIT_INTERVAL] = ToolMetadata(
            name=cls.WAIT_INTERVAL,
            description=(
                "Sleep for a specified duration between observation cycles in a "
                "monitoring task. Zero resource consumption; interruptible by user "
                "messages. Use this in any loop where you periodically check status "
                "and need to wait before the next check."
            ),
            usage_guide="""\
When to Use:
  - Monitoring a long-running process (SSH job, ADB install, browser download)
    and you need to wait between status checks
  - Any task where you observe → judge → wait → repeat
  - When you've confirmed the current state is acceptable and want to check
    again after a delay

When NOT to Use:
  - When you are actively working toward a goal (writing code, debugging, etc.)
  - As a substitute for SSH wait_done (use wait_done for "block until process
    exits" — it's more efficient for that specific case)
  - When the wait would exceed the task's natural completion time (if you expect
    the process to finish in 30s, don't wait 300s)

Monitoring Pattern:
  1. Observe: call your observation tools (ssh.job_status, desktop.screenshot, etc.)
  2. Judge: evaluate whether the state is normal, complete, or anomalous
  3. If anomalous: take action immediately (email, remediate)
  4. If complete: finish the item
  5. If normal: call wait_interval(seconds=N), then go to step 1

Choosing the interval:
  - Fast-changing state (download, install): 30-60s
  - Slow process (build, train, deploy): 300-600s
  - Very long job (hours): 600-1800s""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": (
                            "Duration to wait in seconds (1-7200, default 300). "
                            "Interruptible by user messages."
                        ),
                    },
                },
                "required": [],
            },
            tool_class=WaitIntervalTool,
        )

        cls._tools[cls.SPAWN_AGENT] = ToolMetadata(
            name=cls.SPAWN_AGENT,
            description=(
                "Fork a read-only exploration sub-agent that investigates an "
                "open-ended question by reading/searching and returns ONLY a text "
                "summary. Runs in an isolated context — use it to keep bulky "
                "exploration (dozens of file reads, wide greps) out of your own "
                "context window."
            ),
            usage_guide="""\
When to Use:
  - Open-ended investigation whose intermediate reads you will NOT need again:
    "map how auth works across the codebase", "find every config site for X".
  - Any exploration that would otherwise flood your context with file dumps.

When NOT to Use:
  - You already know the file/target → just read/grep it directly.
  - The work changes state (write/edit/ssh/browser) — the sub-agent is
    read-only by design; do that work yourself.
  - A single quick lookup — spawning has overhead; only worth it for wide/deep
    exploration.

The sub-agent has read / grep / glob / shell (read-only probes). It returns a
concise findings summary with exact paths/values it observed. That summary is
the only thing that enters your context.""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The exploration question for the sub-agent to answer "
                            "by reading/searching. Be specific about what to find "
                            "and what to report (e.g. file:line references)."
                        ),
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": ["explore", "general"],
                        "description": "'explore' (default) — read-only investigation.",
                    },
                },
                "required": ["prompt"],
            },
            tool_class=SpawnAgentTool,
        )

        cls._tools[cls.FAN_OUT_AGENTS] = ToolMetadata(
            name=cls.FAN_OUT_AGENTS,
            description=(
                "Run several independent sub-agent tasks concurrently, each in "
                "its own isolated context. Returns one text summary per task — "
                "use it to process independent items in parallel, or to get "
                "genuinely independent second opinions on the same question."
            ),
            usage_guide="""\
When to Use:
  - Independent items that don't depend on each other's results (check N
    hosts, review N files) and whose intermediate reads you won't need again.
  - You want more than one independent judgment on the same question —
    phrase it as several distinctly-angled prompts and compare the summaries
    yourself; this tool only provides the isolation, not the comparison.

When NOT to Use:
  - The tasks depend on each other's output — run them yourself in sequence.
  - A single task — use spawn_agent instead (this tool's overhead is for N>1).

Each task runs with `tool_profile`'s tools only (read-only by default) and
returns a text summary — the parent's context only sees the summaries, not
the intermediate tool calls. A failed task does not fail the batch; check
each result's `ok` field.""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": (
                            "1-30 independent tasks, each {\"prompt\": \"...\"}. "
                            "Each prompt is self-contained (the sub-agent has no "
                            "memory of this conversation)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string", "description": "The task/question for this sub-agent."},
                            },
                            "required": ["prompt"],
                        },
                    },
                    "tool_profile": {
                        "type": "string",
                        "enum": ["explore", "worker"],
                        "description": (
                            "'explore' (default) — read-only tools. 'worker' — "
                            "adds write/edit; a write/edit outside the working "
                            "directory is refused."
                        ),
                    },
                    "max_concurrency": {
                        "type": "integer",
                        "description": "Max tasks running at once. Default 6, clamped to [1, 10].",
                    },
                },
                "required": ["tasks"],
            },
            tool_class=FanOutAgentsTool,
        )

        cls._tools[cls.TODO_WRITE] = ToolMetadata(
            name=cls.TODO_WRITE,
            description=(
                "Track your own multi-step plan for the current task. Write the "
                "full todo list (re-emit each call to update it); it's shown to "
                "the user as a live progress panel and survives context "
                "compaction. Use for any task with 3+ distinct steps; skip for "
                "trivial one-step work."
            ),
            usage_guide="""\
When to Use:
  - A task with several distinct steps — capture them up front, then flip each
    to in_progress → completed as you go. Keeps you (and the user) oriented
    across a long task and survives compaction.

When NOT to Use:
  - Trivial single-step tasks (just do them).

How:
  - Pass the FULL list each call; it replaces the stored list (edit by
    re-emitting). Each item is {content, status} with status ∈
    pending|in_progress|completed. Keep exactly ONE item in_progress at a time.
  - This is YOUR plan, not a contract — revise it freely as you learn.""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "Your full plan, re-emitted each call. One item "
                            "in_progress at a time; flip to completed as you finish."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "One concrete step."},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "pending | in_progress | completed",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
            tool_class=TodoWriteTool,
        )

        # ── Self-extension: claim_tool / release_tool (real tool_use) ────────
        # Always visible (on_demand=False, like TODO_WRITE) — a model calls
        # these directly instead of embedding JSON in free-text reasoning.
        # execute() only records intent on ctx.pending_claim_tool /
        # pending_release_tool; PersistentAgent drains them after the tool
        # result comes back and applies them via the existing
        # _apply_self_extension. See self_extension_tool.py for the full
        # rationale (fixes a confirmed structural bug, 2026-07-14).
        cls._tools[cls.CLAIM_TOOL] = ToolMetadata(
            name=cls.CLAIM_TOOL,
            description=(
                "Activate one or more on-demand tools (from the [Available "
                "Tools] menu) so they appear in your tool list starting next "
                "turn. Call this directly — do not just mention the tool name "
                "in your reasoning, that has no effect."
            ),
            usage_guide="""\
When to Use:
  - You need a tool that isn't currently in your list (see [Available Tools]
    menu in the system prompt) — e.g. schedule_create, browser_navigate,
    ssh. Call claim_tool with the EXACT name(s) first; the tool becomes
    callable starting the NEXT turn (not the same turn you claim it in).

How:
  - names: exact tool name(s), e.g. ["schedule_create", "schedule_list"].
    No wildcards or family shorthand (\"schedule_*\" is not a valid name) —
    claim each tool you need individually.
  - Claiming an already-visible tool is a harmless no-op.
  - An unknown name is reported back in the tool_result's error, not
    silently dropped — check the result before assuming you're claimed.""",
            parameter_schema={
                "type": "object",
                "properties": ClaimToolTool.get_schema(),
                "required": ["names"],
            },
            tool_class=ClaimToolTool,
        )

        cls._tools[cls.RELEASE_TOOL] = ToolMetadata(
            name=cls.RELEASE_TOOL,
            description=(
                "Hide one or more tools you no longer need from your tool "
                "list, starting next turn. The tool's loaded instance stays "
                "warm — re-claiming later is free."
            ),
            usage_guide="""\
When to Use:
  - You claimed a tool for a specific sub-task and are done with it — release
    it to shrink your visible tool list (fewer choices to reason over).
    Optional; never required for correctness.

How:
  - names: exact tool name(s) to hide.""",
            parameter_schema={
                "type": "object",
                "properties": ReleaseToolTool.get_schema(),
                "required": ["names"],
            },
            tool_class=ReleaseToolTool,
        )

        # ── Agent-facing scheduling tools (Claude Code parity) ───────────────
        # Cross-platform, on_demand: only enter the LLM tool schema once the
        # agent claims them. The three cron tools wrap the process-global
        # Scheduler (ctx.scheduler); schedule_wakeup is a session-scoped
        # self-paced loop primitive that re-queues onto the current TaskChannel.
        cls._tools[cls.SCHEDULE_CREATE] = ToolMetadata(
            name=cls.SCHEDULE_CREATE,
            description=(
                "Schedule a prompt to fire automatically on a cadence (like a "
                "cron job). Each fire runs in a fresh scheduled session. Use for "
                "recurring or future one-shot tasks ('every morning summarise "
                "PRs', 'in 2 hours check the build')."
            ),
            usage_guide="""\
When to Use:
  - The user wants something run repeatedly on a clock ("every weekday at 9am")
    or once at a future time ("remind me in 2 hours", "tomorrow morning run X").

Schedule forms (the 'schedule' arg):
  - Friendly: 'every 5 minutes', 'every 2 hours', 'daily 09:00', 'weekly mon
    09:00', 'once at 2026-06-02 14:30', 'once in 10 minutes'.
  - Standard 5-field cron: '*/5 * * * *' (every 5 min), '0 9 * * 1-5' (weekdays
    9am), '0 0 1 * *' (1st of month). Fields: minute hour day-of-month month
    day-of-week; each supports *, */n, a-b, a,b,c.
  - Omit 'schedule' to infer the cadence from the prompt's own wording.

durable:
  - Leave False (default) for session-only tasks that vanish on app restart.
  - Set True ONLY when the user explicitly wants it to persist across restarts.

Prompt phrasing:
  - Write 'prompt' as an instruction to execute NOW — no relative-time words
    ('tomorrow', 'in 5 minutes'); the schedule already carries the timing.

Not for live watching: this is polling on a clock. To react to a process/file
changing in real time, stay in-session and use wait_interval loops instead.""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The prompt to fire on the cadence, phrased as an "
                            "instruction to do NOW (no relative-time words)."
                        ),
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Cadence: friendly ('every 5 minutes', 'daily 09:00', "
                            "'once in 10 minutes') or cron ('*/5 * * * *', "
                            "'0 9 * * 1-5'). Omit to infer from the prompt."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional label shown in the UI.",
                    },
                    "durable": {
                        "type": "boolean",
                        "description": (
                            "False (default)=session-only; True=persist across "
                            "restarts. Only True when the user explicitly asks."
                        ),
                    },
                },
                "required": ["prompt"],
            },
            tool_class=ScheduleCreateTool,
            on_demand=True,
        )

        cls._tools[cls.SCHEDULE_LIST] = ToolMetadata(
            name=cls.SCHEDULE_LIST,
            description=(
                "List all scheduled tasks (both persistent and session-only), "
                "with their id, cadence, enabled flag, and last run status."
            ),
            usage_guide="""\
When to Use:
  - Before deleting a task (you need its id).
  - To answer "what do I have scheduled?" or confirm a task was created.

Output: {count, tasks:[{id, name, schedule, enabled, durable, next_run_at,
last_status, run_count}]}.""",
            parameter_schema={"type": "object", "properties": {}, "required": []},
            tool_class=ScheduleListTool,
            on_demand=True,
        )

        cls._tools[cls.SCHEDULE_DELETE] = ToolMetadata(
            name=cls.SCHEDULE_DELETE,
            description=(
                "Delete a scheduled task by its id (get ids from schedule_list)."
            ),
            usage_guide="""\
When to Use:
  - The user wants to cancel/remove a scheduled task.

How:
  - Call schedule_list first to get the id, then schedule_delete(task_id=...).""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Id of the task to delete (from schedule_list).",
                    },
                },
                "required": ["task_id"],
            },
            tool_class=ScheduleDeleteTool,
            on_demand=True,
        )

        cls._tools[cls.SCHEDULE_WAKEUP] = ToolMetadata(
            name=cls.SCHEDULE_WAKEUP,
            description=(
                "Schedule yourself to wake up and continue after a delay, in "
                "THIS same session (keeping your context). Use for a self-paced "
                "loop: finish this turn, then resume the given prompt in N "
                "seconds. Unlike schedule_create, this does not spawn a fresh "
                "session — it re-queues work onto the current one."
            ),
            usage_guide="""\
When to Use:
  - A self-paced monitoring/iteration loop where YOU decide the next delay each
    round, and you want to release the session in between (freeing it and saving
    tokens) rather than blocking in-task.

schedule_wakeup vs wait_interval:
  - wait_interval BLOCKS the current item (session stays busy) — right for short
    waits (seconds to a couple minutes) where you resume the same tool loop.
  - schedule_wakeup RELEASES the turn and re-queues 'prompt' later — right for
    longer delays (minutes to an hour) where holding the session open is waste.

Choosing delay_seconds (clamped to [60, 3600]):
  - Under ~270s keeps the prompt cache warm; 300s is the worst of both worlds
    (cache miss without amortising it) — avoid it. For genuinely idle waits,
    prefer 1200-1800s. Match the delay to how fast the thing you're watching
    actually changes.

Ending the loop: simply DON'T call schedule_wakeup on a turn — the loop ends.""",
            parameter_schema={
                "type": "object",
                "properties": {
                    "delay_seconds": {
                        "type": "integer",
                        "description": (
                            "Seconds until you wake up (clamped to 60..3600)."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The instruction to resume with when you wake up."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One short sentence on what you're waiting for and "
                            "why this delay (shown to the user)."
                        ),
                    },
                },
                "required": ["delay_seconds", "prompt"],
            },
            tool_class=ScheduleWakeupTool,
            on_demand=True,
        )

        cls._initialized = True

    @classmethod
    def get_tool_names(cls) -> List[str]:
        """Get list of all registered tool names"""
        cls.initialize()
        return list(cls._tools.keys())

    @classmethod
    def get_tool_metadata(cls, name: str) -> ToolMetadata:
        """
        Get metadata for a specific tool

        Args:
            name: Tool name

        Returns:
            ToolMetadata for the tool

        Raises:
            KeyError: If tool not found
        """
        cls.initialize()
        if name not in cls._tools:
            raise KeyError(f"Tool not found: {name}")
        return cls._tools[name]

    @classmethod
    def get_all_metadata(cls) -> Dict[str, ToolMetadata]:
        """Get metadata for all registered tools"""
        cls.initialize()
        return cls._tools.copy()

    @classmethod
    def create_all_tool_instances(
        cls,
        ctx: Optional["SessionContext"] = None,
        venv_path: Optional[str] = None,
        extra_tool_names: Optional[List[str]] = None,
    ) -> Dict[str, BaseTool]:
        """
        Create instances of all registered tools.

        Args:
            ctx: Optional :class:`SessionContext` carrying per-session
                 resources (IM, file_state, ssh_pool, browser_session,
                 session_registry, desktop_state, interrupt_event). When
                 supplied, every tool's ``__init__`` is called with
                 ``ctx=ctx``; tools that don't read from ctx ignore it.
                 ``None`` is allowed for callers without a session (test
                 fixtures) — tools then fall back to their module-level state.
            venv_path: Optional path to a virtual environment root.  When set,
                       all bash commands run inside that venv (PATH is prepended
                       with the venv bin directory and VIRTUAL_ENV is set),
                       equivalent to sourcing activate before each command.
            extra_tool_names: Optional list of on-demand tool names to include.
                              On-demand tools are excluded by default and only
                              included when the agent claims them via
                              claim_tool.

        Returns:
            Dictionary mapping tool names to tool instances
        """
        cls.initialize()
        requested = set(extra_tool_names or [])
        instances: Dict[str, BaseTool] = {}
        for name, metadata in cls._tools.items():
            if metadata.on_demand and name not in requested:
                continue
            if name in (cls.SHELL, cls.BASH):
                instances[name] = ShellTool(ctx=ctx, venv_path=venv_path)
            elif name == cls.SSH:
                instances[name] = StatelessSSHTool(ctx=ctx) if ctx is not None else StatelessSSHTool()
            else:
                instances[name] = metadata.create_instance(ctx)
        return instances

    @classmethod
    def get_parameter_schemas(cls) -> Dict[str, Dict[str, Any]]:
        """
        Get parameter schemas for all tools

        Returns:
            Dictionary mapping tool names to their parameter schemas
        """
        cls.initialize()
        return {
            name: metadata.parameter_schema
            for name, metadata in cls._tools.items()
        }

    @classmethod
    def generate_tools_for_api(cls, extra_tool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Generate tools list in OpenAI function-calling format.

        Returns a list of tool definitions suitable for passing as the ``tools``
        parameter to the LLM API.  Tool descriptions and parameter schemas are
        taken directly from the registry so there is a single source of truth.

        Args:
            extra_tool_names: Optional list of on-demand tool names to include.
                              On-demand tools are excluded by default.

        Returns:
            List of tool dicts in OpenAI function-calling format, e.g.::

                [
                    {
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "description": "...",
                            "parameters": {...}
                        }
                    },
                    ...
                ]
        """
        cls.initialize()
        requested = set(extra_tool_names or [])
        tools: List[Dict[str, Any]] = []
        for name, metadata in cls._tools.items():
            if metadata.on_demand and name not in requested:
                continue
            # Build a combined description: one-line summary + usage guide.
            # The usage guide contains strategy, examples, and when-to-use hints
            # that help the model pick the right tool and use it correctly.
            description = metadata.description
            if metadata.usage_guide:
                description = f"{description}\n\n{metadata.usage_guide}"
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": metadata.parameter_schema,
                },
            })
        return tools

    @classmethod
    def generate_agent_response_schema(cls, extra_tool_names: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Generate the JSON schema for agent responses

        Args:
            extra_tool_names: Optional list of on-demand tool names to include
                              in the tool_name enum.

        Returns:
            JSON schema dictionary
        """
        cls.initialize()
        requested = set(extra_tool_names or [])
        active_names = [
            n for n, m in cls._tools.items()
            if not m.on_demand or n in requested
        ]
        return {
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "The agent's thought process and reasoning for the chosen action"
                },
                "tool_name": {
                    "type": "string",
                    "enum": active_names,
                    "description": (
                        "The name of the tool to use. "
                        "Omit (or set to null) when the goal is fully achieved — "
                        "the absence of tool_name is the completion signal."
                    )
                },
                "parameters": {
                    "type": "object",
                    "description": (
                        "Parameters for the selected tool. "
                        "ONLY include the exact parameters defined for the chosen tool. "
                        "Do NOT add any extra parameters beyond what the tool accepts. "
                        "If the content you're writing contains patterns that look like "
                        "JSON key-value pairs (e.g. 'key: value', 'version: 42'), keep them "
                        "INSIDE the appropriate parameter string — do NOT let them become "
                        "separate JSON keys in this object."
                    ),
                },
                "error": {
                    "type": "string",
                    "description": (
                        "Explanation of why the goal cannot be achieved. "
                        "Set this field (and omit tool_name/parameters) when the task "
                        "is fundamentally impossible with available tools and information. "
                        "Must explain what was tried, why it failed, and what is missing."
                    )
                },
            },
            "required": ["reasoning"],
        }


# Initialize registry on module import
ToolRegistry.initialize()
