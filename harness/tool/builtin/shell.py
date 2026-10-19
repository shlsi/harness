"""Shell 工具：白名单 + 禁止模式 + 人工审批。

白名单与禁止模式可被配置覆盖（`configure_shell_policy`），默认值即下面的常量。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from ..registry import tool
from ..sandbox import check_command, current_sandbox, resolve_path

# 默认允许执行的命令（可执行文件名，不含路径与扩展名）。
DEFAULT_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        # Python 生态
        "python", "python3", "py", "pip", "pip3", "pytest", "ruff", "mypy", "black", "uv", "poetry",
        # Node 生态
        "node", "npm", "npx", "pnpm", "yarn",
        # 其他语言/构建
        "go", "cargo", "make", "gcc", "clang", "dotnet",
        # 版本控制与常见只读命令
        "git", "gh",
        "echo", "cat", "type", "dir", "ls", "pwd", "cd", "where", "which",
    }
)

# 默认明确禁止的危险模式（即使程序在白名单内也拦截）。
DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    r"\brm\s+-\w*r\w*f",
    r"\bdel\s+/[sfq]",
    r"\brd\s+/s",
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\breg\s+delete\b",
    r"\btaskkill\b",
    r":\(\)\s*\{",
    r"\b(chmod|chown)\s+-R\s+/",
    r">\s*/dev/sd",
    r"\bcurl\b[^|]*\|\s*(ba)?sh",
    r"\bwget\b[^|]*\|\s*(ba)?sh",
)


@dataclass(frozen=True)
class ShellPolicy:
    allowed_commands: frozenset[str]
    deny_patterns: tuple[str, ...]


# 进程级策略（见 docs/configuration-system-design.md §7.2）
_policy = ShellPolicy(DEFAULT_ALLOWED_COMMANDS, DEFAULT_DENY_PATTERNS)


def configure_shell_policy(
    *,
    allowed_commands: list[str] | tuple[str, ...] | None = None,
    deny_patterns: list[str] | tuple[str, ...] | None = None,
) -> ShellPolicy:
    """覆盖本进程的 shell 白名单 / 禁止模式，返回覆盖前的策略。

    传 `None` 或空序列表示沿用旧值。**这是进程级全局状态**：同一进程内所有
    Agent 共用一份策略；沙箱层落地后应迁到那里。
    """
    global _policy
    previous = _policy
    _policy = ShellPolicy(
        frozenset(allowed_commands) if allowed_commands else previous.allowed_commands,
        tuple(deny_patterns) if deny_patterns else previous.deny_patterns,
    )
    return previous


def current_shell_policy() -> ShellPolicy:
    return _policy


def _program(command: str) -> str:
    """提取命令的首个可执行文件名（去掉路径与 .exe）。"""
    token = command.strip().split()[0].strip("\"'")
    name = os.path.basename(token).lower()
    return name[:-4] if name.endswith(".exe") else name


def _guard(command: str) -> None:
    """白名单 + 禁止模式校验，不通过则抛出 ValueError。"""
    if not command.strip():
        raise ValueError("命令不能为空")
    program = _program(command)
    if program not in _policy.allowed_commands:
        raise ValueError(
            f"命令 '{program}' 不在白名单内；允许: {', '.join(sorted(_policy.allowed_commands))}"
        )
    for pattern in _policy.deny_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            raise ValueError(f"命令命中禁止模式，已拦截: {pattern}")


def _decode(stream: bytes) -> str:
    return stream.decode("utf-8", errors="replace")


@tool(
    name="shell.run",
    description=(
        "在 shell 中执行白名单内的命令（如 git / pytest / npm）。"
        "属于危险操作，执行前需要人工确认；命令与参数须为单个字符串。"
    ),
    side_effect="destructive",
    scopes=["shell:run"],
    requires_approval=True,
    timeout_s=120.0,
    result_max_chars=20000,
    tags=["shell"],
)
async def run_command(command: str, cwd: str | None = None) -> str:
    """执行命令。

    Args:
        command: 完整命令行，如 'git status'。
        cwd: 工作目录；沙箱开启时默认工作区根，否则默认当前目录。
    """
    _guard(command)
    sandbox = current_sandbox()
    if sandbox.enabled:
        # cwd 收敛到工作区内；命令串里像路径的 token 也逐个校验（fail-closed）
        workdir = resolve_path(cwd, mode="read") if cwd else sandbox.root
        check_command(command, workdir)
        run_cwd: str | None = str(workdir)
    else:
        run_cwd = cwd
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=run_cwd,
    )
    stdout, stderr = await proc.communicate()
    return (
        f"exit={proc.returncode}\n"
        f"--- stdout ---\n{_decode(stdout)}\n"
        f"--- stderr ---\n{_decode(stderr)}"
    )
