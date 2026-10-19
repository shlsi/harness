"""会话存储协议与内存实现（append-only 事件日志）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .schema import SessionEvent, SessionMeta, replay


@runtime_checkable
class SessionStore(Protocol):
    """会话存储：只追加事件，加载时重放。"""

    async def append_events(self, session_id: str, events: list[SessionEvent]) -> None: ...

    async def load_events(self, session_id: str) -> list[SessionEvent]: ...

    async def list_sessions(self) -> list[SessionMeta]: ...

    async def delete(self, session_id: str) -> None: ...


class MemorySessionStore:
    """进程内实现，用于测试与临时会话。"""

    def __init__(self) -> None:
        self._events: dict[str, list[SessionEvent]] = {}

    async def append_events(self, session_id: str, events: list[SessionEvent]) -> None:
        if not events:
            return
        self._events.setdefault(session_id, []).extend(events)

    async def load_events(self, session_id: str) -> list[SessionEvent]:
        return list(self._events.get(session_id, []))

    async def list_sessions(self) -> list[SessionMeta]:
        return [
            replay(session_id, events).meta
            for session_id, events in sorted(self._events.items())
        ]

    async def delete(self, session_id: str) -> None:
        self._events.pop(session_id, None)

    def __contains__(self, session_id: object) -> bool:
        return isinstance(session_id, str) and session_id in self._events
