"""长期记忆（`MEMORY.md`）：查看 / 整文编辑。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from harness.memory import MemoryFull, MemoryStore, entry_text, is_entry

from ..deps import StateDep
from ..errors import ServiceError

router = APIRouter(tags=["memory"])


class MemoryUpdateRequest(BaseModel):
    content: str


def _memory_body(store: MemoryStore) -> dict:
    try:
        content = store.path.read_text("utf-8") if store.path.is_file() else ""
    except OSError:
        content = ""
    doc = store.load()
    return {
        "enabled": True,
        "path": str(store.path),
        "max_chars": store.max_chars,
        "content": content,
        "chars": len(content),
        "remaining_chars": max(0, store.max_chars - len(content)),
        "sections": [
            {
                "title": section.title,
                "entries": [entry_text(line) for line in section.lines if is_entry(line)],
            }
            for section in doc.sections
        ],
        "entries": doc.entries(),
    }


@router.get("/memory")
async def get_memory(state: StateDep) -> dict:
    if state.memory is None:
        return {
            "enabled": False,
            "path": None,
            "max_chars": 0,
            "content": "",
            "chars": 0,
            "remaining_chars": 0,
            "sections": [],
            "entries": [],
        }
    return _memory_body(state.memory)


@router.put("/memory")
async def put_memory(body: MemoryUpdateRequest, state: StateDep) -> dict:
    if state.memory is None:
        raise ServiceError("memory_disabled", "长期记忆未启用（memory.enabled=false）", status=409)
    try:
        state.memory.replace(body.content)
    except MemoryFull as exc:
        raise ServiceError(
            "memory_full",
            str(exc),
            status=400,
            details={"max_chars": state.memory.max_chars},
        ) from exc
    return _memory_body(state.memory)
