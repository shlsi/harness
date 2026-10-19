"""结果处理层：把工具原始输出转成面向模型的内容，并做截断控制。"""

from __future__ import annotations

import json
from typing import Any

from .schema import ToolSpec


def to_content(value: Any) -> str:
    """把任意返回值转成字符串内容。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def truncate(text: str, max_chars: int) -> str:
    """按字符数截断，并附上截断提示，避免吃满上下文。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[已截断，原始长度 {len(text)} 字符]"


def normalize(value: Any, spec: ToolSpec) -> str:
    return truncate(to_content(value), spec.result_max_chars)
