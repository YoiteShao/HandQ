# HandQ 文件架构

本文档是 "每个文件放在哪里、bridge 如何定位配置、怎么发版" 的权威说明。

> **统一原则**：从源码运行 (`npm start`) 与从 NSIS 安装包运行（用户双击 `HandQ.exe`）行为一致 —— 同一份 bridge 代码，同一棵 `%USERPROFILE%\HandQ\` 用户根目录，同一份 `handq_config.yaml`。本文档不再区分 dev / prod。

---

## 1. 核心原则：bridge 自定位

`bridge_main.py` 在导入任何依赖之前先确定自己的安装目录：

```
INSTALL_DIR =
    parent of sys.executable    若 Nuitka standalone (__compiled__)
                                或 PyInstaller (sys.frozen)
    parent of __file__          其他情况（直接 python 运行）
```

随后按以下优先级选取 `handq_config.yaml`（首个命中即用）：

1. `HANDQ_CONFIG` 环境变量 — 显式覆盖（CI、便携模式）。
2. `%USERPROFILE%\HandQ\handq_config.yaml`（**Windows**） / `<INSTALL_DIR>\handq_config.yaml`（**Linux/macOS**）。
3. `<INSTALL_DIR>\handq_config.yaml` —— 仅在 Windows 上作为 ship-default 源使用：当用户根下不存在时，bridge 在 boot 时自动从 (3) 拷贝到 (2)，之后所有读写都走 (2)。

> Linux/macOS 没有 user-root 惯例，永远走 INSTALL_DIR；本文档其余部分以 Windows 为主目标。

解析出的绝对路径会被写回 `os.environ["HANDQ_CONFIG"]`，再 import `src.bridge.stdio_bridge`，下游所有消费方拿到的都是同一个值。

**bridge 不依赖 cwd**。Electron 也不再给 `spawn()` 传 `cwd`。无论是桌面快捷方式、开始菜单、还是 `cmd /K` 启动，行为完全一致。

---

## 1.5 用户根目录布局

```
%USERPROFILE%\HandQ\               ← 唯一用户根 — 所有 HandQ 写到磁盘的东西
  handq_config.yaml                  用户配置（小、可漫游）
  scheduled_tasks.json               定时任务持久化（与 personality 解耦）
  History\                           会话历史（大、可手动清理）
    <YYYYMMDD-HHMMSS>-<slug>\        每个 `request` 一个目录（session 根，框架元数据；prompt 不暴露）
      handq-engine.log                 ← 该 session 内的全部日志（挂在 root logger：HandQ 树
                                         + shell_tool / session_tool / session_context 等 stdlib
                                          logger；后台常驻子系统已转移到 .dia 不在此，见 §1.5）
      session_<TS>_persiste.log        ← ExecutionRecorder 写的结构化执行轨迹，直接落在 session 根
                                         （文件名 = `session_<时间戳>_<plan_id[:8]>.log`；GUI 桥的 plan_id
                                          恒为 `persistent_session`，故后缀恒为 `persiste`）
      .workspace\                      ← agent 的"全世界"——prompt 中唯一出现的可写路径
                                         （子目录名读自 `session.workspace_base`，默认 `.workspace`；
                                         所有 agent 产物都落这里，UI 的 Files 面板读取并支持拖出/另存为）
  personality\                       ← "HandQ 学到的关于我"的所有数据
    memory.db                          长期记忆 SQLite (LTM)
    memory.db-wal                      WAL 写日志（运行时存在）
    memory.db-shm                      WAL 共享索引
    memory_notes\                      长 /remember 的 .md 镜像
      <id>.md                          (frontmatter + 用户原文)
    spillover\                         PersonalityMonitor 的 ring 溢出兜底
      m<idx>_<ts>_<seq>.jpg            JPEG 字节（quality=85，~300KB/帧）
      m<idx>_<ts>_<seq>.meta.json      元数据（title/app/tier/recent_texts 快照）
  browser_profile\                   browser_tool 状态根（多 session 模型 §1.7）
    sessions\                          per-flow Chromium user-data-dir
      <sid>\                           每个 FlowControllerV2 一个独立 profile
                                       （cookies / 登录 / 扩展状态独立；session 关闭后保留为
                                        孤儿目录，未来加清理任务）
    screenshots\                       browser_tool 截图（vision §1.6，三级分级共享 scratch）
  desktop_shots\                     desktop_tool 截图（vision §1.6）
  email_attachments\                 email_tool 附件沙箱
  logs\                              ← 框架日志（跨 session），每次 launch 一个目录
    <YYYYMMDD-HHMMSS>\
      handq-bridge.log                 Python 端跨 session 框架日志
                                         （LTM / PersonalityMonitor / activity /
                                          Scheduler 已 propagate=False 转移到 .dia，
                                          不再进这里）
      handq-frontend.log               Electron main + preload + renderer
                                         （5MB×3 自轮转；逐 envelope / bridge stderr
                                          镜像默认关，HANDQ_FRONTEND_DEBUG=1 开）
    .dia\                            ← 隐藏目录（NTFS HIDDEN attr 已设置）
      internal-trace.log               LTM / PersonalityMonitor / activity /
                                         Scheduler 四棵 logger tree 的专属去处
                                         （propagate=False 转移：含 error 在内的
                                          全部级别只落这里，不进主 log）

<install_root>\                    ← 程序文件（NSIS per-user 默认装到
                                     %LOCALAPPDATA%\Programs\HandQ\）
  HandQ.exe                          Electron 主程序
  handq-bridge.exe                   Nuitka 冻结的 bridge
  handq_config.yaml                  ship 的默认配置（首次启动拷到上面）
  scripts\                           ship 的辅助脚本
    handq_post_commit.py               post-commit hook 源（被 stdio_bridge
                                         读取后写到 .git/hooks/post-commit）
    start_chrome_with_debug.bat        浏览器 attach 模式启动器
  _internal\                         Nuitka 运行时依赖
  resources\app.asar                 Electron renderer/main 打包包
