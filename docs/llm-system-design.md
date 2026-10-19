# LLM 接入层设计文档

> 定位：**OpenAI 兼容接口的模型接入层** · 技术栈：**Python + 官方 openai SDK** · 状态：**已实现 M1–M4（含流式）**
> 相关：[工具系统设计文档](agent-tool-system-design.md) · [Skills 系统设计文档](skills-system-design.md) · [todo.md](../todo.md)

---

## 1. 定位与非目标

### 定位

为 Agent Loop 提供**统一的模型调用能力**：请求组装、响应解析、`tool_calls` 提取、流式输出、
错误重试、token 计量。它是「模型 ↔ 工具」之间的一层，**向上**暴露稳定的调用接口，**向下**屏蔽各家 API 差异。

### 在架构中的位置

```
                 ┌─────────────────────────────┐
                 │        Agent Loop            │   ← 尚未实现（P0）
                 │  消息编排 / 停止条件 / 循环   │
                 └───────┬─────────────▲────────┘
                         │ ChatRequest │ ChatResponse(tool_calls)
                 ┌───────▼─────────────┴────────┐
                 │      harness/llm            │   ← 本模块
                 │  Provider 抽象 / 重试 / 计量  │
                 └───────┬─────────────▲────────┘
                         │             │
                 ┌───────▼─────────────┴────────┐
                 │      harness/tool           │   ← 已实现
                 │  Executor.execute_tool_calls │
                 └──────────────────────────────┘
                         ▲
                 harness/skill（catalog 注入 prompt，属 Agent Loop 职责）
```

### 非目标（本模块不做）

- 不做 Agent Loop（循环、停止条件、会话编排）
- 不做 prompt 组装（只接收 `messages`，不关心它们怎么拼出来）
- 不做工具执行（`tool_calls` 原样交给 `harness/tool` 的 Executor）
- 不做记忆 / 上下文压缩（属 Session 层）

---

## 2. 数据契约

### 2.1 消息

内部采用 **OpenAI Chat Completions 格式**的轻量 `Message` dataclass（与工具的 schema 导出同源，避免来回转换）：

```python
ContentPart = dict[str, Any]   # {"type": "text", "text": ...}
                               # {"type": "image_url", "image_url": {"url": ...}}

@dataclass
class Message:
    role: str                                             # system | user | assistant | tool
    content: str | list[ContentPart] | None = None         # 支持多模态（文本 + 图片）
    tool_calls: list[ToolCall] | None = None               # 仅 assistant
    tool_call_id: str | None = None                        # 仅 tool
    name: str | None = None

    def to_dict(self) -> dict[str, Any]: ...               # 转成协议 dict

    @classmethod
    def user(cls, text: str, images: Iterable[str] | None = None) -> "Message":
        """构造用户消息；images 接受 http(s) URL 或 data URL（base64）。"""
```

- 纯文本时 `content` 为 `str`；带图片时自动转成 `[{"type":"text",...}, {"type":"image_url",...}]` 的 parts 列表。
- 图片支持 URL 与 base64 data URL 两种形式；**不做本地文件读取**（交给上层或 `fs.read` 处理后再传 data URL）。

> **关键约定**：`Message.to_dict()` 产出的 `tool` 消息形如
> `{"role": "tool", "tool_call_id": "...", "content": "..."}`，
> 与 [`Executor.execute_tool_calls`](../harness/tool/executor.py) 的输出**完全一致**，可原地回填。

### 2.2 工具调用

```python
@dataclass
class ToolCall:
    id: str                          # 模型给出的，原样透传
    name: str
    arguments: str                   # JSON 字符串（保持协议原样，解析交给 Executor）

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}
```

> `to_dict()` 产出的结构正好是 `Executor._parse_call` 接受的形式（`tc["function"]["name"|"arguments"]`），
> 因此**解析 JSON 失败、参数校验失败等问题都由工具系统统一处理**，本模块不重复实现。

### 2.3 请求 / 响应

```python
@dataclass
class ChatRequest:
    messages: list[Message]
    model: str | None = None                     # 缺省用 Provider 配置的模型
    tools: list[dict[str, Any]] | None = None    # 来自 ToolRegistry.to_openai_tools()
    tool_choice: str | None = None               # auto | none | required
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)   # Provider 私有参数

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None             # stop | tool_calls | length | content_filter
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    raw: dict[str, Any] | None = None            # 原始响应，便于排查
```

### 2.4 流式事件

