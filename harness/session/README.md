# session — 会话管理子模块

> 状态：**已实现（M1–M5）**。
> 全局设计见 [docs/session-management-design.md](../../docs/session-management-design.md)。

## 职责

Agent Loop 被刻意设计成**无状态纯函数**（`run(messages, ctx) -> RunResult`，`RunResult.messages` 交还宿主）。
**Session 就是接住这份状态的宿主层**：保存多轮消息、按阈值压缩上下文、持久化到磁盘。

核心目标是 **KV / 前缀缓存友好**——让消息序列的前缀尽可能长时间保持字节级不变。

## 边界

| 归属 | 内容 |
|---|---|
| **session 做** | 消息历史、多轮状态、上下文分区与压缩、持久化、按会话产出 `Context` |
| 不做 | 模型调用（[llm](../llm/README.md)）、工具执行（[tool](../tool/README.md)）、循环编排（[agent](../agent/README.md)）、system prompt 重组（PromptBuilder） |

## 上下文分区模型

消息切成四段，保证前缀单调（新内容只在尾部）：

```
① 固定区  system        首次创建后永不修改        → 前缀缓存长期命中
①′ 固定头 head          首次压缩时一次性固化        → 之后永不压缩
② 压缩区  summary       仅在触发压缩时整体替换      → 替换是一次性代价
③ 活跃区  active        只追加、不修改            → 命中率最高
```

`build_messages()` 输出 `[固定区] + [固定头] + [压缩区] + [活跃区]`。
`head` 由 `adopt_head(keep_head)` 在**首次压缩**时从 active 头部搬入；`keep_head=0` 时该段恒为空。

## KV 缓存友好规则（设计的约束源）

| 规则 | 理由 |
|---|---|
| system 字节级稳定 | 前缀变一位就击穿整段缓存；skill 目录需确定性排序，禁止写入时间戳/随机数 |
| 活跃区只追不改 | 追加不破坏前缀；不做历史中途插入/编辑 |
| 压缩整块替换 | 逐条挤压会让每轮前缀都变，缓存全废 |
| 水位线触发 | `compact_threshold`（触发）与 `keep_tail`（保留量）分离，一次压掉一大块 |
| 不在 system 里同步易变态 | 例如别把 todo 状态每轮刷进 system prompt |
| 持久化 append-only | 事件日志，重放即恢复，历史不被重写 |

## 存储（append-only 事件日志）

```python
class SessionStore(Protocol):
    async def append_events(self, session_id, events) -> None: ...
    async def load_events(self, session_id) -> list[SessionEvent]: ...
    async def list_sessions(self) -> list[SessionMeta]: ...
    async def delete(self, session_id) -> None: ...
```

`Session = replay(events)`。实现：`MemorySessionStore`（进程内）/ `JsonlSessionStore`（每会话一个 `.jsonl`，一行一事件，只追加）。

## 压缩

**压缩器只有 `LLMCompressor` 一种**（早期的 `PlaceholderCompressor` 已删除）。

```python
class Compressor(Protocol):
    async def compress(self, messages: list[Message]) -> str: ...
```

| 实现 | 成本 |
|---|---|
| `LLMCompressor(provider, model=None)` | 1 次模型调用，摘要质量最好；`compress_and_extract()` 可在同一次调用里顺带抽长期记忆 |

`SessionConfig`：`compact_threshold`（触发阈值）/ `keep_head`（固化开头几条进固定头）/ `keep_tail`（压缩后保留最近几条）/ `max_active_tokens` / `auto_title`（首轮自动标题开关）/ `title_max_chars`（标题字数上限）；`0` = 不保护。

阈值触发走 `SessionManager.maybe_compact(session)`（= `compact(session, force=False)`）；前端**手动压缩**用
`compact(session, force=True)` 跳过水位线（`enable_compaction=false` 时也能人工触发）。

## 目录结构

```
harness/session/
├─ README.md     # 本文档
├─ __init__.py   # 公共导出
├─ schema.py     # SessionEvent / SessionMeta / SessionConfig / Session（含 replay）
├─ store.py      # SessionStore 协议 + MemorySessionStore
├─ jsonl.py      # JsonlSessionStore
├─ compact.py    # Compressor 协议 + LLMCompressor（唯一实现）
├─ manager.py    # SessionManager（create/open/list/delete/run_turn/maybe_compact/compact）
├─ title.py      # TitleGenerator（首轮自动标题）+ fallback_title（兜底截断）
└─ test/         # 35 条全离线测试
```

## 快速上手

```python
from pathlib import Path
from harness.agent import AgentLoop, LoopConfig, PromptBuilder
from harness.llm import LLMConfig, Message, OpenAICompatProvider
from harness.session import JsonlSessionStore, LLMCompressor, SessionManager
from harness.tool import default_registry

# 如果要用带 scopes 的工具，记得在 Context 里给权限
from harness.tool import Context

provider = OpenAICompatProvider(LLMConfig(model="gpt-4o-mini"))
manager = SessionManager(
    JsonlSessionStore(Path(".sessions")),
    compressor=LLMCompressor(provider),   # 压缩器必填（只有 LLMCompressor 一种）
)
session = await manager.create(
    session_id="demo-1",
    system=[Message.system("你是一个严谨的助手")],   # 固定区：建会话时固化一次
    title="演示会话",
)

agent = AgentLoop(
    OpenAICompatProvider(LLMConfig(model="gpt-4o-mini")),
    Executor(default_registry),
    default_registry,
    prompt=PromptBuilder(),        # 注意：留空，别再注入 system（见下方约定）
)

result = await manager.run_turn(
    session, "读一下 a.txt 并总结", agent,
    ctx=Context(scopes={"fs:read"}),
)
result.final_text

# 多轮继续；重启进程后
session = await manager.open("demo-1")   # 事件重放恢复
```