```

> **两种 session 目录形态**（在 `History\` 下会同时看到，属正常）：上图是 **Windows GUI 桥**（`stdio_bridge` → `FlowControllerV2`）的产物——ExecutionRecorder 直接写 `session_<TS>_persiste.log` 到 session 根，agent 产物落 `.workspace\`。另一种形态（`executions_logs\plan_<TS>_<hash>.log` + `session_state.json` + 产物直接落 session 根、无 `.workspace\`）来自 **Linux CLI / 远程委托**（`handq.py`，见 §8）——那条路径用 `session_dir/executions_logs/` 子目录、recorder 文件名前缀为 `plan_`。

切分原则：

| 数据 | 路径 | 漫游？ | 用户可见？ | 生命周期 |
|---|---|---|---|---|
| 用户配置 | `%USERPROFILE%\HandQ\handq_config.yaml` | 是 | 是 | 跨升级；按 PRESERVE/OVERRIDE 策略与 ship-default 合并（见下） |
| Session 历史 | `%USERPROFILE%\HandQ\History\<id>\` | 否 | 是 | 跨升级，可手动清理 |
| Per-session engine log | `%USERPROFILE%\HandQ\History\<id>\handq-engine.log` | 否 | 是 | 跟随 session |
| Per-session 执行轨迹 | `%USERPROFILE%\HandQ\History\<id>\session_<TS>_persiste.log` | 否 | 是 | 跟随 session（ExecutionRecorder 写） |
| Per-session 浏览器 profile | `%USERPROFILE%\HandQ\browser_profile\sessions\<sid>\` | 否 | 是 | 每个 flow 独立的 Chromium user-data-dir；详见 §1.7 多 session 模型 |
| LTM SQLite | `%USERPROFILE%\HandQ\personality\memory.db` | 否 | 是 | 跨升级；详见 LTM 设计文档 |
| 长 /remember 镜像 | `%USERPROFILE%\HandQ\personality\memory_notes\<id>.md` | 否 | 是 | 跨升级；用户可编辑器打开 |
| Ring 溢出 buffer | `%USERPROFILE%\HandQ\personality\spillover\` | 否 | 是 | RAM ring 满 / 监视器断开时落盘；OCR 完立删；启动时清理 >24h 残留 |
| 定时任务 | `%USERPROFILE%\HandQ\scheduled_tasks.json` | 否 | 是 | 跨升级；JSON 可手编 |
| 框架日志 | `%USERPROFILE%\HandQ\logs\<launch>\` | 否 | 是 | 自动 prune（保留最近 30 个 launch） |
| 内部排障日志 | `%USERPROFILE%\HandQ\logs\.dia\internal-trace.log` | 否 | 默认隐藏 | RotatingFileHandler 1MB×5 自封顶 |

为什么是一个根：早期把 `logs/` 和 `diag/` 放到 `%LOCALAPPDATA%\HandQ\`，意图是"机器本地、不漫游"。但这两个根都不会随用户漫游（`%USERPROFILE%` 漫游的是 `Documents` / `Desktop` 等子目录，自定义子目录默认也不漫游），且都不被 NSIS 卸载器清理。三根的意义只剩"概念分层"，但带来的是用户必须记两个位置才能找到 HandQ 的全部产物。合并为一根后心智模型更清晰：**"HandQ 写到磁盘的一切都在 `%USERPROFILE%\HandQ\` 下"**。

日志清理策略：

- **`logs\<TS>\`**：每次启动新建一个时间戳目录。`bridge_main._prune_old_log_dirs()` 在 boot 早期跑，按 mtime 排序，只保留最近 30 个，旧的 `shutil.rmtree`。Pattern 严格匹配 `^\d{8}-\d{6}(-\d+)?$`，`.dia/` 等非时间戳目录不会被误删。
- **`logs\.dia\internal-trace.log`**：单文件，`RotatingFileHandler(maxBytes=1MB, backupCount=5)` 自封顶 5MB，跨 launch 持续累积以便交叉关联。Prune 不动它。
- **隐藏机制**：dot 前缀（`.dia`）只在 Linux 风格生效，Windows Explorer 默认会显示。`bridge_main._set_hidden_on_windows()` 通过 `ctypes.windll.kernel32.SetFileAttributesW(FILE_ATTRIBUTE_HIDDEN)` 设置 NTFS HIDDEN 属性，让目录在默认浏览视图下消失（"显示隐藏文件"勾上仍能看到——刻意只拦"无意路过的用户"，不防备主动排查者）。

三层日志的语义切分（§1.5 核心，各司其职、互不重复）：

- **`handq-engine.log`（per-session，最全）**：`stdio_bridge._ensure_flow` 在分配 session 目录后，用 `logger.add_root_file_handler()` 把一个 `SafeRotatingFileHandler`（10MB×5）挂到 **root logger**，`_do_new_session` 用 `remove_root_file_handler()` 摘掉。挂 root 而非 `"HandQ"` 名，意味着它捕获本 session 内**冒泡到 root 的一切**——既有 `get_logger()` 的 HandQ 树，也有 `shell_tool` / `session_tool` / `session_context` 这些用 stdlib `logging.getLogger(__name__)` 的强 session 模块（旧版只挂 `"HandQ"` 名，漏掉这些，这正是 engine.log 曾经"单薄"的根因）。`initialize_logger(..., log_file=None)` 不再给 `"HandQ"` 名挂文件 handler，避免每条记录写两遍。
- **`handq-bridge.log`（per-launch，跨 session）**：root 上常驻的 launch 级 handler，内容 = engine.log 的同源减去 session 边界（跨多个 session）。
- **`.dia/internal-trace.log`（隔离，非副本）**：`handq.ltm` / `handq.personality` / `handq.activity` / `handq.scheduler` 四棵树在 `bridge_main.py` 里 `propagate=False`——它们是不属于任何单一 session 的常驻后台 daemon，**整段（含 error）只写 .dia**，既不进 bridge.log 也不进 engine.log。这让 engine.log 成为干净的"本 session 内发生的一切"视图，代价是排查这些后台子系统的崩溃必须去 .dia 看（刻意取舍）。
- **`handq-frontend.log`（Electron）**：`logLine()` 自带 5MB×3 size-based 轮转（内存字节计数，热路径不每行 stat）。两个高频 firehose——逐 envelope（含每个流式 token delta）与 bridge stderr 全量镜像（后者本就是 bridge.log 的副本）——降级为 `logLineDebug()`，默认静默，置 `HANDQ_FRONTEND_DEBUG=1` 才写。`[bridge]` 的 stderr console echo 与崩溃对话框的 `stderrRing` 缓冲不受影响。

PersonalityMonitor spillover 策略：

- **常态行为（无落盘）**：capture 拿到的 frame 走 ndarray → perceptual_hash → JPEG 入 RAM ring(maxlen=128) per monitor，OCR 推迟到 idle/锁屏 gate 解锁后由 drain worker 串行消化。**正常用户日常 0 次磁盘写入**，与原"每 8s 落一次 PNG 再 unlink"的旧路径相比消除了 AV 扫描互动。
- **溢出兜底（写 `personality\spillover\`）**：仅两种边界条件触发：
  1. ring 满（用户连续高强度切屏 4h+）→ 把最旧的帧 spill 到磁盘等下一次 drain
  2. 监视器中途断开（用户拔外接屏）→ 该 monitor 的 ring 全部 spill，避免数据丢失
- **drain worker** ring 空时回头吃 spillover 目录里 timestamp 最早的一对文件（`.jpg` + `.meta.json`），OCR 完立刻 unlink 两个文件。`.meta.json` 携带 monitor_index、tier、title、app、capture 时刻的 `recent_texts` 快照——后者让"orphan 帧"（监视器已不在）也能正常做 Jaccard 文本去重。
- **启动清理**：`PersonalityMonitor.start()` 扫一遍 `spillover\`，删除 mtime 超过 24h 的残留对（防止旧版本 schema、磁盘满等异常导致永久积压），并 cap 在 `ACTIVITY_SPILL_MAX_FILES`（256 对 = ≤51 MB）以内。
- **路径选择理由**：放 `personality\` 下而非 `%LOCALAPPDATA%`：(1) 与 `memory.db` / `memory_notes` 同根，符合 §1.5 "HandQ 学到的关于我"统一原则；(2) NSIS 卸载器一并清理（`%LOCALAPPDATA%` 不被卸载器清）；(3) AV 扫描行为与 `%LOCALAPPDATA%` 等价，且自定义子目录默认不被 OneDrive 漫游；(4) 触发频率极低，把它和别的活动数据放一起更便于排障。

> Session 根目录强制为 `%USERPROFILE%\HandQ\History\`，不可由 yaml 配置。GUI 模式下用户没有"我在哪个工作目录"的心智，所以 agent 的 `working_directory` 实际指向 `<session>/<workspace_base>/`（默认 `.workspace`）——这是 prompt 中**唯一**出现的可写路径，session 根本身只放 `handq-engine.log` / `session_*persiste.log` 等框架元数据，prompt 永远不提它。这层嵌套是结构防御：即使 LLM 抽风写 `../foo`，文件也只会落到 session 根而不是用户文件系统。`storage_directory` 仍存在但仅供框架内部使用（在 `stdio_bridge._allocate_session_dir` + `_ensure_flow` 里分配，向 PersistentAgent 传 `expose_session_storage_in_prompt=False` 抑制 prompt 里那一行）。

升级时的配置合并：`bridge_main._merge_user_config_with_seed()` 在 boot 早期跑（`_ensure_user_config_present` 之后），按 yaml 顶部 `version:` 字段判断 user 是否落后 ship；如果是，按两套策略合并：

- **PRESERVE**——用户的个性化与凭据由 user 决定。清单（硬编码于 `bridge_main._PRESERVE_PATHS`）：`llm.API_KEY`、`session`、`interaction_switches`、`teams`、`high_risk_commands.whitelist`、`high_risk_commands.custom_patterns`、`desktop.sensitive_window_patterns`、`web_search.default_limit`、`web_search.max_limit`、`web_search.snippet_max_chars`、`personalization`。
- **OVERRIDE**（默认）——其余字段一律以 ship 为准。这是 `llm.models` / `llm.roles.*`（已下线的模型自动剔除）、`vision`、`screenshots`、`browser`、`email`、`update`、以及 `high_risk_commands` 三类危险规则（ship 能向老用户推送新安全规则）的处理路径。

每层 dict 都按 ship 的 key set 走：user-only 的 key 自动丢弃（ship 是 schema 权威），ship-only 的 key 取 ship 默认值（新功能配置项自动出现）。合并前在同目录写一份 `handq_config.yaml.pre-<old_version>` 备份；写入用 `os.replace` 原子重命名，失败路径全异常捕获 + emit boot_progress `config_merge_failed`，bridge 仍能起。

---

## 1.6 Vision artifacts: 三级截图存储

视觉相关的图像产物（浏览器/桌面截图、vision_query 工作图、活动监控帧）按 **producer + 用途** 落到三个分级，每个分级独立配置 retention。统一定义在 `src/infrastructure/vision/storage.py` 的 `ScreenshotStore`，三个 producer（browser_tool / desktop_tool / activity_monitor）各持一个实例，根目录不同但分级语义和配置共享。

**核心原则：这里全是 SCRATCH 空间。** 任何需要长期留存的捕获，agent 应该用绝对路径写到当前 task 的 session 目录 (`%USERPROFILE%\HandQ\History\<id>\`)，而不是依赖 screenshots/ 里的某个分级。screenshots/ 不该承担「长期资产」的语义。

### 分级表

| 类别 | 谁写 | 触发时机 | Retention | LLM 可选 |
|---|---|---|---|---|
| **ephemeral** | vision_query / find_element 的工作图 | 每次 vision 调用内部生成 | LRU(max_files) + 年龄(max_age_minutes)，每写一张触发；session 边界全清 | ❌ producer 内部，schema 不暴露 |
| **task** | 显式 `screenshot` 调用 | agent 主动留档 | session 关闭时按 `retain_after_task_days` 老化扫；max_files 兜底 | ✅ 默认且唯一选项 |
| **activity** | 周期帧（Phase 3） | activity_monitor 主循环 | 年龄 + LRU 双门 | ❌ activity_monitor 独占；其它 producer 写到此目录视为 bug |

### 三个根目录

| Producer | 根目录 |
|---|---|
| browser_tool | `%USERPROFILE%\HandQ\browser_profile\screenshots\<category>\` |
| desktop_tool | `%USERPROFILE%\HandQ\desktop_shots\<category>\` |
| activity_monitor（Phase 3+） | `%USERPROFILE%\HandQ\activity\<category>\` |

### 不变量

- **producer 决定根目录，分级名跨 producer 共享**：同一份 `handq_config.yaml` 的 `screenshots:` 段驱动三个 store。
- **ephemeral 是 producer-internal**：parameter_schema 不暴露给 LLM，防止 LLM 误把重要捕获写到容易被清的层。
- **`screenshot` action 不接受分级参数**：相对路径默认进 task；要长留 agent 用绝对路径写到 session working_directory。
- **activity 仅 activity_monitor 写**：其它 producer 写入此目录视为 bug。
- **清理时机**：写时摊销（每写一张触发自身分级的 LRU+age 清理）+ session 边界全清（ephemeral 全清 + task 老化扫）。无后台定时器。

### 配置（默认值，节自 handq_config.yaml）

```yaml
screenshots:
  ephemeral:
    max_files: 30
    max_age_minutes: 15
  task:
    retain_after_task_days: 1
    max_files: 100
