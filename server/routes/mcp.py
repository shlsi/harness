"""MCP：声明与已连接状态；单条测试连接；重连。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from harness.config import ConfigError, McpServerConfig, build_dataclass, to_runtime_mcp
from harness.tool import ToolRegistry
from harness.tool.loader import load_mcp_servers

from ..deps import StateDep, require_config
from ..errors import ServiceError, not_found

router = APIRouter(tags=["mcp"])

TEST_TIMEOUT_S = 20.0


class McpTestRequest(BaseModel):
    server: dict[str, Any]


class McpToggleRequest(BaseModel):
    enabled: bool


def _enabled_flags(state: StateDep) -> dict[str, bool]:
    """声明的 `enabled`（无配置上下文 → 视为全启用）。"""
    if state.config is None:
        return {}
    return {item.name: item.enabled for item in state.config.mcp.servers}


@router.get("/mcp")
async def list_mcp(state: StateDep) -> dict:
    clients = {}
    if state.loaded_servers is not None:
        clients = {client.config.name: client for client in state.loaded_servers.clients}

    flags = _enabled_flags(state)
    servers = []
    for config in state.mcp_configs:
        prefix = config.tool_prefix or config.name
        tools = [name for name in state.registry.names() if name.startswith(f"{prefix}.")]
        servers.append(
            {
                "name": config.name,
                "enabled": flags.get(config.name, True),
                "transport": config.transport,
                "command": config.command,
                "url": config.url,
                "tool_prefix": config.tool_prefix,
                "connected": config.name in clients,
                "tool_count": len(tools),
                "tools": tools,
                "declared_side_effect": config.default_side_effect,
                "requires_approval": config.default_requires_approval,
                "scopes": list(config.default_scopes),
            }
        )
    return {"servers": servers, "loaded": state.loaded_servers is not None}


@router.post("/mcp/test")
async def test_mcp(body: McpTestRequest, state: StateDep) -> dict:
    """试连一条**尚未保存**的 server 声明：连上 → 列出工具 → 立即断开。"""
    raw_name = body.server.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ServiceError(
            "invalid_server", "缺少 server.name", status=422, details={"path": "name"}
        )
    try:
        item = build_dataclass(McpServerConfig, body.server, "mcp.servers[0]")
    except (ConfigError, TypeError) as exc:
        message = exc.message if isinstance(exc, ConfigError) else str(exc)
        path = exc.path if isinstance(exc, ConfigError) else "mcp.servers[0]"
        raise ServiceError(
            "invalid_server", message, status=422, details={"path": path}
        ) from exc

    runtime = to_runtime_mcp(item)
    registry = ToolRegistry()
    started = time.perf_counter()
    try:
        loaded = await asyncio.wait_for(
            load_mcp_servers([runtime], registry), timeout=TEST_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"连接超时（{TEST_TIMEOUT_S:.0f}s）", "tools": []}
    except Exception as exc:  # noqa: BLE001 - 连通性测试：失败是结果，不是服务错误
        return {"ok": False, "error": str(exc), "tools": []}

    try:
        tools = registry.names()
    finally:
        await loaded.aclose()
    return {
        "ok": True,
        "tools": tools,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@router.patch("/mcp/{name}")
async def patch_mcp(name: str, body: McpToggleRequest, state: StateDep) -> dict:
    """启用 / 停用一个 MCP server：只翻 `enabled` 一个字段并**只重连 MCP**。

    不重建 provider / 会话，因此不要求「无活跃 run」。
    """
    require_config(state)
    assert state.config is not None  # require_config 已保证
    if not any(item.name == name for item in state.config.mcp.servers):
        raise not_found("mcp_server_not_found", f"未声明的 MCP server: {name}")

    warnings = await state.set_mcp_enabled(name, body.enabled)
    if state.runs.active_count() > 0:
        warnings = [
            *warnings,
            "有 run 正在执行：其正在调用的 MCP 工具可能因连接重建而失败，下一轮起恢复正常",
        ]
    result = await list_mcp(state)
    result["warnings"] = warnings
    return result


@router.post("/mcp/reload")
async def reload_mcp(state: StateDep) -> dict:
    """按当前声明重新连接（用于改了外部环境 / 想重连）。"""
    if state.runs.active_count() > 0:
        raise ServiceError(
            "run_conflict", "有 run 正在执行，暂不能重连 MCP（会重建工具表）", status=409
        )
    warnings = await state.reload_mcp()
    body = await list_mcp(state)
    body["warnings"] = warnings
    return body
