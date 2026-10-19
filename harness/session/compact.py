"""上下文压缩：把活跃区头部一批消息折叠成一条摘要。

**压缩器只有 `LLMCompressor` 一种**：摘要质量直接决定压缩后模型还能不能继续干活，
不值得为了省一次模型调用而塞一段占位文本。压缩频率由 `keep_head` / `keep_tail` 控制。

`LLMCompressor.compress_and_extract()` 额外提供「一次调用同时产出摘要 + 候选长期记忆」
——复用压缩这个**上下文正要丢东西**的时刻，不新增模型调用（见
[docs/memory-system-design.md](../../docs/memory-system-design.md) §7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..llm.schema import ChatRequest, Message

if TYPE_CHECKING:
    from ..llm.base import LLMProvider


@runtime_checkable
class Compressor(Protocol):
    async def compress(self, messages: list[Message]) -> str: ...


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


_SUMMARY_PROMPT = (
    "请把下面的对话历史压缩成一段简洁摘要，保留关键事实、结论、未完成事项与重要标识符"
    "（文件路径、命令、参数）。不要加入新信息，不要客套，直接输出摘要正文。"
)

# 记忆抽取指令：与摘要共用同一次调用（压缩时顺带抽，不新增模型调用）。
_SUMMARY_AND_MEMORY_PROMPT = (
    _SUMMARY_PROMPT
    + "\n\n另外：如果这段历史里出现了**值得跨会话长期记住**的内容（用户偏好、稳定事实、"
    "结论与踩过的坑），在摘要之后另起一段逐行列出候选，每行以「记忆：」开头；"
    "没有就只写一行「记忆：无」。不要写一次性的对话内容，也不要重复摘要里的一般信息。"
)

MEMORY_PREFIX = "记忆："
_NO_MEMORY = "无"


@dataclass
class CompressionResult:
    """压缩产物：摘要 + 候选长期记忆。"""

    summary: str
    memories: list[str] = field(default_factory=list)


def split_summary_and_memories(text: str) -> CompressionResult:
    """把「摘要 + 记忆：…」的模型输出拆开（纯函数，便于测试）。"""
    summary_lines: list[str] = []
    memories: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(MEMORY_PREFIX):
            candidate = stripped[len(MEMORY_PREFIX):].strip()
            if candidate and candidate != _NO_MEMORY:
                memories.append(candidate)
            continue
        summary_lines.append(line)
    return CompressionResult(summary="\n".join(summary_lines).strip(), memories=memories)


class LLMCompressor:
    """调模型生成摘要：信息保留最好，代价是每次压缩多一次模型调用。"""

    def __init__(
        self,
        provider: "LLMProvider",
        *,
        model: str | None = None,
        max_chars: int = 800,
        max_input_chars: int = 20_000,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_chars = max_chars
        self.max_input_chars = max_input_chars

    async def compress(self, messages: list[Message]) -> str:
        request = ChatRequest(
            messages=[
                Message.system(_SUMMARY_PROMPT),
                Message.user(self._render(messages)),
            ],
            model=self.model,
            temperature=0.0,
            max_tokens=self.max_chars,
        )
        response = await self.provider.complete(request)
        return _clip((response.content or "").strip(), self.max_chars)

    async def compress_and_extract(self, messages: list[Message]) -> CompressionResult:
        """一次调用同时产出摘要与候选记忆（可选能力，`SessionManager` 用 `getattr` 探测）。

        复用压缩这个「上下文正要丢东西」的时刻，**不新增模型调用**。
        """
        request = ChatRequest(
            messages=[
                Message.system(_SUMMARY_AND_MEMORY_PROMPT),
                Message.user(self._render(messages)),
            ],
            model=self.model,
            temperature=0.0,
            max_tokens=self.max_chars,
        )
        response = await self.provider.complete(request)
        result = split_summary_and_memories(response.content or "")
        return CompressionResult(
            summary=_clip(result.summary, self.max_chars), memories=result.memories
        )

    def _render(self, messages: list[Message]) -> str:
        lines: list[str] = []
        for message in messages:
            if message.role == "tool":
                lines.append(f"[工具结果] {_clip(str(message.content or ''), 500)}")
            elif message.tool_calls:
                names = ", ".join(call.name for call in message.tool_calls)
                lines.append(f"[助手调用工具] {names}")
            else:
                lines.append(f"[{message.role}] {_clip(str(message.content or ''), 1000)}")
        return _clip("\n".join(lines), self.max_input_chars)
