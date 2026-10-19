"""沙箱层：工作区边界 + 路径权限（收口 `fs.*` / `shell.run` / `code.run` / `net.*`）。

设计见 docs/sandbox-design.md。要点：

- **默认关闭**（`enabled=False`）：此时 `resolve_path()` 原样返回入参，行为与引入沙箱前一致。
- 开启后相对路径基于 `root` 解析；判定前一律 `resolve()`，解 `..` 与符号链接。
- 判定顺序：`deny_paths` → `root` 内（写受 `write_paths` 约束）→ `writable_mounts`
  → `readonly_mounts`（只读）→ 拒绝。
- glob 支持 `**`（跨目录）：`**/.git/**` 能匹配根层与任意深度。
- 域名（`check_url`）走**独立开关**：SSRF 基线黑名单**始终生效**，与 `enabled` 无关。

**两级作用域**（为什么需要）：`_policy` 是**进程级**默认策略（装配期由配置写入，见
`harness/config/build.py`），供「没指定工作区的会话」使用；控制台允许**按会话**选工作区，
而 `fs.*` / `shell.run` / `code.run` / `net.*` 这些消费者都没有 `ctx` 形参，
因此用 `contextvars.ContextVar` 承载「本次工具调用的策略」：宿主在
`Executor` 里用 `use_sandbox(ctx.sandbox)` 包一层，判定函数通过 `current_sandbox()`
取到覆盖值。ContextVar 按 task/线程隔离，并发 run 互不影响。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Literal
from urllib.parse import urlsplit

AccessMode = Literal["read", "write"]

# 域名黑名单的 SSRF 基线：默认放行，但这些目标**始终拒绝**（与 sandbox.enabled 无关）。
DEFAULT_DENIED_DOMAINS: tuple[str, ...] = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "169.254.169.254",  # 云元数据服务
    "metadata.google.internal",
    "*.local",
    "*.internal",
    "*.localhost",
)


class SandboxViolation(Exception):
    """路径越界、命令引用越界路径、或 URL 命中域名黑名单。由 Executor 转成结构化错误回喂模型。"""


@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool = False
    root: Path = field(default_factory=Path.cwd)
    write_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    readonly_mounts: tuple[Path, ...] = ()
    writable_mounts: tuple[Path, ...] = ()
    # 追加到 DEFAULT_DENIED_DOMAINS 的额外域名（不能覆盖基线）
    extra_denied_domains: tuple[str, ...] = ()


# 进程级默认策略（与 shell.configure_shell_policy 同款；多租户需迁到 Context）
_policy = SandboxPolicy()

# 每次工具调用的覆盖策略（`None` = 用进程级默认）。由 `Executor` 在调用工具前用
# `use_sandbox(ctx.sandbox)` 设置；ContextVar 天然按 task / 线程隔离。
_override: ContextVar[SandboxPolicy | None] = ContextVar("harness_sandbox_override", default=None)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 翻译成正则。`**/` = 零或多层目录，`*` 不跨 `/`，`?` 单字符。"""
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                i += 1
                out.append("[^/]*")
        elif char == "?":
            i += 1
            out.append("[^/]")
        else:
            i += 1
            out.append(re.escape(char))
    out.append("$")
    return re.compile("".join(out))


@lru_cache(maxsize=256)
def _match(target: str, pattern: str) -> bool:
    return _glob_regex(pattern).match(target) is not None


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _targets(real: Path, policy: SandboxPolicy) -> tuple[str, str]:
    """（路径匹配目标，文件名）。在 root 内用相对路径，否则用绝对路径。"""
    if _is_within(real, policy.root):
        return real.relative_to(policy.root).as_posix(), real.name
    return real.as_posix(), real.name


def _first_match(patterns: Iterable[str], real: Path, policy: SandboxPolicy) -> str | None:
    """命中返回该 pattern；pattern 不含 `/` 时按文件名（basename）匹配。"""
    rel_or_abs, name = _targets(real, policy)
    for pattern in patterns:
        target = name if "/" not in pattern else rel_or_abs
        if _match(target, pattern):
            return pattern
    return None


def _check(real: Path, mode: AccessMode) -> None:
    policy = current_sandbox()

    denied = _first_match(policy.deny_paths, real, policy)
    if denied is not None:
        raise SandboxViolation(f"路径命中黑名单规则 {denied!r}: {real}")

    if _is_within(real, policy.root):
        if mode == "write" and policy.write_paths:
            allowed = _first_match(policy.write_paths, real, policy)
            if allowed is None:
                raise SandboxViolation(
                    f"工作区内该路径不在写白名单（write_paths）内: {real}"
                )
        return

    if any(_is_within(real, m) for m in policy.writable_mounts):
        return
    if any(_is_within(real, m) for m in policy.readonly_mounts):
        if mode == "write":
            raise SandboxViolation(f"该路径是只读挂载，不可写: {real}")
        return

    raise SandboxViolation(f"路径越界: {real} 不在工作区内（root={policy.root}）")


