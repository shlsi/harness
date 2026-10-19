"""观测落地：span 的 IO 层——写入 JSONL，以及从 JSONL 读回。

写侧是 `JsonlTracerSink`（一线一 span、只追加）；
读侧是 `read_spans` / `read_tree`，供离线分析、导出、以及在内存视图被
`max_spans` 截断时取全量。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..tool.telemetry import build_tree


class JsonlTracerSink:
    """JSONL span sink。

    - 工具是并发执行的，写入加锁；
    - 写盘失败静默（观测不能影响主流程，`Tracer` 外面也还有一层兜底）；
    - 进程内先建好父目录，避免首次写入时报错。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, span: dict[str, Any]) -> None:
        line = json.dumps(span, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            pass


def read_spans(path: Path | str, *, trace_id: str | None = None) -> list[dict[str, Any]]:
    """从 `traces.jsonl` 读回 span。`trace_id=None` 表示全部。

    - **流式逐行读**：traces 文件会长，不整个读进内存；
    - **坏行跳过不炸**：崩在写一半会留下半行 JSON，与 session 的 JSONL 同一套对策。
    """
    target = Path(path)
    if not target.is_file():
        return []
    spans: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(span, dict):
                continue
            if trace_id is not None and span.get("trace_id") != trace_id:
                continue
            spans.append(span)
    return spans


def read_tree(path: Path | str, *, trace_id: str | None = None) -> list[dict[str, Any]]:
    """读回并组树。与内存视图共用 `build_tree`，避免两套组树逻辑漂移。"""
    return build_tree(read_spans(path, trace_id=trace_id))


__all__ = ["JsonlTracerSink", "read_spans", "read_tree"]
