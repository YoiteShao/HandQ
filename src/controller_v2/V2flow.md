# V2 Flow — `src/controller_v2/` 完全审阅文档

> 目的：读完此文档等于审阅完 `src/controller_v2/` 实际代码。每段说明都对应函数名/行为，可逆向跳查。
>
> 编写约束：所有内容如实反映代码逻辑，不主观臆断。当文件内 docstring/注释与代码冲突时以代码为准。
>
> 范围：`src/controller_v2/__init__.py`、`flow_controller.py`、`orchestrator.py`、`persistent_agent.py`、`shared_checklist.py`、`planner_mixin.py`、`receptionist.py`、`mention_preprocessing.py`、`risk_check.py`、`agent_utils.py`、`agent_prompts.py`、`planner_prompts.py`。

---

## 1. 架构总览

V2 是一个 **Persistent Agent + 共享 CheckList** 架构。整个 session 内：
- **CheckList** 是一根总线（bus），承载待办 Item、已完成 Result、激活的 Skill / Tool 名录、interrupt 信号。
- **PersistentAgent** 是一个永生循环（不会随 Item 切换重启），它从 CheckList 拉 Item 执行，把结果写回。
- **Orchestrator** 既是用户消息入口（Channel 1），又是后台 Planner 循环（Channel 2），两者共用 `_run_planner`，互斥锁串行。
- **FlowControllerV2** 是 session 级薄壳，启动/拆除三个组件、注册 ContextProvider、把 provider 工具表喂给 Orchestrator 的 planner prompt。

```
                           ┌─────────────────────┐
   user message ──▶ on_user_message              │
                           │   FlowControllerV2  │
                           │  (session-scoped)   │
                           └──┬──────────────────┘
                              │
              ┌───────────────┴────────────────┐
              ▼                                 ▼
    ┌──────────────────┐              ┌─────────────────┐
    │   Orchestrator   │              │ PersistentAgent │
    │  (Channel 1+2)   │              │   run_loop()    │
    └─────┬────────────┘              └────────┬────────┘
          │ replace_post_current               │ wait_for_current_item
          │ activate_skills/tools              │ mark_current_done
          │ interrupt_agent                    │
          ▼                                    ▼
                ┌───────────────────────────────────┐
                │       SharedCheckList (bus)       │
                │  items / results / interrupts     │
                │  active_skills / active_tools     │
                └───────────────────────────────────┘
```

**生命周期一览**（启动 → 任务流转 → 任务完成 → 会话结束）：
1. `FlowControllerV2.start()` 创建 CheckList、Orchestrator、PersistentAgent，启动 `agent.run_loop()` 与 `orchestrator.run_planner_loop()` 两个常驻 task。
2. 用户消息进入 → `on_user_message` → Stage 1 INTENT → 若 `task` 则 Stage 2 PLAN_MODIFY → 写 CheckList。
3. Agent 阻塞在 `wait_for_current_item` 上，CheckList 一旦有 item 立即拉走执行。每完成一个 item 触发 `_planner_trigger` → 后台 planner_loop 重新评估 → 写新一轮 post_current_items。
4. 当 planner 输出 `post_current_items=[]` 且无 in-progress + completed_count>0 → 进入 B1 验证门 → `synthesize_acceptance` 5-verdict 决断 → 完成或注入 acceptance_* item 继续。
5. `destroy()` 取消两个 task，session 结束。

---

## 2. 关键数据结构（schema）

### 2.1 `CheckListItem`（`shared_checklist.py`）

planner 写入、agent 读取的工作单元。

| 字段 | 类型 | 含义 | 可见性 |
|------|------|------|--------|
| `item_id` | str | 短 kebab-case 标识（验证轮以 `acceptance_` 开头） | planner + agent + verifier |
| `instruction` | str | 给 agent 的指令；planner 端要求 >20 字符 | planner + agent |
| `expected_outcomes` | List[str] | 1–4 条可观察成功标准；planner 用来 drift 检测 | planner + **agent**（在 [Expected Outcomes] 段） |
| `supplement` | str | 额外数据/上下文 | planner + **agent**（在 [Input] 段） |
| `planner_reasoning` | str | 该 item 为什么存在 | planner only（仅在 ExecutionRecorder 落盘） |
| `risk_assessment` | str | 风险与回退 | **planner only**（不进 agent prompt） |
| `ssh_target` | str | `user@host`（远程时） | planner + agent（通过 provider hint） |

构造：`CheckListItem.from_planner_dict(data)` 将 planner JSON 转结构体（容错处理 `step_id` 别名）。

agent 视角：`item.to_agent_message()` 输出固定三段
```
[New Task]
<instruction>

[Input]                  ← supplement 非空时
<supplement>

[Expected Outcomes]      ← expected_outcomes 非空时
  - <outcome 1>
  - <outcome 2>
```

### 2.2 `ItemResult`（`shared_checklist.py`）

agent 完成 item 后写入。

| 字段 | 类型 | 含义 | 写入者 | 读取者 |
|------|------|------|--------|--------|
| `item_id` | str | 同 item | agent | planner / verifier |
| `success` | bool | 是否成功 | agent | planner / verifier / boundary 渲染 |
| `factual_outcome` | List[str] | 可验证事实陈述 | agent | planner（drift 评估）/ verifier / `_compose_completion_reply` 组合最终回复 |
| `key_findings` | List[str] | 离散事实（API 名、路径、版本等） | agent | planner（epistemic concretization）/ verifier |
| `artifacts` | List[str] | 写过/改过的文件路径 | agent | planner / verifier |
| `issues` | List[str] | 失败原因；interrupt 形如 `"Interrupted by planner: <reason>"` | agent | planner（失败末项顾问识别 interrupt）/ verifier |
| `iterations` | int | 该 item 用了多少 iter | agent | 日志 |
| `token_usage` | TokenUsage | LLM token 累计 | agent | session 计数 |
| `completed_at` | datetime | `mark_current_done` 时打时间戳 | CheckList | 仅记录 |

### 2.3 `AcceptanceVerdict`（`planner_mixin.py`）

B1 验证门的输出。

| 字段 | 含义 |
|------|------|
| `verdict` | `PASS` / `TRIVIAL` / `EXTEND` / `VALIDATE` / `ACCEPT`，未知值在 `from_dict` 中 snap 到 `ACCEPT` |
| `gap_summary` | 一句话 gap 描述（仅 `ACCEPT` 必填；`PASS`/`TRIVIAL` 强制为空） |
| `items_to_inject` | EXTEND/VALIDATE 时的注入项；其他 verdict 强制为空 |
| `fallback` | LLM 调用异常时返回 fail-open `ACCEPT` 时为 True |

`from_dict` 的防御：未知 verdict snap 到 ACCEPT；`PASS/TRIVIAL/ACCEPT` 强清 items_to_inject；`PASS/TRIVIAL` 强清 gap_summary；空 instruction 的 item 被丢弃。

### 2.4 `ConversationTurn`（`agent_utils.py`）

agent 单轮对话载体。

| 字段 | 含义 |
|------|------|
| `assistant_message` | `{"role": "assistant", "content": ..., "tool_calls": [...]?}` |
| `observations` | 该轮 tool_calls 对应的 `ToolResult` 列表（无 tool_calls 的非常规事件场景下也可承载 obs） |

`has_tool_calls` 属性：`"tool_calls" in self.assistant_message`。
`total_obs_chars()`：所有 obs 用 `to_obs_json` 序列化后的字符总数（参与预算/压缩计算）。

### 2.5 `TurnOutcome`（`agent_utils.py`）

agent 单 think 结果的判别式：
- `tool_calls` 非空 → 执行后继续循环
- `is_completion`（无 tool_calls 且无 error）→ instruction 完成
- `is_error`（error 字段非空）→ 不可达

`from_completion_text(raw)` 解析 LLM 完成态文本；含 `reasoning` 字段的 dict → 结构化结果；否则按 plain text 兜底为 `factual_outcome`。

### 2.6 `IterationAdvisor` 内部状态（`agent_utils.py`）

| 字段 | 含义 | 重置时机 |
|------|------|----------|
| `_success_history` | 每个 ToolResult 的 success（最近 10 条用于 success_rate） | `reset_for_item()` |
| `_last_error_hint` | `"write_param_error"` 时触发专门 reminder | `reset_for_item()` / 成功时清 |
| `_failed_approaches` | `Dict[signature, count]`，`failed_approach_signature` 归一化生成 | `reset_for_item()` |
| `_iteration_tool_counts` | 最近 16 turn 的 tool_calls 数量（用于并发提醒） | `reset_for_item()` |
| `_parallelism_cooldown` | parallelism 提醒后冷却 5 turn | 同上；每次 `record_turn_tool_count` 递减 |
| `_ltm_refresh_cooldown` | stagnation LTM 刷新后冷却 5 turn | 同上；每次 `record_turn_tool_count` 递减 |

阈值常量（模块级私有）：
- `_MODERATE_STAGNATION = 3`
- `_SEVERE_STAGNATION = 5`
- `_LTM_REFRESH_THRESHOLD = 3`
- `_LTM_REFRESH_COOLDOWN = 5`

---

## 3. SharedCheckList（**重点**：每个字段对 Orchestrator/Agent 的可见性）

### 3.1 实例字段（`SharedCheckList.__init__`）

| 字段 | 类型 | 用途 | Orchestrator 可见性 | Agent 可见性 |
|------|------|------|---------------------|--------------|
| `_items` | `List[CheckListItem]` | 完整 item 序列（已完成 + 当前 + 待办） | 读+写（`replace_post_current`）；planner prompt 全见 | 读（`wait_for_current_item` 拉当前） |
| `_results` | `List[ItemResult]` | 已完成结果 | 读（`get_completed_results` / `get_checklist_context_for_planner`） | 读（仅通过 `get_recent_results_for_agent` 渲染成 `[Item Boundary History]`，不直接读） |
| `_current_index` | int | 指向当前 in-progress item 的下标 | 读 | 读+写（`mark_current_done` 自增） |
| `_lock` | asyncio.Lock | 互斥 `replace_post_current` 临界区 | 用 | 不用（agent 路径只读 + lock-free） |
| `_item_available` | asyncio.Event | agent 阻塞唤醒 | set（写 item 时） | wait |
| `_interrupt_event` / `_interrupt_acked` / `_interrupt_reason` | Event + str | interrupt 信号 + ack 节流 + 原因载体 | set（`interrupt_agent`） | check / acknowledge |
| `_active_skills` | Set[str] | session 级 skill 名（**append-only**） | 读+写（`activate_skills`） | 读（属性 `active_skills` snapshot） + 通过 `on_skills_changed` 回调感知 delta |
| `_active_tools` | Set[str] | session 级 tool 名（**append-only**） | 同上 | 同上（`on_tools_changed` 回调） |
| `_on_item_done_callbacks` | List[Callable] | item 完成回调 | Orchestrator 注册 `_on_item_done_sync` | — |
| `_on_skills_changed_callbacks` | List[Callable] | skill 激活回调 | — | PersistentAgent 注册 `_handle_skills_added` |
| `_on_tools_changed_callbacks` | List[Callable] | tool 激活回调 | — | PersistentAgent 注册 `_handle_tools_added` |

### 3.2 接口分层（关键）

**Agent 接口**（`shared_checklist.py` 中标注 "Agent interface" 段）：
- `wait_for_current_item()` — 阻塞拉当前 item，永不返回 None。
- `mark_current_done(result)` — 同步、无锁（单线程 asyncio 模型下原子）；写 result + index++ + 唤醒 `_item_available` + 触发 `on_item_done`。
- `check_interrupt()` / `acknowledge_interrupt()` — interrupt 检查与 ack（ack 后允许 planner 发下一个 interrupt）。
- `get_current_item()` — 非阻塞读。

**Planner 接口**（`shared_checklist.py` 中标注 "Planner interface" 段）：
- `replace_post_current(items)` — **唯一**的 checklist 变更操作。语义：替换 `_items[_current_index+1:]`。
  - 空 checklist 时：items 直接作为初始列表。
  - 非空时：保留 `_items[:current_index+1]`（已完成 + 当前 in-progress），后段被替换。
  - 空 items 输入合法（清空 pending）。
  - **当前 in-progress item 永远不会被改**——要中断必须额外发 `interrupt_agent`。
  - 无返回值（`None`）。
- `interrupt_agent(reason)` — 等 ack → 设置原因 + 触发 event。

**共享读**：`get_completed_results / get_pending_items / get_checklist_context_for_planner / get_recent_results_for_agent`。

### 3.3 Skill / Tool 激活语义（**注意 append-only**）

```
activate_skills(["a", "b"]) 第一次  → {a, b}，回调收到 ["a", "b"]
activate_skills(["a", "c"]) 第二次  → {a, b, c}，回调收到 ["c"]（仅 delta）
                            没有 deactivate 路径
```

理由：skill body / tool 实例已经被注入到 agent 上下文/工具表，**写下去就不可逆**（agent 已经看见了），所以模型不允许"撤销"。Planner 重复声明已激活的名字是安全 no-op。

### 3.4 渲染：planner 视角 vs agent 视角

`get_checklist_context_for_planner()`（`shared_checklist.py`）—— **完整三段**：

```
## CheckList Status (3/5 done)

[Done] item=foo
  Outcome: ...
  Artifacts: ...
  Findings: ...
[Failed] item=bar
  Outcome: ...
  Issues: ...

[In Progress] item=baz
  Instruction: ...

[Pending] (2 items)
  - quux: <instruction[:80]>
  - corge: ...
```

`get_recent_results_for_agent(limit=10)`（`shared_checklist.py`）—— **agent 看到的简化版**：

```
[Item Boundary History]
✓ [foo] <factual_outcome[:120]>
⊗ [bar] <issues[0]>          ← 被 planner interrupt 时
✗ [baz] <issues[0]>
```

**差异关键**：agent 看不到 pending 列表（避免它越界处理后续 item），看不到 in-progress（它就是当前 item，避免自指）。tag 区分中断（⊗）与失败（✗）。

---

## 4. 启动流程（`FlowControllerV2.start`）

`FlowControllerV2.start`。一次性 bootstrap：

1. **创建 ExecutionRecorder**：`plan_id="persistent_session"`，落盘到 `storage_directory`。
2. **创建 SharedCheckList**：空状态。
3. **创建 Orchestrator**，把 checklist 注入。Orchestrator `__init__` 会：
   - 调 `self._init_planner()` → 初始化 `_on_demand_tools_table / _on_demand_routing_rules / _on_demand_antipatterns` 三个空字符串。
   - 注册 `self._on_item_done_sync` 到 `checklist.on_item_done`。
   - 创建 `_planner_lock`、`_planner_trigger`。
4. **`_push_provider_table_to_orchestrator()`**（`flow_controller.py`）：把 ContextProvider 的 `planner_description / planner_routing_rule / planner_antipatterns` 渲染成三段字符串，赋给 orchestrator 的 `_on_demand_*` 字段。**这一步必须在 PersistentAgent 启动前**——不然第一次 plan 看不到动态工具。
5. **创建 PersistentAgent**，注入 `pre_item_hint_provider=self._gather_pre_item_hints`。Agent `__init__`（`PersistentAgent.__init__`）会：
   - 计算 `_obs_budget_chars`（首个 service 的 context_window）。
   - 用 `ToolRegistry.create_all_tool_instances()` 加载 base 工具，`generate_tools_for_api()` 生成 schema。
   - 把 `checklist._interrupt_event` 注入到 `shell` / `session` 工具（让 shell 可被 ctrl+c 风格中断）。
   - `FileState.reset_for_session()` 清掉 read-tracking。
   - 注册 `_handle_skills_added` 与 `_handle_tools_added` 回调到 checklist。
   - 拼接 `_system_prompt`：`AGENT_SYSTEM_PROMPT + 工作目录 + 存储目录 + 平台标识`。