## API 摘要

| 成员 | 说明 |
|---|---|
| `Session.create(*, session_id, system, title, metadata)` | 建会话（`system` 即固定区） |
| `Session.build_messages()` | 四段拼装：**固定区 + 固定头 + 压缩区 + 活跃区**，前缀单调 |
| `Session.append(*messages)` | 追加到活跃区，返回待落库事件 |
| `Session.record(result, sent)` | 取本轮新增消息落活跃区（按 `sent` 末条身份定位）；同时累加 `turns` 与用量，产出 `meta(turns)` + `usage` 事件 |
| `Session.rename(title)` | 改标题：只产一条 `meta(title=…)` 事件，消息序列不动 |
| `Session.adopt_head(keep_head)` | 首次压缩时把开头几条固化进固定头（产出 `meta(head_count=N)`） |
| `Session.compact(summary, dropped)` | 折叠活跃区头部为一条摘要 |
| `Session.is_over_threshold(config)` | 水位线判断（条数 / 可选 token 估算） |
| `Session.context(scopes=..., approval=..., ask=...)` | 产出带 `session_id` 的 `Context` |
| `estimate_tokens(messages)` | 模块级公开函数：粗略估算 token（中文≈1 字/token、ASCII≈4 字符/token、图片按固定值），供阈值判断与前端展示 |
| `replay(session_id, events)` | 事件重放 → `Session`（累加 `usage` 事件进 `usage_totals`；旧日志无该事件 → 保持 0） |
| `MemorySessionStore` / `JsonlSessionStore` | 两种存储（后者一行一事件，只追加） |
| `SessionManager.create/open/list/delete` | 会话生命周期 |
| `SessionManager.run_turn(session, text, agent, ctx=)` | **薄封装**：append → 压缩 → run → record |
| `SessionManager.maybe_compact(session)` | 达水位线时压缩（未触发返回 `[]`）；顺带抽候选记忆 |
| `SessionManager.compact(session, *, force=False)` | 压缩；`force=True` 跳过水位线（前端手动压缩） |
| `TitleGenerator(provider, *, model=None, max_chars=24, timeout_s=8.0)` | 首轮自动标题；异常/超时/空一律返回 `None`（绝不抛） |
| `fallback_title(user_text, *, max_chars=30)` | LLM 不可用时截断首条用户消息兜底 |
| `LLMCompressor` | 唯一的压缩实现（调模型） |

> `SessionMeta` 新增 `usage_totals`（`prompt_tokens` / `completion_tokens` / `total_tokens`），
> 跨轮累加，并出现在 `SessionMeta.to_dict()` 里（列表接口随之带上）。

## 压缩时抽取长期记忆（可选）

`LLMCompressor.compress_and_extract()` 用**同一次模型调用**同时产出摘要与候选长期记忆
（压缩正是"上下文要丢东西"的时刻）。`SessionManager(on_memory=...)` 用
`getattr(compressor, "compress_and_extract", None)` **探测**能力：

- 探测不到 → 走原来的 `compress()`；
- 探测到且有回调 → 摘要照常用，候选交给回调（写入策略由装配层决定，见
  [docs/memory-system-design.md](../../docs/memory-system-design.md) §7）；回调抛异常**不影响压缩**。

## 与 AgentLoop 的约定（缓存友好）

- **system 由 Session 固化**：建会话时写死一次，之后每轮不再重组。
- 因此 `AgentLoop` 应配 **空的 `PromptBuilder()`**，否则它会注入/合并 system，
  导致固定区被改写（前缀缓存击穿）。`record()` 已能容错，但缓存收益会丢失。
- Session 只传 `messages` 与 `ctx`，不干预 Loop 内部。

## 权限提示

`session.context()` 默认 `scopes=set()`（安全默认）。带 scopes 的工具（`fs.*` / `shell.run`）
必须在 `run_turn(..., ctx=Context(scopes={...}))` 里显式授权，否则返回 `permission_denied`。

## 测试

```bash
python -m harness.session.test.test_session   # 或 pytest harness/session/test
```

35 条测试**全离线**，覆盖：四段布局与前缀单调、`record` 切分（含头部注入 system 的容错）、
事件重放一致、内存/JSONL 存储（含只追加校验、非法 id 拒绝、半行跳过）、
压缩水位线与 `dropped` 计算、`keep_head` / `keep_tail` 两端保护、固定头重放、**压缩顺带抽候选记忆**
（切分纯函数 / 一次调用 / 无能力压缩器不抽取 / 落点异常不中断压缩）、
`run_turn` 多轮端到端与重放一致、固定区跨轮不变、
**`usage` 事件与 `usage_totals` 累计（含旧日志向后兼容）、`Session.rename`、`compact(force=True)`、
`TitleGenerator` / `fallback_title`（异常与超时回退）**。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 契约 + `MemorySessionStore`（四段布局 + replay） | ✅ 已完成 |
| M2 | `JsonlSessionStore`（事件追加 + 重放恢复） | ✅ 已完成 |
| M3 | 压缩（`compact.py` + 水位线触发） | ✅ 已完成 |
| M4 | `SessionManager.run_turn()` 端到端 | ✅ 已完成 |
| M5 | `LLMCompressor`（唯一压缩器）、按 token 估算触发、`keep_head` / `keep_tail` 两端保护 | ✅ 已完成 |
| M6 | 可观测与体验：`usage` 事件 + `usage_totals` 累计、`Session.rename`、`compact(force)`、首轮自动标题（`TitleGenerator`）、`estimate_tokens` 公开 | ✅ 已完成 |

## 待确认

见设计文档 §11（阈值默认值、是否补 SQLite；会话标题自动生成已落地）。
