"""组合根：把 `HarnessConfig` 装配成可对外服务的对象图。

装配细节全部交给 [`harness.config.build`](../harness/config/build.py)，
本模块只负责「放进 AppState + 管生命周期」。依赖方向单向：`server → harness`。

配置热重建（`AppState.apply_config`）在此**原地替换**各部件（不换 AppState 对象），
这样路由里通过 `StateDep` 拿到的引用始终有效。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from harness.agent import LoopConfig, PromptBuilder
from harness.config import (
    HarnessConfig,
    build_executor,
    build_mcp_configs,
    build_memory,
    build_net_policy,
    build_prompt,
    build_provider,
    build_registry,
    build_sandbox,
    build_session,
    build_shell_policy,
    build_skills,
    build_tracer,
)
from harness.config.admin import dump_raw, ordered_config, read_yaml, validate
from harness.llm import LLMProvider
from harness.memory import MemoryStore
from harness.session import SessionManager, TitleGenerator
from harness.skill import SkillRegistry
from harness.tool import Executor, ToolRegistry, Tracer
from harness.tool.adapter.mcp import MCPServerConfig
from harness.tool.loader import LoadedServers, load_mcp_servers

from .interaction import InteractionRegistry
from .runner import RunRegistry


@dataclass
class _Parts:
    """一次装配的同步产物（不含 runs / interactions 这类随请求变的状态）。"""

    provider: LLMProvider
    registry: ToolRegistry
    manager: SessionManager
    executor: Executor
    prompt: PromptBuilder
    loop_config: LoopConfig
    tracer: Tracer
    skills: SkillRegistry | None
    memory: MemoryStore | None
    mcp_configs: list[MCPServerConfig]
    tracer_path: str | None
    heartbeat_s: float
    title_namer: TitleGenerator


def _build_parts(config: HarnessConfig) -> _Parts:
    """按配置装配同步部件。任一步抛 `ConfigError` 时，调用方状态保持无损。"""
    skills = build_skills(config)
    memory = build_memory(config)
    registry = build_registry(config, skills, memory)
    build_shell_policy(config)  # 进程级 shell 白名单/禁止模式
    build_sandbox(config)  # 进程级工作区边界 + 路径权限 + 域名黑名单（默认关闭）
    build_net_policy(config)  # 进程级 net.* 体积上限
    observability = config.observability
    tracer = build_tracer(observability)
    # 只有 JSONL sink 才有可读的 trace 文件（otel / none 没有）
    is_jsonl = observability.tracer_sink.strip().lower() == "jsonl"
    provider = build_provider(config.llm, tracer)
    return _Parts(
        provider=provider,
        registry=registry,
        manager=build_session(config, memory, tracer, provider),
        executor=build_executor(registry, config, tracer),
        prompt=build_prompt(skills, memory, config.agent.base_instructions),
        loop_config=config.agent,
        tracer=tracer,
        skills=skills,
        memory=memory,
        mcp_configs=build_mcp_configs(config),
        tracer_path=observability.tracer_path if is_jsonl else None,
        heartbeat_s=config.server.heartbeat_s,
        title_namer=TitleGenerator(provider, max_chars=config.session.title_max_chars),
    )


class AppState:
    """服务层共享状态。测试可直接构造（注入 FakeProvider）。"""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        manager: SessionManager,
        executor: Executor | None = None,
        prompt: PromptBuilder | None = None,
        loop_config: LoopConfig | None = None,
        tracer: Tracer | None = None,
        skills: SkillRegistry | None = None,
        memory: MemoryStore | None = None,
        runs: RunRegistry | None = None,
        interactions: InteractionRegistry | None = None,
        heartbeat_s: float = 15.0,
        mcp_configs: list[MCPServerConfig] | None = None,
        tracer_path: str | None = None,
        config: HarnessConfig | None = None,
        config_path: str | None = None,
        title_namer: TitleGenerator | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.tracer = tracer
        self.tracer_path = tracer_path  # 仅 jsonl sink 时非 None；供 /runs/{id}/trace?source=file
        self.executor = executor or Executor(registry, tracer=tracer)
        self.manager = manager
        self.skills = skills
        self.memory = memory
        self.prompt = prompt or PromptBuilder(skills=skills, memory=memory)
        self.loop_config = loop_config or LoopConfig()
        self.runs = runs or RunRegistry()
        self.interactions = interactions or InteractionRegistry()
        self.heartbeat_s = heartbeat_s
        self.mcp_configs = list(mcp_configs or [])
        self.loaded_servers: LoadedServers | None = None
        # 配置上下文本体与落盘路径（测试注入的 AppState 可为 None → /config 只读不可用）
        self.config = config
        self.config_path = config_path
        # 轻量开关端点的读-改-写锁（防两个 PATCH 并发丢更新）
        self.config_lock = asyncio.Lock()
        # 自动标题生成器（配置装配时创建；测试注入的 state 为 None → 不自动生成）
        self.title_namer = title_namer
        # 后台任务（如自动标题）；配置重建 / 关闭时统一取消
        self.background: set[asyncio.Task] = set()

    def _enabled_mcp_configs(self) -> list[MCPServerConfig]:
        """只连 `enabled: true` 的声明。

        `mcp_configs` 保持「全量声明」语义（`/mcp` 与 `healthz` 仍能看到停用项），
        是否连接是宿主的生命周期决策。测试注入的 state 没有 config → 视为全启用。
        """
        if self.config is None:
            return list(self.mcp_configs)
        enabled = {item.name for item in self.config.mcp.servers if item.enabled}
        return [config for config in self.mcp_configs if config.name in enabled]

    async def start(self) -> None:
        """异步装配：连接配置里**已启用**的 MCP server，成功后把其工具注册进 registry。

        与 `build_state` 分开是因为 MCP 连接是异步的，而 `create_app` 必须同步可用
        （测试用的 `httpx.ASGITransport` 不触发 lifespan）。
        """
        if self.loaded_servers is not None:
            return
        configs = self._enabled_mcp_configs()
        if configs:
            self.loaded_servers = await load_mcp_servers(configs, self.registry)

    # ---- 轻量开关（只翻一个字段 + 只热重建对应部分）----
    async def patch_config(self, mutate: Callable[[dict[str, Any]], None]) -> HarnessConfig:
        """读 YAML → 原地改字段 → 校验 → 原子落盘。**不重建任何部件**。

        供「技能 / MCP 开关」端点使用：不像 `PUT /config` 那样全量重建 provider，
        因此**不要求无活跃 run**。加锁避免并发读-改-写丢更新。
        """
        if self.config_path is None:
            raise RuntimeError("AppState 无配置上下文（config_path 为空），无法写配置")
        async with self.config_lock:
            data = read_yaml(self.config_path)
            mutate(data)
            new_config = validate(data)
            dump_raw(ordered_config(data), self.config_path)
        return new_config

    async def set_skill_enabled(self, name: str, enabled: bool) -> None:
        """切换技能停用状态并**只热切换过滤面**（`state.prompt` 持有 registry 引用，无需重建）。"""
        def mutate(data: dict[str, Any]) -> None:
            skills = data.get("skills")
            if not isinstance(skills, dict):
                skills = {}
                data["skills"] = skills
            names = [item for item in (skills.get("disabled") or []) if item != name]
            if not enabled:
                names.append(name)
            skills["disabled"] = sorted(set(names))

        new_config = await self.patch_config(mutate)
        if self.config is not None:
            self.config.skills.disabled = list(new_config.skills.disabled)
        if self.skills is not None:
            self.skills.set_disabled(new_config.skills.disabled)

    async def set_mcp_enabled(self, name: str, enabled: bool) -> list[str]:
        """切换 MCP server 的 enabled 并**只重连 MCP**（不碰 provider / 会话 / loop_config）。"""
        def mutate(data: dict[str, Any]) -> None:
            mcp = data.get("mcp")
            if not isinstance(mcp, dict):
                return
            for item in mcp.get("servers") or []:
                if isinstance(item, dict) and item.get("name") == name:
                    item["enabled"] = enabled

        new_config = await self.patch_config(mutate)
        # 必须先同步 config 再 reload_mcp：reload_mcp 里的 build_registry / _enabled_mcp_configs 都读 self.config
        if self.config is not None:
            self.config.mcp.servers = list(new_config.mcp.servers)
        return await self.reload_mcp()

    async def apply_config(
        self, config: HarnessConfig, *, reload_mcp: bool = True
    ) -> list[str]:
        """用新配置**原地重建**各部件（不换 AppState 对象）。

        先构建新部件（`ConfigError` 在此抛出，旧状态无损），再拆旧件、换新件。
        返回非致命警告（如 MCP 连不上）。
        """
        parts = _build_parts(config)

        for task in list(self.background):
            task.cancel()
        self.background.clear()
        warnings: list[str] = []
        if self.loaded_servers is not None:
            warnings.extend(await self.loaded_servers.aclose())
            self.loaded_servers = None

        old_provider = self.provider
        self.provider = parts.provider
        await old_provider.aclose()

        self.registry = parts.registry
        self.manager = parts.manager
        self.executor = parts.executor
        self.prompt = parts.prompt
        self.loop_config = parts.loop_config
        self.tracer = parts.tracer
        self.skills = parts.skills
        self.memory = parts.memory
        self.mcp_configs = parts.mcp_configs
        self.tracer_path = parts.tracer_path
        self.heartbeat_s = parts.heartbeat_s
        self.title_namer = parts.title_namer
        self.config = config
        self.runs.set_ttl(config.server.run_ttl_s)
        self.interactions.set_timeout(config.server.interaction_timeout_s)

        warnings: list[str] = []
        if reload_mcp:
            warnings = await self._reconnect_mcp()
        return warnings

    async def reload_mcp(self) -> list[str]:
        """只重连 MCP（不动 provider / 会话）：重建 registry 去掉已删 server 的残留工具，再连接。

        返回非致命告警（含"连接关不干净"这类 anyio 报错，见 `MCPClient.aclose`）。
        """
        warnings: list[str] = []
        if self.loaded_servers is not None:
            warnings.extend(await self.loaded_servers.aclose())
            self.loaded_servers = None
        if self.config is not None:
            self.registry = build_registry(self.config, self.skills, self.memory)
            self.executor = build_executor(self.registry, self.config, self.tracer)
        warnings.extend(await self._reconnect_mcp())
        return warnings

    async def _reconnect_mcp(self) -> list[str]:
        try:
            await self.start()
        except Exception as exc:  # noqa: BLE001 - MCP 连不上不等于配置非法
            return [f"MCP 连接失败：{exc}"]
        return []

    async def aclose(self) -> None:
        for task in list(self.background):
            task.cancel()
        self.background.clear()
        self.interactions.cancel_all()
        if self.loaded_servers is not None:
            await self.loaded_servers.aclose()
            self.loaded_servers = None
        await self.provider.aclose()


def build_state(config: HarnessConfig) -> AppState:
    """按配置装配对象图（同步部分；MCP 连接需再调 `await state.start()`）。"""
    parts = _build_parts(config)
    return AppState(
        provider=parts.provider,
        registry=parts.registry,
        manager=parts.manager,
        executor=parts.executor,
        prompt=parts.prompt,
        loop_config=parts.loop_config,
        tracer=parts.tracer,
        skills=parts.skills,
        memory=parts.memory,
        runs=RunRegistry(ttl_s=config.server.run_ttl_s),
        interactions=InteractionRegistry(timeout_s=config.server.interaction_timeout_s),
        heartbeat_s=parts.heartbeat_s,
        mcp_configs=parts.mcp_configs,
        tracer_path=parts.tracer_path,
        config=config,
        config_path=None,  # 由 create_app 按实际路径注入
        title_namer=parts.title_namer,
    )


__all__ = ["AppState", "build_state"]
