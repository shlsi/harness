"""三档会话权限：`只读` / `读写·需批准` / `读写·免批准`。

**为什么需要这一层**：控制台里用户不该自己拼 scopes（`fs:read` + `fs:write` + `shell:run`…），
而应该从三个语义明确的档位里选一个。「档位 → 权限域 + 审批行为」的映射必须只有**一处**，
否则前端、服务层、文档三份定义早晚漂移——所以放在这里。

档位与 `SessionMeta.metadata["permission"]` 同值，由 `server/runner.py` 在每轮 run 开始时读取：

| 档位 | 授予的 scopes | 审批 |
|---|---|---|
| `read` | `fs:read`, `net:read` | 无（没有写 / 危险工具可用） |
| `write` | `read` 的全部 + `fs:write`, `shell:run`, `code:run`, `memory:write` | 沿用 `Policy` 判定：`requires_approval=True` 或 `side_effect="destructive"` 的工具弹审批 |
| `write_free` | 同 `write` | **全部旁路**（含危险操作），不弹卡片 |

注意两点：

- `todo.write` 与 `ask_user` 声明了**空 scopes**，因此在三档下都可用——它们是会话内交互，
  不触碰外部状态。
- `net:read` 归入「只读」：读网络不改变本地状态。若希望只读档也禁网，改本文件的
  `_SCOPES` 一处即可。
- MCP 工具默认 `scopes=()` 且 `requires_approval=False`，**不受档位约束**——
  收口请用 MCP server 的 `default_scopes` / `default_requires_approval`。

设计见 docs/frontend-console-design.md 与 docs/sandbox-design.md。
"""

from __future__ import annotations

READ = "read"
WRITE = "write"
WRITE_FREE = "write_free"

#: 合法档位（顺序即 UI 展示顺序：从最保守到最放开）
PERMISSIONS: tuple[str, ...] = (READ, WRITE, WRITE_FREE)


class PermissionError_(ValueError):
    """非法档位。单独设类便于服务层区分「档位非法」与其他参数错误。"""


# 「只读」能做什么：读文件 + 读网络
_READ_SCOPES: tuple[str, ...] = ("fs:read", "net:read")
# 「读写」在只读基础上加上所有写 / 执行类权限域
_WRITE_SCOPES: tuple[str, ...] = _READ_SCOPES + (
    "fs:write",
    "shell:run",
    "code:run",
    "memory:write",
)

_SCOPES: dict[str, tuple[str, ...]] = {
    READ: _READ_SCOPES,
    WRITE: _WRITE_SCOPES,
    WRITE_FREE: _WRITE_SCOPES,
}

# 津贴：档位是否为「免批准」——需要旁路 `Context.approval` 的档位
_BYPASS: dict[str, bool] = {READ: False, WRITE: False, WRITE_FREE: True}


def normalize(level: str | None) -> str | None:
    """校验并规范化档位。`None` 原样返回（表示「未设置」）；非法值抛 `PermissionError_`。"""
    if level is None:
        return None
    cleaned = str(level).strip().lower()
    if cleaned not in _SCOPES:
        raise PermissionError_(
            f"权限档位非法: {level!r}；可选 {' / '.join(PERMISSIONS)}"
        )
    return cleaned


def scopes_for(level: str) -> tuple[str, ...]:
    """档位 → 该会话被授予的权限域。"""
    return _SCOPES[normalize(level)]


def approval_bypass(level: str) -> bool:
    """该档位是否需要**旁路审批**（`write_free` 为 True）。

    True 时宿主应把审批 hook 换成恒真回调——`Policy` 仍照常判定「需不需要审批」，
    只是「是否真弹卡片」被跳过（见 docs/sandbox-design.md §权限档位）。
    """
    return _BYPASS[normalize(level)]


__all__ = [
    "PERMISSIONS",
    "READ",
    "WRITE",
    "WRITE_FREE",
    "PermissionError_",
    "approval_bypass",
    "normalize",
    "scopes_for",
]
