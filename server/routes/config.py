"""配置：读取 / 写回 / 重载。

`PUT` 写回 `harness.yaml`（密钥只允许 `${VAR}` 引用）并**原地重建**运行时部件。
写回细节见 [harness/config/admin.py](../../harness/config/admin.py)，
重建见 [server/state.py](../state.py)。
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from harness.config import ConfigError, HarnessConfig, load_config
from harness.config.admin import (
    backup_path,
    current_env,
    dump_raw,
    env_override_report,
    is_plaintext_secret,
    ordered_config,
    read_yaml,
    redact,
    restore_secrets,
    validate,
)

from ..deps import StateDep
from ..errors import ServiceError, conflict

router = APIRouter(tags=["config"])


class ConfigUpdateRequest(BaseModel):
    config: dict[str, Any]
    reload: bool = True


def _snapshot(state: Any, warnings: list[str] | None = None) -> dict:
    """GET/写回后统一的响应体（脱敏；`effective` 是运行时真正生效的值）。"""
    env = current_env()
    body: dict[str, Any] = {
        "config_path": state.config_path,
        "editable": True,
        "config": redact(read_yaml(state.config_path)),
        "effective": redact(dataclasses.asdict(state.config)),
        "env_overrides": env_override_report(env),
        "sections": list(state.config.section_names),
        "backup_path": str(backup_path(state.config_path)),
    }
    if warnings is not None:
        body["warnings"] = warnings
    return body


def _unavailable() -> dict:
    return {
        "config_path": None,
        "editable": False,
        "config": None,
        "effective": None,
        "env_overrides": [],
        "sections": list(HarnessConfig().section_names),
        "backup_path": None,
    }


def _require_context(state: Any) -> None:
    if state.config is None or state.config_path is None:
        raise ServiceError(
            "config_unavailable", "当前 app 未持有配置上下文，无法写配置", status=409
        )
    if state.runs.active_count() > 0:
        raise conflict(
            "config_reload_conflict",
            "有 run 正在执行；配置重建会中断它。请等其结束或先取消全部 run 再试",
        )


def _invalid(exc: ConfigError) -> ServiceError:
    return ServiceError(
        "config_invalid", exc.message, status=422, details={"path": exc.path}
    )


@router.get("/config")
async def get_config(state: StateDep) -> dict:
    if state.config is None or state.config_path is None:
        return _unavailable()
    return _snapshot(state)


@router.put("/config")
async def put_config(body: ConfigUpdateRequest, state: StateDep) -> dict:
    _require_context(state)
    previous = copy.deepcopy(read_yaml(state.config_path))

    data = restore_secrets(body.config, previous)
    plain = is_plaintext_secret(data)
    if plain:
        raise ServiceError(
            "config_invalid",
            "密钥请写入 .env 并用 ${VAR} 引用，不要明文落 harness.yaml",
            status=422,
            details={"path": plain},
        )
    try:
        new_config = validate(data)
    except ConfigError as exc:
        raise _invalid(exc) from exc

    dump_raw(ordered_config(data), state.config_path)
    try:
        warnings = await state.apply_config(new_config, reload_mcp=body.reload)
    except ConfigError as exc:  # 如 otel sink 未安装 → 回滚文件
        dump_raw(previous, state.config_path, backup=False)
        raise _invalid(exc) from exc
    return _snapshot(state, warnings)


@router.post("/config/reload")
async def reload_config(state: StateDep) -> dict:
    """从磁盘重读并重建（用于在编辑器里手改了 YAML）。不写盘。"""
    _require_context(state)
    try:
        new_config = load_config(state.config_path)
    except ConfigError as exc:
        raise _invalid(exc) from exc
    warnings = await state.apply_config(new_config)
    return _snapshot(state, warnings)
