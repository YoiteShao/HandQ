# HandQ 文件架构（py / Nuitka standalone exe / yaml）

本文档是 "每个文件放在哪里、bridge 如何定位配置" 的权威说明。覆盖两种模式：

- **开发模式（Dev）** — 直接从源码树运行，`python` + `npm start`。
- **生产模式（Prod）** — 用 Nuitka 编译 standalone exe 后，由 electron-builder 打包。

两种模式共用同一套代码路径。bridge 是**自定位（self-locating）**的：从不依赖工作目录（cwd）。

---

## 1. 核心原则：bridge 自定位

`bridge_main.py` 在导入任何依赖之前先确定自己的安装目录：

```
INSTALL_DIR =
    parent of sys.executable    若 Nuitka standalone (__compiled__)
                                或 PyInstaller (sys.frozen)
    parent of __file__          其他情况（dev）
```

随后按以下优先级（首个命中即用）选取 `handq_config.yaml`：

1. `HANDQ_CONFIG` 环境变量 — 显式覆盖（CI、便携模式）。
2. `%USERPROFILE%\HandQ\handq_config.yaml` — 用户级配置；与 session 历史
   `%USERPROFILE%\HandQ\History\` 同根，方便用户在一个地方找到所有"属于他自己"的东西。
3. `<INSTALL_DIR>\handq_config.yaml` — 与构建一起 ship 的默认配置（首次启动可拷到 (2)）。

解析出的绝对路径会被写回 `os.environ["HANDQ_CONFIG"]`，再 import
`src.bridge.stdio_bridge`，下游所有消费方拿到的都是同一个值。

**bridge 不再关心 cwd。** Electron 也不再给 `spawn()` 传 `cwd`。无论是
桌面快捷方式、开始菜单、还是 `cmd /K` 启动，行为完全一致。

---

## 1.5 三类数据，三个根（Windows）

```
%USERPROFILE%\HandQ\               ← 用户拥有的数据
  handq_config.yaml                  用户配置（小、可漫游）
  History\                           会话历史（大、不漫游）
    <YYYYMMDD-HHMMSS>-<slug>\        每个 `request` 一个目录
      session_state.json
      executions_logs\
      ... (FlowController 的所有输出)

%LOCALAPPDATA%\HandQ\              ← 机器本地的调试产物
  logs\<YYYYMMDD-HHMMSS>\            每次 Electron 启动一个目录
    handq-frontend.log               main + preload + renderer
    handq-bridge.log                 Python 端框架日志

<install_root>\                    ← 程序文件（默认 %LOCALAPPDATA%\Programs\HandQ）
  HandQ.exe                          Electron 主程序
  handq-bridge.exe                   Nuitka 冻结的 bridge
  handq_config.yaml                  ship 的默认配置（首次启动拷到上面 (1)）
