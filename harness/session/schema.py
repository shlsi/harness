"""会话管理的数据契约：事件、元信息、配置与 Session（含事件重放）。

关键不变量：`build_messages()` 输出必须是「固定区 + 压缩区 + 活跃区」的**前缀单调**序列，
新内容只出现在尾部，已发出的前缀不被改写（KV / 前缀缓存友好）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..agent.schema import RunResult
from ..llm.schema import Message, Usage
from ..tool.schema import Context

EVENT_META = "meta"
EVENT_MESSAGE = "message"
EVENT_COMPACT = "compact"
EVENT_USAGE = "usage"

# 累计用量的三个键（顺序即展示顺序）
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

# 压缩区摘要消息的前缀标记
SUMMARY_PREFIX = "[此前对话摘要]"


class SessionError(Exception):
    """会话模块异常基类。"""


class SessionNotFound(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"会话不存在: {session_id}")
        self.session_id = session_id


def _message_from_dict(raw: dict[str, Any]) -> Message:
    return Message.from_dict(raw)


def summary_message(text: str) -> Message:
    """把摘要文本包成压缩区的消息（role=system）。"""
    return Message.system(f"{SUMMARY_PREFIX}\n{text}")


@dataclass
class SessionEvent:
    """append-only 日志的基本单位。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "ts": self.ts}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SessionEvent":
        return cls(
            type=str(raw.get("type", "")),
            data=raw.get("data") or {},
            ts=float(raw.get("ts") or 0.0),
        )

    # ---- 构造函数 ----
    @classmethod
    def message(cls, message: Message) -> "SessionEvent":
        return cls(type=EVENT_MESSAGE, data={"message": message.to_dict()})

    @classmethod
    def compact(cls, summary: str, dropped: int) -> "SessionEvent":
        return cls(type=EVENT_COMPACT, data={"summary": summary, "dropped": dropped})

    @classmethod
    def meta(cls, **data: Any) -> "SessionEvent":
        return cls(type=EVENT_META, data=data)

    @classmethod
    def usage(cls, usage: Usage, *, turns: int) -> "SessionEvent":
        """一轮的 token 用量。与 `meta(turns=...)` 同流，replay 时累加进 `usage_totals`。"""
        return cls(
            type=EVENT_USAGE,
            data={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "turns": turns,
            },
        )


@dataclass
class SessionMeta:
    session_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    title: str | None = None
    turns: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # 会话累计 token（跨轮累加，来自每轮的 usage 事件；旧日志无该事件 → 保持 0）
    usage_totals: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key in USAGE_KEYS}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "turns": self.turns,
            "metadata": self.metadata,
            "usage_totals": dict(self.usage_totals),
        }


@dataclass
class SessionConfig:
    """上下文分区与压缩策略。"""

    compact_threshold: int = 40  # 对话消息（固定头 + 活跃区）超过这么多条 → 触发压缩
    keep_head: int = 0  # 首次压缩时把活跃区开头这么多条固化进固定头，此后永不压缩；0 = 不保护
    keep_tail: int = 16  # 压缩后至少保留最近这么多条；0 = 不保护（连尾部也可被压掉）
    max_active_tokens: int | None = None  # 可选：按估算 token 触发
    enable_compaction: bool = True
    auto_title: bool = True  # 首轮问答结束后用 LLM 总结一个标题
    title_max_chars: int = 24  # 生成标题的字数上限


def estimate_tokens(messages: Iterable[Message]) -> int:
    """粗略估算（字符数 / 4），仅用于阈值判断与前端展示，不追求精确。"""
    total = 0
    for message in messages:
        content = message.content
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(part)) for part in content)
        for call in message.tool_calls or []:
            total += len(call.arguments)
    return total // 4


def _messages_after(all_messages: list[Message], sent: list[Message]) -> list[Message]:
    """取 `all_messages` 中位于 `sent` 之后的尾部（即本轮新增消息）。

    Loop 只会**在尾部追加**，而可能的头部改写（如 PromptBuilder 注入/合并 system）只影响开头。
    因此用 `sent` 的**最后一条**做身份定位最稳：前缀怎么变都不影响定位。
    """
    if not sent:
        return list(all_messages)
    last = sent[-1]
    for index in range(len(all_messages) - 1, -1, -1):
        if all_messages[index] is last:
            return list(all_messages[index + 1 :])
    # 兜底：定位失败时按长度切分
    return list(all_messages[len(sent) :]) if len(all_messages) > len(sent) else []


