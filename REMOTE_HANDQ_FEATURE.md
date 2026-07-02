# Linux Sub-HandQ 远程委派 —— 功能设计说明

> 一个 Windows HandQ 通过 SSH 控制多个 Linux HandQ 的能力（“1 控 N”）。
> 本文档以**功能说明书**的形式描述该特性的设计目的、整体框架、各组件方案、
> 通信协议、端到端工作流、构建部署与安全模型，供 reviewer 完整了解该功能。

---

## 1. 设计目的

让运行在 Windows 上的 HandQ 主体把**需要推理/规划的复杂任务**委派给远程 Linux 主机
上的一个自治 HandQ Agent，而不仅仅是远程执行已知命令。

- **ssh 工具**：由 Windows 这端逐步驱动（智能在本地），适合“我已经知道要敲哪些命令”。
- **remote_handq 工具（本特性）**：远端 Agent 自治规划并执行（智能在远端），适合
  “分析这段代码 / 修复所有测试失败 / 排查并解决构建错误”这类需要远端自己想办法的任务。

远端 HandQ 是一个**常驻进程**（resident daemon）：`setsid` 脱离终端，无需 tmux/systemd，
不带 LTM/scheduler/personality。它在 Windows 掉电/断网后**进程仍存活**，可后续重连继续
查询结果（注意：进程级持久，**非任务级重放**——见 §10 已知限制）。

每个 Linux 主机同时保留一个**本地应急 console**：当操作 Windows 不便时，本地也能
直接投递目标、查看状态、回答确认。本地 console 与 Windows 共用同一套文件管道，二者
对称、可调试。

---

## 2. 架构总览

```
┌──────────────────── Windows HandQ ────────────────────┐         ┌──────────── Linux 主机 ────────────┐
│                                                        │         │                                     │
│  Planner 在 step 声明 tools_required=["remote_handq"]  │         │   handq_linux.py  (常驻 daemon)     │
│  + ssh_target="user@host"                              │         │   ├─ FlowControllerV2 (Agent)       │
│        │                                               │         │   ├─ StateMirror (UIDelegate)       │
│        ▼                                               │         │   └─ 文件 IPC 泵 (drain 消息/命令)  │
│  RemoteHandQContextProvider.before_item()              │         │                                     │
│   - 经 SSHSetupManager 建立凭据                        │  SSH    │   ~/.handq/<user>@<host>/  (IPC 根)  │
│   - 发现远端 HANDQ_DIR                                 │ ──────► │   ├─ state.json                     │
│   - 注入 credentials_file / handq_dir 提示             │  (paramiko 连接池)  ├─ messages/<id>.txt   │
│        │                                               │         │   ├─ commands/<id>.json             │
│        ▼                                               │         │   ├─ reply/<id>.txt                 │
│  RemoteHandQTool (10 个 action，base64 文件读写)       │ ◄────── │   ├─ confirmation_request.json      │
│        ▲                                               │  回读   │   ├─ confirmation_response.json     │
│        │ 复用 ssh_tool 的连接池 + 凭据基础设施          │         │   └─ handq.pid / daemon.log         │
└────────────────────────────────────────────────────────┘         └─────────────────────────────────────┘
                                                                    本地应急 console（handq_linux 无参/带 goal）
                                                                    与 Windows 共用同一 IPC 目录
```

### 组件清单

| 文件 | 角色 |
|------|------|
| `handq_linux.py` | Linux 入口：常驻 daemon（`--_daemon`）+ 本地 console 客户端 + `StateMirror` |
| `src/tools/remote_handq_tool.py` | Windows 侧 `RemoteHandQTool`：经 SSH 说该 daemon 的文件 IPC 协议 |
| `src/infrastructure/remote_handq_setup.py` | `RemoteHandQContextProvider`：执行前建立 SSH 凭据 + 发现 `HANDQ_DIR` + 注入提示 |
| `src/tools/tool_registry.py` | 把 `remote_handq` 注册为 **Windows-only、on-demand** 工具，并附 usage guide |
| `packaging/build_linux.sh` | 用 Nuitka 把 `handq_linux.py` 编为 Linux standalone 包 |
| `handq_setup.sh` | 在 Linux 上安装 `handq_linux` 命令（+ `handq`/`hi` 别名）并自检 |

---

## 3. 文件 IPC 协议

