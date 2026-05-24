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
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .notebook_edit_tool import NotebookEditTool
from .browser_tool import BrowserTool

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
    BROWSER = "browser"

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
  - Read multiple related files together (pass as array) to understand context

When NOT to Use:
  - When you already have the file content from a previous read in this session
    (re-reading the same unchanged file wastes context budget)
  - When you only need to check if a file exists (use bash: if exist "path" echo yes)
  - When you need to search for a pattern across files (use bash: findstr /s "pattern" *.py)
  - Files > 100 KB: use bash (type with findstr, or powershell Get-Content/Select-String)
    to extract relevant sections

Strategy:
  - Batch related reads: pass an array of paths to read multiple files in one call
  - For large files, read targeted sections with bash rather than the full file
  - Context budget: each read result is appended to your conversation history and
    cannot be removed. Read what you need — avoid reading files you won't act on.
  - Prefer reading the most specific file first; broaden only if needed

Examples:
  GOOD: {"path": ["src/main.py", "src/utils.py"]}  — batch read, one call
  GOOD: {"path": "config/settings.yaml"}            — single targeted read
  BAD:  Read the same file twice without changes in between
  BAD:  Read an entire 2000-line file when you only need one function
        → use bash: findstr /n "def target_function" file.py, then read specific lines"""
        else:
            _read_usage_guide = """\
When to Use:
  - Examine file contents you haven't seen yet in this session
  - Read directory structure to understand project layout
  - Read multiple related files together (pass as array) to understand context

When NOT to Use:
  - When you already have the file content from a previous read in this session
    (re-reading the same unchanged file wastes context budget)
  - When you only need to check if a file exists (use bash: test -f <path>)
  - When you need to search for a pattern across files (use bash: grep -r)
  - Files > 100 KB: use bash (head/tail/sed/grep) to extract relevant sections

Strategy:
  - Batch related reads: pass an array of paths to read multiple files in one call
  - For large files, read targeted sections with bash rather than the full file
  - Context budget: each read result is appended to your conversation history and
    cannot be removed. Read what you need — avoid reading files you won't act on.
  - Prefer reading the most specific file first; broaden only if needed

