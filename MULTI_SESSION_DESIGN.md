# HandQ 多会话并发模型 — 设计与实现

> **范围**：单个 `handq-bridge` 进程承载 N 个并发的 `FlowControllerV2` 实例，
> 以 `session_id` 为键。renderer 中每个会话拥有一张永远可见的卡片，可与
> 其他会话**真正并发**地推进任务（除了一个物理屏幕只能由一个 session 同
> 时驱动输入这一不可绕开的物理约束）。本文档描述当前实现，不阐述历史
> 变更。
>
> **读者**：本仓库的下一位 AI / 工程师。
>
> **相关代码**：`src/bridge/stdio_bridge.py`、`bridge_main.py`、
> `src/controller_v2/flow_controller.py`、`src/tools/desktop_tool.py`、
> `src/tools/browser_tool.py`、`src/infrastructure/personality/service.py`、
> `electron/renderer/renderer.js`、`electron/renderer/index.html`、
> `tests/v2/test_multisession_*.py`。
>
> **架构上位文档**：`ARCHITECTURE.md`（用户根目录布局、打包、发版、启动
> 流程）。本文档专注于多会话的并发与隔离语义。

---

## 1. 心智模型

```
┌──────────────────────────────────────────────────────────────┐
│ Renderer (Electron)                                          │
│                                                              │
│  Tab bar:  [ Session 1 ✕ ] [ Session 2 ✕ ] … [ + ]           │
│            ^focused (focus aid only — never show/hide)       │
│                                                              │
│  #conversation  (horizontal TILED row — flex-direction:row)  │
│    .session-card[data-sid=A]   ← visible, side by side       │
│    .session-card[data-sid=B]   ← visible, side by side       │
│    .session-card[data-sid=C]   ← visible, side by side       │
│                                                              │
│  Each card owns:                                             │
│    - header: name · status pill · close ✕                    │
│    - .session-card-body: scrollable bubbles + checklist      │
│    - .session-card-confirm: inline confirmation host         │
│    - .session-card-composer: per-pane input + send button    │
└──────────────────────────────────────────────────────────────┘
              │  JSON-line IPC over stdio
              ▼
┌──────────────────────────────────────────────────────────────┐
│ StdioBridge (single process, single asyncio loop)            │
│                                                              │
│  Per-sid state (Dict[sid, ...]):                             │
│   _flows                    FlowControllerV2 per session     │
│   _services_by_session      List[LLMService] per session     │
│   _uis                      _StdioUI delegate per session    │
│   _generations              gen watermark per session        │
│   _engine_log_handlers      per-session handq-engine.log     │
│   _session_dispatch_locks   per-sid FIFO mutex (F1)          │
│   _inflight_by_sid          running request per session (N3) │
│   _closing                  sids currently being torn down   │
│                                                              │
│  Cross-session state:                                        │
│   _uis_lock                 threading.Lock guarding _uis     │
│                              against reader-thread snapshot  │
│   _inflight_tasks           set of all dispatch tasks        │
│                              (used by drain on shutdown)     │
│                                                              │
│  Process-wide singletons (NOT per-session):                  │
│   personality_monitor       activity capture (one daemon)    │
│   scheduler                 cron-style task dispatcher       │
│   LongTermMemory            shared knowledge base            │
│   SkillRegistry             read-only skill metadata         │
│                                                              │
│  Cross-session lock (only one):                              │
│   _GLOBAL_DESKTOP_OWNERSHIP_LOCK   one physical screen,      │
│                                    one driver at a time      │
└──────────────────────────────────────────────────────────────┘
```

**第一性原理：用户应能像同时打开多个应用实例一样使用多个 session。**
唯一无法绕开的是物理屏幕 —— 一块屏幕只有一套鼠标键盘，多个 session 同
时驱动输入是没有意义的，所以 desktop 必须串行。其他一切（agent 推理、
浏览器、文件、SSH 终端、shell 命令）都是**真并发**。

---

## 2. IPC 协议

### 2.1 信封规则

- **会话作用域入站**信封**必须**携带 `session_id`：
  - `request`、`user_input`（kind: `message` / `confirmation` /
    `desktop_takeover_revoked`）、`close_session`
  - 缺失 `session_id` 时 bridge 用 `error` 信封拒绝
- **bridge-meta 入站**信封**不携带** `session_id`（进程级）：
  - `config_get`、`config_set`、`shutdown`、所有 `ltm_*`、所有 `cron_*`、
    `personality_status` / `personality_pause` / `personality_resume`、
    `ltm_remember`
- **会话作用域出站**信封同时携带 `session_id` 与 `gen`（由 `_StdioUI`
  依据其捕获的 `(generation, session_id)` 标记）
- **bridge-meta 出站**信封未加标记 —— renderer 视为"始终接受"并作为
  系统消息路由到当前活动标签页：
  - LLM fallback / 网络断开/等待/恢复、llm_server_error
  - config/ltm/cron 处理器的 final 响应
  - 启动进度（boot_progress）、bridge_exit
  - shutdown final