```

> `activity` 分级**不**在 yaml 里 —— 它是 activity_monitor 的纯 debug backstop（正常路径上 OCR 完立刻 unlink），常量定义于 `src/infrastructure/long_term_memory/_constants.py` 的 `ACTIVITY_SCREENSHOT_MAX_FILES` / `ACTIVITY_SCREENSHOT_MAX_AGE_DAYS`。

数值刻意取保守值。要 bump 上限请有具体证据（看到 agent 因 retention 丢上下文）。

---

## 1.7 多 session 并发模型

单个 `handq-bridge` 进程承载 N 个并发的 `FlowControllerV2` 实例，以
`session_id` 为键路由 IPC。Renderer 把每个 session 渲染为一张永远可见的
卡片，用户可像同时打开多个 application 实例一样并发使用。

**关键不变式**：

| 维度 | 模型 | 实现 |
|------|------|------|
| Agent 推理、文件、SSH、终端、shell | **真并发** | 每 session 独立 FlowControllerV2 + SessionContext |
| **浏览器** | **真并发** | 每 session 独立 Chromium 进程 + 独立 user-data-dir（`browser_profile\sessions\<sid>\`） |
| **桌面（鼠标键盘）** | **任务级 FIFO 排队** | 物理约束：一块屏幕不能两个 session 同时驱动输入。orchestrator 完成 task 时 `_forward_state_to_ui("idle")` 释放 `_GLOBAL_DESKTOP_OWNERSHIP_LOCK`，等待中的 session 立即获取 |
| **LTM / Scheduler / PersonalityMonitor** | **进程级单例** | 共享知识库 / 调度 / 活动监控；并发安全靠各自的 lock |
| **PersonalityMonitor pause** | **直接查询 `_GLOBAL_DESKTOP_OWNER`** | 不用 refcount，无漂移；任何 session 持有桌面时自动 pause，全部释放时自动 resume |

**生命周期**：

- 创建：renderer 生成 UUID → 发 `request` 带 session_id → bridge `_ensure_flow`
  构建 per-sid 状态（flow / services / UI / generation 计数 / engine.log handler）
- 销毁：renderer 点 X 发 `close_session` → bridge `_do_close_session` 抢占
  in-flight request → flow.destroy → ctx.close → 释放 desktop 全局锁
- 关闭最后一个 session：renderer 自动 `createSession()`，用户永不处于
  "无 session" 状态

**调度器与 session 解耦**：cron 任务触发时新建 `sched-{uuid4().hex[:12]}`
session 派发，与 renderer-driven session 完全等价并发。

**完整设计**：见 `MULTI_SESSION_DESIGN.md`（IPC 协议、并发派发、跨会话
资源协调、UI 模型、不变式速查、测试覆盖）。

---

## 2. 仓库结构

```
HandQ/                              ← 仓库根，也是直接运行时的 INSTALL_DIR
├── bridge_main.py                  ← bridge 入口（编译为 handq-bridge.exe）
├── handq_config.yaml               ← 本地工作配置（在 .gitignore 中，由 example 拷贝得到）
├── handq_config.example.yaml       ← 跟进 git 的模板（API_KEY 留空，作为 ship-default）
├── requirements.txt                ← Python 依赖（与 packaging\build.ps1 的 --include-package 对齐）
├── scripts/
│   ├── handq_post_commit.py        ← Git hook 源（bridge 安装到 .git/hooks/post-commit）
│   └── start_chrome_with_debug.bat ← Edge/Chrome attach 模式启动器
├── packaging/
│   └── build.ps1                   ← Nuitka + electron-builder 一键打包
├── electron/                       ← 独立 npm 包
│   ├── main.js                     ← Electron 主进程
│   ├── updater.js                  ← SMB 共享更新通知器（§5）
│   ├── preload.js                  ← IPC 桥接层
│   ├── renderer/                   ← UI
│   ├── package.json                ← electron-builder + extraFiles 配置
│   └── node_modules/
├── src/                            ← Python 后端
│   ├── bridge/stdio_bridge.py      ← stdio JSON 调度器
│   ├── controller/
│   ├── infrastructure/
│   └── ...
└── ARCHITECTURE.md                 ← 本文件
```

### 启动流程

1. 用户启动 `HandQ.exe`（或开发者在 `electron/` 下 `npm start`）。
2. Electron 解析 bridge 启动命令：
   - **Packaged**（`app.isPackaged === true`）：`<install_root>\handq-bridge.exe`
   - **直接运行**：`process.env.HANDQ_PYTHON || 'python'` + `<repo>\bridge_main.py`
3. `spawn(cmd, args, { env, stdio: pipe×3 })` —— **不传 cwd**。
4. bridge 计算 `INSTALL_DIR = dirname(sys.executable | __file__)`。
5. 配置查找：`HANDQ_CONFIG` env → `%USERPROFILE%\HandQ\handq_config.yaml` → `<INSTALL_DIR>\handq_config.yaml`。
   - Windows 首次启动时，若 user 根下不存在，自动从 install_root 的 ship-default 拷贝过去。
6. 框架日志落到 `%USERPROFILE%\HandQ\logs\<TS>\`，diag 落到 `%USERPROFILE%\HandQ\logs\.dia\`（NTFS HIDDEN）。
7. `stdio_bridge.run()` 进入 IPC 主循环；首个 `request` 时 `_allocate_session_dir(goal)` 在 `%USERPROFILE%\HandQ\History\<TS>-<slug>\` 创建 session 目录。

---

## 3. 打包管线

### 3.1 一键打包

```powershell
.\packaging\build.ps1                # 全量构建（bridge + installer）
.\packaging\build.ps1 -Clean         # 清缓存重建
.\packaging\build.ps1 -BridgeOnly    # 仅 Nuitka
.\packaging\build.ps1 -ElectronOnly  # 仅 electron-builder（复用已有 bridge dist）
```

脚本两步：

**Step 1 — Nuitka standalone**：把 `bridge_main.py` 编译为 `bridge_main.exe`，输出到 `dist\.nuitka_cache\bridge_main.dist\`，含 `_internal/` 依赖、`handq_config.yaml`（来自 `handq_config.example.yaml`，API_KEY 留空）。完成后重命名为 `handq-bridge.exe`（Electron `resolveBridgeLaunch()` 期待的名字）。

**Step 2 — electron-builder**：在 `electron\` 下跑 `npx electron-builder --win nsis --x64`。`electron\package.json` 的 `extraFiles` 把 bridge dist 平铺到 NSIS 安装根，并把 `scripts\*.bat` + `scripts\*.py` 复制到 `<install_root>\scripts\`，让 `_install_post_commit_hook()` 在 packaged 模式下也能找到 hook 源。

**单产物原则**：默认只产出 `HandQ Setup x.y.z.exe` 一个 NSIS 安装包。`win.target` 不含 `dir`，`nsis.differentialPackage: false` 关闭 blockmap。需要 unpacked 树（不安装直接跑）时，`cd electron && npm run dist:dir` 单独产出到 `dist\installer\win-unpacked\`，但不进默认管线。

输出：`dist\installer\HandQ Setup x.y.z.exe`（NSIS 安装包，约 200 MB）。

### 3.2 Nuitka 关键开关

`packaging\build.ps1` 中已配置：

- `--include-package=src` —— 我们的代码包。
- `--include-package=...` —— 显式列出每个**条件导入**或**try/except ImportError 保护**的第三方包（Nuitka 静态分析穿不透 try/except）。覆盖 anthropic / openai / playwright / mss / pyautogui / pywin32 / win32com / pythoncom / paramiko / keyring / keyrings.alt / cryptography / cffi / rapidocr_onnxruntime / rapidfuzz / psutil / PIL / yaml / httpx / json_repair。
- `--include-package-data=rapidocr_onnxruntime` —— RapidOCR 的 det/rec/cls `.onnx` 模型文件（约 10MB）。**必须**显式声明，否则 desktop_tool find_element 在打包后报 "model not found"。
- `--include-package-data=win32com` —— gen_py 缓存支持。
- `--include-data-files=handq_config.example.yaml=handq_config.yaml` —— ship-default 配置（API_KEY 留空）。
- `--nofollow-import-to=...` —— 排除 GUI 工具包、Jupyter、CLI 子包、未用 stdlib 协议库等以减小体积。
- `--python-flag=no_docstrings` + `no_site` —— 进一步压缩。

### 3.3 NSIS 配置（`electron\package.json`）

```json
"nsis": {
  "oneClick": false,
  "perMachine": false,
  "allowToChangeInstallationDirectory": true,
  "shortcutName": "HandQ",
  "createDesktopShortcut": true,
  "createStartMenuShortcut": true
}
```

- `oneClick:false` —— 显示安装向导（用户可见 EULA / 路径 / 进度），失败也能看到错误。
- `perMachine:false` —— per-user 安装到 `%LOCALAPPDATA%\Programs\HandQ\`，**不需要 UAC**，自动更新流畅。

---

## 4. 代码索引

| 关注点 | 文件 | 符号 |
|---|---|---|
| Bridge 安装目录探测 | `bridge_main.py` | `_INSTALL_DIR`、`_resolve_config_path` |
| 用户根目录 | `bridge_main.py` | `_user_handq_root` |
| Bridge 配置 env 注入 | `bridge_main.py` | `os.environ["HANDQ_CONFIG"]` |
| 升级合并（PRESERVE/OVERRIDE） | `bridge_main.py` | `_PRESERVE_PATHS`、`_merge_config`、`_merge_user_config_with_seed` |
| Personality desktop_query 注入 | `bridge_main.py` | `_get_desktop_query`（lazy import 桥到 desktop_tool） |
| Electron bridge 启动 | `electron/main.js` | `resolveBridgeLaunch()` |
| Electron 日志目录路由 | `electron/main.js` | `LOG_BASE`、`platformLogBase()` |
| Electron 更新检查 | `electron/updater.js` | `checkForUpdates()` |
| Bridge 配置消费 | `src/bridge/stdio_bridge.py` | `run()` 读 `HANDQ_CONFIG` |
| 并发派发 | `src/bridge/stdio_bridge.py` | `_dispatch`、`_session_dispatch_locks`、`_inflight_by_sid`、`_cancel_inflight`、`_drain_inflight` |
| 会话生命周期 | `src/bridge/stdio_bridge.py` | `_ensure_flow`、`_do_close_session`、`_do_shutdown`、`_force_release_session_locks` |
| Session 目录分配 | `src/bridge/stdio_bridge.py` | `_allocate_session_dir`、`_session_history_root` |
| YAML 读写 | `src/bridge/stdio_bridge.py` | `_load_config_dict`、`_save_config_dict` |
| Git hook 安装 / 卸载 | `src/bridge/stdio_bridge.py` | `_install_post_commit_hook`、`_uninstall_post_commit_hook` |
| Hook 脚本 | `scripts/handq_post_commit.py` | `_memory_db_path`、`_insert_candidate` |
| 桌面跨会话所有权 | `src/tools/desktop_tool.py` | `_GLOBAL_DESKTOP_OWNERSHIP_LOCK`、`is_any_session_holding_desktop`、`DesktopState.acquire_global_takeover` / `_release_global_takeover_if_owned` / `reset_takeover_state` |
| 浏览器 per-session profile | `src/infrastructure/browser_paths.py` | `user_browser_profile_dir(sid)`、`_safe_sid` |
| 浏览器 holder | `src/tools/browser_tool.py` | `BrowserSessionHolder(session_id=...)` |
| Personality 查询模型 | `src/infrastructure/personality/service.py` | `PersonalityMonitor(desktop_query=...)`、`_paused`、`pause_by_user` / `resume_by_user` |
| Idle 桌面释放钩子 | `src/controller_v2/flow_controller.py` | `FlowControllerV2._forward_state_to_ui("idle")` |
| Vision LLM 客户端 | `src/infrastructure/vision/llm.py` | `VisionClient`、`get_vision_client`、`_BUILD_LOCK`、`flush_vision_client` |
| Vision 本地 OCR | `src/infrastructure/vision/ocr.py` | `LocalOCR`（RapidOCR）、`get_local_ocr` |
| Vision 截图分级 | `src/infrastructure/vision/storage.py` | `ScreenshotStore` |

---

## 5. 发版与自动更新

### 5.1 分发模型

发布产物（NSIS 安装包）放在公司内网 SMB 共享。默认路径在 `handq_config.yaml`：

```yaml
update:
  share_path: '\\wine\APTAuto\ADAS\fengxuan\HandQ'   # 设为 '' 关闭更新检查
