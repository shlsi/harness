# harness TODO

> Agent 运行时框架的缺口清单与推进顺序。
> 相关文档：Agent Loop [harness/agent/README.md](harness/agent/README.md) · 会话管理 [harness/session/README.md](harness/session/README.md) · 工具系统 [harness/tool/README.md](harness/tool/README.md) · 技能系统 [harness/skill/README.md](harness/skill/README.md) · 模型接入 [harness/llm/README.md](harness/llm/README.md) · 服务层 [server/README.md](server/README.md) · 全局设计 [docs/](docs/)

**当前一句话结论**：核心链路、服务化、配置化、可观测、安全边界都已打通——`server/`（FastAPI + SSE）把「HTTP → 会话 → 模型 → 工具 → 回填 → 持久化 → 流式回推」串起来，审批与提问有真实的跨进程通道，配置收敛到 `harness.yaml`，每次 run 的 span 能聚合成树并导出到 JSONL / OTel，`fs.*` / `shell.run` / `net.*` / `code.run` 全部收在工作区边界与域名黑名单之内。
剩下两块：**工程化**（CLI 入口）、**能力扩展**（搜索 / 子 agent / 长驻进程 / OS 级隔离）。

---

## 已完成

- [x] 工具系统：契约 / 注册 / 执行管线（校验 / 鉴权 / 超时 / 重试）/ 结果归一化 / 追踪
- [x] 工具系统：MCP 外部工具接入（官方 mcp SDK，stdio + streamable-http）
- [x] 内置工具 P0：`fs.read/write/edit/list/glob/grep`、`shell.run`、`todo.write`、`ask_user`
- [x] 内置工具 P1/P2：`net.fetch/download`、`fs.mkdir/copy/move/delete`、`time.now`、`code.run`
      （设计见 [docs/builtin-tools-design.md](docs/builtin-tools-design.md)；`net.search` 待定 provider）
- [x] 技能系统 M1–M4：解析 / 扫描 / catalog / 加载 / 资源读取 / 路径穿越防护 / `skill.load` `skill.read`
- [x] 技能系统 M5：`skill lint` 静态检查（函数 + 命令行入口）+ `reload()` 测试 + BOM 容错
      （设计见 [docs/skills-system-design.md](docs/skills-system-design.md)）
- [x] 模型接入 M1–M4：OpenAI Chat Completions 兼容 + 官方 SDK + 多模态 + 异常映射 + 遥测上报 + **流式（`astream` / `StreamAccumulator`）**
- [x] Agent Loop M1–M5：循环 / 停止条件 / 事件回调 / 审批回喂 / Prompt 组装（含 skill 目录）/ **流式 `text_delta`**
- [x] 会话管理 M1–M5：三段分区 / append-only JSONL 事件日志 / 重放恢复 / 压缩 / `run_turn`
- [x] **FastAPI 服务层 M1–M4**：会话 CRUD / 对话（`stream` 开关）/ SSE 事件流（`Last-Event-ID` 续传）/ 审批与提问回填 / 自省。
      设计见 [docs/api-service-design.md](docs/api-service-design.md)，模块见 [server/README.md](server/README.md)
- [x] **配置系统 M1–M4**：`harness.yaml` + `${ENV}` 插值 → 校验 → 装配（唯一装配点）；
      覆盖**工具开关**（含只读模式）、**shell 白名单可配**、**沙箱工作区与路径权限/域名黑名单**、
      **网络体积上限**、**MCP server 列表**、**观测 sink（JSONL）**。
      设计见 [docs/configuration-system-design.md](docs/configuration-system-design.md)，模块见 [harness/config/README.md](harness/config/README.md)
- [x] 测试：tool 80、skill 61、llm 27、agent 43、session 35、memory 17、config 57、server 55 = **375 条，全部离线**
- [x] **前端控制台 / 配置中心 / 会话可观测**：`server/static/index.html` 从调试壳重写为完整控制台
      （三栏 + **8 标签抽屉**：用量 / 配置 / MCP / 技能 / 工具 / 记忆 / 调用链 / 状态）。
      **配置读写 + 落盘 + 原地热重建**（`GET|PUT /config`、`POST /config/reload`；脱敏 / 还原 / 校验 / 备份 `.bak`，
      明文密钥只放 `.env`）；**会话可观测**（改名 `PATCH /sessions/{id}`、详情扩展 `context` / `compaction` / `budget` / `usage_totals`、
      手动压缩 `POST …/compact`）；**LLM 首轮自动标题**（`TitleGenerator` + `fallback_title`）。
      另有 `GET|PUT /memory`（记忆整文编辑）、`GET /mcp`、`GET /runs`、`POST /llm/ping`、扩展的 `/healthz` 与 `/tools`（含被禁用工具 + `enabled`）。
      前端设计见 [docs/frontend-console-design.md](docs/frontend-console-design.md)，
      接口见 [docs/api-service-design.md](docs/api-service-design.md) §4、配置见 [docs/configuration-system-design.md](docs/configuration-system-design.md) §9
