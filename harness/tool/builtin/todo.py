"""任务清单工具：让 Agent 维护自己的待办列表。"""

from __future__ import annotations

from typing import Any

from ..registry import tool
from ..schema import Context

# 按会话保存的待办列表（进程内存储，后续可替换为持久化实现）。
_STORE: dict[str, list[dict[str, Any]]] = {}

_VALID_STATUS = {"pending", "in_progress", "completed"}
_VALID_PRIORITY = {"high", "medium", "low"}


def _validate(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(todos):
        if not isinstance(item, dict) or not item.get("content"):
            raise ValueError(f"todos[{index}] 必须包含非空的 content 字段")
        status = item.get("status", "pending")
        priority = item.get("priority", "medium")
        if status not in _VALID_STATUS:
            raise ValueError(f"todos[{index}].status 非法，应为 {sorted(_VALID_STATUS)}")
        if priority not in _VALID_PRIORITY:
            raise ValueError(f"todos[{index}].priority 非法，应为 {sorted(_VALID_PRIORITY)}")
        normalized.append({"content": str(item["content"]), "status": status, "priority": priority})
    return normalized


def _render(todos: list[dict[str, Any]]) -> str:
    if not todos:
        return "任务清单已清空。"
    marks = {"pending": " ", "in_progress": "~", "completed": "x"}
    lines = [
        f"- [{marks[t['status']]}] {t['content']} ({t['status']}, {t['priority']})"
        for t in todos
    ]
    return "当前任务清单：\n" + "\n".join(lines)


@tool(
    name="todo.write",
    description=(
        "覆盖式写入当前会话的任务清单，用于规划与跟踪多步工作。"
        "每次传入完整列表；completed 的任务保留在列表中以便追溯。"
    ),
    side_effect="write",
    tags=["todo"],
)
def write_todos(ctx: Context, todos: list[dict[str, Any]]) -> str:
    """写入任务清单。

    Args:
        todos: 完整清单，每项含 content（必填）、status（pending/in_progress/completed）、
            priority（high/medium/low）。
    """
    normalized = _validate(todos)
    _STORE[ctx.session_id or "default"] = normalized
    return _render(normalized)
