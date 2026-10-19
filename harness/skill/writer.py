"""SKILL.md 的**写入**（新建 / 覆盖 / 删除）与草稿校验。

`registry.py` 是只读的（扫描 / 加载 / 读资源）；写操作单独放这里，便于审阅。
**所有写操作都限制在 `root` 之内**：`name` 必须匹配 `NAME_PATTERN`（小写字母/数字/连字符），
且解析后的目录必须落在 root 下（防 `..`、绝对路径、符号链接逃逸）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

import yaml

from .lint import LintIssue, lint_skills
from .parser import NAME_PATTERN, build_meta, parse_skill_md
from .registry import ENTRY_NAME
from .schema import SkillError, SkillMeta, SkillNotFound

FRONTMATTER_TEMPLATE_BODY = (
    "## 何时使用\n\n描述触发场景。\n\n## 步骤\n\n1. …\n"
)


def safe_skill_dir(root: Path | str, name: str) -> Path:
    """把 `name` 解析成 root 内的 skill 目录；任何越界都抛 `SkillError`。

    这是**全部写操作的唯一路径入口**（`write_skill` / `delete_skill` / `pack.install_zip` 都走它），
    因此「路径穿越只有一个地方要审」这个性质得以保持。
    """
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise SkillError(f"skill 名非法（只允许小写字母/数字/连字符）: {name!r}")
    base = Path(root).expanduser().resolve()
    target = (base / name).resolve()
    if target == base or not target.is_relative_to(base):
        raise SkillError(f"路径越界，拒绝访问: {name}")
    return target


def render_skill_md(
    *,
    name: str,
    description: str,
    body: str = "",
    allowed_tools: Iterable[str] = (),
    version: str | None = None,
) -> str:
    """拼出合法的 SKILL.md 文本（frontmatter 用 YAML 生成，避免手写转义问题）。"""
    front: dict[str, object] = {"name": name, "description": description.strip()}
    tools = [str(t) for t in allowed_tools if str(t).strip()]
    if tools:
        front["allowed-tools"] = tools
    if version:
        front["version"] = str(version)
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
    return f"---\n{head}---\n\n{(body or '').strip()}\n"


def write_skill(
    root: Path | str,
    *,
    name: str,
    description: str,
    body: str = "",
    allowed_tools: Iterable[str] = (),
    version: str | None = None,
    overwrite: bool = False,
) -> Path:
    """新建 / 覆盖一个 skill。写前**先解析校验**一次，避免写出加载不了的 SKILL.md。

    覆盖时把原文件备份为 `SKILL.md.bak`（原子替换，不留半文件）。
    """
    directory = safe_skill_dir(root, name)
    entry = directory / ENTRY_NAME
    if entry.exists() and not overwrite:
        raise SkillError(f"skill 已存在: {name}")

    text = render_skill_md(
        name=name, description=description, body=body,
        allowed_tools=allowed_tools, version=version,
    )
    # 自校验：解析 + build_meta（含 name==目录名、description 非空、allowed-tools 形状）
    raw, _ = parse_skill_md_text(text)
    build_meta(directory, raw)

    directory.mkdir(parents=True, exist_ok=True)
    if entry.exists():
        shutil.copy2(entry, entry.with_name(ENTRY_NAME + ".bak"))
    tmp = directory / (ENTRY_NAME + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, entry)
    return entry


def parse_skill_md_text(text: str) -> tuple[dict, str]:
    """解析**文本**形式的 SKILL.md（不写盘），返回 (frontmatter, 正文)。"""
    from .parser import split_frontmatter

    front_text, body = split_frontmatter(text)
    try:
        raw = yaml.safe_load(front_text) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillError("frontmatter 必须是键值映射")
    return raw, body


def read_skill(root: Path | str, name: str) -> tuple[SkillMeta, str]:
    """读取某个 skill 的元信息与**未截断**的正文。"""
    directory = safe_skill_dir(root, name)
    entry = directory / ENTRY_NAME
    if not entry.is_file():
        raise SkillNotFound(name)
    raw, body = parse_skill_md(entry)
    return build_meta(directory, raw), body


def delete_skill(root: Path | str, name: str) -> None:
    """删除整个 skill 目录（含其资源文件）。"""
    directory = safe_skill_dir(root, name)
    if not directory.is_dir():
        raise SkillNotFound(name)
    shutil.rmtree(directory)


def lint_draft(
    *,
    name: str,
    description: str,
    body: str = "",
    allowed_tools: Iterable[str] = (),
    version: str | None = None,
    known_tools: Iterable[str] | None = None,
) -> list[LintIssue]:
    """对一份**尚未落盘**的草稿跑 lint（写进临时目录后复用 `lint_skills`）。"""
    tmp_root = Path(tempfile.mkdtemp(prefix="skill-draft-"))
    try:
        target = tmp_root / name if NAME_PATTERN.match(name or "") else tmp_root / "draft"
        target.mkdir(parents=True, exist_ok=True)
        text = render_skill_md(
            name=target.name, description=description, body=body,
            allowed_tools=allowed_tools, version=version,
        )
        (target / ENTRY_NAME).write_text(text, encoding="utf-8")
        return lint_skills(tmp_root, known_tools=known_tools)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


__all__ = [
    "FRONTMATTER_TEMPLATE_BODY",
    "delete_skill",
    "lint_draft",
    "parse_skill_md_text",
    "read_skill",
    "render_skill_md",
    "safe_skill_dir",
    "write_skill",
]
