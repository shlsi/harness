"""JSONL 会话存储：每会话一个文件，一行一事件，只追加。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .schema import SessionEvent, SessionMeta, SessionNotFound, replay

_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")


def _check_id(session_id: str) -> str:
    if not _SAFE_ID.match(session_id):
        raise ValueError("session_id 只允许字母、数字、下划线、连字符")
    return session_id


def _append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


class JsonlSessionStore:
    """事件以 JSONL 追加写入；重放即恢复会话状态。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{_check_id(session_id)}.jsonl"

    async def append_events(self, session_id: str, events: list[SessionEvent]) -> None:
        path = self._path(session_id)  # 先校验 session_id（空事件也要校验）
        if not events:
            return
        lines = "".join(
            json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in events
        )
        await asyncio.to_thread(_append_text, path, lines)

    async def load_events(self, session_id: str) -> list[SessionEvent]:
        path = self._path(session_id)
        if not path.is_file():
            return []
        raw = await asyncio.to_thread(path.read_text, "utf-8")
        events: list[SessionEvent] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # 跳过崩溃时可能残留的半行
            try:
                events.append(SessionEvent.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return events

    async def list_sessions(self) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for path in sorted(self.root.glob("*.jsonl")):
            events = await self.load_events(path.stem)
            if events:
                metas.append(replay(path.stem, events).meta)
        return metas

    async def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.is_file():
            await asyncio.to_thread(path.unlink)

    async def require_events(self, session_id: str) -> list[SessionEvent]:
        events = await self.load_events(session_id)
        if not events:
            raise SessionNotFound(session_id)
        return events