- [x] **MCP / 技能交由前端自由配置**（承接上一条：从「只读列表 + 改 JSON」升级为可增删改）
      - **MCP**：结构化表单（transport / command / url / args / env / scopes / tool_overrides…）、逐条
        **测试连接**（`POST /mcp/test`：临时 registry 起一次 20s 连接，只探测不落盘）、保存即重连
        （`PUT /config {reload:true}`）、`POST /mcp/reload` 轻量重连（`AppState.reload_mcp()`，不动 provider）
      - **技能**：全 CRUD（`POST /skills`、`GET|PUT|DELETE /skills/{name}`）+ 草稿校验 `POST /skills/validate`
        （**不落盘**）+ 目录级 `GET /skills/lint` + `POST /skills/reload`；写入层新增
        [harness/skill/writer.py](harness/skill/writer.py)（名称正则 + `is_relative_to(root)` 双重防越界、
        落盘前自校验、覆盖前 `.bak`、`.tmp` + `os.replace` 原子替换）
      - 新增错误码 `skill_not_found`(404) / `skill_invalid`(400) / `invalid_server`(422)；
        技能未启用仍为 **400 `skills_disabled`**（沿用既有契约，不改 409）
      - 浏览器验收修复：`init()` 里 `loadConfig()` 必须早于首次渲染，否则停在 MCP / 技能标签刷新时
        残留「配置不可用」占位符
      - 技能设计见 [docs/skills-system-design.md](docs/skills-system-design.md) §M6，接口见
        [docs/api-service-design.md](docs/api-service-design.md) §4.9 / §4.10
- [x] **会话级工作区 + 三档权限**（把「沙箱边界」与「能干什么」从进程级配置升级为会话可选项）
      - **工作区**：会话 `metadata.workspace` 指向一个目录，该会话内 `fs.*` / `shell.run` / `code.run`
        的边界即此目录；`metadata.permission` 三档 **只读 / 读写·需批准 / 读写·免批准**
        （`harness/tool/permission.py` 是档位 → scopes + 是否旁路审批的**单一映射源**）。
        继承全局 `deny_paths` / 挂载点 / 域名黑名单（安全基线不因换工作区失效），不继承 `write_paths`。
      - **沙箱两级作用域**：`_policy` 仍是进程级默认（装配期写入），新增 `contextvars.ContextVar`
        `_override` 承载**每调用覆盖**；`current_sandbox()` 取「覆盖优先」，`use_sandbox(policy)` 上下文管理器
        在 `Executor._run_tool` 包住执行。`asyncio.to_thread` 会 `copy_context()`，`execute_tool_calls` 的
        `gather` 每调用独立 Task → `set`/`reset` 天然按工具隔离，并发不串味。`Context.sandbox` 随 `derive_child` 透传。
      - **免批准**落在宿主层：`server/runner.py::_approval_hook(bypass=True)` 返回恒真 hook 并记一条
        `tool="approval.auto"` 诊断 span（**不动 `Policy`**——Policy 只声明「需不需要审批」，「是否真弹卡片」是宿主决策）。
      - **接口**：`PATCH /sessions/{id}` 支持 `metadata`（改名 + 会话设置合并产出事件）；非法值
        422 `invalid_workspace` / `invalid_permission`；新增 `GET /fs/browse`（服务端目录点选，只列名字与类型，
        路径父级由服务端 `parent` 计算——前端自算会把 `C:\Users` 算成裸 `C:` 而串到默认工作区）。
      - **前端**：对话区顶部三档下拉 + 工作区选择（目录弹层 + 手填，含「用配置默认」），取代原权限域 chips；
        每会话保存，切会话即切换。浏览器验收修复父目录跳转 bug。
      - 设计见 [docs/sandbox-design.md](docs/sandbox-design.md) §4.1 / §6.1、接口见
        [docs/api-service-design.md](docs/api-service-design.md) §4.8 / §4.11、前端见
        [docs/frontend-console-design.md](docs/frontend-console-design.md)，新增
        [harness/tool/permission.py](harness/tool/permission.py) 与 [server/routes/fs.py](server/routes/fs.py)
