# Agent Loop 设计文档

> 定位：**编排层（唯一有循环的地方）** · 技术栈：**Python** · 状态：**已实现 M1–M4**
> 依赖：[harness/llm](../harness/llm/README.md) · [harness/tool](../harness/tool/README.md) · [harness/skill](../harness/skill/README.md)
> 相关：[LLM 接入设计](llm-system-design.md) · [工具系统设计](agent-tool-system-design.md) · [Skills 设计](skills-system-design.md)

---

## 1. 定位与非目标

### 定位

把已有的三块能力串成一个能自主多轮工作的 Agent：

```
用户消息 ─→ 模型 ─→ 需要调工具？ ─→ 执行工具 ─→ 结果回喂 ─→ 模型 ─→ … ─→ 最终回答
```

它是**胶水层**，不实现模型调用、不实现工具执行，只负责**编排与停止**。

### 非目标（本模块不做）

| 不做 | 归属 |
|---|---|
| 会话历史 / 持久化 / 上下文压缩 | 后续 Session 层 |
| 模型调用细节（重试、协议、多模态） | [harness/llm](../harness/llm/README.md) |
| 工具执行（校验、鉴权、超时） | [harness/tool](../harness/tool/README.md) |
| 审批 / 提问的 UI 与传输 | 后续 FastAPI 层（本模块只透传回调） |
| 流式输出 | 暂缓（llms 本期不流式） |
| HTTP / CLI 入口 | 后续服务层 |

---

## 2. 在架构中的位置

```
        FastAPI / CLI（尚未实现）
                 │ RunResult / on_event
        ┌────────▼─────────┐
        │  harness/agent   │  ← 本模块：编排 + 停止条件
        └──┬────┬──────┬───┘
           │    │      │
   complete│    │execute│catalog
   ┌───────▼┐ ┌─▼────┐ ┌▼────────┐
   │ llms   │ │ tools│ │ skills  │
   │Provider│ │Execu.│ │Registry │
   └────────┘ └──────┘ └─────────┘
```

Loop 只依赖**已定死的接口**，因此自身很薄：

| 依赖 | 用到的接口 |
|---|---|
| llms | `LLMProvider.complete(ChatRequest) -> ChatResponse`、`ChatResponse.tool_call_dicts()` |
| tools | `Executor.execute_tool_calls(tool_calls, ctx) -> list[tool 消息]`、`ToolRegistry.to_openai_tools()` |
| skills | `SkillRegistry.catalog() -> str` |

---

## 3. 单轮流程

```
① messages（已含 system）→ ChatRequest(tools=registry.to_openai_tools())
② resp = await provider.complete(request)
③ messages.append(Message.assistant(resp.content, resp.tool_calls))
   发出事件 model_response
④ 若 resp 无 tool_calls → 结束（stop_reason = "final"）
⑤ 否则 results = await executor.execute_tool_calls(resp.tool_call_dicts(), ctx)
   messages += [Message.tool(r["tool_call_id"], r["content"]) for r in results]
   发出事件 tool_finished
⑥ 回到 ①（step += 1）
```

要点：

- **工具结果由 Executor 产出**，结构已与 `Message.tool()` 对齐，Loop **零转换**直接 append。
- **同轮多个 tool_calls** 由 `Executor` 并发执行并按 `tool_call_id` 归位，Loop 不关心顺序。
- **工具失败不中断**：Executor 已把异常转成结构化错误文本，回喂后模型可自行换方案。

---

## 4. 数据契约

```python
@dataclass
class LoopConfig:
    max_steps: int = 12                       # 最大轮次（含纯文本回复那轮）
    max_total_tokens: int | None = None       # 累计 token 预算，None 表示不限
    temperature: float | None = None
    max_tokens: int | None = None             # 单次生成上限
    tool_choice: str | None = "auto"          # 传入 tools 时生效
    model: str | None = None                  # 覆盖 Provider 默认模型
    stream: bool = False                      # 走 astream，逐增量发 text_delta 事件
    repeat_warn_at: int = 3                   # 循环保护：同签名累计到 3 次 → 提醒模型；0 = 关
    repeat_stop_at: int = 6                   # 循环保护：累计到 6 次 → loop_detected；0 = 关

@dataclass
class StepRecord:
    index: int
    response: ChatResponse
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0

@dataclass
class RunResult:
    messages: list[Message]                   # 输入 + 本轮新增（可直接作为下次输入）
    final_text: str | None
    steps: list[StepRecord]
    usage: Usage                              # 累计
    stop_reason: str                          # final | max_steps | token_budget | loop_detected | interrupted | error
    error: LLMError | None = None
    trace_id: str | None = None
```

