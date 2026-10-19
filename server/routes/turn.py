"""一轮对话：POST /sessions/{id}/turns（stream 开关决定返回形态）。

会话已有 run 时不再 409：本次输入登记为该 run 的**用户插话**（软打断），
该 run 在当前工具执行完后收尾，并在 `done` 帧里把插话原样带回，由前端续发（见 §软打断）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from harness.llm import Message

from ..deps import StateDep, require_session
from ..errors import ServiceError, conflict
from ..runner import FINISHED, Run, release_interactions, run_turn
from ..schema import TurnRequest

router = APIRouter(prefix="/sessions", tags=["turn"])


def turn_result(run: Run) -> dict:
    summary = run.summary
    result = {
        "run_id": run.id,
        "session_id": run.session_id,
        "final_text": summary.get("final_text"),
        "stop_reason": summary.get("stop_reason"),
        "usage": summary.get("usage"),
        "steps": summary.get("steps", 0),
        "error": summary.get("error"),
    }
    if run.interrupts:
        result["interrupts"] = list(run.interrupts)
    return result


def _user_input(body: TurnRequest) -> Message:
    if isinstance(body.input, str):
        return Message.user(body.input, body.images or None)
    return Message(role="user", content=list(body.input))


def _interrupt_payload(body: TurnRequest) -> dict:
    """回传给前端、由前端在 `done` 后续发的用户输入（只带用户可见内容）。

    scopes / metadata 不回传：续发时前端按「当时的会话设置」重新推导即可。
    """
    return {"input": body.input, "images": list(body.images or [])}


@router.post("/{session_id}/turns")
async def create_turn(session_id: str, body: TurnRequest, state: StateDep):
    await require_session(state, session_id)

    active_id = state.runs.active_for(session_id)
    active = state.runs.get(active_id) if active_id else None
    if active is not None and not active.done.is_set():
        # 软打断：不动当前 run 的 SSE 连接，等它把当前工具跑完再由前端续发
        active.request_interrupt(_interrupt_payload(body))
        released = release_interactions(state, active)
        return JSONResponse(
            status_code=202,
            content={
                "run_id": active.id,
                "session_id": session_id,
                "events_url": f"/runs/{active.id}/events",
                "status": active.status,
                "interrupt": True,
                "released_interactions": released,
            },
        )
    if active is not None:
        # 极小窗口：run 已终态但尚未 release（finish → release 之间没有 await，正常到不了这里）
        raise conflict("run_conflict", f"会话 {session_id} 的 run 正在收尾，请稍后重试")

    run = state.runs.create(session_id)
    run.task = asyncio.create_task(
        run_turn(
            state,
            run,
            _user_input(body),
            scopes=body.scopes,
            metadata=body.metadata,
        )
    )

    if body.stream:
        return JSONResponse(
            status_code=202,
            content={
                "run_id": run.id,
                "session_id": session_id,
                "events_url": f"/runs/{run.id}/events",
                "status": run.status,
            },
        )

    # 非流式：等 run 结束再一次性返回（收到 interrupt 时上面已直接 202）
    await run.done.wait()
    if run.status == FINISHED:
        return turn_result(run)
    if run.status == "cancelled":
        return turn_result(run)
    raise ServiceError(
        "internal_error",
        (run.error or {}).get("message", "本轮对话执行失败"),
        500,
    )
