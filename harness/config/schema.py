"""配置契约：分节 dataclass 与配置错误。

**纯数据，不做 IO，不引入第三方依赖**——因此 MCP 那节在这里另立一份数据结构
（`McpServerConfig`），由 `build.py` 映射成 `harness.tool.adapter.mcp.MCPServerConfig`；
这样「读配置 / 打印配置」不必把 mcp SDK 拉进来。

各模块既有的 dataclass（`LLMConfig` / `LoopConfig` / `SessionConfig`）直接复用，不重复定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agent.schema import LoopConfig
from ..llm.schema import LLMConfig
from ..session.schema import SessionConfig


class ConfigError(Exception):
    """配置错误。带字段路径，可直接打印给用户。"""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


@dataclass
class SkillsConfig:
    root: str = "skills"  # 空字符串 → 关闭 skill catalog
    # 停用名单：目录仍在、管理端点仍可见，但不进 catalog、不可 load/read（见 harness/skill/registry.py）
    disabled: list[str] = field(default_factory=list)
    max_desc_chars: int = 300
    max_body_chars: int = 60_000
    max_resource_chars: int = 100_000


@dataclass
class PolicyConfig:
    enforce_scopes: bool = True
    approval_for_destructive: bool = True


@dataclass
class ShellConfig:
    """shell.run 的白名单 / 禁止模式；留空表示沿用内置默认值。"""

    allowed_commands: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


@dataclass
class SandboxConfig:
    """工作区边界 + 路径权限（见 docs/sandbox-design.md）。

    默认关闭：开启后相对路径的基准会从进程 cwd 变成 root，属于语义变化，故 opt-in。
    """

    enabled: bool = False
    root: str = "."  # 工作区根，相对启动目录
    write_paths: list[str] = field(default_factory=list)  # root 内写白名单 glob；空 = 全可写
    deny_paths: list[str] = field(default_factory=list)  # 最高优先级黑名单 glob，读写都拒
    readonly_mounts: list[str] = field(default_factory=list)  # root 之外的额外只读目录
    writable_mounts: list[str] = field(default_factory=list)  # root 之外的额外可写目录
    # 追加到内置 SSRF 基线黑名单（不能覆盖基线）；与 enabled 无关，始终生效
    denied_domains: list[str] = field(default_factory=list)


@dataclass
class NetConfig:
    """`net.fetch` / `net.download` 的体积上限（域名策略见 tools.sandbox.denied_domains）。"""

    max_bytes: int = 200_000  # net.fetch 单次读取上限
    max_download_bytes: int = 20_000_000  # net.download 单文件上限


@dataclass
class ToolsConfig:
    enabled: list[str] = field(default_factory=list)  # 留空 = 全开；支持 fnmatch 通配
    disabled: list[str] = field(default_factory=list)  # 优先级高于 enabled
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    net: NetConfig = field(default_factory=NetConfig)


@dataclass
class McpServerConfig:
    """一个 MCP server 的声明（镜像 tool.adapter.mcp.MCPServerConfig 的字段）。"""

    name: str
    enabled: bool = True  # false → 声明保留（管理端点 / healthz 仍可见），但不连接、不注册工具
    transport: str = "stdio"  # stdio | http
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    tool_prefix: str | None = None
    default_side_effect: str = "read"
    default_requires_approval: bool = False
    default_scopes: list[str] = field(default_factory=list)
    default_timeout_s: float = 60.0
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class MemoryConfig:
    """跨会话长期记忆：一个 Markdown 文件，内容注入 system prompt。"""

    enabled: bool = True
    path: str = "MEMORY.md"  # 相对项目根；文件不存在视为空记忆，首次 save 时创建
    max_chars: int = 4000  # 超限拒绝写入（不静默截断）
    auto_extract: bool = False  # 压缩时抽到的候选记忆是否直接写入（默认只记录）


@dataclass
class ObservabilityConfig:
    tracer_sink: str = "none"  # none | jsonl | otel
    tracer_path: str = "data/traces.jsonl"
    tracer_max_spans: int = 500  # 内存中保留的 span 上限（防长跑膨胀）
    # 仅 tracer_sink: otel 时生效；endpoint 留空则沿用 SDK 的环境变量约定
    otel_endpoint: str | None = None
    otel_service_name: str = "harness"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: str = "data/sessions"
    heartbeat_s: float = 15.0
    interaction_timeout_s: float = 300.0
    run_ttl_s: float = 600.0
    # 启动后预热一次模型连接（建好 TLS/连接池）。**只对支持前缀缓存的 provider 才有意义**，
    # 对不做缓存的 provider（如 agnes）收益被 prefill 延迟淹没，故默认关闭。
    warmup_on_start: bool = False
    # 空闲保活间隔（秒）：>0 时每隔该时长、且**无活跃 run**时打一次 mini ping。0 = 关闭
    keepalive_s: float = 0.0


@dataclass
class HarnessConfig:
    """顶层配置。每节对应 YAML 里的同名键。"""

    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: LoopConfig = field(default_factory=LoopConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def section_names(self) -> list[str]:
        return [
            "llm",
            "agent",
            "session",
            "skills",
            "memory",
            "tools",
            "mcp",
            "observability",
            "server",
        ]
