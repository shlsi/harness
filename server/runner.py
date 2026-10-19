"""Run：一轮对话的后台任务、事件缓冲、SSE 订阅与取消。

关键点：run 与 HTTP 连接**解耦**。客户端断开只移除订阅者，agent 照常跑完并落库；
客户端可凭 `Last-Event-ID` 重连补齐（见 docs/api-service-design.md §5.5）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from harness.agent import AgentLoop, EventType
from harness.llm import Message
from harness.session import fallback_title
from harness.tool import SandboxPolicy, approval_bypass, current_sandbox, scopes_for
from harness.tool.permission import PermissionError_, normalize as normalize_permission
from harness.tool.schema import ApprovalRequest, AskRequest

from .sse import to_sse_event, usage_dict

if TYPE_CHECKING:
    from .state import AppState

RUNNING = "running"
FINISHED = "finished"
ERROR = "error"
CANCELLED = "cancelled"

_TERMINAL_EVENT = "done"

# 会话设置类 metadata 的键（与 server/routes/session.py 保持一致）
WORKSPACE_KEY = "workspace"
PERMISSION_KEY = "permission"


@dataclass
class Run:
    id: str
    session_id: str
    trace_id: str | None = None  # 本次 run 的 span 关联键（来自 Context.trace_id）
    status: str = RUNNING
    frames: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: bool = False
    # 运行中收到的用户插话（软打断）：本轮工具执行完后收尾，由前端在 done 后续发
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0
    _subscribers: set[asyncio.Queue] = field(default_factory=set)

    # ---- 事件产出 ----
    def publish(self, name: str, data: dict[str, Any]) -> None:
        self._seq += 1
        frame = {
            "id": str(self._seq),
            "event": name,
            "data": json.dumps(data, ensure_ascii=False, default=str),
        }
        if name == _TERMINAL_EVENT:
            self.terminal = True
        self.frames.append(frame)
        for queue in list(self._subscribers):
            queue.put_nowait(frame)

    @property
    def has_interrupts(self) -> bool:
        """AgentLoop 的 `should_interrupt` 回调入口。"""
        return bool(self.interrupts)

    def request_interrupt(self, payload: dict[str, Any]) -> int:
        """登记一条用户插话；返回当前排队的条数。"""
        self.interrupts.append(payload)
        return len(self.interrupts)

    def publish_loop_event(self, event: Any) -> None:
        """AgentLoop 的 on_event 回调入口。"""
        mapped = to_sse_event(event)
        if mapped is None:
            return
        name, data = mapped
        if event.type == EventType.RUN_STARTED:
            data = {"run_id": self.id, "session_id": self.session_id, "trace_id": self.trace_id}
        elif event.type == EventType.RUN_FINISHED:
            # 前端拿到 trace_id 即可深链到 /runs/{id}/trace
            data = {**data, "trace_id": self.trace_id}
            # 正常收尾的 `done` 由这里发出（`finish()` 只是没走到 RUN_FINISHED 时的兜底），
            # 所以排队中的用户插话必须在这一并带上，否则会被吞掉。
            if self.interrupts:
                data["interrupts"] = list(self.interrupts)
        self.publish(name, data)

    def finish(
        self,
        status: str,
        *,
        summary: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """收尾：补齐终止事件、唤醒等待者、通知订阅者流结束。"""
        self.status = status
        if summary is not None:
            self.summary = summary
        if error is not None:
            self.error = error

        if status == ERROR and not self.terminal:
            self.publish(
                "run_error",
                {"error": error or {"type": "internal_error", "message": "运行失败"}},
            )

        # 无论正常结束、取消还是出错，都用 done 收尾，前端只需监听一个终止事件
        if not self.terminal:
            payload: dict[str, Any] = {
                "stop_reason": self.summary.get("stop_reason") or status,
                "final_text": self.summary.get("final_text"),
                "usage": self.summary.get("usage"),
            }
            # 排队中的用户插话：唯一的交付点（无论 run 以什么 reason 收尾都不会把输入吞掉）。
            # 用户主动 Stop（cancelled）时故意不带——那是"我不要这轮了"，不该再自动续发。
            if self.interrupts and status != CANCELLED:
                payload["interrupts"] = list(self.interrupts)
            self.publish(_TERMINAL_EVENT, payload)

        self.done.set()
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    def frames_after(self, seq: int) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if int(frame["id"]) > seq]

    # ---- 订阅 ----
    async def stream(self, *, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        """产出 SSE 帧：先重放缓冲，再续上实时流。

        快照与订阅注册之间没有 await，因此不会丢帧也不会重复。
        """
        replay = self.frames_after(after)
        queue: asyncio.Queue | None = None
        if not self.done.is_set():
            queue = asyncio.Queue()
            self._subscribers.add(queue)
        try:
            for frame in replay:
                yield frame
            if queue is None:
                return
            while True:
                item = await queue.get()
                if item is None:  # run 已结束的哨兵
                    break
                yield item
        finally:
            if queue is not None:
                self._subscribers.discard(queue)


class RunRegistry:
    """run 的登记、按会话去重，以及结束后的 TTL 回收。"""

    def __init__(self, *, ttl_s: float = 600.0) -> None:
        self._runs: dict[str, Run] = {}
        self._active: dict[str, str] = {}
        self._ttl_s = ttl_s

    def create(self, session_id: str) -> Run:
        run = Run(id=uuid.uuid4().hex, session_id=session_id)
        self._runs[run.id] = run
        self._active[session_id] = run.id
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def active_for(self, session_id: str) -> str | None:
        return self._active.get(session_id)

    def set_ttl(self, ttl_s: float) -> None:
        """更新回收 TTL（配置热重建时调用；对已在跑的 run 生效）。"""
        self._ttl_s = ttl_s

    def active_count(self) -> int:
        return len(self._active)

    def list_active(self) -> list[Run]:
        """当前运行中的 run（供并发看板）。"""
        return [run for run_id in self._active.values() if (run := self._runs.get(run_id))]

    def release(self, run: Run) -> None:
        """run 结束后解除占用，并安排 TTL 回收（保留缓冲供重连）。"""
        if self._active.get(run.session_id) == run.id:
            self._active.pop(run.session_id, None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 无事件循环（测试收尾场景）
            self._runs.pop(run.id, None)
            return
        loop.call_later(self._ttl_s, self._runs.pop, run.id, None)


# ---- 一轮对话的执行 ----
def make_agent(state: "AppState", run: Run) -> AgentLoop:
    """为本次 run 新建 AgentLoop（Loop 无状态，成本极低），把事件桥接到 run。"""
    return AgentLoop(
        state.provider,
        state.executor,
        state.registry,
        config=state.loop_config,
        prompt=state.prompt,
        tracer=state.tracer,
        on_event=run.publish_loop_event,
        # 软打断：用户插话后，本轮工具执行完就收尾（见 docs/agent-loop-design.md）
        should_interrupt=lambda: run.has_interrupts,
    )


def release_interactions(state: "AppState", run: Run) -> list[str]:
    """用默认值兑现该 run 上挂起的审批 / 提问，返回被释放的 id。

    软打断时立即调用：否则模型卡在审批上要等满 `interaction_timeout_s`（默认 300s），
    用户体感是「打断没反应」。
    """
    return state.interactions.resolve_for_run(run.id)


def _approval_hook(state: "AppState", run: Run, *, bypass: bool):
    """审批通道。"免批准"档不弹卡片，直接放行（仅记一条诊断 span 以便事后审计）。"""
    if bypass:

        async def auto_approve(request: ApprovalRequest) -> bool:
            try:
                state.tracer.record(
                    trace_id=run.trace_id,
                    tool="approval.auto",
                    target=request.tool,
                    reason="会话权限档位为「读写·免批准」，未向用户确认",
                )
            except Exception:  # noqa: BLE001 - 观测失败不应影响执行
                pass
            return True

        return auto_approve

    async def approval(request: ApprovalRequest) -> bool:
        interaction_id, future = state.interactions.open(run_id=run.id, default=False)
        run.publish(
            "interaction_required",
            {
                "id": interaction_id,
                "kind": "approval",
                "tool": request.tool,
                "args": request.args,
                "reason": request.reason,
            },
        )
        return await state.interactions.wait(interaction_id, future, default=False)

    return approval


def _ask_hook(state: "AppState", run: Run):
    async def ask(request: AskRequest) -> str:
        interaction_id, future = state.interactions.open(run_id=run.id, default="")
        run.publish(
            "interaction_required",
            {
                "id": interaction_id,
                "kind": "ask",
                "question": request.question,
                "options": list(request.options),
            },
        )
        return await state.interactions.wait(interaction_id, future, default="")

    return ask


def summarize(result: Any) -> dict[str, Any]:
    return {
        "stop_reason": result.stop_reason,
        "final_text": result.final_text,
        "usage": usage_dict(result.usage),
        "steps": len(result.steps),
        "error": result.error.to_dict() if result.error else None,
    }


def permission_level(session: Any) -> str | None:
    """读会话的权限档位。脏数据（旧日志 / 手改的 metadata）→ `None`（回落请求体 scopes）。"""
    raw = (session.meta.metadata or {}).get(PERMISSION_KEY) or None
    try:
        return normalize_permission(raw)
    except PermissionError_:
        return None


def session_scopes(session: Any, requested: list[str]) -> list[str]:
    """本轮生效的权限域。

    **有档位 → 由档位推导（单一真源，忽略请求体）；无档位 → 沿用请求体**。
    后者保证「没设置过档位」的旧会话行为完全不变。
    """
    level = permission_level(session)
    return list(scopes_for(level)) if level else list(requested)


def session_sandbox(session: Any) -> SandboxPolicy | None:
    """会话选了工作区 → 以它为 `root` 构造沙箱策略；没选 → `None`（沿用进程级默认）。

    - 全局的 `deny_paths` / 额外挂载点 / 域名黑名单**继续生效**（黑名单不该因选工作区而失效）。
    - `write_paths` 置空：工作区内全可写；能不能写由权限档位的 scopes 决定。
    """
    workspace = str((session.meta.metadata or {}).get(WORKSPACE_KEY) or "").strip()
    if not workspace:
        return None
    base = current_sandbox()
    return SandboxPolicy(
        enabled=True,
        root=Path(workspace).expanduser().resolve(),
        write_paths=(),
        deny_paths=base.deny_paths,
        readonly_mounts=base.readonly_mounts,
        writable_mounts=base.writable_mounts,
        extra_denied_domains=base.extra_denied_domains,
    )


async def run_turn(
    state: "AppState",
    run: Run,
    user_input: str | Message,
    *,
    scopes: list[str],
    metadata: dict[str, Any],
) -> None:
    """后台任务体：打开会话 → 解析会话设置 → 注入审批/提问通道 → run_turn → 收尾。"""
    try:
        session = await state.manager.open(run.session_id)
        level = permission_level(session)
        ctx = session.context(
            scopes=session_scopes(session, scopes),
            approval=_approval_hook(state, run, bypass=bool(level and approval_bypass(level))),
            ask=_ask_hook(state, run),
            sandbox=session_sandbox(session),
        )
        ctx.metadata.update(metadata)
        run.trace_id = ctx.trace_id  # 供 /runs/{id}/trace 与 SSE 事件关联
        result = await state.manager.run_turn(session, user_input, make_agent(state, run), ctx=ctx)
        run.finish(FINISHED, summary=summarize(result))
        _maybe_schedule_title(state, run, user_input, result.final_text)
    except asyncio.CancelledError:
        run.finish(CANCELLED)
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成 run 级错误
        run.finish(ERROR, error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        state.runs.release(run)


# ---- 自动标题（首轮问答的 LLM 总结；`done` 之后调度，绝不阻塞 SSE）----
def _message_text(value: str | Message) -> str:
    if isinstance(value, str):
        return value
    content = value.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def _maybe_schedule_title(
    state: "AppState", run: Run, user_input: str | Message, final_text: str | None
) -> None:
    namer = state.title_namer
    if namer is None or not state.manager.config.auto_title:
        return
    task = asyncio.create_task(
        _auto_title(state, run.session_id, _message_text(user_input), final_text)
    )
    state.background.add(task)
    task.add_done_callback(state.background.discard)


async def _auto_title(
    state: "AppState", session_id: str, user_text: str, final_text: str | None
) -> None:
    """只在「首轮且尚无标题」时写一次；任何异常都静默（标题不该影响对话）。"""
    try:
        session = await state.manager.open(session_id)
        if session.meta.title or session.meta.turns != 1:
            return
        title = await state.title_namer.generate(user_text, final_text) or fallback_title(
            user_text
        )
        await state.manager.commit(session, session.rename(title))
    except Exception:  # noqa: BLE001
        pass