6. **启动两个常驻 task**：`agent.run_loop()` 和 `orchestrator.run_planner_loop()`。
7. `_started = True`。

至此系统在 idle 状态等待用户消息。

### ContextProvider 注册

`_collect_default_providers`（`flow_controller.py`）按平台白名单拉起 provider：
- 跨平台：`SSHContextProvider`、`CodingContextProvider`
- Windows-only：`Browser / RemoteHandQ / WebSearch / Email / Teams / Desktop / AskHuman / Session`

每个 provider 必须实现 `tool_name`、`needs_per_item_context`、`planner_description / planner_routing_rule / planner_antipatterns`、`prepare(step, im, memory)`（per-item hint）。

`register_item_context_provider(provider)` 允许外部注册；start 之后追加只能影响 per-item hint，**planner prompt 不再更新**（其 docstring 中明示）。

---

## 5. 用户消息：Stage 1 INTENT

入口：`Orchestrator.on_user_message`。

### 5.1 ingress 预处理：`preprocess_mentions`

`mention_preprocessing.py` 中 `preprocess_mentions`：
1. `normalize_at_quoted` —— `@"C:\Program Files\foo"` → `@C:\Program Files\foo`，并把引号内的 UNC 路径 `\\host\share\path` 改写为 `@//host/share/path`。
2. `normalize_at_unc` —— 处理裸 UNC `@\\host\share\path` → `@//host/share/path`。
3. `extract_skill_mentions` —— 用 `_SKILL_MENTION_RE = r"(?<![\w/.])@([a-zA-Z0-9_\-]{1,64})"` 找 @-mention，与 SkillRegistry 求交集。

返回 `(normalized_text, prescan_skills: Set[str])`。`prescan` 是用户显式 @ 的 skill 名集合，无论 LLM 后面是否输出 `skills_needed`，这部分一定会被激活。

### 5.2 conversation_history 写入 + LTM triage 异步提交

```python
self.conversation_history.append({"role": "user", "content": message})
self._submit_user_turn_to_ltm_triage(message)
```

`_submit_user_turn_to_ltm_triage`（`orchestrator.py`）：fire-and-forget 异步任务，把当前 user 消息丢给 LTM 的 candidate triage 流程（决定是否值得跨 session 记忆）。**不 await**，不影响主路径。

### 5.3 prescan skill 立即激活

```python
if prescan:
    self._activate_skills_in_checklist({"skills_needed": []}, prescan)
```

即使后续是 chat 路径（不进 Stage 2），@-mention 的 skill 也会立刻激活。`_activate_skills_in_checklist`（`receptionist.py`）流程：
1. `_merge_activated_skills`：合并 LLM 提的 + prescan，再用 `SkillRegistry.has` 过滤未知名字。
2. 调 `checklist.activate_skills(valid)` —— 这里**只激活 delta**，已激活的名字静默忽略。
3. 通知 UI（`_on_reply_to_user(f"Skills activated: ...")`）。
4. log。

激活会同步触发 `on_skills_changed` 回调 → PersistentAgent 收到 delta（详见 §11）。

### 5.4 `_handle_user_message` —— 真正的 Stage 1 调用

`Orchestrator._handle_user_message`：

1. `sections = await self._gather_context_sections()` —— 一次性算齐 4 段（`ltm` / `conversation` / `shell` / `checklist`），见 §5.5。
2. `intent_context = self._format_for_intent(sections)` —— **缓存友好顺序**：append-only 段在前，volatile 段在后。
3. 拼 messages：

```
system: INTENT_SYSTEM_PROMPT
user:   INTENT_TEMPLATE.format(full_context_block=intent_context, message=message)
```

4. `_call_and_parse_streaming(intent_messages, "intent", on_chunk)` —— 流式调用。`response_to_user` 字段通过 `JsonKeyStreamer` 边解析边推给 UI；其他字段（`intent`、`deferred_actions`）只能在流结束后从 accumulated 文本里 parse。
5. **commitment-leak guard**：当 `intent=="chat"` 但 `deferred_actions` 非空时，强制升级到 `task`。`deferred_actions` = 「执行 agent 要在世界里做的操作」（从请求推导，不看回复语气），非空即真有活要干 → 必须跑 planner。
6. 把 Stage 1 的 `response_to_user` 写进 `conversation_history`（assistant 角色）。
7. 若 `intent != "task"`：直接返回 reply（chat 路径）。
8. 若 `intent == "task"`：进入 `_planner_lock`，调 `_run_planner(trigger="user_msg", precomputed_sections=sections)`，**复用 sections**（LTM 召回是最贵的，不重算）。

### 5.5 `_gather_context_sections`（`orchestrator.py`）

四段：

| key | 内容 | 来源 |
|-----|------|------|
| `ltm` | LTM 召回 block（`format_context_block(query=最近一条 user 消息)`），失败/空时 `""` | `_build_long_term_block` |
| `conversation` | `[Recent Conversation History]\n{conv_raw}\n`（slice off 当前消息，因为它在 prompt 模板末尾另渲染）| `_format_conversation_history` |
| `checklist` | `[Current CheckList]\n{body}\n`，body 为空 checklist 时是 `(empty — no active task)` | `_checklist.get_checklist_context_for_planner()` |

### 5.6 INTENT 顺序 vs PLAN 顺序

| 顺序 | 用途 | 理由 |
|------|------|------|
| `_format_for_intent`：conversation → shell → ltm → checklist | 5 分钟 prefix-cache TTL 内能命中（INTENT 调得频繁） | append-only 段在前保最大 byte 稳前缀 |
| `_format_for_planner`：ltm → shell → conversation → checklist | mark_done 触发周期常 >5min，cache 罕中 | 把 CheckList（即将变更的对象）放最近，最大化 attention |

### 5.7 Prompt block：`INTENT_SYSTEM_PROMPT` 详解

`planner_prompts.py` 中的 `INTENT_SYSTEM_PROMPT`。要点：

- **职责**：分类用户消息为 `task` / `chat`。
- **Pass-through**：`task` 时**不抽取 goal**，原文 verbatim 转给 planner。
- **boundary 例**：`"explain how to fix"`→chat / `"fix it"`→task / `"did the tests pass?"`→chat / `"actually use Python"`→task / `"stop"`→task。
- **deferred_actions 规则**：= 「执行 agent 要在世界里做的操作」（文件/代码/系统/外部/多步工作），从**请求**推导而非看回复语气。需要 agent 干活 → `intent=task` 且列出操作；纯偏好/记忆/确认 → `chat`、`deferred_actions=[]`。计划控制（stop/cancel/skip）也是 task。
- **响应风格**：chat 写完整回复；task 写一句过渡 ack（"Sure, working on it"），实质 plan reply 由 Stage 2 给。
- **Skills note**：明示 skill 激活是 Stage 2 职责（INTENT 不要碰）。

输出 schema：

```json
{
  "intent": "task | chat",
  "response_to_user": "...",
  "deferred_actions": ["..."]
}
```

### 5.8 Prompt template：`INTENT_TEMPLATE`

```
{full_context_block}[User Message]
"{message}"
```

`full_context_block` 末尾本身已带 `\n`，所以 `[User Message]` 紧接其后。

---

## 6. 用户消息：Stage 2 PLAN_MODIFY（仅 task 路径）

入口：`Orchestrator._run_planner`，**两路共用**（user_msg 与 mark_done）。调用方必须持有 `_planner_lock`。

### 6.1 输入构建

1. **复用或重算 sections**：`precomputed_sections or await self._gather_context_sections()`。
2. **`full_context_block = self._format_for_planner(sections)`** —— attention 序：ltm → shell → conversation → checklist。
3. **质量控制 preamble**：
   - `_detect_loops(completed_results)`（`planner_mixin.py`）：扫描完成 item 的 instruction（前 80 字符 lowercased）做归一化，挑出失败次数 ≥1 且尝试 ≥2 的 instruction，渲染 `⚠️ LOOP DETECTED` 警告。
   - `_build_epistemic_inventory_warning`（`planner_mixin.py`）：
     - **首次调用**（results 空）：用 7 个 regex（hostname / domain / 文件路径 / Windows 路径 / 文件名 / env var / API endpoint）扫 user_message，最多列 10 条 ASSUMED 实体，要求 planner 把验证 item 排在依赖它的 action item 之前。
     - **首个 item 完成后**：`results 长度==1` 且有 `key_findings/factual_outcome` → 注入 `[Instruction Grounding Requirement]` 段，要求后续 instruction 用具体值（路径、函数名等），禁止抽象表述。
4. **skills_section** = `self._build_skills_section()`（`receptionist.py`）：从 `SkillRegistry.names()` 列出全部已安装 skill，名字命中 `checklist.active_skills` 时打 `(active)` tag，附 4 条 rule。
5. **system_prompt** = `build_plan_modify_system_prompt(...)`（`planner_prompts.py`）。
6. **user_content** = `PLAN_MODIFY_TEMPLATE.format(full_context_block, epistemic_preamble, loop_warning, user_message=last_user)`，其中 `last_user` 来自 `_last_user_message`（最近一条 user 消息）。

### 6.2 Prompt block：`PLAN_MODIFY` 系统 prompt 三段拼装

```
_PLAN_MODIFY_HEAD
  + _PLAN_MODIFY_TOOLS_WINDOWS / _PLAN_MODIFY_TOOLS_LINUX  ← 平台分支
  + _PLAN_MODIFY_OPS_TAIL
  + skills_section                                          ← 末尾，volatile 段
```

注意 `skills_section` 故意放最后——它是唯一会随 active_skills 变化的段；前面 ~11KB 是稳定前缀，可被 prefix cache 命中。

#### `_PLAN_MODIFY_HEAD`（`planner_prompts.py`）骨架

- **角色定位**：oversight planner（不要分解执行步骤，agent 自己做）；职责 = 设 checkpoint + 定 expected_outcomes + drift 检测。
- **Planning Philosophy**：始终维护**完整的 post-current 列表**；用户首次请求时一次画完整路径；每个 item 完成后重发整张表（必要时调整）。
- **Why Items Exist**：item 不是上下文边界（agent 始终持有完整历史），而是 **drift checkpoint**——决策点是"我是否需要在此处评估 + 可能改向"。给了详尽 split / merge 判据。
- **Drift Monitoring**：3 类漂移（无漂移 / soft / hard）+ 5 个 drift signal（错文件、幻觉路径/函数名/API、>5 iter 卡循环、factual_outcome 与 request 不符、expected_outcomes 未覆盖）。
- **Evaluating Completed Items**：信任工具 ground truth，怀疑无证据的 agent 断言。
- **First Principles** + **Goal-respect floor** + **Epistemic Discipline**（`Information-first rule`：缺信息时第一个 item 必须是探查；`Discovery-tool preference`：用 `glob`/`grep` 而非 `read` 查代码）。
- **Item Instruction Quality**：写 WHAT 而非 HOW；指令 + expected_outcomes 形成契约。
- **Scope Discipline**：不超出用户要求加东西。
- **Expected Outcomes & Risk**：每 item 必须 1–4 条可观察 outcome + risk_assessment（最低 "Low risk — read-only"）。
- **Tool Selection**：在 `tools_needed` 顶层字段声明 session 级激活。

#### `_PLAN_MODIFY_TOOLS_WINDOWS` / `_PLAN_MODIFY_TOOLS_LINUX`（`planner_prompts.py`）

定义 **always-available core tools**：`read · write · edit · glob · grep · shell · notebook_edit`（**不要在 tools_needed 列出**——是基础工具）。

**On-demand tools 表格**：模板 `{on_demand_tools_table}` / `{on_demand_routing_rules}` / `{on_demand_antipatterns}` 由 `FlowControllerV2._push_provider_table_to_orchestrator` 填入。Windows 平台静态行包含 `ssh`、`session`；Linux 包含 `ssh`。最后总有一行 `coding`（写源代码时激活）。

**Routing rules**（first-match-wins）+ **Anti-patterns**（❌ 列举常见错配）。

#### `_PLAN_MODIFY_OPS_TAIL`（`planner_prompts.py`）

- **Single Output**：唯一输出是 `post_current_items`，每次 re-emit 完整 post-current 列表（非 append、非局部修改）。四种路径：smooth path（原样 re-emit）/ adjustment（修改后整体替换）/ failure redirect（`interrupt_current=true` + 矫正项作为新 head）/ end-of-task（`post_current_items=[]`）。
- **Item shape**：

```json
{
  "item_id": "<short kebab-case>",
  "instruction": "<specific, actionable, >20 chars>",
  "expected_outcomes": ["<observable success criterion>", ...],
  "supplement": "<extra data/context>",
  "planner_reasoning": "<why this item exists>",
  "risk_assessment": "<what could go wrong + fallback>",
  "ssh_target": "<user@host if remote>"
}
```

- **Re-emit 不变 item**：必须 verbatim 复制全部字段（不允许改写）。
- **Interrupt Rule**：仅在 (a) 用户说停 / (b) 已完成 item 让当前 item 前提失效 时才 `interrupt_current=true`，必须给 `interrupt_reason`。
- **Output Schema**：

```json
{
  "interrupt_current": false,
  "interrupt_reason": "",
  "post_current_items": [ /* item 数组 */ ],
  "skills_needed": [],
  "tools_needed": [],
  "response_to_user": ""
}
```

注：`response_to_user` 可空——任务完成回复由系统自动生成（见 §8）。

### 6.3 流式 LLM 调用：`_call_and_parse_streaming`

`Orchestrator._call_and_parse_streaming`。

- 接 `extra_kwargs={"json_mode": True}`（PLAN_MODIFY 用，INTENT 不传）。
- 用 `JsonKeyStreamer("response_to_user")` 边吃 delta 边吐 `response_to_user` 片段给 `on_response_chunk`（INTENT 用 chunk_cb，PLAN 用 `self._on_response_chunk`）。
- 流末 `try_parse_json(full_text)` 解析所有字段。
- 异常时 fallback 到非流式 `_call_and_parse`。

### 6.4 失败末项顾问：`_build_failed_tail_warning`

`PlannerMixin._build_failed_tail_warning`（`_detect_loops` 的兄弟方法，**顾问而非执行者**）。

当最近一个完成 item 失败、且**不是 interrupt 引发**时——这是"可能在失败状态上偷偷收尾"的信号。顾问返回一段**注入 planner prompt 的建议串**（与 `loop_warning`/`epistemic_preamble` 同一机制），提醒 planner 收尾前刻意判断：若是真正的终止性障碍（缺凭据 / 用户拒绝 / 工具不可用 / 用户要求停止）就明确记录后收尾；否则换一种方法补做。

它**不改写** `post_current_items`——决定权留给 planner；planner 若选择收尾，真正的有界兜底是 §6.6 → §8 的接受门（info-gain 终止 + `_ACCEPTANCE_SEATBELT_ROUNDS` 安全带）。这样避免了旧 Guard 1"强制注入矫正 item"在不可恢复障碍上无限打转（活锁）的问题。

