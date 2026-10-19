"""工具系统（harness.tool）公共导出。"""

from __future__ import annotations

from . import builtin  # noqa: F401 - 导入即注册内置工具
from .executor import Executor
from .normalizer import normalize, to_content, truncate
from .permission import (
    PERMISSIONS,
    READ,
    WRITE,
    WRITE_FREE,
    PermissionError_,
    approval_bypass,
    scopes_for,
)
from .permission import normalize as normalize_permission
from .policy import Policy
from .registry import RegisteredTool, ToolRegistry, default_registry, tool
from .sandbox import (
    DEFAULT_DENIED_DOMAINS,
    SandboxPolicy,
    SandboxViolation,
    check_command,
    check_url,
    configure_sandbox,
    current_sandbox,
    resolve_path,
    use_sandbox,
)
from .schema import (
    ApprovalRequest,
    AskRequest,
    Context,
    ErrorInfo,
    ErrorType,
    ToolError,
    ToolResult,
    ToolSpec,
)
from .telemetry import (
    Tracer,
    build_tree,
    current_parent_span,
    current_trace_id,
    reset_current_parent,
    reset_current_trace,
    set_current_parent,
    set_current_trace,
    summarize_spans,
)

__all__ = [
    "ApprovalRequest",
    "AskRequest",
    "Context",
    "DEFAULT_DENIED_DOMAINS",
    "ErrorInfo",
    "ErrorType",
    "Executor",
    "PERMISSIONS",
    "PermissionError_",
    "Policy",
    "READ",
    "RegisteredTool",
    "SandboxPolicy",
    "SandboxViolation",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "Tracer",
    "WRITE",
    "WRITE_FREE",
    "approval_bypass",
    "build_tree",
    "check_command",
    "check_url",
    "configure_sandbox",
    "current_parent_span",
    "current_sandbox",
    "current_trace_id",
    "default_registry",
    "normalize",
    "normalize_permission",
    "reset_current_parent",
    "reset_current_trace",
    "resolve_path",
    "scopes_for",
    "set_current_parent",
    "set_current_trace",
    "summarize_spans",
    "to_content",
    "tool",
    "truncate",
    "use_sandbox",
]