### 2.2 IPC 类型表

| 类型 | 方向 | 会话作用域？ | 用途 |
|------|------|-------------|------|
| `request` | renderer → bridge | ✅ 需 sid | 用户消息：触发 `_ensure_flow` + `flow.on_user_message` |
| `user_input` | renderer → bridge | ✅ 需 sid | 确认应答 / 中途消息 / 桌面接管撤销 |
| `close_session` | renderer → bridge | ✅ 需 sid | 拆除会话；由每个标签页的 X 按钮触发 |
| `config_get` / `config_set` | renderer → bridge | ❌ | 全局配置读写 |
| `ltm_*` | renderer → bridge | ❌ | LTM 浏览 / 编辑 / 归档 |
| `cron_*` | renderer → bridge | ❌ | 调度任务 CRUD |
| `personality_*` | renderer → bridge | ❌ | 手动暂停 / 恢复活动监控 |
| `shutdown` | renderer → bridge | ❌ | 进程退出 |

### 2.3 `_resolve_session_id`（`stdio_bridge.py:891`）

```python
def _resolve_session_id(self, msg) -> Optional[str]:
    sid = msg.get("session_id")
    return sid.strip() if isinstance(sid, str) and sid.strip() else None
```

返回 `Optional[str]`。**没有 DEFAULT_SESSION_ID 回退** —— renderer 永远
生成 UUID；scheduler 派发时生成 `sched-{uuid4().hex[:12]}`。

---

## 3. 并发派发

### 3.1 主循环

`StdioBridge.run()` 不再串行 await `_handle`。每条入站信封都包装成
独立任务：

```python
async def run(self):
    while True:
        msg = await self._inbox.get()
        if msg.type == "shutdown":
            await self._drain_inflight(timeout)
            await self._handle(msg)
            break
        task = asyncio.create_task(self._dispatch(msg))
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)
```

### 3.2 `_dispatch` 的两路语义

会话作用域消息（`request` / `user_input`）需要**同 sid FIFO**（保持
会话内顺序）；不同 sid 并发。`_dispatch` 用每-sid 锁实现：

```python
async def _dispatch(self, msg):
    sid = self._resolve_session_id(msg) if is_session_scoped(msg) else None
    if sid is None:
        await self._handle(msg)   # bridge-meta — no serialisation
        return
    lock = self._session_dispatch_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        if msg.type == "request":
            self._inflight_by_sid[sid] = asyncio.current_task()
        try:
            await self._handle(msg)
        finally:
            if msg.type == "request":
                self._inflight_by_sid.pop(sid, None)
```

**`close_session` 故意跳过锁** —— 它通过 `_cancel_inflight(sid)` **抢占**
正在运行的 request（cancel + bounded await），缓慢的 LLM 往返不能 wedge
标签页关闭。

### 3.3 In-flight 跟踪 + Shutdown drain

- `_inflight_by_sid[sid]`：当前正在跑的 request 任务，仅 request 类型才进入
- `_cancel_inflight(sid)`：cancel + bounded await（默认 2s）
- `_drain_inflight(timeout)`：shutdown 前有界等待全部派发任务，超时不无限阻塞

---

## 4. 会话生命周期

### 4.1 创建（惰性）

```
renderer:   "+" → uuid4() → _mountSession(tab + tiled card) → switchSession()
            user types in that card's composer
            → IPC request{ session_id: sid, goal }
bridge:     _handle("request"):
              sid = _resolve_session_id(msg)
              if sid is None: emit error; return
              if sid in self._closing: emit error; return
              _ensure_flow(sid, goal):
                _get_or_create_ui(sid)  → _uis[sid]
                _allocate_session_dir(goal)
                  → %USERPROFILE%\HandQ\History\<TS>-<slug>\
                engine.log handler → _engine_log_handlers[sid]
                AnthropicStreamingService × N → _services_by_session[sid]
                _on_reply closure binds sid for receptionist replies
                FlowControllerV2(
                    ..., session_id=sid,     # passed to BrowserSessionHolder
                ) → _flows[sid]
                  └─ flow.start() 构建 SessionContext + Orchestrator +
                     PersistentAgent，启动 agent_task + planner_task
                bind flow.interaction_manager.set_delegate(_uis[sid])
              await flow.on_user_message(goal) → reply
              emit final{ ok, reply }  with sid + gen
```

### 4.2 关闭（用户点击标签页 X）