无已完成结果 / 末项干净成功 / 末项失败但为 interrupt 引发 → 返回 `""`（不唠叨）。

### 6.5 应用输出：`_apply_planner_output`

`Orchestrator._apply_planner_output`。处理顺序很关键：

1. **skills**：`_activate_skills_in_checklist(parsed)`（receptionist 流程）—— skills 激活 delta，触发 `on_skills_changed` 回调，PersistentAgent 注入 skill body 到 prelude。
2. **tools**：`_activate_tools_in_checklist(parsed)`（`Orchestrator`）—— tools 激活 delta，触发 `on_tools_changed` 回调，PersistentAgent 加载新工具实例 + 重建 `_api_tools` schema。
3. **解析 items**：`raw_items = parsed["post_current_items"] or []`，每条经 `CheckListItem.from_planner_dict`，**过滤 instruction <=20 字符的**。
4. **写 post_current_items 在前，发 interrupt 在后**（`_apply_planner_output` 注释明确）：先 `replace_post_current(new_items)`，再 `interrupt_agent(reason)`。理由：若颠倒顺序，agent 收到 interrupt 后可能在 `replace` 抢锁前推进 `_current_index`，这样旧的下一个 pending item 已变成"current"，replace 不能再触它。
5. **Reply**：`response_to_user` 非空且未流过 → 通过 `_on_reply_to_user` batch-emit。
6. **任务完成判定**：`return completed_count > 0 and not has_pending and get_current_item() is None`。空初始状态不算完成。

### 6.6 任务完成回 → 验证门

若 `_apply_planner_output` 返回 True，`_run_planner` 调 `_handle_task_complete_candidate()`（详见 §8）。

### 6.7 PLAN_MODIFY_TEMPLATE

```
{full_context_block}{epistemic_preamble}{loop_warning}{failure_tail_warning}[User Original Message]
"{user_message}"

---
Before emitting operations, reason through: drift check ..., what "done" requires,
epistemic state (observed vs assumed), checkpoint design, tool needs.
```

注意顺序：context → preamble（epistemic + loop + failed-tail 顾问） → user message → 思考骨架。

---

## 7. 后台 Planner Loop（Channel 2）

入口：`Orchestrator.run_planner_loop`。

### 7.1 触发链

```
Agent.mark_current_done(result)
  → checklist._on_item_done_callbacks 同步触发
  → Orchestrator._on_item_done_sync(result)
  → self._planner_trigger.set()
  → run_planner_loop wakeup
```

### 7.2 循环体

```python
while True:
    await self._planner_trigger.wait()
    self._planner_trigger.clear()
    async with self._planner_lock:
        await self._run_planner(trigger="mark_done")
```

`trigger` 字符串只用于日志区分。

### 7.3 与 Channel 1 的串行化

`_planner_lock` 同时被 `_handle_user_message` (task 路径) 与 `run_planner_loop` 抢。先到先得。第二个调用看到第一个的 mutation。

异常处理：CancelledError 退出循环；其他 Exception 仅 log，不退出（保证 loop 永生）。

---

## 8. 任务完成验证门（B1）

入口：`Orchestrator._handle_task_complete_candidate`。当 `_apply_planner_output` 判定 task complete 时被 `_run_planner` 调用。

### 8.1 流程

1. 拉 `completed = checklist.get_completed_results()`，空则直接 return（防御）。
2. 调 `synthesize_acceptance` 跑一次 LLM 验证。失败兜底为 `ACCEPT(fallback=True, gap_summary="Verification synthesis failed.")`。
3. 5-verdict dispatcher：

| Verdict | 动作 |
|---------|------|
| `PASS` / `TRIVIAL` | `_emit_completion_reply()` |
| `ACCEPT` | `_emit_completion_reply(prefix=f"(verification: {gap_summary})\n")` |
| `EXTEND` / `VALIDATE` | `replace_post_current(items_to_inject)`；不发回复，agent 跑完后 mark_done → planner_loop → 再次进门 |
| `EXTEND/VALIDATE` 但 `items_to_inject` 空 | `_emit_completion_reply(prefix=f"(verification {verdict.lower()} produced no item)\n")` |
| 未知 verdict | `_emit_completion_reply(prefix=f"(unknown verdict ...)\n")`（防御；`from_dict` 已 snap 过） |

### 8.2 `synthesize_acceptance`（`planner_mixin.py`）

单次 LLM 调用。输入：

- **Conversation block**：`_render_conversation_block(conversation_history)`（`planner_mixin.py`）—— 全部 user/assistant turn 一字不差（与 `_format_conversation_history` 不同：那个 slice off 当前消息，这里要全保留，因为目标 = 用户最新意图）。
- **Completed items block**：`_render_completed_items_block(completed_results, checklist_items)`（`planner_mixin.py`）—— 每个 item 列 instruction / expected / outcome / artifacts / findings / issues。`item_id.startswith("acceptance_")` 时打 `[acceptance attempt #N]` tag（让验证器自己计数轮次）。
- **Acceptance history line**：`_render_acceptance_history_line`（`planner_mixin.py`）—— 如 `"2 prior acceptance items already ran. If the gap they targeted is still open, return ACCEPT — do not loop."`

### 8.3 Prompt block：`ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT`

`planner_prompts.py` 中的 `ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT`。核心规则：

- **PASS**：用户目标显然达成（工具证据 > agent 断言）；不要追求最大化验证。
- **TRIVIAL**：小请求（单次答复/快速查询），无 artifact/无代码改动；省略验证。
- **EXTEND**：大部分目标已达成但具名子交付缺失，可注入 1+ item 补齐。
- **VALIDATE**：工作看起来完成但缺可观察确认（改了代码但没语法检查、写了文件没读回、远程操作没读回 status）；注入一个窄检查 item。
- **ACCEPT**：差距实际无法用本地工具验证，**或** completed 列表里已有 `acceptance_*` item 而 gap 仍存在 → 不要循环，落地差距描述。

**Restraint**：1+ acceptance_* 已跑且 gap 仍在 → MUST `ACCEPT`。`PASS/TRIVIAL` 强制空 items_to_inject + 空 gap_summary。`ACCEPT` 强制空 items_to_inject + 一句 gap_summary。`EXTEND/VALIDATE` 至少 1 item 且 item_id 必须以 `acceptance_` 开头。

输出 schema：

```json
{
  "verdict": "PASS|TRIVIAL|EXTEND|VALIDATE|ACCEPT",
  "gap_summary": "...",
  "items_to_inject": [ /* 0+ items */ ]
}
```

### 8.4 完成回复合成：`_compose_completion_reply`

`Orchestrator._compose_completion_reply`。从最后一个 completed item 抽：

```
Done: {factual_outcome 拼接}
Key findings: {key_findings}
Artifacts: {artifacts}
```

无字段时回 `"Task complete."`。**没有额外 LLM 调用**——agent 完成 item 时已经写好这些字段，直接拼装即可。

---

## 9. PersistentAgent 单循环（Agent 路径）

入口：`PersistentAgent.run_loop`。

```python
while True:
    item = await self._checklist.wait_for_current_item()
    await self._execute_item(item)
```

CancelledError 退出（log 处理过/成功/失败计数后 raise）。

### 9.1 单 item 执行：`_execute_item`

1. `_advisor.reset_for_item()` —— 清 success_history / failed_approaches / cooldown 等。
2. **设三个 item-static 字段**（**只在 item 切换时重算**，多 iter 期间不变；`_build_messages` 每轮重读）：
   - `_current_item_block = item.to_agent_message()` —— `[New Task] / [Input] / [Expected Outcomes]`。
   - `_current_item_hint = await self._gather_pre_item_hint(item)` —— provider hint（SSH 凭证、浏览器 session、远程 HandQ 路径等）。`flow_controller._gather_pre_item_hints` 遍历 `eligible = providers where needs_per_item_context AND tool_name in active_tools`，构造 v1 `Step` proxy 调 `provider.prepare(step, im, memory)`。
   - `_current_ltm_block = await self._gather_ltm_block(item)` —— 用 `item.instruction` 作 query 调 `ltm.format_context_block(rerank=False)`。
3. `execution_recorder.write_agent_start(...)` —— 落盘 `step_id / goal / planner_reasoning / expected_outcomes / active_tools / ssh_target / skills_required`。
4. IM 状态推到 `"executing"`。
5. `item_result = await self._item_loop(item)` —— 进入 OTA 循环（§9.2）。
6. 累加 token usage + 处理/成功/失败计数器。
7. `execution_recorder.write_agent_end(...)` —— 落盘 `success / factual_outcome / artifacts / key_findings / issues`。
8. `checklist.mark_current_done(item_result)` —— 唤醒 planner_trigger。

### 9.2 单 iteration：`_item_loop`

每轮 8 步：

#### Step 0. interrupt 检查

`if self._checklist.check_interrupt(): ...`：
- `acknowledge_interrupt()` 取 reason，组装 `issue_msg = "Interrupted by planner: <reason>"`。
- 提取 `_extract_partial_artifacts(_tools_used)` —— 从 `_tools_used` 字符串列表里挑出 `write: <path>` / `edit: <path>` 的路径（去重）。
- 立刻 return 一个 `ItemResult(success=False, issues=[...], artifacts=...)`。

#### Step 1. 后台任务收割 + 上下文压缩

- `_poll_completed_background_tasks()`：从 shell 工具拿已完成的后台任务，每条造一个 `ToolResult(tool_name="shell", tool_parameters={"task_id":..., "command":..., "run_in_background":True}, output={...})`，调 `_persist_event_observations(observations, "(Background task results received: ...)")` 把它们当作一个独立 ConversationTurn 写进 `_turns`。
- `await self._compact_conversation()`（详见 §13）。

#### Step 2. reminder + stagnation LTM refresh

- `reminder = self._advisor.get_reminder()` —— 三段提示拼接（详见 §14）。
- `extra_ltm_block = None`；若 `should_refresh_ltm()` 返回 True：
  - 取 `signatures = self._advisor.get_recent_failure_signatures(limit=3)`
  - 调 `extra_ltm_block = await self._gather_stagnation_ltm_block(item, signatures)`—— 用 `f"{item.instruction}\n\nFailed approaches so far: {signatures}"` 作 query 重新召回 LTM。
- 这两个变量**只参与本轮**，**不替换 `_current_ltm_block`**（保留原 happy-path 召回 + 不破坏 prefix-cache anchor）。

#### Step 3. think + act（流式）

`turn_result, tool_results, _iter_token_usage = await self._think_streaming(instruction, reminder, extra_ltm_block)` —— 详见 §10。

`self._advisor.record_turn_tool_count(len(tool_calls))`：递减 cooldown + 更新 16 窗口。

#### Step 4. PTL 恢复

仅当 `turn_result.is_error and error.startswith(LLM_API_ERROR_TAG)` 时进入。剥掉 tag 拿到 `raw_error`，若以 `"PTL:"` 开头：

按顺序尝试，**每步成功就 `continue` 重试本轮**：

1. **缩小 obs budget**：`min_budget = resolve_obs_budget(min(svc.context_window for all services))`，若当前更大就替换（无 continue，直接进入下一步）。
2. **语义压缩**：`_compact_conversation()`，若 `len(_turns)` 减少则 continue。
3. **硬丢弃 turns**：`_hard_drop_turns()`（按字节降到 60% effective budget），>0 则 continue。
4. **丢 LTM 块**：`_drop_current_ltm_block()`（regenerable，最低值），>0 则 continue。
5. **逐出最老 skill prelude entry**：`_evict_oldest_skill_entry()`（session 内不可再注入但仍可调用工具），>0 则 continue。
6. **截断最老 turn 的 obs**：仅保留最后一条，continue。
7. 全部失败 → 剥掉 `PTL:` 前缀，落到普通 LLM 错误处理路径，return `ItemResult(success=False, issues=[...])`。

非 PTL 的 LLM_API_ERROR：直接 return ItemResult。

#### Step 5. completion

`turn_result.is_completion`：
- 若 tool_results 非空（说明流式途中虽然没新 tool_calls 但有 stream error 残留）→ 视为 stream error，失败返回。
- 否则成功返回，把 `factual_outcome / artifacts / key_findings` 复制到 ItemResult。

#### Step 6. tool_results 记录

- 把每个 ToolResult 渲染成 `_tools_used` 字符串（`format_tool_entry`）。`shell/bash` 取 `command`；`read/write/edit` 取 `path`；`ssh` 拼 `action: command/remote_path`；其他工具取第一个 param 值。最多 200 字符。
- 调 `execution_recorder.write_iteration(...)` 落盘每条 ToolResult（带 `parallel_index` 区分同轮多工具）。

#### Step 7. USER_NEW_INSTRUCTION 传播

任一 tool_result `error == "USER_NEW_INSTRUCTION"` → 立即 return ItemResult(success=False, issues=["User new instruction"])。这是 IM 弹窗给用户提供"新指令"通道时的特殊错误码。

#### Step 8. advisor 更新

每个 ToolResult 调 `_advisor.record_tool_result(tr)`：写 success_history、更新 failed_approaches signature、必要时打 `write_param_error` hint。

### 9.3 iteration cap

`MAX_ITEM_ITERATIONS = 999`（默认）。到顶 → log advisor summary（success_rate / consecutive_failures / failed_approaches_count），return ItemResult(issues=["Reached per-item iteration cap (999)"])。

---

## 10. **`_build_messages` —— 每轮 LLM 提交结构（重点）**

`PersistentAgent._build_messages`。

### 10.1 整体布局图

```
┌─────────────────────────────────────────────────────────────┐
│ [system]   _system_prompt                                    │  ← session lifetime stable
├─────────────────────────────────────────────────────────────┤
│ [skill prelude]  _skill_entries flatten                      │  ← append-only
│   user:      <skill bodies block 1>                          │
│   assistant: "Acknowledged."                                 │
│   user:      <skill bodies block 2>                          │
│   assistant: "Acknowledged." [_cache_anchor=True]            │  ← prefix-cache 锚点
├─────────────────────────────────────────────────────────────┤
│ [top user]                                                   │  ← 慢变（compaction/item 完成时变）
│   ---                                                        │
│   [Earlier session progress]                                 │  ← _conversation_summary
│   ...                                                        │
│   ---                                                        │
│   [Item Boundary History]                                    │  ← cross-item view
│   ✓ [item-foo] ...                                           │
│   ✗ [item-bar] ...                                           │
├─────────────────────────────────────────────────────────────┤
│ [conversation trace]                                         │  ← append-only 当 item 内
│   assistant: <reasoning + tool_calls>                        │
│   tool: <tool_result_json>                                   │
│   ... (turns 2..N)                                           │
├─────────────────────────────────────────────────────────────┤
│ [bottom user] —— 每轮重建（最 fresh）                          │
│   <_current_item_block>          ← [New Task]/[Input]/[Expected Outcomes]
│   <_current_ltm_block>           ← per-item LTM (item 启动时召回)
│   [Host Context]\n<_current_item_hint>                       │  ← provider hint
│   [Stagnation Recall — refreshed LTM for current blockers]   │  ← extra_ltm_block (可选)
│   <reminder>                                                 │  ← advisor (可选)
│   "Pick one: (a) tool calls — batch ... (b) completion ..."  │  ← next_action
└─────────────────────────────────────────────────────────────┘
```