> `RunResult.messages` 回传完整消息序列，宿主下次直接传回即可继续对话——
> **这就是「不含会话」的边界**：Loop 不存历史，只负责把这一轮的消息还给你。

---

## 5. 事件回调（为 SSE 留口）

```python
@dataclass
class LoopEvent:
    type: str                                 # run_started | model_response | tool_finished | step_finished | run_finished | error
    step: int = 0
    text: str | None = None                   # 模型的文本输出
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None
    error: LLMError | None = None

EventHook = Callable[[LoopEvent], Awaitable[None] | None]
```

- 回调**同步与异步都支持**：返回值是 awaitable 就 await，否则忽略。
- 回调异常**被吞掉**（同 `Tracer`），不能因为前端推送失败而中断 Agent。
- 本期只做回调，**不做流式**；将来 FastAPI SSE 直接把这些事件推给前端即可。

| 事件 | 时机 |
|---|---|
| `run_started` | 进入循环前 |
| `model_response` | 每轮拿到模型响应后（含文本与 tool_calls） |
| `tool_finished` | 一轮工具执行完成后 |
| `step_finished` | 每轮结束（含 usage） |
| `run_finished` | 循环结束（含 stop_reason） |
| `error` | 不可重试错误 |

---

## 6. 停止条件

| stop_reason | 触发 |
|---|---|
| `final` | 模型响应不含 `tool_calls`（正常结束） |
| `max_steps` | 达到 `LoopConfig.max_steps` |
| `token_budget` | 累计 `total_tokens` 超过 `max_total_tokens` |
| `loop_detected` | 同一工具被以相同参数反复调用（循环保护，见 §6.1） |
| `interrupted` | 用户插话：本轮工具已执行完，宿主探测回调命中而提前收尾（软打断，见 §6.2） |
| `error` | 抛出**不可重试**的 `LLMError`（可重试错误由 llms 内部重试，不冒泡到这里） |

### 6.1 循环保护（`LoopGuard`）

`max_steps` 只能兜住**总轮次**，兜不住**原地打转**：模型反复用一模一样的参数调用同一个工具，
每次都拿到同样的结果，却始终不换策略——这样烧完 12 轮和 token 预算，也没有任何进展。

```
同一签名（工具名 + 规范化参数）的累计调用次数
  ≥ repeat_warn_at（默认 3）→ 在该次工具结果末尾追加提醒（每个签名只提醒一次）
  ≥ repeat_stop_at（默认 6）→ 结束本轮，stop_reason = "loop_detected"
```

实现约定：

- **签名与键序无关**：`arguments` 先 `json.loads` 再 `sort_keys` 序列化，`{"a":1,"b":2}` 与
  `{"b":2,"a":1}` 是同一签名；解析失败则退化为原串比较。
- **提醒追加而非替换**：工具输出原样保留，提醒文本附在其后——模型能同时看到结果与劝告。
- **判定后仍然执行工具**：即使这一步已判定要终止，工具也会先跑完。否则 `assistant.tool_calls`
  缺少对应的 `tool` 消息，会话记录非法，**下一轮请求会直接被模型拒绝**。
- **先提醒、再终止**：与「审批缺回调 → 回喂模型自纠」一脉相承——先给模型一次改策略的机会。
- 两个阈值取 `0` 即关闭。`LoopGuard` 有状态，**每次 `run()` 新建**，不跨 run 复用。
- 不新增事件类型：提醒随 `tool_finished` 可见，终止随 `done` 的 `stop_reason` 可见。

> 早先设计里写的是「不设 `no_progress`（重复调用检测）——先观察真实行为，避免过早加复杂度」。
> 但它一直是 [todo.md](../todo.md)「防 agent 失控」三件套里唯一没做的那件，
> 而 `max_steps` 与 token 预算确实挡不住「参数完全相同的空转」，遂补上。

### 6.2 软打断（用户插话）

对话运行时，用户常常想**插话**：不必等整轮跑完，「当前工具一跑完就听我说」。
这需要一个能提前收尾、但**不破坏会话历史**的出口——`StopReason.INTERRUPTED`。

机制：`AgentLoop.__init__(..., should_interrupt: Callable[[], bool] | None = None)`。
宿主提供一个**无参探测回调**（例如「该会话是否又收到了一条用户输入」），Loop 在检查点调用它：

- **无回调、或回调抛异常，一律视为 `False`**——不能因宿主缺陷而中断 Agent。
- 命中即 `_finish(..., StopReason.INTERRUPTED, final_text=None)` 提前返回。

检查点位置（第 N 步）：

```
model_response → tool_finished → step_finished
  → 【用户插话检查点】← 命中即收尾（stop_reason = "interrupted"）
  → 循环保护判定（verdict.stopped）
  → token 预算判定（_over_budget）
```