```
renderer:   X click → closeSession(sid):
              IPC close_session{ session_id: sid }
              remove pane + tab DOM
              sessions.delete(sid)
              if sid was active: switch to neighbour
              if Map empty: createSession()    # never leave user session-less
bridge:     _handle("close_session"):
              sid = _resolve_session_id(msg)
              await _do_close_session(sid, msg_id)
            _do_close_session(sid, msg_id):
              self._closing.add(sid)
              await _cancel_inflight(sid)        # N3
              flow = self._flows.pop(sid)
              services = self._services_by_session.pop(sid)
              with self._uis_lock: self._uis.pop(sid)
              self._generations.pop(sid)
              if flow is None and not services:
                  logger.warning("unknown sid; idempotent cleanup")
              ctx_ref = flow._ctx          ← saved BEFORE destroy
              handler = self._engine_log_handlers.pop(sid)
              remove_root_file_handler(handler)
              await flow.destroy()  with 2.5s timeout
                ├─ checklist.interrupt_event.set()
                ├─ cancel agent_task + planner_task
                └─ await ctx.close()
                    ├─ browser_session.close()   ← closes THIS session's
                    │     Chromium (per-session profile dir)
                    ├─ session_registry.close_all()
                    ├─ ssh_pool.close()  (asyncio.to_thread)
                    ├─ desktop_state.close()
                    │     → reset_takeover_state()
                    │     → releases global desktop lock
                    └─ file_state dropped
              for svc in services: await svc.close()  with 2.0s timeout
              finally:
                # Defensive: release desktop global lock even if destroy
                # timed out. Personality auto-un-pauses via the query model
                # (it reads desktop_tool._GLOBAL_DESKTOP_OWNER directly).
                _force_release_session_locks(ctx_ref, sid)
                self._closing.discard(sid)
                self._session_dispatch_locks.pop(sid)
                self._inflight_by_sid.pop(sid)
              emit final{ close_session: ok }
```

#### 4.2.1 close_session 的"未知 sid 静默成功"语义

`_do_close_session` 对未知 sid 不报错，只记 WARNING。**四类正当触发**：

1. 用户连续双击 X 按钮（第一次成功，第二次到达时 sid 已 pop）
2. IPC 时序错乱（close 比 request 先到达，sid 还未注册）
3. Renderer 重启 / 刷新发送了旧 sid 的清理
4. 测试 / 脚本客户端 sid 拼错

**为何接受静默成功**：renderer 的 UI 状态是真相 —— 用户认为这个 session
已关闭，bridge 同步到这个真相即可。强制报错会让 renderer 难以正确处理
（要么忽略错误回到原状态，要么显示给用户但用户没做任何事），不如让
bridge 幂等。WARNING 日志保证生产环境出现时可见。

### 4.3 关停

```
bridge: run() 看到 "shutdown":
  await self._drain_inflight(timeout=...)
  _do_shutdown():
    self._shutdown_requested = True
    for sid in list(self._flows.keys()):
      pop flow + services
      await flow.destroy() with 2.5s timeout
      for svc: await asyncio.wait_for(svc.close(), 2.0s)
    emit final{ shutdown: ok }
```

`_drain_inflight` 是有界的（`asyncio.wait(..., timeout)`），卡住的 request
任务无法阻塞退出。`_force_release_session_locks` 在 shutdown 中**不**被
调用 —— 进程退出时锁随进程一同拆除。按会话关闭则把它作为防御。

### 4.4 调度器派发

```
scheduler timer fires → dispatch_scheduled_task(task):
  bridge.accept_scheduled_task(task):
    if self._shutdown_requested: return False  # 调度器顺延到下次
    sid = f"sched-{uuid4().hex[:12]}"          # fresh sid per fire
    _ensure_flow(sid, task.prompt)
    await flow.on_user_message(...)
    # renderer 的 onStatus 看到新 sid → 自动惰性挂载一个标签页
```

调度器不检查"是否有 session 空闲" —— 它就是新建一个 sid 派发，与
renderer-driven session 完全等价并发。

---

## 5. 跨会话资源协调

### 5.1 桌面 — 任务级 FIFO 排队

物理约束：一块屏幕、一套鼠键，**多个 session 同时驱动输入没有意义**。
所以 desktop 是 HandQ 中**唯一**仍有跨会话锁的资源。

**模块级状态**（`src/tools/desktop_tool.py:152-172`）：

```python
_desktop_lock = asyncio.Lock()                  # per-action serialiser
_GLOBAL_DESKTOP_OWNERSHIP_LOCK = asyncio.Lock() # cross-session ownership
_GLOBAL_DESKTOP_OWNER: Optional["DesktopState"] = None

def is_any_session_holding_desktop() -> bool:
    """直接查询 — PersonalityMonitor 的 pause 模型用此判定。"""
    return _GLOBAL_DESKTOP_OWNER is not None
```

**生命周期**：

- `DesktopState.acquire_global_takeover()` —— 幂等；驱动输入的动作
  （click / type / drag / scroll / hotkey / key_press / hover_at）前由
  `_input_action_guard` 调用
- `DesktopState._release_global_takeover_if_owned()` —— 仅当此 state 持
  有锁时释放；由以下两条路径调用：
  - `DesktopState.reset_takeover_state()`（**任务级释放点**）由
    `FlowControllerV2._forward_state_to_ui("idle")` 调用 —— orchestrator
    完成 task 时（`_emit_completion_reply` / planner empty-fallback）
  - `DesktopState.close()`（会话级释放点）由 `ctx.close()` 调用

**任务级排队的实际语义**：