所有通信经 `~/.handq/<user>@<host>/` 下的文件完成。三端（Windows 工具、本地 console、
daemon）通过同一套**短主机名 + 用户名**规则解析出**同一个目录**：

- `handq_linux.py`：`socket.gethostname().split(".")[0]` + `getpass.getuser()`；
- 远端探测脚本：`hostname -s` + `whoami`；
- `handq_setup.sh`：`hostname -s` + `whoami`。

```
~/.handq/<user>@<host>/
  state.json                    daemon 写：粗状态 + latest_tool + checklist 进度
  messages/<id>.txt             入站目标/追加消息（Windows 和本地 console 都写这里）
  commands/<id>.json            入站 new_session / interrupt
  reply/<id>.txt                出站回复，按投递时选定的 message id 索引
  confirmation_request.json     daemon 写：待回答的 risk/tool/secret/text 门控
  confirmation_response.json    回答方写：与 request.id 对齐的应答
  handq.pid                     daemon 存活（pid + kill -0）
  messages/.processed/          已消费的消息/命令归档
  daemon.log / daemon_error.txt 日志
```

**原子性**：所有写入走 “写 `.tmp` 再 `mv`”，读端永远看不到半写文件。Windows 侧把
任意文本（含引号/换行/unicode）**base64 编码**后传输、远端解码，规避 shell 转义风险。

**目录权限**：daemon 创建 IPC 根目录后 `chmod 0o700`，多用户主机上仅属主能投递目标/
回答确认（best-effort，无 POSIX 权限的文件系统上为 no-op）。

---

## 4. 组件详解

### 4.1 `handq_linux.py` —— Linux 入口

同一个文件承担三件事：常驻 daemon、本地 console、`StateMirror`。重的 controller 导入
全部延迟到 daemon `start()`，使纯 console 路径（只碰文件 + subprocess）不拉起模块树。

**命令行**
```
handq_linux                  启动（如需要）+ 进入交互 console
handq_linux <goal...>        直接投递一个目标并打印回复
handq_linux --new            （重）开一个全新 session
handq_linux --status         打印 daemon 的 state.json
handq_linux --exit           停止 daemon
handq_linux --_daemon        内部：以常驻 daemon 方式运行（setsid 目标）
```

**`_LinuxDaemon`（常驻进程）**
- `start()`：构建 LLM 服务池（与 stdio_bridge 一致的回退链 + helper 池）、建会话目录、
  构建 `FlowControllerV2`、写 `handq.pid`、首帧 `state.json`。
- 主泵 `run()`：在一个 asyncio 循环里按 `POLL_INTERVAL=0.2s` 轮询，依次
  `_drain_commands()` → `_drain_messages()`，收到 SIGTERM/SIGINT 优雅退出
  （退出时 `stop()` 会 unlink `handq.pid` 与 `state.json`）。

**消息处理 `_drain_messages()`（结果路由的关键）**

V2 的执行跑在**后台任务**里（`flow_controller.py:210/214` 的 `_agent_task`/`_planner_task`），
因此 `on_user_message()` 只返回**初始 plan ack**，真正的完成总结稍后才通过
`on_reply_to_user` 回调异步送达。daemon 据此设计：
- 在 `await on_user_message` **之前**记下 `self._active_msgid = stem`；
- 构建 flow 时接上 sink：`FlowControllerV2(..., on_reply_to_user=self._on_agent_reply)`；
  `_on_agent_reply` 把**完整回复**写入 `reply/<active_msgid>.txt`，并把该 id 记入
  `self._final_reply_msgids`；
- `on_user_message` 返回后，**仅当** `stem not in self._final_reply_msgids` 才把返回值
  写入 reply 文件——
  - **chat 路径**：无 Agent/Planner 运行，返回值即聊天回复，直接写入；
  - **task 路径**：返回值是 plan ack，作为**占位**先落地；待任务 settle 后被 sink 用
    完整总结**覆盖**。
- chat 永远不会被误判为长跑任务：处理完若 `checklist.get_current_item() is None`
  即把 `task_status` 置 `idle`（task 路径的首个 item 此时仍在飞，故不会被误置）。

**命令处理 `_handle_command()` —— `interrupt` 的空闲安全**