**为什么放这里**：此刻本轮 `assistant(tool_calls)` 与对应的 `tool` 消息**已成对**，
`result.messages` 仍然合法、可直接落库；若放得更早（模型生成中或工具执行中）就会留下不成对的历史。
也正因如此，它在判定顺序上**用户意图优先**——只要插话命中就先收尾，不再看是否同时触发了循环保护或预算。

**能力边界（协作式，不是强制中止）**：

| 能 | 不能 |
|---|---|
| 当前模型回复结束、其工具全部执行完之后立即收尾 | 打断**正在进行的模型生成**（`provider.astream` 没有取消管线） |
| 保留本轮已成对的消息，正常返回 `RunResult` | 打断**正在执行的工具**（`executor.execute_tool_calls` 是 `gather`，不加取消） |

这正是需求要的「等工具跑完」语义。

**与硬取消的对比**：

| 维度 | 硬取消 | 软打断 |
|---|---|---|
| 入口 | `POST /runs/{id}/cancel` | 宿主注入的 `should_interrupt` 探针 |
| 实现 | `task.cancel()` 立即中断 | 检查点轮询，命中才收尾 |
| `stop_reason` | `cancelled` | `interrupted` |
| 本轮新增消息 | **整批不落库**（`SessionManager.run_turn` 的 `session.record` 不执行） | 照常 `record`，部分历史成对落库 |
| 生效时刻 | 任意时刻 | 模型回复结束、工具全跑完 |

可观测性：run 根 span 的 `stop_reason` 因此新增取值 `interrupted`（`_finish` 里统一
`tracer.record(RUN_SPAN, stop_reason=...)`），**无需新埋点**。

---

## 7. 审批与提问（缺回调 → 回喂模型）

**决策：无回调时不中断，把结构化错误回喂给模型。**

| 场景 | 行为 |
|---|---|
| 有 `Context.approval` | 正常走审批（如 `shell.run` 放行/拒绝） |
| 无 `Context.approval` | Executor 返回 `approval_required` → 作为 tool 结果回喂 → **模型看到「需要人工确认」后可换方案或说明** |
| 有 `Context.ask` | `ask_user` 正常提问取答复 |
| 无 `Context.ask` | 返回 `execution_error`（未提供回调）→ 回喂模型 |

这样 Loop 在**没有前端**的情况下也能跑（例如 CLI、测试、批处理），不会卡在等人确认。

```python
# 宿主注入回调（FastAPI 层将来提供）
ctx = Context(scopes={"fs:read"}, approval=my_approval, ask=my_ask)
result = await agent.run(messages, ctx=ctx)
```

---

## 8. Prompt 组装

```python
class PromptBuilder:
    def __init__(
        self,
        *,
        base_instructions: str = "",
        skills: SkillRegistry | None = None,
        include_skill_catalog: bool = True,
    ) -> None: ...

    def build(self, messages: list[Message]) -> list[Message]:
        """在最前插入（或合并进已有）system 消息。"""
```

system prompt 结构：

```
<base_instructions>          # 角色与通用规则

## 可用技能（skill）
<SkillRegistry.catalog()>    # 仅 name + description，正文靠 skill.load 按需取
```

两条重要约定：

1. **工具定义不进 system prompt**——它们是 `ChatRequest.tools` 参数（function calling），
   由 Loop 从 `ToolRegistry.to_openai_tools()` 直接取。
2. **skill 只放目录不放正文**——正文通过 `skill.load` 工具按需加载（渐进式披露）。

> 这一步同时修掉了当前 `SkillRegistry.catalog()` **无调用方**的孤儿状态。

---

## 9. 目录结构

```
harness/agent/
├─ README.md     # 模块文档
├─ __init__.py   # 公共导出
├─ schema.py     # LoopConfig / StepRecord / RunResult
├─ events.py     # LoopEvent / EventHook
├─ prompt.py     # PromptBuilder
├─ loop.py       # AgentLoop
└─ test/        # 全离线测试（FakeProvider + 真实 registry）
```

---

## 10. 核心实现骨架