### 10.2 各段详解

#### system 段（`_system_prompt`，由 `PersistentAgent.__init__` 拼接）

`AGENT_SYSTEM_PROMPT + 工作目录 + 存储目录 + 平台信息`。session 生命周期内不变。

`AGENT_SYSTEM_PROMPT`（`agent_prompts.py`）核心：

- **Autonomy & Persistence**：完成指令再回报；不达完成不要中途问；遇阻先换路；`error` 仅在确证不可达时用。
- **Operating Mode: Parallel-First Execution**：默认所有独立 tool call 一轮内并发；明确批量场景（多文件读、多 grep、多写）；`concurrent_safe=true` for 只读 shell（ls/find/grep/cat/which 等）。
- **Core Execution Principles** 8 条：理解后再动 / 最小化改动 / 可逆与安全 / 失败诊断协议（连续两次同法失败 = 换路） / 验证（重读改动段、检查 exit code 等） / 用积累的事实 / 大结果集要 cache 到 temp 文件 / 完整交付。
- **Tool Usage**：`read/edit/write` 区分；非平凡数据处理用 Python；并发为默认。
- **Completion / Error JSON 格式**（不调 tool 时输出）：

```json
// completion
{
  "reasoning": "...",
  "factual_outcome": ["..."],
  "artifacts": ["..."],
  "key_findings": ["..."]
}

// error
{
  "reasoning": "...",
  "error": "..."
}
```

平台变量替换：Windows 用 `Get-ChildItem / Test-Path / Select-String / %TEMP%\handq_*.txt`；Linux 用 `ls / test -f / grep / /tmp/handq_*.txt`。

#### skill prelude（`_skill_entries`，每 entry 是 `{"names": (...), "messages": [user, ack]}`）

注入位置：紧跟 system，**始终在 cache anchor 之前**。

- `user` 角色装 `SkillRegistry.render_active_block(delta)` 渲染的 skill body 块。
- `assistant` 角色装固定字符串 `"Acknowledged."`（仅占位，构造 user/assistant 交替合法序列）。
- 最后一条 message 上挂 `_cache_anchor=True`（在 `_build_messages` 的 skill prelude 段）—— `_convert_messages_to_anthropic` 把它转成 `cache_control` breakpoint，整个 prelude 走 prefix cache。
- **append-only**：planner 激活新 skill 时 `_handle_skills_added` 追加新 entry（详见 §11.1）。
- **PTL 恢复时可被逐出**：`_evict_oldest_skill_entry` 删最老的 entry（详见 §14.6）。

#### top user 段（"session context"）

**慢变内容**，集合在一个 user message 里：

```python
top_parts = []
if _conversation_summary:
    top_parts.append(f"---\n[Earlier session progress]\n{summary}\n---")
boundary = checklist.get_recent_results_for_agent(limit=10)
if boundary:
    top_parts.append(boundary)
if turns and not top_parts:
    top_parts.append(_current_item_block or "[Current Task]\n{instruction}")
```

最后一行的 guard 处理特殊情况：当有历史 turns 但 summary 和 boundary 都为空时，需要在 turn trace 之前放一个 user 消息（Anthropic API 要求 turn trace 前必须有 user message；skill prelude 末尾是 assistant ack）。fallback 复用当前 item block。

变更频次：summary 在 compaction 时变化，boundary 在 item 完成时增长。**单 item 多 iter 内基本不变**——这是它放在中间不放最底的原因（让其下方的 turn trace 维持稳定可缓存的前缀，而不是每轮被 instruction 块顶到底）。

#### conversation trace（来自 `_budget_enforced_turns()`）

`_budget_enforced_turns`：
- 计算 `_effective_obs_budget()`（详见 §10.5）。
- 若所有 turn 总字符 ≤ 预算 → 全部返回。
- 超预算 → 从最旧开始 pop，保至少 2 turn。

之后 `_supersede_stale(turns)`：从新到旧扫，对 `(tool_name, action) ∈ SUPERSEDABLE_TOOL_ACTIONS` 的 obs：保留最新一条，更老的同签名 obs 设 `superseded_note`（"[superseded by newer ...]"）。

```python
SUPERSEDABLE_TOOL_ACTIONS = frozenset({
    ("desktop", "screenshot"), ("desktop", "snapshot"),
    ("desktop", "hover_at"), ("desktop", "find_element"),
    ("desktop", "find_and_click"),
    ("browser", "screenshot"), ("browser", "snapshot"),
})
```

是否 mutation 是单调的：turns append-only，预算/压缩只丢最旧；超越的 obs 永不会变成最新。

trace 渲染：

```python
for turn in turns:
    messages.append(turn.assistant_message)
    if turn.has_tool_calls:
        # 标准 tool_calls + 配对 tool_result（按 index 配对 call_id）
        for i, obs in enumerate(turn.observations):
            call_id = tc_list[i]["id"] if i < len(tc_list) else f"call_{i}"
            messages.append({"role": "tool", "tool_call_id": call_id, "content": obs.to_tool_result_json()})
    elif turn.observations:
        # OOB 事件场景（背景任务收割、context truncation notice）
        # 没有 tool_calls 的 obs 用 user 角色合并
        combined = "\n\n".join(obs.to_obs_json(i+1) for i, obs in enumerate(turn.observations))
        messages.append({"role": "user", "content": combined})
```

#### bottom user 段（**每轮重建**，最 fresh）

```python
bottom_parts = [_current_item_block or f"[Current Task]\n{instruction}"]
if _current_ltm_block:
    bottom_parts.append(_current_ltm_block)
if _current_item_hint:
    bottom_parts.append(f"[Host Context]\n{_current_item_hint}")

next_action = (
    "Pick one: "
    "(a) tool calls — batch every independent call in this same turn; "
    "(b) completion JSON (no tool calls) when the instruction is fully achieved; "
    "(c) error JSON (no tool calls) only when the instruction is genuinely unachievable."
)

reminder_section_parts = []
if extra_ltm_block:
    reminder_section_parts.append(
        f"[Stagnation Recall — refreshed LTM for current blockers]\n{extra_ltm_block}"
    )
if reminder:
    reminder_section_parts.append(reminder)
reminder_section_parts.append(next_action)

bottom_parts.append("\n\n".join(reminder_section_parts))
messages.append({"role": "user", "content": "\n\n".join(bottom_parts)})
```

**为什么 bottom 段不在 cache 锚点之内**：
- `_current_item_block` 每个 item 不同。
- `_current_item_hint` 同上（且依赖 SSH 凭证等会变的状态）。
- `_current_ltm_block` 同上。
- `reminder` / `extra_ltm_block` 可能每轮变化。
- 把这些放在 cache anchor 之后，反正不会缓存命中 → 放在最底让模型最 fresh 上下文 = 当前指令，不被旧 obs 顶下去。

**为什么相同 role 相邻不算 bug**：`_build_messages` 内部注释明确 —— Anthropic adapter 会把相邻同 role message coalesce。这里上一个 message 可能是 tool（trace 末尾）或 user（OOB obs 末尾）；当上一个是 user 时，bottom user 与之合并；当上一个是 tool 时则正常排列。

### 10.3 随 item 切换的演化

| 段 | item N→N+1 切换时变化 |
|----|----------------------|
| system | 不变 |
| skill prelude | 仅在 planner 中途激活新 skill 时增长（与 item 切换无关） |
| top user (summary + boundary) | boundary 多一行（item N 的结果）；summary 视压缩触发可能重写 |
| trace | item 内 trace 在 item 结束时被打包成 boundary 行；多数情况下 turns 进入下一个 item 时仍保留（因为 `_turns` 不在 item 切换时清空），由 budget 控制何时丢弃 |
| bottom user | `_current_item_block / _ltm_block / _item_hint` 全部用新 item 重算 |

**关键观察**：`_turns` **不在 item 切换时清空**，会被 budget/compaction 自然推出。这意味着 item N 完成后的 trace 在 item N+1 早期还可见（直到预算压缩它）。这是设计意图——agent 跨 item 持续累积上下文，而不是每个 item 重启。

### 10.4 prefix-cache 担忧详述

cache anchor 一个，挂在 skill prelude 最后一条 message 上。这意味着：

- system + skill prelude = 稳定可缓存前缀。
- top user 段在多 iter 内稳定（仅 compaction / item 完成才变）—— 但**不在 anchor 之内**，所以严格不享 prefix cache。`_build_messages` 内部注释解释这个设计：把 anchor 放在 skill prelude 末尾是因为 skill 增长频繁度低（一次性激活后 session 内不再变），把 top user 段也放在 anchor 内会因 boundary/summary 变更不停 invalidate cache。
- trace 段每轮 append 一个 turn → 严格说在最后一个 turn 之前的字节还是稳定的。但 prefix cache 没法跨"轮"在同一 anchor 上累积命中（除非显式打更多 anchor）。当前实现仅 1 个 anchor。
- bottom user 段每轮变 → 一定不命中。

→ **当前 prefix cache 主要受益者**：system + skill prelude。其他段在 5 分钟 TTL 内偶发可能在 LLM 内部低层缓存帮上忙，但应用层不依赖。

### 10.5 `_effective_obs_budget` —— 真实预算

```python
overhead = (
    sum chars across _skill_entries
    + len(_conversation_summary or "")
    + len(_current_item_block or "")
    + len(_current_item_hint or "")
    + len(_current_ltm_block or "")
)
return max(_obs_budget_chars - overhead, 100_000)
```

为什么扣这些：原 `_obs_budget_chars`（`resolve_obs_budget(context_window)`，见 `agent_utils.py`）只考虑 turn 段；如果 skill prelude / LTM 很大却不扣，session 一旦 skill 累积就会超 PTL 而 budget 端浑然不觉。地板 100k 防御 budget 跌为 0 / 负。

**注意**：100k 地板是 budget 下限，不是绝对装得下的保证——若 skill+LTM+system 已超 context window，PTL 仍会触发，需要 §13 的恢复链路。

---

## 11. Inject 全景对照（**重点**：每类 inject 的 role / 位置 / 生命周期）

### 11.1 Skill 注入

| 维度 | 内容 |
|------|------|
| 触发 | `Orchestrator._activate_skills_in_checklist` → `checklist.activate_skills(delta)` → `checklist._on_skills_changed_callbacks` 同步触发 → `PersistentAgent._handle_skills_added(delta)` |
| 注入位置 | `_skill_entries`（`_build_messages` 中渲染为 system 之后、top user 之前的 user/assistant 对） |
| 注入内容 | `SkillRegistry.render_active_block(delta)` 的输出（为 delta 中所有 skill 名拼接的 body 文本） |
| 角色 | user（body）+ assistant（"Acknowledged."） |
| 生命周期 | session（append-only，no deactivate） |
| PTL 可逐出 | 是（最老 entry 优先） |
| 对 Orchestrator 可见 | 名字（`active_skills`）；body 不见 |
| 对 Agent 可见 | body 全见 |
| 对 verifier 可见 | 不直接可见（verifier 仅看 conversation + completed items） |

`_handle_skills_added`：

```python
delta = [n for n in names if n and n not in self._injected_skills]
if not delta: return
block = SkillRegistry.get().render_active_block(delta)
if not block.strip(): return
self._skill_entries.append({
    "names": tuple(delta),
    "messages": [
        {"role": "user", "content": block},
        {"role": "assistant", "content": "Acknowledged."},
    ],
})
self._injected_skills.update(delta)  # 仅在 body 真正渲染时记账
```

### 11.2 Tool 注入（base / extra 区分）

#### base tools（`read · write · edit · glob · grep · shell · notebook_edit`）

- 加载时机：`PersistentAgent.__init__` 调 `ToolRegistry.create_all_tool_instances()`。
- API schema：`ToolRegistry.generate_tools_for_api()` 一次性生成。
- planner 视角：在 `_PLAN_MODIFY_TOOLS_*` prompt 段顶部明示"DO NOT list"。
- agent 视角：直接通过 `tools=` 参数传给 LLM API（不在 prompt body 里）。

#### extra tools（on-demand，如 `ssh / session / browser / desktop / coding / remote_handq / web_search / email / teams / ask_human`）

- 触发：planner 在 `tools_needed` 中声明 → `_activate_tools_in_checklist` → `checklist.activate_tools(delta)` → `_on_tools_changed_callbacks` → `PersistentAgent._handle_tools_added(delta)`。
- 加载：`ToolRegistry.create_all_tool_instances(extra_tool_names=delta)` 增量加载实例到 `self.tools` dict。
- API schema 重建：`self._api_tools = ToolRegistry.generate_tools_for_api(extra_tool_names=all_loaded_extra)`，覆盖整个 schema 列表（base + 累计的 extra）。
- planner 视角：在 `_PLAN_MODIFY_TOOLS_*` 表里看见可用名字 + 激活条件（`{on_demand_tools_table}` 由 provider 描述填）。
- agent 视角：API schema 自带描述，agent 看到的是 `tools=` 参数下的工具定义；prompt body 不重复说明。

#### 工具的别名 / 验证（执行时）

- `TOOL_NAME_ALIASES`（`agent_utils.py`）：`write_file → write`、`read_file → read`、`search → grep`、`find_files / list_files → glob`、`bash → shell`。
- `_validate_tool_parameters`（`PersistentAgent`）：用 `ToolRegistry.get_tool_metadata(tool_name).parameter_schema` 校验 allowed/required；多余/缺失参数构造可读错误。

### 11.3 LTM 注入（**三处**召回）

| 召回点 | query | 注入位置 | 触发 |
|--------|-------|----------|------|
| Orchestrator INTENT/PLAN 上下文 | 最近一条 user 消息（`conversation_history[-1]`） | `_format_for_intent` / `_format_for_planner` 里的 `[Long-Term Memory]` 段（具体格式由 `format_context_block` 决定） | 每条用户消息 / 每次 `_run_planner` |
| Agent 启动 item | `item.instruction` | `_current_ltm_block`（bottom user 段，多 iter 共享） | item 切换时（`_execute_item`） |
| Agent stagnation 刷新 | `f"{item.instruction}\n\nFailed approaches so far: {signatures}"` | `extra_ltm_block`（bottom user 段的 reminder section，每轮独立块） | `IterationAdvisor.should_refresh_ltm() == True` 时 |

**关键差异**：
- Orchestrator 召回为"会话级理解"服务（决定 intent + plan）。
- Agent item 启动召回为"happy-path 召回"（item 入口快照）。
- stagnation 刷新为"卡住时换角度"召回（query 含失败签名），**不替换 item 启动的快照**——并存可见。

LTM 是只读消费者：召回失败 → 返回 `""` 或 None，注入即 no-op。所有调用都 wrap 在 try/except 里。

### 11.4 Item 内容注入

CheckListItem 的字段在不同消费者眼里：

| 字段 | Orchestrator planner prompt | Agent prompt | Verifier prompt | ExecutionRecorder |
|------|----------------------------|--------------|-----------------|-------------------|
| `item_id` | ✓（CheckList Status） | ✓（boundary） | ✓（completed items） | ✓ |
| `instruction` | ✓ | ✓（`[New Task]`） | ✓ | ✓ |
| `expected_outcomes` | ✓ | ✓（`[Expected Outcomes]`） | ✓ | ✓ |
| `supplement` | — | ✓（`[Input]`） | — | — |
| `planner_reasoning` | — | — | — | ✓ |
| `risk_assessment` | — | — | — | — |
| `ssh_target` | — | provider hint 通过它生成（间接） | — | ✓ |

