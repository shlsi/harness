"""FastAPI app 工厂。

启动：根目录 `python main.py`（读 `harness.yaml` 的 `server.host/port`）；
等价的手写命令是 `uvicorn server.app:create_app --factory`。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from harness.config import DEFAULT_CONFIG_FILE, HarnessConfig, load_config

from .errors import register_error_handlers
from .routes import config as config_routes
from .routes import fs, interaction, mcp, memory, meta, run, session, skill, turn
from .state import AppState, build_state

ROUTERS = (
    session.router,
    turn.router,
    run.router,
    interaction.router,
    meta.router,
    config_routes.router,
    memory.router,
    mcp.router,
    skill.router,
    fs.router,
)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: HarnessConfig | None = None, *, state: AppState | None = None
) -> FastAPI:
    """构造应用。

    - `config` 缺省走 `load_config()`（`harness.yaml` + `.env` + 环境变量）
    - `state` 用于测试注入（FakeProvider / 内存会话）；注入时不会连接 MCP
    """
    resolved = config or load_config()
    harness_state = state or build_state(resolved)
    if state is None:
        # 记录实际生效的配置路径，供 GET/PUT /config 读写（注入的测试 state 保持 None）
        harness_state.config_path = os.environ.get("HARNESS_CONFIG", DEFAULT_CONFIG_FILE)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await harness_state.start()  # 连接配置中的 MCP server
        yield
        await harness_state.aclose()

    app = FastAPI(title="harness api", version="0.1.0", lifespan=lifespan)
    app.state.harness = harness_state
    register_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router)

    # 简易前端：与 API 同源，省去 CORS
    if STATIC_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def _root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    return app