def resolve_path(path: str | Path, *, mode: AccessMode = "read") -> Path:
    """把入参解析成安全路径。沙箱关闭时原样返回（零行为变化）。"""
    policy = current_sandbox()
    candidate = path if isinstance(path, Path) else Path(path)
    if not policy.enabled:
        return candidate

    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = policy.root / candidate
    real = candidate.resolve()
    _check(real, mode)
    return real


# 「像路径」的 token：盘符路径 / 绝对路径 / `..` 及其后代
_PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|\.\.(?:[\\/]|$))")
_TOKENS = re.compile(r"[^\s\"'|;&<>()]+")


def check_command(command: str, cwd: Path) -> None:
    """扫描命令串里像路径的 token，越界即拒（fail-closed）。

    只做保守扫描：变量、命令替换、重定向目标、`--flag=路径` 里的值都不展开
    （见设计文档 §8）。宁可漏报，也不能把 `--grep=..` 这类普通参数误判成路径。
    """
    policy = current_sandbox()
    if not policy.enabled:
        return
    for token in _TOKENS.findall(command):
        if not _PATH_LIKE.match(token):
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            resolve_path(candidate, mode="read")
        except SandboxViolation as exc:
            raise SandboxViolation(f"命令引用了工作区外的路径 {token!r}：{exc}") from exc


def configure_sandbox(
    *,
    enabled: bool | None = None,
    root: str | Path | None = None,
    write_paths: Iterable[str] | None = None,
    deny_paths: Iterable[str] | None = None,
    readonly_mounts: Iterable[str | Path] | None = None,
    writable_mounts: Iterable[str | Path] | None = None,
    extra_denied_domains: Iterable[str] | None = None,
) -> SandboxPolicy:
    """覆盖进程级沙箱策略，返回覆盖前的策略。`None` 表示沿用旧值。"""
    global _policy
    previous = _policy
    _policy = SandboxPolicy(
        enabled=previous.enabled if enabled is None else enabled,
        root=previous.root if root is None else Path(root).expanduser().resolve(),
        write_paths=previous.write_paths if write_paths is None else tuple(write_paths),
        deny_paths=previous.deny_paths if deny_paths is None else tuple(deny_paths),
        readonly_mounts=(
            previous.readonly_mounts
            if readonly_mounts is None
            else tuple(Path(p).expanduser().resolve() for p in readonly_mounts)
        ),
        writable_mounts=(
            previous.writable_mounts
            if writable_mounts is None
            else tuple(Path(p).expanduser().resolve() for p in writable_mounts)
        ),
        extra_denied_domains=(
            previous.extra_denied_domains
            if extra_denied_domains is None
            else tuple(extra_denied_domains)
        ),
    )
    return previous


def current_sandbox() -> SandboxPolicy:
    """当前生效的策略：优先本次工具调用的覆盖值，否则进程级默认。"""
    return _override.get() or _policy


@contextmanager
def use_sandbox(policy: SandboxPolicy | None) -> Iterator[None]:
    """在 `with` 块内把沙箱策略临时覆盖为 `policy`。

    `policy is None` 时是 **no-op**——不覆盖，沿用上层（进程级默认或外层 run 的覆盖）。
    这一点很关键：若把 `None` 真的 `set()` 进去，会抹掉外层的 per-run 策略，
    导致派生上下文静默回落到进程全局。

    ContextVar 按 task / 线程隔离：`asyncio.gather` 里并发执行多个工具调用时，
    每个调用在自己的 Task 里 set/reset，互不串味。
    """
    if policy is None:
        yield
        return
    token = _override.set(policy)
    try:
        yield
    finally:
        _override.reset(token)


# ---- 网络域名（默认放行 + 黑名单；基线始终生效，与 enabled 无关）----
ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


def _domain_denied(host: str) -> str | None:
    """命中返回该 pattern。无通配字符时连带子域（`localhost` 也拦 `a.localhost`）。"""
    host = host.strip().lower().rstrip(".")
    if not host:
        return None
    policy = current_sandbox()
    for pattern in (*DEFAULT_DENIED_DOMAINS, *policy.extra_denied_domains):
        cleaned = pattern.strip().lower().rstrip(".")
        if not cleaned:
            continue
        if any(ch in cleaned for ch in "*?["):
            if _match(host, cleaned):
                return pattern
        elif host == cleaned or host.endswith("." + cleaned):
            return pattern
    return None


def check_url(url: str) -> str:
    """校验 URL 的协议与域名，返回 host；命中黑名单抛 SandboxViolation。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SandboxViolation(f"只允许 http/https，收到 {scheme or '(空)'}: {url}")
    host = parts.hostname or ""
    if not host:
        raise SandboxViolation(f"URL 缺少主机名: {url}")
    denied = _domain_denied(host)
    if denied is not None:
        raise SandboxViolation(f"URL 命中域名黑名单 {denied!r}: {host}")
    return host


__all__ = [
    "ALLOWED_SCHEMES",
    "AccessMode",
    "DEFAULT_DENIED_DOMAINS",
    "SandboxPolicy",
    "SandboxViolation",
    "check_command",
    "check_url",
    "configure_sandbox",
    "current_sandbox",
    "resolve_path",
    "use_sandbox",
]