注：`risk_assessment` 是 planner 自我决策辅助字段，从未被任何 prompt 渲染——仅在 planner prompt 模板里要求 planner 写出来。是写给 planner 自己未来回看用的。

---

## 12. Tool 执行链：`_execute_one`

每个 ToolCall 走的路径：

1. **别名解析**：`tool_name = TOOL_NAME_ALIASES.get(tool_name, tool_name)`（重建 ToolCall）。
2. **`_check_before_act`** 决定是否需要确认（详见 §12.1）。
3. **工具存在性检查**：`tool_name not in self.tools` → `Unknown tool: ...`。
4. **`_validate_tool_parameters`**：参数 schema 校验。
5. **IM 通知开始执行**（带 truncated params，>2000 字截断）。
6. **`tool.execute(**parameters)`** —— 真正执行。
7. 若 `result.tool_name` / `result.tool_parameters` 未设置则补齐。
8. **IM 通知执行完成**（带 truncated output）。
9. 返回 ToolResult。

### 12.1 `_check_before_act`

决策树：

1. **`is_high_risk` 命中**（详见 §15）：
   - 若 `auto_approve.high_risk` enabled → 放行（return None）。
   - 否则若有 IM → `request_risk_confirmation(get_risk_description(...))` 弹窗。
   - 无 IM → `UserConfirmation.no()`。
2. **`write/edit` 在 working dir 内**：直接放行。
3. **`desktop`** 工具：
   - `is_task_approved()` 已批 → 放行。
   - 未 rescind 且 `auto_approve.tool_desktop` enabled → mark_task_approved 后放行。
   - 否则 IM 弹窗，approved 时调 `mark_task_approved`。
4. **tool-specific switch**：`write→tool_write`、`edit→tool_edit`、`bash/shell→tool_bash`、`browser→tool_browser`，对应 config switch enabled 则放行。
5. **fallback**：IM `request_tool_confirmation`；无 IM → `UserConfirmation.no()`。

`UserConfirmation` 五种语义（`models/state.py`，外部模块）：approved / rejected / risk_guidance（带 message）/ new_instruction（带 message）/ no。`_execute_one` 对每种返回不同的 ToolResult：
- approved → 继续执行。
- rejected → `error="User rejected operation"`。
- risk_guidance → `error=f"High-risk operation was not executed. User guidance: {message}"`。
- new_instruction → `output=message, error="USER_NEW_INSTRUCTION"` —— Step 7 会把它向上传播，让 item 中止。

### 12.2 并发安全：`_is_concurrency_safe_call`

- `bash/shell`：取 `tc.parameters.get("concurrent_safe", False)`（agent 自己声明）。
- 其他工具：取 `tool.is_concurrency_safe` 类属性。

`_think_streaming` 用这个判定来决定新 ToolCall 是否要等待已 dispatch 的任务完成（详见 §13.2）。

---

## 13. `_think_streaming` —— 流式工具分发

### 13.1 起手

1. `messages = self._build_messages(instruction, reminder, extra_ltm_block)`。
2. IM 状态推到 `"thinking"`。
3. `chat_kwargs = dict(messages=messages, tools=self._api_tools, json_mode=False)`。
4. `service_offset = 0`，进入外层 `while True` 循环（用于 mid-stream 失败的多 service 重试）。

### 13.2 核心循环 —— 流事件分发

`call_with_fallback_stream(services_slice, chat_kwargs, ...)` 产生异步事件流。

`StreamToolCallEvent`：每出一个 tool_call 立即分发：

```python
tc = ToolCall(call_id, tool_name, args)
stream_tool_calls.append(tc)
api_tool_calls_for_msg.append({"id": call_id, "function": {...}})

is_safe = self._is_concurrency_safe_call(tc)
# 写/编辑同路径强制串行（同路径多次写有依赖）
if tool_name in ("write", "edit"):
    path = parameters.get("path", "")
    if path and path in dispatched_write_paths:
        is_safe = False
    else:
        is_safe = True
        dispatched_write_paths.add(path)

prereqs = [] if is_safe else [t for _, t in running_tasks if not t.done()]
task = asyncio.create_task(_run_after(tc, prereqs))
running_tasks.append((tc, task))
ordered_tasks.append((tc, task))
```

`_run_after(tc, prereqs)` 先 await prereqs（gather with return_exceptions）再调 `_execute_one(tc)`。

`StreamDoneEvent`：流结束。
- `reasoning = result.content or ""`
- 若 stream_tool_calls 非空 → 构造 assistant message 含 `tool_calls`，turn_outcome 走 act 路径。
- 否则 → 构造无 tool_calls 的 assistant，调 `TurnOutcome.from_completion_text(reasoning)` 解析 completion JSON / error JSON。
- 通知 IM `notify_decision_made(iteration, reasoning, total_tokens)`。

### 13.3 异常处理

捕到 `_stream_error`：
- IM 状态恢复 `"executing"` + `display_error`。
- 取消所有 running_tasks。
- **PTL 检测**：`if self._services[0]._is_prompt_too_long_error(_stream_error): return TurnOutcome(error=f"{LLM_API_ERROR_TAG}:PTL: {error}")`，让上层走 §9.2 Step 4 的 PTL 链路。
- **Mid-stream 重试**：仅当 `not stream_tool_calls`（即未分发任何工具）时，才尝试 `service_offset += 1` 落到下一个 service 重试。已分发工具的场景不重试（防止重复副作用）。
- 否则 → 返回普通 `LLM_API_ERROR`。

### 13.4 收尾

正常退出 `while True` 后：

- 若 `turn_outcome is None`（流没出 StreamDoneEvent 就结束了）—— 取消 tasks + 返回失败 outcome 与一个特殊 ToolResult(`tool_name="llm_stream"`, error=...)。
- 否则收集 `tool_results`：每个 ordered_task await，cancel/exception 都包成失败 ToolResult。
- **持久化 turn**：仅当 `_asst_msg` 有 tool_calls 或 tool_results 非空时（`_think_streaming` 末尾的 if 分支），把 `ConversationTurn(assistant_message=_asst_msg, observations=tool_results)` append 到 `_turns`。这条注释明确解释：纯 completion turn（无工具、无 obs）不写入是为了避免两条相邻的 assistant 消息（API 会 400）。
- 返回 `(turn_outcome, tool_results, TokenUsage)`。

---

## 14. 上下文管理（compaction + supersession + PTL）

### 14.1 ConversationTurn 累积与渲染（已在 §10.2 trace 段说过，此处只补齐 OOB 路径）

`_persist_event_observations`：把 OOB 事件（背景任务收割、context truncation 通知）封装成一个独立 ConversationTurn——`assistant_message={"role": "assistant", "content": note}`（合成 ack）+ `observations=...`。这条 turn 在 `_build_messages` 里走 `elif turn.observations:` 分支（无 tool_calls），所有 obs 合并成一个 user message 渲染。

### 14.2 `_compact_conversation`

**触发条件**：
1. `len(_turns) > KEEP_RECENT_TURNS + 2` 即 >7。
2. `total_chars > _effective_obs_budget * 0.8`（即用了 ≥80%）。

**流程**：
- `compress_count = len(_turns) - KEEP_RECENT_TURNS`（即压缩前段，保留最近 5 turn）。
- 用 `_build_trace_for_compaction(compress_count)` 把要压缩的 turn 渲染成可读 trace 文本。
- 若已有 `_conversation_summary` → 拼接成 `[Previous summary to re-compress]\n... \n\n[New turns]\n...`，避免 summary 无界增长。
- `await _llm_compress(trace_text, compress_count)`（详见 §14.3）。
- 更新：`_turns = _turns[-5:]`；`_conversation_summary = new_summary`（**整体替换**而非 append，旧 summary 已折进 trace 里）。

### 14.3 `_llm_compress`

- prompt：`COMPACT_CONVERSATION_PROMPT.format(trace_text=...)`（`agent_prompts.py`）。
- 规则：保留 key discoveries / actions+outcomes / failed approaches+why / current state。
- 压缩规则：合并重复读、丢冗余中间输出、保路径/签名/配置/错误信息、≤800 token 数字编号叙事。
- 输出 plain text 不要 JSON。
- 调 `call_with_fallback` 单次 LLM 调用。
- 失败 → 调 `_rule_based_fallback_summary(turn_count)` 写一条机械 fallback。

### 14.4 `_rule_based_fallback_summary`

每条 obs 渲染成 `- {tool_name}({param_desc}) → OK/FAIL`，仅在 LLM 压缩失败时使用。

### 14.5 `_hard_drop_turns`

按字节降到 60% effective budget：
- 计算 `target = int(_effective_obs_budget * 0.60)`。
- 从 `_turns[0]` 起 pop，累计 dropped_chars，到 `total - dropped <= target` 停。
- 把丢弃 turns 的 obs 用机械 summary 渲染，append 到 `_conversation_summary`（"[Hard-drop summary]\n..."）。
- `_persist_event_observations` 写一条 `tool_name="context_truncation_notice"` 的 ToolResult，让 agent 看到"X turns dropped"通知。

### 14.6 PTL 链路总览

已在 §9.2 Step 4 详述。再列一遍恢复梯度（从无损到有损）：

```
1. obs budget 收缩       —— 调最小 service 的 budget；对历史无影响
2. semantic compaction   —— 老 turn 压缩成 narrative summary，可恢复
3. hard drop turns       —— 老 turn 直接丢，summary 兜底
4. drop _current_ltm_block —— 本 item 的 happy-path LTM 抹掉，可重召回
5. evict 最老 skill entry  —— skill body 永久丢（session 内不可再注入）
6. truncate 最老 turn obs  —— 留最后一条 obs；语义损失最大
7. give up               —— 返回 LLM_API_ERROR
```

设计原则：先动可重建/低价值的内容，再动高价值的（system / 当前 item / host hint 永远不动）。

---

## 15. `IterationAdvisor` —— per-item 健康追踪（`agent_utils.py`）

### 15.1 输入信号

- `record_tool_result(tr)`：成功/失败计入 `_success_history`；失败时 `failed_approach_signature(tr)` 归一化签名（数字归 `#`、长字符串 head/tail 截断）累加 count；`write` 工具 + 参数错误 → 设置 `_last_error_hint = "write_param_error"`；成功时清 hint。
- `record_turn_tool_count(count)`：每轮调一次；维护最近 16 轮的 tool 数；递减 `_parallelism_cooldown` 与 `_ltm_refresh_cooldown`。

### 15.2 `get_reminder()` 输出（merge 三段）

1. **Anti-repeat guard**：`failed_approaches` 中 count ≥2 的取前 5，渲染 `(Nx failed) <signature>` 列表。
2. **Parallelism nudge**：cooldown 0 + 最近 5 轮全是 1 tool/轮 → 提醒批量。设 cooldown=5。
3. **Stagnation / write_param_error**（`_check_stagnation`）：
   - `_last_error_hint == "write_param_error"` → 返回 `_WRITE_PARAM_ERROR_REMINDER`（详细教用 append 模式分块写）。
   - 连续失败 ≥5（`_SEVERE_STAGNATION`）→ "Significant Challenge Detected"，建议根本性换路或 `error`。
   - 连续失败 ≥3（`_MODERATE_STAGNATION`）→ "Progress Note"，建议反思与换路。

三段如有任一非空，用 `\n\n` 拼起来；都空则返回 None。

### 15.3 `should_refresh_ltm()` —— LTM stagnation 刷新触发（新增）

- cooldown 0 + 连续失败 ≥3 → True，设 cooldown=5。
- 否则 False。

`get_recent_failure_signatures(limit=3)`：`failed_approaches` 按 count 降序取前 N 个 signature。

### 15.4 `get_summary()`

供 iteration cap log 用：`success_rate / consecutive_failures / failed_approaches_count`。

---

## 16. Risk Check（`risk_check.py`）

无状态函数集合，操作在 `(tool_name, parameters)` 元组上。

### 16.1 `is_high_risk`

- `tool=="browser"` + `action=="attach_browser"` → True（共享浏览器是大风险）。
- `tool not in ("bash","shell")` → False（其他工具不直接走风险路径，由 `_check_before_act` 的 tool-specific 分支处理）。
- 命令文本经 4 层判定：
  1. **whitelist**：含任意白名单子串 → False。
  2. **always_dangerous**：keyword（边界匹配）/ pattern → True。
  3. **path-based**：`high_risk_keywords` (rm/delete/format/truncate) 或 `custom_patterns` 命中 → 进入 `_all_paths_within_working_dir(command, wd)` 判定，若所有路径都在 working dir 内 → False（自动放行），否则 True。
  4. 其他 → False。

### 16.2 `get_risk_description`

人类可读的弹窗文本：browser 走预设话术；shell 走 "Command : ...\nKeyword : ..."（围绕第一个命中关键词智能截断 ±50 字符上下文）。

### 16.3 `is_path_within_working_dir`

`Path.resolve` 后看能否 `relative_to(wd)`。失败 catch（ValueError/OSError）→ False。

### 16.4 `_all_paths_within_working_dir` 拒绝用户家目录引用（`~/`）、parent traversal（`../`）；提取所有绝对路径（POSIX `/...` + Windows `X:\...`），逐条检查。无绝对路径时返回 True（认为是"无路径或仅相对路径，按 working dir 算"）。

---

## 17. Mention 预处理（`mention_preprocessing.py`）

入口：`preprocess_mentions(text)` → `(normalized, prescan_skills)`。

3 个 regex：

```python
_AT_QUOTED_RE  = r'(?<![\w/.])@"([^"]*)"'
_AT_UNC_RE     = r'(?<![\w/.])(@)\\\\([A-Za-z][A-Za-z0-9.\-]{0,62})\\([A-Za-z0-9_$][^\s,;<>"\'\)\]|]*)'
_SKILL_MENTION_RE = r"(?<![\w/.])@([a-zA-Z0-9_\-]{1,64})"
```

共享的 lookbehind `(?<![\w/.])` 避免 email (`@user`)、装饰器 (`@decorator`)、路径片段 (`/@thing`)。

顺序：quoted strip → UNC normalize → skill scan。Quoted strip 必须先做，否则带空格的引号路径会让 UNC regex 在 path 边界失效。

`extract_skill_mentions` 用 `SkillRegistry.has(name)` 过滤，未注册的 @-mention 静默丢弃。

---

## 18. 文件级函数索引

### `__init__.py`

| 名字 | 用途 |
|------|------|
| `SharedCheckList / CheckListItem / ItemResult / INTERRUPTED_BY_PLANNER` | re-export |
| `Orchestrator / PersistentAgent / FlowControllerV2 / AcceptanceVerdict` | re-export |

### `flow_controller.py`

| 名字 | 用途 |
|------|------|
| `FlowControllerV2.__init__` | 持有 services / wd / config / IM；预收集 ContextProvider |
| `start` | 启动 ExecutionRecorder + CheckList + Orchestrator + Agent + 两个 task |
| `on_user_message` | 委托给 Orchestrator |
| `destroy / cancel_all_tasks` | 取消两个 task |
| `register_item_context_provider` | 外部追加 provider |
| `_cancel_loops / _forward_reply_to_ui / _describe_current_status` | helper |
| `_collect_default_providers` | 平台白名单批量收集 provider |
| `_push_provider_table_to_orchestrator` | 把 provider 描述塞到 planner prompt 占位符 |
| `_gather_pre_item_hints` | per-item 给 agent 的 provider hint（构造 v1 Step proxy 调 prepare） |

