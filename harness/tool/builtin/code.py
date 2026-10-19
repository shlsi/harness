"""代码执行工具：`code.run`（**应用层沙箱 + 环境清理**，非 OS 级隔离）。

设计见 docs/builtin-tools-design.md §6。与 `shell.run` 的差别：

- 输入是结构化的 `language` + `code`，执行走 `create_subprocess_exec`（不经 shell，无注入面）。
- 临时脚本与工作目录**总是**落在工作区内的 `data/tmp/`（沙箱开启时受边界约束）。
- 子进程**只继承 allowlist 里的环境变量**——不把 `OPENAI_API_KEY` 之类密钥带进去。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from ..registry import tool
from ..sandbox import resolve_path
from .shell import current_shell_policy

# 语言 → 候选解释器（按顺序取第一个「在 shell 白名单里且系统里找得到」的）
LANGUAGES: dict[str, tuple[str, ...]] = {
    "python": ("python", "python3", "py"),
    "node": ("node",),
    "javascript": ("node",),
}
EXTENSIONS: dict[str, str] = {
    "python": ".py",
    "node": ".js",
    "javascript": ".js",
}

# 子进程环境变量 allowlist（默认不给，可审计；不用靠猜名字的 denylist）
SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "NODE_PATH",
    }
)

# 运行期临时目录（与 data/sessions、data/traces.jsonl 同级）
TMP_DIR = Path("data/tmp")
DEFAULT_TIMEOUT_S = 120.0


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}


def _pick_interpreter(language: str) -> str:
    key = language.strip().lower()
    candidates = LANGUAGES.get(key)
    if not candidates:
        raise ValueError(
            f"不支持的语言 {language!r}；可用: {', '.join(sorted(LANGUAGES))}"
        )
    allowed = current_shell_policy().allowed_commands
    for name in candidates:
        if name in allowed and shutil.which(name):
            return name
    raise ValueError(
        f"语言 {language!r} 没有可用解释器（候选 {list(candidates)}）；"
        "请把解释器加入 tools.shell.allowed_commands"
    )


@tool(
    name="code.run",
    description=(
        "把一段脚本写入临时文件并执行，返回退出码与输出（危险操作，执行前需要人工确认）。"
        "支持 python / node。临时文件与工作目录都在工作区内，且子进程不继承密钥类环境变量；"
        "注意这**不是**操作系统级隔离。"
    ),
    side_effect="destructive",
    scopes=["code:run"],
    requires_approval=True,
    timeout_s=DEFAULT_TIMEOUT_S,
    result_max_chars=20000,
    tags=["code"],
)
async def run_code(language: str, code: str) -> str:
    """执行代码片段。

    Args:
        language: 语言，支持 python / node（javascript 等同 node）。
        code: 完整脚本源码。
    """
    key = language.strip().lower()
    interpreter = _pick_interpreter(key)
    # 沙箱关闭时 resolve_path 原样返回，这里统一取绝对路径给子进程当 cwd
    workdir = resolve_path(TMP_DIR, mode="write").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="code-", dir=str(workdir)))
    try:
        script = run_dir / f"main{EXTENSIONS[key]}"
        script.write_text(code, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            interpreter,
            str(script),
            cwd=str(run_dir),
            env=_clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            f"exit={proc.returncode}\n"
            f"--- stdout ---\n{stdout.decode('utf-8', errors='replace')}\n"
            f"--- stderr ---\n{stderr.decode('utf-8', errors='replace')}"
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


__all__ = ["EXTENSIONS", "LANGUAGES", "SAFE_ENV_KEYS", "TMP_DIR", "run_code"]
