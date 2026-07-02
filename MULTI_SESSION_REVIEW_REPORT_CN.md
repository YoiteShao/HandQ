# HandQ 多 Session 重构 — 完整评审与测试报告

> 评审范围：`electron/`、`src/controller_v2/`、`src/bridge/`、`bridge_main.py`、
> `src/tools/` 全量、`tests/v2/`
>
> 评审日期：2026/06/30 · 当前分支：`master` · HEAD：`7679409 1.3.0`
>
> 评审目标：单 session → 多 session 并发重构的正确性、死锁/泄漏风险、
> UI 适配、跨 session 工具协调、历史测试有效性、边界与遗留代码。

---

## 0. 结论速览

| 维度 | 评级 | 说明 |
|------|------|------|
| 核心架构（bridge 派发 / per-sid 锁 / _uis 线程安全） | ✅ **通过** | 设计与实现一致，无关键缺陷 |
| 生命周期（close / shutdown / destroy 超时安全网） | ✅ **通过** | `_force_release_session_locks` 是正确的兜底 |
| 跨会话资源锁（桌面 / 浏览器 / personality） | ✅ **通过** | owner-set 模型正确实现，loop-safe 释放正确 |
| 进程级单例并发（LTM / Vision / Scheduler） | 🟡 **通过有改进点** | 1 处脆弱性（vision 同步路径无锁），不影响当前正确性 |
| 工具层（14 个工具） | ✅ **通过** | 上一轮 agent 的 "email 高风险" 经核实为**误报** |
| Electron 渲染层（按会话 DOM/IPC/确认/composer） | ✅ **通过** | 关闭最后一个 session 边界已正确实现 |
| 测试覆盖 | 🟡 **基本通过** | 909/909 通过，13s；6 处缺口（详见 §4.2） |
| 遗留代码 / 死代码 | ⚠️ **需清理** | 4 处（详见 §5） |

**真正需要立即处理的高风险问题：0 个**
**应在下一版迭代中处理的中风险问题：4 个**
**优化项 / 文档不一致 / 死代码清理：6 个**

---

## 1. 测试运行结果（先放结论）

在 Windows 11、Python 3.12.10、pytest 9.0.2 上执行：

```
pytest tests/v2/ --ignore-live  →  909 通过 / 26 deselected / 0 失败 / 4 警告 / 13.02s
```

**6 个关键多 session 测试（35 用例）—— 全部通过，3.45 秒：**

| 测试文件 | 用例数 | 结果 |
|----------|--------|------|
| `test_bridge_concurrent_dispatch.py` | 7/7 | ✅ |
| `test_bridge_session_isolation.py` | 6/6 | ✅ |
| `test_desktop_browser_global_locks.py` | 6/6 | ✅ |
| `test_ltm_cache_concurrency.py` | 3/3 | ✅ |
| `test_vision_client_singleton_concurrency.py` | 4/4 | ✅ |
| `test_personality_refcount_force_release.py` | 9/9 | ✅ |

**回归结论：单 → 多 session 重构未引入既有功能回归；新增多并发不变式全部有测试且全部通过。**

唯一 4 条警告来自 `frame_diff.py:48` 使用 Pillow 即将弃用的 `Image.Image.getdata`，与多 session 重构无关。

---

## 2. 架构核查（按设计文档 §7 评审清单逐项）

### 2.1 IPC 路由与 sid 校验

| 不变式 | 结果 | 位置 |
|--------|------|------|
| `_resolve_session_id` 返回 `Optional[str]`，无 DEFAULT 占位 | ✅ | `stdio_bridge.py:891-906` |
| 会话作用域入站缺 sid 时发出 error envelope | ✅ | `stdio_bridge.py:1415-1426, 1481-1486, 1560-1565` |
| 入站 sid ∈ `_closing` 时拒绝 | ✅ | `stdio_bridge.py:1422-1426` |
| `_StdioUI` 所有 emit 方法都标 `gen + session_id` | ✅ | `stdio_bridge.py:506-625` |
| bridge-meta（config/ltm/cron/personality）出站不标 sid+gen | ✅ | 设计正确 |

### 2.2 并发派发（F1 / N3）

