"""LLM 接入层的核心契约。

消息与工具调用结构**对齐 OpenAI Chat Completions 协议**，从而：
- `Message` / `ToolCall` 与 harness/tool 的输入输出零转换直通；
- 所有 OpenAI 兼容模型共用同一套数据结构。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .errors import LLMError

# 多模态内容片段：{"type": "text", ...} 或 {"type": "image_url", ...}
ContentPart = dict[str, Any]


@dataclass
class ToolCall:
    """模型返回的工具调用。arguments 保持 JSON 字符串原样，解析交给工具系统。"""

    id: str
    name: str
    arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        function = data.get("function") or {}
        arguments = function.get("arguments", data.get("arguments", ""))
        if not isinstance(arguments, str):
            import json

            arguments = json.dumps(arguments, ensure_ascii=False)
        return cls(
            id=str(data.get("id") or ""),
            name=str(function.get("name") or data.get("name") or ""),
            arguments=arguments,
        )


@dataclass
class Message:
    """一条对话消息。content 为 str 时是纯文本，为列表时是多模态片段。"""

    role: str
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        # 统一表示：空列表与 None 等价，避免 to_dict/from_dict 往返后不相等
        if not self.tool_calls:
            self.tool_calls = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        return data

    # ---- 构造便捷方法 ----
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        tool_calls = [ToolCall.from_dict(raw) for raw in (data.get("tool_calls") or [])]
        return cls(
            role=str(data.get("role", "user")),
            content=data.get("content"),
            tool_calls=tool_calls or None,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, text: str, images: Iterable[str] | None = None) -> "Message":
        """构造用户消息。images 接受 http(s) URL 或 data URL（base64）。"""
        urls = list(images or ())
        if not urls:
            return cls(role="user", content=text)
        parts: list[ContentPart] = [{"type": "text", "text": text}]
        parts.extend({"type": "image_url", "image_url": {"url": url}} for url in urls)
        return cls(role="user", content=parts)

    @classmethod
    def assistant(
        cls, content: str | None = None, tool_calls: list[ToolCall] | None = None
    ) -> "Message":
        return cls(role="assistant", content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=tool_call_id)


@dataclass
class ChatRequest:
    """一次模型请求。tools 直接来自 ToolRegistry.to_openai_tools()。"""

    messages: list[Message]
    model: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self, default_model: str) -> dict[str, Any]:
        """组装成 chat.completions.create 的关键字参数。"""
        data: dict[str, Any] = {
            "model": self.model or default_model,
            "messages": [message.to_dict() for message in self.messages],
        }
        if self.tools:
            data["tools"] = self.tools
        if self.tool_choice:
            data["tool_choice"] = self.tool_choice
        if self.temperature is not None:
            data["temperature"] = self.temperature
        if self.max_tokens is not None:
            data["max_tokens"] = self.max_tokens
        if self.stop:
            data["stop"] = self.stop
        if self.extra:
            data.update(self.extra)
        return data


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    raw: dict[str, Any] | None = None

    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def tool_call_dicts(self) -> list[dict[str, Any]]:
        """转成 Executor.execute_tool_calls 可直接消费的格式。"""
        return [call.to_dict() for call in self.tool_calls]


class StreamEventType:
    """流式事件类型。"""

    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    DONE = "done"
    ERROR = "error"


@dataclass
class StreamEvent:
    """流式增量事件。

    OpenAI 兼容流式把一次回复拆成若干片段，本结构描述**单个片段**：

    - `TEXT_DELTA`：一段文本增量（`text`）；
    - `TOOL_CALL_DELTA`：某个工具调用的参数增量（`index` 定位第几个调用，`tool_call` 为片段，
      `id` / `name` 仅首次出现时非空，`arguments` 是字符串片段）；
    - `DONE`：流结束（`usage` / `finish_reason`）；
    - `ERROR`：流中断（`error`）。

    片段由 [`StreamAccumulator`](stream.py) 拼回完整的 `ChatResponse`。
    """

    type: str
    text: str | None = None
    index: int = 0
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error: "LLMError | None" = None


@dataclass
class LLMConfig:
    """OpenAI 兼容接入配置。换模型只改 base_url / api_key / model。"""

    model: str = "gpt-4o-mini"
    api_key: str | None = None
    base_url: str | None = None
    timeout_s: float = 60.0
    max_retries: int = 2
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        return self.api_key or os.environ.get("OPENAI_API_KEY")