@dataclass
class Session:
    """四段式上下文：固定区 / 固定头 / 压缩区 / 活跃区。

    渲染顺序固定为 `system → head → summary → active`。`head` 是被 `keep_head` 保护下来的
    开头若干条对话消息（只在首次压缩时固化一次），因此摘要**排在它后面**，顺序与时间一致。
    """

    meta: SessionMeta
    system: list[Message] = field(default_factory=list)
    head: list[Message] = field(default_factory=list)
    summary: Message | None = None
    active: list[Message] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.meta.session_id

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None = None,
        system: Iterable[Message] | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Session":
        meta = SessionMeta(
            session_id=session_id or uuid.uuid4().hex,
            title=title,
            metadata=dict(metadata or {}),
        )
        return cls(meta=meta, system=list(system or []))

    # ---- 拼装 ----
    def build_messages(self) -> list[Message]:
        """固定区 + 固定头 + 压缩区 + 活跃区。前缀单调，只在尾部增长。"""
        prefix = list(self.system) + list(self.head)
        if self.summary is not None:
            prefix.append(self.summary)
        return prefix + list(self.active)

    def context(
        self,
        *,
        scopes: Iterable[str] | None = None,
        approval: Any = None,
        ask: Any = None,
        sandbox: Any = None,
    ) -> Context:
        """产出带 session_id 的上下文（复用 tool 模块的 Context）。

        `sandbox` 传该会话的工作区策略（控制台按会话选的工作区）；`None` 表示沿用
        进程级默认沙箱（见 harness/tool/sandbox.py）。
        """
        return Context(
            session_id=self.meta.session_id,
            scopes=set(scopes or ()),
            approval=approval,
            ask=ask,
            sandbox=sandbox,
        )

    # ---- 变更（返回事件，由调用方落库）----
    def append(self, *messages: Message) -> list[SessionEvent]:
        events: list[SessionEvent] = []
        for message in messages:
            self.active.append(message)
            events.append(SessionEvent.message(message))
        if messages:
            self.meta.updated_at = time.time()
        return events

    def record(self, result: RunResult, sent: list[Message]) -> list[SessionEvent]:
        """把本轮新增（Loop 追加的 assistant / tool 消息）写入活跃区。"""
        events = self.append(*_messages_after(result.messages, sent))
        self.meta.turns += 1
        events.append(SessionEvent.meta(turns=self.meta.turns))
        self._add_usage(result.usage)
        events.append(SessionEvent.usage(result.usage, turns=self.meta.turns))
        return events

    def rename(self, title: str) -> list[SessionEvent]:
        """改标题（只产出一条 meta 事件，消息序列不动）。"""
        self.meta.title = title
        self.meta.updated_at = time.time()
        return [SessionEvent.meta(title=title)]

    def set_metadata(self, **data: Any) -> list[SessionEvent]:
        """合并式更新会话元数据（只产出一条 meta 事件，消息序列不动）。

        控制台用它按会话保存「工作区」与「权限档位」（键见
        `harness/tool/permission.py`）。**合并**而非替换：未提到的键保持原样，
        因此不会把创建时写入的其他 metadata 抹掉。
        """
        if not data:
            return []
        self.meta.metadata.update(data)
        self.meta.updated_at = time.time()
        return [SessionEvent.meta(metadata=dict(data))]

    def _add_usage(self, usage: Usage) -> None:
        for key in USAGE_KEYS:
            self.meta.usage_totals[key] += getattr(usage, key, 0) or 0

    def adopt_head(self, keep_head: int) -> list[SessionEvent]:
        """把活跃区开头 `keep_head` 条固化进固定头（**只做一次**，此后永不压缩）。

        放在固定头里而不是"每次都跳过前 N 条"，是为了让摘要的位置**在它之后**——
        否则摘要会渲染在两个更早的消息之前，时间顺序是反的。

        事件里只记**搬了多少条**（`head_count`），replay 时对 active 做同一段切片——
        若把消息列表整个记下来，replay 会把它们同时算进 head 与 active，导致重复。
        """
        if keep_head <= 0 or self.head:
            return []
        moved = min(keep_head, len(self.active))
        if moved <= 0:
            return []
        self.head = list(self.active[:moved])
        self.active = list(self.active[moved:])
        self.meta.updated_at = time.time()
        return [SessionEvent.meta(head_count=moved)]

    def compact(self, summary: str, dropped: int) -> list[SessionEvent]:
        """把活跃区头部 `dropped` 条折叠成一条摘要（前缀只在此刻变化一次）。"""
        dropped = max(0, min(dropped, len(self.active)))
        if dropped == 0:
            return []
        self.active = self.active[dropped:]
        self.summary = summary_message(summary)
        self.meta.updated_at = time.time()
        return [SessionEvent.compact(summary, dropped)]

    # ---- 阈值 ----
    def is_over_threshold(self, config: SessionConfig) -> bool:
        """按「固定头 + 活跃区」的对话消息数判断（不含固定区与压缩区）。"""
        if not config.enable_compaction:
            return False
        conversation = list(self.head) + list(self.active)
        if len(conversation) > config.compact_threshold:
            return True
        if config.max_active_tokens is not None:
            return estimate_tokens(conversation) > config.max_active_tokens
        return False


def replay(session_id: str, events: Iterable[SessionEvent]) -> Session:
    """由事件日志重放得到会话状态。"""
    session = Session(meta=SessionMeta(session_id=session_id))
    seen_any = False
    for event in events:
        if not seen_any:
            session.meta.created_at = event.ts
            seen_any = True
        if event.ts:
            session.meta.updated_at = max(session.meta.updated_at, event.ts)

        if event.type == EVENT_META:
            if "system" in event.data:
                session.system = [_message_from_dict(d) for d in event.data["system"]]
            if "head_count" in event.data:
                # 与 Session.adopt_head 做同一段搬移（不是赋值，避免 head/active 重复）
                count = int(event.data["head_count"])
                session.head = session.active[:count]
                session.active = session.active[count:]
            if "title" in event.data:
                session.meta.title = event.data["title"]
            if "metadata" in event.data:
                session.meta.metadata.update(event.data["metadata"])
            if "turns" in event.data:
                session.meta.turns = int(event.data["turns"])
        elif event.type == EVENT_MESSAGE:
            session.active.append(_message_from_dict(event.data["message"]))
        elif event.type == EVENT_COMPACT:
            session.summary = summary_message(str(event.data.get("summary", "")))
            session.active = session.active[int(event.data.get("dropped", 0)) :]
        elif event.type == EVENT_USAGE:
            for key in USAGE_KEYS:
                session.meta.usage_totals[key] += int(event.data.get(key, 0) or 0)
    return session
