"""配置系统测试（全离线：临时 YAML + 显式 env，不读真实环境变量与 .env）。

运行：python -m harness.config.test.test_config   （或 pytest）
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harness.config import (
    ConfigError,
    build_mcp_configs,
    build_memory,
    build_net_policy,
    build_prompt,
    build_provider,
    build_registry,
    build_sandbox,
    build_session,
    build_shell_policy,
    build_skills,
    build_tracer,
    is_tool_allowed,
    load_config,
    load_env_file,
    read_spans,
    read_tree,
)
from harness.config import OtelTracerSink
from harness.session import LLMCompressor
from harness.tool import (
    SandboxViolation,
    Tracer,
    build_tree,
    check_url,
    configure_sandbox,
    current_sandbox,
    resolve_path,
)
from harness.tool.builtin import net as net_mod
from harness.tool.builtin import shell


def _write_yaml(text: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return handle.name


def _tmp_path(name: str) -> str:
    return (Path(tempfile.mkdtemp()) / name).as_posix()


# ---- M1：加载 / 优先级 / 插值 / 校验 ----
def test_defaults_when_file_absent():
    config = load_config("definitely-not-here.yaml", env={})
    assert config.llm.model == "gpt-4o-mini"
    assert config.agent.max_steps == 12
    assert config.agent.stream is False
    assert config.server.port == 8000
    assert config.mcp.servers == []
    assert config.tools.enabled == []


def test_yaml_overrides_defaults_with_type_coercion():
    path = _write_yaml(
        "llm:\n  model: m1\nserver:\n  port: 9000\n  heartbeat_s: 20\n"
        "agent:\n  max_steps: '8'\n"
    )
    config = load_config(path, env={})
    assert config.llm.model == "m1"
    assert config.server.port == 9000
    assert config.server.heartbeat_s == 20.0  # int 宽松转 float
    assert config.agent.max_steps == 8  # 字符串宽松转 int


def test_env_var_overrides_yaml():
    path = _write_yaml("llm:\n  model: from-yaml\n  api_key: ${OPENAI_API_KEY}\n")
    config = load_config(path, env={"HARNESS_MODEL": "from-env", "OPENAI_API_KEY": "sk-x"})
    assert config.llm.model == "from-env"  # 环境变量 > YAML
    assert config.llm.api_key == "sk-x"


def test_interpolation_keeps_characters_that_would_break_yaml():
    # 值里有 # 与 : —— 若把变量拼进 YAML 文本，会被当成注释/映射；解析后插值才安全
    path = _write_yaml("llm:\n  api_key: ${KEY}\n")
    config = load_config(path, env={"KEY": "abc#not-comment:still-value"})
    assert config.llm.api_key == "abc#not-comment:still-value"


def test_interpolation_default_value():
    path = _write_yaml("observability:\n  tracer_path: ${TRACE_PATH:-data/f.jsonl}\n")
    assert load_config(path, env={}).observability.tracer_path == "data/f.jsonl"
    assert load_config(path, env={"TRACE_PATH": "/tmp/x"}).observability.tracer_path == "/tmp/x"


def test_missing_env_var_raises():
    path = _write_yaml("llm:\n  api_key: ${MISSING_KEY}\n")
    try:
        load_config(path, env={})
    except ConfigError as exc:
        assert "MISSING_KEY" in exc.message and exc.path == "llm.api_key"
    else:  # pragma: no cover
        raise AssertionError("缺失环境变量应当报错")


def test_unknown_field_raises():
    path = _write_yaml("llm:\n  max_step: 3\n")  # 少写一个 s
    try:
        load_config(path, env={})
    except ConfigError as exc:
        assert "max_step" in exc.message and exc.path == "llm"
    else:  # pragma: no cover
        raise AssertionError("未知字段应当报错")


def test_unknown_section_raises():
    path = _write_yaml("unknown_section:\n  a: 1\n")
    try:
        load_config(path, env={})
    except ConfigError as exc:
        assert "unknown_section" in exc.message
    else:  # pragma: no cover
        raise AssertionError("未知配置节应当报错")


def test_type_error_reports_path():
    path = _write_yaml("llm:\n  timeout_s: 60s\n")
    try:
        load_config(path, env={})
    except ConfigError as exc:
        assert exc.path == "llm.timeout_s" and "期望数字" in exc.message
    else:  # pragma: no cover
        raise AssertionError("类型不匹配应当报错")


def test_empty_env_value_is_honoured():
    path = _write_yaml("skills:\n  root: skills\n")
    config = load_config(path, env={"HARNESS_SKILLS_DIR": ""})
    assert config.skills.root == ""  # 显式置空 → 关闭 skill catalog
    assert build_skills(config) is None


def test_dotenv_file_is_loaded(tmp: str = ""):
    env_file = Path(tempfile.mkdtemp()) / ".env"
    env_file.write_text('# 注释\nOPENAI_API_KEY="sk-from-dotenv"\n\nBLANK=\n', encoding="utf-8")
    values = load_env_file(env_file)
    assert values == {"OPENAI_API_KEY": "sk-from-dotenv", "BLANK": ""}
    assert load_env_file(Path(tempfile.mkdtemp()) / "absent.env") == {}


# ---- M2：装配与工具开关 ----
def test_is_tool_allowed_glob_and_precedence():
    assert is_tool_allowed("fs.read", [], []) is True
    assert is_tool_allowed("fs.read", ["fs.*"], []) is True
    assert is_tool_allowed("shell.run", ["fs.*"], []) is False
    assert is_tool_allowed("fs.write", ["fs.*"], ["fs.write"]) is False  # disabled 优先
    assert is_tool_allowed("fs.write", [], ["fs.*"]) is False


def test_build_registry_whitelist_only():
    config = load_config(_write_yaml('tools:\n  enabled: ["fs.*"]\n'), env={})
    names = build_registry(config).names()
    assert names == [
        "fs.copy",
        "fs.delete",
        "fs.edit",
        "fs.glob",
        "fs.grep",
        "fs.list",
        "fs.mkdir",
        "fs.move",
        "fs.read",
        "fs.write",
    ]
    assert "shell.run" not in names and "net.fetch" not in names


def test_build_registry_readonly_mode():
    config = load_config(
        _write_yaml('tools:\n  disabled: ["fs.write", "fs.edit", "shell.run"]\n'), env={}
    )
    names = build_registry(config).names()
    assert "fs.read" in names and "ask_user" in names
    assert "fs.write" not in names and "fs.edit" not in names and "shell.run" not in names


def test_build_registry_is_isolated_from_default_registry():
    config = load_config(_write_yaml('tools:\n  enabled: ["fs.read"]\n'), env={})
    registry = build_registry(config)
    assert registry.names() == ["fs.read"]  # 独立表，不污染 default_registry


# ---- M3：shell 白名单可配 ----
def test_shell_policy_from_config():
    path = _write_yaml(
        "tools:\n  shell:\n    allowed_commands: ['echo', 'git']\n    deny_patterns: ['\\bfoo\\b']\n"
    )
    config = load_config(path, env={})
    previous = build_shell_policy(config)
    try:
        policy = shell.current_shell_policy()
        assert policy.allowed_commands == frozenset({"echo", "git"})
        assert policy.deny_patterns == ("\\bfoo\\b",)
    finally:
        # 还原进程级策略，避免影响其它测试
        shell.configure_shell_policy(
            allowed_commands=sorted(previous.allowed_commands),
            deny_patterns=list(previous.deny_patterns),
        )
    assert shell.current_shell_policy().allowed_commands == shell.DEFAULT_ALLOWED_COMMANDS


def test_shell_policy_empty_keeps_defaults():
    config = load_config(_write_yaml("tools:\n  shell:\n    allowed_commands: []\n"), env={})
    build_shell_policy(config)
    assert shell.current_shell_policy().allowed_commands == shell.DEFAULT_ALLOWED_COMMANDS


# ---- 沙箱（tools.sandbox）----
def _restore_sandbox(previous) -> None:
    configure_sandbox(
        enabled=previous.enabled,
        root=previous.root,
        write_paths=previous.write_paths,
        deny_paths=previous.deny_paths,
        readonly_mounts=previous.readonly_mounts,
        writable_mounts=previous.writable_mounts,
        extra_denied_domains=previous.extra_denied_domains,
    )


def test_sandbox_config_defaults_are_off():
    config = load_config("definitely-not-here.yaml", env={})
    sandbox = config.tools.sandbox
    assert sandbox.enabled is False
    assert sandbox.root == "."
    assert sandbox.write_paths == [] and sandbox.deny_paths == []
    assert sandbox.readonly_mounts == [] and sandbox.writable_mounts == []


def test_build_sandbox_applies_policy_then_restores():
    root, mount = tempfile.mkdtemp(), tempfile.mkdtemp()
    path = _write_yaml(
        "tools:\n"
        "  sandbox:\n"
        "    enabled: true\n"
        f"    root: '{Path(root).as_posix()}'\n"
        "    write_paths: ['src/**']\n"
        "    deny_paths: ['.env']\n"
        f"    readonly_mounts: ['{Path(mount).as_posix()}']\n"
    )
    config = load_config(path, env={})
    previous = build_sandbox(config)
    try:
        policy = current_sandbox()
        assert policy.enabled is True
        assert policy.root == Path(root).resolve()
        assert policy.write_paths == ("src/**",) and policy.deny_paths == (".env",)
        assert policy.readonly_mounts == (Path(mount).resolve(),)

        assert resolve_path(Path(root) / "src" / "a.py", mode="write")  # 命中写白名单
        try:
            resolve_path(Path(root) / "docs" / "b.md", mode="write")
            raise AssertionError("未命中 write_paths 的路径不应可写")
        except SandboxViolation:
            pass
        assert resolve_path(Path(mount) / "x.txt", mode="read")  # 只读挂载可读
        try:
            resolve_path(Path(mount) / "x.txt", mode="write")
            raise AssertionError("只读挂载不应可写")
        except SandboxViolation:
            pass
    finally:
        _restore_sandbox(previous)
    assert current_sandbox().enabled is False


def test_sandbox_disabled_build_leaves_paths_untouched():
    config = load_config(_write_yaml("tools:\n  sandbox:\n    enabled: false\n"), env={})
    previous = build_sandbox(config)
    try:
        assert current_sandbox().enabled is False
        assert resolve_path("../anywhere.txt", mode="write") == Path("../anywhere.txt")
    finally:
        _restore_sandbox(previous)


def test_sandbox_denied_domains_are_appended_not_replaced():
    config = load_config(
        _write_yaml("tools:\n  sandbox:\n    denied_domains: ['blocked.example']\n"), env={}
    )
    previous = build_sandbox(config)
    try:
        assert current_sandbox().extra_denied_domains == ("blocked.example",)
        raised = False
        try:
            check_url("https://blocked.example/x")
        except SandboxViolation:
            raised = True
        assert raised
        # 内置 SSRF 基线仍在（配置只能追加）
        for url in ("http://localhost/", "http://169.254.169.254/"):
            try:
                check_url(url)
                raise AssertionError(f"基线不应被配置覆盖: {url}")
            except SandboxViolation:
                pass
    finally:
        _restore_sandbox(previous)
    assert check_url("https://example.com") == "example.com"


# ---- 网络上限（tools.net）----
def test_net_config_defaults():
    config = load_config("definitely-not-here.yaml", env={})
    assert config.tools.net.max_bytes == 200_000
    assert config.tools.net.max_download_bytes == 20_000_000


def test_build_net_policy_applies_then_restores():
    config = load_config(
        _write_yaml("tools:\n  net:\n    max_bytes: 1024\n    max_download_bytes: 2048\n"),
        env={},
    )
    previous = build_net_policy(config)
    try:
        policy = net_mod.current_net_policy()
        assert policy.max_bytes == 1024 and policy.max_download_bytes == 2048
    finally:
        net_mod.configure_net_policy(
            max_bytes=previous.max_bytes, max_download_bytes=previous.max_download_bytes
        )
    assert net_mod.current_net_policy().max_bytes == net_mod.DEFAULT_MAX_BYTES


# ---- M3：MCP 列表 ----
def test_mcp_servers_parsed_and_mapped():
    path = _write_yaml(
        "mcp:\n"
        "  servers:\n"
        "    - name: fs\n"
        "      transport: stdio\n"
        "      command: npx\n"
        "      args: ['-y', 'server-fs']\n"
        "      tool_prefix: mcpfs\n"
        "      tool_overrides:\n"
        "        write_file:\n"
        "          side_effect: destructive\n"
        "          requires_approval: true\n"
        "    - name: docs\n"
        "      transport: http\n"
        "      url: https://example.com/mcp\n"
    )
    config = load_config(path, env={})
    assert [item.name for item in config.mcp.servers] == ["fs", "docs"]

    runtime = build_mcp_configs(config)
    assert runtime[0].command == "npx" and runtime[0].args == ["-y", "server-fs"]
    assert runtime[0].env is None  # 空 dict → None（适配器约定）
    assert runtime[0].tool_overrides["write_file"]["side_effect"] == "destructive"
    assert runtime[1].transport == "http" and runtime[1].url == "https://example.com/mcp"


def test_mcp_enabled_and_skills_disabled_defaults():
    config = load_config(_write_yaml(""), env={})
    assert config.skills.disabled == []
    assert config.mcp.servers == []

    # 声明里不写 enabled → 默认启用；写了 false → 保留声明但不连接
    path = _write_yaml(
        "mcp:\n"
        "  servers:\n"
        "    - name: fs\n"
        "    - name: docs\n"
        "      enabled: false\n"
        "skills:\n"
        "  disabled: [legacy-skill]\n"
    )
    config = load_config(path, env={})
    assert [item.name for item in config.mcp.servers] == ["fs", "docs"]
    assert [item.enabled for item in config.mcp.servers] == [True, False]
    assert config.skills.disabled == ["legacy-skill"]
    # 运行期描述符不带 enabled（它是宿主侧的开关语义）
    assert not hasattr(build_mcp_configs(config)[0], "enabled")


def test_mcp_enabled_must_be_bool():
    path = _write_yaml("mcp:\n  servers:\n    - name: x\n      enabled: maybe\n")
    try:
        load_config(path, env={})
    except ConfigError as exc:
        assert exc.path == "mcp.servers[0].enabled"
    else:  # pragma: no cover
        raise AssertionError("非布尔 enabled 应报错")


def test_dump_raw_keeps_new_switch_keys():
    from harness.config.admin import dump_raw, ordered_config, read_yaml

    path = _tmp_path("cfg.yaml")
    dump_raw(
        {
            "skills": {"disabled": ["b", "a"], "root": "skills"},
            "mcp": {"servers": [{"name": "fs", "enabled": False}]},
        },
        path,
        backup=False,
    )
    raw = read_yaml(path)
    assert raw["skills"]["disabled"] == ["b", "a"]
    assert raw["mcp"]["servers"][0]["enabled"] is False
    # ordered_config 按 dataclass 字段顺序重排：enabled 紧跟 name
    keys = list(ordered_config(raw)["mcp"]["servers"][0].keys())
    assert keys[:2] == ["name", "enabled"]


# ---- M4：观测 sink ----
def test_tracer_jsonl_sink_writes_spans():
    path = _tmp_path("traces.jsonl")
    config = load_config(
        _write_yaml(f"observability:\n  tracer_sink: jsonl\n  tracer_path: {path}\n"), env={}
    )
    tracer = build_tracer(config.observability)
    tracer.record(tool="fs.read", ok=True)
    tracer.record(tool="shell.run", ok=False)

    lines = Path(path).read_text("utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "fs.read"
    assert json.loads(lines[1])["ok"] is False


def test_tracer_none_sink_by_default():
    config = load_config(_write_yaml("observability:\n  tracer_sink: none\n"), env={})
    tracer = build_tracer(config.observability)
    tracer.record(tool="x")
    assert tracer.spans[-1]["tool"] == "x"


def test_unknown_tracer_sink_raises():
    config = load_config(_write_yaml("observability:\n  tracer_sink: bogus\n"), env={})
    try:
        build_tracer(config.observability)
    except ConfigError as exc:
        assert "bogus" in exc.message
    else:  # pragma: no cover
        raise AssertionError("未知 sink 应当报错")


def test_tracer_max_spans_bounds_memory():
    tracer = Tracer(max_spans=2)
    for index in range(5):
        tracer.record(index=index)
    assert len(tracer.spans) == 2
    assert tracer.spans[-1]["index"] == 4


# ---- M5：trace 读侧（离线）----
def _write_spans(path: str, spans: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(span, ensure_ascii=False) + "\n" for span in spans), encoding="utf-8"
    )


def test_read_spans_filters_by_trace_id():
    path = _tmp_path("traces.jsonl")
    _write_spans(
        path,
        [
            {"span_id": "a", "tool": "agent.run", "trace_id": "t1", "ok": True},
            {"span_id": "b", "tool": "fs.read", "trace_id": "t1", "ok": True},
            {"span_id": "c", "tool": "agent.run", "trace_id": "t2", "ok": True},
        ],
    )
    assert [span["span_id"] for span in read_spans(path, trace_id="t1")] == ["a", "b"]
    assert len(read_spans(path)) == 3  # trace_id=None → 全部


def test_read_spans_skips_bad_lines():
    path = _tmp_path("traces.jsonl")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 中间插入半行（崩溃残留）与空行
    target.write_text(
        json.dumps({"span_id": "a", "trace_id": "t1"})
        + "\n{半行\n\n"
        + json.dumps({"span_id": "b", "trace_id": "t1"})
        + "\n",
        encoding="utf-8",
    )
    assert [span["span_id"] for span in read_spans(path, trace_id="t1")] == ["a", "b"]


def test_read_spans_missing_file_returns_empty():
    assert read_spans(_tmp_path("nope.jsonl")) == []
    assert read_tree(_tmp_path("nope.jsonl")) == []


def test_read_tree_matches_memory_tree():
    path = _tmp_path("traces.jsonl")
    spans = [
        {"span_id": "root", "tool": "agent.run", "trace_id": "t1"},
        {"span_id": "child", "tool": "fs.read", "trace_id": "t1", "parent_span_id": "root"},
    ]
    _write_spans(path, spans)

    from_memory = build_tree(spans)
    from_file = read_tree(path, trace_id="t1")
    assert [node["span"]["span_id"] for node in from_file] == ["root"]
    assert [node["span"]["span_id"] for node in from_file[0]["children"]] == ["child"]
    assert from_memory == from_file  # 同一份 build_tree，不漂移


# ---- M6：OTel 桥接（可选依赖，未装则跳过）----
def _require_otel() -> bool:
    try:
        import opentelemetry.sdk.trace  # noqa: F401
    except ImportError:  # pragma: no cover - 取决于是否装了 [otel] extra
        print('  SKIP: 未安装可选依赖 [otel]（pip install -e ".[otel]"）')
        return False
    return True


def test_build_provider_receives_tracer():
    """不接 tracer 的 Provider 不会记 `llm.complete` span（一次 run 就只剩 agent.run）。"""
    tracer = Tracer()
    provider = build_provider(load_config(_write_yaml("llm:\n  model: demo\n"), env={}).llm, tracer)
    assert provider._tracer is tracer


# ---- M6：长期记忆 ----
def test_memory_config_defaults_and_can_be_disabled():
    config = load_config(_write_yaml(""), env={})
    assert config.memory.enabled is True
    assert config.memory.path == "MEMORY.md"
    assert build_memory(config) is not None

    off = load_config(_write_yaml("memory:\n  enabled: false\n"), env={})
    assert build_memory(off) is None

    blank = load_config(_write_yaml('memory:\n  path: "   "\n'), env={})
    assert build_memory(blank) is None


def test_build_registry_registers_memory_tools_and_respects_switch():
    path = _tmp_path("MEMORY.md")
    config = load_config(_write_yaml(f"memory:\n  path: {path}\n"), env={})
    names = build_registry(config, None, build_memory(config)).names()
    assert "memory.save" in names and "memory.forget" in names

    off = load_config(_write_yaml('tools:\n  disabled: ["memory.*"]\n'), env={})
    disabled = build_registry(off, None, build_memory(off)).names()
    assert not any(name.startswith("memory.") for name in disabled)


def test_build_prompt_injects_memory_into_system():
    path = _tmp_path("MEMORY.md")
    config = load_config(_write_yaml(f"memory:\n  path: {path}\n"), env={})
    memory = build_memory(config)
    memory.save("回复用中文")

    system = build_prompt(None, memory).system_prompt()
    assert "## 长期记忆" in system and "回复用中文" in system
    assert build_prompt(None, None).system_prompt() == ""  # 不配记忆 → 与今天一致


def test_build_session_always_uses_llm_compressor():
    """压缩器只有 LLMCompressor 一种：是否抽取记忆由 on_memory 决定，不影响压缩器选择。"""
    memory_path = _tmp_path("MEMORY.md")
    data_dir = _tmp_path("sessions")
    provider = build_provider(load_config(_write_yaml("llm:\n  model: demo\n"), env={}).llm)

    for extra in ("  auto_extract: true\n", ""):
        config = load_config(
            _write_yaml(
                f"memory:\n  path: {memory_path}\n{extra}server:\n  data_dir: {data_dir}\n"
            ),
            env={},
        )
        manager = build_session(config, build_memory(config), None, provider)
        assert isinstance(manager.compressor, LLMCompressor)


def test_build_session_without_provider_raises():
    """没有 provider 就是配置错误——不再静默降级为占位压缩器。"""
    config = load_config(_write_yaml(f"server:\n  data_dir: {_tmp_path('sessions')}\n"), env={})
    try:
        build_session(config)
    except ConfigError as exc:
        assert exc.path == "session" and "provider" in exc.message
    else:
        raise AssertionError("无 provider 时应报配置错误")


def test_build_tracer_wires_otel_sink():
    if not _require_otel():
        return
    config = load_config(_write_yaml("observability:\n  tracer_sink: otel\n"), env={})
    tracer = build_tracer(config.observability)
    assert isinstance(tracer._sink, OtelTracerSink)


def test_otel_sink_exports_span_with_parent_and_times():
    if not _require_otel():
        return
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    sink = OtelTracerSink(service_name="harness-test", exporter=exporter)

    # 故意让子 span 先到（真实情况：agent.run 在 _finish 才记录，一定最后到）
    sink(
        {
            "span_id": "root000000001",
            "ts": 1700000001.5,
            "duration_ms": 100.0,
            "trace_id": "t1",
            "tool": "agent.run",
            "ok": True,
            "stop_reason": "final",
        }
    )
    sink(
        {
            "span_id": "child00000001",
            "ts": 1700000001.2,
            "duration_ms": 20.0,
            "trace_id": "t1",
            "tool": "fs.read",
            "ok": False,
            "parent_span_id": "root000000001",
        }
    )

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"agent.run", "fs.read"}
    root, child = spans["agent.run"], spans["fs.read"]

    # 父子靠 id 确定性派生对上，与到达顺序无关
    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id
    assert child.context.trace_id == root.context.trace_id
    # 起止时间由 ts - duration_ms 反推
    assert root.end_time - root.start_time == 100_000_000  # 100ms → ns
    assert child.end_time - child.start_time == 20_000_000
    # 属性保留，ok=False 标成错误状态
    assert root.attributes["stop_reason"] == "final"
    assert child.status.status_code is StatusCode.ERROR


def test_otel_sink_swallows_broken_span():
    if not _require_otel():
        return
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    sink = OtelTracerSink(exporter=InMemorySpanExporter())
    sink({"tool": "x"})  # 缺 span_id / ts —— 观测失败不该抛



def test_project_harness_yaml_is_valid():
    """仓库自带的 harness.yaml 必须能被解析（防止模板写坏）。"""
    root = Path(__file__).resolve().parents[3]
    path = root / "harness.yaml"
    if not path.is_file():  # pragma: no cover
        return
    config = load_config(path, env={"OPENAI_API_KEY": "sk-placeholder"})
    assert config.llm.base_url and config.llm.model
    assert config.agent.stream is True


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
