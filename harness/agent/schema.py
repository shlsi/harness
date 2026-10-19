"""Agent Loop 的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm.errors import LLMError
from ..llm.schema import ChatResponse, Message, Usage

DEFAULT_MAX_STEPS = 12


class StopReason:
    """循环结束原因。"""

    FINAL = "final"  # 模型不再要求调工具（正常结束）
    MAX_STEPS = "max_steps"  # 达到最大轮次
    TOKEN_BUDGET = "token_budget"  # 累计 token 超出预算
    LOOP_DETECTED = "loop_detected"  # 同一工具被以相同参数反复调用（循环保护）
    INTERRUPTED = "interrupted"  # 用户插话：本轮工具已执行完，提前收尾去响应用户新输入
    ERROR = "error"  # 模型调用不可恢复错误


@dataclass
class LoopConfig:
    max_steps: int = DEFAULT_MAX_STEPS
    max_total_tokens: int | None = None  # None 表示不限
    temperature: float | None = None
    max_tokens: int | None = None  # 单次生成上限
    tool_choice: str | None = "auto"  # 仅在传入 tools 时生效
    model: str | None = None  # 覆盖 Provider 默认模型
    stream: bool = False  # True 且 Provider 支持 astream 时，逐增量发出 text_delta 事件
    # 循环保护：同一签名（工具名 + 相同参数）的累计调用次数。
    repeat_warn_at: int = 3  # 达到即在该次工具结果里提醒模型（每个签名只提醒一次）；0 = 关闭
    repeat_stop_at: int = 6  # 达到即结束本轮，stop_reason = loop_detected；0 = 关闭
    # 基础系统指令（由 PromptBuilder 拼进 system prompt 最前面；Loop 自身不使用）
    base_instructions: str = ""


@dataclass
class StepRecord:
    index: int
    response: ChatResponse
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0


@dataclass
class RunResult:
    """一次 run 的结果。messages 含输入与本轮新增，可直接作为下次输入。"""

    messages: list[Message]
    final_text: str | None
    steps: list[StepRecord]
    usage: Usage
    stop_reason: str
    error: LLMError | None = None
    trace_id: str | None = None
