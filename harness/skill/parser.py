"""SKILL.md 解析：拆出 frontmatter（YAML）与正文，并校验元信息。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .schema import SkillMeta, SkillParseError

# name 约定：小写字母/数字，单词间用连字符
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 已识别的 frontmatter 字段，其余透传到 SkillMeta.metadata
KNOWN_FRONTMATTER_KEYS = {"name", "description", "allowed-tools", "allowed_tools", "version"}


def split_frontmatter(text: str) -> tuple[str, str]:
    """把 SKILL.md 拆成 (frontmatter 文本, 正文)。要求文件以独占一行的 --- 包裹。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillParseError("SKILL.md 必须以 --- 开头的 frontmatter 起始")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip("\n")
    raise SkillParseError("frontmatter 未闭合（缺少结束的 ---）")


def build_meta(directory: Path, raw: dict[str, Any]) -> SkillMeta:
    """校验并规范化 frontmatter 字段。"""
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillParseError("frontmatter 缺少 name")
    name = name.strip()
    if not NAME_PATTERN.match(name):
        raise SkillParseError(f"name 非法（应为小写字母/数字/连字符）: {name}")
    if name != directory.name:
        raise SkillParseError(f"name({name}) 与目录名({directory.name})不一致")

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillParseError("frontmatter 缺少 description")

    allowed = raw.get("allowed-tools", raw.get("allowed_tools", [])) or []
    if isinstance(allowed, str):
        allowed = [allowed]
    if not isinstance(allowed, (list, tuple)):
        raise SkillParseError("allowed-tools 应为列表")

    version = raw.get("version")
    metadata = {k: v for k, v in raw.items() if k not in KNOWN_FRONTMATTER_KEYS}

    return SkillMeta(
        name=name,
        description=description.strip(),
        path=directory,
        entry=directory / "SKILL.md",
        allowed_tools=tuple(str(t) for t in allowed),
        version=str(version) if version is not None else None,
        metadata=metadata,
    )


def parse_skill_md(path: Path) -> tuple[dict[str, Any], str]:
    """解析 SKILL.md，返回 (frontmatter 字典, 正文)。

    用 `utf-8-sig` 读：Windows 编辑器（记事本 / PowerShell `Set-Content`）可能写入 BOM，
    而 BOM 会让首行变成 `\\ufeff---`、frontmatter 判定失败，skill 就静默加载不了。
    """
    text = path.read_text(encoding="utf-8-sig")
    frontmatter_text, body = split_frontmatter(text)
    try:
        raw = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillParseError("frontmatter 必须是键值映射")
    return raw, body


def load_meta(directory: Path, entry: Path) -> SkillMeta:
    """读取并校验某个 skill 目录的元信息。"""
    raw, _ = parse_skill_md(entry)
    return build_meta(directory, raw)
