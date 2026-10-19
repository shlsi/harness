"""Prompt 组装：把基础指令、skill 目录与长期记忆拼成 system 消息。

约定：
- 工具定义**不进** system prompt（走 ChatRequest.tools 的 function calling）；
- skill 只放目录（name + description），正文由 skill.load 按需加载；
- 长期记忆全量进 system（它就是"必须始终记得的那几十条"）；
- **拼接顺序固定**（base → skill → memory），且渲染必须是确定性的
  ——顺序或字节一变就击穿 KV 前缀缓存（见 docs/memory-system-design.md §2.1）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..llm.schema import Message

if TYPE_CHECKING:
    from ..memory.store import MemoryStore
    from ..skill.registry import SkillRegistry

SKILL_SECTION_TITLE = "## 可用技能（skill）"
MEMORY_SECTION_TITLE = "## 长期记忆"


class PromptBuilder:
    def __init__(
        self,
        *,
        base_instructions: str = "",
        skills: "SkillRegistry | None" = None,
        memory: "MemoryStore | None" = None,
        include_skill_catalog: bool = True,
        skill_section_title: str = SKILL_SECTION_TITLE,
        memory_section_title: str = MEMORY_SECTION_TITLE,
    ) -> None:
        self.base_instructions = base_instructions
        self.skills = skills
        self.memory = memory
        self.include_skill_catalog = include_skill_catalog
        self.skill_section_title = skill_section_title
        self.memory_section_title = memory_section_title

    def system_prompt(self) -> str:
        parts: list[str] = []
        if self.base_instructions.strip():
            parts.append(self.base_instructions.strip())
        if self.include_skill_catalog and self.skills is not None:
            catalog = self.skills.catalog()
            if catalog:
                parts.append(f"{self.skill_section_title}\n{catalog}")
        if self.memory is not None:
            memory = self.memory.render()  # 空记忆返回 ""，不产生空节
            if memory:
                parts.append(f"{self.memory_section_title}\n{memory}")
        return "\n\n".join(parts)

    def build(self, messages: list[Message]) -> list[Message]:
        """在最前插入 system 消息；若已有文本型 system 消息则合并进去。"""
        system = self.system_prompt()
        result = list(messages)
        if not system:
            return result
        if result and result[0].role == "system" and isinstance(result[0].content, str):
            existing = result[0].content
            merged = f"{system}\n\n{existing}" if existing else system
            result[0] = Message.system(merged)
            return result
        return [Message.system(system), *result]