```

```
\\wine\APTAuto\ADAS\fengxuan\HandQ\
├── HandQ Setup 0.1.0.exe
├── HandQ Setup 0.2.0.exe       ← 开发者扔进来即生效
└── ...（保留旧版用于回滚）
```

**没有 version.json、没有 SHA 文件、没有发布脚本**。文件名 `HandQ Setup x.y.z.exe`（electron-builder 默认 `${productName} Setup ${version}.exe`）即元数据。

share_path 解析顺序（高优先级在前）：

1. `HANDQ_UPDATE_BASE` 环境变量（联调 / per-machine 覆盖）
2. `%USERPROFILE%\HandQ\handq_config.yaml` 的 `update.share_path`
3. `electron/updater.js` 顶部的 `DEFAULT_UPDATE_BASE`（编译期兜底）

把 `share_path` 设为 `''`（空字符串）即可关闭更新检查。

### 5.2 客户端通知机制

`electron/updater.js` 在主窗口 `did-finish-load` 后触发一次：

1. `fs.promises.readdir(UPDATE_BASE)`（5s 超时）；SMB 不可达静默失败。
2. 过滤 `/^HandQ Setup (\d+\.\d+\.\d+)\.exe$/`，取最大版本。
3. 与 `app.getVersion()` 比较。
4. 新版本 → 弹窗 `[打开更新目录并退出, 稍后]`。
5. 用户点主按钮：`shell.openPath(UPDATE_BASE)`（启动独立 explorer 进程）→ `app.quit()`（触发 `before-quit` → 给 bridge 发 shutdown envelope → 2s grace → exit）。

用户在资源管理器里把安装包复制到本地双击安装。**HandQ 进程已退出**，NSIS 不会撞 file-in-use。

### 5.3 发版步骤

```powershell
# 1. 在 master 分支
git switch master && git pull

