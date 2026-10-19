# agent — Agent Loop 模块

> 状态：**已实现（M1–M5）**。
> 全局设计见 [docs/agent-loop-design.md](../../docs/agent-loop-design.md)。对外服务见 [server/README.md](../../server/README.md)。

## 职责

把已有的三块能力串成一个能自主多轮工作的 Agent：

```
用户消息 ─→ 模型 ─→ 需要调工具？ ─→ 执行工具 ─→ 结果回喂 ─→ 模型 ─→ … ─→ 最终回答
```

它是**胶水层**，只负责**编排与停止**，不实现模型调用、不实现工具执行。

## 边界

| 不做 | 归属 |
|---|---|
| 会话历史 / 持久化 / 上下文压缩 | [harness/session](../session/README.md)（本模块只把 `RunResult.messages` 交还宿主） |
| 模型调用细节（重试、协议、多模态、流式） | [harness/llm](../llm/README.md) |
| 工具执行（校验、鉴权、超时） | [harness/tool](../tool/README.md) |
| 审批 / 提问的 UI 与传输 | [server](../../server/README.md)（本模块只透传回调） |
| HTTP / SSE 入口 | [server](../../server/README.md) |

## 为什么很薄：三个契约已就绪

| 依赖 | 用到的接口 | 状态 |
|---|---|---|
| llms | `LLMProvider.complete()` / `ChatResponse.tool_call_dicts()` | ✅ |
| tools | `Executor.execute_tool_calls()` / `ToolRegistry.to_openai_tools()` | ✅ |
| skills | `SkillRegistry.catalog()` | ✅（接进 prompt 后即不再是孤儿能力） |

## 循环体

```
① messages → ChatRequest(tools=registry.to_openai_tools())
② resp = await provider.complete(request)
③ messages.append(Message.assistant(resp.content, resp.tool_calls))
④ 若无 tool_calls → 结束（stop_reason = "final"）
⑤ 否则 results = executor.execute_tool_calls(resp.tool_call_dicts(), ctx)
   messages += Message.tool(...)   # 零转换，Executor 输出即 Message.tool 的入参
⑥ 回到 ①
```

工具结果由 `Executor` 并发执行并按 `tool_call_id` 归位；工具失败不中断（结构化错误回喂模型）。

## 停止条件

| stop_reason | 触发 |
|---|---|
| `final` | 响应不含 `tool_calls` |
| `max_steps` | 达到 `LoopConfig.max_steps`（默认 12） |
| `token_budget` | 累计 token 超 `max_total_tokens` |
| `loop_detected` | 同一工具被以相同参数反复调用（见下节） |
| `interrupted` | 用户插话：本轮工具已执行完，`should_interrupt` 命中而提前收尾（见「软打断」） |
| `error` | 不可重试的 `LLMError`（可重试的由 llms 内部重试，不冒泡） |

## 循环保护

`max_steps` 只能兜住**总轮次**，兜不住**原地打转**——模型反复用一模一样的参数调用同一个工具，
每次都拿到同样的结果却始终不换策略。`LoopGuard` 补这一块：

```
同一签名（工具名 + 规范化参数）的累计调用次数
  ≥ repeat_warn_at（默认 3）→ 在该次工具结果末尾追加一句提醒（每个签名只提醒一次）
  ≥ repeat_stop_at（默认 6）→ 结束本轮，stop_reason = "loop_detected"
```

要点：

- **签名与键序无关**：`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 视为同一签名（`call_signature`）。
- **提醒是追加而非替换**：工具输出原样保留，提醒文本附在其后，模型能同时看到结果与劝告。
- **仍然执行工具**：即便这一轮已判定要终止，工具也会先执行——否则 `assistant.tool_calls`
  没有对应的 `tool` 消息，会话记录非法，下一轮会直接报错。
- **先提醒再终止**，与项目一贯的「失败不抛异常、回喂模型自纠」一致（对照：审批缺回调 → 回喂模型）。
- 两个阈值取 `0` 即关闭（可整体关闭循环保护）。

## 软打断（用户插话）

对话运行时允许用户**插话**：不必等整轮跑完，希望「当前工具一跑完就听我说」。`AgentLoop` 用一个**宿主提供的探测回调**支持它：

```python
loop = AgentLoop(
    provider, executor, registry,
    should_interrupt=lambda: bool(session.has_pending_input),   # 每步工具执行完后调用
    on_event=on_event,
)

