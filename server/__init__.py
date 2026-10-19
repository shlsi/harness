"""FastAPI 服务层：harness 的对外 HTTP / SSE 服务（组合根）。

设计见 docs/api-service-design.md。
"""

from __future__ import annotations

from .app import create_app
from .state import AppState, build_state

__all__ = ["AppState", "build_state", "create_app"]