# 2. bump 版本号（electron/package.json 是唯一权威）
# 手动编辑 electron/package.json 的 "version" 字段，例如 0.1.0 → 0.2.0
# 同步把 handq_config.example.yaml:1 的 `version:` 字段改为相同值——
#   这是 bridge_main._merge_user_config_with_seed 触发升级合并的依据，
#   不改的话老用户那份 yaml 不会被合并到新结构。

# 3. 一键构建
.\packaging\build.ps1

# 4. 验证产物
#    dist\installer\HandQ Setup 0.2.0.exe   ← 唯一产物
# 直接安装到本机（推荐）：双击该 .exe 走 NSIS 向导
# 或者本地不安装跑一下：cd electron && npm run dist:dir 然后跑 dist\installer\win-unpacked\HandQ.exe

# 5. 推到 SMB 共享（updater.js 自动识别新版本）
Copy-Item ".\dist\installer\HandQ Setup 0.2.0.exe" `
          "\\wine\APTAuto\ADAS\fengxuan\HandQ\" -Force

# 6. 提交版本号 bump
git add electron/package.json
git commit -m "release: 0.2.0"
git push
```

### 5.4 用户感知的更新体验

- **下次启动 HandQ** → 弹窗 "HandQ 0.2.0 已发布（当前 0.1.0）"。
- 点"打开更新目录并退出" → 资源管理器跳出 SMB 路径 + HandQ 关闭。
- 用户拖 `HandQ Setup 0.2.0.exe` 到本地 Desktop / Downloads → 双击 → NSIS 向导 → 安装完成。
- 重新打开 HandQ → 已是 0.2.0。

