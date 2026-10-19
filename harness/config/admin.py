"""配置管理：原始 YAML 的读 / 脱敏 / 校验 / 回写。

供控制台（`GET|PUT /config`）与 CLI 复用。**只碰文件与数据，不做装配**——
装配（provider/registry 重建）在 `server/state.py:AppState.apply_config`。

设计取舍：回写用 `yaml.safe_dump` **会丢失原文件注释与键序**，换零新依赖；
写前备份 `<file>.bak`，并在文件头写明「由控制台生成」。见
[docs/configuration-system-design.md](../../docs/configuration-system-design.md) §9。
"""

from __future__ import annotations

import copy
import os
import re
import shutil
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import yaml

from .dotenv import DEFAULT_ENV_FILE, load_env_file
from .loader import ENV_OVERRIDES, build_from_dict, read_yaml
from .schema import HarnessConfig

# 脱敏占位符：前端提交它表示「保持原值不动」
SENTINEL = "***"

# 需要脱敏的字段路径（值若是 `${VAR}` 引用则原样保留；否则替换为 SENTINEL）
SECRET_PATHS: tuple[tuple[str, ...], ...] = (("llm", "api_key"),)

# 完整的 `${VAR}` / `${VAR:-default}` 引用（整串匹配，不允许前后缀）
_VAR_FULL = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}$")

MANAGED_HEADER = (
    "# 本文件由 harness 控制台生成/更新：注释与键序会被规范化，请勿手工排布。\n"
    "# 真实密钥请放在 .env，这里只写 ${VAR} 引用。\n"
)


# ---- 路径读写（点号路径）----
def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _delete(data: dict[str, Any], path: tuple[str, ...]) -> None:
    node: Any = data
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(path[-1], None)


# ---- 脱敏 / 还原 ----
def redact(data: dict[str, Any]) -> dict[str, Any]:
    """把敏感字段替换成占位符：`${VAR}` 引用保留，其余字面量一律 `***`。"""
    out = copy.deepcopy(data)
    for path in SECRET_PATHS:
        value = _get(out, path)
        if value is None:
            continue
        if isinstance(value, str) and (_VAR_FULL.match(value) or not value.strip()):
            continue
        _set(out, path, SENTINEL)
    return out


def restore_secrets(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """把新配置里「未改动」的敏感字段还原为旧值（`***`/空/None 都算未改动）。"""
    out = copy.deepcopy(new)
    for path in SECRET_PATHS:
        incoming = _get(out, path)
        if incoming is not None and incoming != "" and incoming != SENTINEL:
            continue
        previous = _get(old, path)
        if previous is None:
            _delete(out, path)
        else:
            _set(out, path, previous)
    return out


def is_plaintext_secret(data: dict[str, Any]) -> str | None:
    """返回第一个「明文（既非 `${VAR}` 也非占位/空）」的敏感字段路径，用于拦截落盘。"""
    for path in SECRET_PATHS:
        value = _get(data, path)
        if isinstance(value, str) and value.strip() and value != SENTINEL:
            if not _VAR_FULL.match(value):
                return ".".join(path)
    return None


# ---- 环境变量 ----
def current_env(env_file: Path | str | None = None) -> dict[str, str]:
    """当前生效的环境层：`.env` + `os.environ`（优先级低的在前，后者覆盖）。"""
    resolved = env_file or os.environ.get("HARNESS_ENV_FILE", DEFAULT_ENV_FILE)
    return {**load_env_file(resolved), **os.environ}


def env_override_report(env: dict[str, str]) -> list[dict[str, Any]]:
    """列出真正生效的环境变量覆盖（这些项改文件是无效的）。敏感值脱敏。"""
    report: list[dict[str, Any]] = []
    for name, path in ENV_OVERRIDES.items():
        if name not in env:
            continue
        value = SENTINEL if tuple(path) in SECRET_PATHS and env[name] else env[name]
        report.append(
            {"env": name, "path": ".".join(path), "value": value, "active": True}
        )
    return report


# ---- 校验 ----
def validate(data: dict[str, Any], *, env: dict[str, str] | None = None) -> HarnessConfig:
    """把原始配置字典校验成 `HarnessConfig`（插值 + 环境覆盖 + 类型校验）。

    出错抛 `ConfigError`（`path` 指向具体字段，可直接回显给用户）。
    """
    return build_from_dict(data, env if env is not None else current_env())


# ---- 键序 ----
def _unwrap(tp: Any) -> Any:
    origin = get_origin(tp)
    if origin is UnionType or origin is Union:
        candidates = [arg for arg in get_args(tp) if arg is not type(None)]
        if len(candidates) == 1:
            return candidates[0]
    return tp


def _order_value(value: Any, tp: Any) -> Any:
    tp = _unwrap(tp)
    origin = get_origin(tp)
    if isinstance(tp, type) and is_dataclass(tp) and isinstance(value, dict):
        return ordered_config(value, tp)
    if origin is list and isinstance(value, list):
        args = get_args(tp)
        inner = args[0] if args else Any
        return [_order_value(item, inner) for item in value]
    return value


def ordered_config(data: Any, cls: type = HarnessConfig) -> Any:
    """按 dataclass 字段顺序重排（递归），让 dump 出来的 YAML 键序稳定可读。"""
    if not isinstance(data, dict):
        return data
    hints = get_type_hints(cls)
    declared = {item.name for item in fields(cls)}
    out: dict[str, Any] = {}
    for name in [item.name for item in fields(cls)]:
        if name in data:
            out[name] = _order_value(data[name], hints.get(name))
    for key, value in data.items():  # 未声明的键（一般不该有）补到末尾，不丢
        if key not in declared:
            out[key] = value
    return out


# ---- 回写 ----
def backup_path(path: Path | str) -> Path:
    return Path(str(path) + ".bak")


def dump_raw(data: dict[str, Any], path: Path | str, *, backup: bool = True) -> Path:
    """把配置写回 YAML（原子替换）。`backup=True` 时先把旧文件复制为 `<file>.bak`。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = MANAGED_HEADER + yaml.safe_dump(
        ordered_config(data),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if backup and target.is_file():
        shutil.copy2(target, backup_path(target))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


__all__ = [
    "MANAGED_HEADER",
    "SECRET_PATHS",
    "SENTINEL",
    "backup_path",
    "current_env",
    "dump_raw",
    "env_override_report",
    "is_plaintext_secret",
    "ordered_config",
    "read_yaml",
    "redact",
    "restore_secrets",
    "validate",
]