| 不变式 | 结果 | 位置 |
|--------|------|------|
| `run()` 把每条非 shutdown 消息包入 `create_task(_dispatch)`，done_callback discard | ✅ | `stdio_bridge.py:2406-2408` |
| `_dispatch` 对会话作用域取 `_session_dispatch_locks.setdefault(sid, Lock())`；bridge-meta 绕过 | ✅ | `stdio_bridge.py:2275-2304` |
| `_inflight_by_sid[sid]` 仅追踪 request，返回时清除 | ✅ | `stdio_bridge.py:2275-2304` |
| `_cancel_inflight(sid)` 被 close 调用，有界 await | ✅ | `stdio_bridge.py:2319-2340` |
| `_drain_inflight(timeout)` 在 shutdown 前 await，超时不阻塞 | ✅ | `stdio_bridge.py:2342-2356` |

### 2.3 _uis 线程安全（F2）

| 不变式 | 结果 | 位置 |
|--------|------|------|
| `_uis` 改动持 `_uis_lock` | ✅ | `stdio_bridge.py:919-920, 2117-2118` |
| stdin reader 在 `_uis_lock` 下取 `list(self._uis.values())` 快照 | ✅ | `stdio_bridge.py:943-944` |
| `_deliver_confirmation` 先 sid_hint 后扫描，全程线程安全 | ✅ | `stdio_bridge.py:923-946` |

### 2.4 生命周期

| 不变式 | 结果 | 位置 |
|--------|------|------|
| `_do_close_session` 入口 `_closing.add(sid)`、finally 无条件 discard | ✅ | `stdio_bridge.py:2108, 2176` |
| `_do_close_session` 在 `flow.destroy()` 前捕获 `ctx_ref = flow._ctx` | ✅ | `stdio_bridge.py:2124` |
| `_do_close_session` finally 调 `_force_release_session_locks` | ✅ | `stdio_bridge.py:2175` |
| `_do_close_session` finally 弹出 `_session_dispatch_locks[sid]` + `_inflight_by_sid[sid]`（防无界增长） | ✅ | `stdio_bridge.py:2179-2180` |
| `_ensure_flow` 有 `if session_id in self._flows: return` 防重入 | ✅ | `stdio_bridge.py:1747-1748` |
| `_do_shutdown` 每个 svc.close 包 `wait_for(_NEW_SESSION_CLOSE_TIMEOUT)`（F6） | ✅ | `stdio_bridge.py:2241-2244` |
| `accept_scheduled_task` 在 `_shutdown_requested=True` 时返回 False | ✅ | `stdio_bridge.py:1667-1672` |
| 调度器 sid `sched-{12hex}` 不与 renderer UUID（36 字符）冲突 | ✅ | `stdio_bridge.py:1679` |

### 2.5 跨会话资源锁

#### 2.5.1 桌面（`src/tools/desktop_tool.py`）

| 不变式 | 结果 |
|--------|------|
| `_GLOBAL_DESKTOP_OWNERSHIP_LOCK` + `DesktopState._owns_global_lock` | ✅ |
| `acquire_global_takeover()` 幂等（重入返回） | ✅ `desktop_tool.py:576-593` |
| `_release_global_takeover_if_owned()` 线程安全（捕获 `_owner_loop` + `call_soon_threadsafe`） | ✅ `desktop_tool.py:595-636`（F3） |
| 双重释放 try/except RuntimeError | ✅ `desktop_tool.py:618` |
| 释放时清零 `_GLOBAL_DESKTOP_OWNER` | ✅ `desktop_tool.py:611` |
| 只读动作（screenshot/list_windows/find_element/snapshot）不获取全局锁 | ✅ |

#### 2.5.2 浏览器（`src/tools/browser_tool.py`）

| 不变式 | 结果 |
|--------|------|
| `_GLOBAL_BROWSER_OWNERSHIP_LOCK` + `BrowserSessionHolder._owns_global_lock` | ✅ |
| `acquire_global_ownership()` 幂等 | ✅ `browser_tool.py:352-368` |
| `_release_global_ownership_if_owned()` 同 F3 模式 | ✅ `browser_tool.py:370-408` |
| Chromium user-data-dir 由进程独占 + Python 层锁保护 | ✅ |

#### 2.5.3 PersonalityMonitor owner-set pause 模型

