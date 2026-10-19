"""工具系统的核心契约层。

只依赖标准库，保证契约零依赖、可被任意上层复用。
包含：ToolSpec（工具声明）、ToolResult / ToolError（执行结果与错误）、
Context（会话上下文）、ApprovalRequest（审批请求）以及内部异常。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from .sandbox import SandboxPolicy

SideEffect = Literal["read", "write", "destructive"]
ConcurrencyMode = Literal["parallel", "serial"]


class ErrorType:
    """统一错误类型（用常量而非 Enum，便于直接序列化回喂模型）。"""

    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_ARGS = "invalid_args"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    SANDBOX_DENIED = "sandbox_denied"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"


class ToolSystemError(Exception):
    """工具系统内部异常基类，Executor 会统一捕获并转为 ToolError。"""


class ToolNotFound(ToolSystemError):
    def __init__(self, name: str) -> None:
        super().__init__(f"工具不存在: {name}")
        self.name = name


class InvalidArgs(ToolSystemError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class PermissionDenied(ToolSystemError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ApprovalRequired(ToolSystemError):
    def __init__(self, tool: str) -> None:
        super().__init__(f"工具 {tool} 需要人工确认，但当前上下文未提供审批回调")
        self.tool = tool


@dataclass
class ApprovalRequest:
    """提交给审批回调的请求。宿主（Agent Loop）据此决定是否放行。"""

    tool: str
    args: dict[str, Any]
    reason: str = ""


# 审批回调：接收请求，返回是否放行。
ApprovalHook = Callable[[ApprovalRequest], Awaitable[bool]]


@dataclass
class AskRequest:
    """提交给宿主（如 FastAPI 前端）的提问请求。"""

    question: str
    options: tuple[str, ...] = ()


# 提问回调：接收请求，返回用户答复。
AskHook = Callable[[AskRequest], Awaitable[str]]


@dataclass
class ErrorInfo:
    """结构化错误，统一回喂给模型，便于其自纠或换工具。"""

    type: str
    message: str
    hint: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out


# 兼容设计文档中的命名 ToolError。
ToolError = ErrorInfo


@dataclass
class ToolResult:
    """一次工具调用的结果。content 面向模型，raw 面向内部（可落文件）。"""

    ok: bool
    content: Any = None
    raw: Any = None
    error: ErrorInfo | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: Any, raw: Any = None, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, raw=raw, meta=dict(meta))

    @classmethod
    def failure(cls, error: ErrorInfo, **meta: Any) -> "ToolResult":
        return cls(ok=False, error=error, meta=dict(meta))

    def to_model_content(self) -> str:
        """转成可回填到 tool 消息的字符串内容。"""
        if self.ok:
            from .normalizer import to_content

            return to_content(self.content)
        import json

        return json.dumps(
            {"ok": False, "error": self.error.to_dict() if self.error else {}},
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class ToolSpec:
    """工具声明。既用于生成 function calling 定义，也用于执行策略与权限判定。"""

    name: str
    description: str
    # 入参 JSON Schema；留空则由 registry 从函数签名自动生成。
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None

    # 执行策略
    timeout_s: float = 30.0
    max_retries: int = 0
    idempotent: bool = False
    concurrency: ConcurrencyMode = "parallel"

    # 安全策略
    side_effect: SideEffect = "read"
    requires_approval: bool = False
    scopes: tuple[str, ...] = ()

    # 面向模型的输出控制
    result_max_chars: int = 8000
    tags: tuple[str, ...] = ()


@dataclass
class Context:
    """一次会话的上下文，承载 trace、权限域、审批与提问回调。"""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    scopes: set[str] = field(default_factory=set)
    session_id: str | None = None
    approval: ApprovalHook | None = None
    ask: AskHook | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 本次调用的沙箱策略（工作区边界）。`None` = 沿用进程级默认策略。
    # 由 `Executor` 用 `use_sandbox()` 在调用工具前生效（见 harness/tool/sandbox.py）。
    sandbox: "SandboxPolicy | None" = None

    def derive_child(
        self,
        *,
        scopes: set[str] | None = None,
        session_id: str | None = None,
        sandbox: "SandboxPolicy | None" = None,
        **metadata: Any,
    ) -> "Context":
        """派生子上下文（为多 Agent 预留的隔离边界）。

        新 trace；scopes 可收窄（不传则继承）；审批 / 提问回调与沙箱策略透传
        （**必须透传**：否则派生上下文会静默回落到进程级沙箱、逃出会话工作区）；
        metadata 记录 parent_trace_id 以关联父子调用链。
        """
        return Context(
            scopes=set(self.scopes if scopes is None else scopes),
            session_id=session_id if session_id is not None else self.session_id,
            approval=self.approval,
            ask=self.ask,
            sandbox=sandbox if sandbox is not None else self.sandbox,
            metadata={**self.metadata, "parent_trace_id": self.trace_id, **metadata},
        )
