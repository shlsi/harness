"""`LoopEvent` → SSE 帧的映射，以及 `Usage` 的序列化辅助。"""

from __future__ import annotations

from typing import Any

from harness.agent import EventType, LoopEvent
from harness.llm import Usage


def usage_dict(usage: Usage | None) -> dict[str, int] | None:
    """`Usage` 没有 to_dict()，在服务层补齐（不改 harness）。"""
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


# LoopEvent.type → SSE event 名。命名分歧只在这一张表里。
# 注意：不要用 `error` 作事件名——它与 EventSource 内建的 error 事件撞名，
# 前端无法区分「服务端业务错误」与「连接断开」，故用 `run_error`。
EVENT_NAMES: dict[str, str] = {
    EventType.RUN_STARTED: "run_started",
    EventType.TEXT_DELTA: "text_delta",
    EventType.MODEL_RESPONSE: "message",
    EventType.TOOL_FINISHED: "tool_finished",
    EventType.STEP_FINISHED: "step_finished",
    EventType.RUN_FINISHED: "done",
    EventType.ERROR: "run_error",
}


def to_sse_event(event: LoopEvent) -> tuple[str, dict[str, Any]] | None:
    """把 LoopEvent 映射成 (SSE 事件名, 载荷)；未知类型返回 None。"""
    name = EVENT_NAMES.get(event.type)
    if name is None:
        return None

    if event.type == EventType.RUN_STARTED:
        return name, {}
    if event.type == EventType.TEXT_DELTA:
        return name, {"step": event.step, "text": event.text}
    if event.type == EventType.MODEL_RESPONSE:
        return name, {
            "step": event.step,
            "text": event.text,
            "tool_calls": [call.to_dict() for call in event.tool_calls],
        }
    if event.type == EventType.TOOL_FINISHED:
        return name, {"step": event.step, "tool_results": event.tool_results}
    if event.type == EventType.STEP_FINISHED:
        return name, {"step": event.step, "usage": usage_dict(event.usage)}
    if event.type == EventType.RUN_FINISHED:
        return name, {
            "stop_reason": event.stop_reason,
            "final_text": event.text,
            "usage": usage_dict(event.usage),
        }
    if event.type == EventType.ERROR:
        return name, {"error": event.error.to_dict() if event.error else None}
    return None