```
session A 在执行 task1：acquire 成功，driving input
session B 用户请求需要桌面：B.acquire 在锁上 await
session C 用户请求需要桌面：C.acquire 在锁上 await
...
session A 的 task1 完成 → orchestrator emit "idle" →
  FlowControllerV2._forward_state_to_ui("idle") →
  ctx.desktop_state.reset_takeover_state() →
  _release_global_takeover_if_owned() →
  global lock 释放 → B 立即唤醒拿到锁

(session A 还活着 — 用户没关闭它)
session A 的 task2 又需要桌面：A.acquire 再次排队（FIFO 在 C 后）
session B 完成 → C 拿到 → C 完成 → A 拿到 task2
```

**Loop-safe 释放（F3）**：`close()` 通过 `asyncio.to_thread` 运行，释放
可能从**工作线程**触发。`asyncio.Lock.release()` 不是线程安全的。修复：
acquire 时捕获 `_owner_loop`；release 时同步清零所有权标志，从非 loop
线程调用时用 `call_soon_threadsafe` 把实际的 `.release()` 弹回 owner loop。

**只读动作**（screenshot / list_windows / find_element / snapshot）**不**
获取全局锁 —— 它们不驱动输入，可自由并发。

### 5.2 浏览器 — Per-session 真并发

**Chromium 的 user-data-dir 是 OS 级独占锁**，所以两个 session 不能共享
同一 dir。**解决方案：每个 session 用独立 dir**。

```
%USERPROFILE%\HandQ\browser_profile\
  sessions\
    <sid-a>\        ← session A 的独立 user-data-dir
    <sid-b>\        ← session B 的独立 user-data-dir
    ...
  screenshots\      ← 共享的 vision scratch 空间（ScreenshotStore 分级管理）
```

**实现要点**：

- `user_browser_profile_dir(sid)`（`src/infrastructure/browser_paths.py`）
  接受 sid 返回 `<base>\sessions\<safe_sid>\`；`sid=None` 回退到 legacy
  单一目录（仅 ctx-less 测试 fixture 使用）
- `BrowserSessionHolder(session_id=sid)` 接受 sid，在 launch 时用 sid 解析
  profile dir
- bridge 在 `_ensure_flow` 把 `session_id` 传给 `FlowControllerV2`，flow
  在 `start()` 构造 `BrowserSessionHolder(session_id=self._session_id)`
- **无跨会话锁** —— 不再有 `_GLOBAL_BROWSER_OWNERSHIP_LOCK`。两个 session
  并发 launch_browser → 两个独立 Chromium 进程

**取舍**：

- 优点：真并发，无"等待对方关闭"的 UX 死锁
- 代价：每 session 全新空 profile（cookies / 登录态不共享），磁盘占用 N 倍
- 清理：session 关闭时 ctx.close() 关闭自己的 Chromium，但磁盘 profile 留
  存（孤儿；未来可加清理任务）

### 5.3 PersonalityMonitor — 直接查询，零漂移

**模型**：

```python
class PersonalityMonitor:
    def __init__(self, *, desktop_query: Callable[[], bool], ...):
        self._user_paused: bool = False
        self._desktop_query: Callable[[], bool] = desktop_query

    @property
    def _paused(self) -> bool:
        if self._user_paused:
            return True
        try:
            return bool(self._desktop_query())  # 直接读 _GLOBAL_DESKTOP_OWNER
        except Exception:
            return False  # fail-open — better to over-capture than starve

    def pause_by_user(self) -> None:   self._user_paused = True
    def resume_by_user(self) -> None:  self._user_paused = False
