"""网络工具：`net.fetch` / `net.download`（标准库 `urllib`，零新增依赖）。

域名策略（**默认放行 + SSRF 基线黑名单**）在 [sandbox.py](../sandbox.py) 的 `check_url()`；
本模块只管体积上限、重定向复查与 HTML 提取。设计见 docs/builtin-tools-design.md §3–§4。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from ..registry import tool
from ..sandbox import check_url, resolve_path

DEFAULT_MAX_BYTES = 200_000
DEFAULT_MAX_DOWNLOAD_BYTES = 20_000_000
_REQUEST_TIMEOUT_S = 20.0
_USER_AGENT = "harness/0.1"


@dataclass(frozen=True)
class NetPolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES


# 进程级策略（与 shell / sandbox 同款；多租户需迁到 Context）
_policy = NetPolicy()


def configure_net_policy(
    *, max_bytes: int | None = None, max_download_bytes: int | None = None
) -> NetPolicy:
    """覆盖进程级网络上限，返回覆盖前的策略。`None` 表示沿用旧值。"""
    global _policy
    previous = _policy
    _policy = NetPolicy(
        max_bytes=previous.max_bytes if max_bytes is None else int(max_bytes),
        max_download_bytes=(
            previous.max_download_bytes
            if max_download_bytes is None
            else int(max_download_bytes)
        ),
    )
    return previous


def current_net_policy() -> NetPolicy:
    return _policy


class _GuardedRedirect(urlrequest.HTTPRedirectHandler):
    """逐跳复查域名：防「允许的域名 → 内网地址」一次跳转绕过入口检查。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    check_url(url)
    request = urlrequest.Request(url, headers={"User-Agent": _USER_AGENT})
    opener = urlrequest.build_opener(_GuardedRedirect())
    try:
        return opener.open(request, timeout=_REQUEST_TIMEOUT_S)
    except HTTPError as exc:
        raise ValueError(f"HTTP {exc.code} {exc.reason}: {url}") from exc
    except URLError as exc:
        raise ValueError(f"请求失败: {exc.reason}") from exc


class _TextExtractor(HTMLParser):
    """扒标签取纯文本；跳过 script / style / noscript。"""

    _SKIP = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self.parts.append(text)


def _charset(content_type: str) -> str:
    lowered = content_type.lower()
    if "charset=" in lowered:
        return lowered.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    return "utf-8"


def _to_text(body: bytes, content_type: str) -> str:
    """HTML 转纯文本（零依赖的粗略实现，不做 markdown 转换）。"""
    text = body.decode(_charset(content_type), errors="replace")
    if "html" not in content_type.lower() and "<html" not in text[:500].lower():
        return text
    parser = _TextExtractor()
    parser.feed(text)
    return "\n".join(parser.parts)


@tool(
    name="net.fetch",
    description=(
        "抓取一个 URL 并返回其正文（只读）。HTML 会被转成纯文本；返回含状态码与最终 URL。"
        "适合查看文档、网页与 GET 接口；下载大文件请用 net.download。"
    ),
    side_effect="read",
    scopes=["net:read"],
    timeout_s=30.0,
    result_max_chars=20000,
    tags=["net", "read"],
)
def fetch(url: str) -> dict[str, Any]:
    """抓取 URL 内容。

    Args:
        url: 目标 URL，仅支持 http / https。
    """
    limit = current_net_policy().max_bytes
    with _open(url) as response:
        raw = response.read(limit + 1)
        truncated = len(raw) > limit
        content_type = response.headers.get("Content-Type", "")
        status = getattr(response, "status", None) or response.getcode()
        final_url = response.geturl()
    return {
        "url": url,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "truncated": truncated,
        "text": _to_text(raw[:limit], content_type),
    }


def _pump(response: Any, out: Any, limit: int) -> int:
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            return total
        total += len(chunk)
        if total > limit:
            raise ValueError(
                f"下载超过上限 {limit} 字节（tools.net.max_download_bytes），已中止"
            )
        out.write(chunk)


@tool(
    name="net.download",
    description=(
        "把 URL 内容下载到本地文件（写入操作）。落盘路径受工作区沙箱约束；"
        "只看网页正文请用 net.fetch。"
    ),
    side_effect="write",
    scopes=["net:read", "fs:write"],
    timeout_s=300.0,
    tags=["net", "write"],
)
def download(url: str, path: str) -> str:
    """下载文件到本地。

    Args:
        url: 目标 URL，仅支持 http / https。
        path: 保存路径（受工作区沙箱约束）。
    """
    limit = current_net_policy().max_download_bytes
    dest = resolve_path(path, mode="write")
    dest.parent.mkdir(parents=True, exist_ok=True)

    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".net-", suffix=".part")
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as out, _open(url) as response:
            total = _pump(response, out, limit)
        os.replace(temp_path, dest)
        temp_path = None
        return f"已下载 {total} 字节到 {dest}"
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_DOWNLOAD_BYTES",
    "NetPolicy",
    "configure_net_policy",
    "current_net_policy",
    "download",
    "fetch",
]