result = await loop.run(messages, ctx=ctx)
result.stop_reason   # 命中插话时为 "interrupted"
```

要点：

- `should_interrupt` 是**协作式探针**：Loop 在每步**工具全部执行完之后**调用它；返回真值即提前收尾
  （`stop_reason = "interrupted"`、`final_text = None`）。
- **无回调、或回调抛异常，一律视为 `False`**——不能因宿主缺陷而中断 Agent。
- 命中点在本轮 `assistant(tool_calls)` 与 `tool` 结果**已成对之后**，所以 `RunResult.messages` 依旧合法、可直接落库。
- **用户意图优先**：检查点在循环保护与 token 预算判定**之前**，命中即收尾。
- **能力边界**：只在这个时刻生效，**不打断**正在进行的模型生成（`astream` 无取消管线）与正在执行的工具
  （`execute_tool_calls` 是 `gather`，不加取消）——这正是「等工具跑完」的语义。
- **与硬取消的区别**：`POST /runs/{id}/cancel` 是立即中断、`stop_reason = "cancelled"`、本轮新增消息**整批不落库**；
  软打断是正常返回、`record` 照常执行、部分历史成对落库（在 run 根 span 上分别记为 `cancelled` / `interrupted`）。
  详见设计文档 §6.2。

## 审批与提问（已定：缺回调 → 回喂模型）

| 场景 | 行为 |
|---|---|
| 有 `Context.approval` / `ask` | 正常走审批 / 提问 |
| 无回调 | Executor 返回 `approval_required` / `execution_error` → **作为 tool 结果回喂**，模型换方案继续 |

好处：**没有前端也能跑**（CLI、测试、批处理），不会卡在等人确认。

## 事件回调（SSE 的事件源）

`on_event` 支持同步与异步，回调异常被吞掉（不能因前端推送失败中断 Agent）。

事件：`run_started` / `text_delta` / `model_response` / `tool_finished` / `step_finished` / `run_finished` / `error`。

`text_delta` 仅在 `LoopConfig.stream=True` 且 Provider 支持 `astream` 时出现（逐增量文本）；
`model_response` 始终给出**整段**回复，因此消费方两种模式都能用。

## 流式

```
LoopConfig(stream=True) → provider.astream(request)
  逐块 feed 到 StreamAccumulator，并把文本增量发成 text_delta 事件
  → 流结束得到完整 ChatResponse，后续步骤（tool_calls / 停止判断）与原来完全一致
```

`stream=False`（默认）或 Provider 无 `astream` 时自动回退到 `complete()`，行为与之前完全一致。

## Prompt 组装

`PromptBuilder.build(messages)` 在最前插入 system 消息：

```
<base_instructions>

## 可用技能（skill）
<SkillRegistry.catalog()>
```

两条约定：
1. **工具定义不进 system prompt**——走 `ChatRequest.tools`（function calling）。
2. **skill 只放目录不放正文**——正文靠 `skill.load` 按需加载（渐进式披露）。

## 目录结构

```
harness/agent/
├─ README.md     # 本文档
├─ __init__.py   # 公共导出
├─ schema.py     # LoopConfig / StepRecord / RunResult / StopReason
├─ events.py     # LoopEvent / EventType / EventHook
├─ guard.py      # LoopGuard：重复调用检测（循环保护）
├─ prompt.py     # PromptBuilder
├─ loop.py       # AgentLoop
└─ test/         # 43 条全离线测试
```

## 快速上手

```python
from harness.agent import AgentLoop, LoopConfig, PromptBuilder, StopReason
from harness.llm import LLMConfig, Message, OpenAICompatProvider
from harness.tool import Context, Executor, default_registry
from harness.skill import SkillRegistry, register_skill_tools

registry = default_registry
skills = SkillRegistry("skills")          # 构造即扫描
register_skill_tools(registry, skills)    # 让模型能 skill.load

provider = OpenAICompatProvider(LLMConfig(model="gpt-4o-mini"))
loop = AgentLoop(
    provider,
    Executor(registry),
    registry,
    config=LoopConfig(max_steps=8, max_total_tokens=200_000),
    prompt=PromptBuilder(base_instructions="你是一个严谨的助手", skills=skills),
    on_event=lambda e: print(e.type, e.step),     # 同步/异步均可
)

