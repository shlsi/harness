"""服务层测试（全离线：FakeProvider + 内存会话，无需网络与 API Key）。

运行：python -m server.test.test_api   （或 pytest）
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from harness.agent import LoopConfig, PromptBuilder
from harness.config import JsonlTracerSink, load_config
from harness.llm import ChatResponse, FakeProvider, Message, ToolCall, Usage
from harness.memory import MemoryStore, register_memory_tools
from harness.session import LLMCompressor, MemorySessionStore, SessionManager
from harness.skill import SkillRegistry
from harness.tool import ToolRegistry, ToolSpec, Tracer
from server.app import create_app
from server.state import AppState, build_state


def _run(coro):
    return asyncio.run(coro)


def _text(content: str) -> ChatResponse:
    return ChatResponse(content=content, usage=Usage(10, 5, 15), finish_reason="stop")


def _calls(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(
        content=None, tool_calls=list(calls), usage=Usage(10, 5, 15), finish_reason="tool_calls"
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    def echo(text: str) -> str:
        return f"echo:{text}"

    def danger(target: str = "x") -> str:
        return f"deleted:{target}"

    def write(path: str = "a.txt") -> str:
        return f"written:{path}"

    registry.register(ToolSpec(name="t.echo", description="回显"), echo)
    registry.register(
        ToolSpec(name="t.danger", description="危险操作", side_effect="destructive"), danger
    )
    # 需要写权限域的普通写工具（用于验证「档位 → scopes」的推导）
    registry.register(
        ToolSpec(
            name="t.write", description="写文件", side_effect="write", scopes=("fs:write",)
        ),
        write,
    )
    return registry


def _make_app(
    responses=None,
    *,
    chunk_size: int = 4,
    loop_config: LoopConfig | None = None,
    tracer: Tracer | None = None,
    memory: MemoryStore | None = None,
    skills: SkillRegistry | None = None,
):
    provider = FakeProvider(list(responses or []), chunk_size=chunk_size)
    registry = _registry()
    if memory is not None:
        register_memory_tools(registry, memory)
    state = AppState(
        provider=provider,
        registry=registry,
        manager=SessionManager(MemorySessionStore(), compressor=LLMCompressor(provider)),
        prompt=PromptBuilder(base_instructions="你是测试助手", memory=memory),
        loop_config=loop_config or LoopConfig(stream=True),
        tracer=tracer,
        memory=memory,
        skills=skills,
        heartbeat_s=30.0,
    )
    return create_app(state=state), state


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _new_session(client: httpx.AsyncClient, title: str = "测试") -> str:
    response = await client.post("/sessions", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _wait_for_event(run, name: str, timeout: float = 5.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for frame in run.frames:
            if frame["event"] == name:
                return json.loads(frame["data"])
        await asyncio.sleep(0.01)
    raise AssertionError(f"未等到事件 {name}: {[f['event'] for f in run.frames]}")


# ---- 会话 CRUD ----
def test_session_crud():
    async def scenario():
        app, state = _make_app()
        async with _client(app) as client:
            session_id = await _new_session(client, "解标")

            detail = (await client.get(f"/sessions/{session_id}")).json()
            assert detail["title"] == "解标" and detail["turns"] == 0
            assert detail["active_count"] == 0 and detail["has_summary"] is False

            listed = (await client.get("/sessions")).json()
            assert [item["session_id"] for item in listed] == [session_id]

            deleted = await client.delete(f"/sessions/{session_id}")
            assert deleted.status_code == 204
            missing = await client.get(f"/sessions/{session_id}")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "session_not_found"
        await state.aclose()

    _run(scenario())


def test_create_session_with_system_messages():
    async def scenario():
        app, state = _make_app()
        async with _client(app) as client:
            response = await client.post(
                "/sessions",
                json={"system": [{"role": "system", "content": "你是助手"}]},
            )
            session_id = response.json()["session_id"]
            messages = (await client.get(f"/sessions/{session_id}/messages")).json()["messages"]
            assert messages == [{"role": "system", "content": "你是助手"}]
        await state.aclose()

    _run(scenario())


# ---- 会话详情 / 重命名 / 压缩 ----
def test_session_detail_context_and_rename():
    async def scenario():
        app, state = _make_app(responses=[_text("你好")])
        async with _client(app) as client:
            session_id = await _new_session(client, "原标题")
            await client.post(f"/sessions/{session_id}/turns", json={"input": "hi"})

            detail = (await client.get(f"/sessions/{session_id}")).json()
            assert detail["title"] == "原标题"
            assert detail["turns"] == 1
            assert detail["usage_totals"]["total_tokens"] == 15  # FakeProvider：Usage(10,5,15)
            assert detail["head_count"] == 0
            assert detail["active_run_id"] is None
            ctx = detail["context"]
            assert ctx["system"]["count"] == 0
            assert ctx["active"]["count"] == 2
            assert ctx["conversation_count"] == 2
            assert ctx["message_count"] == 2
            assert detail["compaction"]["over_threshold"] is False
            assert detail["compaction"]["remaining_messages"] == 38  # 40 - 2
            assert detail["budget"] == {
                "max_total_tokens": None,
                "used_total_tokens": 15,
                "remaining": None,
            }

            renamed = await client.patch(
                f"/sessions/{session_id}", json={"title": "新标题"}
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["title"] == "新标题"
            assert (await client.get(f"/sessions/{session_id}")).json()["title"] == "新标题"

            bad = await client.patch(f"/sessions/{session_id}", json={"title": ""})
            assert bad.status_code == 422
            assert bad.json()["error"]["code"] == "invalid_request"

            ghost = await client.patch("/sessions/ghost", json={"title": "x"})
            assert ghost.status_code == 404

            # 人为占用一个 run → 改名被拒（并发保护）
            state.runs.create(session_id)
            conflicted = await client.patch(
                f"/sessions/{session_id}", json={"title": "又改"}
            )
            assert conflicted.status_code == 409
            assert conflicted.json()["error"]["code"] == "run_conflict"
        await state.aclose()

    _run(scenario())


def test_compact_endpoint():
    async def scenario():
        app, state = _make_app(responses=[_text("你好")])
        async with _client(app) as client:
            session_id = await _new_session(client)
            await client.post(f"/sessions/{session_id}/turns", json={"input": "hi"})

            below = (
                await client.post(f"/sessions/{session_id}/compact", json={})
            ).json()
            assert below["compacted"] is False and below["reason"] == "below_threshold"

            # force 但 active(2) <= keep_tail(16) → 无可压
            forced = (
                await client.post(f"/sessions/{session_id}/compact", json={"force": True})
            ).json()
            assert forced["compacted"] is False and forced["reason"] == "nothing_to_drop"
            assert forced["summary_chars"] == 0

            ghost = await client.post("/sessions/ghost/compact", json={"force": True})
            assert ghost.status_code == 404
        await state.aclose()

    _run(scenario())


# ---- 非流式一轮对话 ----
def test_turn_non_stream():
    async def scenario():
        app, state = _make_app(responses=[_text("你好")])
        async with _client(app) as client:
            session_id = await _new_session(client)
            response = await client.post(f"/sessions/{session_id}/turns", json={"input": "hi"})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["final_text"] == "你好"
            assert data["stop_reason"] == "final"
            assert data["usage"]["total_tokens"] == 15
            assert data["steps"] == 1 and data["error"] is None

            messages = (await client.get(f"/sessions/{session_id}/messages")).json()["messages"]
            # 会话自身只存「系统提示之外」的分区；PromptBuilder 注入的 system 不落库
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[1]["content"] == "你好"
            # 但发给模型的请求里带了注入的 system
            sent = state.provider.requests[0].messages
            assert [m.role for m in sent] == ["system", "user"]
        await state.aclose()

    _run(scenario())


def test_turn_with_images_uses_multimodal_parts():
    async def scenario():
        app, state = _make_app(responses=[_text("看到图了")])
        async with _client(app) as client:
            session_id = await _new_session(client)
            await client.post(
                f"/sessions/{session_id}/turns",
                json={"input": "这是什么", "images": ["https://x/y.png"]},
            )
            messages = (await client.get(f"/sessions/{session_id}/messages")).json()["messages"]
            user = next(m for m in messages if m["role"] == "user")
            assert user["content"][0] == {"type": "text", "text": "这是什么"}
            assert user["content"][1]["image_url"]["url"] == "https://x/y.png"
            # 模型也收到了多模态 parts
            assert state.provider.requests[0].messages[1].content[0]["type"] == "text"
        await state.aclose()

    _run(scenario())


# ---- 流式 SSE ----
def test_turn_stream_sse_events():
    async def scenario():
        app, state = _make_app(responses=[_text("abcdef")], chunk_size=2)
        async with _client(app) as client:
            session_id = await _new_session(client)
            response = await client.post(
                f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
            )
            assert response.status_code == 202, response.text
            handle = response.json()
            assert handle["events_url"] == f"/runs/{handle['run_id']}/events"

            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            stream = await client.get(handle["events_url"])
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            body = stream.text
            for name in ("run_started", "text_delta", "message", "done"):
                assert f"event: {name}" in body
            # 顺序：run_started → text_delta → message → done
            order = [body.index(f"event: {n}") for n in ("run_started", "text_delta", "message", "done")]
            assert order == sorted(order)
            assert '"final_text": "abcdef"' in body
            # 文本增量拼起来等于完整回复
            deltas = [
                json.loads(frame["data"])["text"]
                for frame in run.frames
                if frame["event"] == "text_delta"
            ]
            assert "".join(deltas) == "abcdef"
        await state.aclose()

    _run(scenario())


def test_sse_replay_with_after_param():
    async def scenario():
        app, state = _make_app(responses=[_text("abcd")], chunk_size=2)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            # 只取第 1 条之后的事件（模拟断线续传）
            resumed = await client.get(handle["events_url"], params={"after": 1})
            assert "event: run_started" not in resumed.text
            assert "event: done" in resumed.text
        await state.aclose()

    _run(scenario())


def test_run_status_snapshot():
    async def scenario():
        app, state = _make_app(responses=[_text("ok")])
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            snapshot = (await client.get(f"/runs/{handle['run_id']}")).json()
            assert snapshot["status"] == "finished"
            assert snapshot["summary"]["final_text"] == "ok"
            assert snapshot["events"] > 0

            missing = await client.get("/runs/nope")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "run_not_found"
        await state.aclose()

    _run(scenario())


# ---- 长期记忆 ----
def test_memory_reaches_system_prompt_and_tool_list():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(str(Path(tmp) / "MEMORY.md"))
            store.save("回复必须用中文", section="用户偏好")
            app, state = _make_app(responses=[_text("ok")], memory=store)
            async with _client(app) as client:
                session_id = await _new_session(client)
                handle = (
                    await client.post(
                        f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                    )
                ).json()
                run = state.runs.get(handle["run_id"])
                await asyncio.wait_for(run.done.wait(), timeout=5)

                # 记忆进了发给模型的 system
                system = next(
                    m for m in state.provider.requests[-1].messages if m.role == "system"
                )
                assert "## 长期记忆" in system.content
                assert "回复必须用中文" in system.content

                # 记忆工具出现在自省端点里
                names = [item["name"] for item in (await client.get("/tools")).json()]
                assert "memory.save" in names and "memory.forget" in names
            await state.aclose()

    _run(scenario())


def test_memory_tool_writes_file_through_service():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MEMORY.md"
            store = MemoryStore(str(path))
            responses = [
                _calls(
                    ToolCall(
                        id="c1",
                        name="memory.save",
                        arguments='{"text":"用户不用向量库","section":"用户偏好"}',
                    )
                ),
                _text("记住了"),
            ]
            app, state = _make_app(responses=responses, memory=store)
            async with _client(app) as client:
                session_id = await _new_session(client)
                handle = (
                    await client.post(
                        f"/sessions/{session_id}/turns",
                        json={
                            "input": "记一下：我不用向量库",
                            "stream": True,
                            "scopes": ["memory:write"],
                        },
                    )
                ).json()
                run = state.runs.get(handle["run_id"])
                await asyncio.wait_for(run.done.wait(), timeout=5)

                assert store.entries() == ["用户不用向量库"]
                assert "用户偏好" in path.read_text("utf-8")
            await state.aclose()

    _run(scenario())


# ---- 观测：run 级 span 聚合 ----
def test_run_trace_endpoint_returns_run_span():
    async def scenario():
        tracer = Tracer()
        app, state = _make_app(responses=[_text("完成")], tracer=tracer)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            assert run.trace_id  # 由 Context.trace_id 赋值
            done = json.loads(next(f for f in run.frames if f["event"] == "done")["data"])
            assert done["trace_id"] == run.trace_id
            started = json.loads(next(f for f in run.frames if f["event"] == "run_started")["data"])
            assert started["trace_id"] == run.trace_id

            trace = (await client.get(f"/runs/{handle['run_id']}/trace")).json()
            assert trace["trace_id"] == run.trace_id
            assert [span["tool"] for span in trace["spans"]] == ["agent.run"]
            assert trace["tree"][0]["span"]["tool"] == "agent.run"
            assert trace["summary"]["span_count"] == 1
            assert trace["summary"]["stop_reason"] == "final"

            missing = await client.get("/runs/nope/trace")
            assert missing.status_code == 404
        await state.aclose()

    _run(scenario())


def test_run_trace_includes_tool_spans():
    async def scenario():
        tracer = Tracer()
        responses = [
            _calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"hi"}')),
            _text("ok"),
        ]
        app, state = _make_app(responses=responses, tracer=tracer)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            trace = (await client.get(f"/runs/{handle['run_id']}/trace")).json()
            assert {span["tool"] for span in trace["spans"]} == {"agent.run", "t.echo"}
            assert all(span["trace_id"] == run.trace_id for span in trace["spans"])
        await state.aclose()

    _run(scenario())


def test_run_trace_empty_when_no_tracer():
    async def scenario():
        app, state = _make_app(responses=[_text("ok")])
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            trace = (await client.get(f"/runs/{handle['run_id']}/trace")).json()
            assert trace["spans"] == [] and trace["tree"] == []
            assert trace["summary"]["span_count"] == 0  # 缺数据不报错
        await state.aclose()

    _run(scenario())


def test_run_trace_file_source_returns_full_log():
    """内存视图被 max_spans 截断时，source=file 仍能给全量。"""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "traces.jsonl")
            tracer = Tracer(JsonlTracerSink(path), max_spans=1)  # 内存只留最后一条
            responses = [
                _calls(ToolCall(id="c1", name="t.echo", arguments='{"text":"hi"}')),
                _text("ok"),
            ]
            app, state = _make_app(responses=responses, tracer=tracer)
            state.tracer_path = path
            async with _client(app) as client:
                session_id = await _new_session(client)
                handle = (
                    await client.post(
                        f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                    )
                ).json()
                run = state.runs.get(handle["run_id"])
                await asyncio.wait_for(run.done.wait(), timeout=5)
                run_id = handle["run_id"]

                memory = (await client.get(f"/runs/{run_id}/trace")).json()
                assert memory["summary"]["span_count"] == 1  # 只剩 agent.run

                full = (await client.get(f"/runs/{run_id}/trace?source=file")).json()
                assert {span["tool"] for span in full["spans"]} == {"agent.run", "t.echo"}
                assert full["summary"]["span_count"] == 2
                assert full["tree"][0]["span"]["tool"] == "agent.run"

                bad = await client.get(f"/runs/{run_id}/trace?source=nope")
                assert bad.status_code == 400
                assert bad.json()["error"]["code"] == "invalid_source"
            await state.aclose()

    _run(scenario())


def test_run_trace_file_source_without_jsonl_sink_is_rejected():
    async def scenario():
        app, state = _make_app(responses=[_text("ok")])  # 无 tracer、无 tracer_path
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            response = await client.get(f"/runs/{handle['run_id']}/trace?source=file")
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "trace_file_not_configured"
        await state.aclose()

    _run(scenario())


# ---- 审批通道 ----
def test_approval_roundtrip():
    async def scenario():
        responses = [_calls(ToolCall(id="c1", name="t.danger", arguments="{}")), _text("已执行")]
        app, state = _make_app(responses=responses)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "删库", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])

            pending = await _wait_for_event(run, "interaction_required")
            assert pending["kind"] == "approval"
            assert pending["tool"] == "t.danger"
            # 审批只看模型可见参数（默认值已按入参模型填充）
            assert pending["args"] == {"target": "x"}

            # 回填审批 → run 继续跑完
            resolved = await client.post(f"/interactions/{pending['id']}", json={"approved": True})
            assert resolved.status_code == 200 and resolved.json()["ok"] is True

            await asyncio.wait_for(run.done.wait(), timeout=5)
            assert run.status == "finished"
            assert run.summary["final_text"] == "已执行"

            # 一次性消费：重复回填 404
            again = await client.post(f"/interactions/{pending['id']}", json={"approved": True})
            assert again.status_code == 404
            assert again.json()["error"]["code"] == "interaction_not_found"
        await state.aclose()

    _run(scenario())


def test_approval_denied_returns_structured_error_to_model():
    async def scenario():
        responses = [_calls(ToolCall(id="c1", name="t.danger", arguments="{}")), _text("好的")]
        app, state = _make_app(responses=responses)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "删库", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            pending = await _wait_for_event(run, "interaction_required")

            await client.post(f"/interactions/{pending['id']}", json={"approved": False})
            await asyncio.wait_for(run.done.wait(), timeout=5)

            # 拒绝不是异常：工具结果以结构化错误回喂模型，run 正常结束
            tool_frame = next(f for f in run.frames if f["event"] == "tool_finished")
            content = json.loads(tool_frame["data"])["tool_results"][0]["content"]
            assert json.loads(content)["error"]["type"] == "permission_denied"
            assert run.status == "finished" and run.summary["final_text"] == "好的"
        await state.aclose()

    _run(scenario())


def test_cancel_run():
    async def scenario():
        app, state = _make_app(responses=[_calls(ToolCall(id="c1", name="t.danger", arguments="{}"))])
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "删库", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await _wait_for_event(run, "interaction_required")

            response = await client.post(f"/runs/{run.id}/cancel")
            assert response.status_code == 200 and response.json()["status"] == "cancelling"

            task = run.task
            assert task is not None
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.CancelledError:
                pass
            assert run.status == "cancelled"
            # 终止事件已补发
            assert run.frames[-1]["event"] == "done"
            assert json.loads(run.frames[-1]["data"])["stop_reason"] == "cancelled"
        await state.aclose()

    _run(scenario())


# ---- 自省与错误体 ----
def test_meta_endpoints():
    async def scenario():
        app, state = _make_app()
        async with _client(app) as client:
            health = (await client.get("/healthz")).json()
            assert health["status"] == "ok" and health["tools"] == 3 and health["skills"] == 0
            assert health["config_path"] is None  # 注入 state：无配置上下文
            assert health["mcp"] == {"connected": 0, "total": 0}
            assert health["active_runs"] == 0
            assert health["version"] and health["uptime_s"] >= 0

            # /tools 返回「全部内置工具 + 已注册扩展工具」（含被禁用的），带 enabled 标记
            tools = (await client.get("/tools")).json()
            names = {tool["name"] for tool in tools}
            assert {"t.echo", "t.danger"} <= names
            assert "fs.read" in names  # 内置工具也在列表里（供前端启停）
            danger = next(tool for tool in tools if tool["name"] == "t.danger")
            assert danger["side_effect"] == "destructive"
            assert danger["enabled"] is True
            assert "input_schema" in danger and "timeout_s" in danger

            assert (await client.get("/skills")).json() == []

            # 技能未启用：重载应被拒绝（skills_disabled）
            reloaded = await client.post("/skills/reload")
            assert reloaded.status_code == 400
            assert reloaded.json()["error"]["code"] == "skills_disabled"
        await state.aclose()

    _run(scenario())


def test_tools_enabled_flag_reflects_config():
    async def scenario():
        app, state, _ = _config_app()
        async with _client(app) as client:
            tools = {t["name"]: t for t in (await client.get("/tools")).json()}
            assert tools["fs.read"]["enabled"] is True
        await state.aclose()

    _run(scenario())


def test_memory_and_mcp_endpoints():
    async def scenario():
        memory = MemoryStore(Path(tempfile.mkdtemp()) / "MEMORY.md", max_chars=4000)
        app, state = _make_app(memory=memory)
        async with _client(app) as client:
            body = (await client.get("/memory")).json()
            assert body["enabled"] is True and body["content"] == "" and body["entries"] == []

            written = await client.put(
                "/memory", json={"content": "# 记忆\n\n## 用户偏好\n\n- 回复用中文\n"}
            )
            assert written.status_code == 200, written.text
            assert written.json()["entries"] == ["回复用中文"]
            assert memory.path.is_file()

            # 超限 → 400 memory_full
            too_big = await client.put("/memory", json={"content": "- " + "x" * 5000})
            assert too_big.status_code == 400
            assert too_big.json()["error"]["code"] == "memory_full"

            # 没配 MCP → 空列表
            assert (await client.get("/mcp")).json() == {"servers": [], "loaded": False}
        await state.aclose()

    _run(scenario())


def test_memory_disabled_and_llm_ping():
    async def scenario():
        app, state = _make_app(responses=[_text("pong")])  # 未注入 memory
        async with _client(app) as client:
            body = (await client.get("/memory")).json()
            assert body["enabled"] is False
            rejected = await client.put("/memory", json={"content": "x"})
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "memory_disabled"

            ping = (await client.post("/llm/ping")).json()
            assert ping["ok"] is True and ping["reply"] == "pong"
            assert ping["latency_ms"] >= 0
        await state.aclose()

    _run(scenario())


def test_runs_list_running_only():
    async def scenario():
        app, state = _make_app()
        async with _client(app) as client:
            assert (await client.get("/runs")).json() == []

            run = state.runs.create("s-busy")
            listed = (await client.get("/runs")).json()
            assert [item["run_id"] for item in listed] == [run.id]
            assert listed[0]["status"] == "running"

            bad = await client.get("/runs", params={"status": "finished"})
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "invalid_status"
        await state.aclose()

    _run(scenario())


def test_auto_title_from_first_turn():
    async def scenario():
        # FakeProvider 依次返回：① 首轮正文 ② 标题生成
        from harness.session import TitleGenerator

        app, state = _make_app(responses=[_text("你好"), _text("打招呼"), _text("再聊")])
        state.title_namer = TitleGenerator(state.provider)  # 注入 state 默认不自动生成
        async with _client(app) as client:
            created = await client.post("/sessions", json={})  # 无标题
            session_id = created.json()["session_id"]
            await client.post(f"/sessions/{session_id}/turns", json={"input": "你好"})
            # 标题任务在 done 之后异步跑，稍等
            for _ in range(50):
                detail = (await client.get(f"/sessions/{session_id}")).json()
                if detail["title"]:
                    break
                await asyncio.sleep(0.02)
            assert detail["title"] == "打招呼"

            # 第二轮不再重新生成标题（已有标题 / turns != 1）
            await client.post(f"/sessions/{session_id}/turns", json={"input": "再聊"})
            for _ in range(20):
                await asyncio.sleep(0.02)
            assert (await client.get(f"/sessions/{session_id}")).json()["title"] == "打招呼"
        await state.aclose()

    _run(scenario())


def test_auto_title_falls_back_when_llm_unavailable():
    async def scenario():
        from harness.session import TitleGenerator

        # 队列只够正文：标题调用拿到空内容 → 回退为截断首条用户消息
        app, state = _make_app(responses=[_text("你好")])
        state.title_namer = TitleGenerator(state.provider)
        async with _client(app) as client:
            created = await client.post("/sessions", json={})
            session_id = created.json()["session_id"]
            await client.post(f"/sessions/{session_id}/turns", json={"input": "帮我写个函数"})
            for _ in range(50):
                detail = (await client.get(f"/sessions/{session_id}")).json()
                if detail["title"]:
                    break
                await asyncio.sleep(0.02)
            assert detail["title"] == "帮我写个函数"
        await state.aclose()

    _run(scenario())


def test_skill_crud_lint_and_delete():
    async def scenario():
        root = Path(tempfile.mkdtemp())
        app, state = _make_app(skills=SkillRegistry(root))
        async with _client(app) as client:
            assert (await client.get("/skills")).json() == []

            created = await client.post(
                "/skills",
                json={
                    "name": "alpha",
                    "description": "用于测试的技能说明文本",
                    "body": "## 步骤\n\n1. 做事\n",
                    "allowed_tools": ["fs.read"],
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["skill"]["name"] == "alpha"
            assert (root / "alpha" / "SKILL.md").is_file()

            # 重名 → 400
            dup = await client.post(
                "/skills", json={"name": "alpha", "description": "另一个描述文本内容"}
            )
            assert dup.status_code == 400
            assert dup.json()["error"]["code"] == "skill_invalid"

            # 详情（含未截断正文）
            got = (await client.get("/skills/alpha")).json()
            assert got["description"] == "用于测试的技能说明文本"
            assert "步骤" in got["body"]
            assert got["allowed_tools"] == ["fs.read"]

            # 列表与整体 lint
            assert [s["name"] for s in (await client.get("/skills")).json()] == ["alpha"]
            lint = (await client.get("/skills/lint")).json()
            assert lint["enabled"] is True and isinstance(lint["issues"], list)

            # 草稿校验（不落盘、不新建目录）
            draft = (
                await client.post(
                    "/skills/validate",
                    json={"name": "beta", "description": "太短", "body": "正文"},
                )
            ).json()
            assert any(i["code"] == "description-too-short" for i in draft["issues"])
            assert not (root / "beta").exists()

            # 更新（覆盖前备份）
            updated = await client.put(
                "/skills/alpha",
                json={"description": "改过的描述文本（足够长）", "body": "新正文", "allowed_tools": []},
            )
            assert updated.status_code == 200, updated.text
            assert (await client.get("/skills/alpha")).json()["body"].strip() == "新正文"
            assert (root / "alpha" / "SKILL.md.bak").is_file()

            # 非法名（大写/下划线、路径穿越）→ 400，且不在 root 外落文件
            for bad_name in ("Bad_Name", "../evil"):
                bad = await client.post(
                    "/skills", json={"name": bad_name, "description": "描述文本足够长了"}
                )
                assert bad.status_code == 400, bad_name
                assert bad.json()["error"]["code"] in ("skill_invalid", "invalid_request")
            assert not (root.parent / "evil").exists()

            # 删除
            assert (await client.delete("/skills/alpha")).status_code == 204
            assert not (root / "alpha").exists()
            assert (await client.get("/skills/alpha")).status_code == 404
            assert (await client.delete("/skills/alpha")).status_code == 404
        await state.aclose()

    _run(scenario())


def test_skill_endpoints_require_enabled_skills():
    async def scenario():
        app, state = _make_app()  # 未启用技能
        async with _client(app) as client:
            assert (await client.get("/skills")).json() == []
            assert (await client.get("/skills/lint")).json() == {"issues": [], "enabled": False}

            created = await client.post(
                "/skills", json={"name": "alpha", "description": "描述文本足够长了"}
            )
            assert created.status_code == 400
            assert created.json()["error"]["code"] == "skills_disabled"
        await state.aclose()

    _run(scenario())


def test_mcp_test_and_reload_endpoints():
    async def scenario():
        app, state = _make_app()
        async with _client(app) as client:
            # 缺 name → 422 invalid_server
            invalid = await client.post("/mcp/test", json={"server": {}})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_server"

            # 命令不存在 → ok:false（连通性失败是结果，不是服务错误）
            failed = await client.post(
                "/mcp/test",
                json={"server": {"name": "ghost", "command": "definitely-not-a-real-cmd-xyz"}},
            )
            assert failed.status_code == 200
            assert failed.json()["ok"] is False and failed.json()["tools"] == []

            # 重连（无声明）→ 正常返回且带 warnings
            reloaded = await client.post("/mcp/reload")
            assert reloaded.status_code == 200
            assert reloaded.json()["servers"] == [] and "warnings" in reloaded.json()
        await state.aclose()

    _run(scenario())


def test_skills_reload_endpoint():
    """技能启用时，POST /skills/reload 重新扫描目录并返回最新清单。"""

    async def scenario():
        root = Path(tempfile.mkdtemp())
        (root / "alpha").mkdir()
        (root / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: 用于测试重载的技能说明\n---\n\n正文\n",
            encoding="utf-8",
        )
        app, state = _make_app(skills=SkillRegistry(root))
        async with _client(app) as client:
            assert [s["name"] for s in (await client.get("/skills")).json()] == ["alpha"]

            # 新增一个技能目录后重载 → 生效，且不需要换会话
            (root / "beta").mkdir()
            (root / "beta" / "SKILL.md").write_text(
                "---\nname: beta\ndescription: 重载后新增的技能说明\n---\n\n正文\n",
                encoding="utf-8",
            )
            body = (await client.post("/skills/reload")).json()
            assert [s["name"] for s in body["skills"]] == ["alpha", "beta"]
            assert body["warnings"] == []
        await state.aclose()

    _run(scenario())


def test_error_body_shapes():
    async def scenario():
        app, state = _make_app()
        async with _client(app) as client:
            # 会话不存在
            missing = await client.get("/sessions/ghost/messages")
            assert missing.status_code == 404
            body = missing.json()
            assert set(body) == {"error"}
            assert set(body["error"]) == {"code", "message", "details"}

            # 参数校验失败
            session_id = await _new_session(client)
            invalid = await client.post(f"/sessions/{session_id}/turns", json={})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_request"

            # 交互回填缺字段
            bad = await client.post("/interactions/whatever", json={})
            assert bad.status_code == 422
            assert bad.json()["error"]["code"] == "invalid_request"
        await state.aclose()

    _run(scenario())


def test_llm_error_is_reported_in_result_not_http_error():
    async def scenario():
        from harness.llm import LLMError, LLMErrorType

        app, state = _make_app()

        async def boom(request):
            raise LLMError(LLMErrorType.RATE_LIMIT, "限流了", retryable=True)

        async def boom_stream(request):
            raise LLMError(LLMErrorType.RATE_LIMIT, "限流了", retryable=True)
            yield  # pragma: no cover - 使其成为 async generator

        # LoopConfig.stream=True，两条路径都要覆盖
        state.provider.complete = boom
        state.provider.astream = boom_stream
        async with _client(app) as client:
            session_id = await _new_session(client)
            response = await client.post(f"/sessions/{session_id}/turns", json={"input": "hi"})
            # 模型失败仍是 200，错误在 result.error 里（与 harness 契约一致）
            assert response.status_code == 200
            data = response.json()
            assert data["stop_reason"] == "error"
            assert data["error"]["type"] == "rate_limit" and data["error"]["retryable"] is True
        await state.aclose()

    _run(scenario())


def test_loop_protection_surfaces_loop_detected_over_sse():
    async def scenario():
        # 模型反复用完全相同的参数调同一工具 → 循环保护在第 2 次终止
        responses = [
            _calls(ToolCall(id=f"c{index}", name="t.echo", arguments='{"text":"same"}'))
            for index in range(4)
        ]
        app, state = _make_app(
            responses=responses,
            loop_config=LoopConfig(
                stream=True, max_steps=8, repeat_warn_at=1, repeat_stop_at=2
            ),
        )
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = (
                await client.post(
                    f"/sessions/{session_id}/turns", json={"input": "hi", "stream": True}
                )
            ).json()
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            assert run.status == "finished"
            assert run.summary["stop_reason"] == "loop_detected"
            done = json.loads(next(f for f in run.frames if f["event"] == "done")["data"])
            assert done["stop_reason"] == "loop_detected"

            # 提醒随工具结果一起推给前端
            tool_frame = next(f for f in run.frames if f["event"] == "tool_finished")
            content = json.loads(tool_frame["data"])["tool_results"][0]["content"]
            assert "循环保护" in content

            # 会话记录仍合法：assistant.tool_calls 都有对应的 tool 回复
            messages = (await client.get(f"/sessions/{session_id}/messages")).json()["messages"]
            assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "tool"]
        await state.aclose()

    _run(scenario())


# ---- 配置中心 ----
_CONFIG_YAML = (
    "llm:\n"
    "  model: m1\n"
    "  base_url: https://example.invalid/v1\n"
    "  api_key: ${HARNESS_TEST_KEY}\n"
    "agent:\n"
    "  max_steps: 5\n"
    "skills:\n"
    '  root: ""\n'
    "observability:\n"
    "  tracer_sink: none\n"
)


def _config_app():
    """造一个持有真实配置上下文的 app（临时 YAML + 临时 data_dir）。"""
    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "harness.yaml"
    cfg_path.write_text(_CONFIG_YAML, encoding="utf-8")
    env = {"HARNESS_TEST_KEY": "sk-test-secret"}
    config = load_config(cfg_path, env=env)
    config.server.data_dir = str(tmp / "sessions")
    state = build_state(config)
    state.config_path = str(cfg_path)
    return create_app(state=state), state, cfg_path


def test_config_get_masks_secret():
    import os

    os.environ["HARNESS_TEST_KEY"] = "sk-test-secret"
    try:

        async def scenario():
            app, state, _ = _config_app()
            async with _client(app) as client:
                response = await client.get("/config")
                assert response.status_code == 200
                body = response.json()
                assert body["editable"] is True
                # 编辑对象保留 ${VAR} 引用
                assert body["config"]["llm"]["api_key"] == "${HARNESS_TEST_KEY}"
                # 运行时生效值脱敏
                assert body["effective"]["llm"]["api_key"] == "***"
                assert body["sections"][:3] == ["llm", "agent", "session"]
                assert body["backup_path"].endswith("harness.yaml.bak")
                # 响应体里绝不出现真实密钥
                assert "sk-test-secret" not in response.text
            await state.aclose()

        _run(scenario())
    finally:
        os.environ.pop("HARNESS_TEST_KEY", None)


def test_config_put_writes_and_rebuilds():
    import os

    os.environ["HARNESS_TEST_KEY"] = "sk-test-secret"
    try:

        async def scenario():
            app, state, cfg_path = _config_app()
            async with _client(app) as client:
                # 提交占位 api_key（表示不改）+ 改 max_steps
                payload = {
                    "config": {
                        "llm": {
                            "model": "m2",
                            "base_url": "https://example.invalid/v1",
                            "api_key": "***",
                        },
                        "agent": {"max_steps": 9},
                    },
                    "reload": True,
                }
                response = await client.put("/config", json=payload)
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["effective"]["agent"]["max_steps"] == 9
                assert body["effective"]["llm"]["model"] == "m2"
                # 密钥仍是原引用（未被 *** 覆盖）
                assert body["config"]["llm"]["api_key"] == "${HARNESS_TEST_KEY}"
                assert "sk-test-secret" not in response.text

                # 文件已落盘并可被重新读回
                reloaded = load_config(cfg_path, env={"HARNESS_TEST_KEY": "sk-test-secret"})
                assert reloaded.agent.max_steps == 9 and reloaded.llm.model == "m2"
                assert "max_steps: 9" in cfg_path.read_text(encoding="utf-8")

                # 运行时真的换了：/tools 数量可能变，改用 /healthz 确认服务仍健康
                assert (await client.get("/healthz")).json()["status"] == "ok"
            await state.aclose()

        _run(scenario())
    finally:
        os.environ.pop("HARNESS_TEST_KEY", None)


def test_config_put_rejects_plaintext_and_bad_type():
    import os

    os.environ["HARNESS_TEST_KEY"] = "sk-test-secret"
    try:

        async def scenario():
            app, state, _ = _config_app()
            async with _client(app) as client:
                plaintext = await client.put(
                    "/config",
                    json={"config": {"llm": {"api_key": "sk-live-secret"}}},
                )
                assert plaintext.status_code == 422
                assert plaintext.json()["error"]["code"] == "config_invalid"
                assert plaintext.json()["error"]["details"]["path"] == "llm.api_key"

                bad = await client.put(
                    "/config", json={"config": {"agent": {"max_steps": "abc"}}}
                )
                assert bad.status_code == 422
                assert bad.json()["error"]["details"]["path"] == "agent.max_steps"
            await state.aclose()

        _run(scenario())
    finally:
        os.environ.pop("HARNESS_TEST_KEY", None)


def test_config_reload_and_conflict():
    import os

    os.environ["HARNESS_TEST_KEY"] = "sk-test-secret"
    try:

        async def scenario():
            app, state, cfg_path = _config_app()
            async with _client(app) as client:
                # 手改文件 → reload 生效
                cfg_path.write_text(
                    _CONFIG_YAML.replace("max_steps: 5", "max_steps: 11"), encoding="utf-8"
                )
                response = await client.post("/config/reload")
                assert response.status_code == 200
                assert response.json()["effective"]["agent"]["max_steps"] == 11

                # 有 run 在跑 → 拒绝写配置
                state.runs.create("busy-session")
                blocked = await client.put(
                    "/config", json={"config": {"agent": {"max_steps": 3}}}
                )
                assert blocked.status_code == 409
                assert blocked.json()["error"]["code"] == "config_reload_conflict"
            await state.aclose()

        _run(scenario())
    finally:
        os.environ.pop("HARNESS_TEST_KEY", None)


def test_config_unavailable_on_injected_state():
    async def scenario():
        app, state = _make_app()  # 注入 state：无 config 上下文
        async with _client(app) as client:
            body = (await client.get("/config")).json()
            assert body["editable"] is False and body["config"] is None
            put = await client.put("/config", json={"config": {"agent": {"max_steps": 3}}})
            assert put.status_code == 409
            assert put.json()["error"]["code"] == "config_unavailable"
        await state.aclose()

    _run(scenario())


# ---- 会话工作区 + 三档权限 ----
async def _stream_turn(client, session_id: str, text: str = "hi", **extra) -> dict:
    body = {"input": text, "stream": True, **extra}
    response = await client.post(f"/sessions/{session_id}/turns", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def _tool_error(run) -> dict:
    """取本次 run 第一个工具结果里的结构化错误（工具成功时返回空 dict）。"""
    frame = next(f for f in run.frames if f["event"] == "tool_finished")
    content = json.loads(frame["data"])["tool_results"][0]["content"]
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return {}
    return (parsed.get("error") or {}) if isinstance(parsed, dict) else {}


def test_patch_session_metadata_and_validation():
    async def scenario():
        app, state = _make_app()
        workspace = tempfile.mkdtemp()
        async with _client(app) as client:
            session_id = await _new_session(client)

            ok = await client.patch(
                f"/sessions/{session_id}",
                json={"metadata": {"workspace": workspace, "permission": "write", "note": "x"}},
            )
            assert ok.status_code == 200, ok.text
            meta = ok.json()["metadata"]
            assert meta["workspace"] == str(Path(workspace).resolve())  # 存规范化绝对路径
            assert meta["permission"] == "write" and meta["note"] == "x"

            # 合并语义：只改 permission 不会抹掉 workspace / 其他键
            merged = await client.patch(
                f"/sessions/{session_id}", json={"metadata": {"permission": "read"}}
            )
            assert merged.json()["metadata"]["permission"] == "read"
            assert merged.json()["metadata"]["workspace"] == meta["workspace"]
            assert merged.json()["metadata"]["note"] == "x"

            # 非法工作区 / 非法档位 / 空请求体
            bad_ws = await client.patch(
                f"/sessions/{session_id}",
                json={"metadata": {"workspace": str(Path(workspace) / "不存在")}},
            )
            assert bad_ws.status_code == 422
            assert bad_ws.json()["error"]["code"] == "invalid_workspace"
            bad_level = await client.patch(
                f"/sessions/{session_id}", json={"metadata": {"permission": "yolo"}}
            )
            assert bad_level.status_code == 422
            assert bad_level.json()["error"]["code"] == "invalid_permission"
            empty = await client.patch(f"/sessions/{session_id}", json={})
            assert empty.status_code == 422
            assert empty.json()["error"]["code"] == "invalid_request"

            # 创建时就带着设置也走同一套校验
            created = await client.post(
                "/sessions",
                json={"metadata": {"permission": "read", "workspace": workspace}},
            )
            assert created.status_code == 201, created.text
            assert created.json()["metadata"]["permission"] == "read" and created.json()["metadata"]["workspace"]
            rejected = await client.post("/sessions", json={"metadata": {"permission": "yolo"}})
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "invalid_permission"

            # run 活跃时拒绝改设置
            state.runs.create(session_id)
            conflicted = await client.patch(
                f"/sessions/{session_id}", json={"metadata": {"permission": "read"}}
            )
            assert conflicted.status_code == 409
            assert conflicted.json()["error"]["code"] == "run_conflict"
        await state.aclose()

    _run(scenario())


def test_permission_read_blocks_write_tool_and_overrides_body_scopes():
    async def scenario():
        responses = [
            _calls(ToolCall(id="c1", name="t.write", arguments='{"path": "a.txt"}')),
            _text("好"),
        ]
        app, state = _make_app(responses=responses)
        async with _client(app) as client:
            session_id = await _new_session(client)
            await client.patch(
                f"/sessions/{session_id}", json={"metadata": {"permission": "read"}}
            )
            # 即便请求体里硬塞 fs:write，档位优先（单一真源）
            handle = await _stream_turn(client, session_id, "写文件", scopes=["fs:write", "fs:read"])
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)
            assert run.status == "finished"
            assert _tool_error(run)["type"] == "permission_denied"
        await state.aclose()

    _run(scenario())


def test_permission_write_allows_write_but_keeps_destructive_approval():
    async def scenario():
        responses = [
            _calls(ToolCall(id="c1", name="t.write", arguments='{"path": "a.txt"}')),
            _text("已写"),
        ]
        app, state = _make_app(responses=responses)
        async with _client(app) as client:
            session_id = await _new_session(client)
            await client.patch(
                f"/sessions/{session_id}", json={"metadata": {"permission": "write"}}
            )
            handle = await _stream_turn(client, session_id, "写文件")
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)
            assert run.status == "finished"
            # 普通写（side_effect=write）不弹审批
            assert not any(f["event"] == "interaction_required" for f in run.frames)

            # 危险工具在 write 档仍要人工批准
            responses2 = [
                _calls(ToolCall(id="c2", name="t.danger", arguments="{}")),
                _text("已执行"),
            ]
            app2, state2 = _make_app(responses=responses2)
            async with _client(app2) as client2:
                sid2 = await _new_session(client2)
                await client2.patch(
                    f"/sessions/{sid2}", json={"metadata": {"permission": "write"}}
                )
                handle2 = await _stream_turn(client2, sid2, "删库")
                run2 = state2.runs.get(handle2["run_id"])
                pending = await _wait_for_event(run2, "interaction_required")
                assert pending["tool"] == "t.danger"
                await client2.post(f"/interactions/{pending['id']}", json={"approved": True})
                await asyncio.wait_for(run2.done.wait(), timeout=5)
                assert run2.status == "finished"
            await state2.aclose()
        await state.aclose()

    _run(scenario())


def test_permission_write_free_skips_destructive_approval():
    async def scenario():
        tracer = Tracer()
        responses = [
            _calls(ToolCall(id="c1", name="t.danger", arguments="{}")),
            _text("已执行"),
        ]
        app, state = _make_app(responses=responses, tracer=tracer)
        async with _client(app) as client:
            session_id = await _new_session(client)
            await client.patch(
                f"/sessions/{session_id}", json={"metadata": {"permission": "write_free"}}
            )
            handle = await _stream_turn(client, session_id, "删库")
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)

            assert run.status == "finished"
            assert not any(f["event"] == "interaction_required" for f in run.frames)
            assert run.summary["final_text"] == "已执行"
            # 旁路仍留一条诊断 span，便于事后审计
            auto = [s for s in tracer.spans if s.get("tool") == "approval.auto"]
            assert auto and auto[0]["target"] == "t.danger"
        await state.aclose()

    _run(scenario())


def test_session_without_permission_still_uses_body_scopes():
    """没设档位的会话（含改造前建的）行为不变：仍按请求体的 scopes 授权。"""
    async def scenario():
        responses = [
            _calls(ToolCall(id="c1", name="t.write", arguments='{"path": "a.txt"}')),
            _text("好"),
        ]
        app, state = _make_app(responses=responses)
        async with _client(app) as client:
            session_id = await _new_session(client)
            # 只给 fs:write → 放行
            handle = await _stream_turn(client, session_id, "写", scopes=["fs:write"])
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)
            assert run.status == "finished" and not _tool_error(run)

            # 只给 fs:read → 拒绝
            responses2 = [
                _calls(ToolCall(id="c2", name="t.write", arguments='{"path": "a.txt"}')),
                _text("好"),
            ]
            app2, state2 = _make_app(responses=responses2)
            async with _client(app2) as client2:
                sid2 = await _new_session(client2)
                handle2 = await _stream_turn(client2, sid2, "写", scopes=["fs:read"])
                run2 = state2.runs.get(handle2["run_id"])
                await asyncio.wait_for(run2.done.wait(), timeout=5)
                assert _tool_error(run2)["type"] == "permission_denied"
            await state2.aclose()
        await state.aclose()

    _run(scenario())


def test_session_scopes_and_sandbox_helpers():
    """档位 → scopes 的推导与「会话工作区 → 沙箱策略」的构造。"""
    from harness.session import Session as _Session
    from server.runner import session_sandbox, session_scopes

    workspace = tempfile.mkdtemp()
    plain = _Session.create(session_id="s1")

    # 无档位 → 原样用请求体
    assert session_scopes(plain, ["fs:read"]) == ["fs:read"]
    # 有档位 → 由档位推导并忽略请求体
    plain.meta.metadata["permission"] = "read"
    assert set(session_scopes(plain, ["fs:write"])) == {"fs:read", "net:read"}
    # 脏数据 → 回落请求体
    plain.meta.metadata["permission"] = "yolo"
    assert session_scopes(plain, ["fs:read"]) == ["fs:read"]

    # 工作区：没设 → None（沿用进程级默认）
    assert session_sandbox(plain) is None
    plain.meta.metadata["workspace"] = workspace
    policy = session_sandbox(plain)
    assert policy is not None and policy.enabled is True
    assert policy.root == Path(workspace).resolve()
    assert policy.write_paths == ()  # 工作区内全可写；写权限由档位的 scopes 控制


def test_fs_browse_endpoint():
    async def scenario():
        app, state = _make_app()
        root = Path(tempfile.mkdtemp())
        (root / "sub").mkdir()
        (root / "note.txt").write_text("secret-content", encoding="utf-8")
        async with _client(app) as client:
            body = (await client.get("/fs/browse", params={"path": str(root)})).json()
            assert body["path"] == str(root.resolve())
            assert body["parent"] == str(root.resolve().parent)
            assert body["truncated"] is False
            # 目录在前、文件在后；只给名字与类型
            assert body["entries"][0] == {"name": "sub", "kind": "dir"}
            assert {"name": "note.txt", "kind": "file"} in body["entries"]
            # 绝不返回文件内容
            assert "secret-content" not in (await client.get("/fs/browse", params={"path": str(root)})).text

            # 缺省起点可用（沙箱未开 → 用户主目录）
            default = await client.get("/fs/browse")
            assert default.status_code == 200 and default.json()["path"]

            missing = await client.get("/fs/browse", params={"path": str(root / "nope")})
            assert missing.status_code == 422
            assert missing.json()["error"]["code"] == "invalid_path"
        await state.aclose()

    _run(scenario())


# ---- 技能 / MCP 开关 + zip 上传 ----
_SKILL_MD = """---
name: {name}
description: 当用户给出招标文件并要求解标时使用；从文件提取评审要点并输出报告
---