Examples:
  GOOD: {"path": ["src/main.py", "src/utils.py"]}  — batch read, one call
  GOOD: {"path": "config/settings.yaml"}            — single targeted read
  BAD:  Read the same file twice without changes in between
  BAD:  Read an entire 2000-line file when you only need one function
        → use bash: grep -n "def target_function" file.py, then read specific lines"""

        cls._tools[cls.READ] = ToolMetadata(
            name=cls.READ,
            description=(
                "Read one or more files or directories. "
                "Supports a single path, a list of paths, or both simultaneously. "
                "For a single path the result is returned directly; "
                "for multiple paths a summary with per-path results is returned. "
                "Supports PDF files (requires PyPDF2, pdfplumber, or pymupdf). "
                "Files larger than 100 KB cannot be read directly."
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
                    "start_line": {
                        "type": "integer",
                        "description": "1-based line number to start reading from (inclusive)"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "1-based line number to stop reading at (inclusive)"
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
Platform: Windows. Default shell: PowerShell (pwsh 7+ / powershell.exe).
Use the 'shell' parameter to select: "powershell" (default), "cmd", or "bash" (Git Bash).

Working Directory: Each command runs in the agent's project directory by default.
Use 'cd subdir && cmd' within a single command for subdirectory operations.
Shell state (variables, functions) does NOT persist between calls — each call runs
in a fresh process.

When to Use:
  - Run programs, scripts, tests, or build commands
  - Search for patterns, check system state, verify results
  - Process text, install packages or manage dependencies
  - Long-running tasks: use run_in_background=true

When NOT to Use:
  - Reading file contents — use read instead (cleaner, no shell escaping issues)
  - Interactive commands that will hang (see Forbidden Commands below)

Forbidden Commands (will hang — tool runs non-interactively):
  - Read-Host, Get-Credential, Out-GridView, $Host.UI.PromptForChoice, pause
  - git rebase -i, git add -i, or any command that opens an interactive editor
  - Destructive cmdlets without -Confirm:$false may prompt and hang

PowerShell Syntax (default):
  • List files: Get-ChildItem (aliases: ls, dir)
  • Find files: Get-ChildItem -Recurse -Filter *.ext
  • Search text: Select-String -Pattern "x" -Path *.py (or -Recurse)
  • Check path: Test-Path "path"
  • Remove file: Remove-Item "path"
  • Remove dir: Remove-Item -Recurse -Force -Confirm:$false "path"
  • Print file: Get-Content "path"
  • Current dir: Get-Location (alias: pwd)
  • Move/rename: Move-Item
  • Copy: Copy-Item
  • Env vars: read with $env:NAME, set with $env:NAME = "value"
  • Path separator: \\ (forward slashes also work)
  • Null device: $null (NOT /dev/null)
  • Python: python (NOT python3)
  • Command chaining: cmd1 && cmd2 (stop on fail), cmd1; cmd2 (always both)
  • String interpolation: "Hello $name" or "Value: $($obj.Property)"
  • Pipeline: passes objects, not text — use Select-Object, Where-Object, ForEach-Object
  • Ternary: $condition ? $true_val : $false_val
  • Null-coalescing: $var ?? "default"
  • Null-conditional: $obj?.Property

Destructive Cmdlet Safety:
  Destructive cmdlets (Remove-Item, Stop-Process, Clear-Content) may prompt for
  confirmation. Add -Confirm:$false when you intend the action to proceed.
  Use -Force for read-only or hidden items.

Multiline Strings (Here-Strings):
  Use single-quoted here-strings for literal content (no variable expansion):
    git commit -m @'
    Commit message here.
    Second line with $literal dollar signs.
    '@
  CRITICAL: Closing '@ MUST be at column 0 (no leading whitespace) on its own line.
  Use @"..."@ (double-quoted) only when you need variable expansion.

Stop-Parsing Token:
  For arguments containing -, @, or other PowerShell operators:
    git log --% --format=%H

Registry Access:
  Use PSDrive prefixes (NOT raw paths):
  ✓ Get-ItemProperty HKLM:\\SOFTWARE\\...
  ✓ Get-ItemProperty HKCU:\\...
  ✗ HKEY_LOCAL_MACHINE\\... (will fail)

Exit Code Handling:
  -ErrorAction SilentlyContinue suppresses error output but the tool still reports
  exit 1. For truly non-fatal errors, wrap in try-catch:
    try { Cmdlet ... -ErrorAction Stop } catch { }

Unix → PowerShell Equivalents (Do NOT use Unix commands without shell="bash"):
  head -N file         → Get-Content file -TotalCount N
  tail -N file         → Get-Content file -Tail N
  head (piped)         → | Select-Object -First N
  tail (piped)         → | Select-Object -Last N
  which cmd            → (Get-Command cmd).Source
  touch path           → if (-not (Test-Path path)) { New-Item -ItemType File path }
  wc -l file           → (Get-Content file | Measure-Object -Line).Lines
  mkdir -p dir         → New-Item -ItemType Directory -Force dir
  rm -rf dir           → Remove-Item -Recurse -Force dir
  ln -s target link    → New-Item -ItemType SymbolicLink -Path link -Target target
  2>/dev/null          → 2>$null
  VAR=x cmd            → $env:VAR = 'x'; cmd
  if [ -f x ]          → if (Test-Path x) { ... }
  for x in *           → foreach ($x in ...) { ... }
  `cmd` (backtick)     → $(cmd)
  chmod/chown          → icacls (Windows ACL model)

cmd.exe alternative (use shell="cmd"):
  • List: dir /s /b
  • Search: findstr /s /i "pattern" *.py
  • Chaining: cmd1 && cmd2
  • Env vars: %VAR%

Background Execution:
  Set run_in_background=true for long-running commands (tests, builds, servers).
  Returns a task_id immediately. You will be notified when it completes.
  No timeout limit for background tasks (foreground: 600s max).
  Use task_id="..." to query status.
  Use task_id="...", command="kill" to terminate.

Output Handling:
  - Output is truncated at 30,000 characters (head 10k + tail 5k + notice)
  - Background tasks: output capped at 30,000 bytes per task
  - Use Select-String or Select-Object -First N to filter proactively

Git Security:
  NEVER use --no-verify to skip hooks or --no-gpg-sign to bypass signing
  unless explicitly requested. If a hook fails, investigate the underlying issue.

Strategy:
  - Always use non-interactive flags: --yes, -y, --no-input, -Confirm:$false
  - Check exit codes: zero = success; non-zero = failure
  - Limit output: pipe to Select-Object -First N or Select-String to filter
  - For long-running commands, use run_in_background=true
  - Context budget: large outputs consume context. Filter aggressively.

Python as a power tool (PREFERRED for complex operations):
  Python is available and should be your first choice when:
  - Processing structured data (JSON, YAML, CSV, XML)
  - Complex text manipulation across multiple files
  - Any logic requiring conditionals, loops, or error handling
  - Anything that would require more than 2 piped commands

Examples:
  GOOD: Get-ChildItem -Recurse -Filter *.py | Select-String 'pattern'
  GOOD: python -m pytest tests/test_core.py -x -q
  GOOD: {"command": "npm test", "run_in_background": true, "description": "Running tests"}
  GOOD: {"task_id": "bg_1"} — check background task status
  GOOD: {"task_id": "bg_1", "command": "kill"} — kill background task
  BAD:  grep -rn "pattern" src/   — Unix command, will fail on PowerShell
  BAD:  find . -name "*.py"       — Unix; use Get-ChildItem -Recurse
  BAD:  Read-Host "prompt"        — will hang (non-interactive)"""
        else:
            _shell_usage_guide = """\
Platform: Linux/macOS. Default shell: /bin/sh.
Use the 'shell' parameter to select: "sh" (default), "bash", "zsh".

Working Directory: Each command runs in the agent's project directory by default.
Use 'cd subdir && cmd' within a single command for subdirectory operations.
Shell state (variables, functions) does NOT persist between calls — each call runs
in a fresh process.

When to Use:
  - Run programs, scripts, tests, or build commands
  - Search for patterns: grep, find, rg (ripgrep)
  - Check system state: ls, ps, df, env, which
  - Verify results: run tests, check syntax, list outputs
  - Process text: awk, sed, sort, uniq, wc
  - Long-running tasks: use run_in_background=true

When NOT to Use:
  - Reading file contents — use read instead (cleaner, no shell escaping issues)
  - Interactive commands: git rebase -i, git add -i, editors (vi, nano)
  - Commands that prompt for input without a --yes/-y flag

Background Execution:
  Set run_in_background=true for long-running commands (tests, builds, servers).
  Returns a task_id immediately. You will be notified when it completes.
  No timeout limit for background tasks (foreground: 600s max).
  Use task_id="..." to query status.
  Use task_id="...", command="kill" to terminate.

Output Handling:
  - Output is truncated at 30,000 characters (head 10k + tail 5k + notice)
  - Background tasks: output capped at 30,000 bytes per task
  - Use head/tail/grep to filter proactively

Scope Warning:
  Search commands (grep, find, rg) must target the working directory or subdirectories.
  Searching / or ~ can hang on large directory trees — always scope to '.' or a subdir.

Git Security:
  NEVER use --no-verify to skip hooks or --no-gpg-sign to bypass signing
  unless explicitly requested. If a hook fails, investigate the underlying issue.

Strategy:
  - Always use non-interactive flags: --yes, -y, --no-input, --force, -f,
    DEBIAN_FRONTEND=noninteractive
  - Check exit codes: zero = success; non-zero = failure
  - Limit output: command | head -100, command | tail -50, command | grep ERROR
  - For long-running commands, use run_in_background=true
  - Chain commands with && to stop on first failure
  - Context budget: large outputs consume context. Filter aggressively.

Python as a power tool (PREFERRED for complex operations):
  Python is available and should be your first choice when:
  - Processing structured data (JSON, YAML, CSV, XML)
  - Complex text manipulation across multiple files
  - Any logic requiring conditionals, loops, or error handling
  - Anything that would require more than 2-3 piped shell commands

Examples:
  GOOD: grep -rn "def process_batch" src/ | head -20
  GOOD: python -m pytest tests/test_core.py -x -q 2>&1 | tail -30
  GOOD: find . -name "*.py" -newer requirements.txt | head -20
  GOOD: {"command": "npm test", "run_in_background": true, "description": "Running tests"}
  GOOD: {"task_id": "bg_1"} — check background task status
  GOOD: {"task_id": "bg_1", "command": "kill"} — kill background task
  BAD:  cat large_file.log  — use read or grep instead
  BAD:  git rebase -i HEAD~3  — interactive, will hang
  BAD:  Running a command that hangs waiting for user input"""

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
        cls._tools[cls.SSH] = ToolMetadata(
            name=cls.SSH,
            description=(
                "Stateless SSH tool. Each action opens a fresh connection and closes it "
                "when done — no persistent session state. "
                "SECURITY: hostname, username, password, and key_path are read from a local "
                "credentials file at runtime; only the file path is passed through the LLM."
            ),
            usage_guide="""\
SECURITY: Pass credentials_file (a local YAML/JSON path) — never hostname/username/password directly.
Credentials file format:
  hostname: 192.168.1.100
  username: user
  key_path: ~/.ssh/id_rsa      # optional; tried before password
  password: secret             # optional; used when key auth fails
  keyring_service: myapp       # RECOMMENDED on shared machines: fetch password from OS keyring
                               # (Windows Credential Manager / Linux Secret Service / macOS Keychain)
                               # Store once: python handq_keyring.py set myapp user
                               # Password never written to any file on disk.

Login shell detection:
  The first action='exec' call returns a 'login_shell' field ("bash"/"tcsh"/"zsh"/"sh"/"unknown").
  A 'shell_warning' field is added when the shell is not bash.
  Built-in actions (exec_bg, job_status, safe_exit) wrap ALL commands in 'bash -c' internally
  and work correctly regardless of login shell.
  For action='exec' with your own commands on a non-bash host, wrap them:
    command='bash -c "your_command_here"'

Actions:
  exec        Run a short command; return stdout/stderr/exit_code/login_shell.
              Required: credentials_file, command.
              Optional: workdir, timeout (30).

  exec_bg     Launch a long-running command as nohup background process.
              Required: credentials_file, command.
              Optional: job_id, log_path, pid_file, workdir, timeout (30).
              Returns: job_id, pid, pid_file, log_path, exit_file.
              Returns success=False with actionable error if PID capture fails.

  job_status  Poll a background job (call this externally on an interval).
              Required: credentials_file, pid_file, log_path.
              Optional: exit_file, tail_lines (50), timeout (15).
              Returns: status ("running"|"done"|"unknown"), exit_code,
                       total_lines, log_tail (only when done), error_summary (only when done+nonzero).
              POLLING RULES:
                • When status="running" the output has NO log_tail field — it is
                  omitted to keep polling calls slim.  Do NOT try to read log_tail
                  from a RUNNING result; wait until status="done".
                • Recommended poll interval: 30–900 s depending on expected job
                  duration.  Polling faster than 30 s wastes context with no benefit.
                • PREFER wait_done over job_status for long tasks — it uses a
                  single SSH connection instead of one per poll.

  wait_done   PREFERRED for long tasks: block inside a SINGLE SSH connection
              until the background job finishes, then return the result.
              Eliminates repeated job_status polling (each poll = new connection).
              The remote host runs a sleep loop; Python waits for it to return.
              SSH keepalive packets prevent NAT/firewall from dropping the idle
              connection during the wait.
              Required: credentials_file, pid_file, log_path.
              Optional: exit_file, timeout (300), poll_interval (5), tail_lines (50).
              Returns: status ("done"|"timeout"), exit_code, log_tail,
                       total_lines, error_summary, waited_seconds.
              USE THIS instead of job_status when you don't need to do other work
              while the remote job runs.  Fall back to job_status only when you
              need to interleave local work with remote polling.

  tail_log    Read the last N lines of a remote log file.
              Required: credentials_file, log_path.
              Optional: lines (100), pattern (grep -E filter), timeout (15).

  fetch_log   Page through a large log file by line range.
              Required: credentials_file, log_path.
              Optional: start_line (1), end_line (start+199), timeout (15).
              USE THIS to debug failures in large logs.

  write_file  Upload inline string content to a remote path via SFTP.
              Required: credentials_file, remote_path, content.

  run_script  HIGH-LEVEL: write_file → chmod +x → exec_bg in one call.
              Required: credentials_file, script_content.
              Optional: script_name, job_id, workdir, timeout_hint_seconds.
              Returns: job_id, pid, pid_file, log_path, exit_file, script_remote_path.
              USE THIS for long-running remote scripts.

  safe_exit   Kill all nohup jobs tracked under ~/handq_jobs/ and remove pid files.
              Required: credentials_file. Optional: timeout (15).
              ALWAYS call this when done.

Recommended workflow for long-running remote scripts:
  1. ssh(exec)            — verify workdir and environment; check login_shell field
  2. ssh(run_script)      — upload and launch the script as nohup background
  3. ssh(wait_done)       — PREFERRED: single connection, blocks until job finishes
                            (set timeout to expected job duration + buffer)
     — OR —
     ssh(job_status)      — poll externally every 30–60 s until status != "running"
                            (use when you need to interleave local work during the wait)
  4. ssh(tail_log)        — inspect output on success, or
     ssh(fetch_log)       — page through large logs to debug failures
  5. ssh(safe_exit)       — clean up all jobs
  6. write report to local file""",
            parameter_schema=StatelessSSHTool().parameter_schema,
            tool_class=StatelessSSHTool,
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
