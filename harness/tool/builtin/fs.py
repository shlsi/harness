"""文件系统工具（示例内置工具）。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from ..registry import tool
from ..sandbox import SandboxViolation, current_sandbox, resolve_path


@tool(
    name="fs.read",
    description=(
        "读取文本文件内容（只读操作）。用于查看代码、配置、日志等文本文件；"
        "不用于写入或修改文件，也不用于列目录。"
    ),
    side_effect="read",
    scopes=["fs:read"],
    tags=["fs", "read"],
)
def read_file(path: str, max_bytes: int = 200_000, encoding: str = "utf-8") -> str:
    """读取文件。

    Args:
        path: 文件的绝对路径。
        max_bytes: 最多读取的字节数，超出部分被截断。
        encoding: 文本编码，默认 utf-8。
    """
    p = resolve_path(path, mode="read")
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if p.is_dir():
        raise IsADirectoryError(f"这是一个目录而非文件: {path}")
    data = p.read_bytes()[:max_bytes]
    return data.decode(encoding, errors="replace")


@tool(
    name="fs.write",
    description=(
        "将文本写入文件（写入操作，会覆盖同名文件的全部内容）。"
        "用于创建或修改文本文件；本工具不负责删除文件。"
    ),
    side_effect="write",
    scopes=["fs:write"],
    idempotent=True,
    tags=["fs", "write"],
)
def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """写入文件。

    Args:
        path: 文件的绝对路径。
        content: 要写入的完整文本内容。
        encoding: 文本编码，默认 utf-8。
    """
    p = resolve_path(path, mode="write")
    if p.exists() and p.is_dir():
        raise IsADirectoryError(f"目标是一个目录，无法写入: {path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding=encoding)
    return f"已写入 {len(content)} 字符到 {path}"


@tool(
    name="fs.edit",
    description=(
        "对文件做精确文本替换（old_string -> new_string）。"
        "old_string 必须在文件中唯一出现，否则报错；适合修改代码中的一小段。"
        "整文件重写请用 fs.write。"
    ),
    side_effect="write",
    scopes=["fs:write"],
    tags=["fs", "write"],
)
def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    encoding: str = "utf-8",
) -> str:
    """精确替换文件内容。

    Args:
        path: 文件的绝对路径。
        old_string: 待替换的原文，必须在文件中唯一出现。
        new_string: 替换后的新文本。
        encoding: 文本编码，默认 utf-8。
    """
    p = resolve_path(path, mode="write")
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    text = p.read_text(encoding=encoding)
    occurrences = text.count(old_string)
    if occurrences == 0:
        raise ValueError("old_string 未在文件中找到")
    if occurrences > 1:
        raise ValueError(f"old_string 在文件中出现 {occurrences} 次，不唯一，请提供更长的上下文")
    p.write_text(text.replace(old_string, new_string, 1), encoding=encoding)
    return f"已替换 1 处内容: {path}"


@tool(
    name="fs.list",
    description="列出目录内容（只读）。返回条目列表，目录以 / 结尾；depth 控制递归层级。",
    side_effect="read",
    scopes=["fs:read"],
    tags=["fs", "read"],
)
def list_dir(path: str = ".", depth: int = 1, max_entries: int = 500) -> list[str]:
    """列目录。

    Args:
        path: 目录路径，默认当前目录。
        depth: 递归深度，1 表示只看当前层。
        max_entries: 最多返回的条目数。
    """
    root = resolve_path(path, mode="read")
    if not root.exists():
        raise FileNotFoundError(f"路径不存在: {path}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {path}")

    lines: list[str] = []

    def walk(current: Path, prefix: str, level: int) -> None:
        if level > depth or len(lines) >= max_entries:
            return
        for entry in sorted(current.iterdir(), key=lambda e: (e.is_file(), e.name)):
            if len(lines) >= max_entries:
                return
            try:
                resolve_path(entry, mode="read")
            except SandboxViolation:
                continue  # 越界条目直接隐藏（不因个别条目让整次列目录失败）
            lines.append(f"{prefix}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and level < depth:
                walk(entry, prefix + "  ", level + 1)

    walk(root, "", 1)
    return lines


@tool(
    name="fs.glob",
    description="按 glob 模式查找文件（只读），如 '**/*.py'。返回匹配的文件路径列表。",
    side_effect="read",
    scopes=["fs:read"],
    tags=["fs", "read"],
)
def glob_files(pattern: str, root: str = ".", max_results: int = 200) -> list[str]:
    """按模式查找文件。

    Args:
        pattern: glob 模式，如 '**/*.py'、'*.md'。
        root: 起始目录，默认当前目录。
        max_results: 最多返回的结果数。
    """
    base = resolve_path(root, mode="read")
    if not base.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")
    out: list[str] = []
    for hit in sorted(base.glob(pattern)):
        try:
            real = resolve_path(hit, mode="read")
        except SandboxViolation:
            continue  # 指向工作区外的符号链接：静默跳过
        out.append(str(real))
        if len(out) >= max_results:
            break
    return out


@tool(
    name="fs.grep",
    description="在文件中按正则检索内容（只读）。返回 {file, line, text} 列表，用于定位代码/日志。",
    side_effect="read",
    scopes=["fs:read"],
    tags=["fs", "read"],
)
def grep_files(
    pattern: str,
    root: str = ".",
    file_glob: str = "**/*",
    ignore_case: bool = False,
    max_results: int = 200,
) -> list[dict[str, Any]]:
    """内容检索。

    Args:
        pattern: 正则表达式。
        root: 起始目录，默认当前目录。
        file_glob: 限定检索哪些文件，默认全部。
        ignore_case: 是否忽略大小写。
        max_results: 最多返回的匹配数。
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ValueError(f"正则表达式无效: {exc}") from exc

    base = resolve_path(root, mode="read")
    if not base.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")
    results: list[dict[str, Any]] = []
    for fp in sorted(base.glob(file_glob)):
        if not fp.is_file():
            continue
        try:
            real = resolve_path(fp, mode="read")
        except SandboxViolation:
            continue  # 指向工作区外的符号链接：静默跳过
        try:
            text = real.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                results.append({"file": str(real), "line": lineno, "text": line[:200]})
                if len(results) >= max_results:
                    return results
    return results


