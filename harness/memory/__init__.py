"""记忆系统：跨会话长期记忆（文件即记忆，注入 system prompt）。

设计见 [docs/memory-system-design.md](../../docs/memory-system-design.md)。
"""

from __future__ import annotations

from .schema import (
    DEFAULT_SECTION,
    DOC_TITLE,
    MemoryDoc,
    MemoryEntryInvalid,
    MemoryFull,
    MemoryStoreError,
    Section,
    entry_text,
    is_entry,
    normalize,
    parse_doc,
    render_doc,
    render_for_prompt,
)
from .store import MemoryStore
from .tools import FORGET_TOOL, SAVE_TOOL, register_memory_tools

__all__ = [
    "DEFAULT_SECTION",
    "DOC_TITLE",
    "FORGET_TOOL",
    "SAVE_TOOL",
    "MemoryDoc",
    "MemoryEntryInvalid",
    "MemoryFull",
    "MemoryStore",
    "MemoryStoreError",
    "Section",
    "entry_text",
    "is_entry",
    "normalize",
    "parse_doc",
    "register_memory_tools",
    "render_doc",
    "render_for_prompt",
]
