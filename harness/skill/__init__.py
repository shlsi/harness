"""Skills 系统：按需加载的能力说明书（渐进式披露）。"""

from __future__ import annotations

from .parser import (
    KNOWN_FRONTMATTER_KEYS,
    build_meta,
    parse_skill_md,
    split_frontmatter,
)
from .registry import SkillRegistry
from .schema import (
    PackError,
    Skill,
    SkillDisabled,
    SkillError,
    SkillMeta,
    SkillNotFound,
    SkillParseError,
)
from .tools import register_skill_tools

# 写操作（`writer` / `pack`）与 `lint` 一样**刻意不在这里导入**：它们都依赖 `lint`，
# 导进来会让 `python -m harness.skill.lint` 触发 runpy RuntimeWarning，
# 也会让服务启动白白加载 lint。用 `from harness.skill.pack import install_zip` 之类按需导入。

__all__ = [
    "KNOWN_FRONTMATTER_KEYS",
    "PackError",
    "Skill",
    "SkillDisabled",
    "SkillError",
    "SkillMeta",
    "SkillNotFound",
    "SkillParseError",
    "SkillRegistry",
    "build_meta",
    "parse_skill_md",
    "register_skill_tools",
    "split_frontmatter",
]