```

**注入**：`bridge_main.py` 构造时传入
`desktop_query=desktop_tool.is_any_session_holding_desktop`。

**为何是查询不是 mirror / refcount**：

- 单一 authoritative source（`_GLOBAL_DESKTOP_OWNER`）—— 任何镜像都引入
  "两件事是否同步"的可能漂移
- destroy 超时后 `_force_release_session_locks` 清零 `_GLOBAL_DESKTOP_OWNER`
  → 下一次 `_paused` 检查自动 un-pause，**安全网免费**
- 没有计数器下溢可能，pause/resume 与桌面持有完全解耦

**两个独立维度**：

| `_user_paused` | `desktop_query()` | `_paused` |
|---|---|---|
| False | False | **False**（活动监控运行） |
| False | True | True（agent 正在用桌面） |
| True | False | True（用户手动暂停） |
| True | True | True |

手动按钮（IPC `personality_pause` / `personality_resume`）调
`pause_by_user()` / `resume_by_user()`；与 desktop 占用完全独立。

### 5.4 Per-session 资源（无跨会话竞争）

| 资源 | 类型 | 隔离方式 |
|------|------|----------|
| `FileState` | per-session | 文件读 / 写时间戳跟踪 |
| `SshConnectionPool` | per-session | host_key → SSHClient 字典，per-flow |
| `BrowserSessionHolder` | per-session | 见 §5.2 |
| `SessionRegistry`（终端） | per-session | flow 内部的终端 ID 命名空间 |
| `DesktopState` | per-session | takeover 状态 + snapshot_cache + ScreenshotStore |
| `ProviderCache` | per-session | ContextProvider 的 cred / "已准备"缓存 |
| `ExecutionRecorder` | per-session | 写 `session_<TS>_persiste.log` |
| `SharedCheckList` | per-session | `interrupt_event` per flow |
| `Orchestrator.conversation_history` | per-session | 与外界无共享 |

### 5.5 进程级共享单例（设计上一份）

| 单例 | 并发安全机制 |
|------|-------------|
| `LongTermMemory` | SQLite WAL；写用 `asyncio.Lock`；recall cache 用 `_cache_lock` 守护 check→fetch→write，`archive()` 在同锁下使缓存失效 |
| `PersonalityMonitor` | 单一 asyncio task，pause 用查询模型（§5.3）；OCR drain 串行 |
| `Scheduler` | 单一 asyncio task；`accept_scheduled_task` 在 `_shutdown_requested=True` 时返回 False |
| `SkillRegistry` | boot 时一次性构建，后续只读访问（dict 浅读取安全） |
| `Vision client` | `threading.Lock` 守护构建路径（双检查），单例后只读 |
| `Outlook COM (email_tool)` | 单线程 STA `ThreadPoolExecutor` + `asyncio.Lock` 串行所有 COM 调用 |

### 5.6 LLM fallback / Network notify（broadcast）

`llm_pool` 的 fallback 通知器与网络断开通知器是模块级槽位。bridge
启动时注册**一个广播闭包** —— 不携带 sid。理由：所有 session 共用同一
套 LLM server，服务端问题对每个 session 影响完全相同。Renderer 收到这类
未标 sid 的信封时作为系统消息渲染到**当前活动卡片**。

---

## 6. Renderer 模型

### 6.1 平铺 DOM

```
#conversation { display: flex; flex-direction: row; }
  .session-card[data-sid=A] { ... }   ← 永远可见
  .session-card[data-sid=B] { ... }   ← 永远可见
```

- 没有 `display:none` 切换
- `.active` class 只是焦点高亮（边框发光），不控制显隐
- `switchSession(sid)` 滚到目标卡片并清未读点

### 6.2 每卡片结构（`_mountSession(sid)`）

每张卡片包含：

- header：`name · status pill · close ✕`
- `.session-card-body`：可滚的气泡区 + 内嵌折叠 `<details>` 活动分组
- `.session-card-confirm`：内联确认 UI 宿主（**绝非全局模态**）
- `.session-card-composer`：每卡片自有的 textarea + send 按钮

### 6.3 按会话状态分桶（`sessions: Map<sid, SessionState>`）

每个 SessionState 至少包含：
`gen`、`pane`、`composerInput`、`confirmEl`、`activityItems`、
`checklistItems`、`checklistExpanded`、`pendingConfirm`、`pillEl`、
`tabEl`、`sessionState`。

### 6.4 Generation 水位线

```js
function gateGen(evt) {
    if (!evt) return true;
    if (typeof evt.gen !== 'number') return false;  // bridge-meta 放行
    const sid = _resolveSid(evt);
    const s = sessions.get(sid);
    if (!s) return true;        // unknown sid → drop
    if (evt.gen < s.gen) return true;
    if (evt.gen > s.gen) s.gen = evt.gen;
    return false;
}
```

每个 session 独立 gen 计数器；旧 flow 销毁后到达的延迟事件被丢弃。

### 6.5 派发路由

`handq.onStatus / onFinal / onError` 顶部设置 `_dispatchSid =
_resolveSid(evt)`，finally 重置为 `null`。气泡 helper（`addUserBubble`、
`addAssistantTextBubble`、`appendReceptionistDelta`、`showThinkingBubble`、
`addSystemBubble`、`addErrorBubble`）以 `_dispatchPane()` 为目标 ——
即当前信封 sid 的卡片 body；派发上下文外则回退到当前聚焦卡片。

### 6.6 按会话内联确认

- 没有全局模态框（`#overlay-confirmation` 已移除）
- `showConfirmationModal(evt)` 通过 `_resolveSid(evt)` 解析归属 sid，
  挂载到该卡片的 `.session-card-confirm` 宿主，并仅在该会话上记录
  `pendingConfirm = { id, kind }`
- `sendConfirmationAnswer(sid, answer)` 标记**归属** sid +
  `pendingConfirm.id` —— **绝不**用 `currentSid()`。这是"一个 session
  的弹窗绝不影响另一个"的核心
- bridge 侧：每个 `_StdioUI` 自己的 `_pending: Dict[prompt_id, Future]`。
  reader 线程的确认快路径先用 sid_hint 命中，失败回退全表扫描

### 6.7 关闭最后一个 session 的边界

```js
function closeSession(sid) {
    handq.sendRequest({ type: 'close_session', session_id: sid });
    // ... remove DOM ...
    sessions.delete(sid);
    if (activeSid === sid) {
        const next = sessions.keys().next();
        if (next.done) {
            createSession();    // 用户永不处于"无 session"
        } else {
            switchSession(next.value);
        }
    }
}
```

