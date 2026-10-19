"""LLM 接入层测试（无需网络）。

运行：python -m harness.llm.test.test_llms   （或 pytest）
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from types import SimpleNamespace as NS
from pathlib import Path

from harness.llm import (
    ChatRequest,
    ChatResponse,
    FakeProvider,
    LLMConfig,
    LLMError,
    LLMErrorType,
    Message,
    StreamAccumulator,
    StreamEvent,
    StreamEventType,
    ToolCall,
    Usage,
    map_exception,
    to_chat_response,
    to_stream_event,
)
from harness.tool import Context, Executor, default_registry

DATA_URL = "data:image/png;base64,iVBORw0KGgo="


def _run(coro):
    return asyncio.run(coro)


def _tmp_file(text: str = "hello world") -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    return tmp.name


# ---- M1：契约 ----
def test_message_to_dict_basic():
    assert Message.system("s").to_dict() == {"role": "system", "content": "s"}
    user = Message.user("hi")
    assert user.to_dict() == {"role": "user", "content": "hi"}
    assistant = Message.assistant("ok")
    assert assistant.to_dict() == {"role": "assistant", "content": "ok"}


def test_message_tool_roundtrip_matches_executor_output():
    msg = Message.tool(tool_call_id="call_a", content="done")
    assert msg.to_dict() == {"role": "tool", "tool_call_id": "call_a", "content": "done"}


def test_message_assistant_with_tool_calls():
    call = ToolCall(id="call_a", name="fs.read", arguments='{"path": "a.txt"}')
    data = Message.assistant(tool_calls=[call]).to_dict()
    assert data["role"] == "assistant"
    assert data["content"] is None
    assert data["tool_calls"] == [
        {"id": "call_a", "type": "function", "function": {"name": "fs.read", "arguments": '{"path": "a.txt"}'}}
    ]


def test_message_user_with_images():
    msg = Message.user("看图", images=["https://x/y.png", DATA_URL])
    assert isinstance(msg.content, list)
    assert msg.content[0] == {"type": "text", "text": "看图"}
    assert msg.content[1]["image_url"]["url"] == "https://x/y.png"
    assert msg.content[2]["image_url"]["url"] == DATA_URL


def test_tool_call_from_dict_and_serialization():
    call = ToolCall.from_dict(
        {"id": "c1", "function": {"name": "fs.read", "arguments": '{"path":"x"}'}}
    )
    assert (call.id, call.name, call.arguments) == ("c1", "fs.read", '{"path":"x"}')
    # arguments 为 dict 时序列化为字符串
    call2 = ToolCall.from_dict({"id": "c2", "function": {"name": "t", "arguments": {"a": 1}}})
    assert json.loads(call2.arguments) == {"a": 1}


def test_chat_request_payload():
    req = ChatRequest(
        messages=[Message.user("hi")],
        tools=[{"type": "function", "function": {"name": "fs.read"}}],
        tool_choice="auto",
        temperature=0.2,
        max_tokens=100,
        extra={"top_p": 0.9},
    )
    payload = req.payload("gpt-4o-mini")
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tool_choice"] == "auto"
    assert payload["temperature"] == 0.2 and payload["max_tokens"] == 100
    assert payload["top_p"] == 0.9
    # 未设置的字段不应出现
    assert "stop" not in payload


def test_chat_response_helpers():
    res = ChatResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="fs.read", arguments="{}")],
        usage=Usage(1, 2, 3),
    )
    assert res.has_tool_calls()
    assert res.tool_call_dicts()[0]["function"]["name"] == "fs.read"


def _fake_chunk(*, content=None, tool_calls=None, usage=None, finish_reason=None):
    return NS(
        choices=[NS(delta=NS(content=content, tool_calls=tool_calls), finish_reason=finish_reason)],
        usage=usage,
    )


def test_stream_event_contract():
    assert StreamEventType.TEXT_DELTA == "text_delta"
    assert StreamEventType.TOOL_CALL_DELTA == "tool_call_delta"
    event = StreamEvent(type=StreamEventType.TEXT_DELTA, text="hi")
    assert event.text == "hi" and event.tool_call is None and event.index == 0


def test_stream_accumulator_reassembles_text_and_tool_calls():
    accumulator = StreamAccumulator()
    accumulator.feed(StreamEvent(type=StreamEventType.TEXT_DELTA, text="he"))
    accumulator.feed(StreamEvent(type=StreamEventType.TEXT_DELTA, text="llo"))
    accumulator.feed(
        StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA,
            index=0,
            tool_call=ToolCall(id="call_1", name="fs.read", arguments='{"pa'),
        )
    )
    accumulator.feed(
        StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA,
            index=0,
            tool_call=ToolCall(id="", name="", arguments='th":"a"}'),
        )
    )
    accumulator.feed(
        StreamEvent(type=StreamEventType.DONE, usage=Usage(1, 2, 3), finish_reason="tool_calls")
    )

    response = accumulator.to_response()
    assert response.content == "hello"
    assert response.has_tool_calls()
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].arguments == '{"path":"a"}'
    assert response.usage.total_tokens == 3
    assert response.finish_reason == "tool_calls"


def test_to_stream_event_maps_chunk():
    assert to_stream_event(_fake_chunk(content="x")).type == StreamEventType.TEXT_DELTA

    raw = NS(index=0, id="call_1", function=NS(name="fs.read", arguments="{"))
    event = to_stream_event(_fake_chunk(tool_calls=[raw]))
    assert event.type == StreamEventType.TOOL_CALL_DELTA
    assert event.tool_call.name == "fs.read" and event.index == 0

    done = to_stream_event(
        NS(choices=[], usage=NS(prompt_tokens=1, completion_tokens=2, total_tokens=3))
    )
    assert done.type == StreamEventType.DONE and done.usage.total_tokens == 3

    # 无有效增量的块（如仅 finish_reason 或全空）不产出事件
    assert to_stream_event(_fake_chunk()) is None
    assert to_stream_event(_fake_chunk(finish_reason="stop")).type == StreamEventType.DONE


def test_fake_provider_astream_reassembles():
    async def collect():
        provider = FakeProvider(
            [
                ChatResponse(
                    content="abcdefg",
                    tool_calls=[ToolCall(id="c1", name="t.echo", arguments='{"a":1}')],
                    usage=Usage(1, 2, 3),
                )
            ],
            chunk_size=3,
        )
        accumulator = StreamAccumulator()
        count = 0
        async for event in provider.astream(ChatRequest(messages=[Message.user("hi")])):
            accumulator.feed(event)
            count += 1
        return accumulator.to_response(), count

    response, count = _run(collect())
    assert response.content == "abcdefg"  # 3 段文本 + 1 段工具 + 1 段 done
    assert response.tool_calls[0].id == "c1"
    assert response.usage.total_tokens == 3
    assert count == 5


def test_openai_compat_provider_astream():
    from harness.llm.openai_compat import OpenAICompatProvider

    chunks = [
        _fake_chunk(content="he"),
        _fake_chunk(content="llo", finish_reason="stop"),
        NS(choices=[], usage=NS(prompt_tokens=1, completion_tokens=2, total_tokens=3)),
    ]
    seen: dict = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            seen.update(kwargs)

            async def _gen():
                for chunk in chunks:
                    yield chunk

            return _gen()

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.config = LLMConfig(model="m")
    provider._tracer = None
    provider._client = NS(chat=NS(completions=_FakeCompletions()))

    accumulator = StreamAccumulator()

    async def consume():
        async for event in provider.astream(ChatRequest(messages=[Message.user("hi")])):
            accumulator.feed(event)

    _run(consume())
    assert seen["stream"] is True and seen["stream_options"] == {"include_usage": True}
    assert accumulator.to_response().content == "hello"
    assert accumulator.usage.total_tokens == 3


def test_openai_compat_provider_astream_maps_errors():
    from harness.llm.openai_compat import OpenAICompatProvider

    class _FakeCompletions:
        async def create(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.config = LLMConfig(model="m")
    provider._tracer = None
    provider._client = NS(chat=NS(completions=_FakeCompletions()))

    async def consume():
        async for _ in provider.astream(ChatRequest(messages=[Message.user("hi")])):
            pass

    try:
        _run(consume())
    except LLMError as exc:
        assert exc.type == LLMErrorType.NETWORK and exc.retryable is True
    else:  # pragma: no cover
        raise AssertionError("astream 应把异常映射为 LLMError")


def test_llm_config_env_fallback(monkeypatch=None):
    import os

    config = LLMConfig(model="m", api_key=None)
    os.environ.pop("OPENAI_API_KEY", None)
    assert config.resolved_api_key() is None
    os.environ["OPENAI_API_KEY"] = "env-key"
    try:
        assert config.resolved_api_key() == "env-key"
        assert LLMConfig(api_key="explicit").resolved_api_key() == "explicit"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)


# ---- M1：错误映射 ----
class _FakeAuthError(Exception):
    status_code = 401


class _FakeRateLimitError(Exception):
    status_code = 429


class _FakeServerError(Exception):
    status_code = 503


class _FakeTimeoutError(Exception):
    pass


class _AuthenticationError(Exception):
    pass


class _RateLimitError(Exception):
    pass


class _APITimeoutError(Exception):
    pass


class _APIConnectionError(Exception):
    pass


def test_map_exception_by_class_name():
    assert map_exception(_AuthenticationError()).type == LLMErrorType.AUTH
    assert map_exception(_RateLimitError()).type == LLMErrorType.RATE_LIMIT
    assert map_exception(_APITimeoutError()).type == LLMErrorType.TIMEOUT
    assert map_exception(_APIConnectionError()).type == LLMErrorType.NETWORK


def test_map_exception_by_status_code():
    auth = map_exception(_FakeAuthError())
    assert auth.type == LLMErrorType.AUTH and auth.retryable is False and auth.status == 401

    rate = map_exception(_FakeRateLimitError())
    assert rate.type == LLMErrorType.RATE_LIMIT and rate.retryable is True

    server = map_exception(_FakeServerError())
    assert server.type == LLMErrorType.SERVER and server.retryable is True

    assert map_exception(_FakeTimeoutError()).type == LLMErrorType.TIMEOUT


def test_map_exception_context_length():
    exc = Exception("This model's maximum context length is 8192 tokens")
    exc.status_code = 400  # type: ignore[attr-defined]
    mapped = map_exception(exc)
    assert mapped.type == LLMErrorType.CONTEXT_LENGTH and mapped.retryable is False


def test_llm_error_to_dict():
    data = LLMError(LLMErrorType.RATE_LIMIT, "slow down", retryable=True, status=429).to_dict()
    assert data == {"type": "rate_limit", "message": "slow down", "retryable": True, "status": 429}


# ---- M2：SDK 响应解析 ----
def _fake_completion(content=None, tool_calls=None, finish_reason="stop"):
    calls = [
        NS(id=cid, function=NS(name=name, arguments=args)) for cid, name, args in (tool_calls or [])
    ]
    return NS(
        choices=[NS(message=NS(content=content, tool_calls=calls or None), finish_reason=finish_reason)],
        usage=NS(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        model="gpt-4o-mini",
    )


def test_to_chat_response_text():
    res = to_chat_response(_fake_completion(content="你好"))
    assert res.content == "你好"
    assert res.tool_calls == []
    assert res.usage.total_tokens == 18
    assert res.model == "gpt-4o-mini"
    assert res.finish_reason == "stop"


def test_to_chat_response_tool_calls():
    res = to_chat_response(
        _fake_completion(
            tool_calls=[("call_a", "fs.read", '{"path":"a.txt"}')],
            finish_reason="tool_calls",
        )
    )
    assert res.has_tool_calls() and res.content is None
    call = res.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_a", "fs.read", '{"path":"a.txt"}')
    assert res.finish_reason == "tool_calls"


def test_to_chat_response_without_usage():
    completion = NS(choices=[NS(message=NS(content="x", tool_calls=None), finish_reason="stop")], model="m")
    res = to_chat_response(completion)
    assert res.usage == Usage(0, 0, 0) and res.raw is None


# ---- M2：Provider 调用（用假客户端，不走网络）----
def test_openai_compat_provider_complete():
    from harness.llm.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)  # 绕过 __init__，避免建真实客户端
    provider.config = LLMConfig(model="m")
    provider._tracer = None
    captured = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _fake_completion(content="pong")

    provider._client = NS(chat=NS(completions=_FakeCompletions()))

    request = ChatRequest(messages=[Message.user("ping")], tools=[{"type": "function"}])
    res = _run(provider.complete(request))
    assert res.content == "pong"
    assert captured["model"] == "m"
    assert captured["messages"] == [{"role": "user", "content": "ping"}]
    assert captured["tools"] == [{"type": "function"}]


def test_openai_compat_provider_maps_errors():
    from harness.llm.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.config = LLMConfig(model="m")
    provider._tracer = None

    class _FailingCompletions:
        async def create(self, **kwargs):
            raise _FakeRateLimitError("busy")

    provider._client = NS(chat=NS(completions=_FailingCompletions()))
    try:
        _run(provider.complete(ChatRequest(messages=[Message.user("hi")])))
    except LLMError as exc:
        assert exc.type == LLMErrorType.RATE_LIMIT and exc.retryable is True
    else:
        raise AssertionError("应抛出 LLMError")


def test_openai_compat_provider_records_trace():
    from harness.llm.openai_compat import OpenAICompatProvider

    spans = []

    class _Tracer:
        def record(self, **fields):
            spans.append(fields)

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.config = LLMConfig(model="m")
    provider._tracer = _Tracer()

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _fake_completion(content="ok")

    provider._client = NS(chat=NS(completions=_FakeCompletions()))
    _run(provider.complete(ChatRequest(messages=[Message.user("hi")])))
    assert spans[0]["tool"] == "llm.complete" and spans[0]["ok"] is True
    assert spans[0]["total_tokens"] == 18


# ---- M2：FakeProvider ----
def test_fake_provider_queue_and_records():
    provider = FakeProvider([ChatResponse(content="a"), ChatResponse(content="b")])
    first = _run(provider.complete(ChatRequest(messages=[Message.user("1")])))
    second = _run(provider.complete(ChatRequest(messages=[Message.user("2")])))
    third = _run(provider.complete(ChatRequest(messages=[Message.user("3")])))
    assert (first.content, second.content, third.content) == ("a", "b", "")
    assert len(provider.requests) == 3
    _run(provider.aclose())
    assert provider.closed is True


# ---- 关键：与工具系统的闭环（tool_calls 直喂 Executor）----
def test_tool_calls_feed_executor_and_refill():
    path = _tmp_file("payload")
    response = to_chat_response(
        _fake_completion(
            tool_calls=[("call_a", "fs.read", json.dumps({"path": path}))],
            finish_reason="tool_calls",
        )
    )

    executor = Executor(default_registry)
    results = _run(
        executor.execute_tool_calls(response.tool_call_dicts(), Context(scopes={"fs:read"}))
    )
    assert results[0]["tool_call_id"] == "call_a" and results[0]["content"] == "payload"

    # 回填成 Message，序列化后与协议一致
    refill = Message.tool(tool_call_id=results[0]["tool_call_id"], content=results[0]["content"])
    assert refill.to_dict() == {
        "role": "tool",
        "tool_call_id": "call_a",
        "content": "payload",
    }


def test_tool_schema_export_feeds_request():
    tools = default_registry.to_openai_tools(["fs.read"])
    request = ChatRequest(messages=[Message.user("读文件")], tools=tools)
    payload = request.payload("m")
    assert payload["tools"][0]["function"]["name"] == "fs.read"


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
