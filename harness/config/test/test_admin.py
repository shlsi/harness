"""配置管理（admin）测试：脱敏 / 还原 / 键序 / 校验 / 回写。全离线。

运行：python -m harness.config.test.test_admin   （或 pytest）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness.config import ConfigError, load_config
from harness.config.admin import (
    SENTINEL,
    backup_path,
    current_env,
    dump_raw,
    env_override_report,
    is_plaintext_secret,
    ordered_config,
    read_yaml,
    redact,
    restore_secrets,
    validate,
)


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp())


# ---- 脱敏 / 还原 ----
def test_redact_keeps_var_and_masks_literal():
    kept = redact({"llm": {"api_key": "${OPENAI_API_KEY}", "model": "m"}})
    assert kept["llm"]["api_key"] == "${OPENAI_API_KEY}"
    assert kept["llm"]["model"] == "m"  # 非敏感字段不动

    masked = redact({"llm": {"api_key": "sk-plain-123"}})
    assert masked["llm"]["api_key"] == SENTINEL

    # 空串/缺省保持原样（没有可泄露的内容）
    assert redact({"llm": {"api_key": ""}})["llm"]["api_key"] == ""
    assert "api_key" not in redact({"llm": {"model": "m"}})["llm"]


def test_redact_leaves_original_untouched():
    data = {"llm": {"api_key": "sk-x"}}
    redact(data)
    assert data["llm"]["api_key"] == "sk-x"  # 深拷贝，不改原对象


def test_restore_secrets_keeps_previous_value():
    old = {"llm": {"api_key": "${OPENAI_API_KEY}"}}
    # 提交占位 / 空 / None 都表示「未改动」→ 还原为旧值
    for incoming in (SENTINEL, "", None):
        restored = restore_secrets({"llm": {"api_key": incoming}}, old)
        assert restored["llm"]["api_key"] == "${OPENAI_API_KEY}"

    # 用户明确改了引用 → 保留新值
    restored = restore_secrets({"llm": {"api_key": "${OTHER_KEY}"}}, old)
    assert restored["llm"]["api_key"] == "${OTHER_KEY}"

    # 旧值也没有 → 不凭空造键
    assert "api_key" not in restore_secrets({"llm": {"api_key": SENTINEL}}, {})["llm"]


def test_is_plaintext_secret():
    assert is_plaintext_secret({"llm": {"api_key": "sk-abc"}}) == "llm.api_key"
    assert is_plaintext_secret({"llm": {"api_key": "${OPENAI_API_KEY}"}}) is None
    assert is_plaintext_secret({"llm": {"api_key": SENTINEL}}) is None
    assert is_plaintext_secret({"llm": {"api_key": ""}}) is None
    assert is_plaintext_secret({"llm": {"model": "m"}}) is None


# ---- 键序 ----
def test_ordered_config_follows_dataclass_field_order():
    ordered = ordered_config({"server": {"port": 9000}, "llm": {"model": "m"}})
    assert list(ordered) == ["llm", "server"]  # 按 HarnessConfig 字段顺序，未出现的省略
    assert list(ordered["llm"]) == ["model"]


def test_ordered_config_reorders_nested_list_items():
    data = {"mcp": {"servers": [{"tool_prefix": "p", "name": "s"}]}}
    ordered = ordered_config(data)
    # McpServerConfig 的首字段是 name → 排到前面
    assert list(ordered["mcp"]["servers"][0])[0] == "name"


# ---- 校验 ----
def test_validate_reports_field_path():
    try:
        validate({"agent": {"max_steps": "abc"}}, env={})
    except ConfigError as exc:
        assert exc.path == "agent.max_steps"
    else:  # pragma: no cover
        raise AssertionError("非法类型应当报错")


def test_validate_accepts_and_coerces():
    config = validate({"agent": {"max_steps": "8"}, "llm": {"model": "m1"}}, env={})
    assert config.agent.max_steps == 8 and config.llm.model == "m1"


# ---- 环境变量报告 ----
def test_env_override_report_masks_secret():
    report = env_override_report({"OPENAI_API_KEY": "sk-secret", "HARNESS_MODEL": "m2"})
    by_env = {item["env"]: item for item in report}
    assert by_env["OPENAI_API_KEY"]["value"] == SENTINEL  # 敏感值脱敏
    assert by_env["OPENAI_API_KEY"]["path"] == "llm.api_key"
    assert by_env["HARNESS_MODEL"]["value"] == "m2"
    assert env_override_report({}) == []


def test_current_env_reads_dotenv_file():
    env_file = _tmp_dir() / ".env"
    env_file.write_text("HARNESS_MODEL=from-file\n", encoding="utf-8")
    # .env 被并入环境层（进程变量会覆盖它，故只断言 .env 的值确实读进来了）
    assert "HARNESS_MODEL" in current_env(env_file)


# ---- 回写 ----
def test_dump_raw_roundtrip_and_backup():
    path = _tmp_dir() / "harness.yaml"
    dump_raw({"llm": {"model": "m1"}, "agent": {"max_steps": 7}}, path)

    assert path.read_text(encoding="utf-8").startswith("#")  # 带「由控制台生成」头
    assert read_yaml(path)["llm"]["model"] == "m1"
    loaded = load_config(path, env={})
    assert loaded.llm.model == "m1" and loaded.agent.max_steps == 7

    # 再次写回 → 生成 .bak，且备份里是上一版
    dump_raw({"llm": {"model": "m2"}}, path)
    assert backup_path(path).is_file()
    assert "m1" in backup_path(path).read_text(encoding="utf-8")
    assert load_config(path, env={}).llm.model == "m2"


def test_dump_raw_is_atomic_and_creates_parent():
    path = _tmp_dir() / "nested" / "dir" / "harness.yaml"
    dump_raw({"llm": {"model": "m"}}, path)
    assert path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()  # 临时文件已被 replace 掉


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
