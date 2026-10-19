"""Run 资源：状态快照 / 调用链 / SSE 事件流 / 取消。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from harness.config import read_spans
from harness.tool import build_tree, summarize_spans

from ..deps import StateDep, require_run
from ..errors import ServiceError
from ..runner import RUNNING, Run

router = APIRouter(prefix="/runs", tags=["run"])


@router.get("")
async def list_runs(state: StateDep, status: str = RUNNING) -> list[dict]:
    """当前运行中的 run（并发看板）。已结束的 run 请按 run_id 查询。"""
    if status != RUNNING:
        raise ServiceError(
            "invalid_status", "仅支持 status=running（已结束的 run 按 run_id 查询）"
        )
    return [
        {
            "run_id": run.id,
            "session_id": run.session_id,
            "status": run.status,
            "trace_id": run.trace_id,
            "events": len(run.frames),
        }
        for run in state.runs.list_active()
    ]


@router.get("/{run_id}")
async def get_run(run_id: str, state: StateDep) -> dict:
    run = require_run(state, run_id)
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "status": run.status,
        "events": len(run.frames),
        "summary": run.summary or None,
        "error": run.error,
    }


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str, request: Request, state: StateDep, after: int = 0
) -> EventSourceResponse:
    run = require_run(state, run_id)
    # 浏览器重连会自动带 Last-Event-ID；也支持显式 ?after=<seq>
    last_event_id = request.headers.get("last-event-id", "")
    start = int(last_event_id) if last_event_id.isdigit() else max(0, after)
    return EventSourceResponse(
        run.stream(after=start), ping=state.heartbeat_s, headers={"Cache-Control": "no-store"}
    )


@router.get("/{run_id}/trace")
async def get_run_trace(run_id: str, state: StateDep, source: str = "memory") -> dict:
    """本次 run 的调用链（span 聚合）。

    - `source=memory`（默认）：走**内存索引**，快，但可能被 `tracer_max_spans` 截断；
    - `source=file`：从 `traces.jsonl` 读**全量**，需配置了 jsonl sink。

    观测缺数据不是错误——没有 tracer / 没有 span 时返回空列表。
    """
    run = require_run(state, run_id)
    if source not in ("memory", "file"):
        raise ServiceError("invalid_source", "source 只支持 memory / file")

    if run.trace_id is None:
        return _empty_trace(run)

    if source == "file":
        if state.tracer_path is None:
            raise ServiceError(
                "trace_file_not_configured",
                "未配置 jsonl tracer sink，无法按文件取全量 span",
            )
        spans = await asyncio.to_thread(read_spans, state.tracer_path, trace_id=run.trace_id)
        return {
            "run_id": run.id,
            "trace_id": run.trace_id,
            "summary": summarize_spans(spans),
            "spans": spans,
            "tree": build_tree(spans),
        }

    if state.tracer is None:
        return _empty_trace(run)
    return {
        "run_id": run.id,
        "trace_id": run.trace_id,
        "summary": state.tracer.summary(run.trace_id),
        "spans": state.tracer.spans_for(run.trace_id),
        "tree": state.tracer.tree(run.trace_id),
    }


def _empty_trace(run: Run) -> dict:
    return {
        "run_id": run.id,
        "trace_id": run.trace_id,
        "summary": summarize_spans([]),
        "spans": [],
        "tree": [],
    }


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, state: StateDep) -> dict:
    run = require_run(state, run_id)
    if run.status == RUNNING and run.task is not None and not run.task.done():
        run.task.cancel()
        status = "cancelling"
    else:
        status = run.status
    return {"run_id": run.id, "status": status}
