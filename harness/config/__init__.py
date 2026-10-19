"""配置系统：YAML + `${ENV}` 插值 → 校验 → 装配成运行期对象。

设计见 [docs/configuration-system-design.md](../../docs/configuration-system-design.md)。
"""

from __future__ import annotations

from .build import (
    build_executor,
    build_mcp_configs,
    build_memory,
    build_net_policy,
    build_policy,
    build_prompt,
    build_provider,
    build_registry,
    build_sandbox,
    build_session,
    build_shell_policy,
    build_skills,
    build_tracer,
    is_tool_allowed,
    to_runtime_mcp,
)
from .dotenv import load_env_file
from .loader import (
    DEFAULT_CONFIG_FILE,
    ENV_OVERRIDES,
    build_dataclass,
    build_from_dict,
    load_config,
    read_yaml,
)
from .otel_sink import OtelTracerSink
from .schema import (
    ConfigError,
    HarnessConfig,
    McpConfig,
    McpServerConfig,
    MemoryConfig,
    NetConfig,
    ObservabilityConfig,
    PolicyConfig,
    SandboxConfig,
    ServerConfig,
    ShellConfig,
    SkillsConfig,
    ToolsConfig,
)
from .sink import JsonlTracerSink, read_spans, read_tree

__all__ = [
    "DEFAULT_CONFIG_FILE",
    "ENV_OVERRIDES",
    "ConfigError",
    "HarnessConfig",
    "JsonlTracerSink",
    "McpConfig",
    "McpServerConfig",
    "MemoryConfig",
    "NetConfig",
    "ObservabilityConfig",
    "OtelTracerSink",
    "PolicyConfig",
    "SandboxConfig",
    "ServerConfig",
    "ShellConfig",
    "SkillsConfig",
    "ToolsConfig",
    "build_executor",
    "build_dataclass",
    "build_from_dict",
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
    "load_config",
    "load_env_file",
    "read_spans",
    "read_tree",
    "read_yaml",
    "to_runtime_mcp",
]