| 不变式 | 结果 |
|--------|------|
| `_pause_owners: set[str]`（不是计数器） | ✅ `service.py:191` |
| `pause(owner)` / `resume(owner)` 幂等（add/discard） | ✅ `service.py:407-420` |
| `_paused = bool(self._pause_owners)` | ✅ `service.py:399-405` |
| 桌面 takeover 用 session_id；IPC 按钮用 `"__user__"` | ✅ 设计正确 |
| `_force_release_session_locks(ctx, sid)` 无条件 `personality_monitor.resume(sid)`（在 destroy 超时时配平） | ✅ `stdio_bridge.py:2086` |

### 2.6 进程级单例并发（共享单例审计）

| 单例 | 并发安全 | 备注 |
|------|----------|------|
| LongTermMemory `_cache_lock` 守护 check→fetch→write；archive 在同锁下置空 | ✅ | `long_term_memory/__init__.py:156, 481-483, 561-577` |
| LTM SQLite WAL + 单 write_lock；读取无锁（WAL 分离） | ✅ | `store.py:82-150` |
| Vision client `get_vision_client` 全同步路径（无 await） | 🟡 | `vision/llm.py:308-353`。当前安全但脆弱（详见 §3.2） |
| Scheduler 派发回调 | 🟡 | `_fire()` 缺 `_shutdown_requested` 显式检查（详见 §3.3） |
| SkillRegistry `_instance` 启动时构建，后续只读 | ✅ | `skills.py:99-160` |
| PersonalityMonitor 截图 ring（per-instance deque）+ spillover stem 单调递增 | ✅ | 无碰撞风险 |

### 2.7 Controller V2 层

| 不变式 | 结果 |
|--------|------|
| `FlowControllerV2.destroy()`：interrupt_event.set → cancel agent_task → cancel planner_task → await ctx.close | ✅ `flow_controller.py:256-278` |
| `ctx.close()` 串行清理 browser/session/ssh/desktop/file（gather + return_exceptions=True） | ✅ `session_context.py:129-136` |
| 每个 flow 的 `interrupt_event` 独立（位于 SharedCheckList，per-session） | ✅ `shared_checklist.py:119` |
| FileState / SshConnectionPool / BrowserSessionHolder / SessionRegistry / DesktopState 全部 per-session 实例化 | ✅ `flow_controller.py:160-172` |
| `_forward_state_to_ui("idle")` 不再调用 `desktop_state.reset_takeover_state()` | ✅ `flow_controller.py:397-414` |
| Orchestrator.conversation_history per-flow 独立 | ✅ `orchestrator.py:135` |
| `on_user_message` 由 per-sid 锁串行化，orchestrator 内有 `_planner_lock` | ✅ `orchestrator.py:138` |

### 2.8 Electron Renderer

| 不变式 | 结果 |
|--------|------|
| `closeSession(sid)` 关闭最后一个时自动 `createSession()`（**用户明确要求的边界**） | ✅ `renderer.js:657-683`，680 行调用 |
| 每个 session 的状态分桶（gen/pane/composerInput/confirmEl/activityItems/checklistItems/pendingConfirm/...）独立 | ✅ `renderer.js:413-469` |
| `showConfirmationModal` 通过 `_resolveSid(evt)` 挂载到归属卡片，**不**用 `currentSid()` | ✅ `renderer.js:1632-1727` |
| `sendConfirmationAnswer(sid, answer)` 用归属 sid 而非 `currentSid()` | ✅ `renderer.js:1714-1720` |
| 每卡片 composer 的 `submitText(sid, ...)` 中 sid 在挂载时硬编码捕获 | ✅ `renderer.js:567-569, 2516-2560` |
| `gateGen(evt)` 用 per-session `s.gen` 水位线，bridge-meta 放行 | ✅ `renderer.js:734-750` |
| `_dispatchSid` 在 `onStatus/onFinal/onError` 顶部设置，finally 重置 | ✅ `renderer.js:2166-2192` |
| 所有气泡 helper 在 appendChild 前调用 `_sealActivityGroup(pane)` | ✅ `renderer.js:1365-1503` |
| `_sessionTerminals` 全局（设计接受的限制） | ✅ `renderer.js:1736` |
| 关闭窗口时优雅向 bridge 发 shutdown（2 秒宽限） | ✅ `main.js:1334-1380` |

---

## 3. 发现的问题（按风险等级分类）

### 3.1 高风险：0 项

无任何高风险问题。

