"""
Tool Registry - Centralized Tool Management
Provides a single source of truth for all tools, their metadata, and schemas
"""
import sys
from typing import Dict, List, Type, Any, Optional
from .base_tool import BaseTool
from .read_tool import ReadTool
from .write_tool import WriteTool
from .edit_tool import EditTool
from .shell_tool import ShellTool
from .ssh_tool import StatelessSSHTool
from .remote_handq_tool import RemoteHandQTool
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .notebook_edit_tool import NotebookEditTool
from .browser_tool import BrowserTool
from .desktop_tool import DesktopTool
from .web_search_tool import WebSearchTool
from .email_tool import EmailTool
from .teams_tool import TeamsTool
from .ask_human_tool import AskHumanTool

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

    def create_instance(self) -> BaseTool:
        """Create an instance of the tool"""
        # Tool classes have their own __init__ that sets the name
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
    SESSION = "session"
    BROWSER = "browser"
    DESKTOP = "desktop"
    WEB_SEARCH = "web_search"
    EMAIL = "email"
    TEAMS = "teams"
    ASK_HUMAN = "ask_human"

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

WHEN TO USE
  - Run programs, scripts, tests, builds
  - Quick state checks (Get-ChildItem, Test-Path, Get-Process)
  - Long-running tasks → run_in_background=true, returns task_id

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

OUTPUT
  - Truncated at 30,000 chars (10k head + 5k tail). Filter aggressively.
  - Background tasks capped at 30,000 bytes per stream.
  - Filter via | Select-Object -First N, | Select-String PATTERN.

SCOPE GUARD
  Search commands (grep, find, rg, Get-ChildItem -Recurse) MUST target '.'
  or a subdirectory. Searching drive root or %USERPROFILE% hangs on large
  trees → tool will warn but allow.

ESCALATION (planner-activated, do not request manually)
  - Long-running remote batch (submit script → poll/wait → fetch logs)
    → 'ssh' tool activates when the step targets a remote host
  - Persistent subprocess where EXACTLY ONE of these holds:
      (a) state must survive across commands (cwd, env, REPL, adb shell context)
      (b) watch streaming output AND inject commands concurrently
      (c) tty-bound device (serial console, minicom)
      (d) user explicitly asked to watch the process live
    → 'session' tool activates when the planner detects the pattern
  - Web automation: visit URL, fill form, click, extract page, login flows
    → 'browser' tool activates when the step references a URL or web action
  - Native Windows app automation: Notepad, Excel, File Explorer, Settings,
    Task Manager, Office apps, third-party desktop software
    → 'desktop' tool activates when the step targets a native app or
       screen-level interaction
  If a step matches one of the above but the corresponding tool is not in
  your list, the planner under-declared `tools_required`. Stop calling
  shell on the wrong path — set the completion `error` field with a one-
  line note ("step needs <tool_name>: <why>") and omit `tool_name`. The
  planner will re-classify on the next observe_and_plan() round and
  re-issue the step with the right tool. This costs one iteration; far
  cheaper than thrashing on shell trying to fake the missing capability.

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

WHEN TO USE
  - Run programs, scripts, tests, builds
  - Quick state checks (ls, ps, df, env, which)
  - Long-running tasks → run_in_background=true, returns task_id

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

OUTPUT
  - Truncated at 30,000 chars (10k head + 5k tail). Filter aggressively.
  - Background tasks capped at 30,000 bytes per stream.
  - Filter via | head -N, | tail -N, | grep PATTERN.

SCOPE GUARD
  Search commands (grep, find, rg) MUST target '.' or a subdirectory.
  Searching / or ~ hangs on large directory trees → tool will warn but allow.

ESCALATION (planner-activated, do not request manually)
  Long-running remote batch (submit script → poll/wait → fetch logs)
  → 'ssh' tool activates when the step targets a remote host

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
                            "Use for long-running commands (tests, builds, servers). "
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

        # Register SSH tool (on_demand=True: only activated when a StepContextProvider requests it)
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

WORKFLOW (the recommended pattern — 5 steps)
  1. exec        Verify env, run < 30s commands. Returns 'login_shell' field.
  2. run_script  PREFERRED: upload script + launch as nohup background.
                 The job survives ssh disconnect.
  3. wait_done   PREFERRED over polling: single SSH connection blocks until
                 the job finishes. Set timeout = expected duration + buffer.
                   — OR —
     job_status  Poll every 30-60s when interleaving local work.
                 NOTE: log_tail is OMITTED while status="running"; use tail_log
                 to peek at running output.
  4. tail_log    Inspect output on success.
     fetch_log   Page through large logs to debug failures (start_line/end_line).
  5. safe_exit   ALWAYS call when done — kills tracked jobs, removes pid files.

ACTIONS (full param schemas in tool definition):
  exec | exec_bg | job_status | wait_done | tail_log | fetch_log
  write_file | run_script | safe_exit

CONNECTION MANAGEMENT (transparent)
  All actions to one host share a single TCP connection (pool). First action
  pays the handshake; subsequent actions reuse it for free. Auto-reconnects
  on transport death with exponential backoff. Keepalive every 30s prevents
  NAT/firewall from dropping the idle pool.

SHELL COMPATIBILITY
  Built-in actions (exec_bg, job_status, safe_exit) wrap commands in 'bash -c'
  regardless of remote login shell. For action='exec' on non-bash hosts:
    command='bash -c "your_command"'