- [x] **技能 zip 上传 · 技能/MCP 一键启停 · 对话软打断**（三项能力扩展）
      - **技能 zip 上传**：新增 [harness/skill/pack.py](harness/skill/pack.py)（`install_zip(root, data, *,
        overwrite, limits, known_tools)`）与 `POST /skills/upload`（**raw body**，不引入 `python-multipart`）。
        两种包结构**自动识别**（根层 `SKILL.md` = 单技能；一级子目录各含 `SKILL.md` = 多技能；同时存在 →
        `zip_ambiguous`）。**整包两阶段原子**：先在 root 同级临时目录解包 + `build_meta` + lint 校验（error 级才拒）
        + 算冲突，全部通过才逐个 `os.replace` 进 root，因此「有同名冲突 / 包内有非法技能 / 命中限额」时
        root 里一个字节都没写；同名冲突默认拒绝（409 `skill_conflict` + 冲突名单，前端确认后带
        `?overwrite=true` 重传，覆盖时旧 `SKILL.md` 备份为 `.bak` 且不删旧资源）。安全面覆盖
        **zip slip**（绝对路径 / 盘符 / `..` / ADS `:` / 结尾 `.`或空格 / 保留设备名 / Windows 非法字符 /
        层级超限）、**符号链接与特殊文件**（类型位）、**zip bomb**（条目数 + 声明总量 + 单文件 + 压缩比 +
        有界实读）、**大小写覆盖冲突**、噪声条目（`__MACOSX`、`.DS_Store` 等）与 BOM 容错。
        `writer._safe_dir` 提升为公开 `safe_skill_dir`，保持「路径穿越只有一个入口」。
      - **技能启停**：`harness.yaml` 新增 `skills.disabled: [name]`（持久真源）+ `SkillRegistry(disabled=...)`
        / `all_names()` / `is_enabled()` / `disabled_names()` / `set_disabled()`。**停用只过滤"曝光面"**——
        `scan()` 照旧全扫（管理端点与前端面板仍看得到停用项，否则"重新启用"无从下手），
        `names()` / `catalog()` / `load()` / `read_resource()` 才排除（`catalog` 对显式传名也过滤，防旁路）；
        `get()` 不过滤，`load/read` 抛 `SkillDisabled`（不复用 `SkillNotFound`，因为错误文本会回喂模型）。
        热切换**无需重建任何部件**（`PromptBuilder` 与 `skill.*` 工具都持 registry 引用，下一轮 run 立即生效）。
      - **MCP 启停**：`harness.yaml` 新增 `mcp.servers[].enabled`；运行期 `MCPServerConfig` **刻意不带**
        该字段（它是传输描述符），是否连接是宿主决策（`AppState._enabled_mcp_configs()` 在 `start()` 前过滤，
        `mcp_configs` 保持"全量声明"语义供 `/mcp` 与 `healthz` 显示）。
      - **轻量开关端点**：`PATCH /skills/{name}`、`PATCH /mcp/{name}`（body `{enabled}`）——只翻一个字段 +
        只热重建对应部分（技能重扫 / MCP 重连），**不碰 provider、不要求无活跃 run**（与 `PUT /config` 的
        409 `config_reload_conflict` 形成对照）。新增 `AppState.patch_config()` + `config_lock`（防并发丢更新）
        与 `SkillDisabled` / `mcp_server_not_found`(404) 等码；`/healthz` 新增 `skills_enabled`
        （`skills` 口径改为"含停用"）。
      - **对话软打断**：模型回答、工具执行中用户再次提问时，`POST /sessions/{id}/turns` **不再 409**，
        而是登记为该 run 的插话并返回 **202** `{interrupt: true, run_id: <当前 run>, released_interactions}`；
        `AgentLoop` 在 **`STEP_FINISHED` 之后、循环保护判定之前**检查插话探针
        （`should_interrupt` 回调，异常一律视为 False），命中即 `StopReason.INTERRUPTED` 收尾——**用户意图优先**，
        且此刻 assistant(tool_calls) 与 tool 消息已成对，**部分历史照常落库**（下一轮会话合法；对照硬取消
        `task.cancel()` 不落库）。`done` 帧在 `interrupts` 非空时带回排队输入（无插话时 payload 与改造前逐字节一致），
        由前端按顺序续发。**能力边界**：协作式，无法打断正在进行的模型生成与正在执行的工具
        （`gather` 不加取消）——这正是"等工具跑完"的要求。打断同时**释放该 run 上挂起的审批/提问**
        （默认 `False`/`""`，`InteractionRegistry.resolve_for_run`），否则会卡满 300s 超时。
        `Run.status` 不新增取值（非流式分支只认 `finished`/`cancelled`，新增会让打断变 500）。
      - **顺手修的既有 bug**：跨任务关闭 MCP 长连接时 anyio 抛
        `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`，
        导致「重连 / 停用 MCP」首次点击即 500。改为 `MCPClient.aclose()` 吞掉并回告警、
        `LoadedServers.aclose()` 逐个关闭不因单个失败中断。
      - **前端**：技能面板「⇪ 上传 zip」+ 逐行启停开关（冲突时 `confirmDialog` 后自动重传）；
        MCP 卡片头逐 server 开关（表单带 `enabled`，所以「保存并重连」不会把它重置）；对话区运行中按钮
        变「打断并发送」，`done` 后自动续发排队输入并提示「已打断 · 等你的新输入」。
      - 设计见 [docs/skills-system-design.md](docs/skills-system-design.md) §M7、
        [docs/agent-loop-design.md](docs/agent-loop-design.md) §6.2、
        [docs/api-service-design.md](docs/api-service-design.md) §4.2 / §4.9 / §4.10 / §12、
        [docs/configuration-system-design.md](docs/configuration-system-design.md) §9.6、
        [docs/frontend-console-design.md](docs/frontend-console-design.md)；新增
        [harness/skill/pack.py](harness/skill/pack.py)；测试 328 → **375**

