"""极简 `.env` 读取（`KEY=VALUE` / `#` 注释 / 可选引号），不引入额外依赖。"""

from __future__ import annotations

from pathlib import Path

DEFAULT_ENV_FILE = ".env"


def load_env_file(path: Path | str) -> dict[str, str]:
    """读取 .env。文件不存在返回空 dict（不报错）。"""
    file = Path(path)
    if not file.is_file():
        return {}
    values: dict[str, str] = {}
    for line in file.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key:
            values[key] = raw.strip().strip("'\"")
    return values


__all__ = ["DEFAULT_ENV_FILE", "load_env_file"]
