"""审批与提问的跨进程通道。

harness 的 `Context.approval` / `Context.ask` 是进程内回调；本模块把它们桥接成
「SSE 推 `interaction_required` 事件 + `POST /interactions/{id}` 回填」。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class _Pending:
    """一次待回填的交互。`run_id` / `default` 支撑「按 run 释放」（软打断用）。"""

    future: asyncio.Future
    run_id: str | None
    default: Any


class InteractionRegistry:
    """`interaction_id → _Pending`。审批与提问共用一套通道。"""

    def __init__(self, *, timeout_s: float = 300.0) -> None:
        self._timeout_s = timeout_s
        self._pending: dict[str, _Pending] = {}

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    def set_timeout(self, timeout_s: float) -> None:
        """更新等待超时（配置热重建时调用；对已在等待的交互不追溯）。"""
        self._timeout_s = timeout_s

    def open(self, *, run_id: str | None = None, default: Any = None) -> tuple[str, asyncio.Future]:
        """登记一次待回填的交互，返回 (id, future)。

        `default` 是超时 / 被软打断时的兜底值（审批用 False，提问用 ""）。
        """
        interaction_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[interaction_id] = _Pending(future=future, run_id=run_id, default=default)
        return interaction_id, future

    def resolve(self, interaction_id: str, value: Any) -> bool:
        """由 POST 端点回填。已消费 / 超时 / 未知 id 均返回 False（一次消费）。"""
        pending = self._pending.pop(interaction_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(value)
        return True

    def resolve_for_run(self, run_id: str) -> list[str]:
        """用各自 `default` 兑现某个 run 上全部未决交互，返回被释放的 id 列表。

        软打断时立即调用：否则模型会卡在审批/提问上等满 `timeout_s`（默认 300s），
        用户体感是「打断没反应」。
        """
        released: list[str] = []
        for interaction_id, pending in list(self._pending.items()):
            if pending.run_id != run_id:
                continue
            self._pending.pop(interaction_id, None)
            if not pending.future.done():
                pending.future.set_result(pending.default)
            released.append(interaction_id)
        return released

    def cancel_all(self) -> None:
        """取消所有待回填交互（服务关闭时用）。"""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

    async def wait(self, interaction_id: str, future: asyncio.Future, *, default: Any) -> Any:
        """等待回填；超时返回 default（安全默认，见设计文档 §6）。

        不捕获 CancelledError —— run 被取消时必须继续向上传播。
        """
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_s)
        except asyncio.TimeoutError:
            return default
        finally:
            self._pending.pop(interaction_id, None)
