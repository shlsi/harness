"""技能 zip 包安装测试（harness/skill/pack.py）。

运行：python -m harness.skill.test.test_pack   （或 pytest）
"""

from __future__ import annotations

import io
import stat
import tempfile
import zipfile
from pathlib import Path

from harness.skill.pack import InstallLimits, _safe_entry, install_zip
from harness.skill.registry import ENTRY_NAME
from harness.skill.schema import PackError

LONG_DESC = "当用户给出招标文件并要求解标时使用；从文件提取评审要点并输出带行号溯源的报告"
BODY = "## 何时使用\n\n描述触发场景。\n\n## 步骤\n\n1. …\n"


def md(name: str, desc: str = LONG_DESC) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{BODY}"


def make_zip(files: dict[str, str], *, extra: list[tuple[str, bytes, int]] = ()) -> bytes:
    """`extra` 用来构造带自定义 external_attr 的条目（如符号链接）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
        for name, data, attr in extra:
            info = zipfile.ZipInfo(name)
            info.external_attr = attr
            zf.writestr(info, data)
    return buf.getvalue()


def _tmp() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()


def _expect(code: str, fn) -> PackError:
    try:
        fn()
    except PackError as exc:
        assert exc.code == code, f"期望 {code}，实际 {exc.code}: {exc}"
        return exc
    raise AssertionError(f"应抛 PackError({code})")  # pragma: no cover


# ---- 结构识别 ----
def test_install_single_skill_from_root_zip():
    with _tmp() as d:
        root = Path(d) / "skills"
        report = install_zip(
            root,
            make_zip({ENTRY_NAME: md("single-one"), "reference/x.md": "参考"}),
        )
        assert [i.name for i in report.installed] == ["single-one"]
        assert (root / "single-one" / "reference" / "x.md").is_file()
        assert sorted(report.installed[0].files) == ["SKILL.md", "reference/x.md"]
        assert report.installed[0].overwritten is False


def test_install_multi_skill_zip():
    with _tmp() as d:
        root = Path(d) / "skills"
        report = install_zip(
            root, make_zip({"alpha/SKILL.md": md("alpha"), "beta/SKILL.md": md("beta")})
        )
        assert [i.name for i in report.installed] == ["alpha", "beta"]
        assert (root / "alpha" / "SKILL.md").is_file()


def test_zip_ambiguous_and_no_skill():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({ENTRY_NAME: md("x"), "a/SKILL.md": md("a")})
        exc = _expect("zip_ambiguous", lambda: install_zip(root, z))
        assert exc.details["entries"] == ["SKILL.md", "a/SKILL.md"]

        _expect("zip_no_skill", lambda: install_zip(root, make_zip({"readme.md": "无技能"})))


def test_install_creates_root_when_missing():
    with _tmp() as d:
        root = Path(d) / "nested" / "skills"
        report = install_zip(root, make_zip({"alpha/SKILL.md": md("alpha")}))
        assert root.is_dir() and [i.name for i in report.installed] == ["alpha"]


# ---- 冲突与覆盖 ----
def test_conflict_blocks_whole_package():
    with _tmp() as d:
        root = Path(d) / "skills"
        install_zip(root, make_zip({"alpha/SKILL.md": md("alpha")}))
        before = sorted(p.name for p in root.iterdir())

        z = make_zip({"alpha/SKILL.md": md("alpha"), "beta/SKILL.md": md("beta")})
        exc = _expect("skill_conflict", lambda: install_zip(root, z))
        assert exc.details["conflicts"] == ["alpha"]
        # 整包原子：beta 也没落盘
        assert sorted(p.name for p in root.iterdir()) == before
        assert not (root / "beta").exists()


def test_overwrite_backs_up_entry():
    with _tmp() as d:
        root = Path(d) / "skills"
        install_zip(root, make_zip({"alpha/SKILL.md": md("alpha")}))
        old = (root / "alpha" / "SKILL.md").read_text(encoding="utf-8")

        z = make_zip({"alpha/SKILL.md": md("alpha", LONG_DESC + "（改过）")})
        report = install_zip(root, z, overwrite=True)

        assert report.installed[0].overwritten is True
        assert "改过" in (root / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        assert (root / "alpha" / "SKILL.md.bak").read_text(encoding="utf-8") == old


def test_overwrite_keeps_stale_files_with_warning():
    with _tmp() as d:
        root = Path(d) / "skills"
        install_zip(root, make_zip({"alpha/SKILL.md": md("alpha"), "alpha/old.md": "旧资源"}))
        z = make_zip({"alpha/SKILL.md": md("alpha"), "alpha/new.md": "新资源"})
        report = install_zip(root, z, overwrite=True)

        assert (root / "alpha" / "old.md").is_file()  # 不自动删
        assert (root / "alpha" / "new.md").is_file()
        assert any("保留了" in w for w in report.warnings)


# ---- 安全：路径穿越 / 类型 / 限额 ----
def test_rejects_zip_slip_entries():
    with _tmp() as d:
        base = Path(d)
        root = base / "skills"
        cases = {
            "../evil/SKILL.md": "traversal",
            "..\\..\\evil\\SKILL.md": "traversal",
            "/abs/SKILL.md": "absolute",
            "C:\\evil\\SKILL.md": "drive",
            "a:b/SKILL.md": "drive",
        }
        for name, reason in cases.items():
            exc = _expect(
                "zip_unsafe_entry", lambda n=name: install_zip(root, make_zip({n: md("x")}))
            )
            assert exc.details["reason"] == reason
        assert not (base / "evil").exists()
        assert not root.exists()


def test_rejects_deep_paths_and_normalizes_backslashes():
    with _tmp() as d:
        root = Path(d) / "skills"
        deep = "/".join(["a"] * 12) + "/SKILL.md"
        exc = _expect(
            "zip_unsafe_entry", lambda: install_zip(root, make_zip({deep: md("a")}))
        )
        assert exc.details["reason"] == "too_deep"
        # 反斜杠分隔（第三方工具打的包）按 `/` 归一化后再判定，而不是当成单个文件名
        assert _safe_entry("alpha\\beta\\SKILL.md") == ["alpha", "beta", "SKILL.md"]


def test_rejects_symlink_entry():
    with _tmp() as d:
        root = Path(d) / "skills"
        link_attr = (stat.S_IFLNK | 0o777) << 16
        z = make_zip({"alpha/SKILL.md": md("alpha")}, extra=[("alpha/link", b"/etc/passwd", link_attr)])
        exc = _expect("zip_unsafe_entry", lambda: install_zip(root, z))
        assert exc.details["reason"] == "symlink"


def test_rejects_case_collision():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"alpha/SKILL.md": md("alpha"), "Alpha/SKILL.md": md("alpha")})
        exc = _expect("zip_unsafe_entry", lambda: install_zip(root, z))
        assert exc.details["reason"] == "case_conflict"


def test_limits_entry_count_and_total_bytes():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"alpha/SKILL.md": md("alpha"), "alpha/b.md": "x"})
        exc = _expect(
            "zip_too_large", lambda: install_zip(root, z, limits=InstallLimits(max_entries=1))
        )
        assert exc.details["limit"] == "entries"

        exc = _expect(
            "zip_too_large",
            lambda: install_zip(root, z, limits=InstallLimits(max_total_bytes=10)),
        )
        assert exc.details["limit"] == "total_bytes"
        assert not root.exists()


def test_limits_single_file_and_zip_bytes():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"alpha/SKILL.md": md("alpha")})
        exc = _expect(
            "zip_too_large", lambda: install_zip(root, z, limits=InstallLimits(max_zip_bytes=10))
        )
        assert exc.details["limit"] == "zip_bytes"
        exc = _expect(
            "zip_too_large",
            lambda: install_zip(root, z, limits=InstallLimits(max_file_bytes=5)),
        )
        assert exc.details["limit"] == "file_bytes"


def test_limits_max_skills():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"a/SKILL.md": md("a"), "b/SKILL.md": md("b")})
        exc = _expect(
            "zip_too_large", lambda: install_zip(root, z, limits=InstallLimits(max_skills=1))
        )
        assert exc.details["limit"] == "skills"


def test_invalid_zip_is_rejected():
    with _tmp() as d:
        _expect("zip_invalid", lambda: install_zip(Path(d) / "skills", b"not a zip at all"))


# ---- 内容校验 ----
def test_invalid_skill_in_package_writes_nothing():
    with _tmp() as d:
        root = Path(d) / "skills"
        # alpha 合法，beta 的 description 为空 → lint error
        z = make_zip(
            {
                "alpha/SKILL.md": md("alpha"),
                "beta/SKILL.md": "---\nname: beta\ndescription: \"\"\n---\n\n正文\n",
            }
        )
        exc = _expect("skill_invalid", lambda: install_zip(root, z))
        assert exc.details["skills"][0]["name"] == "beta"
        assert exc.details["issues"]
        assert not root.exists()


def test_dir_name_must_match_frontmatter_name():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"alpha2/SKILL.md": md("mismatch")})
        exc = _expect("skill_invalid", lambda: install_zip(root, z))
        assert "目录名" in exc.details["skills"][0]["reason"]
        assert not root.exists()


def test_single_skill_bad_name_rejected():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({ENTRY_NAME: md("Bad_Name")})
        exc = _expect("skill_invalid", lambda: install_zip(root, z))
        assert "name 非法" in exc.message or "name 必须是" in exc.details["skills"][0]["reason"]


def test_empty_body_is_rejected_like_lint_error():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"alpha/SKILL.md": f"---\nname: alpha\ndescription: {LONG_DESC}\n---\n\n"})
        exc = _expect("skill_invalid", lambda: install_zip(root, z))
        assert exc.details["issues"][0]["code"] == "empty-body"


def test_lint_warning_is_reported_not_rejected():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip({"alpha/SKILL.md": md("alpha", "太短")})  # description-too-short 仅 warning
        report = install_zip(root, z)
        assert [i.name for i in report.installed] == ["alpha"]
        assert any("description-too-short" in w for w in report.warnings)


def test_unknown_field_warning_from_package():
    with _tmp() as d:
        root = Path(d) / "skills"
        text = f"---\nname: alpha\ndescription: {LONG_DESC}\nverison: 1.0\n---\n\n{BODY}"
        report = install_zip(root, make_zip({"alpha/SKILL.md": text}))
        assert any("unknown-field" in w for w in report.warnings)


def test_utf8_bom_skill_md_is_accepted():
    with _tmp() as d:
        root = Path(d) / "skills"
        data = ("\ufeff" + md("alpha")).encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("alpha/SKILL.md", data)
        report = install_zip(root, buf.getvalue())
        assert [i.name for i in report.installed] == ["alpha"]


# ---- 噪声条目 ----
def test_ignores_noise_entries():
    with _tmp() as d:
        root = Path(d) / "skills"
        z = make_zip(
            {
                "__MACOSX/._alpha": "junk",
                ".DS_Store": "junk",
                "Thumbs.db": "junk",
                "alpha/SKILL.md": md("alpha"),
                "alpha/SKILL.md.bak": "junk",
                "alpha/__pycache__/x.pyc": "junk",
            }
        )
        report = install_zip(root, z)
        assert [i.name for i in report.installed] == ["alpha"]
        assert sorted(report.skipped) == [
            ".DS_Store",
            "Thumbs.db",
            "__MACOSX/._alpha",
            "alpha/SKILL.md.bak",
            "alpha/__pycache__/x.pyc",
        ]
        assert not (root / "alpha" / ".DS_Store").exists()


def test_installed_skills_are_loadable_by_registry():
    from harness.skill import SkillRegistry

    with _tmp() as d:
        root = Path(d) / "skills"
        install_zip(root, make_zip({"alpha/SKILL.md": md("alpha")}))
        registry = SkillRegistry(root)
        assert registry.names() == ["alpha"]
        assert "步骤" in registry.load("alpha").body


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
