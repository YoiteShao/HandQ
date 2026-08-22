# HandQ

HandQ is a desktop AI agent designed to autonomously execute complex tasks across your local environment.

Instead of acting as a simple chat interface or a collection of isolated tools, HandQ gives an agent ownership of a task. The agent decides how to decompose the task, which tools to use, when to ask for help, and when the task is complete.

HandQ combines LLM reasoning with local files, shell execution, browser automation, desktop interaction, SSH, persistent memory, Skills, scheduling, and multi-session execution.

## Highlights

* **Autonomous task execution**
  Give HandQ a goal and let the agent decide how to accomplish it. Task decomposition and tool selection are handled by the agent itself.

* **Local-first desktop agent**
  Runs as a native desktop application with an Electron frontend and Python agent runtime.

* **Browser automation**
  Each session can use an isolated Chromium profile, allowing multiple sessions to operate independently.

* **Desktop automation**
  HandQ can interact with the local desktop through mouse, keyboard, screenshots, UI Automation, and vision-based element detection.

* **Local file and shell access**
  Read, write, edit, search, execute commands, inspect repositories, and automate development workflows.

* **SSH and remote execution**
  Connect to remote Linux machines and run tasks through SSH. Linux machines can also run a lightweight Sub-HandQ daemon controlled remotely by a Windows HandQ instance.

* **Persistent memory**
  Long-term memory allows HandQ to retain useful information across sessions. Memory retrieval uses a fast tier for interactive context and a precise tier for task-specific recall.

* **Skills**
  Skills are reusable task recipes that the agent can discover and activate when needed. Users can create and manage their own Skills.

* **Scheduled tasks**
  Natural-language scheduling and persistent background tasks allow HandQ to execute recurring workflows automatically.

* **Multi-session execution**
  Multiple sessions can run concurrently within a single bridge process. Browser, filesystem, SSH, and agent state are isolated per session.

* **Vision and local OCR**
  Browser and desktop workflows can use screenshots, vision models, and local RapidOCR for visual understanding.

* **Risk-aware tool execution**
  Potentially dangerous operations can be intercepted by the interaction layer and require explicit user confirmation.

## Architecture

HandQ consists of three major layers:

```text
┌──────────────────────────────────────────────────────┐
│                    HandQ Desktop                     │
│                  Electron + Renderer                 │
└──────────────────────────┬───────────────────────────┘
                           │ JSON / stdio IPC
                           ▼
┌──────────────────────────────────────────────────────┐
│                    Python Bridge                     │
│                  StdioBridge / IPC                   │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│                  FlowControllerV2                    │
│                                                      │
│  Orchestrator                                       │
│       │                                              │
│       ▼                                              │
│  TaskChannel  ───────────────► PersistentAgent       │
│                                      │               │
│                                      ▼               │
│                               Tool Registry          │
│                                      │               │
│             ┌────────────────────────┼───────────┐  │
│             ▼            ▼           ▼           ▼  │
│           Files        Shell      Browser      Desktop
│             │            │           │           │  │
│             └────────────┴───────────┴───────────┘  │
│                                                      │
│  LTM · Skills · Scheduler · Vision · SSH · Memory   │
└──────────────────────────────────────────────────────┘
```

The Electron application communicates with a persistent Python bridge through a JSON-over-stdio protocol. The bridge manages multiple `FlowControllerV2` instances, one for each session.

Each session owns its execution context, tools, browser profile, filesystem state, execution recorder, and task channel.

The current controller architecture is based entirely on `src/controller_v2/`. The previous v1 controller has been removed.

## Agent Execution Model

HandQ uses a persistent Observe-Think-Act execution loop.

```text
User Request
     │
     ▼
   Intent
     │
     ├── Chat ───────────────► Response
     │
     ├── Queue ──────────────► Task
     │
     └── Interrupt ──────────► Immediate Task
                                  │
                                  ▼
                           Persistent Agent
                                  │
                           ┌──────┴──────┐
                           │   Observe   │
                           │     ↓       │
                           │    Think    │
                           │     ↓       │
                           │     Act     │
                           └──────┬──────┘
                                  │
                         Tool Results / Evidence
                                  │
                                  └──────► Next Iteration
```

The coordinator performs lightweight intent classification and task queuing. It does not pre-plan the entire task.

The agent itself decides:

1. How to decompose the task.
2. Which tools are necessary.
3. Whether additional tools should be activated.
4. When a task has reached completion.
5. Whether user clarification is required.

This design intentionally avoids a traditional fixed Planner → DAG → Executor architecture. The agent can continuously adapt its execution strategy based on observations and tool results.

## Prompt and Context Management

HandQ is designed for long-running agent sessions.

