"""适配器包。

注意：本文件只导出与 mcp SDK 无关的基础类型，保证未安装 mcp 时工具系统仍可用。
MCP 相关实现请显式导入 harness.tool.adapter.mcp。
"""

from .base import ToolAdapter

__all__ = ["ToolAdapter"]
