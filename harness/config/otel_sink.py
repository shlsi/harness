"""OTel 桥接：把 span dict 转成 OpenTelemetry span，经 OTLP 导出。

**可选能力**：`opentelemetry-*` 不在核心依赖里（见 pyproject 的 `[otel]` extra）。
本模块本身可安全导入；只有构造 `OtelTracerSink` 时才会去导入 OTel，
未安装时抛出带安装提示的 `RuntimeError`（由 `build_tracer` 转成 `ConfigError`）。
"""

from __future__ import annotations

import contextvars
import hashlib
from typing import Any

MISSING_HINT = (
    "使用 observability.tracer_sink: otel 需要可选依赖：pip install -e \".[otel]\""
)

# 由 `_emit` 设定、被 `_DeterministicIdGenerator` 读取：
# 借此让 OTel 采用我们派生的 id，父子关联便**不依赖 span 的到达顺序**
# ——`agent.run` 是最后才记录的，而它的子 span 早就发出去了。
_pending_trace_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "otel_pending_trace_id", default=None
)
_pending_span_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "otel_pending_span_id", default=None
)

_ID_HEX_DIGITS = {"span": 16, "trace": 32}  # OTel：SpanId 8 字节 / TraceId 16 字节


def derive_id(value: str, *, kind: str) -> int:
    """把任意 id 确定性地派生成 OTel 要求的整数 id。

    同一个源 id 永远映射到同一个 OTel id——这就是「乱序也能对上父子」的根据。
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[: _ID_HEX_DIGITS[kind]], 16)


def _build_id_generator() -> Any:
    from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator

    class _DeterministicIdGenerator(IdGenerator):
        """优先用 contextvar 里预置的 id，没有才随机（如外部库自建的 span）。"""

        def __init__(self) -> None:
            self._fallback = RandomIdGenerator()

        def generate_span_id(self) -> int:
            pending = _pending_span_id.get()
            return pending if pending is not None else self._fallback.generate_span_id()

        def generate_trace_id(self) -> int:
            pending = _pending_trace_id.get()
            return pending if pending is not None else self._fallback.generate_trace_id()

    return _DeterministicIdGenerator()


class OtelTracerSink:
    """Tracer 的 OTel sink（契约与 `JsonlTracerSink` 相同：`dict -> None`）。

    - span 名取 `tool` 字段；
    - 起止时间由 `ts - duration_ms` 反推（span 只记了结束时刻）；
    - 父子按 `parent_span_id` **显式**关联，不用 `start_as_current_span` 的隐式栈；
    - 任何异常都吞掉：观测失败绝不冒泡。
    """

    def __init__(
        self,
        *,
        service_name: str = "harness",
        endpoint: str | None = None,
        exporter: Any = None,
        provider: Any = None,
    ) -> None:
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise RuntimeError(MISSING_HINT) from exc

        if provider is None:
            provider = TracerProvider(
                resource=Resource.create({"service.name": service_name}),
                id_generator=_build_id_generator(),
            )
            if exporter is None:
                exporter = _otlp_exporter(endpoint)
                processor = BatchSpanProcessor(exporter)
            else:
                # 显式注入 exporter（测试用 InMemory）→ 同步导出，断言立即可见
                processor = SimpleSpanProcessor(exporter)
            provider.add_span_processor(processor)

        self._provider = provider
        self._tracer = provider.get_tracer("harness")

    def __call__(self, span: dict[str, Any]) -> None:
        try:
            self._emit(span)
        except Exception:  # noqa: BLE001 - 与 JSONL sink 同一纪律：观测不影响主流程
            pass

    def _emit(self, span: dict[str, Any]) -> None:
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            Status,
            StatusCode,
            TraceFlags,
            set_span_in_context,
        )

        # 没有 trace_id 的 span（例如 span_none 场景）退化为自成一条 trace
        trace_id = derive_id(str(span.get("trace_id") or span.get("span_id")), kind="trace")
        span_id = derive_id(str(span.get("span_id")), kind="span")

        parent_context = None
        if span.get("parent_span_id"):
            parent_context = set_span_in_context(
                NonRecordingSpan(
                    SpanContext(
                        trace_id=trace_id,
                        span_id=derive_id(str(span["parent_span_id"]), kind="span"),
                        is_remote=True,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    )
                )
            )

        end_time_ns = int(float(span.get("ts", 0.0)) * 1_000_000_000)
        start_time_ns = end_time_ns - int(float(span.get("duration_ms") or 0.0) * 1_000_000)

        attributes: dict[str, Any] = {
            key: value
            for key, value in span.items()
            if key not in ("span_id", "parent_span_id", "ts", "duration_ms", "tool")
            and isinstance(value, (str, bool, int, float))
        }
        attributes["span_id"] = span.get("span_id")

        span_token = _pending_span_id.set(span_id)
        trace_token = _pending_trace_id.set(trace_id)
        try:
            otel_span = self._tracer.start_span(
                str(span.get("tool") or "span"),
                context=parent_context,
                start_time=start_time_ns,
                attributes=attributes,
            )
        finally:
            _pending_trace_id.reset(trace_token)
            _pending_span_id.reset(span_token)

        if span.get("ok") is False:
            otel_span.set_status(Status(StatusCode.ERROR))
        otel_span.end(end_time=end_time_ns)


def _otlp_exporter(endpoint: str | None) -> Any:
    """OTLP/HTTP exporter；endpoint 为 None 时沿用 SDK 的环境变量约定。"""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter() if endpoint is None else OTLPSpanExporter(endpoint=endpoint)


__all__ = ["MISSING_HINT", "OtelTracerSink", "derive_id"]
