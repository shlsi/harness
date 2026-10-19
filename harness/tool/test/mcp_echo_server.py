"""测试用最小 MCP server（stdio 传输）。

由 tests/test_tool_system.py 以子进程方式拉起，用于验证 MCP 适配器端到端可用。
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo", log_level="WARNING")


@server.tool(description="原样返回输入文本")
def echo(text: str) -> str:
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