```python
class StreamEventType:
    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    DONE = "done"
    ERROR = "error"

@dataclass
class StreamEvent:
    type: str
    text: str | None = None                 # TEXT_DELTA
    index: int = 0                          # TOOL_CALL_DELTA 的序号
    tool_call: ToolCall | None = None       # TOOL_CALL_DELTA 片段（id/name 仅首块非空）
    usage: Usage | None = None              # DONE
    finish_reason: str | None = None        # DONE
    error: LLMError | None = None           # ERROR
```

片段由 [`StreamAccumulator`](../harness/llm/stream.py) 拼回完整 `ChatResponse`（见 §4）。

---

## 3. 接入方式与 Provider

### 3.1 只做一种接入：OpenAI Chat Completions 兼容

**所有模型都从这一个接口进来**——OpenAI、DeepSeek、Qwen、vLLM、Ollama、各类兼容网关，
差异只体现在 `base_url` / `api_key` / `model` 三项配置上，代码路径完全相同。

### 3.2 使用官方 openai SDK

依赖 `openai`（`pip install openai`），**不自行封装 HTTP**：

```python
from openai import AsyncOpenAI

class OpenAICompatProvider:
    def __init__(self, config: LLMConfig) -> None:
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,      # 指向任意兼容端点
            timeout=config.timeout_s,
            max_retries=config.max_retries,
        )

    async def complete(self, request: ChatRequest) -> ChatResponse: ...
    async def aclose(self) -> None:
        await self._client.close()
```

选官方 SDK 的原因：`chat.completions.create` 已覆盖 **tools / 多模态 / usage / 错误类型 / 重试**，
协议演进由 SDK 兜底，本模块只需做「请求组装 + 响应解析 + 错误归一化」。

### 3.3 抽象与测试缝

```python
class LLMProvider(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResponse: ...
    def astream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]: ...
    async def aclose(self) -> None: ...
```

保留协议而非直连具体类，是为了给 Agent Loop 留一个**可注入的测试缝**——
测试用 `FakeProvider` 返回预置响应，无需真实网络。

| 实现 | 说明 | 状态 |
|---|---|---|
| `OpenAICompatProvider` | 基于官方 openai SDK（`complete` + `astream`） | 已实现 |
| `FakeProvider` | 测试用，`astream` 按 `chunk_size` 切文本 | 已实现 |
| Anthropic 等 | 结构差异大，需要独立转换层 | **暂缓** |

---

## 4. 流式处理（已实现）

**`astream()` 逐块吐 `StreamEvent`**，请求自动带 `stream=True` 与
`stream_options={"include_usage": True}`；失败**抛 `LLMError`**（与 `complete()` 一致）。

SSE 每个 chunk 只带增量，`arguments` 会被切成多段：

```
chunk1: delta.tool_calls[0] = {index:0, id:"call_a", function:{name:"fs.read", arguments:""}}
chunk2: delta.tool_calls[0] = {index:0, function:{arguments:"{\"pa"}}
chunk3: delta.tool_calls[0] = {index:0, function:{arguments:"th\":\"a.txt\"}"}}
```

`StreamAccumulator` 负责重组：文本按序拼接；工具调用按 `index` 归位、`arguments` 字符串拼接、
`id` / `name` 取首个非空片段。重组结果与 `complete()` 的 `ChatResponse` **同构**，
因此 Agent Loop 的后续步骤（`tool_calls` 判断、停止条件）无需区分两条路径。

```python
acc = StreamAccumulator()
async for event in provider.astream(request):
    acc.feed(event)
response = acc.to_response()
```

`AgentLoop(LoopConfig(stream=True))` 在此之上把文本增量转发成 `text_delta` 事件（见服务层 §5.3）。

---

## 5. 错误与重试

```python
class LLMError(Exception):
    type: str            # auth | rate_limit | timeout | context_length | server | bad_request | network
    message: str
    retryable: bool
    status: int | None
```

| 类型 | 触发 | 可重试 |
|---|---|---|
| `auth` | 401 / 403 | 否 |
| `rate_limit` | 429 | 是（指数退避） |
| `timeout` | 读/连接超时 | 是 |
| `network` | 连接错误 | 是 |
| `server` | 5xx | 是 |
| `context_length` | 超出上下文 | 否（需上层裁剪） |
| `bad_request` | 400 | 否 |