### 5.5 SMB 路径覆盖（联调用）

`updater.js` 支持 `HANDQ_UPDATE_BASE` 环境变量临时覆盖（最高优先级），不动 yaml：

```powershell
$env:HANDQ_UPDATE_BASE = "C:\tmp\fake-update"
cd electron
npm start
```

更长期的修改（比如换发布服务器）应直接编辑 `%USERPROFILE%\HandQ\handq_config.yaml` 的 `update.share_path`，重启 HandQ 即生效。

### 5.6 紧急回滚

把旧版本安装包文件名改个高于当前的版本号（例如把 `HandQ Setup 0.1.5.exe` 改名为 `HandQ Setup 0.99.0.exe`）放回 SMB 路径，所有客户端会被推回到该版本。**不要删除旧版本的安装包**——它们是回滚源。

### 5.7 SmartScreen / 代码签名

**当前发版未做 Authenticode 代码签名**，原因是没有 OV/EV 证书。这导致两类弹窗：

- **首次运行的 SmartScreen 警告**："Windows 已保护你的电脑"。**前提是文件带 MOTW**（Mark of the Web，浏览器下载会打这个标记，从 SMB 共享拷贝**不会**打）。
- **UAC 弹窗中的"未知发布者"**（仅在 per-machine 安装时；本项目 `oneClick:false, perMachine:false` 是 per-user，没有 UAC，无影响）。

