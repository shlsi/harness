"""Agent Loop：编排「模型 ↔ 工具」的循环。

无状态：messages 进、messages 出，不持有会话历史（会话由宿主管理）。
"""

from __future__ import annotations

import inspect
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

from ..llm.errors import LLMError
from ..llm.schema import ChatRequest, ChatResponse, Message, StreamEventType, Usage
from ..llm.stream import StreamAccumulator
from ..tool.schema import Context
from ..tool.telemetry import (
    RUN_SPAN,
    reset_current_parent,
    reset_current_trace,
    set_current_parent,
    set_current_trace,
)
from .events import EventHook, EventType, LoopEvent
from .guard import LoopGuard
from .prompt import PromptBuilder
from .schema import LoopConfig, RunResult, StepRecord, StopReason

if TYPE_CHECKING:
    from ..llm.base import LLMProvider
    from ..tool.executor import Executor
    from ..tool.registry import ToolRegistry
    from ..tool.telemetry import Tracer


def _add_usage(left: Usage, right: Usage) -> Usage:
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class AgentLoop:
    def __init__(
        self,
        provider: "LLMProvider",
        executor: "Executor",
        registry: "ToolRegistry",
        *,
        config: LoopConfig | None = None,
        prompt: PromptBuilder | None = None,
        on_event: EventHook | None = None,
        tracer: "Tracer | None" = None,
        should_interrupt: "Callable[[], bool] | None" = None,
    ) -> None:
        self.provider = provider
        self.executor = executor
        self.registry = registry
        self.config = config or LoopConfig()
        self.prompt = prompt or PromptBuilder()
        self._on_event = on_event
        self.tracer = tracer
        # 用户插话探测（宿主提供）。**协作式**：只在本轮工具全部执行完之后检查，
        # 不打断正在进行的模型生成与正在执行的工具（见 docs/agent-loop-design.md）。
        self._should_interrupt = should_interrupt

    async def run(
        self, messages: list[Message], ctx: Context | None = None
    ) -> RunResult:
        ctx = ctx or Context()
        # 在 run 边界设当前 trace 与根 span：Provider / Executor 拿不到 Context，
        # 靠它们把 span 关联到本次 run，并挂成以 run 为根的树。
        run_span_id = uuid.uuid4().hex[:12]
        trace_token = set_current_trace(ctx.trace_id)
        parent_token = set_current_parent(run_span_id)
        try:
            return await self._run_inner(messages, ctx, run_span_id)
        finally:
            reset_current_parent(parent_token)
            reset_current_trace(trace_token)

    async def _run_inner(
        self, messages: list[Message], ctx: Context, run_span_id: str
    ) -> RunResult:
        started = time.perf_counter()
        history = self.prompt.build(messages)
        tools = self.registry.to_openai_tools() or None
        usage = Usage()
        steps: list[StepRecord] = []
        # 每次 run 新建：guard 是有状态的，不能跨 run 复用（Loop 本身无状态）
        guard = LoopGuard(
            warn_at=self.config.repeat_warn_at, stop_at=self.config.repeat_stop_at
        )

        await self._emit(LoopEvent(type=EventType.RUN_STARTED))

        for index in range(1, self.config.max_steps + 1):
            step_started = time.perf_counter()
            try:
                response = await self._respond(self._request(history, tools), index)
            except LLMError as exc:
                # 重试由 Provider 内部完成；此处一律结束并回报，不抛异常、不重试。
                await self._emit(LoopEvent(type=EventType.ERROR, step=index, error=exc))
                return await self._finish(
                    history, steps, usage, StopReason.ERROR, None, ctx,
                    started=started, run_span_id=run_span_id, error=exc,
                )

            usage = _add_usage(usage, response.usage)
            history.append(Message.assistant(response.content, response.tool_calls))
            await self._emit(
                LoopEvent(
                    type=EventType.MODEL_RESPONSE,
                    step=index,
                    text=response.content,
                    tool_calls=list(response.tool_calls),
                    usage=response.usage,
                )
            )

            if not response.has_tool_calls():
                steps.append(StepRecord(index, response, [], _elapsed_ms(step_started)))
                return await self._finish(
                    history, steps, usage, StopReason.FINAL, response.content, ctx,
                    started=started, run_span_id=run_span_id,
                )

            # 循环保护：先判定（用于给结果附提醒），但**仍然执行工具**——
            # 每个 assistant.tool_calls 都必须有对应的 tool 消息，否则下一轮会话非法。
            verdict = guard.inspect(list(response.tool_calls))
            results = await self.executor.execute_tool_calls(response.tool_call_dicts(), ctx)
            for position, count in verdict.warnings.items():
                if 0 <= position < len(results):
                    call = response.tool_calls[position]
                    results[position] = {
                        **results[position],
                        "content": str(results[position].get("content", ""))
                        + guard.warning_text(call, count),
                    }
            history.extend(
                Message.tool(item["tool_call_id"], item["content"]) for item in results
            )
            await self._emit(
                LoopEvent(type=EventType.TOOL_FINISHED, step=index, tool_results=results)
            )

            steps.append(StepRecord(index, response, results, _elapsed_ms(step_started)))
            await self._emit(
                LoopEvent(type=EventType.STEP_FINISHED, step=index, usage=usage)
            )

            # 用户插话检查点：此刻本轮工具**已全部执行完**，assistant(tool_calls) 与 tool
            # 结果成对，历史仍然合法。用户意图优先于循环保护判定。
            if self._interrupted():
                return await self._finish(
                    history, steps, usage, StopReason.INTERRUPTED, None, ctx,
                    started=started, run_span_id=run_span_id,
                )

            if verdict.stopped:
                return await self._finish(
                    history, steps, usage, StopReason.LOOP_DETECTED, None, ctx,
                    started=started, run_span_id=run_span_id,
                )

            if self._over_budget(usage):
                return await self._finish(
                    history, steps, usage, StopReason.TOKEN_BUDGET, None, ctx,
                    started=started, run_span_id=run_span_id,
                )

        return await self._finish(
            history, steps, usage, StopReason.MAX_STEPS, None, ctx,
            started=started, run_span_id=run_span_id,
        )

    # ---- 内部 ----
    def _interrupted(self) -> bool:
        """用户插话探测。**回调异常一律视为 False**——不能因宿主缺陷而中断 Agent。"""
        if self._should_interrupt is None:
            return False
        try:
            return bool(self._should_interrupt())
        except Exception:  # noqa: BLE001
            return False

    async def _respond(self, request: ChatRequest, step: int) -> ChatResponse:
        """取模型回复。配置开启流式且 Provider 支持时，逐增量发 text_delta 事件。"""
        astream = getattr(self.provider, "astream", None)
        if not self.config.stream or astream is None:
            return await self.provider.complete(request)

        accumulator = StreamAccumulator()
        async for event in astream(request):
            accumulator.feed(event)
            if event.type == StreamEventType.TEXT_DELTA and event.text:
                await self._emit(
                    LoopEvent(type=EventType.TEXT_DELTA, step=step, text=event.text)
                )
        return accumulator.to_response()

    def _request(self, history: list[Message], tools: list[dict[str, Any]] | None) -> ChatRequest:
        return ChatRequest(
            messages=list(history),
            model=self.config.model,
            tools=tools,
            tool_choice=self.config.tool_choice if tools else None,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

    def _over_budget(self, usage: Usage) -> bool:
        limit = self.config.max_total_tokens
        return limit is not None and usage.total_tokens > limit

    async def _finish(
        self,
        history: list[Message],
        steps: list[StepRecord],
        usage: Usage,
        stop_reason: str,
        final_text: str | None,
        ctx: Context,
        *,
        started: float,
        run_span_id: str,
        error: LLMError | None = None,
    ) -> RunResult:
        await self._emit(
            LoopEvent(
                type=EventType.RUN_FINISHED,
                step=len(steps),
                text=final_text,
                usage=usage,
                stop_reason=stop_reason,
            )
        )
        if self.tracer is not None:
            self.tracer.record(
                tool=RUN_SPAN,
                span_id=run_span_id,  # 子 span 先于它记录，靠这个 id 挂上来
                ok=stop_reason != StopReason.ERROR,
                stop_reason=stop_reason,
                steps=len(steps),
                total_tokens=usage.total_tokens,
                duration_ms=_elapsed_ms(started),
                trace_id=ctx.trace_id,
                # 显式传（可能为 None）：根 span 不做「当前父」兜底，否则会自己挂自己
                parent_span_id=ctx.metadata.get("parent_span_id"),
            )
        return RunResult(
            messages=history,
            final_text=final_text,
            steps=steps,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            trace_id=ctx.trace_id,
        )

    async def _emit(self, event: LoopEvent) -> None:
        """发出事件。回调异常被吞掉——不能因前端推送失败而中断 Agent。"""
        if self._on_event is None:
            return
        try:
            result = self._on_event(event)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            pass