Agent 只在其 item 循环内 ack 中断；空闲时它停在 `wait_for_current_item()`，不会 ack。
因此 interrupt 分支按是否**确有任务在飞**分流：
- `checklist.get_current_item() is not None`：先 `replace_post_current([])` 清待执行尾部，
  再（无 await 复检后）`interrupt_agent(...)` 中止在飞 item；
- 空闲：**只**清尾部，**绝不** signal 中断——否则会留下 `_interrupt_event` 置位（误中止
  下一个任务的首个 item），且二次空闲中断会永久阻塞在 `_interrupt_acked.wait()`，冻结整个泵。

此分流只动 Linux daemon 自己的逻辑，不触碰 Windows 也在用的 `SharedCheckList` 本体。

**`StateMirror`（daemon 的 `UIDelegate`）**
- 唯一职责：把 controller 活动镜像进 `state.json`，并把确认请求路由进文件管道；不渲染任何东西。
- `show_state_changed`：`"idle"` 是唯一对外可见的“任务已 settle”信号，据此维护
  `task_status ∈ {"", "running", "idle"}`。
- `notify_tool_execution_started`：只抓 START 快照（最新单次 tool 调用），不留历史。
- `snapshot()`：写出 `pid/handq_active/session_id/task_status/status_text/latest_tool/
  checklist/completed/total/last_updated`。
- 确认路由：`request_risk/tool/secret/user_text` → `_await_response(kind, payload)` 写
  `confirmation_request.json` 并异步轮询 `confirmation_response.json`；超时（默认 300s，
  `HANDQ_CONFIRM_TIMEOUT` 可调）按安全默认（拒绝/空）处理。

**本地 console 客户端**
- `_ensure_daemon` 不在则 `setsid` 拉起；`_submit_message` 写消息、`_wait_reply` 轮询 reply；
- `_wait_reply` 期间还会 `_pump_confirmation`：本地交互式回答 risk/tool/secret/text 门控，
  让“仅本地 console 在场”时也能解锁 Agent，而非干等超时。

### 4.2 `src/tools/remote_handq_tool.py` —— Windows 侧 `RemoteHandQTool`

复用 `ssh_tool` 的连接池 + 凭据基础设施；阻塞式 paramiko I/O 经 `asyncio.to_thread` 移出事件循环。

**发现（`_discover`，按主机缓存）**：一次 SSH 探测拿回 `handq_dir` 与 `launch` 调用前缀，
优先级为：① `handq_setup.sh` 安装的 `handq_linux` 调度器（自注入 per-host `--config`）→
② standalone Nuitka 二进制（`handq_linux.dist/handq_linux.bin`，自载 dist-root 配置）→
③ 源码 checkout（`<python> handq_linux.py`，优先用同目录 `.venv/venv`）。找不到则抛错并
指导用户先部署。

**唤醒模型（`_wake_daemon`）**：daemon 不存活时以 `nohup setsid <launch> --_daemon` 拉起，
使其脱离 SSH 会话进程组、在连接关闭/Windows 掉电后仍存活；随后轮询 `handq.pid` 确认起来。

**10 个 action**

| action | 说明 |
|--------|------|
| `discover` | 定位 handq_linux + 报告 daemon 状态（强制刷新发现缓存） |
| `submit_goal` | 需要时唤醒 daemon + 投递目标；可 `wait_timeout` 等结果，返回 `message_id` |
| `send_message` | 向运行中的任务注入追加消息；返回自己的 `message_id` |
| `get_status` | 读 `state.json`（task_status/status_text/latest_tool/checklist）；交叉校验存活；浮现待答确认 |
| `get_result` | 取 `reply/<message_id>.txt`；可 `wait_timeout` 轮询 |
| `get_confirmation` | 读取待回答的 risk/tool/secret/text 请求（若有） |
| `answer_confirmation` | 回答待答确认，使远端任务继续 |
| `new_session` | 让 daemon 开一个全新 session |
| `interrupt` | 中止在飞任务并清待执行尾部 |
| `exit_handq` | 停止远端 daemon |