## 何时使用

描述触发场景。

## 步骤

1. …
"""

_SWITCH_YAML = (
    "llm:\n"
    "  model: m1\n"
    "  base_url: https://example.invalid/v1\n"
    "  api_key: ${HARNESS_TEST_KEY}\n"
    "skills:\n"
    '  root: "__ROOT__"\n'
    "mcp:\n"
    "  servers:\n"
    "    - name: broken\n"
    "      command: __no_such_mcp_binary__\n"
    "observability:\n"
    "  tracer_sink: none\n"
)


def _env(name: str, value: str):
    """临时设置环境变量（`patch_config` 里的 `validate` 用 `current_env()` 做 `${VAR}` 插值）。"""
    import contextlib
    import os

    @contextlib.contextmanager
    def _ctx():
        previous = os.environ.get(name)
        os.environ[name] = value
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    return _ctx()


def _write_skill(root: Path, name: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")


def _switch_app(skill_names: list[str]):
    """造一个带真实配置上下文（含 skills.root 与一条 MCP 声明）的 app。"""
    tmp = Path(tempfile.mkdtemp())
    skills_root = tmp / "skills"
    skills_root.mkdir()
    for name in skill_names:
        _write_skill(skills_root, name)

    cfg_path = tmp / "harness.yaml"
    # 用 as_posix()：Windows 反斜杠在 YAML 双引号里是转义符
    cfg_path.write_text(
        _SWITCH_YAML.replace("__ROOT__", skills_root.as_posix()), encoding="utf-8"
    )
    config = load_config(cfg_path, env={"HARNESS_TEST_KEY": "sk-test"})
    config.server.data_dir = str(tmp / "sessions")
    state = build_state(config)
    state.config_path = str(cfg_path)
    return create_app(state=state), state, cfg_path, skills_root


def test_patch_skill_toggle_persists_and_filters():
    from harness.config.admin import read_yaml

    async def scenario():
        app, state, cfg_path, _ = _switch_app(["alpha", "beta"])
        async with _client(app) as client:
            listed = (await client.get("/skills")).json()
            assert [s["name"] for s in listed] == ["alpha", "beta"]
            assert all(s["enabled"] is True for s in listed)

            off = await client.patch("/skills/alpha", json={"enabled": False})
            assert off.status_code == 200, off.text
            assert off.json()["skill"]["enabled"] is False
            assert off.json()["disabled"] == ["alpha"]
            # 落盘 + 运行期镜像同步
            assert read_yaml(cfg_path)["skills"]["disabled"] == ["alpha"]
            assert state.config.skills.disabled == ["alpha"]
            assert state.skills.names() == ["beta"]           # 曝光面已过滤
            assert state.skills.all_names() == ["alpha", "beta"]

            after = (await client.get("/skills")).json()
            assert {s["name"]: s["enabled"] for s in after} == {"alpha": False, "beta": True}
            health = (await client.get("/healthz")).json()
            assert health["skills"] == 2 and health["skills_enabled"] == 1

            on = await client.patch("/skills/alpha", json={"enabled": True})
            assert on.json()["skill"]["enabled"] is True
            assert read_yaml(cfg_path)["skills"].get("disabled") == []
            assert state.skills.names() == ["alpha", "beta"]
        await state.aclose()

    with _env("HARNESS_TEST_KEY", "sk-test"):
        _run(scenario())


def test_patch_skill_unknown_name_and_unavailable_config():
    async def scenario():
        app, state, _, _ = _switch_app(["alpha"])
        async with _client(app) as client:
            missing = await client.patch("/skills/nope", json={"enabled": False})
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "skill_not_found"
            bad = await client.patch("/skills/alpha", json={})
            assert bad.status_code == 422  # 缺 enabled → Pydantic 校验
        await state.aclose()

        # 注入 state（无配置上下文）→ 409
        app2, state2 = _make_app(skills=SkillRegistry(Path(tempfile.mkdtemp())))
        async with _client(app2) as client:
            blocked = await client.patch("/skills/x", json={"enabled": True})
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "config_unavailable"
        await state2.aclose()

    _run(scenario())


def test_patch_skill_works_while_a_run_is_active():
    """开关只翻一个字段、不重建部件，所以运行中也能切（不返回 config_reload_conflict）。"""
    async def scenario():
        app, state, _, _ = _switch_app(["alpha"])
        async with _client(app) as client:
            session_id = await _new_session(client)
            state.runs.create(session_id)  # 模拟一个活跃 run
            assert state.runs.active_count() == 1
            response = await client.patch("/skills/alpha", json={"enabled": False})
            assert response.status_code == 200, response.text
        await state.aclose()

    with _env("HARNESS_TEST_KEY", "sk-test"):
        _run(scenario())


def test_patch_mcp_toggle_disables_and_reconnects():
    from harness.config.admin import read_yaml

    async def scenario():
        app, state, cfg_path, _ = _switch_app([])
        async with _client(app) as client:
            before = (await client.get("/mcp")).json()
            assert [(s["name"], s["enabled"]) for s in before["servers"]] == [("broken", True)]

            off = await client.patch("/mcp/broken", json={"enabled": False})
            assert off.status_code == 200, off.text
            server = off.json()["servers"][0]
            assert server["enabled"] is False and server["connected"] is False
            assert read_yaml(cfg_path)["mcp"]["servers"][0]["enabled"] is False
            assert state.mcp_configs[0].name == "broken"  # 声明仍在
            health = (await client.get("/healthz")).json()
            assert health["mcp"]["total"] == 1 and health["mcp"]["connected"] == 0

            # 打开：真的会去连（command 不存在 → 失败但不算服务错误，只回 warning）
            on = await client.patch("/mcp/broken", json={"enabled": True})
            assert on.status_code == 200, on.text
            assert on.json()["servers"][0]["enabled"] is True
            assert any("MCP 连接失败" in w for w in on.json()["warnings"])
            assert read_yaml(cfg_path)["mcp"]["servers"][0]["enabled"] is True

            missing = await client.patch("/mcp/nope", json={"enabled": False})
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "mcp_server_not_found"
        await state.aclose()

    with _env("HARNESS_TEST_KEY", "sk-test"):
        _run(scenario())


def _zip_of(files: dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_upload_zip_installs_conflicts_and_overwrites():
    async def scenario():
        app, state, _, skills_root = _switch_app(["alpha"])
        async with _client(app) as client:
            payload = _zip_of({"beta/SKILL.md": _SKILL_MD.format(name="beta")})
            headers = {"Content-Type": "application/zip"}
            created = await client.post("/skills/upload", content=payload, headers=headers)
            assert created.status_code == 201, created.text
            assert [i["name"] for i in created.json()["installed"]] == ["beta"]
            assert (skills_root / "beta" / "SKILL.md").is_file()
            assert [s["name"] for s in (await client.get("/skills")).json()] == ["alpha", "beta"]

            # 同名冲突：整包不落盘，回冲突名单
            both = _zip_of(
                {
                    "alpha/SKILL.md": _SKILL_MD.format(name="alpha"),
                    "gamma/SKILL.md": _SKILL_MD.format(name="gamma"),
                }
            )
            conflict = await client.post("/skills/upload", content=both, headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "skill_conflict"
            assert conflict.json()["error"]["details"]["conflicts"] == ["alpha"]
            assert not (skills_root / "gamma").exists()

            # 勾选覆盖 → 成功并留 .bak
            ok = await client.post(
                "/skills/upload?overwrite=true", content=both, headers=headers
            )
            assert ok.status_code == 201, ok.text
            assert (skills_root / "alpha" / "SKILL.md.bak").is_file()
            assert (skills_root / "gamma").is_dir()
        await state.aclose()

    _run(scenario())


def test_upload_zip_rejects_unsafe_entry():
    async def scenario():
        app, state, _, _ = _switch_app([])
        async with _client(app) as client:
            payload = _zip_of({"../evil/SKILL.md": _SKILL_MD.format(name="evil")})
            response = await client.post(
                "/skills/upload", content=payload, headers={"Content-Type": "application/zip"}
            )
            assert response.status_code == 422
            body = response.json()["error"]
            assert body["code"] == "zip_unsafe_entry"
            assert body["details"]["reason"] == "traversal"
            assert body["details"]["entry"] == "../evil/SKILL.md"
        await state.aclose()

    _run(scenario())


# ---- 软打断（用户插话）----
def _slow_app(gate):
    """带一个可阻塞工具的 app：用来把 run 卡在"工具执行中"。"""
    import threading

    provider = FakeProvider(
        [_calls(ToolCall(id="c1", name="t.slow", arguments="{}")), _text("之后")],
        chunk_size=4,
    )
    registry = _registry()

    def slow() -> str:
        gate.wait(timeout=5)
        return "done"

    registry.register(ToolSpec(name="t.slow", description="慢工具"), slow)
    state = AppState(
        provider=provider,
        registry=registry,
        manager=SessionManager(MemorySessionStore(), compressor=LLMCompressor(provider)),
        prompt=PromptBuilder(base_instructions="你是测试助手"),
        loop_config=LoopConfig(stream=True),
        heartbeat_s=30.0,
    )
    assert isinstance(gate, threading.Event)
    return create_app(state=state), state


def _done_payload(run) -> dict:
    frame = next(f for f in run.frames if f["event"] == "done")
    return json.loads(frame["data"])


def test_turn_interrupt_returns_202_and_reuses_run():
    import threading

    async def scenario():
        gate = threading.Event()
        app, state = _slow_app(gate)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = await _stream_turn(client, session_id, "慢活")
            run = state.runs.get(handle["run_id"])

            second = await client.post(
                f"/sessions/{session_id}/turns", json={"input": "插话", "stream": True}
            )
            assert second.status_code == 202, second.text
            body = second.json()
            assert body["interrupt"] is True
            assert body["run_id"] == handle["run_id"]   # 复用同一个 run，不新建
            assert body["events_url"] == f"/runs/{handle['run_id']}/events"

            gate.set()
            await asyncio.wait_for(run.done.wait(), timeout=5)
            assert run.status == "finished"  # 不新增 Run.status，靠 stop_reason 表达
            payload = _done_payload(run)
            assert payload["stop_reason"] == "interrupted"
            assert payload["interrupts"] == [{"input": "插话", "images": []}]

            # 部分历史落库且成对（下一轮会话合法）
            detail = (await client.get(f"/sessions/{session_id}/messages")).json()
            roles = [m["role"] for m in detail["messages"]]
            assert roles[-2:] == ["assistant", "tool"]
        await state.aclose()

    _run(scenario())


def test_done_payload_has_no_interrupts_without_interrupt():
    async def scenario():
        app, state = _make_app(responses=[_text("正常")])
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = await _stream_turn(client, session_id, "hi")
            run = state.runs.get(handle["run_id"])
            await asyncio.wait_for(run.done.wait(), timeout=5)
            payload = _done_payload(run)
            assert "interrupts" not in payload  # 协议向后兼容
            assert payload["stop_reason"] == "final"
        await state.aclose()

    _run(scenario())


def test_interrupt_releases_pending_approval():
    async def scenario():
        responses = [_calls(ToolCall(id="c1", name="t.danger", arguments="{}")), _text("好的")]
        app, state = _make_app(responses=responses)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = await _stream_turn(client, session_id, "删库")
            run = state.runs.get(handle["run_id"])

            pending = await _wait_for_event(run, "interaction_required")
            # 不点审批卡片，直接插话：应立即用默认值（拒绝）兑现，而不是等满 300s 超时
            second = await client.post(
                f"/sessions/{session_id}/turns", json={"input": "改主意了", "stream": True}
            )
            assert second.status_code == 202
            assert second.json()["released_interactions"] == [pending["id"]]

            await asyncio.wait_for(run.done.wait(), timeout=1)
            assert _done_payload(run)["stop_reason"] == "interrupted"

            # 已消费：审批卡片回填变 404
            again = await client.post(f"/interactions/{pending['id']}", json={"approved": True})
            assert again.status_code == 404
        await state.aclose()

    _run(scenario())


def test_non_stream_interrupt_returns_202():
    import threading

    async def scenario():
        gate = threading.Event()
        app, state = _slow_app(gate)
        async with _client(app) as client:
            session_id = await _new_session(client)
            handle = await _stream_turn(client, session_id, "慢活")
            run = state.runs.get(handle["run_id"])

            # 非流式也统一返回 202（等到的会是"上一条"的答复，返回它反而是错的）
            response = await client.post(
                f"/sessions/{session_id}/turns", json={"input": "插话", "stream": False}
            )
            assert response.status_code == 202
            assert response.json()["interrupt"] is True

            gate.set()
            await asyncio.wait_for(run.done.wait(), timeout=5)
        await state.aclose()

    _run(scenario())


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