> **本项目的工作流天然规避了 SmartScreen** —— 用户从 SMB 共享 `\\wine\...` 复制安装包到本地、双击运行，文件不带 MOTW，SmartScreen 默认跳过检查。前提是用户机器没启用 Smart App Control（默认关闭）、SMB 路径在"本地 Intranet"区（默认）。

如果在某些特殊机器上仍弹警告，告诉用户点 **更多信息 → 仍要运行**。

**未来要彻底消除警告**：

1. 取得证书：
   - **OV 证书**（约 $200-400/年，签发后需积累下载量才有声誉，发布初期仍弹警告）
   - **EV 证书**（约 $400-1000/年，签发后立即获得 SmartScreen 声誉，零警告）
   - **Qualcomm 内部代码签名服务**（推荐，跟 IT 协调）
2. 启用签名：在跑 `packaging\build.ps1` 之前 export 两个环境变量，electron-builder 会自动签名生成的 `HandQ Setup x.y.z.exe`：

   ```powershell
   $env:CSC_LINK = "C:\path\to\handq-cert.pfx"   # 或 HTTPS URL
   $env:CSC_KEY_PASSWORD = "<pfx 密码>"
   .\packaging\build.ps1
   ```

   `electron/package.json` 的 NSIS 配置无需改动；`electron-builder` 检测到 `CSC_LINK` 后会自动签 NSIS installer 和内部 `HandQ.exe`。

