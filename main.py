"""项目入口：读 `harness.yaml` 的 `server` 段启动 FastAPI 服务。

用法：
    python main.py                 # 用配置里的 host / port 启动
    python main.py --port 9000     # 覆盖端口
    python main.py --reload        # 开发用：代码改动自动重载

启动后控制台在 `http://<host>:<port>/ui/`。
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from harness.config import load_config

APP = "server.app:create_app"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="启动 harness 服务（默认读 harness.yaml 的 server 段）"
    )
    parser.add_argument("--host", default=None, help="监听地址，默认取 server.host")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取 server.port")
    parser.add_argument("--reload", action="store_true", help="代码改动自动重载（仅开发用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    host = args.host or config.server.host
    port = args.port or config.server.port

    print(f"harness 控制台：http://{host}:{port}/ui/")
    uvicorn.run(APP, factory=True, host=host, port=port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
