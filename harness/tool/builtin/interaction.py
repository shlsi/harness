"""交互工具：Agent 主动向用户提问。"""

from __future__ import annotations

from ..registry import tool
from ..schema import AskRequest, Context


@tool(
    name="ask_user",
    description=(
        "当需求不明确或需要用户拍板时，向用户提问并等待答复。"
        "仅在必须澄清时使用，不要用于确认已明确的信息。"
    ),
    side_effect="read",
    tags=["interaction"],
)
async def ask_user(ctx: Context, question: str, options: list[str] | None = None) -> str:
    """向用户提问。

    Args:
        question: 要问用户的问题。
        options: 可选项列表，用户若需从选项中选择则提供。
    """
    if ctx.ask is None:
        raise RuntimeError("当前上下文未提供 ask 回调，无法向用户提问")
    request = AskRequest(question=question, options=tuple(options or ()))
    return await ctx.ask(request)