```

切分原则：

| 数据 | 路径 | 漫游？ | 用户可见？ | 生命周期 |
|---|---|---|---|---|
| 用户配置 | `%USERPROFILE%\HandQ\handq_config.yaml` | 是 | 是 | 跨升级 |
| Session 历史 | `%USERPROFILE%\HandQ\History\<id>\` | 否 | 是 | 跨升级，可手动清理 |
| 框架日志 | `%LOCALAPPDATA%\HandQ\logs\<launch>\` | 否 | 否 | 一次启动 |

> 关键变化（vs 早期方案）：废弃 `session.workspace_base` 字段——session
> 根目录强制为 `%USERPROFILE%\HandQ\History\`，不可由 yaml 配置。GUI
> 模式下用户没有"我在哪个工作目录"的心智，所有任务的中间产物都自动
> 留存在各自的 session 目录里，agent 的 `working_directory` 与
> `storage_directory` 都指向该目录（在 `stdio_bridge._allocate_session_dir`
> 里分配）。

---

## 1.6 Vision artifacts: 三级截图存储

视觉相关的图像产物（浏览器/桌面截图、vision_query 工作图、活动监控
帧）按 **producer + 用途** 落到三个分级，每个分级独立配置 retention。
统一定义在 `src/infrastructure/vision/storage.py` 的 `ScreenshotStore`，
三个 producer（browser_tool / desktop_tool / activity_monitor）各持
一个实例，根目录不同但分级语义和配置共享。

**核心原则：这里全是 SCRATCH 空间。** 任何需要长期留存的捕获，
agent 应该用绝对路径写到当前 task 的 session 目录
(`%USERPROFILE%\HandQ\History\<id>\`)，而不是依赖 screenshots/
里的某个分级。screenshots/ 不该承担「长期资产」的语义。

### 分级表

| 类别 | 谁写 | 触发时机 | Retention | LLM 可选 |
|---|---|---|---|---|
| **ephemeral** | vision_query / find_element 的工作图 | 每次 vision 调用内部生成 | LRU(max_files) + 年龄(max_age_minutes)，每写一张触发；session 边界全清 | ❌ producer 内部，schema 不暴露 |
| **task** | 显式 `screenshot` 调用 | agent 主动留档 | session 关闭时按 `retain_after_task_days` 老化扫；max_files 兜底 | ✅ 默认且唯一选项 |
| **activity** | 周期帧（Phase 3） | activity_monitor 主循环 | 年龄 + LRU 双门 | ❌ activity_monitor 独占；其它 producer 写到此目录视为 bug |

> 取消了早期方案里的 `persistent` 分级——长期保留的语义错配（应该走
> session 目录而不是全局 screenshots/）。

### 三个根目录

| Producer | 根目录 |
|---|---|
| browser_tool | `%USERPROFILE%\HandQ\browser_profile\screenshots\<category>\` |
| desktop_tool（Phase 2+） | `%USERPROFILE%\HandQ\desktop_shots\<category>\` |
| activity_monitor（Phase 3+） | `%USERPROFILE%\HandQ\activity\<category>\` |

### 不变量

- **producer 决定根目录，分级名跨 producer 共享**：同一份
  `handq_config.yaml` 的 `screenshots:` 段驱动三个 store。
- **ephemeral 是 producer-internal**：parameter_schema 不暴露给 LLM，
  防止 LLM 误把重要捕获写到容易被清的层。
- **`screenshot` action 不接受分级参数**：相对路径默认进 task；要长留
  agent 用绝对路径写到 session working_directory。
- **activity 仅 activity_monitor 写**：其它 producer 写入此目录视为 bug。
- **清理时机**：写时摊销（每写一张触发自身分级的 LRU+age 清理）+ session
  边界全清（ephemeral 全清 + task 老化扫）。无后台定时器。

### 配置（默认值，节自 handq_config.yaml）

```yaml
screenshots:
  ephemeral:
    max_files: 30
    max_age_minutes: 15
  task:
    retain_after_task_days: 1
    max_files: 100
  activity:
    max_files: 1000
    max_age_days: 1