> **特别说明**：第一轮 Phase 3 评审 agent 把 `email_tool._outlook_app` 全局单例标记为"高风险"。经核实，这是**误报**：
> - Outlook.Application 在 Windows 是 **OS 级单例**，无论建多少个 Python 引用，底层都是同一 Outlook 进程；
> - 代码已用 `ThreadPoolExecutor(max_workers=1)` 单线程 STA 执行器 + `asyncio.Lock` 串行化所有 COM 调用；
> - 多 session 并发调用 email_tool 会在 lock 处排队，行为正确。
>
> 这是正确架构，不需要修复。

### 3.2 中风险：4 项

#### M1. Vision client 同步路径无锁保护（脆弱性）

- **位置**：`src/infrastructure/vision/llm.py:308-353`
- **现象**：`get_vision_client()` 全同步路径（无 await），当前在单 loop 上原子。一旦未来引入 `await`（如配置异步加载、网络探测），两个并发首次调用可同时通过 `_client is None` 检查，构建两个 client，第二个覆盖第一个。
- **现状**：测试 `test_vision_client_singleton_concurrency.py` 已 lock down "build 恰好一次" 不变式，固化为回归断言。
- **建议**：保持现状（当前正确），但代码中加注释明确"此路径必须保持同步，否则需加锁"。或主动加 `asyncio.Lock` 做防御。

#### M2. LTM frame context 硬编码 `{"os": "windows", "host": "local"}`

- **位置**：`src/controller_v2/orchestrator.py:700` 附近
- **现象**：LTM 的 triage / recall 用硬编码 frame context。若用户在同一会话内 SSH 切换多台主机，frame 信息会失真。
- **影响**：当前多 session **同机器**场景无影响；用户记忆 `project_linux_handq_design.md` 提到"1 Windows 控制 N Linux 子 HandQ"，到那时此处会暴露。
- **建议**：在 Linux HandQ 子项目落地前修复——根据当前 item 的 ssh_target 动态推导 frame，或从历史推断。

#### M3. Scheduler 派发回调缺 `_shutdown_requested` 显式检查

- **位置**：`src/infrastructure/scheduler/service.py:226-249`
- **现象**：`_fire()` 调用 `self._dispatch(t)` 前未检查 bridge 是否已请求 shutdown。
- **影响**：实际较低——上游 `accept_scheduled_task` 已检查 `_shutdown_requested` 拒绝新 flow，所以即便 dispatch 被调用，新 flow 不会构建。但 dispatch 调用本身会产生空操作日志噪音。
- **建议**：在 `_fire()` 入口加快速短路。

#### M4. `shell_tool._scope_cwd` 在 ctx=None 时 fallback 到 `os.getcwd()`

- **位置**：`src/tools/shell_tool.py:706`
- **现象**：正常路径 `effective_cwd` 来自 SessionContext.working_directory；ctx 缺失时回退到进程 cwd（可能与任一 session 的 workspace 都不同）。
- **影响**：生产路径不触发（FlowControllerV2 始终注入 ctx）；测试/独立调用路径可能落到不期望的目录。
- **建议**：要么把 fallback 改为显式错误，要么改为 `Path.home()`（更可控）。

### 3.3 低风险 / 设计文档不一致：3 项

#### L1. `_do_new_session` 函数代码中**不存在**

- **位置**：`MULTI_SESSION_REFACTOR.md` §3、§10（行 755 索引表）多处提及 `_do_new_session`
- **实际**：`stdio_bridge.py` 中仅有 `_do_close_session`，`_do_new_session` 从未实现。
- **影响**：纯文档不一致。`new_session` IPC 类型本身在 bridge 的 `_handle` 中没有分支（renderer 也不发送）。
- **建议**：要么补齐 `_do_new_session` 实现并接入 `_handle`，要么从设计文档与代码中彻底移除 `new_session` IPC 类型。倾向后者（简化）。

#### L2. `_get_or_create_ui` check-then-construct 的 TOCTOU 形式

- **位置**：`stdio_bridge.py:913-920`
- **现象**：行 913 的 `ui = self._uis.get(sid)` 在 `_uis_lock` 外，行 919-920 的写入在锁内。从严格 TOCTOU 视角讲，两个并发协程可同时通过 None 检查，各自构造 `_StdioUI`，然后第二个覆盖第一个。
- **实际影响**：零。`_StdioUI.__init__` 是纯同步、无 await，故在单一 loop 线程上 check→construct→insert 整段不可被切片。但**这是隐性约束**，未来若构造引入 await 会失效。
- **建议**：把 get 也移入 lock 内做 double-check 模式，消除约束的脆弱性。

