"""OpenAI Chat Completions 兼容接入，基于官方 openai SDK。

所有模型（OpenAI / DeepSeek / Qwen / vLLM / Ollama / 兼容网关）都从这里接入，
差异只在 LLMConfig 的 base_url / api_key / model。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from .errors import map_exception
from .schema import (
    ChatRequest,
    ChatResponse,
    LLMConfig,
    StreamEvent,
    StreamEventType,
    ToolCall,
    Usage,
)

if TYPE_CHECKING:
    from ..tool.telemetry import Tracer


def _usage_from(obj: Any) -> Usage:
    return Usage(
        prompt_tokens=getattr(obj, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(obj, "completion_tokens", 0) or 0,
        total_tokens=getattr(obj, "total_tokens", 0) or 0,
    )


def to_stream_event(chunk: Any) -> StreamEvent | None:
    """把 SDK 的 ChatCompletionChunk 转成一个 StreamEvent（无有效增量时返回 None）。"""
    choices = getattr(chunk, "choices", None) or []
    choice = choices[0] if choices else None
    delta = getattr(choice, "delta", None) if choice is not None else None
    finish_reason = getattr(choice, "finish_reason", None) if choice is not None else None
    usage_obj = getattr(chunk, "usage", None)

    # stream_options.include_usage 会在末尾额外推一个 choices 为空的用量块
    if usage_obj is not None:
        return StreamEvent(
            type=StreamEventType.DONE, usage=_usage_from(usage_obj), finish_reason=finish_reason
        )
    if delta is None:
        return None

    raw_calls = getattr(delta, "tool_calls", None) or []
    if raw_calls:
        # OpenAI 一次只推一个 index 的增量
        raw = raw_calls[0]
        function = getattr(raw, "function", None)
        return StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA,
            index=getattr(raw, "index", 0) or 0,
            tool_call=ToolCall(
                id=getattr(raw, "id", "") or "",
                name=getattr(function, "name", "") or "",
                arguments=getattr(function, "arguments", "") or "",
            ),
        )

    text = getattr(delta, "content", None)
    if text:
        return StreamEvent(type=StreamEventType.TEXT_DELTA, text=text)
    if finish_reason is not None:
        return StreamEvent(type=StreamEventType.DONE, finish_reason=finish_reason)
    return None


def to_chat_response(completion: Any) -> ChatResponse:
    """把 SDK 的 ChatCompletion 对象转成本模块的 ChatResponse。"""
    choices = getattr(completion, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None) if choice is not None else None

    tool_calls: list[ToolCall] = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        tool_calls.append(
            ToolCall(
                id=getattr(call, "id", "") or "",
                name=getattr(function, "name", "") or "",
                arguments=getattr(function, "arguments", "") or "",
            )
        )

    usage_obj = getattr(completion, "usage", None)
    usage = _usage_from(usage_obj) if usage_obj is not None else Usage()

    raw = completion.model_dump() if hasattr(completion, "model_dump") else None

    return ChatResponse(
        content=getattr(message, "content", None) if message is not None else None,
        tool_calls=tool_calls,
        finish_reason=getattr(choice, "finish_reason", None) if choice is not None else None,
        usage=usage,
        model=getattr(completion, "model", None),
        raw=raw,
    )


class OpenAICompatProvider:
    """OpenAI 兼容 Provider。重试与超时交给官方 SDK。"""

    def __init__(self, config: LLMConfig, *, tracer: "Tracer | None" = None) -> None:
        # 延迟导入：未安装 openai 时，本模块其余部分（契约/错误映射）仍可用。
        from openai import AsyncOpenAI

        self.config = config
        self._tracer = tracer
        kwargs: dict[str, Any] = {
            # 本地推理服务常不需要 key，用占位符避免 SDK 直接报错。
            "api_key": config.resolved_api_key() or "EMPTY",
            "timeout": config.timeout_s,
            "max_retries": config.max_retries,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.extra_headers:
            kwargs["default_headers"] = config.extra_headers
        self._client = AsyncOpenAI(**kwargs)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        payload = request.payload(self.config.model)
        model = payload["model"]
        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - 统一映射为 LLMError
            error = map_exception(exc)
            self._record(model, started, ok=False, error_type=error.type)
            raise error from exc

        response = to_chat_response(completion)
        self._record(
            model,
            started,
            ok=True,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            tool_calls=len(response.tool_calls),
        )
        return response

    async def aclose(self) -> None:
        await self._client.close()

    # ---- 流式 ----
    async def astream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """流式请求。失败向上抛 LLMError（与 complete 一致，由调用方统一处理）。"""
        payload = request.payload(self.config.model)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        model = payload["model"]
        started = time.perf_counter()
        try:
            stream = await self._client.chat.completions.create(**payload)
            async for chunk in stream:
                event = to_stream_event(chunk)
                if event is not None:
                    yield event
        except Exception as exc:  # noqa: BLE001 - 统一映射为 LLMError
            error = map_exception(exc)
            self._record(model, started, ok=False, error_type=error.type, stream=True)
            raise error from exc
        self._record(model, started, ok=True, stream=True)

    def _record(self, model: str, started: float, *, ok: bool, **fields: Any) -> None:
        if self._tracer is None:
            return
        self._tracer.record(
            tool="llm.complete",
            model=model,
            ok=ok,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            **fields,
        )
