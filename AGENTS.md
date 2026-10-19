# AGENTS.md

## 技术栈

- Python（3.10+）后端 + FastAPI。
- Agent harness 源码在 `harness/`（Agent 运行时框架，纯库、不感知 HTTP）；对外服务层在同级 `server/`。
- 代码注释与文档均为中文，新增内容保持一致。

## 过程文档要求

- 所有文档使用 **Markdown**，文件名要能体现内容（如：设计文档、系统架构图、API 文档）。
- 全局性文档放在 `docs/` 目录下。
- 每个模块的文档放在该模块的根目录下。
- 代码改动要及时更新对应文档。

## 开发命令

```bash
pip install -e ".[test]"        # 运行时依赖 + pytest/httpx；只跑服务用 "pip install -e ."
python main.py                  # 读 harness.yaml 的 server 段；控制台 http://127.0.0.1:8000/ui/
python main.py --port 9000 --reload
pytest                          # 标准入口；testpaths = harness, server，全部离线
pytest harness/config/test/test_config.py::test_name
python -m harness.config.test.test_config   # 每个测试模块自带 main()，无 pytest 也能直跑
```

仓库没有 lint / formatter / typecheck / CI 配置，不要假设存在这些命令。

## 架构约束

- 依赖方向单向 `server → harness`：`harness/` 是纯库、不感知 HTTP；`server/state.py::AppState` 是宿主 / 组合根。
- `harness/config/build.py` 是**唯一装配点**。CLI / server / 测试都走 `load_config()` + `build_*()`，不要另写装配代码。
- `harness/tool/permission.py` 是会话三档权限（`read` / `write` / `write_free`）→ scopes + 审批旁路的**单一映射源**；改档位语义只改这里。
- 配置真源：结构在 `harness.yaml`，密钥在 `.env`（YAML 里只写 `${VAR}` 引用）。优先级：`load_config(env=)` > 进程环境变量 > `.env` > `harness.yaml` > 代码默认值。
- `harness.yaml` 由控制台规范化写入（键序 / 注释会重排，写盘前备份 `.bak`）；改配置优先走 `PUT /config`，别手工排布。
- 沙箱 / shell 白名单 / 网络上限是**进程级全局状态**；沙箱默认关闭（opt-in），关闭时路径语义逐字节不变。

## 容易踩的坑

- 技能路由顺序：`server/routes/skill.py` 中 `/skills/lint`、`/skills/validate`、`/skills/reload`、`/skills/upload` 必须声明在 `GET /skills/{name}` 之前，否则会被当成技能名。
- `build_state()` 是同步的，MCP 连接在 lifespan 里 `await state.start()`。测试用 `httpx.ASGITransport` 不触发 lifespan；注入 `state` 时不会连 MCP。
- 测试全离线、不触网。根 `conftest.py` 的 autouse fixture 在用例前后复位 sandbox / shell / net 全局策略；在 `use_sandbox()` 之外手工 `set()` 会泄漏给同线程后续用例（pytest 不会首败即停）。
- 会话级工作区不约束 MCP 工具（MCP 由外部进程访问文件）；要收口需给 MCP server 配 `default_scopes` / `default_requires_approval`。

## 目录结构

- `harness/{config,agent,session,tool,skill,llm,memory}/`：每个模块的 `README.md` 是模块文档，测试在其 `test/` 子目录。
- `server/{routes,static,test}/`；`server/static/index.html` 是单文件零依赖控制台，与 API 同源。
- 进度 / 缺口看 `todo.md`；配置字段速查 `docs/configuration-reference.md`；控制台内还有 `skills/harness-config/SKILL.md`。