```python
class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        executor: Executor,
        registry: ToolRegistry,
        *,
        config: LoopConfig | None = None,
        prompt: PromptBuilder | None = None,
        on_event: EventHook | None = None,
        tracer: "Tracer | None" = None,
        should_interrupt: "Callable[[], bool] | None" = None,
    ) -> None: ...

    async def run(self, messages: list[Message], ctx: Context | None = None) -> RunResult:
        ctx = ctx or Context()
        messages = self.prompt.build(messages)
        tools = self.registry.to_openai_tools() or None
        usage = Usage()
        steps: list[StepRecord] = []

        for index in range(1, self.config.max_steps + 1):
            response = await self.provider.complete(self._request(messages, tools))
            usage = _add_usage(usage, response.usage)
            messages.append(Message.assistant(response.content, response.tool_calls))

            if not response.has_tool_calls():
                return self._finish(messages, steps, usage, "final", response.content)

            results = await self.executor.execute_tool_calls(response.tool_call_dicts(), ctx)
            messages += [Message.tool(r["tool_call_id"], r["content"]) for r in results]
            steps.append(StepRecord(index, response, results))

            # 用户插话检查点：本轮工具已跑完、历史成对；用户意图优先于下面两项
            if self._interrupted():
                return self._finish(messages, steps, usage, "interrupted", None)

            if self._over_budget(usage):
                return self._finish(messages, steps, usage, "token_budget", None)

        return self._finish(messages, steps, usage, "max_steps", None)
```

错误处理（实现已定）：`LLMError` **一律不抛异常**，统一以 `stop_reason="error"` + `result.error` 返回
（宿主读 `error.retryable` 决定是否重跑）；**Loop 自身不重试**（llms 已用 SDK 重试，避免双重退避叠加）。
非 `LLMError` 的异常（程序 bug）仍然照常抛出。

---

## 11. 测试计划（全离线，无需网络与 API Key）

| 用例 | 断言 |
|---|---|
| 单轮纯文本 | `stop_reason == "final"`、`final_text` 正确、messages 恰好多 1 条 |
| 工具调用一轮 | 第二轮回文本；tool 消息结构正确、`tool_call_id` 对应 |
| 多轮工具 | 3 步后结束；`steps` 长度与顺序正确 |
| `max_steps` | 模型一直要求调工具 → `stop_reason == "max_steps"` |
| token 预算 | `max_total_tokens` 触发 → `stop_reason == "token_budget"` |
| 审批缺回调 | `shell.run` 无 `approval` → 错误回喂 → 模型收到文本后结束（不中断） |
| 事件回调 | 事件类型顺序与 step 编号；回调抛异常不影响主流程 |
| Prompt 组装 | system prompt 含 skill catalog；工具定义出现在 `ChatRequest.tools` 而非 prompt |
| 可重试错误 | Provider 抛可重试 `LLMError` → 同样**返回**不抛，`error.retryable=True`；不产生死循环 |
| 流式 | `stream=True` → 逐增量发 `text_delta`，拼起来等于 `final_text`；默认关闭时不发 |
| 循环保护 | 同签名键序无关；第 3 次提醒（只提醒一次）、第 6 次 `loop_detected`；阈值 0 关闭；换参数不算重复 |
| 软打断 | `should_interrupt` 在工具步后命中 → `stop_reason == "interrupted"`、历史成对、`final_text is None`；无回调 / 回调抛异常视为 `False`；打断优先于 `loop_detected` |

---

## 12. 落地路线

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 契约 + Prompt | `schema.py` + `events.py` + `PromptBuilder` | ✅ 已完成 |
| M2 核心循环 | `AgentLoop.run()` | ✅ 已完成 |
| M3 停止条件 + 事件 | `max_steps` / token 预算 / 事件回调 | ✅ 已完成 |
| M4 审批回喂 + 测试补全 | 缺回调场景 + 全量测试 | ✅ 已完成 |
| M5 流式 | `LoopConfig.stream` + `text_delta` | ✅ 已完成 |
| M6 循环保护 | `LoopGuard` 重复调用检测 | ✅ 已完成 |

实现见 [harness/agent/README.md](../harness/agent/README.md)；
测试：`python -m harness.agent.test.test_agent_loop`（43/43，全离线）。

---

## 13. 决策记录

| 议题 | 结论 |
|---|---|
| 模块落点 | `harness/agent/`，与 tool / skill / llm 并列 |
| 循环范围 | **纯循环**，不含会话历史与持久化（`RunResult.messages` 交还宿主） |
| 审批/提问缺回调 | **回喂模型继续**，不中断（结构化错误作为 tool 结果） |
| 事件回调 | **本期就加** `on_event`，为后续 FastAPI SSE 留口 |
| 软打断（用户插话） | **协作式探针** `should_interrupt`：本轮工具执行完后收尾（`stop_reason=interrupted`），不打断模型生成与工具执行；与硬取消（`cancelled`、整批不落库）严格区分 |

### 仍待确认

- [ ] `max_steps` 默认值（当前 12）是否合适？
- [ ] 是否需要在 `run()` 入口做输入裁剪（超长历史先压缩）？（当前交给宿主）
- [ ] 回调是否支持「中断」语义（返回 False 即停止）？当前不支持——用户插话走独立的 `should_interrupt` 探针（见 §6.2），不改动事件回调的返回值语义。