---

## P0 核心路径（已完成）

> 关键路径是「LLM Provider + Prompt 组装 + Agent Loop」，已全部落地。

- [x] **Agent Loop**：已完成（[harness/agent](harness/agent/README.md)，M1–M5：循环 / 停止条件 / 事件回调 / 审批回喂 / 流式；
      设计见 [docs/agent-loop-design.md](docs/agent-loop-design.md)）
- [x] **LLM Provider 抽象**：已完成（[harness/llm](harness/llm/README.md)，OpenAI Chat Completions 兼容 + 官方 SDK + 多模态 + 流式；
      设计见 [docs/llm-system-design.md](docs/llm-system-design.md)）
- [x] **Prompt 组装**：已完成（`AgentLoop` 的 `PromptBuilder`，把 `SkillRegistry.catalog()` 注入 system prompt）
- [x] **Session / 消息管理**：已完成（[harness/session](harness/session/README.md)，M1–M5：三段分区 + append-only JSONL
      事件日志 + `run_turn()` 薄封装；设计见 [docs/session-management-design.md](docs/session-management-design.md)）

## P1 服务与交互（已完成）

- [x] **FastAPI 服务层**：HTTP / SSE 端点（[server/](server/README.md)，M1–M4；
      设计见 [docs/api-service-design.md](docs/api-service-design.md)）
- [x] **审批与提问的真实传输**：`InteractionRegistry` 把 `Context.approval` / `Context.ask`
      桥接成「SSE `interaction_required` 事件 + `POST /interactions/{id}` 回填」；
      `shell.run` 审批与 `ask_user` 在真实服务里可用

## P2 增强

- [x] **循环保护**：`max_steps` / `token_budget` 已有；**重复调用检测**已补（`LoopGuard`，
      同一工具以相同参数累计 3 次提醒模型、6 次终止 `loop_detected`）。
      设计见 [docs/agent-loop-design.md](docs/agent-loop-design.md) §6.1，模块见 [harness/agent/README.md](harness/agent/README.md)
- [x] **观测落地**：JSONL sink（配置系统 M4）+ **请求级 span 聚合** + **trace 读侧** + **OTel 桥接**（可选依赖）。
      run 边界设「当前 trace / 根 span」，`llm.complete` 与工具 span 自动挂到 `agent.run` 下成树；
      `GET /runs/{id}/trace`（`?source=memory|file`）、`done` 事件带 `trace_id`、
      `read_spans/read_tree` 离线读日志、`tracer_sink: otel` 经 OTLP 导出。
      设计见 [docs/observability-design.md](docs/observability-design.md)，模块见 [harness/tool/README.md](harness/tool/README.md) · [harness/config/README.md](harness/config/README.md)
