"""MCP 适配器：把外部 MCP server 的工具接入本工具系统。

依赖官方 mcp SDK（stdio / streamable-http 两种传输）。
权限策略默认「宽松」（read、免审批），可在 MCPServerConfig 中按 server 或按工具加严。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..schema import Context, ToolSpec


@dataclass
class MCPServerConfig:
    """一个 MCP server 的连接与权限配置。"""

    name: str
    transport: str = "stdio"  # stdio | http
    # stdio 传输
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    # http 传输
    url: str | None = None

    # 注册名前缀，默认用 name；最终工具名为 "{prefix}.{原始工具名}"
    tool_prefix: str | None = None

    # 默认权限（宽松）
    default_side_effect: str = "read"
    default_requires_approval: bool = False
    default_scopes: list[str] = field(default_factory=list)
    default_timeout_s: float = 60.0

    # 按工具名覆盖，如 {"write_file": {"side_effect": "destructive", "requires_approval": True}}
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


def _render_result(result: Any) -> Any:
    """把 MCP 的 CallToolResult 转成本工具系统的原始结果。"""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(block.text or "")
        elif block_type == "image":
            parts.append(f"[图片: {getattr(block, 'mime_type', 'unknown')}]")
        elif block_type in ("resource", "resource_link"):
            resource = getattr(block, "resource", block)
            text = getattr(resource, "text", None)
            parts.append(text if text is not None else f"[资源: {getattr(resource, 'uri', '?')}]")
        else:
            parts.append(str(block))

    if getattr(result, "is_error", False):
        raise RuntimeError("MCP 工具返回错误: " + ("\n".join(parts) or "未知错误"))
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    return structured if structured is not None else ""


class MCPClient:
    """维护一个 MCP server 的长连接，负责握手、发现工具与调用。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.session: ClientSession | None = None
        self._stack = AsyncExitStack()
        self._raw_tools: list[types.Tool] = []

    async def connect(self) -> None:
        cfg = self.config
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"MCP server '{cfg.name}' 缺少 command")
            params = StdioServerParameters(
                command=cfg.command, args=list(cfg.args), env=cfg.env, cwd=cfg.cwd
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        elif cfg.transport in ("http", "streamable_http"):
            if not cfg.url:
                raise ValueError(f"MCP server '{cfg.name}' 缺少 url")
            # `streamable_http_client` 的返回值在不同 SDK 版本里可能是
            # (read, write) 或 (read, write, get_session_id)——只取前两个，忽略其余。
            entered = await self._stack.enter_async_context(
                streamable_http_client(cfg.url)
            )
            read, write = entered[0], entered[1]
        else:
            raise ValueError(f"不支持的 MCP 传输方式: {cfg.transport}")

        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self._raw_tools = await self._list_all_tools()

    async def _list_all_tools(self) -> list[types.Tool]:
        assert self.session is not None
        tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await self.session.list_tools(params=params)
            tools.extend(result.tools or [])
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.session is None:
            raise RuntimeError(f"MCP server '{self.config.name}' 尚未连接")
        result = await self.session.call_tool(name, arguments or {})
        return _render_result(result)

    @property
    def raw_tools(self) -> list[types.Tool]:
        return list(self._raw_tools)

    async def aclose(self) -> str | None:
        """关闭连接；返回非致命告警（`None` 表示干净关闭）。

        MCP 的 stdio 客户端内部用 anyio 的 task group，而 anyio 的 cancel scope
        **必须在创建它的同一个 task 里退出**。宿主通常在 lifespan 任务里连接、在请求任务里
        关闭（重连 / 停用 MCP），此时 anyio 会抛 `RuntimeError`；但那时子进程已终止、
        连接实际已断，所以这里记为告警而不是让请求 500。
        """
        try:
            await self._stack.aclose()
        except RuntimeError as exc:
            self.session = None
            return (
                f"MCP '{self.config.name}' 连接已断开；关闭时底层库报错（已忽略，不影响使用）：{exc}"
            )
        self.session = None
        return None


class MCPToolAdapter:
    """把单个 MCP 工具适配为 ToolAdapter。"""

    def __init__(self, client: MCPClient, remote_name: str, spec: ToolSpec) -> None:
        self._client = client
        self._remote_name = remote_name
        self._spec = spec

    def describe(self) -> list[ToolSpec]:
        return [self._spec]

    async def invoke(self, spec: ToolSpec, args: dict[str, Any], ctx: Context) -> Any:
        return await self._client.call_tool(self._remote_name, args)


def build_tool_specs(client: MCPClient) -> list[tuple[ToolSpec, MCPToolAdapter]]:
    """把 MCP server 暴露的工具转成 (ToolSpec, 适配器) 列表。"""
    cfg = client.config
    prefix = cfg.tool_prefix or cfg.name
    built: list[tuple[ToolSpec, MCPToolAdapter]] = []
    for remote in client.raw_tools:
        override = cfg.tool_overrides.get(remote.name, {})
        spec = ToolSpec(
            name=f"{prefix}.{remote.name}",
            description=remote.description or f"MCP 工具 {remote.name}（来自 {cfg.name}）",
            input_schema=dict(remote.input_schema or {"type": "object", "properties": {}}),
            output_schema=remote.output_schema,
            timeout_s=override.get("timeout_s", cfg.default_timeout_s),
            side_effect=override.get("side_effect", cfg.default_side_effect),
            requires_approval=override.get(
                "requires_approval", cfg.default_requires_approval
            ),
            scopes=tuple(override.get("scopes", cfg.default_scopes)),
            result_max_chars=override.get("result_max_chars", 8000),
            tags=tuple(override.get("tags", ["mcp", cfg.name])),
        )
        built.append((spec, MCPToolAdapter(client, remote.name, spec)))
    return built
