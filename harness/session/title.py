"""会话标题生成：首轮问答结束后，用一次轻量模型调用概括出短标题。

对齐 [`compact.py`](compact.py) 的形状：`TitleGenerator` 持 provider，
`generate()` 失败/超时一律返回 `None`（由调用方回退到 `fallback_title`）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..llm.schema import ChatRequest, Message

if TYPE_CHECKING:
    from ..llm import LLMProvider

DEFAULT_MAX_CHARS = 24
DEFAULT_TIMEOUT_S = 8.0
_MAX_INPUT_CHARS = 2000

_TITLE_PROMPT = (
    "你是会话标题生成器。用不超过 12 个汉字概括下面这段对话的主题，"
    "只输出标题本身：不要引号、不要句号、不要「标题：」之类的前缀。"
)

_STRIP_PREFIXES = ("标题：", "标题:", "Title:", "title:")
_STRIP_CHARS = "“”\"'`#*· \t\r\n"


def _render(user_text: str, assistant_text: str | None) -> str:
    parts = [f"用户：{(user_text or '').strip()}"]
    if assistant_text and assistant_text.strip():
        parts.append(f"助手：{assistant_text.strip()}")
    return "\n".join(parts)[:_MAX_INPUT_CHARS]


def _clean(text: str | None, max_chars: int) -> str | None:
    if not text:
        return None
    line = text.strip().splitlines()[0].strip()
    for prefix in _STRIP_PREFIXES:
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
    line = line.strip(_STRIP_CHARS).strip()
    if not line:
        return None
    return line[:max_chars]


def fallback_title(user_text: str, *, max_chars: int = 30) -> str:
    """LLM 不可用时的兜底标题：截断首条用户消息。"""
    text = (user_text or "").strip()
    if not text:
        return "新会话"
    first = text.splitlines()[0].strip()
    return first[:max_chars] + ("…" if len(first) > max_chars else "")


class TitleGenerator:
    """把首轮问答概括成短标题；任何异常/超时都返回 `None`（绝不抛出）。"""

    def __init__(
        self,
        provider: "LLMProvider",
        *,
        model: str | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_chars = max_chars
        self.timeout_s = timeout_s

    async def generate(self, user_text: str, assistant_text: str | None) -> str | None:
        try:
            response = await asyncio.wait_for(
                self.provider.complete(
                    ChatRequest(
                        messages=[
                            Message.system(_TITLE_PROMPT),
                            Message.user(_render(user_text, assistant_text)),
                        ],
                        model=self.model,
                        temperature=0.0,
                        max_tokens=self.max_chars,
                    )
                ),
                timeout=self.timeout_s,
            )
        except Exception:  # noqa: BLE001 - 标题失败不该影响对话
            return None
        return _clean(response.content, self.max_chars)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TIMEOUT_S",
    "TitleGenerator",
    "fallback_title",
]
