"""`skill lint`：静态检查 SKILL.md（**只读，不修改任何文件**）。

为什么需要它：`SkillRegistry.scan()` 遇到坏 skill 只把提示塞进 `registry.warnings`，
而这个列表**没有任何出口**——服务 `/healthz` 只报 `skills: 0`，分不清"没有 skill"
与"有 skill 但全写坏了"。本模块把那份提示（外加 `scan()` 查不出的隐患）变成
**能定位到 skill 的清单**。

用法：

    python -m harness.skill.lint [root]     # root 默认 skills；有问题退出码非 0

代码里也能直接用：`lint_skills(root, known_tools=registry.names())`。

检查项（`code` 是稳定标识）：
`missing-root` / `missing-entry` / `parse-error` / `empty-body`（error）；
`unknown-field` / `body-too-long` / `description-truncated` / `description-too-short` /
`unknown-tool`（warning）。

> 「两个目录声明同一个 name」**不需要检查**：`build_meta` 强制 `name` 必须等于目录名，
> 同级目录不可能重名，所以那条规则天然不可达。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .parser import KNOWN_FRONTMATTER_KEYS, build_meta, parse_skill_md
from .registry import ENTRY_NAME
from .schema import SkillParseError

ERROR = "error"
WARNING = "warning"

DEFAULT_MAX_BODY_CHARS = 60_000
DEFAULT_MAX_DESC_CHARS = 300
# description 短于此长度，模型难以判断"该不该选用这个 skill"
MIN_DESC_CHARS = 20


@dataclass(frozen=True)
class LintIssue:
    """一条检查结果。`code` 是稳定标识，便于脚本按类型过滤。"""

    skill: str
    level: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.skill}: {self.message} ({self.code})"


def lint_skills(
    root: Path | str,
    *,
    known_tools: Iterable[str] | None = None,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    max_desc_chars: int = DEFAULT_MAX_DESC_CHARS,
) -> list[LintIssue]:
    """扫描 `root` 并返回全部问题；空列表表示没问题。

    `known_tools` 给了才检查 `allowed-tools` 是否已注册（服务里工具集合随配置变化，
    所以命令行默认不查，避免 MCP / 被开关关掉的工具造成误报）。
    """
    base = Path(root)
    if not base.is_dir():
        return [_issue(base.name or str(base), ERROR, "missing-root", f"skill 根目录不存在: {base}")]

    known = set(known_tools) if known_tools is not None else None
    issues: list[LintIssue] = []

    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        entry = child / ENTRY_NAME
        if not entry.is_file():
            issues.append(_issue(child.name, ERROR, "missing-entry", f"缺少 {ENTRY_NAME}"))
            continue
        try:
            raw, body = parse_skill_md(entry)
            meta = build_meta(child, raw)
        except SkillParseError as exc:
            issues.append(_issue(child.name, ERROR, "parse-error", str(exc)))
            continue

        if not body.strip():
            issues.append(
                _issue(meta.name, ERROR, "empty-body", "正文为空：只有 frontmatter，没有实际内容")
            )

        for key in raw:
            if key not in KNOWN_FRONTMATTER_KEYS:
                issues.append(
                    _issue(
                        meta.name,
                        WARNING,
                        "unknown-field",
                        f"未识别字段 {key!r} 会被静默忽略（拼写错误？）",
                    )
                )

        if len(body) > max_body_chars:
            issues.append(
                _issue(
                    meta.name,
                    WARNING,
                    "body-too-long",
                    f"正文 {len(body)} 字符 > 上限 {max_body_chars}，模型只能看到前 {max_body_chars} 字符",
                )
            )
        if len(meta.description) > max_desc_chars:
            issues.append(
                _issue(
                    meta.name,
                    WARNING,
                    "description-truncated",
                    f"description {len(meta.description)} 字符 > {max_desc_chars}，目录里会显示不全",
                )
            )
        if len(meta.description) < MIN_DESC_CHARS:
            issues.append(
                _issue(
                    meta.name,
                    WARNING,
                    "description-too-short",
                    f"description 只有 {len(meta.description)} 字符，模型难以判断该不该选用",
                )
            )
        if known is not None:
            for tool in meta.allowed_tools:
                if tool not in known:
                    issues.append(
                        _issue(
                            meta.name,
                            WARNING,
                            "unknown-tool",
                            f"allowed-tools 声明的工具未注册: {tool}",
                        )
                    )

    issues.sort(key=lambda issue: (issue.skill, issue.level, issue.code))
    return issues


def _issue(skill: str, level: str, code: str, message: str) -> LintIssue:
    return LintIssue(skill=skill, level=level, code=code, message=message)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m harness.skill.lint [root]`。有问题返回 1。"""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path("skills")
    issues = lint_skills(root)
    if not issues:
        print(f"OK: {root} 没有发现问题")
        return 0
    for issue in issues:
        print(issue)
    errors = sum(1 for issue in issues if issue.level == ERROR)
    print(f"\n共 {len(issues)} 个问题（{errors} 个 error，{len(issues) - errors} 个 warning）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MAX_BODY_CHARS",
    "DEFAULT_MAX_DESC_CHARS",
    "ERROR",
    "LintIssue",
    "MIN_DESC_CHARS",
    "WARNING",
    "lint_skills",
    "main",
]
