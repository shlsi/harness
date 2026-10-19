"""循环保护：检测「同一工具被以完全相同的参数反复调用」。

Agent 失控的典型形态不是步数多，而是**原地打转**——反复用一模一样的参数调用同一个工具，
每次都拿到同样的结果，却始终不换策略。`max_steps` 只能兜住总轮次，兜不住这种空转。

策略与项目一贯的「先回喂、再终止」一致（对照：审批缺回调 → 回喂模型自纠）：

    同一签名（工具名 + 规范化参数）的累计调用次数
      ≥ repeat_warn_at  → 在该次工具结果末尾附一句提醒（**每个签名只提醒一次**）
      ≥ repeat_stop_at  → 结束本轮，`stop_reason = "loop_detected"`

任一阈值取 0 或负数表示关闭该档。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..llm.schema import ToolCall

WARNING_TEMPLATE = (
    "\n\n[循环保护] 你已第 {count} 次以完全相同的参数调用 `{name}`。"
    "如果结果没有变化，请不要继续重复调用：换参数、换工具，或直接用已有信息作答。"
)


def call_signature(call: ToolCall) -> str:
    """工具调用签名：名称 + 规范化参数（**键序无关**，`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 同签名）。"""
    return f"{call.name}({_canonical(call.arguments)})"


def _canonical(arguments: str | None) -> str:
    raw = arguments or ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw.strip()
    try:
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return raw.strip()


@dataclass
class GuardVerdict:
    """一次 inspect 的结论。"""

    warnings: dict[int, int] = field(default_factory=dict)  # 调用下标 → 已累计次数
    stopped: bool = False
    signature: str | None = None  # 触发终止的签名
    count: int = 0


class LoopGuard:
    """一次 run 内有效的重复调用计数器（**不要跨 run 复用**）。"""

    def __init__(self, *, warn_at: int = 3, stop_at: int = 6) -> None:
        self.warn_at = warn_at
        self.stop_at = stop_at
        self._counts: dict[str, int] = {}
        self._warned: set[str] = set()

    def inspect(self, calls: list[ToolCall]) -> GuardVerdict:
        """按调用顺序累计签名次数，返回需要提醒的下标与是否应终止。"""
        verdict = GuardVerdict()
        for index, call in enumerate(calls):
            signature = call_signature(call)
            count = self._counts.get(signature, 0) + 1
            self._counts[signature] = count

            if self.warn_at > 0 and count >= self.warn_at and signature not in self._warned:
                self._warned.add(signature)
                verdict.warnings[index] = count

            if self.stop_at > 0 and count >= self.stop_at and not verdict.stopped:
                verdict.stopped = True
                verdict.signature = signature
                verdict.count = count
        return verdict

    @staticmethod
    def warning_text(call: ToolCall, count: int) -> str:
        return WARNING_TEMPLATE.format(count=count, name=call.name)


__all__ = ["LoopGuard", "GuardVerdict", "call_signature"]
