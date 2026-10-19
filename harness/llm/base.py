"""Provider 抽象与测试用实现。"""

from __future__ import annotations

from typing import AsyncIterator, Iterable, Protocol, runtime_checkable

from .schema import ChatRequest, ChatResponse, StreamEvent, StreamEventType


@runtime_checkable
class LLMProvider(Protocol):
    """模型调用接口。保留协议是为了给 Agent Loop 一个可注入的测试缝。"""

    async def complete(self, request: ChatRequest) -> ChatResponse: ...

    def astream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """流式返回增量事件。实现为 async generator；失败时抛 LLMError。"""
        ...

    async def aclose(self) -> None: ...


class FakeProvider:
    """测试 / 离线用：按预设队列返回响应，并记录收到的请求。

    `astream` 会把队列里的响应切成文本片段吐出，用于离线验证流式链路。
    """

    def __init__(
        self,
        responses: Iterable[ChatResponse] | None = None,
        *,
        default: ChatResponse | None = None,
        chunk_size: int = 4,
    ) -> None:
        self._queue = list(responses or [])
        self._default = default if default is not None else ChatResponse(content="")
        self.chunk_size = max(1, chunk_size)
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def complete(self, request: ChatRequest) -> ChatResponse:
        return self._next(request)

    async def astream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        response = self._next(request)
        content = response.content or ""
        for start in range(0, len(content), self.chunk_size):
            yield StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text=content[start : start + self.chunk_size],
            )
        for index, call in enumerate(response.tool_calls):
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_DELTA, index=index, tool_call=call
            )
        yield StreamEvent(
            type=StreamEventType.DONE,
            usage=response.usage,
            finish_reason=response.finish_reason,
        )

    async def aclose(self) -> None:
        self.closed = True

    def _next(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return self._default