### 6.8 按窗格 composer

`_mountSession(sid)` 中接线 `form.addEventListener('submit', ...)` 时，
**sid 在闭包中被硬编码捕获**。`submitText(sid, text, ta)` 发送 IPC 时
直接使用该 sid，绝不读 `currentSid()` —— 即使用户在卡片 A 输入到一半切
到卡片 B，再切回 A 按发送，IPC 仍带卡片 A 的 sid。

旧版全局 `#composer` 仍存在于 DOM 但 `display:none`，事件接线保留作为
防御性 stub（避免历史路径抛错）。

---

## 7. 不变式速查（回归测试固化）

| 不变式 | 测试 |
|--------|------|
| `_resolve_session_id` 返回 `Optional[str]`，无 DEFAULT 回退 | `test_bridge_session_isolation.py` |
| 同 sid 入站走 FIFO、不同 sid 并发 | `test_bridge_concurrent_dispatch.py::test_different_sessions_dispatch_concurrently` + `test_multisession_4plus.py::test_same_sid_serialises_under_per_sid_lock_across_many_sessions` |
| 8 session 真并发 request | `test_multisession_4plus.py::test_eight_sessions_concurrent_request_all_run_in_parallel` |
| close_session 抢占正在跑的 request | `test_bridge_concurrent_dispatch.py::test_close_session_cancels_inflight` |
| close_session 未知 sid 幂等成功 | `test_multisession_e2e.py::test_close_unknown_sid_silent_ok` |
| `_uis` 线程安全（reader 快照在锁下） | `test_bridge_session_isolation.py` |
| 桌面任务级排队（idle 释放） | `test_desktop_browser_global_locks.py::test_desktop_task_level_queueing_via_reset_takeover_state` |
| 4 session FIFO desktop 排队 | `test_multisession_4plus.py::test_four_sessions_queue_for_desktop_and_unblock_in_order` |
| 同 session reacquire 桌面正确排队 | `test_multisession_4plus.py::test_session_can_reacquire_desktop_after_its_own_idle_release` |
| 浏览器 per-session 独立 profile | `test_multisession_4plus.py::test_four_browser_holders_each_get_their_own_profile_dir` |
| Browser 全局锁 / acquire_global_ownership / `_GLOBAL_BROWSER_*` **不存在** | `test_multisession_4plus.py::test_invariant_no_global_browser_lock` |
| Personality 用直接查询 `_GLOBAL_DESKTOP_OWNER` | `test_personality_refcount_force_release.py::test_production_wiring_query_returns_global_owner_state` |
| Personality `_pause_owners` 字段**不存在** | `test_multisession_4plus.py::test_invariant_personality_has_no_pause_owner_refcount` |
| destroy 超时后 `_force_release_session_locks` 释放桌面 | `test_personality_refcount_force_release.py::test_force_release_clears_global_desktop_owner_unblocking_personality` |
| `_force_release_session_locks` 不释放浏览器（不再需要） | （隐式：函数体不调浏览器） |
| Vision client 构建路径有锁守护 | `test_vision_client_singleton_concurrency.py` |
| LTM cache check→fetch→write 在 `_cache_lock` 下 | `test_ltm_cache_concurrency.py` |
| 调度器在 shutdown 时拒绝新触发 | `test_scheduler_schedule.py` |
| Renderer：关闭最后一个 session 自动创建新 session | UX 手动验证 + 待 Playwright |

---

## 8. 手动验证（冒烟测试）

### 8.1 单 session 回归

```
启动 app → bridge boot → "Session 1" 卡片可见
在 Session 1 的 composer 输入 "list .py files in current dir"
  → request 出口标 session_id
agent 响应 → 气泡出现在 Session 1 卡片 body
"+" → 第二张 "Session 2" 卡片在右侧（焦点移到它）
两张卡片始终同时可见 —— Session 1 的气泡绝不被隐藏
X 关闭 Session 2 → close_session IPC；卡片 + 标签消失
```

### 8.2 并发任务

```
Session 1: "write a python fibonacci(20)"
Session 2: "write a python factorial(10)"
两个并行运行，气泡各自累积；未聚焦卡片标签显红色未读点
```

### 8.3 桌面任务级排队

```
Session 1 agent 执行 click_at + type_text
Session 2 agent 执行 click_at → 阻塞在 acquire_global_takeover
Session 1 task 完成（reply 发出） → 立即释放桌面
Session 2 立即获取，开始执行
Session 1 没关闭，仍可在它的 composer 发新消息
  → 新 task 需要桌面时排队等 Session 2 完成
```

### 8.4 浏览器真并发

```
Session 1: launch_browser → 第一个 Chromium 进程启动
Session 2: launch_browser → 第二个 Chromium 进程同时启动（无等待）
两个 Chromium 各自独立 user-data-dir，cookies 不共享
```

### 8.5 Personality 自动暂停 / 恢复

