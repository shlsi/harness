"""Skills 系统的核心契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SkillError(Exception):
    """Skill 系统异常基类。"""


class SkillNotFound(SkillError):
    def __init__(self, name: str) -> None:
        super().__init__(f"skill 不存在: {name}")
        self.name = name


class SkillParseError(SkillError):
    """SKILL.md 结构或 frontmatter 非法。"""


class SkillDisabled(SkillError):
    """技能被停用（目录仍在，但不可 load / read）。

    刻意不复用 `SkillNotFound`：`skill.load` 的错误文本会直接回喂模型，
    必须能区分「没这个技能」与「这个技能被关了」。
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"skill 已停用: {name}")
        self.name = name


class PackError(SkillError):
    """zip 安装失败。带稳定 `code`（HTTP 状态由 server 层决定）。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True)
class SkillMeta:
    """skill 的元信息（由 SKILL.md frontmatter 解析得到）。"""

    name: str
    description: str
    path: Path  # skill 目录
    entry: Path  # SKILL.md 路径
    allowed_tools: tuple[str, ...] = ()
    version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    """skill 元信息 + 正文（按需加载）。"""

    meta: SkillMeta
    body: str
