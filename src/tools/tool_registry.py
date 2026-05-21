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
from .bash_tool import BashTool
from .ssh_tool import StatelessSSHTool

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
    BASH = "bash"
    SSH  = "ssh"

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
                "old_content must match the file exactly, including whitespace and indentation."
            ),
            usage_guide="""\
When to Use:
  - Make targeted changes to specific parts of an existing file
  - Change a function, a few lines, or a section without rewriting the whole file
  - Fix a bug, update a value, or refactor a specific block

When NOT to Use:
  - Creating a new file — use write instead
  - Changing more than ~50% of the file — write the whole file instead
  - When old_content appears multiple times (edit replaces the FIRST match only;
    verify uniqueness before using)

Strategy:
  - Include 3-5 lines of context around the change in old_content to ensure uniqueness
  - old_content must match EXACTLY: same whitespace, indentation, and line endings
  - For multiple changes to the same file, make separate edit calls in sequence
  - After editing, re-read the modified section to verify the change is correct
  - If the edit fails (old_content not found), re-read the file first to get the
    current exact content, then retry

Examples:
  GOOD: old_content includes the full function signature + 2 lines before/after
  GOOD: old_content is a unique 5-line block that appears exactly once
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
                            "to ensure uniqueness. Replaces the FIRST match."
                        )
                    },
                    "new_content": {
                        "type": "string",
                        "description": "New content to replace old_content with"
                    }
                },
                "required": ["path", "old_content", "new_content"],
                "additionalProperties": False
            },
            tool_class=EditTool
        )

        # Register BASH tool
        _bash_description = (
            "Execute a shell command and return its stdout, stderr, and exit code. "
            "Use for running programs, scripts, searches, system operations, "
            "and verifying results."
        )
        if _IS_WINDOWS:
            _bash_usage_guide = """\
Platform: Windows. Default shell: cmd.exe.
Use the 'shell' parameter to select: "cmd" (default), "powershell", or "bash" (Git Bash).

When to Use:
  - Run programs, scripts, tests, or build commands
  - Search for patterns, check system state, verify results
  - Process text, install packages or manage dependencies

When NOT to Use:
  - Reading file contents — use read instead (cleaner, no shell escaping issues)
  - When the command requires interactive input without a non-interactive flag

CRITICAL — Command Syntax (cmd.exe default):
  • List files: dir (NOT ls)
  • Find files: dir /s /b *.ext (NOT find . -name)
  • Search text: findstr /s /i "pattern" *.py (NOT grep)
  • Check path exists: if exist "path" (echo yes) (NOT test -f)
  • Remove file: del /f "path" (NOT rm)
  • Remove dir: rmdir /s /q "path" (NOT rm -rf)
  • Print file: type "path" (NOT cat)
  • Current dir: cd (NOT pwd)
  • Move/rename: move (NOT mv)
  • Copy: copy (NOT cp)
  • Env vars: %VAR% (NOT $VAR)
  • Path separator: \\ (NOT /)
  • Null device: NUL (NOT /dev/null)
  • Python: python (NOT python3)
  • Command chaining: cmd1 && cmd2 (stop on fail), cmd1 & cmd2 (always both)

PowerShell alternative (use shell="powershell"):
  • List: Get-ChildItem (aliases: ls, dir)
  • Search: Select-String -Pattern "x" -Path *.py
  • Find: Get-ChildItem -Recurse -Filter *.ext
  • Test path: Test-Path "path"
  • Remove: Remove-Item -Recurse -Force "path"
  • Print: Get-Content "path"
  • Env vars: $env:VAR

Do NOT use Unix commands (ls, grep, find, cat, rm, test, chmod, mv, cp, head, tail,
awk, sed, wc, etc.) unless you specify shell="bash" (requires Git Bash installed).

Strategy:
  - Always use non-interactive flags for commands that might prompt:
      --yes, -y, --no-input, --force, -f
  - Check exit codes: zero = success; non-zero = failure
  - Limit output: use findstr to filter, or pipe to powershell Select-Object -First N
  - For long-running commands, add a timeout or run in background if appropriate
  - Context budget: large outputs consume context. Filter aggressively.

Python as a power tool (PREFERRED for complex operations):
  Python is available via this tool and should be your first choice when:
  - Processing structured data (JSON, YAML, CSV, XML) — always prefer python over
    fragile shell parsing with findstr/for loops
  - Complex text manipulation across multiple files
  - Any logic requiring conditionals, loops, or error handling
  - File system operations on many files (batch rename, filter, transform)
  - Calculations, data aggregation, or report generation
  - Anything that would require more than 2 piped commands

  Patterns:
    • Quick one-liner: python -c "import json; print(json.load(open('x.json'))['key'])"
    • Multi-step: write a .py script with the write tool, then run it with bash
    • For complex tasks, prefer writing a script (easier to debug than long one-liners)

  Available: Python standard library is always available. Common third-party packages
  (requests, pyyaml, etc.) may also be installed — try importing before assuming unavailable.

