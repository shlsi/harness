"""装配：`HarnessConfig` → 运行期对象。

**唯一装配点**——CLI / server / 测试都调这里，不各写一份装配代码。
"""

from __future__ import annotations

import fnmatch
from typing import Callable

from ..agent import PromptBuilder
from ..llm import LLMConfig, LLMProvider, OpenAICompatProvider
from ..memory import MemoryStore, MemoryStoreError, register_memory_tools
from ..session import JsonlSessionStore, LLMCompressor, SessionManager
from ..skill import SkillRegistry, register_skill_tools
from ..tool import Executor, Policy, ToolRegistry, Tracer, default_registry
from ..tool.adapter.mcp import MCPServerConfig as RuntimeMcpServerConfig
from ..tool.builtin import shell
from ..tool.builtin.net import NetPolicy, configure_net_policy
from ..tool.sandbox import SandboxPolicy, configure_sandbox
from .schema import ConfigError, HarnessConfig, McpServerConfig, ObservabilityConfig
from .sink import JsonlTracerSink

# ---- provider / session / skills ----
def build_provider(llm: LLMConfig, tracer: Tracer | None = None) -> LLMProvider:
    # 传入 tracer 才会记 `llm.complete` span（否则这次 run 只有 agent.run 一个 span）
    return OpenAICompatProvider(llm, tracer=tracer)


def build_session(
    config: HarnessConfig,
    memory: MemoryStore | None = None,
    tracer: Tracer | None = None,
    provider: LLMProvider | None = None,
) -> SessionManager:
    # 压缩器只有 LLMCompressor 一种（摘要质量决定压缩后还能不能继续干活），
    # 所以没有 provider 就是配置错误——不再静默降级为占位压缩器。
    if provider is None:
        raise ConfigError(
            "session",
            "会话压缩需要可用的 LLM provider（压缩器固定为 LLMCompressor）；"
            "请检查 llm 配置或不要在无 provider 时构造 SessionManager",
        )
    return SessionManager(
        JsonlSessionStore(config.server.data_dir),
        config=config.session,
        compressor=LLMCompressor(provider),
        on_memory=_memory_sink(
            memory, auto_write=config.memory.auto_extract, tracer=tracer
        ),
    )


def _memory_sink(
    memory: MemoryStore | None, *, auto_write: bool, tracer: Tracer | None
) -> Callable[[list[str]], None] | None:
    """压缩抽到候选记忆时的落点：始终记一条 span；开了 `auto_write` 才真正落盘。"""
    if memory is None:
        return None

    def handle(candidates: list[str]) -> None:
        for text in candidates:
            written = False
            if auto_write:
                try:
                    memory.save(text)
                    written = True
                except MemoryStoreError:
                    written = False  # 满了/非法：候选留在 span 里，由人处理
            if tracer is not None:
                tracer.record(tool="memory.candidate", text=text, written=written)

    return handle


def build_skills(config: HarnessConfig) -> SkillRegistry | None:
    """`skills.root` 为空白 → 关闭 skill catalog（返回 None）。"""
    root = config.skills.root.strip()
    if not root:
        return None
    return SkillRegistry(
        root,
        disabled=config.skills.disabled,
        max_desc_chars=config.skills.max_desc_chars,
        max_body_chars=config.skills.max_body_chars,
        max_resource_chars=config.skills.max_resource_chars,
    )


def build_memory(config: HarnessConfig) -> MemoryStore | None:
    """`memory.enabled: false` 或 path 为空白 → None（关闭长期记忆）。"""
    memory = config.memory
    path = memory.path.strip()
    if not memory.enabled or not path:
        return None
    return MemoryStore(path, max_chars=memory.max_chars)


def build_prompt(
    skills: SkillRegistry | None,
    memory: MemoryStore | None = None,
    base_instructions: str = "",
) -> PromptBuilder:
    return PromptBuilder(
        base_instructions=base_instructions, skills=skills, memory=memory
    )


# ---- 工具 ----
def is_tool_allowed(name: str, enabled: list[str], disabled: list[str]) -> bool:
    """工具开关：`disabled` 优先；`enabled` 留空表示全开；支持 fnmatch 通配。"""
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in disabled):
        return False
    if not enabled:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in enabled)


def build_registry(
    config: HarnessConfig,
    skills: SkillRegistry | None = None,
    memory: MemoryStore | None = None,
) -> ToolRegistry:
    """组装**独立**的 ToolRegistry，被禁用的工具根本不在表里。

    这样 Executor 的 lookup 自然返回 `tool_not_found`，核心类不必再懂「工具开关」。
    """
    enabled, disabled = config.tools.enabled, config.tools.disabled
    registry = ToolRegistry()

    for spec in default_registry.list():
        if is_tool_allowed(spec.name, enabled, disabled):
            registry.register_tool(default_registry.get(spec.name))

    # 需要注入实例的工具（skill / memory）：先注册到临时表，再按开关搬进正式表
    if skills is not None:
        staging = ToolRegistry()
        for name in register_skill_tools(staging, skills):
            if is_tool_allowed(name, enabled, disabled):
                registry.register_tool(staging.get(name))

    if memory is not None:
        staging = ToolRegistry()
        for name in register_memory_tools(staging, memory):
            if is_tool_allowed(name, enabled, disabled):
                registry.register_tool(staging.get(name))

    return registry


