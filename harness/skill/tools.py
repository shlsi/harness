"""把 skill 能力以只读工具的形式接入工具系统。

方案 C：catalog 由宿主注入 system prompt，这里只暴露两个只读工具——
`skill.load`（取正文）与 `skill.read`（取 skill 内资源）。不提供 `skill.list`。
"""

from __future__ import annotations

from ..tool.registry import ToolRegistry
from ..tool.schema import ToolSpec
from .registry import SkillRegistry

LOAD_TOOL = "skill.load"
READ_TOOL = "skill.read"


def register_skill_tools(
    tool_registry: ToolRegistry,
    skills: SkillRegistry,
    *,
    prefix: str = "skill",
) -> list[str]:
    """把 skill.load / skill.read 注册进工具注册表，返回注册的工具名。"""

    def load_skill(name: str) -> str:
        """加载指定 skill 的完整说明（SKILL.md 正文）。

        Args:
            name: skill 名称，取值见系统提示中的 skill 目录。
        """
        skill = skills.load(name)
        return skill.body

    def read_resource(name: str, path: str) -> str:
        """读取某个 skill 目录内的资源文件（参考文档、脚本等）。

        Args:
            name: skill 名称。
            path: 相对该 skill 目录的文件路径，如 reference.md。
        """
        return skills.read_resource(name, path)

    specs = [
        (
            ToolSpec(
                name=f"{prefix}.load",
                description=(
                    "加载指定 skill 的完整说明（步骤、输出格式、注意事项）。"
                    "当任务与系统提示中某个 skill 的用途匹配时，先调用本工具获取其正文，"
                    "再按正文指示使用其他工具执行。"
                ),
                side_effect="read",
                result_max_chars=60_000,
                tags=("skill",),
            ),
            load_skill,
        ),
        (
            ToolSpec(
                name=f"{prefix}.read",
                description=(
                    "读取某个 skill 目录内的资源文件（参考文档、脚本、模板等）。"
                    "仅在 skill 正文中提及该文件时使用。"
                ),
                side_effect="read",
                result_max_chars=100_000,
                tags=("skill",),
            ),
            read_resource,
        ),
    ]

    registered: list[str] = []
    for spec, fn in specs:
        tool_registry.register(spec, fn)
        registered.append(spec.name)
    return registered
