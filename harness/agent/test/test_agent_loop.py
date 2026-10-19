"""Agent Loop 测试（全离线：FakeProvider + 真实 registry，无需网络与 API Key）。

运行：python -m harness.agent.tests.test_agent_loop   （或 pytest）
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from harness.agent import (
    AgentLoop,
    EventType,
    LoopConfig,
    LoopGuard,
    PromptBuilder,
    StopReason,
    call_signature,
)
from harness.llm import ChatResponse, FakeProvider, LLMError, LLMErrorType, Message, ToolCall, Usage
from harness.memory import MemoryStore
from harness.skill import SkillRegistry
from harness.tool import (
    Context,
    Executor,
    ToolRegistry,
    ToolSpec,
    Tracer,
    current_trace_id,
    default_registry,
)


def _run(coro):
    return asyncio.run(coro)


def _text(content: str, usage: Usage | None = None) -> ChatResponse:
    return ChatResponse(content=content, usage=usage or Usage(1, 1, 2), finish_reason="stop")


def _calls(*calls: ToolCall, usage: Usage | None = None) -> ChatResponse:
    return ChatResponse(
        content=None,
        tool_calls=list(calls),
        usage=usage or Usage(1, 1, 2),
        finish_reason="tool_calls",
    )


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def echo(text: str) -> str:
        return f"echo:{text}"

    registry.register(ToolSpec(name="t.echo", description="回显"), echo)
    return registry


def _loop(
    responses,
    *,
    registry: ToolRegistry | None = None,
    config: LoopConfig | None = None,
    prompt: PromptBuilder | None = None,
    on_event=None,
    tracer: Tracer | None = None,
):
    provider = FakeProvider(responses)
    registry = registry if registry is not None else _echo_registry()
    loop = AgentLoop(
        provider,
        Executor(registry),
        registry,
        config=config,
        prompt=prompt,
        on_event=on_event,
        tracer=tracer,
    )
    return loop, provider


def _tmp_skills_root() -> tempfile.TemporaryDirectory:
    root = tempfile.TemporaryDirectory()
    directory = Path(root.name) / "demo-skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能\n---\n正文", encoding="utf-8"
    )
    return root


# ---- M1：Prompt 组装 ----
def test_prompt_prepends_system_message():
    prompt = PromptBuilder(base_instructions="你是助手")
    built = prompt.build([Message.user("hi")])
    assert built[0].role == "system" and built[0].content == "你是助手"
    assert built[1].role == "user"


def test_prompt_merges_existing_system_message():
    prompt = PromptBuilder(base_instructions="基础指令")
    built = prompt.build([Message.system("用户自定义"), Message.user("hi")])
    assert len(built) == 2
    assert built[0].role == "system"
    assert "基础指令" in built[0].content and "用户自定义" in built[0].content


def _tmp_memory_file() -> str:
    return str(Path(tempfile.mkdtemp()) / "MEMORY.md")


def test_prompt_includes_memory_after_skill_catalog():
    root = _tmp_skills_root()
    memory = MemoryStore(_tmp_memory_file())
    try:
        memory.save("回复用中文", section="用户偏好")
        prompt = PromptBuilder(
            base_instructions="你是助手", skills=SkillRegistry(root.name), memory=memory
        )
        system = prompt.system_prompt()
        # 顺序固定：base → skill → memory（顺序变了会击穿前缀缓存）
        assert system.index("你是助手") < system.index("可用技能") < system.index("长期记忆")
        assert "### 用户偏好" in system and "回复用中文" in system
    finally:
        root.cleanup()


def test_prompt_omits_empty_memory_section():
    prompt = PromptBuilder(memory=MemoryStore(_tmp_memory_file()))
    assert prompt.system_prompt() == ""  # 空记忆不产生空节
    assert "长期记忆" not in PromptBuilder(memory=None).system_prompt()


def test_prompt_is_byte_stable_when_memory_unchanged():
    memory = MemoryStore(_tmp_memory_file())
    memory.save("一条记忆")
    prompt = PromptBuilder(base_instructions="你是助手", memory=memory)
    assert prompt.system_prompt() == prompt.system_prompt()


def test_prompt_can_disable_skill_catalog():
    root = _tmp_skills_root()
    try:
        prompt = PromptBuilder(skills=SkillRegistry(root.name), include_skill_catalog=False)
        assert prompt.system_prompt() == ""
        assert prompt.build([Message.user("hi")]) == [Message.user("hi")]
    finally:
        root.cleanup()


# ---- M2：核心循环 ----
def test_single_turn_text_final():
    loop, provider = _loop([_text("你好")], prompt=PromptBuilder())
    result = _run(loop.run([Message.user("hi")]))
    assert result.stop_reason == StopReason.FINAL
    assert result.final_text == "你好"
    assert len(result.steps) == 1
    assert len(result.messages) == 2  # user + assistant
    assert result.messages[-1].role == "assistant"
    assert result.usage.total_tokens == 2


def test_tool_call_then_final():
    loop, provider = _loop(
        [
            _calls(ToolCall(id="c1", name="t.echo", arguments=json.dumps({"text": "hi"}))),
            _text("完成"),
        ]
    )
    result = _run(loop.run([Message.user("调用工具")]))
    assert result.stop_reason == StopReason.FINAL
    assert result.final_text == "完成"
    assert len(provider.requests) == 2

    # 第二轮的请求里应带上工具定义
    assert provider.requests[1].tools is not None
    assert provider.requests[1].tools[0]["function"]["name"] == "t.echo"

    # 消息序列：user / assistant(tool_calls) / tool / assistant
    roles = [m.role for m in result.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = result.messages[2]
    assert tool_msg.tool_call_id == "c1" and tool_msg.content == "echo:hi"
    assert result.messages[1].tool_calls[0].id == "c1"


def test_multi_step_loop():
    loop, provider = _loop(
        [
            _calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"a"}')),
            _calls(ToolCall(id="c2", name="t.echo", arguments='{"text":"b"}')),
            _text("done"),
        ]
    )
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.FINAL
    # 每轮迭代都计入 steps：2 轮工具 + 1 轮收尾
    assert len(result.steps) == 3
    assert len(provider.requests) == 3
    # 累计 3 轮 usage
    assert result.usage.total_tokens == 6


def test_usage_accumulates():
    loop, _ = _loop(
        [
            _calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"a"}'), usage=Usage(2, 3, 5)),
            _text("done", usage=Usage(1, 1, 2)),
        ]
    )
    result = _run(loop.run([Message.user("go")]))
    assert result.usage == Usage(3, 4, 7)


# ---- M3：停止条件 ----
def test_max_steps():
    always_tool = [
        _calls(ToolCall(id=f"c{i}", name="t.echo", arguments='{"text":"x"}')) for i in range(10)
    ]
    loop, _ = _loop(always_tool, config=LoopConfig(max_steps=3))
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.MAX_STEPS
    assert len(result.steps) == 3


def test_token_budget():
    loop, _ = _loop(
        [
            _calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"x"}'), usage=Usage(50, 50, 100)),
            _text("never reached", usage=Usage(1, 1, 2)),
        ],
        config=LoopConfig(max_total_tokens=50),
    )
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.TOKEN_BUDGET
    assert result.final_text is None


def test_no_budget_limit_by_default():
    loop, _ = _loop([_text("ok", usage=Usage(10**6, 10**6, 2 * 10**6))])
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.FINAL


def test_llm_error_stops_run_without_raising():
    class _BoomProvider:
        async def complete(self, request):
            raise LLMError(LLMErrorType.AUTH, "bad key", retryable=False, status=401)

        async def aclose(self):
            pass

    registry = _echo_registry()
    loop = AgentLoop(_BoomProvider(), Executor(registry), registry)
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.ERROR
    assert result.error is not None and result.error.type == LLMErrorType.AUTH
    assert result.final_text is None


def test_retryable_error_also_returned_not_raised():
    class _RateLimitedProvider:
        async def complete(self, request):
            raise LLMError(LLMErrorType.RATE_LIMIT, "busy", retryable=True, status=429)

        async def aclose(self):
            pass

    registry = _echo_registry()
    loop = AgentLoop(_RateLimitedProvider(), Executor(registry), registry)
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.ERROR and result.error.retryable is True


# ---- M3：事件回调 ----
def test_event_sequence():
    events = []
    loop, _ = _loop(
        [
            _calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"a"}')),
            _text("done"),
        ],
        on_event=events.append,
    )
    _run(loop.run([Message.user("go")]))
    types = [e.type for e in events]
    assert types == [
        EventType.RUN_STARTED,
        EventType.MODEL_RESPONSE,
        EventType.TOOL_FINISHED,
        EventType.STEP_FINISHED,
        EventType.MODEL_RESPONSE,
        EventType.RUN_FINISHED,
    ]
    assert events[1].step == 1 and events[1].tool_calls[0].name == "t.echo"
    assert events[-1].stop_reason == StopReason.FINAL
    assert events[-1].usage.total_tokens == 4


def test_async_event_hook():
    seen = []

    async def hook(event):
        seen.append(event.type)

    loop, _ = _loop([_text("ok")], on_event=hook)
    _run(loop.run([Message.user("go")]))
    assert seen == [EventType.RUN_STARTED, EventType.MODEL_RESPONSE, EventType.RUN_FINISHED]


def test_event_hook_exception_is_swallowed():
    def bad_hook(event):
        raise RuntimeError("前端推送失败")

    loop, _ = _loop([_text("ok")], on_event=bad_hook)
    result = _run(loop.run([Message.user("go")]))
    assert result.stop_reason == StopReason.FINAL and result.final_text == "ok"


# ---- M4：审批/提问缺回调 → 回喂模型 ----
def test_missing_approval_is_refilled_to_model():
    # shell.run 需要 shell:run 权限与审批；此处不给 approval 回调
    path = Path(tempfile.mkdtemp()) / "out.txt"
    loop, provider = _loop(
        [
            _calls(
                ToolCall(
                    id="c1",
                    name="shell.run",
                    arguments=json.dumps({"command": "echo hi"}),
                )
            ),
            _text("那我换个方式"),
        ],
        registry=default_registry,
    )
    result = _run(loop.run([Message.user("执行命令")], ctx=Context(scopes={"shell:run"})))

    assert result.stop_reason == StopReason.FINAL  # 不中断
    tool_msg = result.messages[2]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "c1"
    assert "approval_required" in tool_msg.content  # 结构化错误已回喂
    assert result.final_text == "那我换个方式"
    assert not path.exists()


def test_approval_hook_is_passed_through():
    approved = []

    async def approve(request):
        approved.append(request.tool)
        return True

    loop, _ = _loop(
        [
            _calls(
                ToolCall(id="c1", name="shell.run", arguments=json.dumps({"command": "echo hi"}))
            ),
            _text("ok"),
        ],
        registry=default_registry,
    )
    result = _run(
        loop.run(
            [Message.user("执行命令")],
            ctx=Context(scopes={"shell:run"}, approval=approve),
        )
    )
    assert approved == ["shell.run"]
    assert result.messages[2].content.startswith("exit=0")


# ---- M3/M4：遥测 ----
def test_tracer_records_run_span():
    tracer = Tracer()
    loop, _ = _loop([_text("ok")], tracer=tracer, prompt=PromptBuilder())
    _run(loop.run([Message.user("go")]))
    run_spans = [s for s in tracer.spans if s["tool"] == "agent.run"]
    assert run_spans and run_spans[0]["stop_reason"] == StopReason.FINAL
    assert run_spans[0]["total_tokens"] == 2


def test_tracer_records_parent_span_id_from_context():
    tracer = Tracer()
    loop, _ = _loop([_text("ok")], tracer=tracer, prompt=PromptBuilder())
    ctx = Context(metadata={"parent_span_id": "parent-1"})
    _run(loop.run([Message.user("go")], ctx=ctx))
    run_span = next(s for s in tracer.spans if s["tool"] == "agent.run")
    assert run_span["parent_span_id"] == "parent-1"


# ---- M6：trace 贯通与聚合 ----
class _TracingProvider:
    """像真 Provider 那样在 complete 里记 llm span——**不显式传 trace_id**。

    用于验证 `Tracer.record` 的 contextvar 兜底：Provider 拿不到 Context，
    但仍应把 span 关联到当前 run。
    """

    def __init__(self, responses, tracer: Tracer) -> None:
        self._inner = FakeProvider(responses)
        self._tracer = tracer

    async def complete(self, request):
        response = await self._inner.complete(request)
        self._tracer.record(tool="llm.complete", ok=True, duration_ms=1.0)
        return response

    async def aclose(self) -> None:
        return None


def test_llm_span_carries_current_trace_id():
    tracer = Tracer()
    provider = _TracingProvider([_text("ok")], tracer)
    registry = _echo_registry()
    loop = AgentLoop(
        provider,
        Executor(registry, tracer=tracer),
        registry,
        prompt=PromptBuilder(),
        tracer=tracer,
    )
    ctx = Context()
    _run(loop.run([Message.user("go")], ctx=ctx))

    llm_span = next(s for s in tracer.spans if s["tool"] == "llm.complete")
    assert llm_span["trace_id"] == ctx.trace_id
    # 一次 run 的 span 能按 trace_id 全部取回：llm + run
    assert {s["tool"] for s in tracer.spans_for(ctx.trace_id)} == {"llm.complete", "agent.run"}
    assert tracer.summary(ctx.trace_id)["span_count"] == 2


def test_current_trace_is_reset_after_run():
    loop, _ = _loop([_text("ok")], prompt=PromptBuilder())
    assert current_trace_id() is None
    _run(loop.run([Message.user("go")]))
    assert current_trace_id() is None  # 不跨 run 泄漏


def test_run_span_has_duration_ms():
    tracer = Tracer()
    loop, _ = _loop([_text("ok")], tracer=tracer, prompt=PromptBuilder())
    _run(loop.run([Message.user("go")]))
    run_span = next(s for s in tracer.spans if s["tool"] == "agent.run")
    assert isinstance(run_span["duration_ms"], float) and run_span["duration_ms"] >= 0


def test_record_without_trace_context_omits_trace_id():
    tracer = Tracer()
    span = tracer.record(tool="x")  # 不在任何 run 内
    assert "trace_id" not in span


def test_tracer_tree_groups_by_parent_span_id():
    tracer = Tracer()
    root = tracer.record(tool="root", trace_id="t1")
    tracer.record(tool="child", trace_id="t1", parent_span_id=root["span_id"])
    tracer.record(tool="other", trace_id="t2")

    tree = tracer.tree("t1")
    assert len(tree) == 1
    assert tree[0]["span"]["tool"] == "root"
    assert [node["span"]["tool"] for node in tree[0]["children"]] == ["child"]
    assert tracer.summary("t1") == {
        "span_count": 2,
        "error_count": 0,
        "duration_ms": None,
        "stop_reason": None,
    }


def test_tracer_tree_keeps_orphan_as_root():
    tracer = Tracer()
    tracer.record(tool="orphan", trace_id="t1", parent_span_id="missing")
    tree = tracer.tree("t1")
    assert len(tree) == 1 and tree[0]["span"]["tool"] == "orphan"


def test_spans_form_tree_rooted_at_run_span():
    """工具 span 自动挂到 run 根 span 下——trace 是树，不是一堆散点。"""
    tracer = Tracer()
    registry = _echo_registry()
    loop = AgentLoop(
        FakeProvider(
            [_calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"hi"}')), _text("ok")]
        ),
        Executor(registry, tracer=tracer),
        registry,
        prompt=PromptBuilder(),
        tracer=tracer,
    )
    ctx = Context()
    _run(loop.run([Message.user("go")], ctx=ctx))

    tree = tracer.tree(ctx.trace_id)
    assert len(tree) == 1
    assert tree[0]["span"]["tool"] == "agent.run"
    assert [node["span"]["tool"] for node in tree[0]["children"]] == ["t.echo"]
    assert tracer.summary(ctx.trace_id)["span_count"] == 2


def test_run_span_stays_root_when_no_parent_in_context():
    """根 span 不做「当前父」兜底，否则会自己挂自己。"""
    tracer = Tracer()
    loop, _ = _loop([_text("ok")], tracer=tracer, prompt=PromptBuilder())
    _run(loop.run([Message.user("go")]))
    run_span = next(s for s in tracer.spans if s["tool"] == "agent.run")
    assert "parent_span_id" not in run_span


# ---- 与 skills / tools 的接线 ----
def test_skill_load_available_as_tool():
    from harness.skill import register_skill_tools

    root = _tmp_skills_root()
    try:
        registry = ToolRegistry()
        register_skill_tools(registry, SkillRegistry(root.name))
        loop, provider = _loop(
            [
                _calls(ToolCall(id="c1", name="skill.load", arguments='{"name":"demo-skill"}')),
                _text("已加载"),
            ],
            registry=registry,
            prompt=PromptBuilder(skills=SkillRegistry(root.name)),
        )
        result = _run(loop.run([Message.user("用技能")]))
        assert result.stop_reason == StopReason.FINAL
        tool_msg = next(m for m in result.messages if m.role == "tool")
        assert "正文" in tool_msg.content
        # skill 目录进了 system prompt，而工具定义走 tools 参数
        system_msg = next(m for m in result.messages if m.role == "system")
        assert "demo-skill" in system_msg.content
        assert "skill.load" not in system_msg.content
    finally:
        root.cleanup()


# ---- M5：流式（text_delta）----
def test_stream_config_emits_text_delta_events():
    events: list[tuple[str, str | None]] = []
    registry = _echo_registry()
    # chunk_size=1 保证文本被切成多个增量，用于验证「逐增量」而非「整段」
    loop = AgentLoop(
        FakeProvider([_text("你好世界")], chunk_size=1),
        Executor(registry),
        registry,
        config=LoopConfig(stream=True),
        on_event=lambda event: events.append((event.type, event.text)),
    )
    result = _run(loop.run([Message.user("hi")]))

    assert result.stop_reason == StopReason.FINAL
    assert result.final_text == "你好世界"
    deltas = [text for kind, text in events if kind == EventType.TEXT_DELTA]
    assert len(deltas) > 1, "应被切成多段增量"
    assert "".join(t for t in deltas if t) == "你好世界"

    kinds = [kind for kind, _ in events]
    assert kinds[0] == EventType.RUN_STARTED and kinds[-1] == EventType.RUN_FINISHED
    assert kinds.index(EventType.MODEL_RESPONSE) > kinds.index(EventType.TEXT_DELTA)


def test_stream_off_by_default_emits_no_delta():
    kinds: list[str] = []
    loop, _ = _loop([_text("abc")], on_event=lambda event: kinds.append(event.type))
    _run(loop.run([Message.user("hi")]))
    assert EventType.TEXT_DELTA not in kinds


def test_run_finished_event_carries_final_text():
    events = []
    loop, _ = _loop([_text("done!")], on_event=lambda event: events.append(event))
    _run(loop.run([Message.user("hi")]))
    finished = next(e for e in events if e.type == EventType.RUN_FINISHED)
    assert finished.text == "done!" and finished.stop_reason == StopReason.FINAL


# ---- 循环保护（重复调用检测）----
def test_loop_guard_signature_is_key_order_insensitive():
    a = ToolCall(id="1", name="fs.read", arguments='{"path":"a.txt","max_bytes":10}')
    b = ToolCall(id="2", name="fs.read", arguments='{ "max_bytes": 10 , "path" : "a.txt" }')
    other = ToolCall(id="3", name="fs.read", arguments='{"path":"b.txt","max_bytes":10}')

    assert call_signature(a) == call_signature(b)  # 键序/空白无关
    assert call_signature(a) != call_signature(other)

    guard = LoopGuard(warn_at=2, stop_at=3)
    assert guard.inspect([a]).warnings == {}
    assert guard.inspect([b]).warnings == {0: 2}  # 参数等价 → 同一签名
    assert guard.inspect([other]).warnings == {}
    assert guard.inspect([a]).stopped is True


def _repeated_calls(count: int) -> list[ChatResponse]:
    return [
        _calls(ToolCall(id=f"c{index}", name="t.echo", arguments='{"text":"same"}'))
        for index in range(count)
    ]


def test_repeated_identical_calls_warn_then_stop():
    loop, _ = _loop(
        _repeated_calls(6),
        config=LoopConfig(max_steps=10, repeat_warn_at=3, repeat_stop_at=6),
    )
    result = _run(loop.run([Message.user("hi")]))

    assert result.stop_reason == StopReason.LOOP_DETECTED
    assert len(result.steps) == 6  # 第 6 次触发终止，之前每步都照常执行完
    tool_messages = [m for m in result.messages if m.role == "tool"]
    warned = [m for m in tool_messages if "循环保护" in (m.content or "")]
    assert len(warned) == 1, "同一签名只提醒一次"
    assert warned[0].content.startswith("echo:same")  # 工具输出保留，提醒是追加的
    assert "第 3 次" in warned[0].content


def test_repeat_guard_can_be_disabled():
    loop, _ = _loop(
        _repeated_calls(4),
        config=LoopConfig(max_steps=4, repeat_warn_at=0, repeat_stop_at=0),
    )
    result = _run(loop.run([Message.user("hi")]))

    assert result.stop_reason == StopReason.MAX_STEPS
    assert not any(
        "循环保护" in (m.content or "") for m in result.messages if m.role == "tool"
    )


def test_different_arguments_are_not_treated_as_repeats():
    responses = [
        _calls(ToolCall(id=f"c{index}", name="t.echo", arguments=f'{{"text":"v{index}"}}'))
        for index in range(4)
    ]
    loop, _ = _loop(responses, config=LoopConfig(max_steps=4))
    result = _run(loop.run([Message.user("hi")]))

    assert result.stop_reason == StopReason.MAX_STEPS
    assert not any(
        "循环保护" in (m.content or "") for m in result.messages if m.role == "tool"
    )


# ---- 软打断（用户插话）----
class _Flag:
    """可变开关：测试里先用 False 起步，跑到一半再置 True。"""

    def __init__(self, value: bool = False) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


def _interruptible_loop(responses, flag, **kwargs):
    provider = FakeProvider(responses)
    registry = kwargs.pop("registry", None) or _echo_registry()
    loop = AgentLoop(
        provider,
        Executor(registry),
        registry,
        should_interrupt=flag,
        **kwargs,
    )
    return loop, provider


def test_stop_reason_interrupted_constant():
    assert StopReason.INTERRUPTED == "interrupted"


def test_interrupt_after_tool_step():
    """第 1 步工具执行完 → 命中插话 → 立即收尾，不再进入第 2 步。"""
    flag = _Flag()
    provider = FakeProvider(
        [
            _calls(ToolCall(id="c1", name="t.echo", arguments=json.dumps({"text": "a"}))),
            _text("不该被执行"),
        ]
    )

    def mark(text: str) -> str:
        flag.value = True  # 工具执行期间用户发来了新消息
        return f"echo:{text}"

    registry = ToolRegistry()
    registry.register(ToolSpec(name="t.echo", description="回显"), mark)
    loop = AgentLoop(
        provider, Executor(registry), registry, should_interrupt=flag
    )

    result = _run(loop.run([Message.user("hi")]))
    assert result.stop_reason == StopReason.INTERRUPTED
    assert result.final_text is None
    assert len(result.steps) == 1
    # 历史保持合法：assistant(tool_calls) 后紧跟成对的 tool 消息
    roles = [m.role for m in result.messages]
    assert roles[-2:] == ["assistant", "tool"]
    assert result.messages[-2].tool_calls and result.messages[-1].tool_call_id == "c1"


def test_interrupt_not_requested_runs_to_final():
    loop, _ = _interruptible_loop([_text("正常结束")], _Flag(False))
    result = _run(loop.run([Message.user("hi")]))
    assert result.stop_reason == StopReason.FINAL and result.final_text == "正常结束"


def test_interrupt_callback_exception_is_ignored():
    def boom() -> bool:
        raise RuntimeError("宿主坏了")

    provider = FakeProvider([_text("照样跑")])
    registry = _echo_registry()
    loop = AgentLoop(
        provider, Executor(registry), registry, should_interrupt=boom
    )
    result = _run(loop.run([Message.user("hi")]))
    assert result.stop_reason == StopReason.FINAL and result.final_text == "照样跑"


def test_interrupt_priority_over_loop_detected():
    """同时命中循环保护时以 interrupted 收尾（用户意图优先）。"""
    calls = [
        _calls(ToolCall(id=f"c{i}", name="t.echo", arguments=json.dumps({"text": "x"})))
        for i in range(3)
    ] + [_text("不该被执行")]
    # 第 1 次重复就触发循环保护；插话检查点在它之前，所以结果是 interrupted
    config = LoopConfig(repeat_warn_at=0, repeat_stop_at=1)
    loop, _ = _interruptible_loop(calls, _Flag(True), config=config)
    result = _run(loop.run([Message.user("hi")]))
    assert result.stop_reason == StopReason.INTERRUPTED


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"PASS {fn.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")


if __name__ == "__main__":
    main()
