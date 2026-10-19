# 会话管理设计文档

> 定位：**会话状态层（多轮记忆 + 上下文分区 + 持久化）** · 技术栈：**Python** · 状态：**已实现 M1–M5**
> 依赖：[harness/agent](../harness/agent/README.md)（调用）、[harness/llm](../harness/llm/README.md)（消息类型）、[harness/tool](../harness/tool/README.md)（Context）
> 相关：[Agent Loop 设计](agent-loop-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

Agent Loop 被刻意设计成**无状态纯函数**（`run(messages, ctx) -> RunResult`，`RunResult.messages` 交还宿主）。
**Session 就是接住这份状态的宿主层**：保存多轮消息、按阈值压缩上下文、持久化到磁盘。

### 非目标（本模块不做）

| 不做 | 归属 |
|---|---|
| 模型调用 | [harness/llm](../harness/llm/README.md) |
| 工具执行 | [harness/tool](../harness/tool/README.md) |
| 循环编排 / 停止条件 | [harness/agent](../harness/agent/README.md) |
| system prompt 重组 | [PromptBuilder](../harness/agent/prompt.py)（Session 只**固化**首次结果） |
| HTTP / 多租户 | 后续 FastAPI 层 |

---

## 2. 与 Agent Loop 的关系

```
用户消息 ─→ Session.append（落 active 区）
                ↓
        Session.build_messages()            ← 分区拼装：固定区 + 固定头 + 压缩区 + 活跃区
                ↓
        AgentLoop.run(messages, ctx)        ← 完全不动，仍是无状态
                ↓
        Session.record(result)              ← 只把「本轮新增」追加进 active 区
```

`SessionManager.run_turn()` 把上述四步包成一个调用（薄封装，不含任何循环/工具逻辑）。

---

## 3. 上下文分区模型（核心）

消息序列切成**四段**，目的是让前缀尽可能长时间保持字节级不变：

```
┌───────────────────────────────────────────────┐
│ ① 固定区  system                                │  首次创建后**永不修改**
│   角色设定 + skill 目录 + 长期记忆                │  → 前缀缓存长期命中
├───────────────────────────────────────────────┤
│ ①′ 固定头  head（0 或若干条）                     │  首次压缩时**一次性固化**
│   开头若干条对话（keep_head 指定）                 │  → 之后永不压缩
├───────────────────────────────────────────────┤
│ ② 压缩区  summary（0 或 1 条）                   │  仅在触发压缩时**整体替换**
│   早期消息的摘要                                 │  → 替换是一次性代价
├───────────────────────────────────────────────┤
│ ③ 活跃区  active（最近若干轮完整消息）             │  **只追加，不修改**
│   user / assistant(tool_calls) / tool / ...     │  → 命中率最高的一段
└───────────────────────────────────────────────┘
```

```python
@dataclass
class Session:
    meta: SessionMeta
    system: list[Message]        # ① 固定区（通常 1 条）
    head: list[Message]          # ①′ 固定头（首次压缩时从 active 头部搬来）
    summary: Message | None      # ② 压缩区，None 表示尚未压缩
    active: list[Message]        # ③ 活跃区

    def build_messages(self) -> list[Message]:
        prefix = list(self.system) + list(self.head)
        if self.summary is not None:
            prefix.append(self.summary)
        return prefix + list(self.active)
```

> 关键：`build_messages()` 的输出必须是 `[固定区] + [固定头] + [压缩区] + [活跃区]` 的**前缀单调**序列——
> 新消息只出现在尾部，已发出的前缀不被改写。
> `head` 只在**首次压缩**时由 `adopt_head(keep_head)` 从 active 头部一次性搬入（之后为空操作），
> 因此它同样满足「固化后不改」。`keep_head=0` 时该段恒为空。

---

## 4. KV / 前缀缓存友好规则

这是本设计的**约束源**，其余设计都要服从它。

| 规则 | 理由 | 落地做法 |
|---|---|---|
| **system 字节级稳定** | 前缀任何一位变化都会击穿整段缓存 | ① system 只在建会话时生成一次并固化；② skill 目录**确定性排序**（`SkillRegistry.catalog()` 已按 name 排序）；③ **禁止**把时间戳、随机数、易变状态写进 system |
| **活跃区只追不改** | 追加不破坏前缀 | `append()` 只写尾部；**不做**历史中途插入/编辑 |
| **压缩整块替换** | 逐条挤压会导致每轮前缀都变，缓存全废 | 一次把「活跃区头部一大块」折叠成**一条** summary |
| **水位线（hysteresis）** | 防止每轮都触发压缩 | `compact_threshold`（触发）与 `keep_tail`（压缩后保留量）分离，一次压掉一大块 |
| **不在 system 里同步易变态** | 例如 todo 状态每轮变化 | 易变状态放活跃区尾部，或作为工具结果回喂 |
| **持久化 append-only** | 重放即恢复，历史不被重写 | JSONL 事件日志（见 §6） |

> 反例（要避免）：每轮都把「当前 todo 列表」刷新进 system prompt —— 前缀每轮全变，缓存命中率归零。

---

## 5. 数据契约

### 5.1 事件（append-only 的基本单位）

```python
@dataclass
class SessionEvent:
    type: str                    # meta | message | compact | usage
    data: dict[str, Any]
    ts: float

# message : {"message": <Message.to_dict()>}
# compact : {"summary": "<文本>", "dropped": <从 active 头部折叠掉的条数>}
# usage   : {"prompt_tokens": .., "completion_tokens": .., "total_tokens": .., "turns": <轮次>}
# meta    : {"title": "..."} / {"scopes": [...]} / {"head_count": <搬入固定头的条数>} 等元信息增量
```

> `head_count` 记的是**搬移的条数**而非消息快照。重放时执行与在线路径**同一段切片**
> （`head = active[:n]; active = active[n:]`），因此不会出现 head / active 重复计数。
>
> `usage` 事件与 `meta(turns=…)` 同流：`Session.record()` 每轮在累加 `turns` 之后补发一条，
> 携带该轮的 prompt / completion / total token。`replay()` 逐条累加进 `SessionMeta.usage_totals`；
> **旧日志没有 `usage` 事件 → 累计量保持 0**（向后兼容，不报错）。

### 5.2 会话元信息

```python
@dataclass
class SessionMeta:
    session_id: str
    created_at: float
    updated_at: float
    title: str | None = None
    turns: int = 0                          # 已完成轮次
    metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(   # 会话累计 token（跨轮累加）
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
```

> `usage_totals` 的键由模块常量 `USAGE_KEYS` 固定（`prompt_tokens` / `completion_tokens` / `total_tokens`），
> 并出现在 `SessionMeta.to_dict()` 里（列表接口 `/sessions` 随之带上）。

### 5.3 配置

```python
@dataclass
class SessionConfig:
    compact_threshold: int = 40      # 固定头 + 活跃区 的对话消息数超过它 → 触发压缩
    keep_head: int = 0               # 首次压缩时固化进固定头的条数；0 = 不保护
    keep_tail: int = 16              # 压缩后至少保留最近这么多条；0 = 不保护
    max_active_tokens: int | None = None   # 可选：按估算 token 触发（估算值 + 安全余量）
    enable_compaction: bool = True
    auto_title: bool = True          # 首轮问答结束后用 LLM 总结一个标题
    title_max_chars: int = 24        # 生成标题的字数上限
```

> `keep_head` / `keep_tail` 是**两端保护**：`keep_head` 保住对话开头（如最初的任务设定），
> `keep_tail` 保住最近若干条（模型工作内存）。系统提示不算对话消息，恒不参与压缩。
> `auto_title` / `title_max_chars` 控制**会话标题自动生成**（见 §8 末）。

### 5.4 Session API

```python
class Session:
    @property
    def id(self) -> str: ...
    def build_messages(self) -> list[Message]: ...          # §3 四段拼装
    def append(self, *messages: Message) -> list[SessionEvent]: ...   # 生成 message 事件
    def record(self, result: RunResult, sent: list[Message]) -> list[SessionEvent]: ...
        """把本轮新增消息追加进活跃区，并累加 turns 与用量。

        产出：message 事件（本轮新增）+ meta(turns=N) + usage(本轮 token)。
        实现细节：**不按长度切片**——Loop 可能在头部注入/合并 system（PromptBuilder 的行为），
        长度会错位。改为按 `sent` 的**最后一条**做对象身份定位（Loop 只追加，前缀改写只影响头部）。
        """
    def rename(self, title: str) -> list[SessionEvent]: ...
        """改标题：只产出一条 meta(title=…) 事件，消息序列不动。"""
    def set_metadata(self, **data: Any) -> list[SessionEvent]: ...
        """**合并式**更新 meta.metadata：产出一条 meta(metadata=…) 事件。

        控制台用它按会话保存「工作区」与「权限档位」（键见 harness/tool/permission.py）。
        合并而非替换：未提到的键保持原样，不会把创建时写入的其他 metadata 抹掉。
        空调用返回空列表（不产无意义的事件）。
        """
    def context(self, *, scopes=None, approval=None, ask=None, sandbox=None) -> Context: ...
        """产出带 session_id 的 Context（复用 tool 模块的 Context）。

        `sandbox` 是该会话的工作区策略（控制台按会话选的那个）；
        `None` = 沿用进程级默认沙箱（见 docs/sandbox-design.md §4.1/§6.1）。
        """
    def is_over_threshold(self, config: SessionConfig) -> bool: ...
    def adopt_head(self, keep_head: int) -> list[SessionEvent]: ...
        """首次压缩时把 active 头部 keep_head 条搬进固定头；已固化则空操作。
        产出 meta(head_count=N) 事件。"""
    def compact(self, summary: str, dropped: int) -> list[SessionEvent]: ...  # 生成 compact 事件

# 模块级公开函数
def estimate_tokens(messages: Iterable[Message]) -> int: ...
    """粗略估算 token 数（字符数 / 4），仅用于阈值判断与前端展示，不追求精确。"""
```

> `estimate_tokens()` 由模块级公开导出（`harness.session.estimate_tokens`），
> 服务层的会话详情 / 上下文占用面板即用它算各段 `tokens`。

> `record()` 用 `sent`（实际发给 Loop 的消息）做定位，避免 Session 与 Loop 之间共享可变状态。
> 摘要消息以 `role="system"`、带 `[此前对话摘要]` 前缀写入压缩区（实现见 `summary_message()`）。

---

## 6. 存储（append-only 事件日志）

```python
class SessionStore(Protocol):
    async def append_events(self, session_id: str, events: list[SessionEvent]) -> None: ...
    async def load_events(self, session_id: str) -> list[SessionEvent]: ...
    async def list_sessions(self) -> list[SessionMeta]: ...
    async def delete(self, session_id: str) -> None: ...
```

`Session = replay(events)` —— 加载即重放，天然 append-only、可审计、崩溃安全。

| 实现 | 说明 |
|---|---|
| `MemorySessionStore` | 进程内 dict，测试 / 临时会话 |
| `JsonlSessionStore` | 每会话一个 `<session_id>.jsonl`，**一行一事件，只追加**；无额外依赖、可读、可 `tail` |

JSONL 示例：

```jsonl
{"type":"meta","data":{"title":"解标任务"},"ts":1730000000.0}
{"type":"message","data":{"message":{"role":"user","content":"帮我看这个招标文件"}},"ts":...}
{"type":"message","data":{"message":{"role":"assistant","content":null,"tool_calls":[...]}},"ts":...}
{"type":"message","data":{"message":{"role":"tool","tool_call_id":"call_a","content":"..."}},"ts":...}
{"type":"compact","data":{"summary":"[已压缩 24 条早期消息] ...","dropped":24},"ts":...}
```

---

## 7. 压缩

**压缩器只有 `LLMCompressor` 一种**（早期版本有一个零成本的 `PlaceholderCompressor`，已删除）：
摘要质量直接决定长会话的可用性，而占位说明「丢了什么」讲不清楚，反而把上下文弄坏。
压缩需要可用的 provider，**没有 provider 时 `build_session` 直接报错**（见 §8）。

```python
class Compressor(Protocol):
    async def compress(self, messages: list[Message]) -> str: ...
```

| 实现 | 成本 | 说明 |
|---|---|---|
| `LLMCompressor(provider, model=None)` | 1 次模型调用 | 调 [harness/llm](../harness/llm/README.md) 生成摘要，信息保留最好；`compress_and_extract()` 可在同一次调用里顺带抽取长期记忆 |

触发流程（水位线）：

```
固定头 + 活跃区 条数 > compact_threshold
   ↓
events += session.adopt_head(keep_head)      # 首次压缩才真正搬移，之后空操作
   ↓
dropped = len(active) - keep_tail            # 一次压掉一大块
   ↓
summary = await compressor.compress(active[:dropped])
   ↓
events += session.compact(summary, dropped)  # active = active[dropped:]  前缀只在此刻变化一次
   ↓
commit(events)                               # 即使只固化了固定头也落库
```

> `dropped <= 0`（活跃区不足 `keep_tail`）时不做压缩，但 `adopt_head` 产生的固定头事件仍会落库。

自动压缩走 `SessionManager.maybe_compact(session)`，它**等价于 `compact(session, force=False)`**——
先判水位线，未触发直接返回 `[]`。服务层的**手动压缩**（`POST /sessions/{id}/compact`）用
`compact(session, force=True)` 跳过水位线：仍按「固化固定头 → 压掉 `len(active) - keep_tail`」执行，
因此 `enable_compaction=false` 时也能人工触发（响应里带 warning 说明）。

---

## 8. SessionManager 与 run_turn

```python
class SessionManager:
    def __init__(self, store: SessionStore, *, compressor: Compressor,
                 config: SessionConfig | None = None) -> None: ...

    async def create(self, *, session_id: str | None = None,
                     system: list[Message] | None = None, title: str | None = None) -> Session: ...
    async def open(self, session_id: str) -> Session: ...
    async def list(self) -> list[SessionMeta]: ...
    async def delete(self, session_id: str) -> None: ...

    async def run_turn(
        self,
        session: Session,
        user_text: str | Message,
        agent: AgentLoop,
        *,
        ctx: Context | None = None,
    ) -> RunResult:
        """追加用户消息 → 压缩（如需）→ agent.run → 落库本轮新增。"""
```

> `compressor` 是**必填**参数（压缩器只有 `LLMCompressor` 一种，不存在「无压缩器」的占位默认）。
> 组合根 [`build_session()`](../harness/config/build.py) 会先建 provider 再传进来；provider 缺失时抛 `ConfigError`。

`run_turn` 是**薄封装**：不含循环、不碰工具、不改 AgentLoop。等价于：

```python
await manager.commit(session, session.append(Message.user(text)))
await manager.maybe_compact(session)                 # 水位线触发（§7）
sent = session.build_messages()
result = await agent.run(sent, ctx=ctx or session.context())
await manager.commit(session, session.record(result, sent))
```

### 被打断 run 的部分历史落库（软打断 vs 硬取消）

`run_turn` 的实现**没有改动**，但**软打断**让它多了一条要说明的语义：

- **软打断是正常返回**：用户在 run 执行中插话，Loop 让本轮已执行的工具跑完后提前收尾，
  `AgentResult.stop_reason == "interrupted"`（**不是抛异常**）。因此 `session.record(result, sent)` 照常执行。
- **按身份定位增量**：`Session.record(result, sent)` 调 `_messages_after(result.messages, sent)`
  （[harness/session/schema.py](../harness/session/schema.py)），用 `sent[-1]` 的**对象身份**在尾部定位本轮新增，
  因此 Loop 追加的 assistant(tool_calls) 与 tool 消息**成对落库**——下一轮 `build_messages()` 拼出的会话依然合法，
  不会出现「有 tool_calls 没有 tool 结果」。
- **硬取消不落库**：`POST /runs/{id}/cancel` → `task.cancel()` 时 `agent.run` 抛 `CancelledError`，
  `record` **不执行**，本轮新增消息整批不落库（会话停在上一轮结束处，仍然合法）。这是既有行为。

> 两处措辞不要混：**软打断 = 正常返回 + 落库本轮增量**；**硬取消 = 异常中断 + 本轮增量整批丢弃**。

配套的一次改动：`server/interaction.py` 的 `InteractionRegistry` 由 `dict[str, Future]` 改为
`dict[str, _Pending]`（`_Pending(future, run_id, default)`）——`open(*, run_id=None, default=None)`
记录**归属 run** 与**兜底值**，新增 `resolve_for_run(run_id) -> list[str]`：软打断时用它把该 run 上
全部未决交互按各自 `default` 兑现，否则模型会卡在审批 / 提问上等满 `interaction_timeout_s`（默认 300s）。
`resolve` / `cancel_all` / `wait` 行为不变。

### 与 AgentLoop 的约定（缓存友好）

- **system 由 Session 固化**：建会话时把 `PromptBuilder` 的输出存进 `session.system`，
  之后每轮 `agent.run()` 直接拿到完整 system，**不再重新组装**。
  调用方应使用 `PromptBuilder()`（空配置）或提示 AgentLoop 不要重复注入。
- Session 只传 `messages` 与 `ctx`，不干预 Loop 内部。

### 会话标题自动生成（可选）

首轮问答结束后，服务层用一个**后台任务**（`server/runner.py` 的 `_auto_title`）总结一个短标题，
避免标题一直空着。触发与约束：

- **时机**：在 `run.finish(...)`（`done` 事件）**之后**调度，绝不阻塞 SSE 与落库。
- **只写一次**：仅对 `turns == 1` 且 `title` 为空的会话写；已有标题或非首轮直接跳过（不覆盖人工改名）。
- **生成器**：`TitleGenerator(provider, model=None, max_chars=24, timeout_s=8.0)`；
  它调一次模型（`temperature=0`），**异常 / 超时 / 空结果一律返回 `None`，绝不抛出**。
- **回退**：`generate()` 返回 `None` 时用 `fallback_title(user_text, max_chars=30)` 截断首条用户消息兜底。
- **开关**：由 `SessionConfig.auto_title` 控制（服务层在调度前检查）；字数上限 `title_max_chars` 与
  `TitleGenerator.max_chars` 一致。
- 落库走 `Session.rename(title)`（只产一条 `meta(title=…)` 事件）；任务登记在 `AppState.background`，
  配置重建 / 服务关闭时统一取消。

---

## 9. 目录结构

```
harness/session/
├─ README.md     # 模块文档
├─ __init__.py   # 公共导出
├─ schema.py     # SessionEvent / SessionMeta / SessionConfig / Session（含 replay）
├─ store.py      # SessionStore 协议 + MemorySessionStore
├─ jsonl.py      # JsonlSessionStore
├─ compact.py    # Compressor 协议 + LLMCompressor（唯一实现）
├─ manager.py    # SessionManager（create/open/list/delete/run_turn/maybe_compact/compact）
├─ title.py      # TitleGenerator（首轮自动标题）+ fallback_title（兜底截断）
└─ test/
```

---

## 10. 落地路线

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 契约 + 内存 | `schema.py`（四段布局 + replay）+ `MemorySessionStore` | ✅ 已完成 |
| M2 持久化 | `JsonlSessionStore`（事件追加 + 重放） | ✅ 已完成 |
| M3 压缩 | `compact.py` + 水位线触发 | ✅ 已完成 |
| M4 端到端 | `SessionManager.run_turn()` | ✅ 已完成 |
| M5 增强 | `LLMCompressor`（唯一压缩器）、按 token 估算触发（`max_active_tokens`）、两端保护（`keep_head` / `keep_tail`） | ✅ 已完成 |

实现见 [harness/session/README.md](../harness/session/README.md)；
测试：`python -m harness.session.test.test_session`（35/35，全离线）。

> `run_turn()` 依赖 M2 才能验证「重放一致」，故实际按 M1→M2→M3→M4 顺序落地。

---

## 11. 决策记录

| 议题 | 结论 |
|---|---|
| 与 AgentLoop 耦合 | 提供 `run_turn()` **薄封装**（不改 AgentLoop） |
| 持久化 | **内存 + JSONL**（append-only 事件日志，重放恢复） |
| 上下文策略 | **四段分区**：固定 system / 固定头 / 阈值压缩 / 追加活跃区，**KV 缓存友好**（见 §4） |
| 压缩实现 | **只有 `LLMCompressor`**（已删除零成本的 `PlaceholderCompressor`）；无 provider 时 `build_session` 报错 |
| 两端保护 | `keep_head` 固化对话开头进 `head`（首次压缩时搬移一次）；`keep_tail` 保留最近若干条；`0` = 不保护 |
| 加载模型 | 事件重放（`replay(events) -> Session`） |
| `record` 定位方式 | 按 `sent` **末条**对象身份定位（不用长度切片），兼容 Loop 头部注入/合并 system |
| 固定头持久化 | `meta(head_count=N)` 只记**搬移条数**，重放执行同一段切片（避免 head/active 重复计数） |
| 摘要消息形态 | `role="system"` + `[此前对话摘要]` 前缀，写入压缩区 |
| 用量累计 | 每轮补发一条 `usage` 事件（与 `meta(turns)` 同流），`replay` 累加进 `SessionMeta.usage_totals`；旧日志无该事件 → 保持 0 |
| 手动压缩 | `SessionManager.compact(session, force=True)` 跳过水位线（前端按钮触发）；`maybe_compact` = `compact(force=False)` |
| 改名 | `Session.rename(title)` 只产一条 `meta(title=…)` 事件，不触碰消息序列 |
| 会话设置 | `Session.set_metadata(**kv)` 只产一条 `meta(metadata=…)` 事件（**合并**语义，`replay` 做 `update`）；控制台的工作区 / 权限档位走它 |
| 工作区作用域 | 工作区与权限档位存会话 `metadata`（**每会话**），run 开始时由 `server/runner.py` 翻译成 `Context.sandbox` / `scopes` |
| 自动标题 | 首轮 `done` 之后的后台任务；只写「首轮且无标题」的会话，LLM 失败回退 `fallback_title`；开关 `SessionConfig.auto_title` |
| 软打断落库 | 软打断是正常返回（`stop_reason="interrupted"`），`record` 照常执行；`_messages_after` 按 `sent` 末条**对象身份**定位，保证 assistant(tool_calls) 与 tool 消息**成对落库** |
| 硬取消落库 | `task.cancel()` → `agent.run` 抛 `CancelledError` → `record` **不执行**，本轮新增整批不落库（停在上一轮结束处，仍合法） |
| 打断释放交互 | `InteractionRegistry.resolve_for_run(run_id)` 用各自 `default` 兑现该 run 上未决审批 / 提问，避免软打断后卡满 `interaction_timeout_s` |

### 仍待确认

- [ ] 阈值默认值（当前 `compact_threshold=40` / `keep_head=0` / `keep_tail=16`）是否合适？
- [x] 会话 `title` 是否自动生成？→ **已实现**：首轮结束用 `TitleGenerator` 调一次模型总结，
      失败回退 `fallback_title`（截断首条用户消息）；仍支持显式传入 / 手动改名（`Session.rename`）。
- [ ] 是否需要在后续补 SQLite（多会话查询/索引）？
