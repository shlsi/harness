"""记忆契约：文档模型 + 解析 / 渲染（**纯函数、零 IO**）。

记忆文档就是一份 Markdown：

```markdown
# 长期记忆

## 用户偏好
- 回复用中文，不用 emoji

## 项目事实
- 项目根 c:\\Users\\Lenovo\\Desktop\\agent，Python + FastAPI
```

- 一级标题是文档名，**不注入** system；
- `## 分节` 下面每行 `- ` 是**一条记忆**（去重与删除的粒度就是这一行）；
- 渲染是**确定性**的（无时间戳、无随机数），否则每轮重算 system 会击穿前缀缓存。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DOC_TITLE = "# 长期记忆"
DEFAULT_SECTION = "结论与坑"
BULLET = "- "

_TITLE_RE = re.compile(r"^#\s+(.*\S)\s*$")
_SECTION_RE = re.compile(r"^##\s+(.*\S)\s*$")


class MemoryStoreError(Exception):
    """记忆系统错误基类。带 `path` 与 `message`，错误文本可直接回喂模型。

    刻意不叫 `MemoryError`——那是 Python 内建异常的名字，不该被遮蔽。
    """

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


class MemoryFull(MemoryStoreError):
    """记忆已满：**拒绝写入**（不静默截断——静默丢记忆是无声故障）。"""


class MemoryEntryInvalid(MemoryStoreError):
    """条目非法（例如内容为空）。"""


def normalize(text: str) -> str:
    """规范化：折叠所有空白（含换行）并去首尾 —— 一条记忆恒定占一行。"""
    return " ".join(str(text).split())


def is_entry(line: str) -> bool:
    return line.startswith(BULLET)


def entry_text(line: str) -> str:
    """取一行的记忆正文（`- ` 前缀去掉后规范化）。"""
    return normalize(line[len(BULLET):] if is_entry(line) else line)


@dataclass
class Section:
    """一个分节。`title` 为空串表示「无标题散行」（一级标题下、第一个 `## ` 之前的内容）。"""

    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class MemoryDoc:
    title: str = DOC_TITLE
    sections: list[Section] = field(default_factory=list)

    # ---- 查询 ----
    def entries(self) -> list[str]:
        """按顺序取出全部记忆正文（只含 `- ` 条目）。"""
        return [
            entry_text(line)
            for section in self.sections
            for line in section.lines
            if is_entry(line)
        ]

    def find(self, text: str, *, substring: bool = False) -> list[tuple[int, int]]:
        """按规范化文本查找条目，返回 `(分节下标, 行下标)` 列表。"""
        target = normalize(text)
        hits: list[tuple[int, int]] = []
        if not target:
            return hits
        for section_index, section in enumerate(self.sections):
            for line_index, line in enumerate(section.lines):
                if not is_entry(line):
                    continue
                current = entry_text(line)
                if current == target or (substring and target in current):
                    hits.append((section_index, line_index))
        return hits

    # ---- 变更（就地修改；调用方应先 `clone()`，别动缓存里的对象）----
    def clone(self) -> "MemoryDoc":
        return MemoryDoc(
            title=self.title,
            sections=[Section(section.title, list(section.lines)) for section in self.sections],
        )

    def add(self, section: str, text: str) -> None:
        entry = normalize(text)
        if not entry:
            raise MemoryEntryInvalid("", "记忆内容不能为空")
        self._section(normalize(section) or DEFAULT_SECTION, create=True).lines.append(BULLET + entry)

    def remove(self, text: str, *, substring: bool = False) -> int:
        hits = self.find(text, substring=substring)
        for section_index, line_index in sorted(hits, reverse=True):
            del self.sections[section_index].lines[line_index]
        # 删空的分节一并去掉（无标题散行节保留）
        self.sections = [
            section for section in self.sections if section.lines or not section.title
        ]
        return len(hits)

    def _section(self, title: str, *, create: bool) -> Section | None:
        for section in self.sections:
            if section.title == title:
                return section
        if not create:
            return None
        section = Section(title=title)
        self.sections.append(section)
        return section


def parse_doc(text: str) -> MemoryDoc:
    """解析记忆文档。空行被规范化掉（每次写入都会重排成规范形态）。"""
    doc = MemoryDoc()
    current: Section | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        title_match = _TITLE_RE.match(line)
        if title_match and current is None:
            doc.title = line
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            current = Section(title=section_match.group(1))
            doc.sections.append(current)
            continue
        if current is None:
            current = Section(title="")
            doc.sections.append(current)
        current.lines.append(line)

    return doc


def render_doc(doc: MemoryDoc) -> str:
    """渲染成文件内容（确定性）。"""
    lines = [doc.title or DOC_TITLE, ""]
    for section in doc.sections:
        if section.title:
            lines.append(f"## {section.title}")
        lines.extend(section.lines)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_for_prompt(doc: MemoryDoc) -> str:
    """渲染成注入 system 的文本：**去掉一级标题**，其余标题降一级。

    降级是为了让宿主加的「## 长期记忆」成为它们的父标题；
    没有任何内容时返回空串（不留空节）。
    """
    blocks: list[str] = []
    for section in doc.sections:
        chunk: list[str] = []
        if section.title:
            chunk.append(f"### {section.title}")
        chunk.extend(section.lines)
        if chunk:
            blocks.append("\n".join(chunk))
    return "\n\n".join(blocks)