- [x] **沙箱**：**工作区边界 + 路径权限**（`harness/tool/sandbox.py`，**默认关 / opt-in**）。
      单工作区 `root` 作为唯一边界（判定前 `resolve()`，解 `..` 与符号链接），
      root 内默认可读写、`write_paths` 可收窄为写白名单、`deny_paths` 黑名单最高优先（读写都拒）、
      root 外可用 `readonly_mounts` / `writable_mounts` 额外挂载；
      `fs.*` 全部接入（`glob`/`grep`/`list` 越界结果静默丢弃），
      `shell.run` 收敛 `cwd` + 扫描命令串里的路径 token；
      越界返回新错误类型 `sandbox_denied`（不重试，带 hint 回喂模型自纠）。
      shell 白名单/禁止模式仍走 `harness.yaml`（进程级）；**非 OS 级隔离**（限制见文档 §8）。
      设计见 [docs/sandbox-design.md](docs/sandbox-design.md)，模块见 [harness/tool/README.md](harness/tool/README.md)
- [x] **Memory**：跨会话长期记忆——**一个 `MEMORY.md` 文件**（不上向量库），
      内容全量注入 system prompt；`memory.save` / `memory.forget` 两个工具（去重 / 上限 / 原子写）；
      压缩时顺带抽候选记忆（默认只记 span）。
      设计见 [docs/memory-system-design.md](docs/memory-system-design.md)，模块见 [harness/memory/README.md](harness/memory/README.md)
- [x] **P1 工具**：`net.fetch`（HTML→纯文本、重定向逐跳复查、体积上限）、
      `fs.move/copy/mkdir`（复用沙箱 `resolve_path`）、`time.now`。
      `net.search` **本期不做**（需第三方搜索 API key，无 key 即死代码、无法离线测）。
      设计见 [docs/builtin-tools-design.md](docs/builtin-tools-design.md)，模块见 [harness/tool/README.md](harness/tool/README.md)
- [x] **P2 工具**：`code.run`（**应用层沙箱**：临时脚本与 cwd 限定工作区内、解释器走 shell 白名单、
      **子进程环境变量 allowlist 清理**、不经 shell 执行、临时目录必清）、
      `fs.delete`（destructive + 强制审批，目录需显式 `recursive`，拒绝删工作区根）、
      `net.download`（沙箱校验落盘、超限中止不留半成品、原子替换）。
      **网络域名**：默认放行 + SSRF 基线黑名单（`localhost` / `169.254.169.254` / `*.internal` 等**始终拒绝**），
      配置只能追加（`tools.sandbox.denied_domains`）；重定向逐跳复查。**非 OS 级隔离**。
      设计见 [docs/builtin-tools-design.md](docs/builtin-tools-design.md)

## P3 工程化

- [x] **依赖声明**：已写进 [pyproject.toml](pyproject.toml)（`pip install -e .` / `pip install -e ".[test]"`）
      — 运行时只列直接 `import` 的包：`pydantic` / `jsonschema` / `PyYAML` / `openai` / `mcp` /
      `fastapi` / `starlette` / `sse-starlette` / `uvicorn`；可选 `test`（`pytest` / `httpx`）与
      `otel`（`opentelemetry-*`）两个 extra；`requires-python = ">=3.10"`。
      **注意 `pytest` 当前已安装**（本机 `pip install pytest` 后 `pytest` 全量 375/375 通过）；
      `pip install -e ".[test]"` 会一并装 pytest + httpx
- [x] **测试基础设施**：新增仓库根 [conftest.py](conftest.py) 与 pytest 配置（[pyproject.toml](pyproject.toml)
      `[tool.pytest.ini_options]` 补 `addopts = "-ra"`）。共享 fixture：autouse 的
      `isolate_process_globals`（用例前后复位进程级全局策略 sandbox / shell / net，防止失败用例
      把脏状态泄漏给后续用例——pytest 不会像手写 `main()` 那样首败即停）与 `tmp_workspace`
      （临时工作区目录）。**两条运行路径并存**：`python -m <module>` 零依赖直跑仍保留，
      `pytest` 为标准入口；测试正文未改动。
