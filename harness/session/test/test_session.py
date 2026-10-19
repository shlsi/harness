"""会话管理测试（全离线）。

运行：python -m harness.session.test.test_session   （或 pytest）
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from harness.agent import AgentLoop, LoopConfig, PromptBuilder
from harness.llm import (
    ChatResponse,
    FakeProvider,
    Message,
    ToolCall,
    Usage,
)
from harness.session import (
    EVENT_COMPACT,
    SUMMARY_PREFIX,
    CompressionResult,
    JsonlSessionStore,
    LLMCompressor,
    MemorySessionStore,
    Session,
    SessionConfig,
    SessionEvent,
    SessionManager,
    SessionNotFound,
    TitleGenerator,
    fallback_title,
    replay,
    split_summary_and_memories,
)
from harness.tool import Context, Executor, ToolRegistry, ToolSpec


def _run(coro):
    return asyncio.run(coro)


def _text(content: str) -> ChatResponse:
    return ChatResponse(content=content, usage=Usage(1, 1, 2), finish_reason="stop")


def _calls(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(
        content=None, tool_calls=list(calls), usage=Usage(1, 1, 2), finish_reason="tool_calls"
    )


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def echo(text: str) -> str:
        return f"echo:{text}"

    registry.register(ToolSpec(name="t.echo", description="回显"), echo)
    return registry


def _loop(responses, *, registry=None, prompt=None) -> AgentLoop:
    registry = registry if registry is not None else _echo_registry()
    return AgentLoop(
        FakeProvider(responses),
        Executor(registry),
        registry,
        config=LoopConfig(max_steps=4),
        prompt=prompt or PromptBuilder(),
    )


class _StubCompressor:
    """测试压缩器：返回可预测的摘要，不调模型（生产只有 `LLMCompressor`）。"""

    def __init__(self, text: str = "摘要") -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    async def compress(self, messages) -> str:
        self.calls.append(list(messages))
        return self.text


def _manager(store=None, *, compressor=None, **kwargs) -> SessionManager:
    return SessionManager(
        store if store is not None else MemorySessionStore(),
        compressor=compressor if compressor is not None else _StubCompressor(),
        **kwargs,
    )


# ---- M1：分段布局与事件 ----
def test_build_messages_four_sections():
    session = Session.create(system=[Message.system("你是助手")])
    session.append(Message.user("你好"))
    assert [m.role for m in session.build_messages()] == ["system", "user"]

    session.compact("早期消息摘要", dropped=1)
    session.append(Message.assistant("在"))
    roles = [m.role for m in session.build_messages()]
    assert roles == ["system", "system", "assistant"]
    assert session.build_messages()[1].content.startswith(SUMMARY_PREFIX)


def test_build_messages_renders_head_before_summary():
    session = Session.create(system=[Message.system("s")])
    session.append(Message.user("u1"), Message.user("u2"), Message.user("u3"))
    events = session.adopt_head(2)          # u1/u2 固化为固定头
    session.compact("中间摘要", dropped=1)   # u3 被压掉
    assert [e.type for e in events] == ["meta"]
    assert [m.content for m in session.build_messages()] == ["s", "u1", "u2", SUMMARY_PREFIX + "\n中间摘要"]
    assert [m.role for m in session.build_messages()] == ["system", "user", "user", "system"]


def test_build_messages_is_prefix_monotonic():
    session = Session.create(system=[Message.system("s")])
    session.append(Message.user("u1"))
    first = session.build_messages()
    session.append(Message.assistant("a1"))
    second = session.build_messages()
    # 已发出的前缀保持不变，只在尾部追加
    assert [id(m) for m in second[: len(first)]] == [id(m) for m in first]
    assert len(second) == len(first) + 1


def test_record_takes_only_new_messages():
    from harness.agent import RunResult

    session = Session.create(system=[Message.system("s")])
    session.append(Message.user("u1"))
    sent = session.build_messages()
    result = RunResult(
        messages=sent + [Message.assistant("a1"), Message.tool("c1", "ok")],
        final_text="a1",
        steps=[],
        usage=Usage(),
        stop_reason="final",
    )
    events = session.record(result, sent)
    assert len(session.active) == 3  # u1 + a1 + tool
    assert session.meta.turns == 1
    assert [e.type for e in events].count("message") == 2


def test_record_handles_injected_system_prefix():
    from harness.agent import RunResult

    session = Session.create(system=[Message.system("s")])
    session.append(Message.user("u1"))
    sent = session.build_messages()
    # 模拟上层在头部注入了 system（如 PromptBuilder 非空配置）
    result = RunResult(
        messages=[Message.system("注入的指令")] + sent + [Message.assistant("a1")],
        final_text="a1",
        steps=[],
        usage=Usage(),
        stop_reason="final",
    )
    session.record(result, sent)
    assert len(session.active) == 2
    assert session.active[-1].content == "a1"
    # 注入的 system 没有被误当成新增消息
    assert all(m.role != "system" for m in session.active)


# ---- M1/M2：存储与重放 ----
def test_replay_restores_state():
    session = Session.create(system=[Message.system("s")], title="标题")
    events = [__meta_event(session)]
    events += session.append(Message.user("u1"), Message.assistant("a1"))
    events += session.compact("摘要", dropped=1)

    restored = replay(session.id, events)
    assert restored.meta.title == "标题"
    assert restored.system[0].content == "s"
    assert restored.summary is not None and "摘要" in restored.summary.content
    assert [m.content for m in restored.active] == ["a1"]
    assert restored.build_messages() == session.build_messages()


def __meta_event(session: Session):
    from harness.session import SessionEvent

    return SessionEvent.meta(
        session_id=session.id,
        system=[m.to_dict() for m in session.system],
        title=session.meta.title,
    )


def test_record_accumulates_usage_and_replay():
    from harness.agent import RunResult

    session = Session.create(system=[Message.system("s")])
    session.append(Message.user("u1"))
    sent = session.build_messages()
    result = RunResult(
        messages=sent + [Message.assistant("a1")],
        final_text="a1",
        steps=[],
        usage=Usage(10, 5, 15),
        stop_reason="final",
    )
    events = [__meta_event(session)] + session.record(result, sent)
    assert session.meta.usage_totals == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    restored = replay(session.id, events)
    assert restored.meta.usage_totals == session.meta.usage_totals
    assert restored.meta.turns == 1


def test_replay_tolerates_legacy_log_without_usage():
    session = Session.create(system=[Message.system("s")])
    events = [__meta_event(session)] + session.append(Message.user("u1"))
    events.append(SessionEvent.meta(turns=1))  # 旧日志：有 turns，无 usage 事件
    restored = replay(session.id, events)
    assert restored.meta.turns == 1
    assert restored.meta.usage_totals == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_rename_is_replayed():
    session = Session.create(system=[Message.system("s")], title="旧标题")
    events = [__meta_event(session)] + session.rename("新标题")
    assert session.meta.title == "新标题"
    restored = replay(session.id, events)
    assert restored.meta.title == "新标题"


def test_set_metadata_merges_and_is_replayed():
    """会话设置（工作区 / 权限档位）走 set_metadata：合并语义 + 一条 meta 事件即可回放。"""
    session = Session.create(system=[Message.system("s")])
    events = [__meta_event(session)]
    # 预置一个「创建时就带、日志里没记」的键，用来验证合并不会误删
    session.meta.metadata["keep"] = "me"

    added = session.set_metadata(workspace="/tmp/ws", permission="write")
    assert len(added) == 1 and added[0].type == "meta"
    assert session.meta.metadata == {
        "keep": "me", "workspace": "/tmp/ws", "permission": "write",
    }
    # 空调用不产事件（避免无意义的落库）
    assert session.set_metadata() == []

    restored = replay(session.id, events + added)
    assert restored.meta.metadata == {"workspace": "/tmp/ws", "permission": "write"}


def test_context_carries_sandbox():
    """`Session.context(sandbox=...)` 透传给工具上下文（会话级工作区的入口）。"""
    marker = object()
    session = Session.create(session_id="s1")
    ctx = session.context(scopes={"fs:read"}, sandbox=marker)
    assert ctx.scopes == {"fs:read"}
    assert ctx.sandbox is marker
    # 不传 → None（沿用进程级默认沙箱）
    assert session.context(scopes={"fs:read"}).sandbox is None


def test_force_compact_below_threshold():
    async def scenario():
        compressor = _StubCompressor("摘要X")
        manager = _manager(
            compressor=compressor,
            config=SessionConfig(compact_threshold=1000, keep_tail=1),
        )
        session = await manager.create(session_id="s1", system=[Message.system("s")])
        await manager.commit(
            session, session.append(*(Message.user(f"u{i}") for i in range(5)))
        )

        # 远未达阈值：非 force 不压
        assert await manager.compact(session) == []
        assert len(session.active) == 5

        events = await manager.compact(session, force=True)
        assert any(e.type == EVENT_COMPACT for e in events)
        assert len(session.active) == 1  # keep_tail=1
        assert session.summary is not None

    _run(scenario())


def test_fallback_title_truncates_first_line():
    assert fallback_title("帮我写个函数") == "帮我写个函数"
    assert fallback_title("") == "新会话"
    title = fallback_title("长" * 40, max_chars=10)
    assert title == "长" * 10 + "…"


def test_title_generator_cleans_output_and_returns_none_on_empty():
    async def scenario():
        gen = TitleGenerator(FakeProvider([_text("标题：部署流程")]))
        assert await gen.generate("帮我部署", "好的") == "部署流程"
        # provider 无内容 → None（调用方回退）
        empty = TitleGenerator(FakeProvider([]))
        assert await empty.generate("你好", None) is None

    _run(scenario())


def test_memory_store_roundtrip():
    async def scenario():
        store = MemorySessionStore()
        manager = _manager(store)
        session = await manager.create(session_id="s1", system=[Message.system("s")])
        await manager.commit(session, session.append(Message.user("hi")))

        reopened = await manager.open("s1")
        assert [m.content for m in reopened.active] == ["hi"]
        metas = await manager.list()
        assert [m.session_id for m in metas] == ["s1"]

        await manager.delete("s1")
        assert await manager.list() == []

    _run(scenario())


def test_memory_store_open_missing_raises():
    async def scenario():
        manager = _manager()
        try:
            await manager.open("nope")
        except SessionNotFound as exc:
            assert exc.session_id == "nope"
        else:
            raise AssertionError("未知会话应抛 SessionNotFound")

    _run(scenario())


def test_jsonl_store_is_append_only():
    with tempfile.TemporaryDirectory() as d:
        async def scenario():
            store = JsonlSessionStore(d)
            manager = _manager(store)
            session = await manager.create(session_id="s1", system=[Message.system("s")])
            await manager.commit(session, session.append(Message.user("u1")))

            path = Path(d) / "s1.jsonl"
            first = path.read_text(encoding="utf-8")
            assert first.count("\n") == 2  # meta + message

            await manager.commit(session, session.append(Message.assistant("a1")))
            second = path.read_text(encoding="utf-8")
            assert second.startswith(first)  # 只追加，不改写历史
            assert second.count("\n") == 3

            reopened = await manager.open("s1")
            assert [m.content for m in reopened.active] == ["u1", "a1"]
            metas = await manager.list()
            assert len(metas) == 1 and metas[0].session_id == "s1"

            await manager.delete("s1")
            assert not path.exists()

        _run(scenario())


def test_jsonl_store_rejects_unsafe_id():
    with tempfile.TemporaryDirectory() as d:
        store = JsonlSessionStore(d)
        try:
            _run(store.append_events("../evil", []))
        except ValueError as exc:
            assert "session_id" in str(exc)
        else:
            raise AssertionError("非法 session_id 应报错")


def test_jsonl_store_skips_broken_lines():
    with tempfile.TemporaryDirectory() as d:
        async def scenario():
            store = JsonlSessionStore(d)
            manager = _manager(store)
            session = await manager.create(session_id="s1")
            await manager.commit(session, session.append(Message.user("u1")))
            with (Path(d) / "s1.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"type":"message","data":{"messa')  # 半行
            reopened = await manager.open("s1")
            assert [m.content for m in reopened.active] == ["u1"]

        _run(scenario())


# ---- M3：压缩 ----
def test_llm_compressor_uses_provider():
    provider = FakeProvider([_text("这是摘要")])
    text = _run(LLMCompressor(provider).compress([Message.user("u1"), Message.assistant("a1")]))
    assert text == "这是摘要"
    assert provider.requests[0].messages[0].role == "system"


def test_maybe_compact_folds_head_block():
    async def scenario():
        stub = _StubCompressor("摘要")
        manager = _manager(
            compressor=stub,
            config=SessionConfig(compact_threshold=5, keep_tail=2),
        )
        session = await manager.create(session_id="s1", system=[Message.system("s")])
        await manager.commit(
            session,
            session.append(*[Message.user(f"u{i}") for i in range(6)]),
        )
        events = await manager.maybe_compact(session)
        assert [e.type for e in events] == ["compact"]
        assert len(session.active) == 2
        assert session.summary is not None and "摘要" in session.summary.content
        # 前缀：system + summary + active
        assert [m.role for m in session.build_messages()] == ["system", "system", "user", "user"]
        # 只有被丢掉的那 4 条进了压缩器
        assert [m.content for m in stub.calls[0]] == ["u0", "u1", "u2", "u3"]

        # 未达阈值时不触发
        assert await manager.maybe_compact(session) == []

    _run(scenario())


def test_keep_head_protects_opening_messages():
    async def scenario():
        stub = _StubCompressor("摘要")
        manager = _manager(
            compressor=stub,
            config=SessionConfig(compact_threshold=5, keep_head=2, keep_tail=1),
        )
        session = await manager.create(session_id="s1", system=[Message.system("s")])
        await manager.commit(session, session.append(*[Message.user(f"u{i}") for i in range(6)]))
        events = await manager.maybe_compact(session)

        # 一条 meta（固化固定头）+ 一条 compact
        assert [e.type for e in events] == ["meta", "compact"]
        assert [m.content for m in session.head] == ["u0", "u1"]
        assert [m.content for m in session.active] == ["u5"]
        # 只压缩了中间 3 条（u2/u3/u4）；u0/u1 与 u5 都没进压缩器
        assert [m.content for m in stub.calls[0]] == ["u2", "u3", "u4"]
        # 渲染顺序：system → head → summary → active
        assert [m.content for m in session.build_messages()] == [
            "s", "u0", "u1", SUMMARY_PREFIX + "\n摘要", "u5",
        ]

        # 固定头只固化一次：再来一批消息也不会重排
        await manager.commit(session, session.append(*[Message.user(f"v{i}") for i in range(6)]))
        await manager.maybe_compact(session)
        assert [m.content for m in session.head] == ["u0", "u1"]

    _run(scenario())


def test_keep_tail_zero_compresses_everything():
    async def scenario():
        stub = _StubCompressor("摘要")
        manager = _manager(
            compressor=stub,
            config=SessionConfig(compact_threshold=2, keep_head=0, keep_tail=0),
        )
        session = await manager.create(session_id="s1")
        await manager.commit(session, session.append(*[Message.user(f"u{i}") for i in range(3)]))
        events = await manager.maybe_compact(session)
        assert [e.type for e in events] == ["compact"]
        assert session.active == []  # 什么都不保护
        assert [m.content for m in stub.calls[0]] == ["u0", "u1", "u2"]

    _run(scenario())


def test_adopt_head_replayed_from_event_log():
    async def scenario():
        manager = _manager(
            config=SessionConfig(compact_threshold=3, keep_head=2, keep_tail=1)
        )
        session = await manager.create(session_id="s1", system=[Message.system("s")])
        await manager.commit(session, session.append(*[Message.user(f"u{i}") for i in range(4)]))
        await manager.maybe_compact(session)

        reopened = await manager.open("s1")
        assert [m.content for m in reopened.head] == ["u0", "u1"]
        assert reopened.build_messages() == session.build_messages()

    _run(scenario())


def test_compaction_disabled():
    async def scenario():
        manager = _manager(
            config=SessionConfig(compact_threshold=1, keep_tail=1, enable_compaction=False),
        )
        session = await manager.create(session_id="s1")
        await manager.commit(session, session.append(*[Message.user(f"u{i}") for i in range(5)]))
        assert await manager.maybe_compact(session) == []
        assert len(session.active) == 5

    _run(scenario())


def test_token_threshold():
    session = Session.create()
    config = SessionConfig(compact_threshold=1000, max_active_tokens=10)
    session.append(Message.user("x" * 200))  # 约 50 token
    assert session.is_over_threshold(config) is True


# ---- M5：压缩时顺带抽取长期记忆 ----
def test_split_summary_and_memories():
    result = split_summary_and_memories(
        "这是摘要正文。\n继续摘要。\n\n记忆：用户要求回复用中文\n记忆：项目用 FastAPI\n记忆：无\n"
    )
    assert result.summary == "这是摘要正文。\n继续摘要。"
    assert result.memories == ["用户要求回复用中文", "项目用 FastAPI"]


def test_llm_compressor_compress_and_extract_uses_one_call():
    provider = FakeProvider([_text("摘要\n记忆：只用中文")])
    result = _run(
        LLMCompressor(provider).compress_and_extract([Message.user("u1"), Message.assistant("a1")])
    )
    assert result.summary == "摘要" and result.memories == ["只用中文"]
    assert len(provider.requests) == 1  # 复用同一次调用，不新增成本
    assert "记忆：" in provider.requests[0].messages[0].content  # prompt 里有抽取指令


def test_manager_extracts_memories_only_with_capable_compressor():
    async def scenario():
        captured: list[list[str]] = []

        class _Capable:
            async def compress(self, messages):
                return "摘要"

            async def compress_and_extract(self, messages):
                return CompressionResult(summary="摘要", memories=["一条候选"])

        class _Plain:
            async def compress(self, messages):
                return "摘要"

        # 支持抽取的压缩器 + 回调 → 回调被调用，摘要照常使用
        manager = SessionManager(
            MemorySessionStore(),
            config=SessionConfig(compact_threshold=3, keep_tail=1),
            compressor=_Capable(),
            on_memory=captured.append,
        )
        session = await manager.create(session_id="s1")
        await manager.commit(session, session.append(*[Message.user(f"u{i}") for i in range(4)]))
        await manager.maybe_compact(session)
        assert captured == [["一条候选"]]
        assert session.summary is not None and "摘要" in session.summary.content

        # 普通压缩器（不支持抽取）→ 回调不会被调用，行为与今天一致
        plain = SessionManager(
            MemorySessionStore(),
            config=SessionConfig(compact_threshold=3, keep_tail=1),
            compressor=_Plain(),
            on_memory=captured.append,
        )
        other = await plain.create(session_id="s2")
        await plain.commit(other, other.append(*[Message.user(f"u{i}") for i in range(4)]))
        await plain.maybe_compact(other)
        assert captured == [["一条候选"]]  # 没变

    _run(scenario())


def test_memory_handler_error_does_not_break_compaction():
    async def scenario():
        class _Capable:
            async def compress(self, messages):
                return "摘要"

            async def compress_and_extract(self, messages):
                return CompressionResult(summary="摘要", memories=["候选"])

        def boom(_: list[str]) -> None:
            raise RuntimeError("记忆落点炸了")

        manager = SessionManager(
            MemorySessionStore(),
            config=SessionConfig(compact_threshold=3, keep_tail=1),
            compressor=_Capable(),
            on_memory=boom,
        )
        session = await manager.create(session_id="s1")
        await manager.commit(session, session.append(*[Message.user(f"u{i}") for i in range(4)]))
        events = await manager.maybe_compact(session)  # 不抛异常
        assert [event.type for event in events] == ["compact"]
        assert "摘要" in session.summary.content

    _run(scenario())


# ---- M4：run_turn 端到端 ----
def test_run_turn_end_to_end():
    async def scenario():
        store = MemorySessionStore()
        manager = _manager(store, config=SessionConfig(compact_threshold=100))
        session = await manager.create(session_id="s1", system=[Message.system("你是助手")])

        agent = _loop(
            [
                _calls(ToolCall(id="c1", name="t.echo", arguments=json.dumps({"text": "hi"}))),
                _text("已回显"),
                _text("继续回答"),
            ]
        )

        result = await manager.run_turn(session, "调用工具", agent)
        assert result.stop_reason == "final" and result.final_text == "已回显"
        # 活跃区：user + assistant(tool_calls) + tool + assistant
        assert [m.role for m in session.active] == ["user", "assistant", "tool", "assistant"]
        assert session.meta.turns == 1

        second = await manager.run_turn(session, "再来一轮", agent)
        assert second.final_text == "继续回答"

        # 重放后与内存态一致
        reopened = await manager.open("s1")
        assert reopened.build_messages() == session.build_messages()
        assert reopened.meta.turns == 2

    _run(scenario())


def test_run_turn_keeps_system_stable_across_turns():
    async def scenario():
        manager = _manager()
        session = await manager.create(system=[Message.system("固定指令")])
        agent = _loop([_text("a"), _text("b")])

        await manager.run_turn(session, "第一轮", agent)
        first_system = session.build_messages()[0]
        await manager.run_turn(session, "第二轮", agent)
        second_system = session.build_messages()[0]
        # 固定区字节级不变（前缀缓存的前提）
        assert first_system is second_system
        assert second_system.content == "固定指令"

    _run(scenario())


def test_run_turn_passes_session_context():
    async def scenario():
        manager = _manager()
        session = await manager.create(session_id="s-ctx")
        agent = _loop([_text("ok")])
        captured = {}

        original = agent.run

        async def spy(messages, ctx=None):
            captured["session_id"] = ctx.session_id if ctx else None
            return await original(messages, ctx)

        agent.run = spy  # type: ignore[method-assign]
        await manager.run_turn(session, "hi", agent)
        assert captured["session_id"] == "s-ctx"

    _run(scenario())


def test_run_turn_with_injected_prompt_builder():
    """即使 AgentLoop 的 PromptBuilder 会注入 system，record 仍只取新增消息。"""
    async def scenario():
        manager = _manager()
        session = await manager.create(system=[Message.system("会话 system")])
        agent = _loop([_text("答")], prompt=PromptBuilder(base_instructions="额外指令"))
        await manager.run_turn(session, "问", agent)
        assert [m.role for m in session.active] == ["user", "assistant"]

    _run(scenario())


def test_session_context_scopes():
    session = Session.create(session_id="s1")
    ctx = session.context(scopes={"fs:read"}, approval=None)
    assert ctx.session_id == "s1" and ctx.scopes == {"fs:read"}
    assert Context(session_id="x").session_id == "x"


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
