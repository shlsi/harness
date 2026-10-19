"""会话管理：多轮记忆 + 上下文分区 + append-only 持久化。"""

from __future__ import annotations

from .compact import (
    MEMORY_PREFIX,
    CompressionResult,
    Compressor,
    LLMCompressor,
    split_summary_and_memories,
)
from .jsonl import JsonlSessionStore
from .manager import SessionManager
from .schema import (
    EVENT_COMPACT,
    EVENT_MESSAGE,
    EVENT_META,
    EVENT_USAGE,
    IMAGE_TOKEN_ESTIMATE,
    SUMMARY_PREFIX,
    USAGE_KEYS,
    Session,
    SessionConfig,
    SessionError,
    SessionEvent,
    SessionMeta,
    SessionNotFound,
    estimate_tokens,
    replay,
    summary_message,
)
from .store import MemorySessionStore, SessionStore
from .title import TitleGenerator, fallback_title

__all__ = [
    "EVENT_COMPACT",
    "EVENT_MESSAGE",
    "EVENT_META",
    "EVENT_USAGE",
    "IMAGE_TOKEN_ESTIMATE",
    "MEMORY_PREFIX",
    "SUMMARY_PREFIX",
    "USAGE_KEYS",
    "CompressionResult",
    "Compressor",
    "JsonlSessionStore",
    "LLMCompressor",
    "MemorySessionStore",
    "Session",
    "SessionConfig",
    "SessionError",
    "SessionEvent",
    "SessionManager",
    "SessionMeta",
    "SessionNotFound",
    "SessionStore",
    "TitleGenerator",
    "estimate_tokens",
    "fallback_title",
    "replay",
    "split_summary_and_memories",
    "summary_message",
]