@tool(
    name="fs.mkdir",
    description="创建目录（父目录自动创建；已存在不算错误）。",
    side_effect="write",
    scopes=["fs:write"],
    idempotent=True,
    tags=["fs", "write"],
)
def make_dir(path: str) -> str:
    """创建目录。

    Args:
        path: 目录路径（受工作区沙箱约束）。
    """
    p = resolve_path(path, mode="write")
    if p.exists() and not p.is_dir():
        raise NotADirectoryError(f"同名文件已存在，无法创建目录: {path}")
    p.mkdir(parents=True, exist_ok=True)
    return f"已创建目录: {path}"


@tool(
    name="fs.copy",
    description="复制文件或目录（写入操作）。目标已存在会报错，不覆盖。",
    side_effect="write",
    scopes=["fs:write"],
    tags=["fs", "write"],
)
def copy_path(src: str, dst: str) -> str:
    """复制文件或目录。

    Args:
        src: 源路径（按只读校验沙箱）。
        dst: 目标路径（按写入校验沙箱）。
    """
    source = resolve_path(src, mode="read")
    target = resolve_path(dst, mode="write")
    if not source.exists():
        raise FileNotFoundError(f"源路径不存在: {src}")
    if target.exists():
        raise FileExistsError(f"目标已存在，请改名或先删除: {dst}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
        return f"已复制目录 {src} -> {dst}"
    shutil.copy2(source, target)
    return f"已复制文件 {src} -> {dst}"


@tool(
    name="fs.move",
    description="移动或重命名文件/目录（写入操作）。两端都必须在工作区内；目标已存在会报错。",
    side_effect="write",
    scopes=["fs:write"],
    tags=["fs", "write"],
)
def move_path(src: str, dst: str) -> str:
    """移动或重命名。

    Args:
        src: 源路径（按只读校验沙箱）。
        dst: 目标路径（按写入校验沙箱）。
    """
    source = resolve_path(src, mode="read")
    target = resolve_path(dst, mode="write")
    if not source.exists():
        raise FileNotFoundError(f"源路径不存在: {src}")
    if target.exists():
        raise FileExistsError(f"目标已存在，请改名: {dst}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return f"已移动 {src} -> {dst}"


@tool(
    name="fs.delete",
    description=(
        "删除文件或目录（危险操作，执行前需要人工确认）。"
        "目录必须显式 recursive=True；拒绝删除工作区根目录自身。"
    ),
    side_effect="destructive",
    scopes=["fs:write"],
    requires_approval=True,
    tags=["fs", "write"],
)
def delete_path(path: str, recursive: bool = False) -> str:
    """删除文件或目录。

    Args:
        path: 要删除的路径（受工作区沙箱约束）。
        recursive: 目录是否递归删除；默认 False（目录会报错）。
    """
    target = resolve_path(path, mode="write")
    if not target.exists():
        raise FileNotFoundError(f"路径不存在: {path}")
    if target == current_sandbox().root:
        raise ValueError(f"拒绝删除工作区根目录: {target}")
    if target.is_dir():
        if not recursive:
            raise IsADirectoryError(f"{path} 是目录；删除整个目录需显式传 recursive=True")
        shutil.rmtree(target)
        return f"已递归删除目录: {path}"
    target.unlink()
    return f"已删除文件: {path}"
