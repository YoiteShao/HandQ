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
2. `%LOCALAPPDATA%\HandQ\handq_config.yaml` — 用户级覆盖。
3. `<INSTALL_DIR>\handq_config.yaml` — 与构建一起 ship 的默认配置。

解析出的绝对路径会被写回 `os.environ["HANDQ_CONFIG"]`，再 import
`src.bridge.stdio_bridge`，下游所有消费方拿到的都是同一个值。

**bridge 不再关心 cwd。** Electron 也不再给 `spawn()` 传 `cwd`。无论是
桌面快捷方式、开始菜单、还是 `cmd /K` 启动，行为完全一致。

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
├── logs/                           ← 运行时日志，每次启动一个子目录
│   └── 20260521-180449/
│       ├── handq-bridge.log        ← Python 端
│       └── handq-frontend.log      ← Electron 端
├── .workspace/                     ← 运行时，每个会话一个沙箱
└── ARCHITECTURE.md                 ← 本文件
```

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
6. 日志落到 `<repo>/logs/<TS>/`。

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

`%LOCALAPPDATA%` 普通用户可写，`Program Files` 不可写。Electron 主进程
会把写入操作引导到这里：

```
%LOCALAPPDATA%\HandQ\
├── handq_config.yaml               ← （可选）用户级覆盖；优先于安装目录里的版本
├── logs\                           ← packaged 模式下自动选这里
│   └── 20260521-181203\
│       ├── handq-bridge.log
│       └── handq-frontend.log
└── workspace\                      ← 会话沙箱（待办）
    └── <session_id>\
```

`electron/main.js` 在 packaged 模式下已经把日志路由到
`app.getPath('userData')/logs`。Python 端遵循 `HANDQ_LOG_DIR`
环境变量（由 Electron 通过 env 传入），所以前后端会写到同一个
"每次启动一个目录" 下。

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
   - 检查 `%LOCALAPPDATA%\HandQ\handq_config.yaml`（用户级覆盖）。
   - 回落到 `<install_root>\handq_config.yaml`（随安装包 ship 的默认值）。
7. 日志落到 `%LOCALAPPDATA%\HandQ\logs\<TS>\`。

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

启动时如发现 `%LOCALAPPDATA%\HandQ\handq_config.yaml` 不存在，
就把 `<install_root>\handq_config.yaml` 复制过去。之后用户的修改
都落在用户可写的副本里，安装目录里的副本保持原样，便于升级时 diff。

这段逻辑应该放在 `bridge_main.py` 的 `_resolve_config_path()`
（或紧邻的一个小 helper）里，这样无论是谁拉起 bridge 都能正确处理。

---

## 5. 代码索引

| 关注点 | 文件 | 符号 |
|---|---|---|
| Bridge 安装目录探测 | `bridge_main.py` | `_INSTALL_DIR`、`_resolve_config_path` |
| Bridge 配置 env 注入 | `bridge_main.py` | `os.environ["HANDQ_CONFIG"]` |
| Electron dev/prod bridge 选择 | `electron/main.js` | `resolveBridgeLaunch()` |
| Electron 日志目录路由 | `electron/main.js` | `LOG_BASE` |
| Bridge 配置消费 | `src/bridge/stdio_bridge.py` | `run()` 读 `HANDQ_CONFIG` |
| YAML 读写 | `src/bridge/stdio_bridge.py` | `_load_config_dict`、`_save_config_dict` |

---

## 6. 迁移清单（当前进度）

| # | 项目 | 状态 |
|---|---|---|
| 1 | `bridge_main.py` 自定位 `INSTALL_DIR` 和配置路径 | ✅ 已完成 |
| 2 | `electron/main.js` `app.isPackaged` 分支选择 py 或 exe | ✅ 已完成 |
| 3 | `electron/main.js` packaged 模式下日志走 `userData` | ✅ 已完成 |
| 4 | 移除 spawn 时 pin 的 `cwd` | ✅ 已完成 |
| 5 | YAML 字段 `api_key_env` / `api_key` 硬切为 `llm.API_KEY`；后端不再走 `os.environ.get` 间接续 | ✅ 已完成 |
| 6 | `handq_config.yaml` 进 `.gitignore` + 提供 `.example.yaml` 模板 | ⬜ 待办 |
| 7 | 首次启动从安装目录复制配置到 `%LOCALAPPDATA%\HandQ\` | ⬜ 待办 |
| 8 | Nuitka 构建脚本（PowerShell 或 Make） | ⬜ 待办 |
| 9 | electron-builder `extraResources` 配置 | ⬜ 待办 |
| 10 | 生产模式下 workspace 目录迁到 `%LOCALAPPDATA%\HandQ\workspace\` | ⬜ 待办 |
