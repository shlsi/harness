"""目录浏览：`GET /fs/browse`（控制台选「agent 的工作区」用）。

**为什么需要服务端出这个端点**：浏览器出于安全**拿不到本机绝对路径**——
`<input type="file" webkitdirectory>` 只给相对路径，`File System Access API` 给的是句柄而非
真实路径。而工作区必须是服务端可用的绝对路径，所以只能由服务端列举目录、前端逐级点选。

**安全边界**：本端点只返回**条目名与类型**，不读文件内容；不跟随符号链接判定目录类型。
它与 `PUT /config` 属同一风险等级——一旦把服务暴露到 `0.0.0.0`，等于开放目录枚举，
因此默认 `server.host=127.0.0.1`，并写入「已知限制」。此处刻意**不设白名单**：
控制台的价值就在于能选任意目录，若限定死就失去了意义。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from harness.tool import current_sandbox

from ..errors import ServiceError

router = APIRouter(prefix="/fs", tags=["fs"])

#: 单次返回的条目上限（超大目录只给前 N 条，并置 `truncated` 让前端提示）
MAX_ENTRIES = 500


def _start_dir(path: str | None) -> Path:
    """未指定 `path` 时的起点：沙箱开着就用工作区，否则用用户主目录。"""
    if path is None or not str(path).strip():
        policy = current_sandbox()
        return policy.root if policy.enabled else Path.home()
    return Path(str(path)).expanduser()


@router.get("/browse")
async def browse(path: str | None = None) -> dict:
    """列举 `path` 下的一层条目（目录与文件），供前端目录选择器逐级点选。"""
    target = _start_dir(path)
    try:
        resolved = target.resolve()
        if not resolved.is_dir():
            raise NotADirectoryError(str(target))
        with os.scandir(resolved) as scan:
            names: list[tuple[str, str]] = []
            for entry in scan:
                try:
                    # 不跟随符号链接判定类型（避免用链接指到别处制造困惑）
                    kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
                except OSError:
                    continue
                names.append((entry.name, kind))
    except NotADirectoryError as exc:
        raise ServiceError(
            "invalid_path", f"目录不存在或不是目录: {target}", 422, {"path": str(target)}
        ) from exc
    except PermissionError as exc:
        raise ServiceError(
            "invalid_path", f"没有权限读取目录: {target}", 422, {"path": str(target)}
        ) from exc

    # 目录在前、文件在后，各自按名字排序（大小写不敏感，中文也可稳定排序）
    names.sort(key=lambda item: (item[1] != "dir", item[0].lower()))
    truncated = len(names) > MAX_ENTRIES
    entries = [{"name": name, "kind": kind} for name, kind in names[:MAX_ENTRIES]]

    parent = resolved.parent
    return {
        "path": str(resolved),
        "parent": None if parent == resolved else str(parent),
        "entries": entries,
        "truncated": truncated,
    }