Examples:
  GOOD: findstr /s /n "def process_batch" *.py
  GOOD: python -m pytest tests/test_core.py -x -q
  GOOD: dir /s /b *.py | findstr "test_"
  GOOD: python -c "import os; [print(f) for f in os.listdir('.') if f.endswith('.py')]"
  GOOD: python -c "import json,sys; d=json.load(open('config.json')); d['version']='2.0'; json.dump(d,open('config.json','w'),indent=2)"
  GOOD: {"command": "Get-ChildItem -Recurse -Filter *.py | Select-String 'pattern'", "shell": "powershell"}
  BAD:  grep -rn "pattern" src/   — Unix command, will fail on cmd.exe
  BAD:  find . -name "*.py"       — Unix command, will fail on cmd.exe
  BAD:  cat large_file.log        — Unix command; use type or read tool instead
  BAD:  Complex for /f loops to parse JSON — use python instead"""
        else:
            _bash_usage_guide = """\
Platform: Linux. Default shell: /bin/sh.

When to Use:
  - Run programs, scripts, tests, or build commands
  - Search for patterns: grep, find, rg (ripgrep)
  - Check system state: ls, ps, df, env, which
  - Verify results: run tests, check syntax (python -m py_compile), list outputs
  - Process text: awk, sed, sort, uniq, wc
  - Install packages or manage dependencies

When NOT to Use:
  - Reading file contents — use read instead (cleaner, no shell escaping issues)
  - When the command requires interactive input without a non-interactive flag

Strategy:
  - Always use non-interactive flags for commands that might prompt:
      --yes, -y, --no-input, --force, -f, DEBIAN_FRONTEND=noninteractive
  - Check exit codes: a zero exit code means success; non-zero means failure
  - Limit output size to avoid consuming context budget:
      command | head -100        — first 100 lines
      command | tail -50         — last 50 lines
      command 2>&1 | grep ERROR  — filter to relevant lines
  - For long-running commands, add a timeout or run in background if appropriate
  - Chain commands with && to stop on first failure: cmd1 && cmd2 && cmd3
  - Context budget: large command outputs (e.g., full test suite logs) consume
    significant context. Filter output aggressively with grep/head/tail.

Python as a power tool (PREFERRED for complex operations):
  Python is available via this tool and should be your first choice when:
  - Processing structured data (JSON, YAML, CSV, XML) — always prefer python over
    fragile shell pipelines with awk/sed/jq
  - Complex text manipulation across multiple files
  - Any logic requiring conditionals, loops, or error handling
  - File system operations on many files (batch rename, filter, transform)
  - Calculations, data aggregation, or report generation
  - Anything that would require more than 2-3 piped shell commands

  Patterns:
    • Quick one-liner: python3 -c "import json; print(json.load(open('x.json'))['key'])"
    • Multi-step: write a .py script with the write tool, then run it with bash
    • For complex tasks, prefer writing a script (easier to debug than long one-liners)

  Available: Python standard library is always available. Common third-party packages
  (requests, pyyaml, etc.) may also be installed — try importing before assuming unavailable.

Examples:
  GOOD: grep -rn "def process_batch" src/ | head -20
  GOOD: python -m pytest tests/test_core.py -x -q 2>&1 | tail -30
  GOOD: find . -name "*.py" -newer requirements.txt | head -20
  GOOD: python3 -c "import os; [print(f) for f in os.listdir('.') if f.endswith('.py')]"
  GOOD: python3 -c "import json,sys; d=json.load(open('config.json')); d['version']='2.0'; json.dump(d,open('config.json','w'),indent=2)"
  BAD:  cat large_file.log  — use read or grep instead
  BAD:  pip install package  (without -q or output filtering)
        → use: pip install package -q && echo "installed"
  BAD:  Running a command that hangs waiting for user input
  BAD:  Complex awk/sed pipelines to parse JSON — use python instead"""

        cls._tools[cls.BASH] = ToolMetadata(
            name=cls.BASH,
            description=_bash_description,
            usage_guide=_bash_usage_guide,
            parameter_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Shell command to execute. "
                            "Use non-interactive flags for commands that might prompt. "
                            + (
                                "Use cmd.exe syntax by default on Windows. "
                                "Specify shell=\"powershell\" or shell=\"bash\" for alternatives. "
                                if _IS_WINDOWS else
                                "Pipe through head/tail/grep to limit output size. "
                            )
                            + "IMPORTANT: grep/find/search must be scoped to the working "
                            "directory ('.' or a subdirectory) — never to parent "
                            "directories or filesystem root."
                        )
                    },
                    "concurrent_safe": {
                        "type": "boolean",
                        "description": (
                            "Set to true when this command is read-only and safe "
                            "to run concurrently with other commands in the same "
                            "response (e.g. search, list, read, python -c). "
                            "Set to false (or omit) for commands that write files, "
                            "modify state, or have side effects. "
                            "Default: false (serialised)."
                        )
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Maximum seconds to wait for the command to complete. "
                            "Default: 120. Maximum: 300 seconds."
                        )
                    },
                    "shell": {
                        "type": "string",
                        "description": (
                            "Shell to use for execution. "
                            + (
                                "Options: \"cmd\" (default), \"powershell\"/\"pwsh\", \"bash\" (Git Bash). "
                                "Use \"powershell\" for advanced scripting; \"bash\" only if Git Bash is installed."
                                if _IS_WINDOWS else
                                "Options: \"sh\" (default), \"bash\", \"zsh\". "
                                "Usually omit to use the default."
                            )
                        )
                    },
                },
                "required": ["command"],
                "additionalProperties": False
            },
            tool_class=BashTool
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
            if name == cls.BASH:
                instances[name] = BashTool(venv_path=venv_path)
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
