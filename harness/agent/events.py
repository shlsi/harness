"""循环事件：为后续 FastAPI SSE 预留的回传通道。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..llm.errors import LLMError
from ..llm.schema import ToolCall, Usage


class EventType:
    RUN_STARTED = "run_started"
    TEXT_DELTA = "text_delta"
    MODEL_RESPONSE = "model_response"
    TOOL_FINISHED = "tool_finished"
    STEP_FINISHED = "step_finished"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


@dataclass
class LoopEvent:
    type: str
    step: int = 0
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage | None = None
    stop_reason: str | None = None
    error: LLMError | None = None


# 同步与异步回调都支持；返回值是 awaitable 时会被 await。
EventHook = Callable[[LoopEvent], Awaitable[None] | None]
