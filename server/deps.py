"""FastAPI 依赖：从 app.state 取组合根，以及会话 / run 的存在性校验。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from harness.session import Session, SessionNotFound

from .errors import ServiceError, not_found
from .runner import Run
from .state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.harness  # type: ignore[no-any-return]


StateDep = Annotated[AppState, Depends(get_state)]


def require_config(state: AppState) -> None:
    """轻量开关端点用：没有配置上下文就无法落盘（与 `PUT /config` 同口径）。"""
    if state.config is None or state.config_path is None:
        raise ServiceError(
            "config_unavailable", "当前 app 未持有配置上下文，无法写配置", status=409
        )


async def require_session(state: AppState, session_id: str) -> Session:
    try:
        return await state.manager.open(session_id)
    except SessionNotFound as exc:
        raise not_found("session_not_found", str(exc)) from exc


def require_run(state: AppState, run_id: str) -> Run:
    run = state.runs.get(run_id)
    if run is None:
        raise not_found("run_not_found", f"run 不存在或已回收: {run_id}")
    return run


__all__ = ["StateDep", "get_state", "require_config", "require_run", "require_session"]
