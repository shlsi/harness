"""会话管理：创建 / 打开 / 列表 / 删除，以及一轮对话的薄封装。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from ..agent.schema import RunResult
from ..llm.schema import Message
from ..tool.schema import Context
from .compact import Compressor
from .schema import (
    Session,
    SessionConfig,
    SessionEvent,
    SessionMeta,
    SessionNotFound,
    replay,
)
from .store import SessionStore

if TYPE_CHECKING:
    from ..agent.loop import AgentLoop


class SessionManager:
    def __init__(
        self,
        store: SessionStore,
        *,
        compressor: Compressor,
        config: SessionConfig | None = None,
        on_memory: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.store = store
        # 压缩器必填：只有 LLMCompressor 一种实现（见 compact.py 的说明）
        self.compressor = compressor
        self.config = config or SessionConfig()
        # 压缩时抽到候选长期记忆的回调（None = 不抽取）
        self._on_memory = on_memory

    # ---- 生命周期 ----
    async def create(
        self,
        *,
        session_id: str | None = None,
        system: list[Message] | None = None,
        title: str | None = None,
        metadata: dict | None = None,
    ) -> Session:
        session = Session.create(
            session_id=session_id, system=system, title=title, metadata=metadata
        )
        await self.store.append_events(
            session.id,
            [
                SessionEvent.meta(
                    session_id=session.id,
                    system=[message.to_dict() for message in session.system],
                    title=title,
                    metadata=dict(metadata or {}),
                )
            ],
        )
        return session

    async def open(self, session_id: str) -> Session:
        events = await self.store.load_events(session_id)
        if not events:
            raise SessionNotFound(session_id)
        return replay(session_id, events)

    async def list(self) -> list[SessionMeta]:
        return await self.store.list_sessions()

    async def delete(self, session_id: str) -> None:
        await self.store.delete(session_id)

    # ---- 写入与压缩 ----
    async def commit(self, session: Session, events: list[SessionEvent]) -> None:
        await self.store.append_events(session.id, events)

    async def maybe_compact(self, session: Session) -> list[SessionEvent]:
        """达到水位线时压缩；未触发则返回空列表。"""
        return await self.compact(session, force=False)

    async def compact(self, session: Session, *, force: bool = False) -> list[SessionEvent]:
        """把活跃区中间一段折叠成一条摘要（`force=True` 跳过阈值，供前端手动触发）。

        顺序：先把开头 `keep_head` 条固化进固定头（只做一次），再压掉
        `len(active) - keep_tail` 条。两处保护都可以是 0（= 不保护）。
        """
        if not force and not session.is_over_threshold(self.config):
            return []
        events = session.adopt_head(self.config.keep_head)
        dropped = len(session.active) - self.config.keep_tail
        if dropped <= 0:
            # 只固化了固定头、这轮没压：仍要落库，否则重放对不上
            if events:
                await self.commit(session, events)
            return events
        summary = await self._compress(list(session.active[:dropped]))
        events += session.compact(summary, dropped)
        await self.commit(session, events)
        return events

    async def _compress(self, messages: list[Message]) -> str:
        """压缩并（可选）顺带抽取候选长期记忆。

        `LLMCompressor` 支持 `compress_and_extract`；配了 `on_memory` 就用合并调用
        （一次模型调用同时产出摘要与候选），否则走纯 `compress()`。
        """
        combined = getattr(self.compressor, "compress_and_extract", None)
        if self._on_memory is None or combined is None:
            return await self.compressor.compress(messages)

        result = await combined(messages)
        if result.memories:
            try:
                self._on_memory(result.memories)
            except Exception:  # noqa: BLE001 - 记忆落点不得中断这一轮对话
                pass
        return result.summary

    # ---- 一轮对话（薄封装，不含循环、不碰工具、不改 AgentLoop）----
    async def run_turn(
        self,
        session: Session,
        user_text: str | Message,
        agent: "AgentLoop",
        *,
        ctx: Context | None = None,
    ) -> RunResult:
        user_message = (
            user_text if isinstance(user_text, Message) else Message.user(user_text)
        )
        await self.commit(session, session.append(user_message))

        await self.maybe_compact(session)

        sent = session.build_messages()
        result = await agent.run(sent, ctx=ctx or session.context())
        await self.commit(session, session.record(result, sent))
        return result
