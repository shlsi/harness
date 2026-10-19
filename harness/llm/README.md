# llms — 模型接入子模块

> 状态：**已实现（M1–M4，含流式）**；其它 Provider（Anthropic 等）**暂缓**。
> 全局设计见 [docs/llm-system-design.md](../../docs/llm-system-design.md)。

## 职责

为 Agent Loop 提供统一的**模型调用能力**：请求组装、响应解析、`tool_calls` 提取、
错误归一化、token 计量。向上暴露稳定接口，向下屏蔽各家 API 差异。

## 边界

本模块**只做模型调用**，不做：

- Agent Loop（循环 / 停止条件 / 会话编排）—— 见 [harness/agent](../agent/README.md)
- prompt 组装（只接收 `messages`，不管它们怎么拼出来）
- 工具执行（`tool_calls` 原样交给 [harness/tool](../tool/README.md) 的 Executor）
- 记忆与上下文压缩（属 [harness/session](../session/README.md)）
- HTTP / SSE（属 [server](../../server/README.md)）

## 接入方式（已定）

**只做一种接入：OpenAI Chat Completions 兼容接口。** 所有模型（OpenAI / DeepSeek / Qwen / vLLM / Ollama / 兼容网关）
都从这一个口进来，差异只在 `base_url` / `api_key` / `model` 三项配置，**代码路径完全相同**。

实现**使用官方 `openai` SDK**，不自行封装 HTTP：
SDK 已覆盖 tools、多模态、usage、错误类型与重试，协议演进由 SDK 兜底。

> 依赖：`openai`（已安装，验证版本 3.35.1）。`openai_compat.py` 内部延迟导入该 SDK，
> 因此未安装时本模块其余部分（契约 / 错误映射）仍可导入与测试。

## 与工具系统的对接（关键约定）

两条结构对齐，保证**零转换**直通：

1. `Message.to_dict()` 产出的 tool 消息 `{role, tool_call_id, content}`
   ↔ `Executor.execute_tool_calls()` 的返回值**完全一致**，可原地回填。
2. `ToolCall.to_dict()` 产出 `{id, type:"function", function:{name, arguments}}`
   ↔ `Executor._parse_call` 接受的形式一致，`arguments` 保持 JSON 字符串原样，
   **解析与校验失败统一由工具系统处理**。

```
ChatRequest.tools  ←  ToolRegistry.to_openai_tools()
ChatResponse.tool_calls  →  Executor.execute_tool_calls()  →  回填 messages
```

## 快速上手

```python
from harness.llm import LLMConfig, Message, ChatRequest, OpenAICompatProvider
from harness.tool import Context, Executor, default_registry

# 任何 OpenAI 兼容服务：只改 base_url / api_key / model
provider = OpenAICompatProvider(LLMConfig(model="gpt-4o-mini", base_url="https://api.deepseek.com/v1"))

request = ChatRequest(
    messages=[Message.system("你是助手"), Message.user("读一下 a.txt")],
    tools=default_registry.to_openai_tools(),      # 工具定义直出
)
response = await provider.complete(request)

if response.has_tool_calls():
    executor = Executor(default_registry)
    results = await executor.execute_tool_calls(response.tool_call_dicts(), Context(scopes={"fs:read"}))
    request.messages.append(Message.assistant(tool_calls=response.tool_calls))
    request.messages += [Message.tool(r["tool_call_id"], r["content"]) for r in results]
    # 再次 complete(request) 即完成一轮「模型 → 工具 → 回填」

await provider.aclose()
```

图片消息：

```python
Message.user("这张图是什么", images=["https://example.com/a.png", "data:image/png;base64,..."])
```

## 流式

`astream(request)` 是 async generator，逐块吐 `StreamEvent`：

| type | 载荷 |
|---|---|
| `text_delta` | `text`：一段文本增量 |
| `tool_call_delta` | `index` + `tool_call`：某工具的调用片段（`id`/`name` 仅首块非空，`arguments` 是字符串片段） |
| `done` | `usage` / `finish_reason` |
| `error` | `error`（`LLMError`） |

```python
from harness.llm import StreamAccumulator
acc = StreamAccumulator()
async for event in provider.astream(request):
    acc.feed(event)
response = acc.to_response()     # 与 provider.complete(request) 的结果同构
```

约定：
- 请求自动带 `stream=True` 与 `stream_options={"include_usage": True}`（OpenAI 兼容服务普遍支持；不支持的网关可用 `extra` 覆盖）。
- 失败**抛 `LLMError`**（与 `complete()` 一致），由调用方统一处理。
- **片段重组规则集中在 `StreamAccumulator`**，Provider 与 Agent Loop 都不重复实现。

## API 摘要