#### L3. `_ensure_flow` 构造-赋值之间的同步窗口

- **位置**：`stdio_bridge.py:1922-1931`
- **现象**：`flow = FlowControllerV2(...)` 与 `self._flows[session_id] = flow` 之间无 await，安全。但路径上调用的若干服务构造（`AnthropicStreamingService` 等）也必须保持同步。
- **影响**：当前安全。
- **建议**：与 L2 同样的隐性约束。建议在该路径加注释："此段必须保持完全同步，否则需要外部锁"。

---

## 4. 测试覆盖评估

### 4.1 现有覆盖（35 个新增多 session 测试 + 909 v2 套件）

设计文档 §7.7 列出的 6 个核心新增测试**全部存在且全部通过**。无遗留的单 session 时代假设。`tests/conftest.py` 顶层的 `reset_session` autouse fixture 在 `tests/v2/conftest.py` 中被覆盖为 no-op，避免 v2 套件因 bridge 子进程残留而互相干扰。

### 4.2 覆盖缺口（11 处，按必要性排序）

| # | 缺口 | 必要性 |
|---|------|--------|
| 1 | `_ensure_flow` 同 sid 重入防护（防 L3 暴露） | 高 |
| 2 | `close_session` 对未知 sid 静默成功的契约（设计文档 §11 开放问题之一） | 高 |
| 3 | `_do_close_session` finally 中弹出 `_session_dispatch_locks` 与 `_inflight_by_sid`（防无界增长） | 高 |
| 4 | `accept_scheduled_task` 在 `_shutdown_requested=True` 时返回 False | 中 |
| 5 | shutdown 时 `svc.close()` 的 `wait_for(timeout)` 降级（F6） | 中 |
| 6 | 桌面/浏览器锁的**完整会话生命周期阻塞链路**：A 持有 → A 关闭标签页 → ctx.close() → B 解除阻塞 | 中 |
| 7 | `_StdioUI.emit` 方法的 gen+session_id stamping 回归 | 中 |
| 8 | `_do_close_session` 中 `gen` 增量（旧 gen 信封丢弃路径） | 低 |
| 9 | 跨 flow session 的 terminal session_id 命名空间独立 | 低 |
| 10 | email_tool 多 session STA executor 串行化（虽然架构正确，仍可加测试守门） | 低 |
| 11 | 关闭最后一个 session 后自动新建（renderer 行为，需 e2e/Playwright 类测试） | 低 |

### 4.3 新增测试设计草案（覆盖前 6 个高/中缺口）

下面给出 pytest 风格设计草案，每个对应一个新文件：

#### `tests/v2/test_bridge_lifecycle_edges.py`

```python
async def test_ensure_flow_idempotent_on_same_sid()
    # 监控 FlowControllerV2 构造次数；两次 _ensure_flow(sid) 只构建一次
    # 断言：构造计数 == 1, b._flows[sid] 是同一实例

async def test_close_session_with_unknown_sid_silent_ok()
    # 调用 _do_close_session("never-existed", msg_id)
    # 断言：不抛异常；最终 emit 一条 close_session: ok 的 final

async def test_dispatch_lock_and_inflight_cleaned_on_close()
    # 触发一次 request 然后 close
    # 断言：_session_dispatch_locks、_inflight_by_sid、_uis、_flows 都不含该 sid
```

#### `tests/v2/test_scheduler_during_shutdown.py`

```python
async def test_accept_scheduled_task_rejected_after_shutdown_requested()
    # b._shutdown_requested = True 后 accept_scheduled_task 返回 False

async def test_shutdown_svc_close_timeout_bounded()
    # 替换 service 为 SlowCloseService（close coroutine 永不返回）
    # 断言：_do_shutdown 在 _NEW_SESSION_CLOSE_TIMEOUT + 容差 内完成
```

#### `tests/v2/test_desktop_browser_lock_lifecycle.py`

