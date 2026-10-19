"""时间工具：模型自身没有实时时钟，需要当前时间时由它显式调用。"""

from __future__ import annotations

from datetime import datetime

from ..registry import tool


@tool(
    name="time.now",
    description=(
        "获取当前日期时间（带本地时区偏移）。模型自身没有实时时钟，"
        "需要判断「今天」「现在」或计算时间差时调用本工具。"
    ),
    side_effect="read",
    tags=["time"],
)
def now() -> dict[str, str]:
    """返回当前本地时间。"""
    current = datetime.now().astimezone()
    return {
        "iso": current.isoformat(timespec="seconds"),
        "date": current.date().isoformat(),
        "weekday": current.strftime("%A"),
        "timezone": current.tzname() or "",
        "utc_offset": current.strftime("%z"),
    }


__all__ = ["now"]
