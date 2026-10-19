# harness — Agent 运行时框架

> 全局设计见 [docs/agent-tool-system-design.md](../docs/agent-tool-system-design.md)。
> 技术栈：Python + FastAPI（harness 为 Agent 核心运行时，FastAPI 作为对外服务层）。

## 模块职责

harness 是 Agent 运行时框架，负责编排「模型 ↔ 工具 ↔ 会话」。模型接入、工具、技能、Agent Loop、会话管理均已实现。
对外服务层在与之平级的 [server/](../server/README.md)（FastAPI + SSE，依赖方向 `server → harness` 单向）。

## 安装

```bash
pip install -e .            # 运行时依赖（harness + server 一起装）
pip install -e ".[test]"    # 另加 pytest / httpx
```

依赖声明见 [pyproject.toml](../pyproject.toml)：**只列代码里直接 `import` 的包**
（`pydantic` / `jsonschema` / `PyYAML` / `openai` / `mcp` / `fastapi` / `starlette` / `sse-starlette` / `uvicorn`），
传递依赖交给各自约束。需要 **Python 3.10+**。

## 目录结构

```
harness/
├─ __init__.py
├─ config/             # 配置系统（YAML + ${ENV} 插值 → 校验 → 装配）
│  ├─ README.md
│  ├─ schema.py / loader.py / dotenv.py / build.py / sink.py
│  └─ test/
├─ agent/              # Agent Loop（编排层：循环 + 停止条件 + 事件回调）
│  ├─ README.md
│  ├─ schema.py / events.py / prompt.py / loop.py
│  └─ test/
├─ session/            # 会话管理（多轮记忆 + 上下文分区 + append-only 持久化）
│  ├─ README.md
│  ├─ schema.py / store.py / jsonl.py / compact.py / manager.py
│  └─ test/
├─ tool/               # 工具系统（定义/注册/执行/鉴权/观测/扩展）
│  ├─ README.md        # 工具系统模块文档
│  ├─ adapter/         # 外部工具适配器（MCP 等）
│  ├─ builtin/         # 内置工具集
│  └─ test/            # 冒烟测试
├─ skill/              # 技能系统（按需加载的能力说明书）
│  ├─ README.md
│  ├─ schema.py / parser.py / registry.py / tools.py
│  └─ test/
└─ llm/                # 模型接入层（OpenAI Chat Completions 兼容）
   ├─ README.md
   ├─ schema.py / errors.py / base.py / openai_compat.py
   └─ test/
```

## 文档索引

| 模块 | 文档 | 状态 |
|---|---|---|
| 配置系统 | [config/README.md](config/README.md) | 已实现（M1–M4） |
| Agent Loop | [agent/README.md](agent/README.md) | 已实现（M1–M5） |
| 会话管理 | [session/README.md](session/README.md) | 已实现（M1–M5） |
| 工具系统 | [tool/README.md](tool/README.md) | 已实现（P0 + MCP） |
| ‣ 适配器 | [tool/adapter/README.md](tool/adapter/README.md) | 已实现 |
| ‣ 内置工具 | [tool/builtin/README.md](tool/builtin/README.md) | 已实现 |
| ‣ 测试 | [tool/test/README.md](tool/test/README.md) | 已实现 |
| 技能系统 | [skill/README.md](skill/README.md) | 已实现（M1–M4） |
| 模型接入 | [llm/README.md](llm/README.md) | 已实现（M1–M4，含流式） |

## 对外服务（与 harness 平级）

| 模块 | 文档 | 状态 |
|---|---|---|
| FastAPI 服务层 | [../server/README.md](../server/README.md) | 已实现（M1–M4，含 SSE 真流式） |
| 内置控制台 | [../docs/frontend-console-design.md](../docs/frontend-console-design.md) | 已实现（对话 + 用量 + 配置 + 运维） |

## 相关全局文档

- [Agent Loop 设计文档](../docs/agent-loop-design.md)
- [会话管理设计文档](../docs/session-management-design.md)
- [FastAPI 服务层设计文档](../docs/api-service-design.md)
- [配置系统设计文档](../docs/configuration-system-design.md)
- [前端控制台设计文档](../docs/frontend-console-design.md)
- [工具系统设计文档](../docs/agent-tool-system-design.md)
- [Skills 系统设计文档](../docs/skills-system-design.md)
- [LLM 接入层设计文档](../docs/llm-system-design.md)
- [项目 TODO](../todo.md)

## 测试

两条并存的运行路径，测试**全离线**：

```bash
pip install -e ".[test]"        # pytest + httpx
pytest                          # 标准入口（testpaths = harness, server）
python -m harness.tool.test.test_tool_system   # 或单模块零依赖直跑（自带 main()）
```

仓库根 [conftest.py](../conftest.py) 提供共享 fixture：autouse 的**进程级全局状态隔离**
（sandbox / shell / net 策略在每个用例前后复位，防止失败用例污染后续用例）与临时工作区
`tmp_workspace`。

## 规划中（尚未实现）

见 [项目 TODO](../todo.md)：CLI 入口、`net.search`（需先定 provider）、
`agent.dispatch` / `process.*`、OS 级进程隔离。