### `orchestrator.py`

| 名字 | 用途 |
|------|------|
| `__init__` | services + checklist + planner_lock + planner_trigger + 注册 mark_done 回调 |
| `on_user_message` | Channel 1 入口；preprocess + LTM triage + prescan |
| `run_planner_loop` | Channel 2：mark_done 触发的 planner |
| `_handle_user_message` | Stage 1 + 转 Stage 2 dispatcher |
| `_run_planner` | 统一 planner 调用（user_msg / mark_done 共用） |
| `_call_and_parse_streaming` | 流式 LLM 调用 + JsonKeyStreamer 抽 response_to_user |
| `_call_and_parse` | 非流式兜底 |
| `_gather_context_sections` | 一次性算 ltm/conversation/shell/checklist 4 段 |
| `_format_for_intent` | append-only 在前，volatile 在后（cache-friendly） |
| `_format_for_planner` | volatile 在后（attention-friendly） |
| `_format_conversation_history` | slice off 当前消息的前置对话 |
| `_build_long_term_block` | LTM 召回（query=最近 user 消息）|
| `_submit_user_turn_to_ltm_triage` | fire-and-forget triage |
| `_on_item_done_sync` | 触发 planner_trigger |
| `_rewind_user_turn` | 网络失败时回滚 user 消息 |
| `_normalize_deferred_actions` | 强制成 List[str] |
| `_activate_tools_in_checklist` | 激活 tools_needed 的 delta |
| `_last_user_message` | 最近一条 user |
| `_apply_planner_output` | 应用 parsed 到 checklist；返回是否 task complete |
| `_handle_task_complete_candidate` | B1 验证门 5-verdict 分发 |
| `_emit_completion_reply / _compose_completion_reply` | 拼最终完成回复 |

### `persistent_agent.py`

| 名字 | 用途 |
|------|------|
| `__init__` | 加载工具 + system prompt + 注册 callback + 初始化各持久字段 |
| `run_loop` | 永生循环：拉 item + 执行 |
| `_log_observations` | 单轮 obs 摘要日志 |
| `_execute_item` | 单 item 入口：set static blocks + write_agent_start + item_loop + write_agent_end + mark_done |
| `_item_loop` | 单 item 多 iter 主循环（OTA + PTL recovery + completion） |
| `_think_streaming` | 流式 LLM + tool 分发 |
| `_build_messages` | 拼接 LLM messages（重点） |
| `_effective_obs_budget` | 真实可用预算（扣 overhead，地板 100k） |
| `_budget_enforced_turns` | 超预算时丢最旧（保至少 2） |
| `_supersede_stale` | 同一 (tool,action) 旧 obs 设 superseded_note |
| `_execute_one` | 单工具执行链（别名+确认+校验+IM 通知+execute） |
| `_check_before_act` | 风险/写编辑/desktop/tool-switch 多分支 |
| `_validate_tool_parameters` | schema 校验 |
| `_is_concurrency_safe_call` | 并发安全判定 |
| `_persist_event_observations` | OOB 事件以独立 turn 持久化 |
| `_poll_completed_background_tasks` | 收割 shell 后台任务 |
| `_update_obs_budget_for_service` | service 切换时更新预算 |
| `_on_network_event` | 网络 down/restored 通知 IM |
| `_compact_conversation` | 阈值触发的语义压缩 |
| `_build_trace_for_compaction` | 旧 turn 渲染成可读 trace |
| `_llm_compress` | LLM 压缩调用（COMPACT prompt） |
| `_rule_based_fallback_summary` | 机械 fallback summary |
| `_hard_drop_turns` | 字节硬丢弃 + 保留 summary + 通知 |
| `_flat_skill_messages` | _skill_entries 拍平 |
| `_evict_oldest_skill_entry` | PTL 用：删最老 skill entry |
| `_drop_current_ltm_block` | PTL 用：清掉本 item LTM block |
| `_extract_partial_artifacts` | interrupt 时从 _tools_used 里抽 write/edit 路径 |
| `_gather_pre_item_hint` | 调 flow_controller 的 hint provider |
| `_gather_ltm_block` | item 启动时召回（query=instruction） |
| `_gather_stagnation_ltm_block` | stagnation 时召回（query 富化失败签名） |
| `_handle_skills_added` | skill 激活回调：渲染 body + 加 _skill_entries |
| `_handle_tools_added` | tool 激活回调：加载实例 + 重建 _api_tools schema |

### `shared_checklist.py`

| 名字 | 用途 |
|------|------|
| `INTERRUPTED_BY_PLANNER` | 中断 issue 字符串前缀（agent 写、失败末项顾问读） |
| `CheckListItem`（`from_planner_dict / to_agent_message`） | item 模型 |
| `ItemResult` | result 模型 |
| `SharedCheckList.__init__` | 初始化各种集合、event、callback list |
| 属性 `current_index / total_items / completed_count / has_pending / items / active_skills / active_tools` | 只读快照 |
| `activate_skills / activate_tools` | append-only 激活 + 触发 callback |
| `wait_for_current_item / mark_current_done / check_interrupt / acknowledge_interrupt / get_current_item` | agent 接口 |
| `replace_post_current / interrupt_agent` | planner 接口 |
| `get_completed_results / get_pending_items` | 共享读 |
| `get_checklist_context_for_planner` | planner prompt 段渲染 |
| `get_recent_results_for_agent` | agent boundary 段渲染 |
| `on_item_done / on_skills_changed / on_tools_changed` | 注册回调 |

### `planner_mixin.py`

| 名字 | 用途 |
|------|------|
| `_ALLOWED_VERDICTS` | 5 verdict 常量 |
| `AcceptanceVerdict.from_dict / from_data` | 验证器输出解析 + 防御 |
| `PlannerMixin._init_planner` | 初始化 on_demand_* 三个空字符串 |
| `_detect_loops` | 扫描重复失败 instruction 渲染 LOOP DETECTED 警告 |
| `_build_epistemic_inventory_warning` | 首次扫 ASSUMED 实体 / 首个 item 后注入 grounding requirement |
| `_build_failed_tail_warning` | 失败末项顾问：将要在失败上收尾 → 注入 prompt 建议（不改写 items，planner 保权威） |
| `synthesize_acceptance` | B1 LLM 验证调用 |
| `_render_conversation_block / _render_completed_items_block / _render_acceptance_history_line` | 验证 prompt 段渲染 |

### `receptionist.py`

| 名字 | 用途 |
|------|------|
| `_activate_skills_in_checklist` | 合并 LLM + prescan，registry 过滤，激活，UI 通知 |
| `_build_skills_section` | 渲染 [Available Skills] prompt 段（含 (active) tag） |
| `_merge_activated_skills` | helper：合并 + registry 过滤 |

### `mention_preprocessing.py`

| 名字 | 用途 |
|------|------|
| `_normalize_quoted_at` | 单匹配 helper |
| `normalize_at_quoted` | `@"..."` 引号剥离（含引号内 UNC 处理） |
| `normalize_at_unc` | `@\\host\share\path` → `@//host/share/path` |
| `extract_skill_mentions` | @name 扫描 + registry 过滤 |
| `preprocess_mentions` | 总入口（quoted → unc → skills） |

### `risk_check.py`

| 名字 | 用途 |
|------|------|
| `_load_config` | 读 config_manager.high_risk_config，失败给默认值 |
| `is_path_within_working_dir` | resolve + relative_to 判定 |
| `is_high_risk` | 4 层判定（whitelist / always_dangerous / path-based / 其他） |
| `get_risk_description` | 弹窗文案构造 |
| `_all_paths_within_working_dir` | 提取绝对路径并逐条判定 |

### `agent_utils.py`

| 名字 | 用途 |
|------|------|
| `LLM_API_ERROR_TAG` | 错误 tag（`"LLM_API_ERROR"`） |
| `INFRA_TOOL_NAMES` | `{llm_stream, context_truncation_notice}`，advisor 不计入失败签名 |
| `TOOL_NAME_ALIASES` | 工具名别名表 |
| `SUPERSEDABLE_TOOL_ACTIONS` | 可被新结果取代的 (tool, action) 集合 |
| `ConversationTurn` | 单轮容器 |
| `resolve_obs_budget` | context_window → obs budget chars |
| `format_tool_entry` | `_tools_used` 字符串渲染 |
| `_NUMERIC_NORMALIZE_RE / _normalize_numeric / _smart_truncate` | signature 归一化 helper |
| `failed_approach_signature` | tool/cmd/path → 稳定签名 |
| `IterationAdvisor` 全部方法 | 见 §15 |
| `TurnOutcome` | think 结果判别式 |

### `agent_prompts.py`

| 名字 | 用途 |
|------|------|
| `get_platform_context` | 单行平台字符串（拼到 system prompt 末尾） |
| `_generate_system_prompt` | 平台分支生成 AGENT_SYSTEM_PROMPT |
| `AGENT_SYSTEM_PROMPT` | agent 系统提示（成品） |
| `COMPACT_CONVERSATION_PROMPT` | 压缩用 LLM prompt |

### `planner_prompts.py`

| 名字 | 用途 |
|------|------|
| `INTENT_SYSTEM_PROMPT / INTENT_TEMPLATE` | Stage 1 |
| `_PLAN_MODIFY_HEAD / _PLAN_MODIFY_TOOLS_WINDOWS / _PLAN_MODIFY_TOOLS_LINUX / _PLAN_MODIFY_OPS_TAIL` | Stage 2 三段 |
| `build_plan_modify_system_prompt` | 平台分支组装 |
| `PLAN_MODIFY_TEMPLATE` | Stage 2 user template |
| `ACCEPTANCE_SYNTHESIS_SYSTEM_PROMPT / ACCEPTANCE_SYNTHESIS_TEMPLATE` | B1 验证门 prompt |

---

## 19. 已清理 / 仍待审视

> 本节按"已清理 → 仍存"分两段。早期版本残留过几处死代码，本轮已经一并删除。

### 19.A 已清理（本轮删除，不再存在于代码中）

- **`agent_utils.py` 中的 `supersede_stale_snapshots(paired)`** —— 已删除。是早期 paired-tuple 版本，被 `PersistentAgent._supersede_stale(turns)` 替代后再无引用。删除后随之清理了 `agent_utils.py` 顶部的 `import json`（再无消费者）。
- **`shared_checklist.py` 中的 `ItemStatus` 枚举** —— 已删除。`CheckListItem` 没有 `status` 字段，运行时状态由 `current_index / completed_count / has_pending` 推导。同步删了 `from enum import Enum` 引用。
- **`shared_checklist.py` 中的 `CheckListDiff` dataclass + `__init__.py` 的导出** —— 已删除。原意图是用它给 agent loop 注入 skill / tool delta，但实际实现走 `on_skills_changed / on_tools_changed` 回调路径，dataclass 永远是空字段而调用者也丢弃返回值。`replace_post_current` 现在返回 `None`。

### 19.B 仍存的写而不读字段

- **`CheckListItem.risk_assessment`**：planner prompt 要求每条 item 写 `risk_assessment`，`from_planner_dict` 也接收它，但没有任何 prompt / 渲染路径会回读。仅 `_apply_planner_output` 经 `from_planner_dict` 把它存到 CheckListItem 实例。可能用途：（a）planner 自我决策辅助（写下来强迫自己思考）；（b）未来给验证器或 UI 用。**当前无运行时影响**——若确认无人读，可一并删除字段与 prompt 对应文案。

### 19.C 已优化

- **`_check_before_act` 中 desktop_tool 的 lazy import** —— 已迁到模块顶部，加 `_DESKTOP_HELPERS_AVAILABLE` 模块级旗标。原来在热路径里 `from ..tools.desktop_tool import ... except ImportError: pass` 的写法，把"helpers 不可用"的失败信号埋在了每次调用里；现在 import 失败会在模块加载阶段就被识别，`_check_before_act` 里直接 `if not _DESKTOP_HELPERS_AVAILABLE: return UserConfirmation.no()` 显式拒绝（行为与原来等价：缺 helper 时拒绝调用，不退化到每次弹窗）。

---

## 20. 一句话索引（review 自查）

启动后用户输入 → **preprocess_mentions（去 @ 引号 / UNC / skill prescan）→ ltm triage 异步 → prescan skill 立刻激活 → INTENT 流式分类 → task 路径锁内 PLAN_MODIFY 流式（prompt 内含 loop/failed-tail 顾问）→ activate_skills/tools → replace_post_current → optional interrupt** → agent 拉 item → **set 三个 static block → item_loop**：检查 interrupt → 收割后台 + compact → reminder + stagnation LTM refresh（可选）→ **think_streaming（流式 dispatch，写编辑同路径强制串行）**→ PTL 6 级恢复 → 完成判定 / 工具结果记录 / advisor 更新 → mark_done → planner_trigger → 后台 planner 锁内再次 PLAN_MODIFY → 任务收尾 → **B1 验证门 5-verdict（PASS/TRIVIAL/EXTEND/VALIDATE/ACCEPT）** → emit completion reply or inject acceptance_* item。

每段都能定位到具体文件：函数索引在 §18，inject 路径在 §11，schema 在 §2 与 §3，prompt block 在 §5.7 / §6.2 / §8.3 / §10.2 system 段。

---

## 21. 外部接入：Electron Bridge（`bridge_main.py` + `src/bridge/stdio_bridge.py`）

> 范围：本节描述 V2 控制器如何被 Electron 渲染器驱动。`src/controller_v2/` 自身不依赖任何桥代码；桥单向消费 V2 公开 API。Bridge session 全程持有 *一个* `FlowControllerV2`，每条用户消息都走 `await flow.on_user_message(text)` 并立即返回 receptionist 回复。后台 agent / planner 工作发生在 flow 自己的常驻 task 里，事件经 IM delegate 流出。

### 21.1 `bridge_main.py` 启动序列

入口脚本，由 Electron `child_process.spawn` 拉起。只调 `stdio_bridge` 模块级符号。关键步骤（`bridge_main.py`）：

1. **fd 重定向**：dup fd 0/1 到私有 fd（写入 env `HANDQ_BRIDGE_STDOUT_FD` / `HANDQ_BRIDGE_STDIN_FD`），把 sys.stdin 接 `/dev/null`、sys.stdout 接 fd 2。理由：任何调 `print()` 或 `sys.stdin.readline()` 的代码不能污染 IPC 通道。
2. **boot_progress 发射器**：极小的 `_emit_boot_progress(phase, **fields)`——直接 `os.write(_real_stdout_fd, json+"\n")`，零导入依赖，能在重导入之前就给渲染器发 "Starting…" 帧。
3. **配置解析**：`HANDQ_CONFIG` env > `%USERPROFILE%\HandQ\handq_config.yaml` > `<install_dir>\handq_config.yaml`。首次启动会把 ship 默认拷到用户目录；版本升级时按 `_PRESERVE_PATHS` 做 PRESERVE/OVERRIDE 合并 + 单滚动备份。
4. **日志树**：`<USERPROFILE>\HandQ\logs\<TS>\handq-bridge.log`（保留最近 30 个 launch 目录），`.dia/internal-trace.log`（隐藏属性，跨 launch 保留 LTM/personality/scheduler trace）。
5. **`_timed_import` 5 模块**：`stdio_bridge`、`SkillRegistry`、`LongTermMemory`、`PersonalityMonitor`、`Scheduler`。每个 import 发一条 `boot_progress` 让渲染器显示进度。
6. **`_run_with_long_term_memory()`**：异步初始化 LTM、PersonalityMonitor、Scheduler；调度器的 dispatch 闭包指向 `stdio_bridge.dispatch_scheduled_task`；最后 `await stdio_bridge.run()` 进入主循环。

