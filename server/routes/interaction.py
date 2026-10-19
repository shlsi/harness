"""交互回填：POST /interactions/{id}（审批 approved，提问 answer）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import StateDep
from ..errors import ServiceError, not_found
from ..schema import InteractionRequest

router = APIRouter(prefix="/interactions", tags=["interaction"])


@router.post("/{interaction_id}")
async def resolve_interaction(
    interaction_id: str, body: InteractionRequest, state: StateDep
) -> dict:
    if body.approved is None and body.answer is None:
        raise ServiceError("invalid_request", "必须提供 approved 或 answer", 422)

    value = body.approved if body.approved is not None else body.answer
    if not state.interactions.resolve(interaction_id, value):
        raise not_found("interaction_not_found", f"交互请求不存在或已失效: {interaction_id}")
    return {"ok": True}
