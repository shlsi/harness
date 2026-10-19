# harness — Agent 运行时框架 + FastAPI 服务层

> 一个自用的 Agent 运行时：把**模型 ↔ 工具 ↔ 会话**编排起来，附带一套同源的单文件控制台。
> 技术栈：Python 3.10+ / FastAPI。代码注释与文档均为中文。

## 这是什么

- **`harness/`** —— Agent 运行时框架，**纯库、不感知 HTTP**。包含 Agent Loop、工具系统（含 MCP 接入）、
  会话管理、技能系统、模型接入层、记忆、配置与沙箱。
- **`server/`** —— FastAPI 宿主，`AppState` 是**组合根**；提供 REST + SSE 真流式接口，
  并在同一端口挂载内置控制台。依赖方向严格单向：`server → harness`。
- **控制台** —— `server/static/index.html` 单文件、零外部资源，与 API 同源（省 CORS）。
  可对话、看用量与调用链、改配置、启停技能 / MCP、上传技能 zip。

## 架构

```
             ┌──────────────────────────────────────────┐
   浏览器 ───▶│ server/   FastAPI 宿主（AppState 组合根）  │
             │   routes/  REST · SSE · /ui 静态控制台     │
             └───────────────────┬──────────────────────┘
                                 │ 单向依赖
             ┌───────────────────▼──────────────────────┐
             │ harness/  纯库（不感知 HTTP）              │
             │   config  agent  session  tool  skill     │
             │   llm     memory                          │
             └──────────────────────────────────────────┘
                    配置真源：harness.yaml + .env
```

`harness/config/build.py` 是**唯一装配点**：CLI、server、测试都走 `load_config()` + `build_*()`。

## 快速开始

```bash
pip install -e ".[test]"     # 运行时依赖 + pytest/httpx（只跑服务用 pip install -e .）
cp .env.example .env         # 填入 OPENAI_API_KEY（harness.yaml 里只写 ${OPENAI_API_KEY} 引用）
python main.py               # 读 harness.yaml 的 server.host/port
# 控制台：http://127.0.0.1:8000/ui/
```

常用变体：

```bash
python main.py --port 9000   # 覆盖端口
python main.py --reload      # 开发用：代码改动自动重载
```

## 能力一览

| 能力 | 说明 |
|---|---|
| Agent Loop | 多步工具调用循环、循环检测、token 预算、**软打断**（用户插话后本轮工具跑完即收尾） |
| 工具系统 | 内置 `fs.*`（读写/编辑/列举/glob/grep/mkdir/copy/move/delete）、`shell.run`、`net.fetch` / `net.download`、`code.run`、`time.now`、`todo.write`，技能侧 `skill.load` / `skill.read`；外部工具走 MCP（stdio / http）；均带 `side_effect` 与审批 |
| 权限与会话 | 三档权限（`read` / `write` / `write_free`）→ scopes + 审批旁路的单一映射；会话可选工作区（沙箱） |
| 会话管理 | append-only 事件日志、上下文分区（固定头 / 摘要 / 活跃区）、自动压缩、首轮自动标题 |
| 技能系统 | `SKILL.md` 渐进式披露；zip 上传（自动识别结构、冲突可覆盖）；逐技能启停 |
| 配置系统 | `harness.yaml` 结构化配置 + `.env` 密钥 + 环境变量覆盖；控制台可写盘 / 重载 |
| 观测 | span 记录，JSONL sink（默认）或 OTel 导出（可选 extra） |

## 目录结构

```
agent/
├─ main.py                  # 入口：读 harness.yaml 的 server 段启动服务
├─ harness.yaml             # 结构配置（模型、工具开关、MCP 列表、观测、server 段）
├─ .env.example             # 密钥与可选环境变量示例（复制为 .env）
├─ harness/                 # Agent 运行时框架（纯库）
│  ├─ config/ agent/ session/ tool/ skill/ llm/ memory/   # 每个模块都有 README.md
├─ server/                  # FastAPI 服务层
│  ├─ routes/               # sessions / turns / runs / interaction / meta / config / mcp / skill / fs / memory
│  ├─ static/index.html     # 单文件控制台
│  └─ test/
├─ skills/                  # 技能库（每个技能一个目录，如 harness-config/）
├─ docs/                    # 全局设计文档与配置参考
├─ data/                    # 运行期数据：会话日志、traces.jsonl（已 gitignore）
├─ todo.md                  # 进度与缺口
└─ AGENTS.md                # 开发约定（命令 / 架构约束 / 容易踩的坑）
```

## 文档索引

**全局（`docs/`）**

| 文档 | 内容 |
|---|---|
| [agent-tool-system-design.md](docs/agent-tool-system-design.md) | 工具系统设计（注册 / 执行 / 鉴权 / 观测） |
| [agent-loop-design.md](docs/agent-loop-design.md) | Agent Loop 设计 |
| [session-management-design.md](docs/session-management-design.md) | 会话管理与上下文分区 |
| [llm-system-design.md](docs/llm-system-design.md) | 模型接入层（OpenAI 兼容 + 流式） |
| [memory-system-design.md](docs/memory-system-design.md) | 跨会话长期记忆 |
| [skills-system-design.md](docs/skills-system-design.md) | 技能系统 |
| [configuration-system-design.md](docs/configuration-system-design.md) | 配置系统设计（为什么这么设计） |
| [configuration-reference.md](docs/configuration-reference.md) | **配置字段速查**（每个字段怎么改、何时生效） |
| [sandbox-design.md](docs/sandbox-design.md) | 沙箱与路径 / 网络策略 |
| [observability-design.md](docs/observability-design.md) | 观测落地（JSONL / OTel） |
| [api-service-design.md](docs/api-service-design.md) | FastAPI 服务层 |
| [frontend-console-design.md](docs/frontend-console-design.md) | 内置控制台 |
| [harness-config-skill-design.md](docs/harness-config-skill-design.md) | 让模型自己改配置的技能（`skills/harness-config/`） |

**模块**：每个模块根目录的 `README.md`（索引见 [harness/README.md](harness/README.md)、[server/README.md](server/README.md)）。
**进度与缺口**：[todo.md](todo.md)。**开发约定**：[AGENTS.md](AGENTS.md)。

## 测试

测试**全离线**（不触网）：

```bash
pip install -e ".[test]"
pytest                                            # 标准入口（testpaths = harness, server）
pytest harness/config/test/test_config.py::test_name
python -m harness.config.test.test_config         # 每个测试模块自带 main()，无 pytest 也能直跑
```

根 [conftest.py](conftest.py) 提供共享 fixture：用例前后复位进程级全局策略（sandbox / shell / net），
以及临时工作区 `tmp_workspace`。

## 开发约定

见 [AGENTS.md](AGENTS.md)：开发命令、架构约束（依赖方向 / 唯一装配点 / 配置真源优先级）、
以及几个容易踩的坑（技能路由顺序、lifespan 与测试、全局状态泄漏等）。