`stdio_bridge` 模块在 import 时就会构造 `_active_bridge` slot，scheduler 在 `dispatch_scheduled_task(task)` 时通过它找到当前 `StdioBridge` 实例并调 `accept_scheduled_task(task)`（详见 §21.2.5）。

### 21.2 `stdio_bridge.py` 三段

#### 21.2.1 `_StdioUI` —— V2 `UIDelegate` 实现 + 桥侧 confirmation 注册表

类位置：`src/bridge/stdio_bridge.py` `_StdioUI`。这是 V2 IM 的 delegate 端实现。每个方法都把调用串行化为一条 JSON 行写到 IPC stdout。

**生成代际锚（generation tag）**：构造时捕获 `_generation: int`，每条 envelope 都会被 `_emit(obj, gen=...)` 加上 `gen` 字段。`_do_new_session` 在拆掉旧 flow 之前 bump 代际并构造新 `_StdioUI`——旧 `_StdioUI` 仍被旧 IM 引用着继续以 *旧* 代际发事件，而渲染器侧的水位线（generation watermark）会丢弃所有 gen < 当前 gen 的 envelope。这就是新会话不被旧 flow 残音污染的根本机制。

**Confirmation 注册表**：

```python
self._pending: Dict[str, asyncio.Future[str]] = {}
self._pending_lock = threading.Lock()
self._loop: Optional[asyncio.AbstractEventLoop] = None     # 由 _ensure_flow 注入
```

异步流：

1. agent 执行触发 V2 IM 的 `await im.request_risk_confirmation(description)` → IM 通过 `_await_delegate` 调到 `_StdioUI.request_risk_confirmation(description)`。
2. `_StdioUI._await_user_response(kind, payload, prompt_id)` 在 loop 上 `loop.create_future()`，把 future 注册到 `self._pending[prompt_id]`，发出 `{"type":"status","kind":"risk_confirmation","id":prompt_id,...}` envelope，然后 `await fut`。
3. 用户在渲染器里点了 yes/no/text → 渲染器回 `{"type":"user_input","kind":"confirmation","id":prompt_id,"answer":"..."}`。
4. **stdin reader 线程的 fast path**（`_stdin_reader` 内）：识别到 `kind=confirmation` 直接调 `self._ui.deliver_confirmation_response(prompt_id, answer)`——*不* 经过 asyncio 收件箱（避免 future 等待方阻塞了 loop 时还要靠它来 dispatch 答案的死锁）。
5. `deliver_confirmation_response` 在 `self._loop.call_soon_threadsafe(fut.set_result, answer)` 把 future 拍醒，await 端拿到 raw 字符串。
6. `_resolve_confirmation(answer)` 把字符串映射到 `UserConfirmation`：`yes/y/ok/approve` → `yes()`；`no/n/reject/deny` → `no()`；空 → `no()`；`risk:<text>` → `risk_guidance(text)`；其他非空 → `with_message(text)`。

V2 UIDelegate Protocol 实现的方法（V2 IM 直接 forward）：
- `display_error` / `show_state_changed` / `show_inline_event`
- `notify_decision_made(iteration, reasoning, token_count)`
- `notify_tool_execution_started(iteration, tool_name, params, output)`
- `async request_risk_confirmation(description) → UserConfirmation`
- `async request_tool_confirmation(tool_name, params, hint) → UserConfirmation`
- `async request_secret_input(prompt) → str`
- `async request_user_text(prompt) → str`（`ask_human` 工具，非掩码输入）
- `show_receptionist_thinking()` / `clear_receptionist_thinking()`：receptionist「思考中」指示。由 `FlowControllerV2.on_user_message` bracket——入口 `notify_receptionist_thinking()`、`finally` `clear_receptionist_thinking()`。渲染器在首个 `reply_delta` 也会清掉 thinking bubble，所以 streaming 路径下 `finally` 这次 clear 是幂等 no-op；它只在 `response_to_user` 全程静默（如纯后台 re-plan）时兜底，避免气泡常驻。
- `stream_receptionist_reply_chunk(text)` / `seal_receptionist_reply()`：planner `response_to_user` 流式分片 → `reply_delta` / `reply_done`。

**活动条状态词汇**（`show_state_changed` 的 `state` 取值闭集）：这些字符串经 IM `notify_state_changed` → delegate `show_state_changed` → 桥 `_emit({type:"status", kind:"state_changed", state})` → 渲染器 `state_changed` 分支，驱动活动条的工作动画与 pill。取值只有四个，分别由 orchestrator 与 agent 推出：

| `state` | 推送方 | 语义 | 渲染器效果 |
|---------|--------|------|-----------|
| `planning` | `Orchestrator._notify_state("planning")`（编排器准备/修订检查清单时，user-message 与 post-item 后台循环都会触发） | 规划中 | `setWorking('designing…')` |
| `thinking` | `PersistentAgent.notify_state_changed("thinking")`（agent 打开 LLM 流，推理 + 决定工具时） | 思考中 | `setWorking('thinking…')` |
| `executing` | `PersistentAgent.notify_state_changed("executing")`（agent 派发工具 / think-stream 之间） | 执行中 | `setWorking('working…')` |
| `idle` | `Orchestrator._notify_state("idle")`（`_emit_completion_reply` 里终态裁决后、最终回复发出前） | 落定 | `clearWorking()` + `setPill('idle')` |

`planning` / `thinking` / `executing` 是三个“在干活”相位 → 动画 label（与 receptionist 的 “thinking…” 一致）；`idle` 清掉工作动画并把 pill 复位。三相位互相覆盖：planner 的 `planning` 会被 agent 随后的 `thinking` / `executing` 顶掉，task 结束再由 `idle` 收尾，所以活动条不会卡在 “designing…”。渲染器对未列出的 `state` 兜底为 `setPill(evt.state)` 原样显示——属于不应发生的状态，仅作防御。

**非 Protocol forwarder 方法**（V2 IM 通过 `_ui_call(method_name, ...)` 按字符串名字 dispatch；missing 时静默跳过；不在 UIDelegate Protocol 内）：
- `notify_desktop_takeover_started(reason)` / `notify_desktop_takeover_ended(reason)`：来自 `desktop_tool._start_takeover/_end_takeover`（详见 §21.3）。`notify_desktop_takeover_started` 副作用是 `personality_monitor.pause()`，`_ended` 配 `resume()`，避免 OCR 与 agent 鼠键事件互染。
- `notify_session_event(event_name, data)`：交互式 shell session 生命周期（opened / data / input / exec_done / closed）。`session_tool` 在每个生命周期点经 `session._im` 调用；IM forwarder → bridge `_StdioUI.notify_session_event` → `session_event` envelope → 渲染器 session-monitor 面板。

> 说明：`show_receptionist_thinking` / `clear_receptionist_thinking` 与 `notify_session_event` 均已接通。早期的 `show_metrics_report` 预留槽连同 `metrics_collector.py` 已整体移除——V2 不再做 per-task/per-step 指标聚合（无 confidence、无离散 replan 概念，token 由 ExecutionRecorder 逐 ITER 记录）。

`tool_confirmation` 的 desktop 特例：当 `tool_name == "desktop"` 时，envelope 会带 `scope: "task"` 与一段说明文，让渲染器以 task-scope 样式渲染（一次确认覆盖整个 task 的所有 desktop 动作，配合 `desktop_tool.is_task_approved()`）；其他工具走 per-call 模板，附带截断的 params 预览。

#### 21.2.2 `StdioBridge` —— V2 控制器生命周期

`StdioBridge` 类位置：`src/bridge/stdio_bridge.py` `StdioBridge`。状态字段：

```python
self._flow: Optional[FlowControllerV2] = None       # 持续整个 session
self._services: List[AnthropicStreamingService] = []  # 用于 shutdown 时 close httpx pool
self._generation: int = 0
self._ui = _StdioUI(self._generation)
self._shutdown_requested: bool = False
```

`_flow` 持续整个 session（不需要外面包一层 task）；IM 由 flow 持有；调度模型简化（详见 §21.2.5）。

#### 21.2.3 `_ensure_flow` —— 懒构造 FlowControllerV2

首条 `request` 触发，`StdioBridge._ensure_flow`：

1. 读 `handq_config.yaml`、解析 roles。
2. 用 `_allocate_session_dir(goal)` 在 `%USERPROFILE%\HandQ\History\<TS>-<slug>\` 下建 session 目录。所有 agent 写出的 artifact 都落到这。
3. 初始化 HandQ engine logger 落到 session_dir/`handq-engine.log`。
4. **构造 LLM service 数组**（`FlowControllerV2` 接受一个扁平 `llm_services: List[LLMService]`）：
   - 共享池：`agent / receptionist / from_data` 三 role 的模型用 `max_retries=3`，按模型名去重得 `svc_map`。
   - 独立 planner 池：`planner` role 的模型每个独立构造 `max_retries=50` 的实例（planner 调用频次低、单次容忍更长重试链）。
   - **合并到一个有序 list**：`agent_services + planner_services + receptionist_services + from_data_services`，按 `id(svc)` 去重。顺序决定 V2 fallback chain 优先级——用户偏好的 agent 模型最先尝试。
5. **构造 FlowControllerV2**（`flow_controller.py` 的构造器）：
   ```python
   self._flow = FlowControllerV2(
       llm_services=consolidated_services,
       working_directory=None,
       storage_directory=str(session_dir),
       config_path=str(self.config_path),
       on_reply_to_user=self._on_receptionist_reply,
   )
   ```
6. **绑定 delegate**：`self._flow.interaction_manager.set_delegate(self._ui)` —— 把桥的 `_StdioUI` 接到 flow 持有的 V2 IM 上。所有 `notify_*` / `request_*` 自此流到桥。
7. **抓取 event loop ref**：`self._ui._loop = asyncio.get_running_loop()` —— 让 stdin 线程的 confirmation 答复可以 thread-safe 地 set future。
8. **接 `llm_pool` 通知**：`set_fallback_notifier` / `set_network_event_notifier` 把 `llm_fallback` / `network_down` / `network_waiting` / `network_restored` envelope 推到渲染器。
9. 每个 service 的 `on_server_error = _on_llm_server_error`（emit `llm_server_error` envelope）。

工具的 IM ref 在 `flow.start()` 内通过 SessionContext 注入（详见 §22）；bridge 不再单独 wire 工具侧 IM。

#### 21.2.4 入站 IPC dispatcher

`StdioBridge._handle(msg)` 路由表（消息类型 → 行为）：

| 类型 | 行为 |
|------|------|
| `config_get` / `config_set` | 直接 yaml 读写；`config_set` 会 diff `personalization.git_hook_repos` 并同步安装/卸载 post-commit hook（保 `_HOOK_MARKER` 标识，不覆盖用户自己的 hook） |
| `request` | `_ensure_flow` → 若 `not flow.started` 则 `await flow.start()` → `reply = await flow.on_user_message(goal)` → emit `final` envelope `{"reply":reply,"ok":true}`；后台 agent / planner 工作继续在 flow 内部 task 里跑，事件经 IM 流出 |
| `user_input.kind=message` | `await self._flow.on_user_message(text)`（不 emit final，因为这是会话续接） |
| `user_input.kind=confirmation` | 通常已被 stdin reader fast path 拦截；若到了这里 `self._ui.deliver_confirmation_response(prompt_id, answer)` 兜底 |
| `user_input.kind=desktop_takeover_revoked` | 调 `desktop_tool.revoke_takeover()` 翻 `_task_user_rescinded` 标志 |
| `new_session` | `_do_new_session`（详见 §21.2.6） |
| `shutdown` | `_do_shutdown` |
| `ltm_*` / `personality_*` / `cron_*` | 直接转给 LTM / PersonalityMonitor / Scheduler，与 V2 控制器无关 |

**关键点**：`request` 处理是 *await* 而不是 fire-and-forget。`on_user_message` 返回的是 receptionist 在当前用户消息上的同步回答（Stage 1 INTENT + 可选 Stage 2 PLAN 的 `response_to_user`）；后台 planner / agent 循环异步进行，结果通过 delegate 事件流出。

#### 21.2.5 `accept_scheduled_task` —— V2 调度派发模型

`StdioBridge.accept_scheduled_task`。Scheduler 的 dispatch 闭包通过模块级 `dispatch_scheduled_task(task) → await _active_bridge.accept_scheduled_task(task)` 调到这里。

V2 模型：scheduled fire = just-another-user-message。流程：

1. shutdown 中 → `return False`（让 scheduler 推迟）。
2. emit `scheduled_task_started` envelope（让渲染器显示 toast）。
3. `_ensure_flow` + `await flow.start()`（如未启动）。
4. `goal_text = task.dispatch_prompt or task.prompt`（`dispatch_prompt` 是 scheduler.inferer 把相对时间表述剥掉后的 agent-facing 版本）。
5. **`reply = await self._flow.on_user_message(goal_text)`** → emit `final` envelope `{"reply":..., "ok":true, "scheduled":true}`。
6. `await scheduler.notify_task_finished(task.id, ok=True/False, error=...)`，然后 `scheduler._wakeup.set()` 让其他 PENDING 任务尽快 re-scan。
7. return True（已派发）。

**`notify_task_finished(ok)` 的语义**：表示 "派发干净（receptionist 给了回复，没抛异常）"。Persistent flow 没有 per-task 完成信号——planner / agent 会持续跑直到下一条用户消息打断。如果未来需要更细粒度的 "scheduled task 完成度" 追踪，应当 hook 到 `SharedCheckList.on_item_done` 在该次派发产生的 item 全部完成时触发。

#### 21.2.6 `_do_new_session` —— 会话切换

`StdioBridge._do_new_session`。用户在渲染器点 New 时触发。流程：

1. **bump generation**：`old_gen → new_gen`；构造 `new_ui = _StdioUI(new_gen)`；抓 loop ref。
2. **快照 + 清空**：`flow = self._flow; services = self._services; self._flow = None; self._services = []`。任何接下来从 *旧* flow 里 emit 的 envelope 仍能写到 stdout（流是进程级），但都打 *旧* 代际，渲染器会丢。
3. **`await flow.destroy()`**（带 2.0s timeout）—— 一行收口：
   - 触发 `checklist._interrupt_event` 唤醒 shell/session 工具的 `asyncio.wait([communicate, interrupt])` race，让子进程被 kill。
   - 取消 agent + planner 两个 asyncio task。两个 loop 都 catch `CancelledError` 然后 break，协作式退出很快。
   - `await ctx.close()` 关闭浏览器、interactive shell、SSH 池、screenshot stores、清掉 file_state / desktop_state（详见 §22）。
4. **drain HTTP pool**：每个 `AnthropicStreamingService.close()` 都用 `wait_for(timeout=2.0)` 包，超时记 warning 不阻塞。
5. **替换 UI**：`self._ui = new_ui`。下一次 `_ensure_flow` 会构造新 flow，给新 flow 的 IM `set_delegate(new_ui)`。
6. emit `final` envelope。

bridge 不再需要逐个 flush 工具池或 detach IM——这些都由 `flow.destroy()` 接管。新增工具自带 cleanup 不用回头改 bridge。

#### 21.2.7 `_do_shutdown`

`StdioBridge._do_shutdown`。流程：`await flow.destroy()`（带 2.5s timeout，触发 interrupt + 取消 task + 关 ctx）→ 顺序 `await svc.close()` 每个 service。最后 emit `final` envelope `{"shutdown":"ok"}`。

#### 21.2.8 渲染器事件词汇表（envelope `kind`）

26 种事件类型，按来源分组：

| 来源 | `kind` 列表 |
|------|-------------|
| V2 UIDelegate Protocol | `state_changed` · `inline_event` · `decision_made` · `tool_execution_started` · `risk_confirmation` · `tool_confirmation` · `secret_input` · `ask_human` · `receptionist_thinking_on/off` · `reply_delta` · `reply_done` |
| V2 非 Protocol forwarder | `desktop_takeover_started` · `desktop_takeover_ended` · `session_event` |
| 桥侧元事件 | `boot_progress` · `bridge_exit` · `reply`（来自 `_on_receptionist_reply` 回调） · `progress` · `step_started` / `step_completed` / `step_confidence` / `task_completed`（语义槽，向前兼容预留） · `scheduled_task_started` |
| llm_pool | `llm_server_error` · `llm_fallback` · `network_down` · `network_waiting` · `network_restored` |

非 envelope 类型：`final`（每条 `request` 终结）、`error`、`status`（包络容器）、`token_stream`（保留未用）。

### 21.3 工具侧 IM 注入

`desktop_tool` / `browser_tool` 需要直接对 IM 发事件 / 请求确认（takeover 始末通知、attach_browser Chrome 重启确认、request_user_login 登录请求）。这些访问通过 `SessionContext` 注入：`flow.start()` 构造 ctx 时把 `self.interaction_manager` 装进 `ctx.desktop_state` 和工具构造路径，每个工具的 `__init__(ctx=ctx)` 自己从 ctx 取 IM ref。详见 §22.4。

### 21.4 文件级函数索引（桥）

#### `bridge_main.py`

| 名字 | 用途 |
|------|------|
| `_emit_boot_progress` | 零依赖 stdout JSON 行写入器，给渲染器看 boot 进度 |
| `_resolve_config_path` / `_ensure_user_config_present` / `_merge_user_config_with_seed` | 配置三层解析 + 首次拷贝 + 升级合并 |
| `_user_handq_root` / `_user_log_root` / `_prune_old_log_dirs` | 用户根目录与日志目录管理 |
| `_timed_import` | 包计时 + boot_progress 的 import wrapper |
| `_run_with_long_term_memory` | 主入口：初始化 LTM/PersonalityMonitor/Scheduler 后调 `stdio_bridge.run()` |

#### `src/bridge/stdio_bridge.py`

| 名字 | 用途 |
|------|------|
| `_emit` | 写一行 JSON 到 IPC stdout，附 `gen` 戳 |
| `_StdioUI.deliver_confirmation_response` | stdin reader 调，把 future 拍醒 |
| `_StdioUI.{display_error,show_*,notify_*}` | UIDelegate 实现，每个发一条 envelope |
| `_StdioUI._await_user_response` | 注册 future + 发 envelope + await，confirmation 主循环 |
| `_StdioUI.{request_risk,request_tool,request_secret}_confirmation` | 异步确认 / 输入入口 |
| `_StdioUI._resolve_confirmation` | 字符串答复 → `UserConfirmation` |
| `StdioBridge.__init__` | 构造 `_StdioUI`、绑定 `_active_bridge` slot |
| `_stdin_reader` | 守护线程：读 stdin JSON、confirmation 走 fast path、其他丢 inbox |
| `_handle` | 主 dispatcher（按 `type` 分支） |
| `_ensure_flow` | 懒构造 `FlowControllerV2`、绑定 delegate |
| `_on_receptionist_reply` | `flow.on_reply_to_user` 回调 → emit `kind=reply` envelope |
| `accept_scheduled_task` | scheduler 派发入口（派发即完成通知） |
| `_do_new_session` | 切换会话：bump gen、`await flow.destroy()`、关 service |
| `_do_shutdown` | 进程退出：`await flow.destroy()`、关 service |
| `dispatch_scheduled_task`（模块级）| scheduler 拿到的入口闭包，转给 `_active_bridge.accept_scheduled_task` |
| `run`（模块级）| 由 `bridge_main.py` 调用：构造 `StdioBridge` + 跑主循环 |

---

## 22. SessionContext —— 每会话资源容器（`src/controller_v2/session_context.py`）

> 把所有 *session 级* 状态（IM、interrupt event、文件读痕、SSH 连接池、浏览器 session、interactive shell 注册表、desktop 状态机、execution recorder）统一放在一个 per-session 对象上。`flow.destroy()` 调 `await ctx.close()`,bridge 不需要知道有哪些工具池存在;新增工具自带 cleanup 不用回头改 bridge。

### 22.1 设计原则

session 是一个 scope，所有 session 级状态都 own 在这个 scope 里，而不是散在进程里。`InteractionManager` 由 `FlowControllerV2` 持有；工具池（FileState、SSH 连接池、Playwright session、interactive shell 注册表、desktop 状态机）由 `SessionContext` 持有；checklist 上的 `_interrupt_event` 通过 ctx 的 forwarding property 暴露给工具。一次 `flow.destroy()` 把所有这些资源一并释放。

### 22.2 SessionContext 结构

```python
@dataclass
class SessionContext:
    # 标识 / 配置
    working_directory: Optional[str]
    storage_directory: str
    config_manager: ConfigManager

    # UI 总线
    interaction_manager: InteractionManager

    # 工具池 / 状态(per-session 实例)
    file_state: FileState                       # tools/file_state.py
    ssh_pool: SshConnectionPool                 # tools/ssh_tool.py
    browser_session: BrowserSessionHolder       # tools/browser_tool.py
    session_registry: SessionRegistry           # tools/session_tool.py
    desktop_state: DesktopState                 # tools/desktop_tool.py

    # 执行记录器(per-session)
    execution_recorder: Optional[ExecutionRecorder] = None

    # interrupt event 通过 checklist 转发
    @property
    def interrupt_event(self) -> asyncio.Event:
        return self._checklist._interrupt_event   # 单一来源

    async def close(self) -> None: ...   # 见 §22.5
