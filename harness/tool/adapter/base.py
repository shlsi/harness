"""适配器抽象：让「非本地函数」的工具（MCP、进程等）接入统一注册与执行。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..schema import Context, ToolSpec


@runtime_checkable
class ToolAdapter(Protocol):
    """外部工具适配器。

    每个适配器实例绑定一个具体工具：describe() 声明其 ToolSpec，
    invoke() 负责实际调用（Executor 会套超时与追踪）。
    """

    def describe(self) -> list[ToolSpec]:
        """声明该适配器提供的工具（通常为一个）。"""
        ...

    async def invoke(self, spec: ToolSpec, args: dict[str, Any], ctx: Context) -> Any:
        """执行调用并返回原始结果，异常由 Executor 统一转为结构化错误。"""
        ...