```
Session 1 进入 desktop takeover → personality_status: paused=true
Session 2 也开始 takeover（排队等 Session 1）
Session 1 task 完成释放 → Session 2 立即拿到 → 仍 paused
Session 2 task 完成释放 → personality_status: paused=false（自动）
点击 IPC 暂停按钮 → user_paused=true 即使无 takeover 也 paused
```

### 8.6 按会话内联确认隔离

```
Session 1: 启动需要确认的工具
  → 内联确认 UI 出现在 Session 1 卡片，Session 2 不受影响
Session 2: 同时启动另一个需要确认的工具
  → Session 2 卡片也出现自己的确认 UI
两个确认互不干扰；分别点击 Approve 各自推进
```

### 8.7 调度器自动新建标签

```
设定 cron 任务："1 分钟后说 hi"
1 分钟到 → bridge 新建 sched-{uuid} sid，挂载到 renderer
新标签自动出现并接收回复
```

---

## 9. 关键文件索引

`src/bridge/stdio_bridge.py`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| `_emit` | ~400 | session_id + gen 参数 |
| `_StdioUI` | ~445–600 | 捕获 (gen, sid)；每个 emit 标记 |
| `StdioBridge.__init__` | ~755–820 | 所有 per-sid dict 初始化 |
| `_resolve_session_id` | ~891 | Optional[str] |
| `_get_or_create_ui` | ~908 | 幂等；`_uis_lock` 下改 `_uis` |
| `_deliver_confirmation` | ~923 | 先 sid_hint 后扫描；线程安全（F2） |
| `_stdin_reader` | ~952 | 确认快路径 |
| `run()` | ~2380 | 每消息 `create_task(_dispatch)`；shutdown drain |
| `_dispatch` | ~2275 | 每-sid 锁；bridge-meta 绕过 |
| `_cancel_inflight` / `_drain_inflight` | ~2319 | 抢占 + 有界 drain |
| `_handle` | ~1330–1500 | request / user_input / close_session 分支 |
| `accept_scheduled_task` | ~1660 | `_shutdown_requested` 拒绝 + 生成 sched-{12hex} sid |
| `_ensure_flow` | ~1620–1965 | 以 sid 参数化；构造 + 绑定 UI/服务 |
| `_force_release_session_locks` | ~2045 | 仅释放桌面全局锁（浏览器 per-session 无需，personality 走查询） |
| `_do_close_session` | ~2090–2180 | 拆除 + finally 兜底；invalid sid 警告日志 |
| `_do_shutdown` | ~2270+ | 遍历所有 flow |

`src/controller_v2/flow_controller.py`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| `FlowControllerV2.__init__` | ~52–75 | 接受 `session_id` 参数 |
| `start()` | ~124–215 | 构造 SessionContext + `BrowserSessionHolder(session_id=...)` |
| `_forward_state_to_ui` | ~415–435 | `state="idle"` 调 `desktop_state.reset_takeover_state()` —— 任务级排队释放点 |
| `destroy()` | ~256–278 | interrupt + cancel + ctx.close + 有界超时 |

`src/tools/desktop_tool.py`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| `_GLOBAL_DESKTOP_OWNERSHIP_LOCK` | ~155 | 全局所有权锁 |
| `is_any_session_holding_desktop` | ~158 | personality 查询入口 |
| `_snapshot_cache` | ~145 | per-session（DesktopState.snapshot_cache）；模块级孪生已移除 |
| `DesktopState.acquire_global_takeover` | ~580 | 幂等 acquire |
| `DesktopState._release_global_takeover_if_owned` | ~600 | loop-safe（F3） |
| `DesktopState.reset_takeover_state` | ~560 | 任务级释放点（idle 调） |

`src/tools/browser_tool.py`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| 模块级状态 | ~280 | **无全局锁**（per-session 模型） |
| `BrowserSessionHolder.__init__` | ~305 | 接受 `session_id` |
| `_action_launch_browser` | ~1190 | 用 `self.holder.session_id` 解析独立 profile dir |
| `holder.close()` | ~435 | 关闭自己 Chromium；无跨会话锁释放 |

`src/infrastructure/browser_paths.py`：

