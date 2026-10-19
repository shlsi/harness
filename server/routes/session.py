"""会话资源：创建 / 列表 / 详情 / 消息视图 / 改名与设置 / 压缩 / 删除。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response

from harness.llm import Message
from harness.session import EVENT_COMPACT, Session, estimate_tokens
from harness.tool.permission import PermissionError_, normalize as normalize_permission

from ..deps import StateDep, require_session
from ..errors import ServiceError, conflict
from ..schema import CompactSessionRequest, CreateSessionRequest, UpdateSessionRequest
from ..state import AppState

router = APIRouter(prefix="/sessions", tags=["session"])

#: 会话设置类 metadata 的合法键（其余键原样透传，不做解释）
WORKSPACE_KEY = "workspace"
PERMISSION_KEY = "permission"


def _section(messages: list[Message]) -> dict[str, int]:
    return {"count": len(messages), "tokens": estimate_tokens(messages)}


def session_detail(session: Session, state: AppState) -> dict:
    """SessionMeta + 分区统计 + 上下文占用 + 压缩水位 + 预算。"""
    config = state.manager.config
    built = session.build_messages()
    conversation = list(session.head) + list(session.active)
    active_tokens = estimate_tokens(conversation)

    data = session.meta.to_dict()
    data.update(
        active_count=len(session.active),
        head_count=len(session.head),
        has_summary=session.summary is not None,
        system_count=len(session.system),
        active_run_id=state.runs.active_for(session.id),
        context={
            "system": _section(list(session.system)),
            "head": _section(list(session.head)),
            "summary": _section([session.summary] if session.summary is not None else []),
            "active": _section(list(session.active)),
            "conversation_count": len(conversation),
            "conversation_tokens": active_tokens,
            "message_count": len(built),
            "total_tokens": estimate_tokens(built),
        },
        compaction={
            "enabled": config.enable_compaction,
            "compact_threshold": config.compact_threshold,
            "keep_head": config.keep_head,
            "keep_tail": config.keep_tail,
            "max_active_tokens": config.max_active_tokens,
            "over_threshold": session.is_over_threshold(config),
            "remaining_messages": config.compact_threshold - len(conversation),
            "remaining_tokens": (
                None
                if config.max_active_tokens is None
                else config.max_active_tokens - active_tokens
            ),
        },
        budget=_budget(state, session),
    )
    return data


def _budget(state: AppState, session: Session) -> dict:
    limit = state.loop_config.max_total_tokens
    used = session.meta.usage_totals.get("total_tokens", 0)
    return {
        "max_total_tokens": limit,
        "used_total_tokens": used,
        "remaining": None if limit is None else limit - used,
    }


@router.post("", status_code=201)
async def create_session(body: CreateSessionRequest, state: StateDep) -> dict:
    system = [Message.from_dict(raw) for raw in (body.system or [])]
    session = await state.manager.create(
        title=body.title, system=system, metadata=_clean_metadata(body.metadata)
    )
    return session_detail(session, state)


@router.get("")
async def list_sessions(state: StateDep) -> list[dict]:
    return [meta.to_dict() for meta in await state.manager.list()]


@router.get("/{session_id}")
async def get_session(session_id: str, state: StateDep) -> dict:
    return session_detail(await require_session(state, session_id), state)


def _clean_metadata(raw: dict) -> dict:
    """校验并规范化会话设置类 metadata；其余键原样透传。

    - `workspace`：必须是**存在的目录**，存规范化后的绝对路径（空串 = 清除该设置）
    - `permission`：必须是三档之一（空串 / None = 清除该设置）
    """
    cleaned: dict = {}
    for key, value in raw.items():
        if key == WORKSPACE_KEY:
            text = "" if value is None else str(value).strip()
            if not text:
                cleaned[key] = ""
                continue
            resolved = Path(text).expanduser().resolve()
            if not resolved.is_dir():
                raise ServiceError(
                    "invalid_workspace",
                    f"工作区目录不存在或不是目录: {text}",
                    422,
                    {"path": WORKSPACE_KEY, "value": text},
                )
            cleaned[key] = str(resolved)
        elif key == PERMISSION_KEY:
            try:
                cleaned[key] = normalize_permission("" if value is None else value) or ""
            except PermissionError_ as exc:
                raise ServiceError(
                    "invalid_permission", str(exc), 422, {"path": PERMISSION_KEY}
                ) from exc
        else:
            cleaned[key] = value
    return cleaned


@router.patch("/{session_id}")
async def update_session(
    session_id: str, body: UpdateSessionRequest, state: StateDep
) -> dict:
    """改名与/或合并 metadata（控制台的「工作区 + 权限档位」走这里）。"""
    if body.title is None and body.metadata is None:
        raise ServiceError(
            "invalid_request", "至少需要提供 title 或 metadata 之一", 422
        )
    if state.runs.active_for(session_id) is not None:
        raise conflict("run_conflict", "会话正在执行，暂不能修改设置")
    session = await require_session(state, session_id)

    events = []
    if body.title is not None:
        events += session.rename(body.title.strip())
    if body.metadata is not None:
        events += session.set_metadata(**_clean_metadata(body.metadata))
    if events:
        await state.manager.commit(session, events)
    return session_detail(session, state)


@router.post("/{session_id}/compact")
async def compact_session(
    session_id: str, body: CompactSessionRequest, state: StateDep
) -> dict:
    if state.runs.active_for(session_id) is not None:
        raise conflict("run_conflict", "会话正在执行，暂不能压缩")
    session = await require_session(state, session_id)

    before = len(session.active)
    events = await state.manager.compact(session, force=body.force)
    # compact 事件里带 dropped，直接从事件里取，避免依赖前后差
    dropped = sum(
        int(event.data.get("dropped", 0)) for event in events if event.type == EVENT_COMPACT
    )
    response: dict = {
        "session_id": session_id,
        "compacted": dropped > 0,
        "forced": body.force,
        "dropped": dropped,
        "summary_chars": len(session.summary.content) if session.summary else 0,
        "active_before": before,
    }
    if dropped == 0:
        response["reason"] = "nothing_to_drop" if body.force else "below_threshold"
    if body.force and not state.manager.config.enable_compaction:
        response["warning"] = "enable_compaction=false，本次为手动强制压缩"
    response["detail"] = session_detail(session, state)
    return response


@router.get("/{session_id}/messages")
async def get_messages(session_id: str, state: StateDep, after: int = 0) -> dict:
    session = await require_session(state, session_id)
    messages = session.build_messages()[max(0, after) :]
    return {"messages": [message.to_dict() for message in messages]}


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, state: StateDep) -> Response:
    await require_session(state, session_id)
    await state.manager.delete(session_id)
    return Response(status_code=204)
