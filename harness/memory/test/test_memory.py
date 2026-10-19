"""记忆系统测试（全离线：只碰临时文件，无网络无模型）。

运行：python -m harness.memory.test.test_memory   （或 pytest）
"""

from __future__ import annotations

import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness.memory import (
    DEFAULT_SECTION,
    MemoryDoc,
    MemoryEntryInvalid,
    MemoryFull,
    MemoryStore,
    parse_doc,
    register_memory_tools,
    render_doc,
    render_for_prompt,
)
from harness.tool import Context, Executor, Policy, ToolRegistry


def _run(coro):
    return asyncio.run(coro)


def _tmp(name: str = "MEMORY.md") -> Path:
    return Path(tempfile.mkdtemp()) / name


# ---- 解析 / 渲染 ----
def test_parse_and_render_roundtrip():
    text = "# 长期记忆\n\n## 用户偏好\n- 回复用中文\n\n## 事实\n- 用 FastAPI\n"
    doc = parse_doc(text)
    assert doc.title == "# 长期记忆"
    assert [section.title for section in doc.sections] == ["用户偏好", "事实"]
    assert doc.entries() == ["回复用中文", "用 FastAPI"]
    assert parse_doc(render_doc(doc)).entries() == doc.entries()


def test_render_for_prompt_drops_title_and_demotes_sections():
    doc = parse_doc("# 长期记忆\n\n## 用户偏好\n- 回复用中文\n")
    rendered = render_for_prompt(doc)
    assert "# 长期记忆" not in rendered
    assert rendered == "### 用户偏好\n- 回复用中文"


def test_render_for_prompt_is_empty_for_empty_doc():
    assert render_for_prompt(MemoryDoc()) == ""


def test_hand_written_content_is_preserved():
    path = _tmp()
    path.write_text(
        "# 我的记忆\n\n## 用户偏好\n- 回复用中文\n\n一句自由说明\n\n## 项目事实\n- Python 3.10+\n",
        encoding="utf-8",
    )
    store = MemoryStore(path)
    assert store.entries() == ["回复用中文", "Python 3.10+"]

    store.save("不要用向量库")
    text = path.read_text("utf-8")
    assert parse_doc(text).title == "# 我的记忆"  # 自定义一级标题保留
    assert "一句自由说明" in text  # 非条目的散行保留
    assert f"## {DEFAULT_SECTION}" in text  # 缺的分节被创建
    assert "不要用向量库" in text


# ---- 写入：去重 / 上限 / 原子写 ----
def test_normalize_collapses_newlines_into_single_entry():
    path = _tmp()
    store = MemoryStore(path)
    store.save("第一行\n第二行   带空白")
    assert store.entries() == ["第一行 第二行 带空白"]


def test_save_is_idempotent_and_moves_entry_to_target_section():
    path = _tmp()
    store = MemoryStore(path)
    store.save("回复用中文", section="项目事实")
    store.save("回复用中文")  # 同一内容、换节
    assert store.entries() == ["回复用中文"]  # 只有一条

    doc = parse_doc(path.read_text("utf-8"))
    assert [section.title for section in doc.sections] == [DEFAULT_SECTION]  # 旧空节被清掉
    assert store.save("回复用中文") == (
        f"已存在，已移到「{DEFAULT_SECTION}」节末尾：回复用中文"
    )


def test_save_rejects_empty_text():
    store = MemoryStore(_tmp())
    try:
        store.save("   \n  ")
    except MemoryEntryInvalid as exc:
        assert "不能为空" in exc.message
    else:  # pragma: no cover
        raise AssertionError("空内容应当报错")


def test_save_rejects_when_over_limit_and_keeps_file_unchanged():
    path = _tmp()
    store = MemoryStore(path, max_chars=60)
    store.save("短记忆")
    before = path.read_text("utf-8")

    try:
        store.save("这是一条很长很长的记忆" * 5)
    except MemoryFull as exc:
        assert "记忆已满" in exc.message
        assert str(path) in str(exc)  # 错误带路径，便于定位
    else:  # pragma: no cover
        raise AssertionError("超过上限应当报错，而不是静默截断")

    assert path.read_text("utf-8") == before  # 拒绝写入时文件不变