result = await loop.run(
    [Message.user("读一下 a.txt 并总结")],
    ctx=Context(scopes={"fs:read"}, approval=my_approval, ask=my_ask),
)

result.stop_reason   # final | max_steps | token_budget | loop_detected | interrupted | error
result.final_text
result.messages      # 含本轮全部新增，可直接作为下一轮输入
```

## API 摘要

| 成员 | 说明 |
|---|---|
| `AgentLoop(provider, executor, registry, *, config, prompt, on_event, tracer, should_interrupt)` | 构造；`should_interrupt` 为用户插话探测回调（协作式，异常视为 `False`） |
| `AgentLoop.run(messages, ctx=None) -> RunResult` | **无状态**：messages 进、messages 出 |
| `LoopConfig` | `max_steps`(默认 12) / `max_total_tokens` / `temperature` / `max_tokens` / `tool_choice` / `model` / `stream`(默认 `False`) / `repeat_warn_at`(3) / `repeat_stop_at`(6) |
| `RunResult` | `messages` / `final_text` / `steps` / `usage` / `stop_reason` / `error` / `trace_id` |
| `StepRecord` | `index` / `response` / `tool_results` / `duration_ms` |
| `StopReason` | `final` / `max_steps` / `token_budget` / `loop_detected` / `interrupted` / `error` |
| `LoopGuard` / `call_signature` | 重复调用检测；签名 = 工具名 + 规范化参数（键序无关） |
| `PromptBuilder(base_instructions, skills, include_skill_catalog)` | `build(messages)` 在最前插入/合并 system 消息 |
| `LoopEvent` / `EventType` / `EventHook` | 事件回调（同步/异步，异常被吞） |

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 契约 + `PromptBuilder`（system prompt 含 skill 目录） | ✅ 已完成 |
| M2 | 核心循环 `AgentLoop.run()` | ✅ 已完成 |
| M3 | 停止条件 + 事件回调 | ✅ 已完成 |
| M4 | 审批回喂 + 全量测试 | ✅ 已完成 |
| M5 | 流式：`LoopConfig.stream` + `text_delta` 事件（`StreamAccumulator` 回流为完整响应） | ✅ 已完成 |
| M6 | 循环保护：`LoopGuard` 重复调用检测 → 先提醒、再 `loop_detected` 终止 | ✅ 已完成 |
| M7 | 观测贯通：run 边界 set「当前 trace / 根 span」，`agent.run` 带 `duration_ms` → span 聚合成树 | ✅ 已完成 |

## 测试

```bash
python -m harness.agent.test.test_agent_loop   # 或 pytest harness/agent/test
```

43 条测试**全离线**（`FakeProvider` + 真实 registry），覆盖：
Prompt 组装（含 skill 目录加载/禁用、长期记忆注入与顺序）、
单轮文本、工具一轮、多轮、usage 累计、`max_steps`、token 预算、LLM 错误不抛异常、
事件顺序与异步回调、回调异常被吞、**审批缺回调回喂模型**、审批回调透传、
`Tracer` 运行 span 与父子关联、skill 工具接线、流式 `text_delta` 逐增量与默认关闭、
**循环保护**（签名键序无关 / 先提醒后终止且只提醒一次 / 可关闭 / 换参数不算重复）、
**观测贯通**（llm span 拿到当前 trace / run 根 span 带 `duration_ms` / span 成树 / contextvar 不跨 run 泄漏）、
**长期记忆注入**（base → skill → memory 顺序固定 / 空记忆不留空节 / 同内容同字节）、
**软打断**（`StopReason.INTERRUPTED` 常量 / 工具步后命中即收尾且历史成对 / 未请求插话行为不变 / 回调抛异常视为 `False` / 打断优先于 `loop_detected`）。

## 为多 Agent 预留的缝

| 缝 | 落点 |
|---|---|
| Loop 无状态 | `run(messages, ctx)` 不持历史，子 Agent 就是再调一次 |
| `Context` 可派生 | [`Context.derive_child()`](../tool/schema.py)：新 trace、可收窄 scopes、带 `parent_trace_id` |
| 审批/提问走注入 | 不依赖全局单例，子 Agent 可继承或收敛 |
| `Tracer` 父子关联 | 子 span 默认挂到当前 run 的根 span；跨 Agent 用 `ctx.metadata["parent_span_id"]` 接父链 |

## 待确认

见设计文档 §13（`max_steps` 默认值、入口是否做输入裁剪、回调是否支持中断语义）。