**结果获取的“settle 语义”（`_poll_reply`）**：task 的 reply 文件先后经历两态——运行期是
plan-ack 占位、settle 后被完整总结覆盖。故 `_poll_reply` **每轮先读 `state.json`、再读
reply 文件**，仅当 `task_status=="idle"` **且** reply 存在时返回。这样既跳过占位、又借
“先读 state、隔一次 SSH 往返再读 reply”吸收 orchestrator “先发 idle 再写 reply”
（`_emit_completion_reply`）的亚毫秒级间隙。超时返回 `None`（调用方报“仍在运行”）；
daemon 已死则返回现存内容（占位或 `None`）。**空 body 任务**（无任何已完成 item）不触发
reply sink，此时 reply 文件保留占位、`task_status=="idle"`，占位即为合理兜底。

**存活交叉校验**：崩溃的 daemon 只有优雅 `stop()` 才会 unlink `state.json`，否则状态会
永远卡在 `running`。`get_status`/`get_result` 均先 `_daemon_alive`（远端 `kill -0`），
dead 则置 `handq_active=False` / 附 `daemon_alive=False` 与说明，避免误判长跑。

**确认回传契约**（与 `StateMirror._await_response` 对齐，经 `confirmation_id` 做 id 校验）：
- `tool`/`risk` → `decision ∈ {yes,no,message}`（`message` 需附带 `message` 文本）；
- `secret`/`text` → `value`（要回传的密钥或文本）。
`get_status` 在 `task_status=="running"` 时顺带探测并以 `pending_confirmation` 主动浮现
待答项（idle 时跳过这次额外往返）。

### 4.3 `src/infrastructure/remote_handq_setup.py` —— `RemoteHandQContextProvider`

`ContextProvider`，`tool_name="remote_handq"`。Planner 在 step 声明 `remote_handq` 后，
`FlowControllerV2` 在执行该 item **前**调用 `before_item`：
1. 从 `ssh_target`（或指令文本）抽出 `hostname/username`；
2. 经共享的 `SSHSetupManager.ensure_ssh_ready(...)` 建立 SSH 凭据（复用既有 key/keyring/
   password 流程）；
3. 调 `remote_handq_tool._discover` 发现远端 `HANDQ_DIR`；
4. 注入提示：首次见到某主机给**完整提示**（工作流 + `credentials_file` + `handq_dir` +
   动作清单 + 确认→回答工作流），同主机后续给**简短提示**（按主机缓存）。

它还向 Planner 暴露 `planner_description / planner_routing_rule / planner_antipatterns`，
帮助 Planner 正确区分 `ssh`（自己驱动）与 `remote_handq`（委派远端 Agent），并避免把两者
放进同一 step。

### 4.4 `src/tools/tool_registry.py` —— 注册

- `RemoteHandQTool` 在 `_IS_WINDOWS` 下注册（Linux HandQ 不会委派给自己），`on_demand=True`：
  默认**不进** LLM 工具清单，仅当 `RemoteHandQContextProvider` 激活时才注入。
- 附带详尽 `usage_guide`：WHEN/WHEN NOT、ssh vs remote_handq 的核心区分、PREREQUISITES、
  四步 WORKFLOW、CONTROL 动作，引导 Agent 正确使用。

---

## 5. 端到端工作流

**提交→监控→取结果**
1. `submit_goal(goal=..., ssh_target 经 provider 已建凭据)` → 唤醒 daemon、入队、返回 `message_id`；
2. `get_status(wait_timeout=N)` → `task_status` 运行中为 `running`、settle 为 `idle`；
   同时回 `latest_tool` 与 `checklist` 进度；
3. `get_result(message_id=..., wait_timeout=N)` → 取该目标的**完整**回复。

**回答确认**：`get_status` 浮现 `pending_confirmation` → `answer_confirmation(
confirmation_id, decision="yes" | "no" | "message"+message)`（tool/risk）或
`answer_confirmation(confirmation_id, value=...)`（secret/text）→ 远端任务继续，无 300s 卡顿。

**中断 / 新会话 / 退出**：`interrupt` 中止在飞任务并清尾部；`new_session` 弃用当前会话开新；
`exit_handq` 停 daemon（可选——daemon 常驻，掉电/断网后仍可重连）。

---

## 6. 构建与部署