- 风格与工具系统的 `ErrorInfo`（`type` / `message` / `retryable`）保持一致，便于统一回喂与观测。
- **重试交给 SDK**：官方 SDK 已内置重试与退避（`max_retries`），本模块不重复实现；
  只需把 SDK 异常**映射**成上表类型（如 `openai.RateLimitError` → `rate_limit`）。
- 映射关系（`openai` SDK 异常 → 本模块 `type`）：

| SDK 异常 | type |
|---|---|
| `AuthenticationError` / `PermissionDeniedError` | `auth` |
| `RateLimitError` | `rate_limit` |
| `APITimeoutError` | `timeout` |
| `APIConnectionError` | `network` |
| `InternalServerError` | `server` |
| `BadRequestError`（含上下文超限文案） | `bad_request` / `context_length` |

---

## 6. Token 计量

- **优先**使用服务端返回的 `usage`（OpenAI 兼容接口都有）。
- 流式场景：需在请求中带 `stream_options.include_usage`，末帧才有 usage。
- 无 usage 时**只做粗略估算**（中文≈1 字/token、ASCII≈4 字符/token、图片按固定值），并在 `Usage` 上标记为估算值，不引入 tiktoken 依赖。
- 计量结果交给 Agent Loop 做 token 预算与裁剪决策。

---

## 7. 配置

```python
@dataclass
class LLMConfig:
    model: str = "gpt-4o-mini"
    api_key: str | None = None          # 缺省读环境变量 OPENAI_API_KEY
    base_url: str | None = None         # 指向任意 OpenAI 兼容端点；None 用官方默认
    timeout_s: float = 60.0
    max_retries: int = 2                # 交给 openai SDK
    extra_headers: dict[str, str] = field(default_factory=dict)
```

**换模型 = 换配置**：任何 OpenAI 兼容服务都只改 `base_url` / `api_key` / `model` 三项，代码零改动。

- 本期由代码构造；统一配置系统（配置文件加载）在 [todo.md](../todo.md) 的 P1 中。
- `api_key` 缺省读环境变量，**不落日志**（观测层需脱敏）。

---

## 8. 目录结构

```
harness/llm/
├─ README.md          # 模块文档
├─ __init__.py        # 公共导出
├─ schema.py          # Message / ToolCall / ChatRequest / ChatResponse / Usage / StreamEvent / LLMConfig
├─ errors.py          # LLMError 家族 + openai SDK 异常映射
├─ base.py            # LLMProvider 协议 + FakeProvider（测试 / 离线）
├─ openai_compat.py   # OpenAICompatProvider（官方 openai SDK，complete + astream）
├─ stream.py          # StreamAccumulator：增量片段 → 完整 ChatResponse
└─ test/
```

> 只有一种 Provider，故不设 `providers/` 子目录；`openai_compat.py` 单文件即可。

---

## 9. 落地路线

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 契约 | `schema.py`（含多模态 `Message`）+ `errors.py`（SDK 异常映射） | ✅ 已完成 |
| M2 调用 | `base.py` 协议 + `FakeProvider`；`openai_compat.py` 的 `complete()` | ✅ 已完成 |
| M3 观测接线 | usage / 耗时上报 `Tracer` | ✅ 已完成 |
| M4 流式 | `StreamEvent` 契约 + `StreamAccumulator` + `astream()` | ✅ 已完成 |
| **暂缓** | Anthropic 等其它 Provider | — |

**最小可用 = M1 + M2**（配合 Agent Loop 即可跑通「模型 → 工具 → 回填」闭环）。

实现见 [harness/llm/README.md](../harness/llm/README.md)；
测试：`python -m harness.llm.test.test_llms`（27/27，全部离线）。

---

## 10. 决策记录

| 议题 | 结论 |
|---|---|
| `Message` 形态 | **dataclass**（+ `to_dict()`），非裸 dict |
| 流式 | **已实现**（`astream` + `StreamAccumulator`）；请求带 `stream_options.include_usage` |
| Provider 范围 | **只做 OpenAI Chat Completions 兼容**，所有模型统一从此接入 |
| HTTP 实现 | **使用官方 openai SDK**（推翻早前的 httpx 自封装方案） |
| 多模态 | **支持图片**：`Message.content` 可为 parts 列表，图片用 URL 或 data URL |

### 仍待确认

- [ ] 是否需要统一的「模型能力表」（是否支持 tools / 是否支持图片 / 上下文长度）？
- [ ] 图片是否要提供「本地文件 → data URL」的便捷函数？（当前交由上层处理）
- [ ] `tool_choice` 默认值：`auto` 还是 `required`（当传入 tools 时）？
