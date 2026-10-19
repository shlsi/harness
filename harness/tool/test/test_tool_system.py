"""工具系统冒烟测试。

既可用 pytest 运行，也可直接执行：python -m harness.tool.test.test_tool_system
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from harness.tool import (
    DEFAULT_DENIED_DOMAINS,
    Context,
    Executor,
    Policy,
    SandboxPolicy,
    SandboxViolation,
    ToolRegistry,
    ToolSpec,
    Tracer,
    check_url,
    configure_sandbox,
    default_registry,
    resolve_path,
    tool,
)
from harness.tool.builtin import code as code_mod
from harness.tool.builtin import net as net_mod
from harness.tool.builtin import shell as shell_mod
from harness.tool.schema import ErrorType


def _run(coro):
    return asyncio.run(coro)


def _tmp_file(text: str = "hello world") -> str:
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    )
    tmp.write(text)
    tmp.close()
    return tmp.name


# ---- 注册与 schema 导出 ----
def test_openai_schema_exposed():
    defs = default_registry.to_openai_tools()
    names = [d["function"]["name"] for d in defs]
    assert "fs.read" in names and "fs.write" in names
    read = next(d for d in defs if d["function"]["name"] == "fs.read")
    props = read["function"]["parameters"]["properties"]
    assert "path" in props
    assert read["function"]["parameters"]["required"] == ["path"]


def test_filter_by_tags():
    names = {s.name for s in default_registry.list(tags=["write"])}
    assert {"fs.write", "fs.edit"} <= names


# ---- 执行管线 ----
def test_read_ok():
    path = _tmp_file("hello world")
    ex = Executor(default_registry)
    res = _run(ex.call("fs.read", {"path": path}, Context(scopes={"fs:read"})))
    assert res.ok and res.content == "hello world"
    assert res.meta["duration_ms"] >= 0


def test_unknown_tool():
    ex = Executor(default_registry)
    res = _run(ex.call("no.such", {}, Context()))
    assert not res.ok and res.error.type == ErrorType.TOOL_NOT_FOUND


def test_invalid_args_missing_field():
    ex = Executor(default_registry)
    res = _run(ex.call("fs.read", {"max_bytes": 10}, Context(scopes={"fs:read"})))
    assert not res.ok and res.error.type == ErrorType.INVALID_ARGS
    assert res.error.details["fields"][0]["field"] == "path"


def test_permission_denied_when_scope_missing():
    path = _tmp_file()
    ex = Executor(default_registry)
    res = _run(ex.call("fs.read", {"path": path}, Context(scopes=set())))
    assert not res.ok and res.error.type == ErrorType.PERMISSION_DENIED


def test_execution_error_is_structured():
    ex = Executor(default_registry)
    res = _run(
        ex.call("fs.read", {"path": "Z:/definitely/missing.txt"}, Context(scopes={"fs:read"}))
    )
    assert not res.ok and res.error.type == ErrorType.EXECUTION_ERROR
    assert res.error.details["exception"] == "FileNotFoundError"


# ---- 截断 ----
def test_result_truncated():
    reg = ToolRegistry()

    @tool(name="t.big", description="big", result_max_chars=5, registry=reg)
    def big() -> str:
        return "abcdefghij"

    ex = Executor(reg)
    res = _run(ex.call("t.big", {}, Context()))
    assert res.ok and res.content.startswith("abcde") and "已截断" in res.content


# ---- 幂等重试 ----
def test_idempotent_retry():
    reg = ToolRegistry()
    state = {"n": 0}

    @tool(name="t.flaky", description="flaky", idempotent=True, max_retries=3, registry=reg)
    def flaky() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    ex = Executor(reg)
    res = _run(ex.call("t.flaky", {}, Context()))
    assert res.ok and state["n"] == 3 and res.meta["attempts"] == 3


def test_non_idempotent_no_retry():
    reg = ToolRegistry()
    state = {"n": 0}

    @tool(name="t.once", description="once", idempotent=False, max_retries=3, registry=reg)
    def once() -> str:
        state["n"] += 1
        raise RuntimeError("boom")

    ex = Executor(reg)
    res = _run(ex.call("t.once", {}, Context()))
    assert not res.ok and state["n"] == 1


# ---- 审批 ----
def test_destructive_requires_approval_missing_callback():
    reg = ToolRegistry()

    @tool(name="t.del", description="del", side_effect="destructive", registry=reg)
    def rm(path: str) -> str:
        return f"deleted {path}"

    ex = Executor(reg)
    res = _run(ex.call("t.del", {"path": "x"}, Context()))
    assert not res.ok and res.error.type == ErrorType.APPROVAL_REQUIRED


def test_destructive_approved():
    reg = ToolRegistry()
    seen = {}

    @tool(name="t.del2", description="del", side_effect="destructive", registry=reg)
    def rm(path: str) -> str:
        return f"deleted {path}"

    async def approve(req):
        seen["tool"] = req.tool
        return True

    ex = Executor(reg)
    res = _run(ex.call("t.del2", {"path": "x"}, Context(approval=approve)))
    assert res.ok and seen["tool"] == "t.del2" and res.content == "deleted x"


def test_destructive_denied():
    reg = ToolRegistry()

    @tool(name="t.del3", description="del", side_effect="destructive", registry=reg)
    def rm(path: str) -> str:
        return f"deleted {path}"

    async def deny(req):
        return False

    ex = Executor(reg)
    res = _run(ex.call("t.del3", {"path": "x"}, Context(approval=deny)))
    assert not res.ok and res.error.type == ErrorType.PERMISSION_DENIED


# ---- 批量 tool_calls 按 id 归位 ----
def test_execute_tool_calls_maps_by_id():
    path = _tmp_file("payload")
    ex = Executor(default_registry)
    tool_calls = [
        {
            "id": "call_a",
            "type": "function",
            "function": {"name": "fs.read", "arguments": json.dumps({"path": path})},
        },
        {
            "id": "call_b",
            "type": "function",
            "function": {"name": "no.such", "arguments": "{}"},
        },
    ]
    messages = _run(ex.execute_tool_calls(tool_calls, Context(scopes={"fs:read"})))
    assert [m["tool_call_id"] for m in messages] == ["call_a", "call_b"]
    assert messages[0]["role"] == "tool" and messages[0]["content"] == "payload"
    assert "tool_not_found" in messages[1]["content"]


def test_write_then_read_roundtrip():
    reg = ToolRegistry()
    from harness.tool.builtin import fs as fs_mod  # noqa: F401

    reg.register(default_registry.get("fs.read").spec, default_registry.get("fs.read").fn)
    reg.register(default_registry.get("fs.write").spec, default_registry.get("fs.write").fn)
    ex = Executor(reg, policy=Policy())
    with tempfile.TemporaryDirectory() as d:
        target = str(Path(d) / "out.txt")
        ctx = Context(scopes={"fs:read", "fs:write"})
        w = _run(ex.call("fs.write", {"path": target, "content": "data"}, ctx))
        assert w.ok, w.error
        r = _run(ex.call("fs.read", {"path": target}, ctx))
        assert r.ok and r.content == "data"


# ---- P0：文件类工具 ----
def test_fs_edit_ok():
    path = _tmp_file("hello world")
    ex = Executor(default_registry)
    res = _run(
        ex.call(
            "fs.edit",
            {"path": path, "old_string": "world", "new_string": "there"},
            Context(scopes={"fs:write"}),
        )
    )
    assert res.ok and Path(path).read_text(encoding="utf-8") == "hello there"


def test_fs_edit_requires_unique_match():
    path = _tmp_file("a a")
    ex = Executor(default_registry)
    res = _run(
        ex.call(
            "fs.edit",
            {"path": path, "old_string": "a", "new_string": "b"},
            Context(scopes={"fs:write"}),
        )
    )
    assert not res.ok and "不唯一" in res.error.message


def test_fs_list_glob_grep():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.txt").write_text("needle here", encoding="utf-8")
        (Path(d) / "sub").mkdir()
        (Path(d) / "sub" / "b.py").write_text("print('needle')", encoding="utf-8")
        ex = Executor(default_registry)
        ctx = Context(scopes={"fs:read"})

        listing = _run(ex.call("fs.list", {"path": d, "depth": 2}, ctx))
        assert listing.ok and any(str(x).endswith("b.py") for x in listing.raw)

        globbed = _run(ex.call("fs.glob", {"pattern": "**/*.py", "root": d}, ctx))
        assert globbed.ok and len(globbed.raw) == 1

        grepped = _run(ex.call("fs.grep", {"pattern": "needle", "root": d}, ctx))
        assert grepped.ok and len(grepped.raw) == 2


# ---- P0：shell.run（白名单 + 审批）----
async def _approve(_req):
    return True


def test_shell_requires_approval():
    ex = Executor(default_registry)
    res = _run(ex.call("shell.run", {"command": "echo hi"}, Context(scopes={"shell:run"})))
    assert not res.ok and res.error.type == ErrorType.APPROVAL_REQUIRED


def test_shell_blocks_non_whitelisted():
    ex = Executor(default_registry)
    ctx = Context(scopes={"shell:run"}, approval=_approve)
    res = _run(ex.call("shell.run", {"command": "evilcmd --x"}, ctx))
    assert not res.ok and "白名单" in res.error.message


def test_shell_blocks_denied_pattern():
    ex = Executor(default_registry)
    ctx = Context(scopes={"shell:run"}, approval=_approve)
    res = _run(ex.call("shell.run", {"command": "echo hi && rm -rf /"}, ctx))
    assert not res.ok and "禁止模式" in res.error.message


def test_shell_run_echo():
    ex = Executor(default_registry)
    ctx = Context(scopes={"shell:run"}, approval=_approve)
    res = _run(ex.call("shell.run", {"command": "echo harness_ok"}, ctx))
    assert res.ok and "harness_ok" in res.content and "exit=0" in res.content


# ---- P0：todo.write / ask_user（Context 注入）----
def test_todo_write():
    ex = Executor(default_registry)
    ctx = Context(session_id="s1")
    res = _run(
        ex.call(
            "todo.write",
            {
                "todos": [
                    {"content": "写文档", "status": "pending", "priority": "high"},
                    {"content": "写代码", "status": "completed"},
                ]
            },
            ctx,
        )
    )
    assert res.ok and "写文档" in res.content and "[x]" in res.content


def test_todo_write_invalid_status():
    ex = Executor(default_registry)
    res = _run(
        ex.call("todo.write", {"todos": [{"content": "x", "status": "bogus"}]}, Context())
    )
    assert not res.ok and "status" in res.error.message


def test_ask_user_with_hook():
    ex = Executor(default_registry)
    seen = {}

    async def ask(req):
        seen["question"] = req.question
        return "yes"

    res = _run(ex.call("ask_user", {"question": "ok?"}, Context(ask=ask)))
    assert res.ok and res.content == "yes" and seen["question"] == "ok?"


def test_ask_user_without_hook():
    ex = Executor(default_registry)
    res = _run(ex.call("ask_user", {"question": "ok?"}, Context()))
    assert not res.ok and "ask 回调" in res.error.message


def test_context_param_not_exposed_to_model():
    for name in ("todo.write", "ask_user"):
        props = default_registry.get(name).spec.input_schema["properties"]
        assert "ctx" not in props


# ---- 外部工具适配器（伪适配器）----
class _FakeAdapter:
    def __init__(self, spec):
        self._spec = spec
        self.calls = []

    def describe(self):
        return [self._spec]

    async def invoke(self, spec, args, ctx):
        self.calls.append((spec.name, args))
        return {"echo": args}


def _echo_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="echo",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    )


def test_register_external_invoke():
    reg = ToolRegistry()
    spec = _echo_spec("fake.echo")
    adapter = _FakeAdapter(spec)
    reg.register_external(spec, adapter)
    ex = Executor(reg)
    res = _run(ex.call("fake.echo", {"msg": "hi"}, Context()))
    assert res.ok and adapter.calls == [("fake.echo", {"msg": "hi"})]


def test_register_external_validates_by_schema():
    reg = ToolRegistry()
    spec = _echo_spec("fake.echo2")
    reg.register_external(spec, _FakeAdapter(spec))
    ex = Executor(reg)
    res = _run(ex.call("fake.echo2", {}, Context()))
    assert not res.ok and res.error.type == ErrorType.INVALID_ARGS
    assert res.error.details["fields"][0]["field"] == "msg"


def test_register_external_requires_schema():
    reg = ToolRegistry()
    try:
        reg.register_external(ToolSpec(name="fake.bad", description="x"), _FakeAdapter(None))
    except ValueError as exc:
        assert "input_schema" in str(exc)
    else:
        raise AssertionError("缺少 input_schema 时应报错")


# ---- MCP 端到端（stdio 子进程）----
def test_mcp_stdio_end_to_end():
    from harness.tool.adapter.mcp import MCPServerConfig
    from harness.tool.loader import load_mcp_servers

    server_script = Path(__file__).with_name("mcp_echo_server.py")

    async def scenario():
        reg = ToolRegistry()
        config = MCPServerConfig(
            name="echo",
            transport="stdio",
            command=sys.executable,
            args=[str(server_script)],
        )
        loaded = await load_mcp_servers([config], reg)
        try:
            assert "echo.echo" in reg.names()
            spec = reg.get("echo.echo").spec
            # 默认宽松：只读、免审批
            assert spec.side_effect == "read" and spec.requires_approval is False
            ex = Executor(reg)
            return await ex.call("echo.echo", {"text": "hi"}, Context())
        finally:
            await loaded.aclose()

    res = _run(scenario())
    assert res.ok, res.error
    assert "hi" in res.content


def test_mcp_aclose_tolerates_anyio_task_error():
    """anyio 的 cancel scope 只能在创建它的 task 里退出；宿主在请求任务里关 lifespan 建立的
    连接时必然报 `RuntimeError`。此时子进程已终止、连接已断，应记为告警而不是让请求 500。"""
    from harness.tool.adapter.mcp import MCPClient, MCPServerConfig
    from harness.tool.loader import LoadedServers

    class _BoomStack:
        async def aclose(self) -> None:
            raise RuntimeError("Attempted to exit cancel scope in a different task")

    async def scenario():
        client = MCPClient(MCPServerConfig(name="x", command="python"))
        client._stack = _BoomStack()  # type: ignore[assignment]
        loaded = LoadedServers()
        loaded.clients.append(client)

        warnings = await loaded.aclose()
        assert len(warnings) == 1 and "'x'" in warnings[0] and "cancel scope" in warnings[0]
        assert client.session is None
        assert loaded.clients == []
        assert await loaded.aclose() == []  # 可重复调用

    _run(scenario())


def test_mcp_tool_overrides():
    from harness.tool.adapter.mcp import MCPServerConfig
    from harness.tool.loader import load_mcp_servers

    server_script = Path(__file__).with_name("mcp_echo_server.py")

    async def scenario():
        reg = ToolRegistry()
        config = MCPServerConfig(
            name="echo",
            command=sys.executable,
            args=[str(server_script)],
            tool_overrides={
                "echo": {"side_effect": "destructive", "requires_approval": True}
            },
        )
        loaded = await load_mcp_servers([config], reg)
        try:
            spec = reg.get("echo.echo").spec
            assert spec.side_effect == "destructive" and spec.requires_approval is True
            ex = Executor(reg)
            # 未提供审批回调 → approval_required
            return await ex.call("echo.echo", {"text": "hi"}, Context())
        finally:
            await loaded.aclose()

    res = _run(scenario())
    assert not res.ok and res.error.type == ErrorType.APPROVAL_REQUIRED


# ---- 多 Agent 预留：Context 可派生 / Tracer 父子关联 ----
def test_context_derive_child_narrows_scopes():
    parent = Context(scopes={"fs:read", "fs:write"}, session_id="s1", metadata={"k": "v"})
    child = parent.derive_child(scopes={"fs:read"})
    assert child.trace_id != parent.trace_id
    assert child.scopes == {"fs:read"}  # 收窄
    assert child.session_id == "s1"
    assert child.metadata["k"] == "v"  # 继承
    assert child.metadata["parent_trace_id"] == parent.trace_id  # 关联父链


def test_context_derive_child_inherits_scopes_and_hooks():
    async def approve(req):
        return True

    parent = Context(scopes={"a"}, approval=approve)
    child = parent.derive_child()
    assert child.scopes == {"a"} and child.approval is approve


def test_tracer_records_parent_span_id():
    tracer = Tracer()
    span = tracer.record(kind="x", parent_span_id="p1")
    assert span["parent_span_id"] == "p1"
    assert "parent_span_id" not in tracer.record(kind="y")


# ---- 沙箱（工作区边界 + 路径权限）----
@contextmanager
def _sandbox(**kwargs):
    """临时应用一套沙箱策略，退出时还原（策略是进程级全局）。"""
    prev = configure_sandbox(**kwargs)
    try:
        yield prev
    finally:
        configure_sandbox(
            enabled=prev.enabled,
            root=prev.root,
            write_paths=prev.write_paths,
            deny_paths=prev.deny_paths,
            readonly_mounts=prev.readonly_mounts,
            writable_mounts=prev.writable_mounts,
            extra_denied_domains=prev.extra_denied_domains,
        )


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp())


def test_sandbox_disabled_passes_path_through():
    # 默认关闭：resolve_path 原样返回，行为与引入沙箱前逐字节一致
    from harness.tool.sandbox import resolve_path

    raw = "../outside/whatever.txt"
    with _sandbox(enabled=False):
        assert resolve_path(raw, mode="read") == Path(raw)
        assert resolve_path(raw, mode="write") == Path(raw)


def test_sandbox_allows_read_and_write_inside_root():
    root = _tmp_dir()
    with _sandbox(enabled=True, root=root, write_paths=(), deny_paths=()):
        ex = Executor(default_registry)
        ctx = Context(scopes={"fs:read", "fs:write"})
        w = _run(ex.call("fs.write", {"path": "a/b.txt", "content": "hi"}, ctx))
        assert w.ok, w.error and w.error.message
        assert (root / "a" / "b.txt").read_text(encoding="utf-8") == "hi"
        r = _run(ex.call("fs.read", {"path": "a/b.txt"}, ctx))
        assert r.ok and r.content == "hi"


def test_sandbox_relative_path_resolves_against_root():
    root = _tmp_dir()
    (root / "note.txt").write_text("inside", encoding="utf-8")
    with _sandbox(enabled=True, root=root):
        from harness.tool.sandbox import resolve_path

        real = resolve_path("note.txt", mode="read")
    assert real == (root / "note.txt").resolve()


def test_sandbox_rejects_read_outside_root():
    root = _tmp_dir()
    outside = _tmp_file("secret")
    with _sandbox(enabled=True, root=root, readonly_mounts=()):
        ex = Executor(default_registry)
        res = _run(ex.call("fs.read", {"path": outside}, Context(scopes={"fs:read"})))
    assert not res.ok
    assert res.error.type == ErrorType.SANDBOX_DENIED
    assert res.error.retryable is False


def test_sandbox_rejects_write_outside_root():
    root = _tmp_dir()
    target = _tmp_dir() / "evil.txt"
    with _sandbox(enabled=True, root=root):
        ex = Executor(default_registry)
        res = _run(
            ex.call(
                "fs.write",
                {"path": str(target), "content": "x"},
                Context(scopes={"fs:write"}),
            )
        )
    assert not res.ok and res.error.type == ErrorType.SANDBOX_DENIED
    assert not target.exists()


def test_sandbox_rejects_dotdot_escape():
    root = _tmp_dir()
    (root / "sub").mkdir()
    with _sandbox(enabled=True, root=root):
        ex = Executor(default_registry)
        res = _run(
            ex.call("fs.read", {"path": "sub/../../outside.txt"}, Context(scopes={"fs:read"}))
        )
    assert not res.ok and res.error.type == ErrorType.SANDBOX_DENIED


def test_sandbox_write_whitelist_narrows_root():
    root = _tmp_dir()
    with _sandbox(enabled=True, root=root, write_paths=("src/**",)):
        from harness.tool.sandbox import resolve_path

        assert resolve_path("src/a.py", mode="write") == (root / "src" / "a.py").resolve()
        raised = False
        try:
            resolve_path("docs/b.md", mode="write")
        except SandboxViolation:
            raised = True
        assert raised
        # 读不受 write_paths 影响
        assert resolve_path("docs/b.md", mode="read") == (root / "docs" / "b.md").resolve()


def test_sandbox_deny_paths_override_read_and_write():
    root = _tmp_dir()
    (root / ".git").mkdir()
    with _sandbox(enabled=True, root=root, deny_paths=("**/.git/**", ".env")):
        from harness.tool.sandbox import resolve_path

        for target, mode in ((root / ".git" / "config", "read"), (root / ".env", "write")):
            raised = False
            try:
                resolve_path(target, mode=mode)
            except SandboxViolation:
                raised = True
            assert raised, f"{target} ({mode}) 应被黑名单拦下"


def test_sandbox_readonly_mount_allows_read_blocks_write():
    root = _tmp_dir()
    mount = _tmp_dir()
    with _sandbox(enabled=True, root=root, readonly_mounts=(mount,)):
        from harness.tool.sandbox import resolve_path

        assert resolve_path(mount / "a.txt", mode="read") == (mount / "a.txt").resolve()
        raised = False
        try:
            resolve_path(mount / "a.txt", mode="write")
        except SandboxViolation:
            raised = True
        assert raised


def test_sandbox_writable_mount_allows_write():
    root = _tmp_dir()
    mount = _tmp_dir()
    with _sandbox(enabled=True, root=root, writable_mounts=(mount,)):
        from harness.tool.sandbox import resolve_path

        assert resolve_path(mount / "a.txt", mode="write") == (mount / "a.txt").resolve()


def test_sandbox_list_hides_outside_entries():
    root = _tmp_dir()
    outside = _tmp_dir()
    (root / "inside.txt").write_text("x", encoding="utf-8")
    # 用一个指向工作区外的 junction/符号链接考验列目录过滤
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return  # Windows 无权限创建符号链接：跳过
    with _sandbox(enabled=True, root=root):
        ex = Executor(default_registry)
        res = _run(ex.call("fs.list", {"path": "."}, Context(scopes={"fs:read"})))
    assert res.ok
    assert "inside.txt" in res.content
    assert "escape" not in res.content


def test_shell_cwd_defaults_to_root():
    root = _tmp_dir()
    (root / "marker.txt").write_text("x", encoding="utf-8")
    with _sandbox(enabled=True, root=root):
        out = _run(shell_mod.run_command("dir"))
    assert "exit=0" in out and "marker.txt" in out


def test_shell_command_scan_rejects_escape():
    root = _tmp_dir()
    with _sandbox(enabled=True, root=root):
        raised = False
        try:
            _run(shell_mod.run_command(r"type ..\..\Windows\System32\drivers\etc\hosts"))
        except SandboxViolation:
            raised = True
    assert raised


def test_shell_command_scan_ignores_non_path_tokens():
    root = _tmp_dir()
    with _sandbox(enabled=True, root=root):
        out = _run(shell_mod.run_command('echo hello --grep=.. world'))
    assert "exit=0" in out and "hello" in out


def test_sandbox_violation_is_not_retried():
    # 越界是确定性失败：即便工具声明 idempotent 也不重试
    calls: list[int] = []

    def boom(path: str) -> str:
        calls.append(1)
        from harness.tool.sandbox import resolve_path

        resolve_path(path, mode="read")
        return "unreachable"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="t.boom", description="x", idempotent=True, max_retries=3, scopes=()),
        boom,
    )
    root = _tmp_dir()
    with _sandbox(enabled=True, root=root):
        res = _run(Executor(registry).call("t.boom", {"path": _tmp_file("y")}, Context()))
    assert not res.ok and res.error.type == ErrorType.SANDBOX_DENIED
    assert len(calls) == 1


# ---- 网络域名策略 ----
def test_check_url_blocks_ssrf_baseline():
    for url in (
        "http://localhost/admin",
        "http://127.0.0.1:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://api.internal/v1",
        "https://foo.local/x",
        "https://a.localhost/y",
    ):
        raised = False
        try:
            check_url(url)
        except SandboxViolation:
            raised = True
        assert raised, f"{url} 应被 SSRF 基线拦下"


def test_check_url_allows_normal_domains():
    assert check_url("https://api.example.com/v1?x=1") == "api.example.com"
    assert check_url("http://EXAMPLE.com") == "example.com"


def test_check_url_rejects_non_http_scheme():
    for url in ("file:///C:/Windows/win.ini", "ftp://example.com/x", "javascript:alert(1)"):
        raised = False
        try:
            check_url(url)
        except SandboxViolation:
            raised = True
        assert raised, f"{url} 应被拒（非 http/https）"


def test_check_url_extra_denied_domains_appended():
    # 追加**不影响**基线：配置项只能加，不能覆盖
    with _sandbox(extra_denied_domains=("blocked.example",)):
        raised = False
        try:
            check_url("https://sub.blocked.example/x")  # 无通配字符：连带子域
        except SandboxViolation:
            raised = True
        assert raised
        assert check_url("https://example.com") == "example.com"  # 基线仍在
        try:
            check_url("http://localhost/")
            raise AssertionError("基线不应被配置覆盖")
        except SandboxViolation:
            pass
    assert "localhost" in DEFAULT_DENIED_DOMAINS


def test_net_fetch_denied_domain_surfaces_as_sandbox_denied():
    ex = Executor(default_registry)
    res = _run(
        ex.call("net.fetch", {"url": "http://169.254.169.254/"}, Context(scopes={"net:read"}))
    )
    assert not res.ok and res.error.type == ErrorType.SANDBOX_DENIED


# ---- net.fetch / net.download（用假响应，保持离线）----
class _FakeResponse:
    def __init__(self, body: bytes, *, content_type="text/plain", url="https://example.com/x"):
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.status = 200
        self._url = url

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _fake_http(response):
    """替换 net._open，绕过真实网络（域名校验已在 check_url 层单测覆盖）。"""
    original = net_mod._open
    net_mod._open = lambda url: response
    try:
        yield
    finally:
        net_mod._open = original


@contextmanager
def _net_limits(**kwargs):
    previous = net_mod.configure_net_policy(**kwargs)
    try:
        yield previous
    finally:
        net_mod.configure_net_policy(
            max_bytes=previous.max_bytes, max_download_bytes=previous.max_download_bytes
        )


def test_net_fetch_strips_html_and_reports_meta():
    html = b"<html><head><style>p{}</style></head><body><h1>Title</h1><p>Body text</p></body></html>"
    with _fake_http(_FakeResponse(html, content_type="text/html; charset=utf-8")):
        out = net_mod.fetch("https://example.com/page")
    assert out["status"] == 200 and out["final_url"] == "https://example.com/x"
    assert "Title" in out["text"] and "Body text" in out["text"]
    assert "<h1>" not in out["text"] and "p{}" not in out["text"]  # 标签与 style 都剥掉


def test_net_fetch_truncates_at_max_bytes():
    with _net_limits(max_bytes=10):
        with _fake_http(_FakeResponse(b"0123456789ABCDEF")):
            out = net_mod.fetch("https://example.com/big")
    assert out["truncated"] is True
    assert out["text"] == "0123456789"


def test_net_download_writes_atomically():
    dest = Path(tempfile.mkdtemp()) / "sub" / "out.bin"
    with _fake_http(_FakeResponse(b"payload-bytes")):
        message = net_mod.download("https://example.com/f", str(dest))
    assert dest.read_bytes() == b"payload-bytes"
    assert "已下载 13 字节" in message
    assert not list(dest.parent.glob(".net-*.part"))  # 临时文件已清掉


def test_net_download_aborts_over_limit_without_partial_file():
    dest = Path(tempfile.mkdtemp()) / "big.bin"
    raised = False
    with _net_limits(max_download_bytes=5):
        with _fake_http(_FakeResponse(b"0123456789")):
            try:
                net_mod.download("https://example.com/big", str(dest))
            except ValueError as exc:
                raised = "上限" in str(exc)
    assert raised
    assert not dest.exists()  # 不留半成品
    assert not list(dest.parent.glob(".net-*.part"))


def test_net_download_respects_sandbox():
    root = _tmp_dir()
    outside = _tmp_dir() / "x.bin"
    with _sandbox(enabled=True, root=root):
        ex = Executor(default_registry)
        res = _run(
            ex.call(
                "net.download",
                {"url": "https://example.com/f", "path": str(outside)},
                Context(scopes={"net:read", "fs:write"}),
            )
        )
    assert not res.ok and res.error.type == ErrorType.SANDBOX_DENIED


# ---- fs 增删改 ----
def test_fs_mkdir_copy_move_delete_roundtrip():
    root = _tmp_dir()
    ex = Executor(default_registry)
    ctx = Context(scopes={"fs:read", "fs:write"})

    async def approve(_req):
        return True

    ctx.approval = approve  # type: ignore[assignment]
    with _sandbox(enabled=True, root=root):
        assert _run(ex.call("fs.mkdir", {"path": "a/b"}, ctx)).ok
        assert _run(
            ex.call("fs.write", {"path": "a/b/f.txt", "content": "hi"}, ctx)
        ).ok
        assert _run(ex.call("fs.copy", {"src": "a/b/f.txt", "dst": "a/c.txt"}, ctx)).ok
        assert (root / "a" / "c.txt").read_text(encoding="utf-8") == "hi"
        assert _run(ex.call("fs.move", {"src": "a/c.txt", "dst": "a/d.txt"}, ctx)).ok
        assert (root / "a" / "d.txt").exists() and not (root / "a" / "c.txt").exists()
        assert _run(ex.call("fs.delete", {"path": "a/d.txt"}, ctx)).ok
        assert not (root / "a" / "d.txt").exists()


def test_fs_copy_refuses_existing_target():
    root = _tmp_dir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    with _sandbox(enabled=True, root=root):
        res = _run(
            Executor(default_registry).call(
                "fs.copy",
                {"src": "a.txt", "dst": "b.txt"},
                Context(scopes={"fs:write"}),
            )
        )
    assert not res.ok and "目标已存在" in res.error.message
    assert (root / "b.txt").read_text(encoding="utf-8") == "b"  # 未被覆盖


def test_fs_move_cannot_escape_workspace():
    root = _tmp_dir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    outside = _tmp_dir() / "a.txt"
    with _sandbox(enabled=True, root=root):
        res = _run(
            Executor(default_registry).call(
                "fs.move",
                {"src": "a.txt", "dst": str(outside)},
                Context(scopes={"fs:write"}),
            )
        )
    assert not res.ok and res.error.type == ErrorType.SANDBOX_DENIED
    assert (root / "a.txt").exists()  # 没被搬走


def test_fs_delete_requires_approval():
    root = _tmp_dir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    with _sandbox(enabled=True, root=root):
        res = _run(
            Executor(default_registry).call(
                "fs.delete", {"path": "a.txt"}, Context(scopes={"fs:write"})
            )
        )
    assert not res.ok and res.error.type == ErrorType.APPROVAL_REQUIRED
    assert (root / "a.txt").exists()


def test_fs_delete_dir_needs_recursive():
    root = _tmp_dir()
    (root / "d").mkdir()
    (root / "d" / "f.txt").write_text("x", encoding="utf-8")

    async def approve(_req):
        return True

    ctx = Context(scopes={"fs:write"}, approval=approve)
    with _sandbox(enabled=True, root=root):
        ex = Executor(default_registry)
        denied = _run(ex.call("fs.delete", {"path": "d"}, ctx))
        assert not denied.ok and "recursive=True" in denied.error.message
        ok = _run(ex.call("fs.delete", {"path": "d", "recursive": True}, ctx))
        assert ok.ok and not (root / "d").exists()


def test_fs_delete_refuses_workspace_root():
    root = _tmp_dir()

    async def approve(_req):
        return True

    with _sandbox(enabled=True, root=root):
        res = _run(
            Executor(default_registry).call(
                "fs.delete",
                {"path": str(root), "recursive": True},
                Context(scopes={"fs:write"}, approval=approve),
            )
        )
    assert not res.ok and "拒绝删除工作区根目录" in res.error.message
    assert root.exists()


# ---- time.now ----
def test_time_now_returns_iso_fields():
    res = _run(Executor(default_registry).call("time.now", {}, Context()))
    assert res.ok
    assert res.content.count('"iso"') == 1
    assert "T" in res.content  # ISO 8601
    assert res.raw["date"] in res.raw["iso"]


# ---- code.run ----
def test_code_run_python_prints_stdout():
    out = _run(code_mod.run_code("python", "print(6 * 7)"))
    assert "exit=0" in out and "42" in out


def test_code_run_cwd_is_inside_workspace_tmp():
    out = _run(code_mod.run_code("python", "import os; print(os.getcwd())"))
    workdir = resolve_path(code_mod.TMP_DIR, mode="write").resolve()
    assert str(workdir) in out


def test_code_run_cleans_secret_env_vars():
    import os

    os.environ["HARNESS_FAKE_SECRET"] = "leak-me"
    try:
        out = _run(
            code_mod.run_code(
                "python", "import os; print(os.environ.get('HARNESS_FAKE_SECRET', 'MISSING'))"
            )
        )
    finally:
        os.environ.pop("HARNESS_FAKE_SECRET", None)
    assert "MISSING" in out and "leak-me" not in out


def test_code_run_removes_temp_dir():
    workdir = resolve_path(code_mod.TMP_DIR, mode="write").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    _run(code_mod.run_code("python", "print('x')"))
    assert not list(workdir.glob("code-*"))


def test_code_run_rejects_unsupported_language():
    raised = False
    try:
        _run(code_mod.run_code("brainfuck", "+"))
    except ValueError as exc:
        raised = "不支持的语言" in str(exc)
    assert raised


def test_code_run_interpreter_must_be_whitelisted():
    previous = shell_mod.configure_shell_policy(allowed_commands=["echo"], deny_patterns=[])
    try:
        raised = False
        try:
            _run(code_mod.run_code("python", "print(1)"))
        except ValueError as exc:
            raised = "allowed_commands" in str(exc)
        assert raised
    finally:
        shell_mod.configure_shell_policy(
            allowed_commands=sorted(previous.allowed_commands),
            deny_patterns=list(previous.deny_patterns),
        )


# ---- 沙箱的每调用覆盖（会话级工作区：Context.sandbox + use_sandbox）----
def _policy(root: Path) -> SandboxPolicy:
    """一份「开启沙箱 + 指定工作区 + 全可写」的策略（会话级工作区的构造结果）。"""
    return SandboxPolicy(enabled=True, root=root.resolve(), write_paths=(), deny_paths=())


def test_use_sandbox_overrides_and_restores():
    from harness.tool import current_sandbox, use_sandbox

    outer, inner = _tmp_dir(), _tmp_dir()
    with _sandbox(enabled=True, root=outer, write_paths=(), deny_paths=()):
        assert current_sandbox().root == outer.resolve()
        with use_sandbox(_policy(inner)):
            assert current_sandbox().root == inner.resolve()
        assert current_sandbox().root == outer.resolve()


def test_use_sandbox_none_is_noop():
    """`None` 不能覆盖：否则会抹掉外层策略、静默回落到进程全局。"""
    from harness.tool import current_sandbox, use_sandbox

    root = _tmp_dir()
    with _sandbox(enabled=True, root=root, write_paths=(), deny_paths=()):
        with use_sandbox(None):
            assert current_sandbox().root == root.resolve()
            assert current_sandbox().enabled is True


def test_context_sandbox_applies_to_sync_tool_in_thread():
    """`fs.*` 是同步工具，走 `asyncio.to_thread`；覆盖值必须能传进线程。"""
    ctx_root = _tmp_dir()
    (ctx_root / "note.txt").write_text("inside-ctx", encoding="utf-8")

    # 进程级全局**没开**沙箱：只有 ctx.sandbox 生效
    with _sandbox(enabled=False):
        ex = Executor(default_registry)
        ctx = Context(scopes={"fs:read"}, sandbox=_policy(ctx_root))
        res = _run(ex.call("fs.read", {"path": "note.txt"}, ctx))
        assert res.ok, res.error and res.error.message
        assert res.content == "inside-ctx"
        # 没有 ctx.sandbox 时仍走全局（未开沙箱 → 相对路径原样返回）
        assert resolve_path("note.txt", mode="read") == Path("note.txt")


def test_context_sandbox_blocks_outside_path():
    ctx_root, outside, other = _tmp_dir(), _tmp_dir(), _tmp_dir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")

    with _sandbox(enabled=True, root=other, write_paths=(), deny_paths=()):
        ex = Executor(default_registry)
        ctx = Context(scopes={"fs:read"}, sandbox=_policy(ctx_root))
        res = _run(ex.call("fs.read", {"path": str(outside / "secret.txt")}, ctx))
        assert not res.ok
        assert res.error.type == ErrorType.SANDBOX_DENIED


def test_context_sandbox_isolated_across_concurrent_calls():
    """`asyncio.gather` 并发执行时，两个不同工作区的 ctx 不能互相串味。"""
    root_a, root_b = _tmp_dir(), _tmp_dir()
    (root_a / "who.txt").write_text("A", encoding="utf-8")
    (root_b / "who.txt").write_text("B", encoding="utf-8")

    async def both():
        ex = Executor(default_registry)
        calls = [
            ex.call("fs.read", {"path": "who.txt"}, Context(scopes={"fs:read"}, sandbox=p))
            for p in (_policy(root_a), _policy(root_b))
        ]
        return await asyncio.gather(*calls)

    with _sandbox(enabled=False):
        res_a, res_b = _run(both())
    assert res_a.ok and res_a.content == "A"
    assert res_b.ok and res_b.content == "B"


def test_derive_child_carries_sandbox():
    policy = _policy(_tmp_dir())
    parent = Context(scopes={"fs:read", "fs:write"}, sandbox=policy)
    child = parent.derive_child(scopes={"fs:read"})
    assert child.sandbox is policy  # 透传：否则派生上下文会逃出会话工作区
    assert child.scopes == {"fs:read"}


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
