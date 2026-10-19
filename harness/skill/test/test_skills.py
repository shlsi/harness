"""Skills 系统测试。

运行：python -m harness.skill.test.test_skills   （或 pytest）
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import shutil
import tempfile
from pathlib import Path

from harness.skill import (
    SkillNotFound,
    SkillParseError,
    SkillRegistry,
    parse_skill_md,
    register_skill_tools,
)
from harness.skill.lint import ERROR, WARNING, lint_skills
from harness.skill.lint import main as lint_main
from harness.skill.schema import SkillDisabled, SkillError, SkillNotFound
from harness.skill.writer import delete_skill, lint_draft, read_skill, write_skill
from harness.tool import Context, Executor, ToolRegistry

GOOD_SKILL = """---
name: {name}
description: {desc}
allowed-tools: [fs.read, fs.write]
version: 1.0.0
---

# {name} 流程

1. 第一步
2. 第二步
"""


def _write_skill(root: Path, name: str, desc: str = "示例技能", body_extra: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        GOOD_SKILL.format(name=name, desc=desc) + body_extra, encoding="utf-8"
    )
    return directory


def _make_root() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()


def _run(coro):
    return asyncio.run(coro)


# ---- M1：解析与扫描 ----
def test_parse_skill_md():
    with _make_root() as d:
        directory = _write_skill(Path(d), "demo-skill", "演示技能")
        raw, body = parse_skill_md(directory / "SKILL.md")
        assert raw["name"] == "demo-skill"
        assert raw["version"] == "1.0.0"
        assert body.startswith("# demo-skill 流程")
        assert "---" not in body.splitlines()[0]


def test_scan_lists_valid_skills():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill")
        _write_skill(root, "beta-skill")
        registry = SkillRegistry(root)
        assert registry.names() == ["alpha-skill", "beta-skill"]
        assert registry.get("alpha-skill").allowed_tools == ("fs.read", "fs.write")


def test_scan_skips_broken_without_failing():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "ok-skill")
        # 缺 SKILL.md
        (root / "no-entry").mkdir()
        # 缺 description
        bad = root / "bad-front"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: bad-front\n---\n正文", encoding="utf-8")

        registry = SkillRegistry(root)
        assert registry.names() == ["ok-skill"]
        assert len(registry.warnings) == 2


def test_name_must_match_directory():
    with _make_root() as d:
        directory = Path(d) / "dir-name"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            "---\nname: other-name\ndescription: x\n---\n正文", encoding="utf-8"
        )
        registry = SkillRegistry(Path(d))
        assert registry.names() == []
        assert "不一致" in registry.warnings[0]


def test_missing_frontmatter_raises():
    with _make_root() as d:
        path = Path(d) / "SKILL.md"
        path.write_text("# 没有 frontmatter", encoding="utf-8")
        try:
            parse_skill_md(path)
        except SkillParseError as exc:
            assert "frontmatter" in str(exc)
        else:
            raise AssertionError("缺少 frontmatter 时应报错")


def test_utf8_bom_is_tolerated():
    """Windows 编辑器可能写入 BOM；带 BOM 的 SKILL.md 必须照样能加载。"""
    with _make_root() as d:
        directory = Path(d) / "alpha-skill"
        directory.mkdir()
        text = GOOD_SKILL.format(name="alpha-skill", desc=LONG_DESC)
        (directory / "SKILL.md").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

        registry = SkillRegistry(Path(d))
        assert registry.names() == ["alpha-skill"]
        assert registry.warnings == []
        assert "第一步" in registry.load("alpha-skill").body
        assert lint_skills(Path(d)) == []


# ---- M2：目录 / 加载 / 资源 ----
def test_catalog_and_truncation():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill", "很长的描述" * 100)
        registry = SkillRegistry(root, max_desc_chars=10)
        catalog = registry.catalog()
        assert catalog.startswith("- alpha-skill: ")
        assert catalog.endswith("…")


def test_load_returns_body():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill")
        registry = SkillRegistry(Path(d))
        skill = registry.load("alpha-skill")
        assert skill.meta.name == "alpha-skill"
        assert "第一步" in skill.body
        assert "description" not in skill.body  # frontmatter 已被剥离


def test_get_unknown_raises():
    with _make_root() as d:
        registry = SkillRegistry(Path(d))
        try:
            registry.get("nope")
        except SkillNotFound as exc:
            assert exc.name == "nope"
        else:
            raise AssertionError("未知 skill 应抛 SkillNotFound")


def test_read_resource_ok():
    with _make_root() as d:
        directory = _write_skill(Path(d), "alpha-skill")
        (directory / "reference.md").write_text("参考内容", encoding="utf-8")
        registry = SkillRegistry(Path(d))
        assert registry.read_resource("alpha-skill", "reference.md") == "参考内容"


def test_read_resource_blocks_traversal():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill")
        (root / "secret.txt").write_text("机密", encoding="utf-8")
        registry = SkillRegistry(root)
        for bad in ("../secret.txt", "..\\secret.txt", "sub/../../secret.txt"):
            try:
                registry.read_resource("alpha-skill", bad)
            except Exception as exc:  # noqa: BLE001
                assert "越界" in str(exc) or "不存在" in str(exc)
            else:
                raise AssertionError(f"越界读取应被拒绝: {bad}")


def test_read_resource_missing_file():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill")
        registry = SkillRegistry(Path(d))
        try:
            registry.read_resource("alpha-skill", "nope.md")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("不存在的资源应报错")


# ---- M4：声明校验 ----
def test_validate_allowed_tools():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill")
        registry = SkillRegistry(Path(d))
        issues = registry.validate_allowed_tools(["fs.read"])
        assert len(issues) == 1 and "fs.write" in issues[0]
        assert registry.validate_allowed_tools(["fs.read", "fs.write"]) == []


# ---- M3：工具暴露 ----
def _setup_tools():
    root_dir = tempfile.TemporaryDirectory()
    directory = _write_skill(Path(root_dir.name), "alpha-skill")
    (directory / "reference.md").write_text("参考内容", encoding="utf-8")
    skills = SkillRegistry(root_dir.name)
    tool_registry = ToolRegistry()
    register_skill_tools(tool_registry, skills)
    return root_dir, tool_registry


def test_skill_tools_registered():
    root_dir, tool_registry = _setup_tools()
    try:
        assert set(tool_registry.names()) == {"skill.load", "skill.read"}
        defs = tool_registry.to_openai_tools()
        load = next(d for d in defs if d["function"]["name"] == "skill.load")
        assert load["function"]["parameters"]["required"] == ["name"]
    finally:
        root_dir.cleanup()


def test_skill_load_via_executor():
    root_dir, tool_registry = _setup_tools()
    try:
        executor = Executor(tool_registry)
        result = _run(executor.call("skill.load", {"name": "alpha-skill"}, Context()))
        assert result.ok and "第一步" in result.content
    finally:
        root_dir.cleanup()


def test_skill_read_via_executor_and_unknown_skill():
    root_dir, tool_registry = _setup_tools()
    try:
        executor = Executor(tool_registry)
        ok = _run(
            executor.call(
                "skill.read", {"name": "alpha-skill", "path": "reference.md"}, Context()
            )
        )
        assert ok.ok and ok.content == "参考内容"

        bad = _run(executor.call("skill.load", {"name": "nope"}, Context()))
        assert not bad.ok and "不存在" in bad.error.message
    finally:
        root_dir.cleanup()


# ---- M5：reload（热重载）----
def test_reload_picks_up_added_and_removed_skills():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill")
        registry = SkillRegistry(root)
        assert registry.names() == ["alpha-skill"]

        _write_skill(root, "beta-skill")
        assert registry.names() == ["alpha-skill"]  # 未 reload，看不到
        assert len(registry.reload()) == 2
        assert registry.names() == ["alpha-skill", "beta-skill"]

        shutil.rmtree(root / "alpha-skill")
        registry.reload()
        assert registry.names() == ["beta-skill"]


def test_reload_clears_body_cache():
    with _make_root() as d:
        root = Path(d)
        directory = _write_skill(root, "alpha-skill")
        registry = SkillRegistry(root)
        assert "第一步" in registry.load("alpha-skill").body

        (directory / "SKILL.md").write_text(
            GOOD_SKILL.format(name="alpha-skill", desc="示例技能") + "\n改写过的正文\n",
            encoding="utf-8",
        )
        assert "第一步" in registry.load("alpha-skill").body  # 命中旧缓存
        registry.reload()
        assert "改写过的正文" in registry.load("alpha-skill").body


def test_reload_resets_warnings():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill")
        (root / "broken").mkdir()  # 缺 SKILL.md
        registry = SkillRegistry(root)
        assert len(registry.warnings) == 1

        shutil.rmtree(root / "broken")
        registry.reload()
        assert registry.warnings == []


def test_missing_root_warns_without_raising():
    with _make_root() as d:
        registry = SkillRegistry(Path(d) / "nope")
        assert registry.names() == []
        assert "不存在" in registry.warnings[0]
        assert registry.reload() == []


# ---- M5：skill lint ----
LONG_DESC = "当用户给出招标文件并要求解标时使用；从文件提取评审要点并输出带行号溯源的报告"


def test_lint_clean_skill_reports_nothing():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill", LONG_DESC)
        assert lint_skills(Path(d)) == []


def test_lint_missing_root_is_error():
    with _make_root() as d:
        issues = lint_skills(Path(d) / "nope")
        assert [i.code for i in issues] == ["missing-root"]
        assert issues[0].level == ERROR


def test_lint_missing_entry_and_parse_error():
    with _make_root() as d:
        root = Path(d)
        (root / "no-entry").mkdir()
        bad = root / "bad-front"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: bad-front\n---\n正文", encoding="utf-8")
        issues = {i.code for i in lint_skills(root)}
        assert issues == {"missing-entry", "parse-error"}


def test_lint_empty_body_is_error():
    with _make_root() as d:
        root = Path(d)
        directory = root / "alpha-skill"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: alpha-skill\ndescription: {LONG_DESC}\n---\n\n", encoding="utf-8"
        )
        issues = lint_skills(root)
        assert [i.code for i in issues] == ["empty-body"]
        assert issues[0].level == ERROR


def test_lint_unknown_field_is_warning():
    with _make_root() as d:
        root = Path(d)
        directory = root / "alpha-skill"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: alpha-skill\ndescription: {LONG_DESC}\nverison: 1.0.0\n---\n正文\n",
            encoding="utf-8",
        )
        issues = lint_skills(root)
        assert [i.code for i in issues] == ["unknown-field"]
        assert issues[0].level == WARNING and "verison" in issues[0].message


def test_lint_body_and_description_lengths():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill", LONG_DESC)
        assert [i.code for i in lint_skills(Path(d), max_body_chars=5)] == ["body-too-long"]
        assert [i.code for i in lint_skills(Path(d), max_desc_chars=10)] == [
            "description-truncated"
        ]
        assert [i.code for i in lint_skills(Path(d), max_desc_chars=10_000)] == []


def test_lint_short_description_is_warning():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill", "太短")
        issues = lint_skills(Path(d))
        assert [i.code for i in issues] == ["description-too-short"]
        assert issues[0].level == WARNING


def test_lint_unknown_tool_only_checked_when_tools_given():
    with _make_root() as d:
        _write_skill(Path(d), "alpha-skill", LONG_DESC)  # 声明 fs.read / fs.write
        assert lint_skills(Path(d)) == []  # 不给 known_tools 就不查
        issues = lint_skills(Path(d), known_tools=["fs.read"])
        assert [i.code for i in issues] == ["unknown-tool"]
        assert "fs.write" in issues[0].message
        assert lint_skills(Path(d), known_tools=["fs.read", "fs.write"]) == []


def test_lint_issues_are_sorted_and_printable():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "beta-skill", "太短")
        _write_skill(root, "alpha-skill", "太短")
        issues = lint_skills(root)
        assert [i.skill for i in issues] == ["alpha-skill", "beta-skill"]
        assert str(issues[0]).startswith("[warning] alpha-skill:")


def test_lint_main_exit_codes():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill", LONG_DESC)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert lint_main([str(root)]) == 0
        assert "没有发现问题" in out.getvalue()

        _write_skill(root, "beta-skill", "太短")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert lint_main([str(root)]) == 1
        assert "1 个 warning" in out.getvalue()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert lint_main([str(root / "nope")]) == 1
        assert "missing-root" in out.getvalue()


# ---- 写操作（writer：新建 / 覆盖 / 删除 / 草稿校验）----
def test_writer_create_read_update_delete_roundtrip():
    with _make_root() as d:
        root = Path(d)
        entry = write_skill(
            root, name="demo-skill", description="演示技能说明文本",
            body="## 步骤\n\n1. 做事", allowed_tools=["fs.read"], version="1.0",
        )
        assert entry.is_file() and entry.name == "SKILL.md"

        assert SkillRegistry(root).names() == ["demo-skill"]  # 写出的文件能被正常加载
        meta, body = read_skill(root, "demo-skill")
        assert meta.allowed_tools == ("fs.read",) and meta.version == "1.0"
        assert "步骤" in body

        try:  # 重名拒绝
            write_skill(root, name="demo-skill", description="另一段描述文本内容")
        except SkillError as exc:
            assert "已存在" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("重名应报错")

        write_skill(  # 覆盖 + 备份
            root, name="demo-skill", description="改过的描述文本内容",
            body="新正文", overwrite=True,
        )
        assert (root / "demo-skill" / "SKILL.md.bak").is_file()
        assert "新正文" in read_skill(root, "demo-skill")[1]

        delete_skill(root, "demo-skill")
        assert not (root / "demo-skill").exists()
        try:
            read_skill(root, "demo-skill")
        except SkillNotFound:
            pass
        else:  # pragma: no cover
            raise AssertionError("删除后读取应抛 SkillNotFound")


def test_writer_rejects_bad_names_and_traversal():
    with _make_root() as d:
        root = Path(d)
        for bad in ("Bad_Name", "../evil", "", "a/b"):
            try:
                write_skill(root, name=bad, description="描述文本足够长了")
            except SkillError:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"非法名应报错: {bad!r}")
        assert not (root.parent / "evil").exists()  # 没有在 root 外落文件


def test_lint_draft_does_not_touch_disk():
    with _make_root() as d:
        root = Path(d)
        issues = lint_draft(name="draft", description="太短", body="正文")
        assert any(i.code == "description-too-short" for i in issues)
        assert list(root.iterdir()) == []  # 草稿校验不落盘


# ---- 停用（enabled / disabled）----
def test_disabled_skill_hidden_from_names_and_catalog():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill", LONG_DESC)
        _write_skill(root, "beta-skill", LONG_DESC)
        registry = SkillRegistry(root, disabled=["alpha-skill"])

        assert registry.names() == ["beta-skill"]
        assert registry.all_names() == ["alpha-skill", "beta-skill"]
        assert registry.disabled_names() == ["alpha-skill"]
        assert not registry.is_enabled("alpha-skill") and registry.is_enabled("beta-skill")
        assert "alpha-skill" not in registry.catalog()
        assert "beta-skill" in registry.catalog()
        # 显式传 name 也不能绕过开关
        assert registry.catalog(["alpha-skill"]) == ""


def test_disabled_skill_load_and_read_resource_raise():
    with _make_root() as d:
        root = Path(d)
        directory = _write_skill(root, "alpha-skill", LONG_DESC)
        (directory / "reference.md").write_text("参考内容", encoding="utf-8")
        registry = SkillRegistry(root, disabled=["alpha-skill"])

        for call in (lambda: registry.load("alpha-skill"),
                     lambda: registry.read_resource("alpha-skill", "reference.md")):
            try:
                call()
            except SkillDisabled as exc:
                assert "已停用" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("停用的 skill 应抛 SkillDisabled")


def test_list_meta_includes_disabled_and_get_still_works():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill", LONG_DESC)
        registry = SkillRegistry(root, disabled=["alpha-skill"])

        assert [m.name for m in registry.list_meta()] == []
        assert [m.name for m in registry.list_meta(include_disabled=True)] == ["alpha-skill"]
        # 管理端点保存后还要取元信息 → get() 不能被停用挡住
        assert registry.get("alpha-skill").name == "alpha-skill"


def test_set_disabled_hot_switch_and_survives_reload():
    with _make_root() as d:
        root = Path(d)
        _write_skill(root, "alpha-skill", LONG_DESC)
        registry = SkillRegistry(root)
        assert registry.names() == ["alpha-skill"]

        registry.set_disabled(["alpha-skill"])
        assert registry.names() == [] and registry.disabled_names() == ["alpha-skill"]

        registry.reload()  # 重扫不该把停用名单重置掉
        assert registry.names() == []

        registry.set_disabled([])
        assert registry.names() == ["alpha-skill"]


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