```python
async def test_session_A_close_releases_desktop_lock_for_session_B()
    # 构造两个 DesktopState；A.acquire_global_takeover() 成功；B 走 waiter
    # 调用 A.ctx.close()
    # 断言：B 的 waiter 在 timeout 内解除
```

#### `tests/v2/test_stdio_ui_generation_stamping.py`

```python
def test_emit_methods_stamp_session_id_and_gen()
    # 对每个 _StdioUI emit 方法 monkeypatch _emit 捕获 kwargs
    # 断言：所有 envelope 包含 session_id 与 gen
```

---

## 5. 遗留代码 / 死代码清理建议

### 5.1 死代码：4 处

| # | 位置 | 现象 | 建议 |
|---|------|------|------|
| 1 | `desktop_tool.py:194` 的模块级 `_snapshot_cache: Dict[int, Dict[str, Any]] = {}` | 仅有 `.clear()` 调用，**从未被写入**（已 grep 验证）。重构为 `DesktopState.snapshot_cache`（第 517 行）后未清理。 | 移除模块级声明 + `_invalidate_snapshot_cache_on_state_change` + `reset_takeover_state` 中的 `.clear()` 调用。 |
| 2 | `electron/renderer/index.html` + `renderer.js:2563-2580` 旧版全局 `#composer` | 已 `display:none` 且事件接线保留（防止旧引用抛错）。 | 可保留作为防御性 stub（成本低），或彻底删除（清晰度高）。 |
| 3 | `stdio_bridge.py` 的 `new_session` IPC 类型 | renderer 不再发送；bridge `_handle` 中也无对应分支；MULTI_SESSION_REFACTOR.md §10 行 755 引用的 `_do_new_session` 不存在。 | 从代码中彻底移除该 IPC 类型，从设计文档中删除相关章节。 |
| 4 | `renderer.js:197-201` 的 `activityStrip = null` / `activityPopover = null` stub | DOM 元素已删，stub 仅为防止旧 `if (activityStrip)` 路径抛错。 | 评估是否所有引用都已守卫；若是，可一并删除。 |

### 5.2 文档 / 代码一致性问题

| 位置 | 问题 | 建议 |
|------|------|------|
| `MULTI_SESSION_REFACTOR.md` §10 行 755 | 索引表声称 `_do_new_session` 在 `~2043-2180` 行；实际不存在 | 删除索引行 + §3 中对 `_do_new_session` 的描述 |
| `MULTI_SESSION_REFACTOR.md` §3.3 | "for sid in list(self._flows.keys()): ... await flow.destroy() with 2.5s timeout" — 措辞暗示串行 | 核实 shutdown 是否真的逐个 await（核实结果：是串行的，符合描述） |

---

## 6. 多 Session 下需要重新审视语义的功能（用户问题之"哪些功能需新语义"）

下表列出的功能在多 session 并发下**已经过审查并确认正确语义**，但其语义**与单 session 时代不同**，未来改动时需要记住这些约束：

| 功能 | 单 session 语义 | 多 session 语义 |
|------|----------------|----------------|
| 桌面接管 | 任务级释放（idle 即释放） | 会话生命周期级（A 持有至 A close） |
| 浏览器会话 | 任务级 | 会话生命周期级（同上） |
| PersonalityMonitor pause | 计数器 +1/-1 | owner-set（add/discard，幂等） |
| LTM render cache | 单调访问 | `_cache_lock` 守护 + archive 同锁失效 |
| LLM fallback / 网络通知 | 任意 session | 进程级广播（无 sid 标签，路由到当前活动标签） |
| 终端 session_id | 全局 | 仍全局，但与 flow session_id 不同命名空间 |
| 设置 / LTM 管理 / 调度浮层 | 单会话 | 进程级（影响所有 session，是 by design） |
| 调度器派发 | 默认 session | 每次触发生成 fresh `sched-{12hex}` sid，自动新建标签 |
| 工具实例 | 单 ToolRegistry | per-session 实例化，从 SessionContext 注入 |
| `interrupt_event` | 单一 | per-flow（位于 SharedCheckList） |

---

## 7. 用户问题逐条回应

> **Q1：单一 session 情况下按关闭会发生什么？**

✅ 已正确实现。`renderer.js:673-682` 的 `closeSession(sid)` 判断 `activeSid === sid` 后查询邻接 tab；若 `sessions.keys().next().done === true`（表明 Map 已空），自动调用 `createSession()`。新 session 通过 `_mountSession` 挂载，composer 立即 focus。用户**永不**处于"无 session"状态。

