"""流式增量的累加：把一串 `StreamEvent` 拼回完整的 `ChatResponse`。

为什么单独一个模块：流式片段的重组规则（文本拼接、工具调用按 `index` 归位、
参数片段按序拼接）与 Provider、Agent Loop 都无关，属纯数据逻辑，独立后两端共用。
"""

from __future__ import annotations

from .schema import ChatResponse, StreamEvent, StreamEventType, ToolCall, Usage


class StreamAccumulator:
    """按序 feed 流式片段，最后 to_response()。"""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, ToolCall] = {}
        self.usage = Usage()
        self.finish_reason: str | None = None
        self.error: object | None = None

    def feed(self, event: StreamEvent) -> None:
        if event.type == StreamEventType.TEXT_DELTA:
            if event.text:
                self._text.append(event.text)
        elif event.type == StreamEventType.TOOL_CALL_DELTA:
            self._merge_call(event)
        elif event.type == StreamEventType.DONE:
            if event.usage is not None:
                self.usage = event.usage
            if event.finish_reason is not None:
                self.finish_reason = event.finish_reason
        elif event.type == StreamEventType.ERROR:
            self.error = event.error

    def _merge_call(self, event: StreamEvent) -> None:
        delta = event.tool_call
        if delta is None:
            return
        current = self._calls.get(event.index)
        if current is None:
            # 首个片段：id / name 必须来自这一片
            self._calls[event.index] = ToolCall(
                id=delta.id, name=delta.name, arguments=delta.arguments or ""
            )
            return
        if delta.id:
            current.id = delta.id
        if delta.name:
            current.name = delta.name
        if delta.arguments:
            current.arguments += delta.arguments

    def to_response(self) -> ChatResponse:
        return ChatResponse(
            content="".join(self._text) or None,
            tool_calls=[self._calls[i] for i in sorted(self._calls)],
            finish_reason=self.finish_reason,
            usage=self.usage,
        )
