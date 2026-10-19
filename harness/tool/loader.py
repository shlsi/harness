"""插件加载：从配置加载外部工具（当前支持 MCP server）。"""

from __future__ import annotations

from typing import Iterable

from .adapter.mcp import MCPClient, MCPServerConfig, build_tool_specs
from .registry import ToolRegistry


class LoadedServers:
    """已加载的 MCP server 集合，负责统一关闭连接。"""

    def __init__(self) -> None:
        self.clients: list[MCPClient] = []

    async def aclose(self) -> list[str]:
        """关闭全部连接，返回非致命告警（见 `MCPClient.aclose`）。

        单个 client 关不掉不阻断其余 client，也不向上抛——否则「重连 / 停用 MCP」会 500。
        """
        warnings: list[str] = []
        for client in list(self.clients):
            warning = await client.aclose()
            if warning:
                warnings.append(warning)
        self.clients.clear()
        return warnings


async def load_mcp_servers(
    configs: Iterable[MCPServerConfig], registry: ToolRegistry
) -> LoadedServers:
    """连接配置中的 MCP server，把其工具注册进 registry。

    任一步失败会回滚已建立的连接并抛出异常。
    """
    loaded = LoadedServers()
    try:
        for config in configs:
            client = MCPClient(config)
            await client.connect()
            loaded.clients.append(client)
            for spec, adapter in build_tool_specs(client):
                registry.register_external(spec, adapter)
    except Exception:
        await loaded.aclose()
        raise
    return loaded