3. （可选）签 `handq-bridge.exe`：Nuitka 输出的 exe 默认不签。如果证书覆盖范围允许，在 `packaging\build.ps1` 的 Nuitka 步骤之后用 `signtool` 手动签：

   ```powershell
   signtool sign /fd sha256 /a /tr http://timestamp.digicert.com /td sha256 `
       "$BRIDGE_SRC\handq-bridge.exe"
   ```

   一致地签 bridge + installer 可避免某些 EDR / SmartScreen 把 bridge 标为"次级未签可疑"。

4. **不要**用自签证书。SmartScreen 对自签证书的处理比未签更严，反而增加警告概率。

### 5.8 Bridge 启动失败诊断

如果 `handq-bridge.exe` 启动失败（配置损坏、端口占用、依赖缺失等），用户不再看到"卡在 Starting…"这种无信息状态。`electron/main.js` 在 `spawnBridge()` 中：

- 记录 spawn 时间，缓存最近 50 行 bridge stderr。
- 通过 `boot_progress phase=stdio_loop_ready` 或任何非 error 类型的 IPC envelope 标记"已启动"。
- 在 `child.on('exit')` 检测：如果**未启动**或**启动后 10s 内退出**，且不是用户主动 quit（`isQuitting` / `isShuttingDown` 都未设），弹错误对话框。

对话框内容：
- exit code / signal、bridge 是否到达 `stdio_loop_ready`、日志文件路径
- 最近 20 行 bridge stderr（完整 traceback 通常在这）

按钮：
- **打开日志目录并退出** → `shell.openPath(LOG_DIR)` + 退出
- **重置配置并重启** → 把 `%USERPROFILE%\HandQ\handq_config.yaml` 重命名为 `handq_config.yaml.broken-<TS>`，触发 first-run 重新拷贝 ship-default；`app.relaunch()` + `app.quit()`。**用户的 API_KEY 会丢**，但旧文件保留为 `.broken-<TS>` 可以手动恢复。
- **退出**

一次启动只弹一次（`_crashDialogShown` 哨兵）。"是否启动"判定既看到 boot_progress 的 phase，也看到任意非 error 类型的 envelope —— 兼容 stdio_loop_ready 之前就开始服务的场景。

---

## 6. 不变量速查

- `handq-bridge.exe` 必须**与 `HandQ.exe` 同级**，使 `path.dirname(app.getPath('exe'))` 能正确指向它。
- `_internal/`（Nuitka 运行时依赖）与 `handq-bridge.exe` 同级。
- `handq_config.yaml` 与 `handq-bridge.exe` 同级，让 `INSTALL_DIR/handq_config.yaml` 解析到正确的文件。
- `scripts/handq_post_commit.py` 与 `handq-bridge.exe` 同级（在 `<install_root>\scripts\` 下），让 `_hook_source_path()` 在 frozen 模式下能找到 hook 源。
- `electron/package.json` 的 `version` 字段是唯一版本权威；NSIS 文件名、`app.getVersion()`、updater 比对都依赖它。
- 一切用户写入物都在 `%USERPROFILE%\HandQ\` 下，`<install_root>` 由用户级安装器写入后即只读。

---

## 8. 远程委托（分身）

Windows HandQ 可以将复杂任务委托给远程 Linux HandQ 代理自主执行。用户在对话中指明远端地址（如"在 fengxuan@xfeng-lnx 上分析代码"），planner 据此设置 `ssh_target` 并路由到 `remote_handq` 工具。

### 8.1 架构原则

- **通信 only**：Windows 端仅通过 SSH 与已安装的 Linux HandQ 通信，不负责部署
- **Linux 端由用户预装**：用户在 Linux 上运行 `bash handq_setup.sh --config <config>`
- **文件 IPC**：通过 `state.json`（状态）和 `messages/`（消息队列）交互
- **on-demand 工具**：planner 声明 `tools_required: ["remote_handq"]` 时才激活
- **地址由 prompt 提供**：远端地址不在 yaml 配置中硬编码，适应频繁变动的开发机

### 8.2 前置条件

1. Linux 端已安装 HandQ（`handq` 在 PATH 或 `~/.local/bin/`）— 用户运行 `bash handq_setup.sh`
2. Windows 端可 SSH 到该 Linux 主机（凭据由 SSHSetupManager 首次交互时建立）
3. 用户在对话中提供目标地址（`user@hostname`）

### 8.3 通信协议

Windows 端通过 SSH exec 读写 Linux 端的文件 IPC：

| Windows 动作 | 远端操作 | 用途 |
|---|---|---|
| `discover` | probe `~/.handq/<user>@<host>/` + `which handq` | 确认已安装、定位工作目录 |
| `submit_goal` | `handq --new` + write `messages/<ts>.txt` | 启动空闲 HandQ 并提交任务 |
| `get_status` | read `state.json`（可 poll） | 轮询进度 |
| `send_message` | write `messages/<ts>_msg.txt` | 中途注入指令/修正 |
| `get_result` | read `state.json` + tail execution log | 收集最终结果 |
| `exit_handq` | `handq --exit` | 清理关闭 |

### 8.4 Planner 路由

- 远端任务需要推理/规划（"分析代码"、"修复所有测试"）→ `tools_required: ["remote_handq"]` + `ssh_target`
- 远端任务是已知命令（"重启服务"、"跑脚本"）→ `tools_required: ["ssh"]`（Windows agent 驱动）
- 不可组合 `["ssh", "remote_handq"]` 在同一 step

### 8.5 平台限制

- 工具和 provider 仅在 `sys.platform == "win32"` 时注册
- `tool_registry.py`: `if _IS_WINDOWS:` 门控
- `flow_controller.py`: `if not _is_windows: return` 早退

Linux HandQ 不需要委托给自己。

### 8.6 相关文件

| 文件 | 职责 |
|---|---|
| `src/tools/remote_handq_tool.py` | RemoteHandQTool — SSH 通信层（8 actions：discover/submit_goal/send_message/get_status/get_result/new_session/interrupt/exit_handq） |
| `src/infrastructure/remote_handq_setup.py` | RemoteHandQContextProvider — 凭据建立 + HANDQ_DIR 发现 |
| `src/infrastructure/memory.py` | per-host 缓存（`_remote_handq_contexts`） |
| `src/tools/tool_registry.py` | on-demand 注册（Windows-only, line ~954） |
| `src/controller/flow_controller.py` | provider 注册（Windows-only, line ~958） |

---

## 9. 测试覆盖 (tests/v2/)

Controller V2 的完整测试套件位于 `tests/v2/`。详细计划见 `tests/v2/TEST_PLAN_V2.md`。

| 类别 | 测试数 | 运行时间 | 说明 |
|---|---:|---|---|
| 离线确定性测试 | 691 | ~14s | LLM stack、controller_v2 核心、工具层、信号链、风险检查、flow controller |
| LLM 实时集成 | 8 | ~2min | 真实 QGenie 模型（chat/streaming/tool-call + 控制器路由） |
| 端到端实时 (E2E) | 15 | ~17min | 完整 Orchestrator→Planner→Agent→Tools→磁盘，真实 LLM + 真实文件系统 |
| **总计** | **714** | | |

**E2E 覆盖的关键链路：**
- 文件创建/读取/编辑/glob（核心工具链）
- 多步骤规划 + 验证（planner→agent→acceptance gate）
- 中途打断 + 重定向（interrupt_current 机制）
- 工具路由激活（coding / ssh on-demand）
- 承诺泄漏防护（commitment-leak guard）
- 结构守卫（structural guard — 失败后不静默完成）
- 反重复机制（anti-repeat pivot）
- 多轮会话状态持久化
- 并发消息（rapid-fire 无死锁）
- 精确内容验证 + acceptance EXTEND 自限循环

**运行命令：**
```powershell
# 快速健康检查（离线）
python -m pytest tests/v2/ -q -m "not live" --timeout=60

# 完整端到端（需要 api.txt + QGenie 可达）
python -m pytest tests/v2/ -m live --timeout=600 -v
```

---

## 10. 待办

| # | 项目 | 状态 |
|---|---|---|
| 1 | 代码签名（Authenticode）—— 见 §5.7；当前内网 SMB 工作流天然规避 SmartScreen，证书是可选项 | ⬜（已留好 `CSC_LINK` env 接入点） |
| 2 | 渲染层"手动检查更新"按钮（`updater.checkForUpdates` 已就绪，仅缺 UI 触发） | ⬜ |
| 3 | `handq_config.yaml` schema 校验（YAML 写坏时给用户友好提示） | ⬜ |
| 4 | Bridge 启动失败时的用户可见诊断 | ✅ §5.8 |
| 5 | 多 session 并发模型 | ✅ §1.7 + `MULTI_SESSION_DESIGN.md` |
| 6 | 孤儿 browser_profile\sessions\<sid>\ 目录清理（用户关 session 后留下） | ⬜（孤儿目录不影响运行，未来加后台清理） |