| 区域 | 内容 |
|------|------|
| `user_browser_profile_dir(sid)` | sid=None 回退 legacy；非 None 返回 `<base>\sessions\<safe_sid>\` |
| `_safe_sid(sid)` | 路径安全化（仅留 `[A-Za-z0-9_.-]`） |

`src/infrastructure/personality/service.py`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| `__init__(desktop_query=...)` | ~165 | 注入查询回调 |
| `_user_paused` / `_desktop_query` | ~191 | 两个独立维度 |
| `_paused` property | ~398 | `_user_paused OR desktop_query()` |
| `pause_by_user` / `resume_by_user` | ~407 | 手动按钮 IPC |
| `snapshot_status` | ~445 | 输出 user_paused + desktop_active 拆分字段 |

`bridge_main.py`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| `_get_desktop_query()` | ~720 | lazy import 返回 `is_any_session_holding_desktop` |
| `PersonalityMonitor(...)` | ~790 | 注入 `desktop_query=_get_desktop_query()` |

`electron/renderer/renderer.js`：

| 区域 | 行号（约） | 内容 |
|------|----------|------|
| `sessions: Map` + `_newSessionState` | ~413–469 | per-session bucket |
| `_mountSession(sid)` | ~503–613 | 平铺卡片构造 |
| `closeSession(sid)` | ~657–683 | 关闭 + 空 Map 时 createSession |
| `gateGen(evt)` | ~734 | per-session 水位线 |
| `_dispatchSid` / `_dispatchPane` | ~712–732 | 路由 helper |
| 气泡 helper | ~1365–1503 | `_sealActivityGroup` 在 appendChild 前 |
| `showConfirmationModal` | ~1632 | 按归属 sid 挂载内联 UI |
| `sendConfirmationAnswer(sid, answer)` | ~1714 | 用归属 sid 而非 `currentSid()` |
| `submitText(sid, ...)` | ~2516–2560 | composer 提交标记卡片自身 sid |

---

## 10. 测试与验证

### 10.1 自动化测试

| 测试文件 | 覆盖范围 |
|----------|----------|
| `tests/v2/test_bridge_concurrent_dispatch.py` | F1 并发派发 + N3 in-flight 抢占 + drain |
| `tests/v2/test_bridge_session_isolation.py` | F2 `_uis` 线程安全 + 按 sid 确认路由 |
| `tests/v2/test_desktop_browser_global_locks.py` | 桌面全局锁 + loop-safe 释放 + 任务级排队 + 浏览器 per-session 隔离 |
| `tests/v2/test_personality_refcount_force_release.py` | Personality 查询模型 + force-release 安全网 |
| `tests/v2/test_ltm_cache_concurrency.py` | LTM cache 跨 await 并发保护 |
| `tests/v2/test_vision_client_singleton_concurrency.py` | Vision client 单例不变式 |
| `tests/v2/test_multisession_4plus.py` | 4-8 session 单元级并发 + invariant 回归 |
| `tests/v2/test_multisession_e2e.py` | 真 bridge 子进程 + IPC 端到端 |
| `tests/v2/test_multisession_lifecycle_stress.py` | 反复 open/close/recreate UX 压力 |

### 10.2 运行

```powershell
# 离线全套（含多 session 单元 + E2E + 压力）
python -m pytest tests/v2/ -q --ignore=tests/v2/test_e2e_live.py `
  --ignore=tests/v2/test_llm_live.py `
  --ignore=tests/v2/test_ltm_e2e_live.py `
  --ignore=tests/v2/test_controller_live.py

# 仅多 session 相关
python -m pytest tests/v2/test_multisession_*.py `
  tests/v2/test_bridge_concurrent_dispatch.py `
  tests/v2/test_bridge_session_isolation.py `
  tests/v2/test_desktop_browser_global_locks.py `
  tests/v2/test_personality_refcount_force_release.py -q
```

---

## 11. 已知限制 / 接受的取舍

| # | 限制 | 为何接受 |
|---|------|----------|
| 1 | 桌面是任务级排队，不是真并发 | 物理约束：一块屏幕、一套鼠键。任务级已是最细粒度 |
| 2 | 浏览器 per-session 独立 Chromium 实例，磁盘 / 内存占用 N 倍 | 真并发胜过资源节省（用户明确选择） |
| 3 | 每 session 新建的浏览器是空 profile（无 cookies / 登录） | per-session 独立 user-data-dir 的代价 |
| 4 | 没有全局/汇总活动指示器 | 每张卡片自己显示 idle / working / 工具结果，无需跨会话汇总 |
| 5 | `lastCalledTool` / `lastThinking` 等 renderer 全局变量在并发思考时可能映射到错误 sid 的 pill **文本** | pill 元素正确按 sid 路由；仅外观瑕疵（待 v3 派发器重构） |
| 6 | LLM fallback / 网络通知是 broadcast（不带 sid），渲染到当前活动标签 | 所有 session 共用同套 LLM server，服务端故障对所有 session 影响等同 |
| 7 | 终端面板 `_sessionTerminals` Map 跨 flow 共享 | 终端 session_id 属 InteractiveSessionTool 独立命名空间，与 flow sid 解耦 |
| 8 | 设置 / LTM / 调度浮层都是全局的，影响所有 session | 它们就是进程级配置 / 数据 |

---

## 12. 未来工作

1. **浏览器 profile 迁移** —— 用户可选"从 master profile 复制 cookies"
   的设置，让新 session 继承登录态
2. **孤儿 session profile 清理** —— 后台任务清理 `browser_profile\sessions\`
   下不再有对应活动 session 的目录
3. **桌面 takeover overlay 节流** —— v2 任务级排队下 overlay 频繁开关，
   可能需要 250ms 节流
4. **Renderer 全局状态分桶** —— `lastCalledTool` / `lastThinking` /
   `activeExecCount` 移入会话桶（消除限制 5）
5. **Playwright E2E** —— 添加 renderer 侧的 UX 自动化（关闭最后一个
   session 的边界、内联确认隔离等）