```

数值刻意取保守值。要 bump 上限请有具体证据（看到 agent 因 retention 丢
上下文）。

---

## 2. 开发模式目录结构

```
HandQ/                              ← repo 根，dev 模式下也是 INSTALL_DIR
├── bridge_main.py                  ← bridge 入口（将来编译成 handq-bridge.exe）
├── handq_config.yaml               ← 用户配置（应进 .gitignore，由 example 拷贝得到）
├── handq_config.example.yaml       ← 跟进 git 的模板（待办：拆出来）
├── src/                            ← Python 后端
│   ├── bridge/
│   │   └── stdio_bridge.py         ← stdio JSON 调度器
│   ├── controller/
│   ├── infrastructure/
│   └── ...
├── electron/                       ← 独立 npm 包
│   ├── main.js                     ← Electron 主进程
│   ├── preload.js                  ← IPC 桥接层
│   ├── renderer/
│   │   ├── index.html
│   │   ├── renderer.js
│   │   └── styles.css
│   ├── package.json
│   └── node_modules/
├── logs/                           ← Dev 模式下保留在 repo 内便于检查
│   └── 20260521-180449/
│       ├── handq-bridge.log        ← Python 端
│       └── handq-frontend.log      ← Electron 端
└── ARCHITECTURE.md                 ← 本文件
```

> Dev 模式下 session 历史依然写到 `%USERPROFILE%\HandQ\History\`——bridge
> 只看 USERPROFILE 而不看是否打包，所以源码运行和正式运行行为一致。
> 仅日志位置不同（dev → repo `logs/`，prod → `%LOCALAPPDATA%\HandQ\logs\`）。

### Dev 启动流程

1. 开发者在 `electron/` 下执行 `npm start`（或 `npm --prefix electron run start`）。
2. `electron/main.js` 看到 `app.isPackaged === false`，将 bridge 启动命令解析为：
   ```
   cmd  = process.env.HANDQ_PYTHON || 'python'
   args = ['<repo>/bridge_main.py']
   ```
3. `spawn(cmd, args, { env, stdio: pipe×3 })` — **不传 cwd**。
4. `bridge_main.py` 计算 `INSTALL_DIR = dirname(__file__) = <repo>`，
   将配置定位为 `<repo>/handq_config.yaml`，并写入 `HANDQ_CONFIG` env。
5. `stdio_bridge.run()` 读取 `HANDQ_CONFIG` 后正常运行。
6. 日志落到 `<repo>/logs/<TS>/`；session 历史落到
   `%USERPROFILE%\HandQ\History\<TS>-<slug>/`（与 prod 一致）。

---

## 3. 生产模式目录结构（Nuitka standalone + electron-builder）

Nuitka 在 standalone 模式下（推荐用 `--standalone --onefile=no`，便于
和 Electron 同步分发，依赖目录可见可调试）产出：

```
bridge_main.dist/
├── bridge_main.exe                 ← 打包阶段重命名为 handq-bridge.exe
├── python3X.dll
├── _internal/                      ← (Nuitka 布局) 打入的依赖
│   ├── *.pyd
│   └── ...
└── ...
```

electron-builder 把这个 `.dist/` 目录作为 `extraResources` 一起 ship
（或将其内容平铺到 `HandQ.exe` 同级）。推荐安装目录布局如下：

```
<install_root>/                     ← 例如 C:\Program Files\HandQ
├── HandQ.exe                       ← Electron 主程序（用户双击的入口）
├── handq-bridge.exe                ← 重命名后的 bridge_main.exe（与 HandQ.exe 平级）
├── handq_config.yaml               ← 安装器写入的默认配置
├── _internal/                      ← Nuitka 依赖（与 handq-bridge.exe 平级）
│   └── ...
├── resources/
│   ├── app.asar                    ← Electron renderer/main 打包包
│   └── ...
├── locales/
└── *.dll                           ← Electron 运行时 DLL
```

关键不变量：

- `handq-bridge.exe` 必须**与 `HandQ.exe` 同级**，使
  `path.dirname(app.getPath('exe'))` 能正确指向它。
- `_internal/`（Nuitka 运行时依赖）与 `handq-bridge.exe` 同级。
- `handq_config.yaml` 与 `handq-bridge.exe` 同级，
  让 `INSTALL_DIR/handq_config.yaml` 解析到正确的文件。

### 用户级运行时数据

`Program Files` 不可写，`%LOCALAPPDATA%`、`%USERPROFILE%` 普通用户均可写。
我们把**用户拥有的数据**（配置 + session 历史）放到 `%USERPROFILE%\HandQ\`，
**机器本地的调试产物**（日志）放到 `%LOCALAPPDATA%\HandQ\logs\`，两者刻意分离：

```
%USERPROFILE%\HandQ\
├── handq_config.yaml               ← 用户级配置；优先于安装目录里的版本
└── History\                        ← 会话历史（无大小限制，可手动清理）
    └── <YYYYMMDD-HHMMSS>-<slug>\
        ├── session_state.json
        └── executions_logs\

%LOCALAPPDATA%\HandQ\
└── logs\                           ← packaged 模式下自动选这里
    └── 20260521-181203\
        ├── handq-bridge.log
        └── handq-frontend.log
```

`electron/main.js` 在 packaged 模式下把日志路由到
`%LOCALAPPDATA%\HandQ\logs\<TS>\`（见 `packagedLogBase()`）。Python 端遵循
`HANDQ_LOG_DIR` 环境变量（由 Electron 通过 env 传入），所以前后端会写到同一个
"每次启动一个目录" 下。

Session 目录由 `stdio_bridge._allocate_session_dir(goal)` 在收到首个
`request` 信封时分配，路径为 `<USERPROFILE>\HandQ\History\<TS>-<slug>\`，
然后传给 `FlowController(working_directory=..., storage_directory=...)`——
两个参数同值，对外只是一个概念。

### Prod 启动流程

1. 用户双击 `HandQ.exe`。
2. Electron 启动，`app.isPackaged === true`。
3. `electron/main.js` 解析 bridge 启动命令为：
   ```
   cmd  = path.join(path.dirname(app.getPath('exe')), 'handq-bridge.exe')
   args = []
   ```
4. `spawn(cmd, args, { env, stdio: pipe×3 })` — **不传 cwd**。
5. `handq-bridge.exe`（被 Nuitka 冻结）检测到 `__compiled__`，
   计算 `INSTALL_DIR = dirname(sys.executable) = <install_root>`。
6. 配置查找：
   - 检查 `HANDQ_CONFIG` env（极少使用）。
   - 检查 `%USERPROFILE%\HandQ\handq_config.yaml`（用户级配置）。
   - 回落到 `<install_root>\handq_config.yaml`（随安装包 ship 的默认值）。
7. 收到首个 `request` 信封时，`stdio_bridge._allocate_session_dir(goal)`
   在 `%USERPROFILE%\HandQ\History\<TS>-<slug>\` 下创建 session 目录，
   作为 `FlowController` 的 `working_directory` + `storage_directory`。
8. 框架日志落到 `%LOCALAPPDATA%\HandQ\logs\<TS>\`。

---

## 4. 构建管线（目标形态——尚未接入）

### 4.1 用 Nuitka 构建 bridge

```powershell
nuitka `
    --standalone `
    --output-dir=build `
    --output-filename=handq-bridge.exe `
    --include-package=src `
    --include-data-files=handq_config.yaml=handq_config.yaml `
    bridge_main.py