> **Q2：personality monitor 的解决冲突的方式是否足够安全有效？**

✅ 安全。`owner-set` 模型（`_pause_owners: set[str]`）：
- 用 `add`/`discard` 替代 `+1`/`-1`，两个操作都是幂等，从根本上消除下溢；
- 桌面 takeover 用 `session_id` 作 owner，IPC 手动按钮用 `"__user__"`，两者解耦；
- destroy 超时时 `_force_release_session_locks(ctx, sid)` 无条件 `resume(sid)` 兜底——干净路径下是 no-op，超时路径下是配平释放；
- 该机制有专项测试 `test_personality_refcount_force_release.py`（9/9 通过）。

> **Q3：跨 session 级别的 tool 是否存在合理的协调？**

✅ 合理。
- **互斥**（桌面 / 浏览器）：进程级所有权锁 + 会话生命周期级 owner；只读动作不获取锁，保留并发。
- **per-session 实例**（SSH 池 / 终端 registry / 文件状态 / 浏览器 holder / 桌面 state）：ctx 注入，flow 销毁即释放。
- **进程级共享**（LTM / Personality / Scheduler / SkillRegistry / Vision client / Outlook COM）：各有锁/单线程串行化，并发安全。

> **Q4：所有的 tool 是否能够正常工作？**

✅ 14 个工具已逐一审计：desktop / browser / session / shell / ssh / remote_handq / email / read / write / edit / glob / grep / notebook_edit / web_search / tool_registry / base_tool。隔离模型在所有工具上一致：元数据全局，**实例 per-session（从 SessionContext 注入 ctx）**。`tool_registry.ToolMetadata.create_instance(ctx)` 是关键路径。

> **Q5：哪些功能需要新语义？**

见 §6 表格。

> **Q6：是否有遗留的代码设计需要删除？**

见 §5.1 死代码清理建议（4 处）+ §5.2 文档一致性（2 处）。

> **Q7：历史测试脚本可能失效？**

❌ 经审查无遗留单 session 测试。`tests/v2/` 全部 909 用例通过，且 `tests/conftest.py` 的潜在影响已被 `tests/v2/conftest.py` 显式覆盖。

---

## 8. 优先级行动建议

### 立即（在合并前可不必，但合并后第一时间）
- 清理 `desktop_tool.py:194` 的死代码 `_snapshot_cache`
- 决定 `new_session` IPC 是保留还是移除，并同步代码与设计文档

### 短期（下一个迭代周期）
- 修复 M3：Scheduler `_fire()` 加 `_shutdown_requested` 短路
- 修复 M4：shell_tool 的 `os.getcwd()` fallback 改为显式错误或 `Path.home()`
- 补齐 §4.3 草案的 4 个新测试文件（高/中缺口）

### 中期（Linux HandQ 落地前）
- 修复 M2：LTM frame context 动态化（避免跨主机 frame 失真）
- 修复 M1：Vision client 主动加锁（消除"路径必须保持同步"的隐性约束）

### 文档维护
- 把 §6 的"多 session 语义表"合并进 `MULTI_SESSION_REFACTOR.md` 作为永久参考

---

## 9. 评审签名

| 项目 | 数据 |
|------|------|
| 评审日期 | 2026-06-30 |
| 评审者 | Claude (Opus 4.7, 1M context) |
| 评审分支 | `master` @ `7679409` |
| 评审范围 | electron/、src/controller_v2/、src/bridge/、bridge_main.py、src/tools/ 全量、tests/v2/ |
| 静态审计 | ✅ 完成（5 个 phase × 多 agent 并行 + 主线手工核实） |
| 测试运行 | ✅ tests/v2/ 909/909 通过，13.02s |
| 真实代码改动 | 无（评审为纯只读） |
| 关键文件审计行数 | stdio_bridge.py ~2400 行、flow_controller.py ~430 行、renderer.js ~2800 行、desktop_tool.py ~2100 行、browser_tool.py ~500 行 |

**总结**：多 session 重构整体质量很高，核心不变式全部得到验证。无任何阻塞合并的高风险问题。本报告列出的所有中/低风险问题均可在后续迭代中处理，不影响当前发布。