def test_replace_whole_file_and_rejects_over_limit():
    path = _tmp()
    store = MemoryStore(path, max_chars=200)
    store.save("旧记忆")

    store.replace("# 长期记忆\n\n## 用户偏好\n- 回复用中文\n- 不用 emoji\n")
    assert store.entries() == ["回复用中文", "不用 emoji"]
    assert path.read_text("utf-8").startswith("# 长期记忆")
    before = path.read_text("utf-8")

    try:
        store.replace("- " + "x" * 500)
    except MemoryFull:
        pass
    else:  # pragma: no cover
        raise AssertionError("整文替换超过上限也应报错")
    assert path.read_text("utf-8") == before  # 失败时文件不变


def test_render_is_byte_stable_and_leaves_no_temp_file():
    path = _tmp()
    store = MemoryStore(path)
    store.save("A")
    first = store.render()
    assert store.render() == first  # 内容没变 → 同一个字符串（前缀缓存友好的前提）

    store.save("B")
    assert store.render() != first
    assert not (path.parent / (path.name + ".tmp")).exists()  # 原子写不留临时文件


def test_concurrent_saves_keep_all_entries():
    path = _tmp()
    store = MemoryStore(path, max_chars=100_000)
    texts = [f"并发记忆 {index}" for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.save, texts))
    assert sorted(store.entries()) == sorted(texts)


# ---- 删除 ----
def test_forget_matches_exact_then_substring():
    path = _tmp()
    store = MemoryStore(path)
    store.save("用户要求回复必须使用中文，不要 emoji")
    store.save("项目用 FastAPI")

    assert "未找到" in store.forget("完全不存在的记忆")
    assert "已删除 1 条" in store.forget("不要 emoji")  # 子串命中
    assert store.entries() == ["项目用 FastAPI"]
    assert "已删除 1 条" in store.forget("项目用 FastAPI")  # 精确命中
    assert store.entries() == []


def test_forget_drops_emptied_section():
    path = _tmp()
    store = MemoryStore(path)
    store.save("只有一条")
    store.forget("只有一条")
    assert f"## {DEFAULT_SECTION}" not in path.read_text("utf-8")


# ---- 空文件 ----
def test_missing_file_is_empty_memory():
    store = MemoryStore(_tmp("nope.md"))
    assert store.entries() == []
    assert store.render() == ""
    store.save("第一次写入会创建文件")
    assert store.entries() == ["第一次写入会创建文件"]


# ---- 工具接线 ----
def test_memory_tools_roundtrip_via_executor():
    async def scenario():
        store = MemoryStore(_tmp())
        registry = ToolRegistry()
        assert register_memory_tools(registry, store) == ["memory.save", "memory.forget"]
        executor = Executor(registry, policy=Policy(enforce_scopes=False))
        ctx = Context()

        saved = await executor.call(
            "memory.save", {"text": "回复用中文", "section": "用户偏好"}, ctx
        )
        assert saved.ok and "已记住" in saved.content
        assert "用户偏好" in store.path.read_text("utf-8")

        forgotten = await executor.call("memory.forget", {"text": "回复用中文"}, ctx)
        assert forgotten.ok and "已删除 1 条" in forgotten.content
        assert store.entries() == []

    _run(scenario())


def test_memory_tools_require_scope_by_default():
    async def scenario():
        registry = ToolRegistry()
        register_memory_tools(registry, MemoryStore(_tmp()))
        executor = Executor(registry)  # 默认 Policy(enforce_scopes=True)

        denied = await executor.call("memory.save", {"text": "x"}, Context())
        assert denied.ok is False
        assert denied.error.type == "permission_denied"
        assert denied.error.details["required"] == ["memory:write"]

        allowed = await executor.call(
            "memory.save", {"text": "x"}, Context(scopes={"memory:write"})
        )
        assert allowed.ok is True

    _run(scenario())


def test_memory_full_surfaces_as_structured_error_to_model():
    async def scenario():
        store = MemoryStore(_tmp(), max_chars=40)
        registry = ToolRegistry()
        register_memory_tools(registry, store)
        executor = Executor(registry, policy=Policy(enforce_scopes=False))
        ctx = Context()

        result = await executor.call("memory.save", {"text": "很长的一条记忆" * 5}, ctx)
        assert result.ok is False
        assert result.error.type == "execution_error"
        assert "记忆已满" in result.error.message  # 模型能看到"满了"，可自行整理

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