def build_policy(config: HarnessConfig) -> Policy:
    return Policy(
        enforce_scopes=config.tools.policy.enforce_scopes,
        approval_for_destructive=config.tools.policy.approval_for_destructive,
    )


def build_executor(
    registry: ToolRegistry, config: HarnessConfig, tracer: Tracer | None = None
) -> Executor:
    return Executor(registry, policy=build_policy(config), tracer=tracer)


def build_shell_policy(config: HarnessConfig) -> shell.ShellPolicy:
    """把 YAML 里的 shell 白名单/禁止模式应用到进程级策略（见设计文档 §7.2）。"""
    return shell.configure_shell_policy(
        allowed_commands=config.tools.shell.allowed_commands or None,
        deny_patterns=config.tools.shell.deny_patterns or None,
    )


def build_sandbox(config: HarnessConfig) -> SandboxPolicy:
    """把 YAML 里的 `tools.sandbox` 应用到进程级沙箱策略（见 docs/sandbox-design.md §6）。"""
    sandbox = config.tools.sandbox
    return configure_sandbox(
        enabled=sandbox.enabled,
        root=sandbox.root or ".",
        write_paths=sandbox.write_paths,
        deny_paths=sandbox.deny_paths,
        readonly_mounts=sandbox.readonly_mounts,
        writable_mounts=sandbox.writable_mounts,
        extra_denied_domains=sandbox.denied_domains,
    )


def build_net_policy(config: HarnessConfig) -> NetPolicy:
    """把 YAML 里的 `tools.net` 应用到进程级网络上限（域名策略在 tools.sandbox）。"""
    net = config.tools.net
    return configure_net_policy(
        max_bytes=net.max_bytes,
        max_download_bytes=net.max_download_bytes,
    )


# ---- 观测 ----
def build_tracer(observability: ObservabilityConfig) -> Tracer:
    max_spans = observability.tracer_max_spans
    sink_name = observability.tracer_sink.strip().lower()

    if sink_name in ("", "none"):
        return Tracer(max_spans=max_spans)
    if sink_name == "jsonl":
        return Tracer(sink=JsonlTracerSink(observability.tracer_path), max_spans=max_spans)
    if sink_name == "otel":
        # 延迟导入：没装 opentelemetry 也不该影响 none / jsonl 两种 sink
        from .otel_sink import OtelTracerSink

        try:
            sink = OtelTracerSink(
                service_name=observability.otel_service_name,
                endpoint=observability.otel_endpoint or None,
            )
        except RuntimeError as exc:  # 未安装可选依赖
            raise ConfigError("observability.tracer_sink", str(exc)) from exc
        return Tracer(sink=sink, max_spans=max_spans)
    raise ConfigError(
        "observability.tracer_sink",
        f"未知 sink {observability.tracer_sink!r}（可用：none / jsonl / otel）",
    )


# ---- MCP ----
def build_mcp_configs(config: HarnessConfig) -> list[RuntimeMcpServerConfig]:
    """把配置层的 MCP 声明映射成适配器要求的 `MCPServerConfig`。"""
    return [to_runtime_mcp(item) for item in config.mcp.servers]


def to_runtime_mcp(item: McpServerConfig) -> RuntimeMcpServerConfig:
    """单条 `McpServerConfig` → 运行期 `MCPServerConfig`（供 `/mcp/test` 复用）。"""
    return RuntimeMcpServerConfig(
        name=item.name,
        transport=item.transport,
        command=item.command,
        args=list(item.args),
        env=dict(item.env) or None,
        cwd=item.cwd,
        url=item.url,
        tool_prefix=item.tool_prefix,
        default_side_effect=item.default_side_effect,
        default_requires_approval=item.default_requires_approval,
        default_scopes=list(item.default_scopes),
        default_timeout_s=item.default_timeout_s,
        tool_overrides=dict(item.tool_overrides),
    )


__all__ = [
    "build_executor",
    "build_mcp_configs",
    "build_memory",
    "build_net_policy",
    "build_policy",
    "build_prompt",
    "build_provider",
    "build_registry",
    "build_sandbox",
    "build_session",
    "build_shell_policy",
    "build_skills",
    "build_tracer",
    "is_tool_allowed",
    "to_runtime_mcp",
]