The runtime includes:

* Long-term memory retrieval.
* Session history.
* Execution summaries.
* Todo tracking.
* Conversation compaction.
* Tool-result micro-compaction.
* Anthropic prompt caching boundaries.
* Extended-thinking block preservation.
* Incremental execution recording.

Long-term memory retrieval is split into two tiers:

```text
                    User Request
                         │
              ┌──────────┴──────────┐
              │                     │
          FAST Recall          PRECISE Recall
              │                     │
       Intent / Context        Task Execution
              │                     │
              └──────────┬──────────┘
                         ▼
                       Agent
```

The precise retrieval runs concurrently with intent classification so that task-specific memory retrieval does not unnecessarily increase interactive latency.

## Tools

HandQ's tool system is extensible and capability-driven.

Depending on the platform and configuration, available capabilities include:

| Capability    | Description                                      |
| ------------- | ------------------------------------------------ |
| File          | Read, write, edit, glob, grep                    |
| Shell         | Execute local shell commands                     |
| Browser       | Browser automation with isolated profiles        |
| Desktop       | Mouse, keyboard, screenshots and UI automation   |
| Vision        | Vision model integration and screenshot analysis |
| OCR           | Local RapidOCR-based text recognition            |
| SSH           | Remote machine access                            |
| Skills        | Reusable task-specific capabilities              |
| Todo          | Multi-step task tracking                         |
| Scheduler     | Recurring and scheduled tasks                    |
| Memory        | Long-term personal/task memory                   |
| Sub-Agent     | Spawn parallel or delegated agent tasks          |
| Notifications | Notify the user when interaction is required     |

Linux intentionally exposes a smaller capability set. Browser, desktop, web search, email, Teams, and other Windows-specific capabilities are not registered on Linux.

## Multi-Session

A single HandQ bridge can host multiple concurrent sessions.

```text
                    HandQ Bridge
                         │
          ┌──────────────┼──────────────┐
          │              │              │
       Session A      Session B      Session C
          │              │              │
       Agent A        Agent B        Agent C
          │              │              │
       Browser A     Browser B     Browser C
       Profile A     Profile B     Profile C
```

Agent execution, filesystem state, SSH state, and browser profiles are isolated per session.

Desktop control is different because a physical desktop cannot be controlled by two sessions simultaneously. Desktop ownership is therefore serialized through a global lock, while the other session capabilities remain concurrent.

## Memory

HandQ maintains persistent user and execution context under:

```text
%USERPROFILE%\HandQ\
├── handq_config.yaml
├── History\
├── personality\
│   ├── memory.db
│   └── memory_notes\
├── Skill\
├── browser_profile\
├── desktop_shots\
├── logs\
└── scheduled_tasks.json
```

Each task session has its own workspace:

```text
History/
└── <session>/
    ├── .workspace/
    ├── handq-engine.log
    ├── session_<timestamp>_<plan>.jsonl
    └── digest.json
```

The `.workspace` directory is the agent's primary writable workspace. Session metadata and execution records are kept separately from user task artifacts.

## Skills

Skills provide reusable instructions and workflows.

A Skill is stored as:

```text
Skill/
└── <skill-name>/
    └── SKILL.md
```

Skills contain metadata and instructions that the agent can load when appropriate.

This allows specialized workflows to be added without hard-coding every behavior into the core agent.

## Configuration

Copy the example configuration:

```bash
cp handq_config.example.yaml handq_config.yaml
```

Then configure your LLM provider and model pool.

For example:

```yaml
llm:
  API_KEY: "your-api-key"

  agent_models:
    - anthropic::claude-5-sonnet
    - anthropic::claude-4-6-sonnet

  helper_models:
    - anthropic::claude-4-5-haiku
```

The actual configuration file is intentionally excluded from Git because it contains credentials and local settings.

On Windows, HandQ uses:

```text
%USERPROFILE%\HandQ\handq_config.yaml
```

as the persistent user configuration. The `HANDQ_CONFIG` environment variable can be used to explicitly override the configuration location.

## Requirements

### Windows

Recommended development environment:

* Windows 10/11 x64
* Python 3.x
* Node.js
* npm
* Git
* Chromium-compatible browser
* An LLM API key

Some desktop automation features additionally rely on Windows-specific components such as UI Automation and `pywin32`.

### Linux

Linux can run HandQ as a console client or as a resident Sub-HandQ daemon.

The Linux implementation uses a persistent Python process and SSH-based communication. It does not require tmux or systemd.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/YoiteShao/HandQ.git
cd HandQ
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install Electron dependencies:

```bash
cd electron
npm install
```

Create your local configuration:

```bash
cp ../handq_config.example.yaml ../handq_config.yaml
```

