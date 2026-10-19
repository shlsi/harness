"""自省与探活：/healthz、/tools、/llm/ping。（技能见 `skill.py`，配置见 `config.py`）"""

from __future__ import annotations

import time

from fastapi import APIRouter

from harness.config import is_tool_allowed
from harness.llm import ChatRequest, Message
from harness.tool import ToolSpec, default_registry

from ..deps import StateDep

router = APIRouter(tags=["meta"])

APP_VERSION = "0.1.0"
_STARTED_AT = time.time()


def _tool_dict(spec: ToolSpec, enabled: bool) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "side_effect": spec.side_effect,
        "scopes": list(spec.scopes),
        "requires_approval": spec.requires_approval,
        "tags": list(spec.tags),
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
        "timeout_s": spec.timeout_s,
        "max_retries": spec.max_retries,
        "idempotent": spec.idempotent,
        "concurrency": spec.concurrency,
        "result_max_chars": spec.result_max_chars,
        "enabled": enabled,
    }


@router.get("/healthz")
async def healthz(state: StateDep) -> dict:
    loaded = state.loaded_servers
    store_root = getattr(state.manager.store, "root", None)
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_s": round(time.time() - _STARTED_AT, 3),
        "tools": len(state.registry.names()),
        # 口径：目录里有多少个技能（含停用）；已启用数另给 skills_enabled，便于排查"为什么模型看不到"
        "skills": len(state.skills.all_names()) if state.skills is not None else 0,
        "skills_enabled": len(state.skills.names()) if state.skills is not None else 0,
        "config_path": state.config_path,
        "data_dir": str(store_root) if store_root is not None else None,
        "mcp": {
            "connected": len(loaded.clients) if loaded is not None else 0,
            "total": len(state.mcp_configs),
        },
        "active_runs": state.runs.active_count(),
    }


@router.get("/tools")
async def list_tools(state: StateDep) -> list[dict]:
    """全部内置工具 + 当前注册的扩展工具（含被禁用的），带 `enabled` 标记。"""
    enabled_pat = state.config.tools.enabled if state.config else []
    disabled_pat = state.config.tools.disabled if state.config else []

    specs: dict[str, ToolSpec] = {spec.name: spec for spec in default_registry.list()}
    for spec in state.registry.list():  # 扩展工具（skill / memory / mcp）
        specs[spec.name] = spec

    return [
        _tool_dict(specs[name], is_tool_allowed(name, enabled_pat, disabled_pat))
        for name in sorted(specs)
    ]


@router.post("/llm/ping")
async def ping_llm(state: StateDep) -> dict:
    """最小一次模型调用，验证 base_url / key / 模型名是否可用（配置面板用）。"""
    provider_config = getattr(state.provider, "config", None)
    model = state.loop_config.model or getattr(provider_config, "model", None)
    started = time.perf_counter()
    try:
        response = await state.provider.complete(
            ChatRequest(messages=[Message.user("ping")], max_tokens=1)
        )
    except Exception as exc:  # noqa: BLE001 - 连通性测试：失败是结果，不是服务错误
        return {
            "ok": False,
            "model": model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": str(exc),
        }
    return {
        "ok": True,
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "reply": (response.content or "")[:80],
    }
