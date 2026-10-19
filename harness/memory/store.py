"""记忆存储：一个 Markdown 文件的读写（去重 / 上限 / 原子替换 / 按 mtime 缓存）。

设计见 [docs/memory-system-design.md](../../docs/memory-system-design.md)。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .schema import (
    DEFAULT_SECTION,
    MemoryDoc,
    MemoryEntryInvalid,
    MemoryFull,
    normalize,
    parse_doc,
    render_doc,
    render_for_prompt,
)


class MemoryStore:
    """`MEMORY.md` 的读写。**进程内单例**，由 `build_state` 创建后注入 registry 与 prompt。

    - **去重**：一条记忆 = 一行 bullet，按规范化文本比对；重复 save 只把它移到目标节末尾。
    - **上限**：渲染后超过 `max_chars` 就**拒绝写入**并抛 `MemoryFull`，不静默截断。
    - **原子写**：写临时文件再 `os.replace`，不会留下半文件。
    - **解析缓存**：按 `mtime_ns` 缓存解析结果，内容没变时 `render()` 返回同一个字符串
      （前缀缓存友好的前提）。
    """

    def __init__(self, path: Path | str, *, max_chars: int = 4000) -> None:
        self.path = Path(path)
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._cache: tuple[int, MemoryDoc] | None = None  # (mtime_ns, doc)

    # ---- 读 ----
    def load(self) -> MemoryDoc:
        """读并解析；文件不存在 → 空文档（不报错）。返回的是**缓存对象，不要改它**。"""
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            return MemoryDoc()

        cached = self._cache
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        try:
            text = self.path.read_text("utf-8")
        except OSError:
            return MemoryDoc()

        doc = parse_doc(text)
        self._cache = (mtime_ns, doc)
        return doc

    def entries(self) -> list[str]:
        return self.load().entries()

    def render(self) -> str:
        """渲染成注入 system 的文本；空记忆返回空串。"""
        with self._lock:
            return render_for_prompt(self.load())

    # ---- 写 ----
    def save(self, text: str, *, section: str = DEFAULT_SECTION) -> str:
        """记住一条；已存在则移到目标节末尾。返回给模型看的结果文本。"""
        entry = normalize(text)
        if not entry:
            raise MemoryEntryInvalid(str(self.path), "记忆内容不能为空")

        with self._lock:
            doc = self.load().clone()
            existed = doc.remove(entry) > 0  # 去重：先摘掉旧位置
            doc.add(section, entry)
            self._persist(doc)
        action = "已存在，已移到「{}」节末尾".format(normalize(section) or DEFAULT_SECTION)
        return f"{action if existed else '已记住'}：{entry}"

    def forget(self, text: str) -> str:
        """删除匹配的记忆（先精确匹配，没命中再按子串匹配）。"""
        entry = normalize(text)
        if not entry:
            raise MemoryEntryInvalid(str(self.path), "要删除的内容不能为空")

        with self._lock:
            doc = self.load().clone()
            removed = doc.remove(entry)
            if removed == 0:
                removed = doc.remove(entry, substring=True)
            if removed == 0:
                return f"未找到匹配的记忆：{entry}"
            self._persist(doc)
        return f"已删除 {removed} 条记忆：{entry}"

    # ---- 内部 ----
    def replace(self, text: str) -> None:
        """整文替换（控制台编辑用）：解析 → 校验上限 → 原子替换。失败则文件不变。"""
        with self._lock:
            self._persist(parse_doc(text or ""))

    def _persist(self, doc: MemoryDoc) -> None:
        """校验上限 → 原子替换 → 刷新解析缓存。失败时文件保持不变。"""
        rendered = render_doc(doc)
        if len(rendered) > self.max_chars:
            raise MemoryFull(
                str(self.path),
                f"记忆已满（{len(rendered)}/{self.max_chars} 字符），"
                "请先合并或删除旧条目（可用 memory.forget）再写",
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, self.path)
        self._cache = (self.path.stat().st_mtime_ns, doc)