- [x] **入口**：根目录 `main.py`（读 `harness.yaml` 的 `server.host/port`，`--host/--port/--reload` 可覆盖；内置 `/ui` 单文件调试前端）；CLI 待做
- [x] **M5 遗留**：`skill lint` 校验器（`harness/skill/lint.py`：函数 + `python -m harness.skill.lint` 入口，
      9 个 code、error/warning 分级、有问题退出码非 0；`unknown-tool` 默认不查以免误报）；
      `SkillRegistry.reload()` 补测试（新增/删除生效、正文缓存清空、warnings 重置、根目录缺失不炸）。
      顺带修掉真机发现的 BOM 隐患：`SKILL.md` 改为按 `utf-8-sig` 读
      （Windows 编辑器写的 BOM 会让 frontmatter 判定失败、skill 静默加载不了）。
      设计见 [docs/skills-system-design.md](docs/skills-system-design.md) §M5 交付明细
- [x] **技能系统 M5 遗留**：自动热重载 → 落地为 `POST /skills/reload` 端点（命令触发，不加
      `watchdog` 依赖，**不用换会话**：catalog 每轮 run 现读）；技能未启用时 400 `skills_disabled`。
      — 「与 FastAPI 会话集成」**已完成**（catalog 由 `PromptBuilder` 注入 system，`skill.load/read` 随工具注册）
- [x] **压缩器可配置** → 结论：**只保留 `LLMCompressor`**（删除零成本的 `PlaceholderCompressor`；
      摘要质量直接决定长会话可用性，占位说明没意义）。`SessionConfig` 新增 `keep_head` / `keep_tail`
      两端保护（`0` = 不保护，系统提示恒不参与压缩），`Session` 增 `head` 段、`build_messages()` 四段拼装；
      无 provider 时 `build_session` 直接报错（不再静默降级）。
      设计见 [docs/session-management-design.md](docs/session-management-design.md) §3/§7/§11

---

## 待做（控制台构想清单）

> 来源：`前端控制台实现方案.md` §6「你没考虑到的功能（建议，按 价值×成本）」。那份方案的主体已并入
> [docs/frontend-console-design.md](docs/frontend-console-design.md)，这里只保留其中**尚未落地**的构想
> （当时列为 P0 且后来已实现的，用 `[x]` 标出，便于对照）。

**P0 残留**

- [x] 系统提示词可配置：`agent.base_instructions` 已是配置项（控制台「配置」标签可改）
- [ ] 系统提示词**预览**：前端展示 system prompt 的逐段来源与下一轮预览
- [x] 模型连通性测试 `POST /llm/ping`（控制台已有自检按钮）
- [x] 健康检查面板：`/healthz` 扩展（uptime / version / data_dir / MCP / active_runs）
- [ ] 并发运行看板：`GET /runs?status=running` 端点已有，但**前端未接** —— 侧栏「脉冲点 + 一键取消全部」都还没做
- [ ] 会话导出 `GET /sessions/{id}/export?format=md|jsonl`
- [ ] 危险操作分级：`side_effect=destructive` 的视觉分级已有（三档权限 + 权限域），但**会话级「本次批准」白名单**没做
      —— 现在审批是一次性消费，每次都要点
- [x] 离线 / 重连横幅：`navigator.onLine` + `#offlineBanner`

**P1**

- [ ] 消息编辑并重跑 / 分支对话（需新增 `truncate` / `branch` 事件，**动 replay 不变量，风险高**）
- [ ] 权限矩阵编辑
- [ ] 会话置顶 / 归档 / 标签（落在 `metadata`）
- [ ] 跨会话用量趋势 `GET /stats`
- [ ] 服务端日志 tail
- [ ] 速率 / 重试可视化
- [ ] 全文搜索
- [ ] 鉴权：`server.auth_token` + `Authorization`（已在 docs「已知限制」里记为后续项）

**P2**

- [ ] 成本估算（token × 单价表）
- [ ] 消息级删除
- [ ] 非图片附件
- [ ] i18n
- [ ] 快捷键面板
- [ ] 配置变更审计日志
- [ ] 会话 TTL 清理视图

---

## 依赖链

```
LLM Provider ─┐
Prompt 组装 ──┼─→ Agent Loop ─→ server（FastAPI）─→ 审批 / 提问真实可用
Session ──────┘                     └─→ skills catalog 真正生效（也靠 Prompt 组装）
llm.astream ──┘                     └─→ SSE text_delta 真流式
Memory ───────┘（注入 Prompt 组装的 system；压缩时靠 Session 抽候选）
Sandbox ──────┘（收口 fs.* 与 shell.run 的路径；root 之外的挂载点也由此提供）
```

## 建议推进顺序

1. CLI 入口
2. P3 工具（`net.search` 需先定 provider；`agent.dispatch` / `process.*`；真要 OS 级隔离时再开一轮）