### 6.1 `packaging/build_linux.sh`（Nuitka standalone）
- 以脚本位置推导 `PROJECT_ROOT`，`--standalone` 编译 `handq_linux.py`；
- 性能优化：物理核数并行（≤16）、ccache（经 `NUITKA_CCACHE_BINARY`，并从 PATH 摘除
  `/usr/lib/ccache` 以免 Scons 版本探测失败）、增量构建（`CLEAN_BUILD=1` 才清缓存）、
  `--lto=auto`、大量 `--nofollow-import-to` 裁剪（GUI/测试/打包/遗留网络/音频/IPython 等；
  **注意 `unittest` 不可裁**，anthropic/httpx/anyio 在生产路径硬导入它）；
- 显式 `--include-package`：`src/yaml/rich/json_repair/anthropic/httpx/paramiko/keyring/
  keyrings/cffi/cryptography`；
- 产物 `dist/linux-glibc<ver>/`：`handq_linux.dist/`（二进制 + 依赖）+ `handq_setup.sh`
  + `handq_config.yaml`（取自 `handq_config.example.yaml`，置于 dist 根供用户编辑，**不**内嵌）；
- 兼容性提示：产物要求目标机 GLIBC ≥ 构建机；更广兼容建议在 Ubuntu 20.04 容器内构建。

### 6.2 `handq_setup.sh`（在 Linux 上安装）
- 必须 `bash` 执行（禁止 source）；定位入口（standalone `.dist` 二进制 / 顶层 `.bin` /
  源码 `.py`）与 config，做依赖与 YAML/必需键/API key 校验；
- 安装优先 `/usr/local/bin`（非交互 SSH 的 PATH 上，保证 Windows 探测 `command -v
  handq_linux` 命中），回退 `~/.local/bin`；清理其它位置的陈旧 wrapper/别名；
- 采用**静态 hostname 调度器 + per-host 配置**：调度器各主机相同，运行时 `source`
  `~/.config/handq/hosts/<host>`（内含 `exec <入口> --config <cfg> "$@"`），从而 Windows
  侧可在其后追加 `--_daemon`/`--status`/goal 而 config 始终被注入；安装 `handq`/`hi` 别名；
  按需把安装目录写入 shell profile 的 PATH；
- 内置自检（Suite A 静态检查 + Suite B `--help`/`--status` 冒烟），并校验**无遗留**
  tmux/PROMPT_COMMAND/shell-rc 自动 attach（该特性明确**不带** tmux/状态栏）。

---

## 7. 安全模型

- 凭据仅从本地凭据文件读取（`credentials_file`），复用 ssh_tool 的连接池与 keyring 流程；
- 任意目标文本经 base64 传输，规避远端 shell 注入；
- IPC 根目录 `0o700`，限制多用户主机上的横向投递；
- 远端门控（risk/tool/secret/ask_human）默认安全：无人回答则超时拒绝/返回空，绝不默许放行。

---

## 8. 已知限制

- **并发多目标**共享单一 `_active_msgid`：回复归属于“当前活跃”的那条消息（已在代码标注）。
- **进程级而非任务级持久**：daemon 进程在 Windows 掉电/断网后存活，但 daemon 自身崩溃时
  **在飞任务不会被重放**（真正的任务级持久化/重放是独立 feature，超出本特性范围）。
- 低危项作为已知限制保留：base64 经 `ARG_MAX` 的体量上限、`reply/`.processed`` 的 GC、
  PID 复用竞态。

---

## 9. 验证

**已完成（本地，无需 Linux 目标）**：`handq_linux.py`、`src/tools/remote_handq_tool.py`、
`src/infrastructure/remote_handq_setup.py` 三者 AST 解析 + import 均通过。

**完整验证（需 Linux SSH 目标：真机或 WSL）**
1. **结果路由**：Linux 上 `python handq_linux.py "create a file notes.txt with two lines"`
   应打印**完成总结**（`## Task complete …`）而非仅 plan ack；从 Windows
   `submit_goal → get_status(wait_timeout=120) 直到 idle → get_result`，`reply` 须为完整结果。
2. **确认**：提交触发 risk 门控的目标；`get_status` 见 `pending_confirmation`；
   `answer_confirmation(..., decision="yes")`；任务继续（无 300s 卡顿）。
3. **空闲中断**：daemon 空闲时连发两次 `interrupt`，确认 daemon 仍响应（随后 `submit_goal`
   能跑且首个 item 未被误中止）；再在任务运行中 `interrupt`，确认干净中止。
4. **存活**：`kill -9` daemon；`get_status`/`get_result` 须报告 daemon 不在线而非陈旧 `running`。
