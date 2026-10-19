"""配置加载：读 YAML → `${ENV}` 插值 → 环境变量覆盖 → 校验并构造 `HarnessConfig`。

优先级（高 → 低）：`env` 参数 > 真实进程环境变量 > `.env` > YAML 文件 > 代码默认值。

**插值在解析之后对「字符串值」做**，而不是把变量值拼进 YAML 文本——
否则密钥里的 `#` 会被当成注释、`:` 可能被当成映射分隔符，直接破坏配置结构。
"""

from __future__ import annotations

import os
import re
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import yaml

from .dotenv import DEFAULT_ENV_FILE, load_env_file
from .schema import ConfigError, HarnessConfig

DEFAULT_CONFIG_FILE = "harness.yaml"

# 环境变量 → 配置路径。既有的 HARNESS_* / OPENAI_* 语义保持不变。
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "OPENAI_API_KEY": ("llm", "api_key"),
    "OPENAI_BASE_URL": ("llm", "base_url"),
    "HARNESS_MODEL": ("llm", "model"),
    "HARNESS_MAX_STEPS": ("agent", "max_steps"),
    "HARNESS_SKILLS_DIR": ("skills", "root"),
    "HARNESS_DATA_DIR": ("server", "data_dir"),
    "HARNESS_INTERACTION_TIMEOUT_S": ("server", "interaction_timeout_s"),
    "HARNESS_HEARTBEAT_S": ("server", "heartbeat_s"),
    "HARNESS_RUN_TTL_S": ("server", "run_ttl_s"),
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# ---- 插值 ----
def interpolate(value: Any, env: dict[str, str], path: str = "") -> Any:
    """递归替换字符串里的 `${VAR}` / `${VAR:-default}`；变量缺失直接报错。"""
    if isinstance(value, str):
        return _interpolate_str(value, env, path)
    if isinstance(value, dict):
        return {
            key: interpolate(item, env, f"{path}.{key}" if path else str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [interpolate(item, env, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def _interpolate_str(text: str, env: dict[str, str], path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ConfigError(path, f"环境变量 ${{{name}}} 未设置（配置文件中引用了它）")

    return _VAR.sub(replace, text)


# ---- 环境变量覆盖 ----
def apply_env_overrides(data: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """按 ENV_OVERRIDES 把环境变量的值写进配置字典（只覆盖**存在**的变量，空串也算）。"""
    for name, path in ENV_OVERRIDES.items():
        if name not in env:
            continue
        node = data
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[path[-1]] = env[name]
    return data


# ---- 校验与构造 ----
def build_dataclass(cls: type, data: Any, path: str) -> Any:
    """按 dataclass 字段表构造实例：未知名报错、类型不匹配报错（带字段路径）。"""
    if not isinstance(data, dict):
        raise ConfigError(path, f"期望映射，实际是 {_kind(data)}")

    hints = get_type_hints(cls)
    declared = {item.name: item for item in fields(cls)}
    unknown = [key for key in data if key not in declared]
    if unknown:
        raise ConfigError(path, f"未知字段 {sorted(unknown)}（可用：{sorted(declared)}）")

    kwargs: dict[str, Any] = {}
    for name, item in declared.items():
        if name not in data:
            continue  # 缺省用 dataclass 默认值
        field_path = f"{path}.{name}" if path else name
        kwargs[name] = coerce(data[name], hints.get(name, Any), field_path)
    return cls(**kwargs)


def coerce(value: Any, tp: Any, path: str) -> Any:
    """把配置值转成声明类型；数字/布尔允许从字符串宽松转换。"""
    if tp is Any or tp is None:
        return value

    origin = get_origin(tp)
    if origin is UnionType or origin is Union:
        if value is None:
            return None
        candidates = [arg for arg in get_args(tp) if arg is not type(None)]
        if len(candidates) == 1:
            return coerce(value, candidates[0], path)
        for candidate in candidates:
            try:
                return coerce(value, candidate, path)
            except ConfigError:
                continue
        raise ConfigError(path, f"值 {value!r} 不匹配类型 {_type_name(tp)}")

    if origin is list:
        items = get_args(tp) or (Any,)
        if not isinstance(value, list):
            raise ConfigError(path, f"期望列表，实际是 {_kind(value)}")
        return [coerce(item, items[0], f"{path}[{index}]") for index, item in enumerate(value)]

    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(path, f"期望映射，实际是 {_kind(value)}")
        return dict(value)

    if isinstance(tp, type) and is_dataclass(tp):
        return build_dataclass(tp, value, path)

    if tp is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE_WORDS | _FALSE_WORDS:
            return value.strip().lower() in _TRUE_WORDS
        raise ConfigError(path, f"期望布尔值，实际是 {_kind(value)}")

    if tp is int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(path, f"期望整数，实际是 {_kind(value)}")
        try:
            return int(value)
        except ValueError:
            raise ConfigError(path, f"期望整数，实际是 {value!r}") from None

    if tp is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(path, f"期望数字，实际是 {value!r}") from None

    if tp is str:
        if isinstance(value, str):
            return value
        raise ConfigError(path, f"期望字符串，实际是 {_kind(value)}")

    return value


_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def _kind(value: Any) -> str:
    return type(value).__name__


def _type_name(tp: Any) -> str:
    return getattr(tp, "__name__", str(tp))


# ---- 入口 ----
def build_from_dict(data: dict[str, Any], env: dict[str, str]) -> HarnessConfig:
    """由「已解析的 YAML 字典」构造配置：插值 → 环境覆盖 → 校验。

    供 `load_config` 与控制台（`harness/config/admin.py`）复用。
    """
    merged = apply_env_overrides(interpolate(data, env), env)
    return build_dataclass(HarnessConfig, merged, "")


def load_config(
    path: Path | str | None = None,
    *,
    env: dict[str, str] | None = None,
    env_file: Path | str | None = None,
) -> HarnessConfig:
    """加载配置。YAML 文件不存在时返回全默认值（不报错）。

    - `path` 缺省取 `HARNESS_CONFIG`，再退到项目根 `harness.yaml`
    - `env=None`（默认）：环境层 = `.env` + `os.environ`
    - `env` 显式传入：**完全替换**环境层（不读 `.env`、不读 `os.environ`），便于测试与 CLI 复现
    """
    if env is None:
        env_file = env_file or os.environ.get("HARNESS_ENV_FILE", DEFAULT_ENV_FILE)
        merged_env: dict[str, str] = {**load_env_file(env_file), **os.environ}
        config_path = path or os.environ.get("HARNESS_CONFIG", DEFAULT_CONFIG_FILE)
    else:
        merged_env = dict(env)
        if env_file is not None:  # 显式叠加 .env（可选）
            merged_env = {**load_env_file(env_file), **merged_env}
        config_path = path or DEFAULT_CONFIG_FILE

    return build_from_dict(read_yaml(config_path), merged_env)


def read_yaml(path: Path | str) -> dict[str, Any]:
    """读原始 YAML（**不做插值、不叠环境变量**）。文件不存在 → 空字典。"""
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        raw = yaml.safe_load(file.read_text("utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(str(path), f"YAML 解析失败: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(str(path), "配置文件顶层必须是映射（key: value）")
    return raw


# 兼容旧名（模块内部曾用私有名）
_read_yaml = read_yaml


__all__ = [
    "DEFAULT_CONFIG_FILE",
    "ENV_OVERRIDES",
    "apply_env_overrides",
    "build_dataclass",
    "build_from_dict",
    "coerce",
    "interpolate",
    "load_config",
    "read_yaml",
]