Configure the API key and model settings in `handq_config.yaml`.

### Start HandQ in Development

From the Electron directory:

```bash
cd electron
npm start
```

Electron launches the Python bridge as a child process and communicates with it through stdio. The bridge then enters its persistent JSON IPC loop.

## Build

The Windows release pipeline uses Nuitka for the Python bridge and electron-builder for the desktop application.

Build everything:

```powershell
.\packaging\build.ps1
```

Clean build:

```powershell
.\packaging\build.ps1 -Clean
```

Build only the Python bridge:

```powershell
.\packaging\build.ps1 -BridgeOnly
```

Build only Electron:

```powershell
.\packaging\build.ps1 -ElectronOnly
```

The default build produces a single NSIS installer:

```text
dist/
└── installer/
    └── HandQ Setup x.y.z.exe
```

The packaged application uses a per-user installation model and does not require machine-wide installation privileges.

## Linux

The repository also contains a Linux entry point:

```text
handq_linux.py
```

and an installation script:

```text
handq_setup.sh
```

A Linux HandQ can operate as a remote execution target controlled by a Windows HandQ instance.

The architecture is intentionally asymmetric:

```text
Windows HandQ
     │
     │ SSH
     ▼
Linux Sub-HandQ
     │
     ├── File
     ├── Shell
     ├── SSH
     ├── Skills
     ├── Todo
     ├── Sub-Agent
     └── Scheduler primitives
```

Windows remains the primary desktop environment, while Linux provides a persistent remote execution environment.

## Project Structure

```text
HandQ/
├── Skill/                         # Skills
├── assets/
│   └── models/                    # Vendored local models
├── electron/                      # Electron application
│   ├── main.js
│   ├── preload.js
│   ├── updater.js
│   ├── renderer/
│   └── package.json
├── packaging/                     # Build and packaging scripts
│   ├── build.ps1
│   └── build_linux.sh
├── scripts/                       # Runtime helper scripts
├── src/                           # Python backend
│   ├── bridge/
│   ├── controller_v2/
│   ├── infrastructure/
│   └── tools/
├── bridge_main.py                 # Python bridge entry point
├── handq_linux.py                 # Linux entry point
├── handq_setup.sh                 # Linux setup script
├── handq_config.example.yaml      # Configuration template
├── requirements.txt
└── HANDQ_DESIGN.md                # Detailed architecture documentation
```

The detailed architecture, execution flow, storage model, packaging pipeline, and design decisions are documented in [`HANDQ_DESIGN.md`](./HANDQ_DESIGN.md).

## Design Philosophy

HandQ is built around several principles.

### The Agent Owns the Task

The user provides the objective. The agent determines the execution strategy.

```text
User
 │
 │ "Fix the failing tests and verify the result."
 ▼
Agent
 │
 ├── Inspect repository
 ├── Identify failure
 ├── Modify code
 ├── Run tests
 ├── Diagnose failures
 ├── Iterate
 └── Report completion
```

The system deliberately avoids requiring the user to manually specify every intermediate step.

### Evidence Is Not Instruction

Files, command output, web pages, screenshots, and other tool results are treated as evidence.

They cannot redefine the user's task or override the agent's governing instructions.

This provides a foundational defense against prompt injection originating from tool outputs.

### Side Effects Are the Safety Boundary

HandQ does not require a separate global "plan mode".

Instead, potentially consequential actions pass through tool-level risk checks. Exploration can proceed autonomously while operations with meaningful side effects can require explicit user confirmation.

### Long-Running Tasks Are a First-Class Use Case

HandQ is designed for tasks that require many tool calls and multiple reasoning iterations.

Execution history, memory, compaction, task state, and incremental execution records are therefore treated as core infrastructure rather than optional logging.

## Status

HandQ is an actively developed project.

The architecture and APIs may change as the agent runtime evolves. Some capabilities are platform-specific, and the current Windows desktop environment is the primary target.

## Roadmap

Potential areas of future development include:

* More robust remote Linux execution.
* Improved agent planning and look-ahead.
* More Skills and reusable workflows.
* Additional LLM providers.
* Better browser and desktop reliability.
* More powerful memory retrieval.
* Improved observability and execution replay.
* More granular permission and security controls.
* Broader cross-platform support.

## License

See [`LICENSE`](./LICENSE) for the current license.

## Contributing

Issues and pull requests are welcome.

For architectural changes, please read [`HANDQ_DESIGN.md`](./HANDQ_DESIGN.md) before modifying the controller, bridge, session, memory, or tool infrastructure.

---

**HandQ** is an experiment in building a persistent, autonomous desktop agent that can operate across software, files, browsers, terminals, remote machines, and long-running workflows.
