"""把长期记忆接入工具系统：`memory.save` / `memory.forget`。

写法照 [register_skill_tools](../skill/tools.py)：**不注册到 `default_registry`**，
而是由装配期带着 `MemoryStore` 实例注册进正式表——这样记忆的落点由配置决定，
工具本身不持有全局状态。
"""

from __future__ import annotations

from ..tool.registry import ToolRegistry
from ..tool.schema import ToolSpec
from .schema import DEFAULT_SECTION
from .store import MemoryStore

SAVE_TOOL = "memory.save"
FORGET_TOOL = "memory.forget"


def register_memory_tools(
    tool_registry: ToolRegistry,
    store: MemoryStore,
    *,
    prefix: str = "memory",
) -> list[str]:
    """注册记忆工具，返回注册的工具名。"""

    def save(text: str, section: str = DEFAULT_SECTION) -> str:
        """把一条值得长期记住的内容写入长期记忆（跨会话生效）。

        Args:
            text: 一条记忆，写成一句独立陈述（如「用户要求回复用中文」）。
                一条记忆占一行，换行会被折叠。
            section: 归入的分节，如「用户偏好」「项目事实」「结论与坑」。
        """
        return store.save(text, section=section)

    def forget(text: str) -> str:
        """删除一条不再正确的长期记忆。

        Args:
            text: 要删除的记忆内容（精确匹配；没有精确命中时按子串匹配）。
        """
        return store.forget(text)

    specs = [
        (
            ToolSpec(
                name=f"{prefix}.save",
                description=(
                    "把值得**跨会话**长期记住的内容写入长期记忆：用户偏好、稳定事实、"
                    "结论与踩过的坑。它会出现在之后所有会话的系统提示里。"
                    "只写长期有效的东西，不要写一次性的对话内容或能从代码读出的信息。"
                ),
                side_effect="write",
                scopes=("memory:write",),
                result_max_chars=2000,
                tags=("memory",),
            ),
            save,
        ),
        (
            ToolSpec(
                name=f"{prefix}.forget",
                description=(
                    "删除一条不再正确的长期记忆（内容已被推翻、或写错了）。"
                    "传要删除的记忆内容即可。"
                ),
                side_effect="write",
                scopes=("memory:write",),
                result_max_chars=2000,
                tags=("memory",),
            ),
            forget,
        ),
    ]

    registered: list[str] = []
    for spec, fn in specs:
        tool_registry.register(spec, fn)
        registered.append(spec.name)
    return registered
