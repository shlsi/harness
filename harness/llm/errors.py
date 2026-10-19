"""统一的 LLM 错误类型，以及官方 openai SDK 异常到本模块类型的映射。

映射按**异常类名 + status_code**判定，不硬依赖 openai 包，
因此未安装 SDK 时本模块仍可导入与测试。
"""

from __future__ import annotations

from typing import Any


class LLMErrorType:
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER = "server"
    CONTEXT_LENGTH = "context_length"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


RETRYABLE_TYPES = frozenset(
    {
        LLMErrorType.RATE_LIMIT,
        LLMErrorType.TIMEOUT,
        LLMErrorType.NETWORK,
        LLMErrorType.SERVER,
    }
)


class LLMError(Exception):
    """模型调用错误。字段风格与工具系统的 ErrorInfo 对齐。"""

    def __init__(
        self,
        type: str,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.retryable = retryable
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.status is not None:
            data["status"] = self.status
        return data


# 官方 SDK 异常类名 → 本模块类型
_NAME_TO_TYPE = {
    "AuthenticationError": LLMErrorType.AUTH,
    "PermissionDeniedError": LLMErrorType.AUTH,
    "RateLimitError": LLMErrorType.RATE_LIMIT,
    "APITimeoutError": LLMErrorType.TIMEOUT,
    "APIConnectionError": LLMErrorType.NETWORK,
    "InternalServerError": LLMErrorType.SERVER,
    "BadRequestError": LLMErrorType.BAD_REQUEST,
    "NotFoundError": LLMErrorType.BAD_REQUEST,
    "UnprocessableEntityError": LLMErrorType.BAD_REQUEST,
}

_CONTEXT_LENGTH_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
)


def _looks_like_context_length(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _CONTEXT_LENGTH_HINTS)


def _type_from_name(name: str) -> str | None:
    """按异常类名判定类型；先精确匹配，再按后缀兼容子类/包装类。"""
    if name in _NAME_TO_TYPE:
        return _NAME_TO_TYPE[name]
    for suffix, error_type in _NAME_TO_TYPE.items():
        if name.endswith(suffix):
            return error_type
    return None


def map_exception(exc: Exception) -> LLMError:
    """把 SDK / 网络异常映射成 LLMError。"""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None
    message = str(exc) or name

    error_type = _type_from_name(name)
    if error_type is None and status is not None:
        if status in (401, 403):
            error_type = LLMErrorType.AUTH
        elif status == 429:
            error_type = LLMErrorType.RATE_LIMIT
        elif status == 408:
            error_type = LLMErrorType.TIMEOUT
        elif status == 400:
            error_type = LLMErrorType.BAD_REQUEST
        elif 500 <= status < 600:
            error_type = LLMErrorType.SERVER

    if error_type == LLMErrorType.BAD_REQUEST and _looks_like_context_length(message):
        error_type = LLMErrorType.CONTEXT_LENGTH

    if error_type is None:
        lowered_name = name.lower()
        if "timeout" in lowered_name:
            error_type = LLMErrorType.TIMEOUT
        elif "connection" in lowered_name:
            error_type = LLMErrorType.NETWORK

    if error_type is None:
        if isinstance(exc, TimeoutError):
            error_type = LLMErrorType.TIMEOUT
        elif isinstance(exc, (ConnectionError, OSError)):
            error_type = LLMErrorType.NETWORK
        else:
            error_type = LLMErrorType.UNKNOWN

    return LLMError(
        error_type,
        message,
        retryable=error_type in RETRYABLE_TYPES,
        status=status,
    )