| 成员 | 说明 |
|---|---|
| `Message` / `.system()` / `.user(text, images)` / `.assistant(content, tool_calls)` / `.tool(id, content)` | 消息构造，`to_dict()` 输出协议格式 |
| `Message.from_dict()` | 从协议 dict 还原（`to_dict` 的逆运算，供会话重放使用） |
| `ToolCall` / `.from_dict()` / `.to_dict()` | 工具调用，`arguments` 保持 JSON 字符串 |
| `ChatRequest` / `.payload(model)` | 请求；`payload()` 产出 `chat.completions.create` 的 kwargs |
| `ChatResponse` / `.has_tool_calls()` / `.tool_call_dicts()` | 响应；`tool_call_dicts()` 直喂 `Executor` |
| `Usage` | `prompt_tokens` / `completion_tokens` / `total_tokens` |
| `LLMConfig` / `.resolved_api_key()` | 配置；api_key 缺省读 `OPENAI_API_KEY` |
| `OpenAICompatProvider(config, *, tracer=None)` | 官方 SDK 实现；`complete()` / `astream()` / `aclose()` |
| `FakeProvider(responses, *, default=None, chunk_size=4)` | 测试/离线用，按队列返回并记录请求；`astream()` 按 `chunk_size` 切文本 |
| `LLMProvider` | 协议（测试可注入的缝）：`complete` / `astream` / `aclose` |
| `StreamEvent` / `StreamEventType` | 流式增量契约（`text_delta` / `tool_call_delta` / `done` / `error`） |
| `StreamAccumulator` | 增量片段 → 完整 `ChatResponse` |
| `to_chat_response(completion)` | SDK 响应 → `ChatResponse`（可单测，无需网络） |
| `to_stream_event(chunk)` | SDK 流式块 → `StreamEvent`（无有效增量返回 `None`，可单测） |
| `map_exception(exc)` | SDK / 网络异常 → `LLMError` |

## 数据契约摘要

| 类型 | 说明 |
|---|---|
| `Message` | `role` / `content`（`str` 或多模态 parts 列表）/ `tool_calls` / `tool_call_id`；附 `to_dict()`、`Message.user(text, images)` |
| `ToolCall` | `id` / `name` / `arguments`（JSON 字符串），附 `to_dict()` |
| `ChatRequest` | `messages` + `tools` / `tool_choice` / `temperature` / `max_tokens` / `extra` |
| `ChatResponse` | `content` / `tool_calls` / `finish_reason` / `usage` / `model` / `raw` |
| `Usage` | `prompt_tokens` / `completion_tokens` / `total_tokens` |
| `StreamEvent` | `type`(`text_delta`/`tool_call_delta`/`done`/`error`) + `text` / `index` / `tool_call` / `usage` / `finish_reason` / `error` |
| `LLMConfig` | `model` / `api_key` / `base_url` / `timeout_s` / `max_retries` / `extra_headers` |

**图片支持**：`Message.content` 可为 parts 列表
（`[{"type":"text",...}, {"type":"image_url","image_url":{"url":...}}]`），
URL 与 base64 data URL 均可；本模块不做本地文件读取。

**表示统一**：`Message.tool_calls` 的空列表会被规范成 `None`（`__post_init__`），
保证 `to_dict()` / `from_dict()` 往返后对象相等——这是会话持久化与重放的前提。

## 错误类型

| type | SDK 异常 | 可重试 |
|---|---|---|
| `auth` | `AuthenticationError` / `PermissionDeniedError` | 否 |
| `rate_limit` | `RateLimitError` | 是 |
| `timeout` | `APITimeoutError` | 是 |
| `network` | `APIConnectionError` | 是 |
| `server` | `InternalServerError` | 是 |
| `context_length` / `bad_request` | `BadRequestError` | 否 |

重试本身交给 SDK（`max_retries`），本模块只做异常映射。

## 目录结构（规划）

```
harness/llm/
├─ README.md          # 本文档
├─ __init__.py        # 公共导出
├─ schema.py          # Message / ToolCall / ChatRequest / ChatResponse / Usage / StreamEvent / LLMConfig
├─ errors.py          # LLMError 家族 + openai SDK 异常映射
├─ base.py            # LLMProvider 协议 + FakeProvider（测试 / 离线）
├─ openai_compat.py   # OpenAICompatProvider（官方 openai SDK，complete + astream）
├─ stream.py          # StreamAccumulator：增量片段 → 完整 ChatResponse
└─ test/
```

`LLMProvider` 协议保留的意义：给 Agent Loop 一个**可注入的测试缝**，
测试用 `FakeProvider` 返回预置响应，无需真实网络。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 契约：`schema.py`（多模态 `Message`）+ `errors.py`（异常映射） | ✅ 已完成 |
| M2 | 调用：`base.py` 协议 + `FakeProvider`；`openai_compat.py` 的 `complete()` | ✅ 已完成 |
| M3 | 观测接线：usage / 耗时上报 `Tracer` | ✅ 已完成 |
| M4 | 流式：`StreamEvent` 契约 + `StreamAccumulator` + `astream()` | ✅ 已完成 |
| 暂缓 | Anthropic 等其它 Provider | — |

**最小可用 = M1 + M2**（配合 Agent Loop 即可跑通「模型 → 工具 → 回填」闭环）。

## 测试

```bash
python -m harness.llm.test.test_llms   # 或 pytest harness/llm/test
```

27 条测试**全部离线**（用 `FakeProvider` 与假客户端），覆盖：契约与协议对齐、
多模态消息、异常映射、SDK 响应解析、Provider 调用与错误映射、遥测上报、
**流式契约 / `StreamAccumulator` 重组 / `to_stream_event` 映射 / `astream` 调用与错误映射**，
以及**与工具系统的闭环**（`tool_calls` 直喂 `Executor` 并回填）。

## 待确认

见设计文档 §10（模型能力表、图片便捷函数、`tool_choice` 默认值）。