```

输出：`build/bridge_main.dist/`，含 `handq-bridge.exe`、`_internal/`、
以及随包带的 `handq_config.yaml`。

### 4.2 把 dist 交给 electron-builder

在 `electron/package.json`（或独立的 builder 配置文件）里：

```json
{
  "build": {
    "extraResources": [
      {
        "from": "../build/bridge_main.dist",
        "to": ".",
        "filter": ["**/*"]
      }
    ]
  }
}
```

`extraResources` 配合 `to: "."` 会把整个 bridge dist 平铺到安装根目录，
让 `handq-bridge.exe`、`_internal/`、`handq_config.yaml` 都成为
`HandQ.exe` 的兄弟——这正是 `resolveBridgeLaunch()` 期待的布局。

### 4.3 首次启动配置复制（推荐，尚未接入）

启动时如发现 `%USERPROFILE%\HandQ\handq_config.yaml` 不存在，
就把 `<install_root>\handq_config.yaml` 复制过去（顺便确保
`%USERPROFILE%\HandQ\History\` 目录存在）。之后用户的修改
都落在用户可写的副本里，安装目录里的副本保持原样，便于升级时 diff。

这段逻辑应该放在 `bridge_main.py` 的 `_resolve_config_path()`
（或紧邻的一个小 helper）里，这样无论是谁拉起 bridge 都能正确处理。

---

## 5. 代码索引

| 关注点 | 文件 | 符号 |
|---|---|---|
| Bridge 安装目录探测 | `bridge_main.py` | `_INSTALL_DIR`、`_resolve_config_path` |
| 用户根目录（config + History） | `bridge_main.py` | `_user_handq_root` |
| Bridge 配置 env 注入 | `bridge_main.py` | `os.environ["HANDQ_CONFIG"]` |
| Electron dev/prod bridge 选择 | `electron/main.js` | `resolveBridgeLaunch()` |
| Electron 日志目录路由 | `electron/main.js` | `LOG_BASE`、`packagedLogBase()` |
| Bridge 配置消费 | `src/bridge/stdio_bridge.py` | `run()` 读 `HANDQ_CONFIG` |
| Session 目录分配 | `src/bridge/stdio_bridge.py` | `_allocate_session_dir`、`_session_history_root` |
| YAML 读写 | `src/bridge/stdio_bridge.py` | `_load_config_dict`、`_save_config_dict` |
| Vision LLM 客户端 | `src/infrastructure/vision/client.py` | `VisionClient`、`get_vision_client`、`flush_vision_client` |
| Vision 截图分级 | `src/infrastructure/vision/storage.py` | `ScreenshotStore`（ephemeral/task/activity） |

---

## 6. 迁移清单（当前进度）

| # | 项目 | 状态 |
|---|---|---|
| 1 | `bridge_main.py` 自定位 `INSTALL_DIR` 和配置路径 | ✅ 已完成 |
| 2 | `electron/main.js` `app.isPackaged` 分支选择 py 或 exe | ✅ 已完成 |
| 3 | `electron/main.js` packaged 模式日志走 `%LOCALAPPDATA%\HandQ\logs` | ✅ 已完成 |
| 4 | 移除 spawn 时 pin 的 `cwd` | ✅ 已完成 |
| 5 | YAML 字段 `api_key_env` / `api_key` 硬切为 `llm.API_KEY`；后端不再走 `os.environ.get` 间接续 | ✅ 已完成 |
| 6 | 废除 `session.workspace_base`；session 强制为 `%USERPROFILE%\HandQ\History\<id>\` | ✅ 已完成 |
| 7 | `handq_config.yaml` 进 `.gitignore` + 提供 `.example.yaml` 模板 | ⬜ 待办 |
| 8 | 首次启动从安装目录复制配置到 `%USERPROFILE%\HandQ\` | ⬜ 待办 |
| 9 | Nuitka 构建脚本（PowerShell 或 Make） | ⬜ 待办 |
| 10 | electron-builder `extraResources` + per-user NSIS 安装到 `%LOCALAPPDATA%\Programs\HandQ` | ⬜ 待办 |
