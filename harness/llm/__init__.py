"""LLM 接入层：OpenAI Chat Completions 兼容。"""

from __future__ import annotations

from .base import FakeProvider, LLMProvider
from .errors import LLMError, LLMErrorType, map_exception
from .openai_compat import OpenAICompatProvider, to_chat_response, to_stream_event
from .schema import (
    ChatRequest,
    ChatResponse,
    ContentPart,
    LLMConfig,
    Message,
    StreamEvent,
    StreamEventType,
    ToolCall,
    Usage,
)
from .stream import StreamAccumulator

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ContentPart",
    "FakeProvider",
    "LLMConfig",
    "LLMError",
    "LLMErrorType",
    "LLMProvider",
    "Message",
    "OpenAICompatProvider",
    "StreamAccumulator",
    "StreamEvent",
    "StreamEventType",
    "ToolCall",
    "Usage",
    "map_exception",
    "to_chat_response",
    "to_stream_event",
]