EXAMPLES
  GOOD: ssh(exec, command="uname -a")                   (probe first)
  GOOD: ssh(run_script, script_content="...8h job...")  (long batch)
        → ssh(wait_done, timeout=30000)                 (single conn, blocks)
        → ssh(tail_log)                                 (read result)
        → ssh(safe_exit)                                (cleanup)
  BAD:  ssh(exec, command="...8h job...")               → use run_script
  BAD:  Loop ssh(job_status) every 5s                   → use wait_done"""

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
  Remote Linux host must have HandQ pre-installed by the user.
  User runs: bash handq_setup.sh --config <config> on the Linux machine.
  submit_goal can start an idle HandQ (handq --new) but cannot install it.

WORKFLOW
  1. submit_goal  — starts remote HandQ if idle, submits task
  2. get_status   — poll until task_status="completed" (or use wait_timeout)
  3. get_result   — read completion_reason + execution log tail
  4. exit_handq   — clean shutdown (optional; remote HandQ idles on its own)

MID-TASK MESSAGING
  Use send_message to inject instructions/corrections into a running task.
  The remote HandQ's receptionist evaluates and may replan.
"""
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

        # Register SESSION tool — interactive subprocess sessions (adb shell,
        # Python REPL, telnet, etc.). Windows-only: the irreplaceable scenarios
        # (adb dev, serial console, watch-and-inject) are Windows-centric;
        # Linux equivalents (tmux, expect, screen) are mature enough that
        # shell + ssh suffice. on_demand=True: activated by
        # SessionContextProvider when the planner detects the pattern.
        if _IS_WINDOWS:
            from .session_tool import InteractiveSessionTool
            _session_usage_guide = """\
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
      The UI streams session output in real time; only session can do this.
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
  read    drain buffered output (timeout=N waits up to N seconds for new data)
  list    list active sessions
  close   kill the subprocess tree

NOTES
  - Maximum 4 concurrent sessions; auto-killed on task completion.
  - SSH commands: 'ssh -tt' is auto-prepended for remote pty allocation.
    Credentials must be pre-established by ssh_setup (key auth or keyring) —
    no password injection happens here.
  - For streaming processes (logcat, tail -f), use write + read instead of exec.

EXAMPLES
  GOOD scenario (1): open('adb shell') → exec('cd /data') → exec('ls') → close
  GOOD scenario (2): open('adb logcat') → read(timeout=5) → write('q\\n')
  GOOD scenario (3): open('picocom -b 115200 /dev/ttyUSB0', prompt_pattern='> $')
  GOOD scenario (4): user said "watch it live" → open('ssh -tt user@host')
  BAD:  open('adb shell') → exec('ls /data') → close
        ↑ single command, no state reuse → shell with: adb shell ls /data
  BAD:  open('ssh host') → exec('long_script.sh') → block waiting
        ↑ batch job → ssh tool: run_script + wait_done (survives disconnect)
"""
            cls._tools[cls.SESSION] = ToolMetadata(
                name=cls.SESSION,
                description=(
                    "Interactive session tool — spawn and control long-lived subprocesses "
                    "(adb shell, Python REPL, serial console, etc.) across multiple tool "
                    "calls. UI streams stdout in real time. Use ONLY when shell+ssh cannot "
                    "express the scenario; see WHEN TO USE for the 4 irreplaceable cases."
                ),
                usage_guide=_session_usage_guide,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["open", "exec", "write", "read", "list", "close"],
                            "description": "Session action to perform.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "[exec/write/read/close] Session ID returned by open.",
                        },
                        "command": {
                            "type": "string",
                            "description": (
                                "[open] Program to launch (e.g., 'adb shell', 'python -i'). "
                                "[exec] Command to send to stdin and wait for completion."
                            ),
                        },
                        "alias": {
                            "type": "string",
                            "description": (
                                "[open] If a live session with this alias exists, reuse it "
                                "instead of spawning a new one. Useful for re-entering an "
                                "existing adb/REPL session across planner steps."
                            ),
                        },
                        "input": {
                            "type": "string",
                            "description": "[write] Raw text sent to stdin without waiting.",
                        },
                        "description": {
                            "type": "string",
                            "description": "[open] Human-readable label for the session.",
                        },
                        "prompt_pattern": {
                            "type": "string",
                            "description": (
                                "[open] Regex matching the REPL's prompt "
                                "(e.g., '^>>> ' for Python). Used by 'exec' to detect "
                                "completion. Not needed for shells (delimiter auto-injected)."
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "description": "[open] Working directory for the subprocess.",
                        },
                        "timeout": {
                            "type": "number",
                            "description": (
                                "[exec] Max seconds to wait for completion (default 30). "
                                "[read] Seconds to wait for new data (default 0 = immediate)."
                            ),
                        },
                        "append_newline": {
                            "type": "boolean",
                            "description": "[write] Append newline after input (default true).",
                        },
                        "merge_stderr": {
                            "type": "boolean",
                            "description": "[open] Merge stderr into stdout (default true).",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                tool_class=InteractiveSessionTool,
                on_demand=True,
            )

        # Register BROWSER tool. Windows-only — Playwright + Edge channel
        # gating is tested only on Win11. on_demand=True so it only enters
        # the LLM tool list when BrowserContextProvider activates it
        # (Phase 4); without the provider, LLM never sees this tool.
        if _IS_WINDOWS:
            cls._tools[cls.BROWSER] = ToolMetadata(
                name=cls.BROWSER,
                description=(
                    "Persistent Chromium browser automation (single shared session "
                    "with off-screen window position so the user's desktop is not "
                    "disturbed). Cookies persist across HandQ sessions in "
                    "%USERPROFILE%\\HandQ\\browser_profile\\."
                ),
                usage_guide="""\
WHEN TO USE
  - Visit a website, follow links, read page content, fill forms, click buttons
  - Tasks the user phrases in terms of "open", "go to <url>", "查看 <网站>",
    "登录"、"点击"、"填写"

WORKFLOW
  1. action='launch_browser' once per session (idempotent — safe to call
     repeatedly; reuses the existing session). Returns the first tab_id.
  2. action='navigate' with url='https://...' to load a page. The result
     auto-includes a 'page_state' summary (open dialogs + toasts) so you
     usually do NOT need a follow-up extract just to see what is on screen.
  3. action='snapshot' to enumerate every visible button / link / form
     control AND any open dialogs in a single call. Each item carries a
     suggested selector — pass it straight to click / type. STRONGLY
     PREFERRED over speculative extract probes when you don't already
     know the page structure.
  4. action='extract' for content reads:
       - mode='text' (default): visible text (filter dropdowns are
         collapsed to the selected option to keep payloads small).
       - mode='html': outerHTML of the FIRST match (or whole document).
       - mode='attr': attributes of one element.
       - mode='list': outerHTML+text of EVERY match up to 'limit' (default
         20, max 100) — use when enumerating candidates by selector.
  5. action='click' / 'type' to interact. Selectors support CSS,
     text='Login', role='button[name=Submit]', xpath=//.  Both echo a
     'page_state' field after the action so you see modal/toast changes
     immediately — do not chase with a screenshot+extract pair.
  6. action='wait_for' to block until a selector appears (state='visible'
     default) or the URL matches a regex (url_pattern='/dashboard$').
  7. action='vision_query' for content the DOM cannot give you — text or
     visuals rendered inside <canvas>, charts, captcha detection, image
     classification ("is this a dog?", "is the person male or female?",
     "is this chart trending up?"). The tool screenshots the viewport
     (or selector subtree), ships it to a small multimodal model, and
     returns a TEXT answer plus parsed_json when output_schema is given.
     The image bytes never enter your context — only the distilled
     answer. STRICT: read VISION_QUERY DECISION RULE below before
     calling — most "where is X" / "what does this page say" / "what
     does this video cover" questions have better tools.
  8. action='video_context' to read the active <video>'s title,
     description, duration, and CAPTION text via the textTracks API —
     no vision involved, no per-frame sampling needed. This is THE
     answer for "what is this video about" / "is there a part about X"
     / "summarise the lecture" / "watch this section". Pair with
     seek_to_s + pause=true to position on a specific frame, then
     follow with a SINGLE screenshot+vision_query if you need to know
     what that frame looks like.
  9. action='list_tabs' if you have multiple tabs.
  10. action='close_tab' for tabs you no longer need.

KEY INVARIANTS
  - tab_id is optional everywhere except close_tab; defaults to the first tab.
  - extract mode='text' (default) returns visible text capped at ~100KB.
    Filter dropdowns (<select>, <datalist>) are collapsed to their selected
    option so a 160-row dashboard does not dump 1KB+ of option lists per call.
  - extract mode='html' returns outerHTML when selector given, full HTML otherwise.
  - extract mode='attr' requires a selector; returns one attribute or all.
  - extract mode='list' requires a selector; returns up to 'limit' matches
    (default 20, hard cap 100) — use this instead of repeating extract with
    successively narrower selectors.
  - snapshot has no selector arg; it lists every interactable element on
    the page plus open dialogs / toast notifications, with a suggested
    selector for each. Cheap (one page.evaluate call) and bounded.
  - navigate / click / type / snapshot return a 'page_state' field with
    the topmost open dialog and visible toasts when present — read it
    instead of firing a follow-up extract.

PASSWORD GUARD (HARD REFUSAL)
  type is server-side refused on input[type=password]. If you encounter a
  login form, do NOT try to fill the password — call request_user_login
  so the user can log in manually. The resulting cookies persist in the
  user-data-dir for future sessions.

LOGIN HANDOFF (request_user_login)
  When a page requires user credentials:
    1. action='navigate' to the login page (or land there organically).
    2. action='request_user_login', reason='<short explanation>',
       success_url_pattern='<regex matching post-login URL>'   (optional but
       recommended).
    3. The browser window moves on-screen at the login page; HandQ shows an
       Approve/Reject modal explaining what's happening. The user enters
       credentials in Chrome's native UI — agent sees nothing.
    4. User clicks Approve when finished. The window moves back off-screen
       and the action returns the post-login URL plus whether
       success_url_pattern matched.
    5. If the user clicks Reject, the action fails — re-plan or ask the
       user for a different approach.
  Cookies persist across HandQ sessions, so this dance happens at most
  once per site (until cookies expire on the server side).

STEALTH
  The browser launches off-screen (window-position=-32000,-32000) so the
  user's desktop focus is preserved. Do NOT enable headless — sites detect
  headless via JS fingerprinting; we want a real-user fingerprint while
  staying invisible.

VISION_QUERY DECISION RULE (read before each call)
  Before calling vision_query, identify which kind of question you have:

  ✅ vision_query IS THE RIGHT TOOL for IMAGE-LEVEL questions:
    - Image classification ("Is this a dog or a cat?",
      "Is the person in this photo male or female?",
      "Is this chart trending up?")
    - <canvas> / <svg> / <video> SINGLE-FRAME content that the DOM
      cannot read (chart screenshot, slide on a paused video frame)
    - Captcha / verification page DETECTION (recognise that one is
      present and bail out — never try to solve)
    - "Click on the dog image" / "find the orange button" — one-shot
      visual grounding, returns coordinates

  ❌ vision_query IS THE WRONG TOOL for these — use what's listed:
    - "What is on this page?"            → snapshot
    - "What does the heading say?"       → extract mode='text'
    - "Where is the Login link?"         → snapshot (each element gets
                                            a suggested selector)
    - "Did the click open a modal?"      → click already returns
                                            page_state with dialogs
    - "Is the form submitted?"           → wait_for url_pattern or
                                            selector
    - "What's in the search results?"    → extract mode='text' / 'list'
    - "What is this video about?"        → video_context (reads
                                            captions + metadata)
    - "Was section 1 covered?"           → video_context (cues with
                                            timestamps)
    - "Watch this until X is mentioned"  → video_context, then check
                                            captions for X

  ⛔ ANIMATION ANTI-PATTERN:
    Do NOT call vision_query repeatedly to "watch" a moving canvas or
    video. Each call is 5-7 seconds — sampling at 1 fps takes longer
    than the video itself, costs ~1500 input tokens per frame, and
    misses everything between samples. For VIDEO use video_context.
    For CANVAS animation, the data driving it almost always lives in
    a JS variable or network response that DOM extract can reach
    indirectly (chart libraries expose .data on the canvas instance).

  Penalty for misuse: 5-7s latency, ~1500 input tokens, non-deterministic
  hallucinations. snapshot is your default for "what is on this page";
  extract for "give me the content of X"; video_context for any video
  question. vision_query is for image-level questions only.

EXAMPLES
  GOOD: action='launch_browser'
  GOOD: action='navigate', url='https://example.com'
  GOOD: action='snapshot'        (always do this on a new page before guessing selectors)
  GOOD: action='click', selector='text=Sign in'
  GOOD: action='type', selector='input[name=q]', text='hello', press_enter=true
  GOOD: action='wait_for', selector='.results', state='visible'
  GOOD: action='wait_for', url_pattern='/dashboard($|/)'
  GOOD: action='extract', mode='attr', selector='a.next', attribute='href'
  GOOD: action='extract', mode='list', selector='button.action-btn.book', limit=20
  GOOD: action='vision_query', selector='canvas#chart', question='What does this chart show? One sentence.'
  GOOD: action='vision_query', question='Is the person in the photo male or female?',
        selector='img.profile-pic'
  GOOD: action='vision_query', question='Where is the Sign In button? Reply with pixel coordinates.',
        output_schema={"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"}},"required":["x","y"]}
  GOOD: action='video_context'                                          (whole-video summary via captions)
  GOOD: action='video_context', max_cues=2000                           (long lecture)
  GOOD: action='video_context', seek_to_s=150, pause=true               (jump to 2:30 and stop, ready for screenshot)
  GOOD: action='screenshot'                                             (default; auto-cleaned shortly after task)
  GOOD: action='screenshot', path='/abs/path/inside/session/dir/confirm.png'   (long-term keep — write into session working dir)
  GOOD: action='request_user_login', reason='need GitHub credentials',
        success_url_pattern='github\\.com/(?!login)'
  GOOD: action='attach_browser'   (when user said "我刚才打开的" — needs config)
  GOOD: action='new_tab', url='https://...', background=true   (attach mode)
  BAD:  action='type', selector='input[type=password]'  — REFUSED
  BAD:  url='example.com'  — must include scheme
  BAD:  action='navigate' before launch_browser/attach_browser  — no session
  BAD:  selector='SA8797P.HGY.5.1.7.0' — '.5' is a CSS numeric literal,
        use [id='SA8797P.HGY.5.1.7.0'] or text='SA8797P.HGY.5.1.7.0'

LAUNCH vs ATTACH (advanced)
  Default to launch_browser. Use attach_browser ONLY when the task explicitly
  needs the user's RUNNING Chrome state (e.g., "刚才/正在/我现在打开的/接着我那个"):
    - "去 GitHub 看我的 PR review" → launch (just need login state)
    - "把刚才在 Notion 写的草稿发给团队" → attach (needs current Notion tab)
  attach is high-risk: it requires user approval each time and
  browser.attach_enabled: true in handq_config.yaml. New tabs created in
  attach mode default to background=true so the user's focus is preserved.""",
                parameter_schema=BrowserTool.parameter_schema,
                tool_class=BrowserTool,
                on_demand=True,
            )

        # Register DESKTOP tool. Windows-only — uses pyautogui + pywin32 +
        # mss + RapidOCR. on_demand=True so it only enters the LLM tool
        # list when the planner / a context provider explicitly activates
        # it (mirrors the BROWSER pattern).
        if _IS_WINDOWS:
            cls._tools[cls.DESKTOP] = ToolMetadata(
                name=cls.DESKTOP,
                description=(
                    "Windows desktop automation: capture the active window or "
                    "full screen, locate UI elements via local OCR (RapidOCR) "
                    "with optional LLM-vision fallback, and drive the mouse / "
                    "keyboard. Sensitive windows (password managers, banking) "
                    "are refused outright."
                ),
                usage_guide="""\
TOOL CHOICE HIERARCHY (read before EACH desktop call)
  desktop drives OS-level mouse / keyboard — every input action looks
  identical to real human input, steals focus, and is non-deterministic
  (OCR + vision matching, never as exact as a CSS selector). It is the
  most powerful tool in the kit and therefore the most disruptive — use
  it ONLY when no specialised tool fits.

  ✅ desktop is correct when the target is a NATIVE Windows app:
    - Notepad, Excel, Word, PowerPoint, Outlook, OneNote
    - File Explorer, Windows Settings, Task Manager, Control Panel
    - VSCode, Visual Studio, third-party desktop software
    - Anything that lives OUTSIDE a browser window

  ❌ desktop is the WRONG tool for these — use the named alternative:
    - Web pages, URLs, anything in browser  → browser   (DOM is exact,
                                                          ~10x faster than OCR;
                                                          off-screen, no focus theft)
    - Read / write / edit files             → read / write / edit
    - Run commands, scripts, programs       → shell
    - Search files / find pattern           → glob / grep
    - Remote machine                        → ssh
    - Inspect Jupyter notebook cells        → notebook_edit

  ANTI-PATTERN: do NOT use desktop on web pages just because you can see
  them on screen. Even if the user said "click the OK button on
  github.com", that's a browser task — open the page in browser_tool
  and use selectors. Even if the user said "open my Notepad++ at line
  50", it's still desktop because Notepad++ is a native app, but if the
  user said "open the GitHub issue at line 50 of foo.py", that's
  browser. Read the verb-target pair carefully.

  PENALTY for misuse vs the correct tool:
    - 5-10x slower (OCR ~1s + pyautogui PAUSE 50ms vs browser DOM ~50ms)
    - Non-deterministic (OCR fuzzy match vs CSS selector exact)
    - Steals user's actual mouse / keyboard
    - Requires per-task user approval the first time
    - Triggers a visible 'Agent driving' indicator the user CAN revoke
      mid-task with Ctrl+C — over-using desktop trains the user to revoke

WHEN TO USE
  - Tasks that require interacting with NATIVE Windows applications:
    Notepad, File Explorer, Visual Studio, Excel, Outlook, Settings panel,
    third-party desktop apps. The browser tool only covers web pages —
    use desktop for everything outside the browser.
  - Tasks the user phrases as "open <app>", "click <thing on screen>",
    "type <text> into the box", "拖动 / 滚动 / 按 Ctrl+S".

PREREQUISITES (the agent must NOT skip these)
  1. Get the app on screen first: usually a `shell` call to launch it
     (e.g. `notepad.exe`, `explorer.exe path`). Verify with action='list_windows'
     or by asking the user.
  2. Make sure the target window is the FOREGROUND window before any
     mouse / keyboard action. Sensitive-window guard checks the foreground
     before each action and refuses on banking / password manager match.

WORKFLOW (typical)
  1. action='list_windows' — see what is open and which is foregrounded.
  2. action='snapshot' — STRUCTURED listing of every interactable control
     in the foreground window via Windows accessibility tree (UIA). Each
     element comes back with role / text / x / y / selector hint. UIA
     names work even on iconless buttons (gear / refresh / close X) —
     PREFER snapshot over screenshot+OCR 90% of the time. Falls back
     automatically to screenshot+OCR when UIA returns nothing (custom-
     rendered Electron, games). ~100 ms on the UIA path.
  3. **For native Windows apps, drop into pywinauto via shell** — once
     snapshot tells you the target's name / automation_id, drive it
     directly with one shell call instead of click_at + verification
     loops:
        shell: python -c "
        import pywinauto
        app = pywinauto.Application(backend='uia').connect(handle=<HWND>)
        win = app.window(handle=<HWND>)
        win.child_window(title='New Notebook', control_type='Button').click()
        "
     Deterministic, 5-10x faster than the screenshot/click/screenshot
     loop, survives UI shifts. Reach for desktop.click_at only when
     pywinauto cannot reach the target (canvas-rendered subregions,
     custom controls, Electron apps without UIA).
  4. action='screenshot', region='foreground'[, with_ocr=true] — only
     when you actually need the PIXELS (vision_query input, sending the
     image to a vision LLM, debugging). Default to with_ocr=false now
     that snapshot covers the "what's on screen" question with a much
     smaller payload.
  5. action='find_element' / 'find_and_click' — fallback when snapshot
     missed the target or you only have a visual descriptor. Same OCR +
     vision_fallback pipeline as before.
  6. action='hover_at', x, y — TOOLTIP READER. Move cursor to (x, y),
     wait ~800 ms for the Windows tooltip, OCR a 250×120 px region
     around the cursor, return text in 'nearby_text'. Use for
     iconless toolbar buttons that snapshot couldn't name AND no
     visible text label exists. ~1 s.
  7. action='click_at' / 'type_text' / 'drag' / 'scroll' / 'hotkey' /
     'key_press' — drive the input once you have the target.

SCREENSHOT EFFICIENCY
  snapshot is the first move on any new screen — it gives the LLM a
  bounded, structured listing without the OCR-text-blob bloat.
  Reserve screenshot for cases where you need the actual pixels (vision
  LLM input, image debugging). DO NOT screenshot+OCR before every
  action — it is the most common cause of slowness in desktop
  workflows. Re-snapshot only when the UI state has actually changed
  (after a click that opens a menu, after a hotkey, after typing).

DO NOT RE-SCREENSHOT — READ state_after FIRST
  Every input action returns a `state_after` dict that tells you what
  changed on screen WITHOUT another capture. Fields:
    - foreground_title   — current foreground window title
    - foreground_pid     — its PID
    - foreground_changed — bool: did focus move to a different window?
    - title_changed      — bool: same window, but title text changed?
    - new_windows        — list of window titles that appeared since the
                           action (dialogs, popups, toasts)

  Decision rule after every input action:
    1. Read state_after. If foreground_changed=false AND title_changed=
       false AND new_windows=[] → nothing visible changed. Proceed to
       the next action. DO NOT screenshot.
    2. If foreground_changed=true OR new_windows is non-empty → a new
       dialog/window appeared. You likely need its content, so ONE
       screenshot (or snapshot) is justified.
    3. If title_changed=true → the app reacted (e.g. file opened, tab
       switched). Screenshot only if you need to read new content.

  Anti-pattern: click → screenshot → click → screenshot → click →
  screenshot. That is a 4× slowdown over click → click → click. Each
  screenshot is ~200 ms PLUS a full LLM round-trip on the bloated
  output (~3-5 s) — a screenshot you didn't need costs you ~5 s and a
  screen of context every time.

  Correct pattern: click → read state_after → click → read state_after
  → (state says new dialog) → screenshot ONCE → continue.

KEY INVARIANTS
  - Coordinates are PHYSICAL screen pixels. The tool sets per-monitor v2
    DPI awareness on first use so scaling > 100% does not shift coords.
  - find_element returns coords in SCREEN space (already adjusted for
    region origin) — pass them to click_at / scroll directly.
  - region='foreground' captures only the active window. Use this by
    default; region='fullscreen' is needed only for window-switching or
    multi-window comparisons.
  - All input actions queue on a single asyncio lock — no two desktop
    actions run in parallel even if the LLM marks them concurrent_safe.

SENSITIVE WINDOW REFUSAL (HARD)
  Before screenshot / find_element / any input action, the foreground
  window title + process name are matched against
  desktop.sensitive_window_patterns (default covers Bitwarden, 1Password,
  KeePass, LastPass, Dashlane, banking / wallet keywords). On match the
  action is refused and the user must switch focus before retrying.
  This is the desktop analogue of browser_tool's password-field guard.

USER REVOKE (HARD)
  Once the user presses the global revoke hotkey (Ctrl+C while the
  on-screen 'Agent driving' indicator is visible), every input action
  for the rest of this task returns:
    "REFUSED: user revoked desktop control for this task. ..."
  Read-only actions (screenshot / list_windows / find_element) still
  work. Stop using input actions; ask the user whether to continue.

OCR vs VISION FALLBACK
  find_element first asks RapidOCR for every visible text region in the
  capture, then fuzzy-matches description with rapidfuzz token_set_ratio
  (default threshold 70). On match: ~1 s, source='ocr'.
  When OCR misses (visual-only descriptors like "the orange button" /
  "the icon shaped like a gear"), it falls back to a single LLM-vision
  call (~5-7 s, source='vision'). Disable with vision_fallback=false
  when you know the target is plain text.

EXAMPLES
  GOOD: action='list_windows'
  GOOD: action='snapshot'              (FIRST move on any new app window —
                                        UIA tree, no context bloat, names
                                        every iconless button)
  GOOD: action='hover_at', x=720, y=24 (read tooltip on a toolbar icon
                                        when snapshot didn't name it)
  GOOD: shell: python -c "
        import pywinauto
        app = pywinauto.Application(backend='uia').connect(handle=12195718)
        app.window(handle=12195718).child_window(title='New Notebook',
            control_type='Button').click()
        "  (preferred path for native apps once snapshot exposed the name)
  GOOD: action='screenshot', region='foreground'
  GOOD: action='screenshot', region='foreground', with_ocr=true
        — only when you really need pixels + raw OCR; for "what's on
        screen?" prefer snapshot
  GOOD: action='find_element', description='OK button'
  GOOD: action='find_element', description='保存', fuzzy_threshold=80
  GOOD: action='find_element', description='the gear-shaped settings icon',
        vision_fallback=true
  GOOD: action='find_and_click', description='New notebook'
  GOOD: action='find_and_click', description='保存', double=false
  GOOD: action='click_at', x=820, y=412
  GOOD: action='click_at', x=200, y=300, button='right'
  GOOD: action='type_text', text='hello world'
  GOOD: action='hotkey', keys=['ctrl','s']
  GOOD: action='hotkey', keys=['alt','tab']
  GOOD: action='key_press', key='enter'
  GOOD: action='drag', from_x=100, from_y=200, to_x=400, to_y=200, duration=0.5
  GOOD: action='scroll', x=600, y=400, dy=-3   (scroll down 3 clicks)
  BAD:  action='screenshot' as your first move on a native app — use
        snapshot instead (same info, no context bloat, handles
        iconless controls)
  BAD:  click_at without first finding the element / verifying coordinates
        — prefer find_and_click which combines the two
  BAD:  type_text into a window the user hasn't focused (you'll type
        somewhere unexpected — always verify with snapshot first)
  BAD:  type_text with a payload >4000 chars (use clipboard via shell)
  BAD:  using desktop to click a button on a web page — that's a browser
        task. Open the URL in browser_tool and use action='click' with a
        selector.
  BAD:  60-iteration screenshot+click loops. If you're past 15 iterations
        on one step, STOP — switch to pywinauto via shell, or ask the
        user for guidance.""",
                parameter_schema=DesktopTool.parameter_schema,
                tool_class=DesktopTool,
                on_demand=True,
            )

        # Register WEB_SEARCH tool. Windows-only — depends on browser_tool
        # whose Playwright session is Windows-tested. on_demand=True so it
        # only enters the LLM tool list when WebSearchContextProvider
        # activates it (mirrors the browser/desktop pattern).
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

WORKFLOW
  1. Ensure browser is launched. browser.launch_browser is idempotent — if
     in doubt, call it before every web_search.
  2. action='search', source='confluence' (Phase 2 only fully wires this),
     query='free-text or CQL/JQL', limit=10. Default limit 10, hard cap 25
     (clamped from web_search.max_limit in handq_config.yaml).
  3. Hits arrive snippet-truncated to ~300 chars. The agent CHOOSES which
     hit to dig into and calls browser.navigate to read the full document.
     Auto-fetching full bodies here is forbidden — search is for ranking,
     navigation is for reading.
  4. LOGIN RECOVERY: if the result error reads
     '<source> requires login (status=401|403|3xx)':
       a. browser.navigate url='<source's base_url>'
       b. browser.request_user_login reason='auth <source>',
          success_url_pattern='<base_url>'
       c. After user clicks Approve, retry the same search call.
     Cookies persist across HandQ sessions, so this dance is at most once
     per source until cookies expire on the server side.

SOURCES (all four wired)
  - confluence  : qualcomm-confluence.atlassian.net (Atlassian Cloud REST)
                  Query supports CQL ('text ~ "..."', 'space=ENG AND ...')
                  or plain text (auto-wrapped in CQL text~).
  - jira        : jira-dc.qualcomm.com (Jira Data Center REST)
                  Query supports JQL ('project = ANDR AND text ~ "..."')
                  or plain text (auto-wrapped in JQL text~).
  - sharepoint  : qualcomm.sharepoint.com (SharePoint Online Search REST)
                  Plain free-text query — KQL keywords (filetype:pdf,
                  author:"...") work too.
  - orbit       : intranet portal (DOM-extract fallback — no JSON API).
                  Tune web_search.sources.orbit.result_selector in
                  handq_config.yaml when the portal markup shifts.

EXAMPLES
  GOOD: action='search', source='confluence', query='power management release notes'
  GOOD: action='search', source='confluence', query='space=ANDROID AND text ~ "boot trace"', limit=20
  BAD:  action='search', source='confluence', query='everything about X', limit=200
        (hard cap 25)
  BAD:  Use web_search to read a known URL → use browser.navigate
  BAD:  Loop web_search to scrape 100 results → fetch via search once, then
        navigate the top hits agent picks""",
                parameter_schema=WebSearchTool.parameter_schema,
                tool_class=WebSearchTool,
                on_demand=True,
            )

        # Register EMAIL tool. Windows-only — depends on pywin32 (win32com /
        # pythoncom) and a local Outlook MAPI profile. on_demand=True so it
        # only enters the LLM tool list when EmailContextProvider activates it.
        if _IS_WINDOWS:
            cls._tools[cls.EMAIL] = ToolMetadata(
                name=cls.EMAIL,
                description=(
                    "Read Outlook email via local COM automation. "
                    "Reuses the user's MAPI profile — no extra credentials. "
                    "Actions: list_folders, list_messages, read_message, "
                    "search, mark_read, mark_unread, download_attachment."
                ),
                usage_guide="""\
WHEN TO USE
  - Step says "read my email", "show inbox", "翻一下邮箱", "收件箱"
  - User asks who sent them message X, summary of unread, find attachment

WHEN NOT TO USE
  Web mail (Gmail, OWA via browser)   → browser tool
  IMAP/POP3 / Exchange EWS            → not supported here
  Calendar / contacts / tasks         → not in scope

WORKFLOW
  1. action='list_folders'                               (see counts)
  2. action='list_messages' folder='Inbox' [unread_only=true] [limit=20]
       → entry_id + subject + sender + 500-char preview
  3. action='read_message' entry_id='...' [include_full_body=true]
       → full body only when needed (LLM context budget)
  4. action='search' query='...' [folder='Inbox']
  5. action='download_attachment' entry_id='...' attachment_name='file.pdf'
       → sandboxed to %USERPROFILE%\\HandQ\\email_attachments\\

KEY INVARIANTS
  - body_preview always 500 chars; include_full_body=true for full text
  - Outlook stays open — the tool never calls app.Quit()
  - No write actions (compose_draft / send) in this phase
  - output_dir outside sandbox → refused (path-traversal guard)

EXAMPLES
  GOOD: action='list_folders'
  GOOD: action='list_messages', folder='Inbox', unread_only=true, limit=20
  GOOD: action='read_message', entry_id='000000007FAB...', include_full_body=true
  GOOD: action='search', query='qprof ddr', folder='Inbox', limit=10
  GOOD: action='download_attachment', entry_id='...', attachment_name='spec.pdf'
  BAD:  action='send' — not in this phase
  BAD:  output_dir='C:\\Windows\\System32' — refused by sandbox guard
  BAD:  include_full_body=true on 50 messages — context overflow""",
                parameter_schema=EmailTool.parameter_schema,
                tool_class=EmailTool,
                on_demand=True,
            )

        # Register TEAMS tool. Windows-only — registered alongside email so
        # the Linux planner never sees it (consistent with the desktop /
        # browser / email pattern). on_demand=True; activated via
        # TeamsContextProvider when the planner declares "teams". Depends on
        # httpx + playwright (both already required); missing deps surface a
        # clear "install X" message via TeamsContextProvider.prepare().
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

WORKFLOW
  Always discover identifiers BEFORE send / create operations:
    list_chats   → chat_id        → send_chat
    list_teams   → team_id        → list_channels → channel_id → send_channel
    find_person  → emails[] / id  → create_meeting attendees / send_chat
    list_calendar_events → event_id → respond_event / get_event

KEY INVARIANTS
  - top capped at 50 per call; paginate for older history
  - message_html: HTML or plain text, 32 KB cap per message
  - send_* / create_meeting / respond_event NOT undoable
  - 401 mid-task triggers automatic re-bootstrap (3-5s when cookie warm)
  - Bootstrap requires browser_profile to be free; close any running
    browser_tool action first if 'profile_locked' is reported
  - Do NOT shell-search the token cache file; the tool owns it

EXAMPLES
  GOOD: action='list_calendar_events', top=10
  GOOD: action='create_meeting', subject='Spec review',
        start='2026-06-05T15:00:00', end='2026-06-05T15:30:00',
        time_zone='China Standard Time',
        attendees=[{"email":"alice@x.com","name":"Alice"}]
  GOOD: action='respond_event', event_id='AAMkAG...', response='accept'
  GOOD: action='find_person', query='zhang san'
  GOOD: action='list_chats', top=20  →  pick chat_id  →  read_chat
  BAD:  send_chat without first running list_chats — chat_id is opaque
  BAD:  Driving teams.microsoft.com via browser when teams tool covers it""",
                parameter_schema=TeamsTool.parameter_schema,
                tool_class=TeamsTool,
                on_demand=True,
            )

        # Register ASK_HUMAN tool. Windows-only — relies on the GUI bridge to
        # render the modal and capture the reply. Linux/CLI runtimes use the
        # IM's stderr+stdin fallback, but the official surface is the Electron
        # UI. on_demand=True so it only enters the LLM tool list when
        # AskHumanContextProvider activates it (mirrors browser/desktop
        # pattern). Toggleable via the tool_ask_human interaction switch.
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
    have, AND (b) cannot derive by reading the project, asking the planner
    via your reasoning, or making a sensible default choice that is easy
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
    def create_tool_instance(cls, name: str) -> BaseTool:
        """
        Create an instance of a tool

        Args:
            name: Tool name

        Returns:
            Instance of the tool
        """
        metadata = cls.get_tool_metadata(name)
        return metadata.create_instance()

    @classmethod
    def create_all_tool_instances(cls, venv_path: Optional[str] = None, extra_tool_names: Optional[List[str]] = None) -> Dict[str, BaseTool]:
        """
        Create instances of all registered tools.

        Args:
            venv_path: Optional path to a virtual environment root.  When set,
                       all bash commands run inside that venv (PATH is prepended
                       with the venv bin directory and VIRTUAL_ENV is set),
                       equivalent to sourcing activate before each command.
            extra_tool_names: Optional list of on-demand tool names to include.
                              On-demand tools are excluded by default and only
                              activated when a StepContextProvider requests them.

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
                instances[name] = ShellTool(venv_path=venv_path)
            elif name == cls.SSH:
                instances[name] = StatelessSSHTool()
            elif name == cls.SESSION:
                instances[name] = metadata.create_instance()
            else:
                instances[name] = metadata.create_instance()
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
    def generate_system_prompt_tools_section(cls) -> str:
        """
        Generate the tools section for the system prompt.

        Each tool entry includes:
          - Name and one-line description
          - Detailed usage guide (when to use, when not to, strategy, examples)
          - Parameter list with descriptions

        Returns:
            Formatted string describing all available tools
        """
        cls.initialize()

        lines: List[str] = [
            "## Available Tools",
            "",
            "You have access to the following tools. Select the right tool for each action.",
            "",
        ]

        for name, metadata in cls._tools.items():
            lines.append(f"---")
            lines.append(f"### `{name}` — {metadata.description}")
            lines.append("")

            if metadata.usage_guide:
                lines.append(metadata.usage_guide)
                lines.append("")

            # Parameter list
            props = metadata.parameter_schema.get("properties", {})
            required = metadata.parameter_schema.get("required", [])
            if props:
                lines.append("**Parameters**:")
                for param_name, param_info in props.items():
                    req_marker = " *(required)*" if param_name in required else " *(optional)*"
                    param_desc = param_info.get("description", "")
                    lines.append(f"  - `{param_name}`{req_marker}: {param_desc}")
                lines.append("")

        return "\n".join(lines).strip()

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
