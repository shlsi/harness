"""可观测性层：记录每次工具调用的 span（trace 级追溯）。

本模块**只依赖标准库、不做 IO**：span 的落盘（JSONL / OTel）由 sink 负责
（见 `harness/config/sink.py`）。

`Tracer` 是**进程级内存索引**，同一份 span 数据有三个视图：
内存索引（在线查询，可能被 `max_spans` 截断）/ JSONL 日志（离线全量）/ OTel。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import time
import uuid
from typing import Any, Callable

# run 级 span 的名字：一次 run 的根节点，`summary()` 从这里取 run 的整体耗时。
RUN_SPAN = "agent.run"

# ---- 当前 trace / 当前根 span：跨 Provider / Executor 关联同一次 run ----
# Provider 拿不到 Context，无法显式传 trace_id / parent，故用 contextvar 兜底：
# `AgentLoop.run` 在 run 边界 set，`Tracer.record` 读取并写进 span。
# 设了「当前根 span」之后，其下所有 span 自动挂到它下面 —— 这才成了树而不是一堆散点。
_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_trace_id", default=None
)
_current_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_span_parent", default=None
)

# `record(parent_span_id=...)` 的哨兵：区分「没传」与「显式传 None」。
_NOT_GIVEN: Any = object()


def set_current_trace(trace_id: str | None) -> contextvars.Token:
    """设置当前 trace，返回复位用的 token（由 `AgentLoop.run` 调用）。"""
    return _current_trace.set(trace_id)


def current_trace_id() -> str | None:
    """取当前 trace；不在任何 run 内时为 None。"""
    return _current_trace.get()


def reset_current_trace(token: contextvars.Token) -> None:
    """复位到 `set_current_trace` 之前的值，避免跨 run 泄漏。"""
    _current_trace.reset(token)


def set_current_parent(span_id: str | None) -> contextvars.Token:
    """设置当前 run 的根 span id：此后记录的 span 都挂到它下面。"""
    return _current_parent.set(span_id)


def current_parent_span() -> str | None:
    """取当前根 span id；不在任何 run 内时为 None。"""
    return _current_parent.get()


def reset_current_parent(token: contextvars.Token) -> None:
    _current_parent.reset(token)


def build_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 `parent_span_id` 组树（纯函数，内存视图与文件视图共用）。

    返回根节点列表，节点形如 `{"span": <原 span>, "children": [...]}`。
    父节点缺失（被 `max_spans` 裁掉 / 文件里没写全）时挂到根——**不丢子节点**。
    """
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for span in spans:
        span_id = span.get("span_id")
        if span_id is None:
            continue
        nodes[span_id] = {"span": span, "children": []}
        order.append(span_id)

    roots: list[dict[str, Any]] = []
    for span_id in order:
        node = nodes[span_id]
        parent_id = node["span"].get("parent_span_id")
        parent = nodes.get(parent_id) if parent_id else None
        if parent is None or parent is node:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots


def summarize_spans(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """从一批 span 现算汇总（内存视图与文件视图共用，避免两份实现漂移）。

    `duration_ms` / `stop_reason` 取自 run 级 span（`agent.run`，整次 run 的墙钟时间）。
    """
    run_span = next((span for span in spans if span.get("tool") == RUN_SPAN), None)
    return {
        "span_count": len(spans),
        "error_count": sum(1 for span in spans if span.get("ok") is False),
        "duration_ms": run_span.get("duration_ms") if run_span else None,
        "stop_reason": run_span.get("stop_reason") if run_span else None,
    }


class Tracer:
    """收集调用 span，可选地通过 sink 外发（如写入 JSONL / OTel）。

    `max_spans` 限制内存中保留的 span 数（默认不限）；长跑服务建议设上限，
    超限丢弃最旧的——span 在写入时已通过 sink 外发，不会丢数据。
    因此**内存视图可能截断**：在线查询求快用它，要全量请读 JSONL 日志。
    """

    def __init__(
        self,
        sink: Callable[[dict[str, Any]], None] | None = None,
        *,
        max_spans: int | None = None,
    ) -> None:
        self.spans: list[dict[str, Any]] = []
        self._sink = sink
        self._max_spans = max_spans

    def record(
        self,
        *,
        parent_span_id: Any = _NOT_GIVEN,
        span_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        # trace 兜底：调用方（如 Provider）拿不到 Context，由 contextvar 补齐。
        if "trace_id" not in fields:
            trace_id = current_trace_id()
            if trace_id is not None:
                fields["trace_id"] = trace_id
        # 未显式指定父 span 时，挂到当前 run 的根 span（agent.run）下。
        # 显式传 None 表示「就是要当根」，不参与兜底。
        if parent_span_id is _NOT_GIVEN:
            parent_span_id = current_parent_span()
        span: dict[str, Any] = {
            "span_id": span_id or uuid.uuid4().hex[:12],
            "ts": time.time(),
            **fields,
        }
        if parent_span_id:
            # 为多 Agent / 嵌套调用预留的父子关联
            span["parent_span_id"] = parent_span_id
        self.spans.append(span)
        if self._max_spans is not None and len(self.spans) > self._max_spans:
            del self.spans[: len(self.spans) - self._max_spans]
        if self._sink is not None:
            try:
                self._sink(span)
            except Exception:
                # 观测失败不应影响主流程。
                pass
        return span

    # ---- 查询（内存视图）----
    def spans_for(self, trace_id: str) -> list[dict[str, Any]]:
        """取某次 run 的全部 span（保持记录顺序）。"""
        return [span for span in self.spans if span.get("trace_id") == trace_id]

    def tree(self, trace_id: str) -> list[dict[str, Any]]:
        """取某次 run 的 span 树。"""
        return build_tree(self.spans_for(trace_id))

    def summary(self, trace_id: str) -> dict[str, Any]:
        """某次 run 的汇总：span 数、错误数，以及 run 级耗时与停止原因。"""
        return summarize_spans(self.spans_for(trace_id))

    @staticmethod
    def digest(args: Any) -> str:
        """对入参做摘要，用于日志关联与去重键。"""
        try:
            payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = str(args)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