```

### 22.3 构造路径

```
FlowControllerV2.__init__(...)
    [无 ctx,只 store params]
    ↓
await flow.start()
    1. 构造 SharedCheckList()
    2. 构造 SessionContext(...)        ← checklist 已存在,可以转发 interrupt_event
       └─ 内含 FileState() / SshConnectionPool() / BrowserSessionHolder() /
          SessionRegistry() / DesktopState(im=self.interaction_manager)
    3. self._ctx = ctx
    4. 构造 Orchestrator(不依赖 ctx)
    5. 构造 PersistentAgent(ctx=self._ctx)
        └─ ToolRegistry.create_all_tool_instances(ctx=ctx)
            └─ 每个工具的 __init__(ctx=ctx) → 工具读取 ctx.X
    6. 启动两个 run_loop task
```

### 22.4 工具构造点的 DI

`BaseTool.__init__(name, ctx=None)` 增加可选 ctx 字段。`ToolRegistry.create_all_tool_instances(ctx, ...)` 把 ctx 透传给每个 tool 的 `__init__(ctx=ctx)`。

每个工具拿 ctx 做的事:

| 工具 | ctx 用途 |
|------|---------|
| `ShellTool` | `self.interrupt_event = ctx.interrupt_event` |
| `InteractiveSessionTool` | `self.interrupt_event = ctx.interrupt_event`（registry 接 ctx 是 follow-up） |
| `StatelessSSHTool` | 接 ctx（pool 完整切换是 follow-up） |
| `BrowserTool` | 接 ctx（holder + im 完整切换是 follow-up） |
| `DesktopTool` | `self.state = ctx.desktop_state if ctx else DesktopState()` —— 内部所有 takeover / snapshot cache / im 访问全部走 `self.state.X` |
| `ReadTool` / `WriteTool` / `EditTool` | `self.ctx.file_state if self.ctx else FileState.get_instance()` |
| 其他(grep/glob/notebook_edit/web_search/email/teams/remote_handq/ask_human) | 接受 ctx 但目前不使用 |

`PersistentAgent.__init__(ctx=ctx)` 在收到 ctx 时跳过两段冗余初始化：
- `tool.interrupt_event = checklist._interrupt_event` 的 post-injection（shell/session 工具构造时已从 ctx 拿到）
- `FileState.reset_for_session()` 全局 reset（per-ctx 实例不需要）

`PersistentAgent._handle_tools_added(delta)` 也透传 ctx:`ToolRegistry.create_all_tool_instances(ctx=self._ctx, extra_tool_names=delta)`。

### 22.5 `SessionContext.close()` —— 销毁链

```python
async def close(self) -> None:
    if self._closed: return
    self._closed = True
    coros = [
        # 全异步: holder.close 是 coroutine
        self._safe_close("browser_session_holder", self.browser_session.close()),
        self._safe_close("session_registry",       self.session_registry.close_all()),
        # threading-Lock 的同步 close 用 to_thread 桥到 worker thread
        self._safe_close("ssh_pool",       asyncio.to_thread(self.ssh_pool.close)),
        self._safe_close("desktop_state",  asyncio.to_thread(self.desktop_state.close)),
        # file_state 不持 OS 资源,丢引用即可,无 close()
    ]
    await asyncio.gather(*coros, return_exceptions=True)
```

设计要点:
- **forwarding `interrupt_event`**:checklist own 这个 event(planner 也用它做 mid-task interrupt),ctx 只是出口。
- **`asyncio.to_thread` 桥**:SshConnectionPool / DesktopState.close 是同步的(threading.Lock,desktop 锁等),用线程跑避免阻塞 loop。
- **`return_exceptions=True`**:任一资源关失败不应阻塞别的。
- **顺序无关**:resources 互不依赖,并发 close。
- **holder.close 是唯一 teardown**:`browser/ssh/session/desktop/file_state` 工具的内部 callsite 全部经 `ctx` 拿 per-session holder,模块级 globals + `flush_*_pool` 已删。ctx.close 只调 holder.close(详见 §22.9)。

### 22.6 `flow.destroy()` —— async 化 + ctx 收口

```python
async def destroy(self) -> None:
    self._signal_interrupt()                # 触发 checklist._interrupt_event
                                             # → 唤醒 shell/session 工具的 asyncio.wait
    self._cancel_loops()                     # 取消两个 run_loop asyncio task
    if self._ctx is not None:
        await self._ctx.close()              # 关所有 session 级资源
        self._ctx = None
    self._started = False
```

`cancel_all_tasks()` 仍是同步(用于无法 await 的调用方),内部用 `asyncio.create_task(self._ctx.close())` fire-and-forget 的方式释放资源。

### 22.7 Bridge 简化

`_do_new_session` 整段塌缩成一个 `await flow.destroy()`:

```python
async def _do_new_session(self, msg_id, *, _suppress_final=False):
    self._generation += 1
    new_ui = _StdioUI(self._generation)
    new_ui._loop = asyncio.get_running_loop()
    flow, services = self._flow, self._services
    self._flow, self._services = None, []
    self._ui = new_ui
    if flow is not None:
        await asyncio.wait_for(flow.destroy(), timeout=2.0)   # 一行 + 兜底
    for svc in services:
        try: await asyncio.wait_for(svc.close(), 2.0)
        except Exception: pass
    if not _suppress_final:
        _emit({"type": "final", ...})
```

`_do_shutdown` 同样精简（`await flow.destroy()` 带 2.5s timeout + service close 串行）。`user_input.kind=desktop_takeover_revoked` 走 `flow._ctx.desktop_state.revoke_takeover()`。

### 22.8 收益总结

1. **Bridge 与工具解耦**:bridge 的 `_do_new_session` / `_do_shutdown` 只调 `flow.destroy()`;新增工具自己在 SessionContext.close 里收尾,bridge 零改动。
2. **session 之间硬隔离**:同进程跑两个 session 互不干扰;每个 session 起来时 file_state / desktop_state / ssh_pool / browser_session / session_registry 都是新鲜实例。
3. **工具依赖显式化**:每个工具通过 ctx 拿到自己的依赖,不再有隐藏 setter 或模块级 slot。
4. **interrupt 信号统一**:`flow._signal_interrupt()` 在 destroy/cancel 之前 trip checklist 的 `_interrupt_event`,shell/session 工具的 `asyncio.wait([communicate, interrupt])` race 立刻醒过来释放子进程。

### 22.9 当前状态边界（follow-up）

- **browser_tool / ssh_tool / session_tool 内部完全切到 ctx**:目前这三个工具的内部 callsite(launch_browser、_connect、_action_open 等)仍写模块级 `_session` / `_conn_pool` / `_registry`,SessionContext.close 通过 `flush_*_pool` 桥过去。完整 DI 后这些模块级 globals + flush 函数可删。
- **email_tool 的 Outlook COM 状态**:进程级初始化 ~2-3s,跨 session 复用是合理需求,不下放。
- **`cancellation.py` thread-local**:per-invocation 语义,不属于 session 级。
- **`ToolRegistry._tools` 注册表**:immutable,进程级合理。
- **`desktop_tool._DPI_INITIALISED`** / **`_desktop_lock`**:Windows DPI / 鼠标键盘互斥是 OS 进程级,per-process 是对的。

### 22.10 文件级函数索引

| 名字 | 用途 |
|------|------|
| `controller_v2/session_context.py:SessionContext` | per-session 资源容器(dataclass) |
| `controller_v2/session_context.py:SessionContext.close` | async 销毁所有 holder + 调遗留 flush |
| `controller_v2/session_context.py:SessionContext.interrupt_event` | property,转发自 checklist |
| `controller_v2/flow_controller.py:FlowControllerV2._signal_interrupt` | 触发 checklist 的 interrupt event(已在 destroy/cancel 之前调) |
| `controller_v2/flow_controller.py:FlowControllerV2.destroy` | **async** —— `_signal_interrupt + _cancel_loops + await ctx.close` |
| `tools/ssh_tool.py:SshConnectionPool` | 类化 SSH 连接池 + 速率限制 + OS cache(threading.Lock) |
| `tools/browser_tool.py:BrowserSessionHolder` | 类化 BrowserSession + asyncio.Lock + ScreenshotStore;`async close()` mode-aware |
| `tools/session_tool.py:SessionRegistry.close_all` | per-instance kill_all + 一句话日志 |
| `tools/desktop_tool.py:DesktopState` | 类化 takeover 状态机 + IM ref + snapshot cache + ocr prewarm + ScreenshotStore;附 `invalidate_on_state_change` / `prewarm_ocr_if_needed` 方法 |
| `tools/base_tool.py:BaseTool.__init__` | 接 `ctx: Optional[SessionContext] = None` |
| `tools/tool_registry.py:ToolRegistry.create_all_tool_instances` | 接 `ctx` 参数,透传到每个 `tool.__init__(ctx=ctx)` |
| `tools/desktop_tool.py:DesktopTool.__init__` | `self.state = ctx.desktop_state if ctx else DesktopState()` |
