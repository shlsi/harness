# server — FastAPI 服务层

> 状态：**已实现（M1–M4）**。设计见 [docs/api-service-design.md](../docs/api-service-design.md)。

## 职责

`harness/` 是库（无状态运行时），`server/` 是**宿主 / 组合根**：
装配 `provider / registry / executor / session`，并把两个进程内回调
（`Context.approval` / `Context.ask`）升级成**跨进程的真实传输通道**。

依赖方向单向：`server → harness`，harness 不感知 HTTP。

## 目录结构

```
server/
├─ app.py             # create_app()：app 工厂 + 路由挂载
├─ state.py           # AppState（组合根）/ build_state(HarnessConfig) / start() / apply_config()
├─ schema.py          # 请求体 Pydantic 模型
├─ errors.py          # 统一错误体 + 异常处理器
├─ deps.py            # StateDep / require_session / require_run
├─ runner.py          # Run + RunRegistry + run_turn 后台任务 + 自动标题 + 会话设置 → 沙箱/权限
├─ sse.py             # LoopEvent → SSE 帧 + usage_dict()
├─ interaction.py     # InteractionRegistry（审批 / 提问的 Future 通道）
├─ routes/            # session / turn / run / interaction / config / memory / mcp / skill / fs / meta
├─ static/            # 内置单文件控制台（index.html）
└─ test/              # 55 条全离线测试
```

## 接口一览

| 分组 | 端点 |
|---|---|
| 会话 | `POST/GET /sessions`、`GET/PATCH/DELETE /sessions/{id}`、`GET /sessions/{id}/messages`、`POST /sessions/{id}/compact` |
| 目录浏览 | `GET /fs/browse`（按会话选工作区时的目录点选；只列名字与类型） |
| 对话 | `POST /sessions/{id}/turns`（`stream` 开关） |
| Run | `GET /runs`（`?status=running` 并发看板）、`GET /runs/{id}`、`GET /runs/{id}/events`（SSE）、`POST /runs/{id}/cancel` |
| 观测 | `GET /runs/{id}/trace`（`?source=memory\|file`，本次 run 的 span 树） |
| 交互 | `POST /interactions/{id}` |
| 配置 | `GET/PUT /config`、`POST /config/reload` |
| 记忆 | `GET/PUT /memory` |
| MCP | `GET /mcp`、`POST /mcp/test`（只探测）、`POST /mcp/reload`、`PATCH /mcp/{name}`（单条启用开关） |
| 技能 | `GET /skills`、`GET /skills/{name}`、`POST /skills`、`PUT/DELETE /skills/{name}`、`PATCH /skills/{name}`（启用开关）、`POST /skills/upload`（zip 上传）、`GET /skills/lint`、`POST /skills/validate`、`POST /skills/reload` |
| 自省 | `GET /healthz`、`GET /tools`（含被禁用者 + `enabled`）、`POST /llm/ping` |
| 前端 | `GET /ui/`（单文件 HTML 控制台，`/` 302 到它） |

`stream: false` → 200 阻塞返回 `TurnResult`；`stream: true` → 202 返回 `RunHandle`（含 `events_url`）。
**会话已有活跃 run 时不再 409**：登记的输入改走**软打断**，无论 `stream` 取值一律 **202**（`interrupt:true`，复用当前 run），
由前端在 `done` 帧后续发（见设计文档 §4.2）。字段与错误码的权威定义见 [docs/api-service-design.md](../docs/api-service-design.md) §4。

> **技能端点的路由顺序**：`/skills/lint`、`/skills/validate`、`/skills/reload`、`/skills/upload` 必须声明在
> `GET /skills/{name}` **之前**，否则 `lint` / `upload` 会被当成技能名。技能未启用（`skills.root` 为空）时
> `GET /skills` 返回 `[]`，写操作返回 **400 `skills_disabled`**。

## 关键机制

### 事件桥（零转换复用 LoopEvent）

`AgentLoop.on_event` 是 harness 预留的口。**每个 run 新建一个 AgentLoop**（Loop 无状态，成本极低），
把 `on_event` 指向 `Run.publish_loop_event`：

```python
AgentLoop(provider, executor, registry, config=..., prompt=..., on_event=run.publish_loop_event)
```

`LoopEvent.type` 经 [`sse.py`](sse.py) 的 `EVENT_NAMES` 映射成 SSE 的 `event:` 名：
`run_started / text_delta / message / tool_finished / step_finished / done / run_error`。

> **`run_error` 不叫 `error`**：`EventSource` 内建的连接错误事件类型就叫 `error`，同名前端无法区分。
> `done` 是**唯一终止事件**（正常 / 取消 / 内部异常都发），前端只需监听它来收流。

### Run 与连接解耦

POST 建 run（后台 `asyncio.Task`）→ GET 拉流。客户端断开**只移除订阅者，不取消 run**；
agent 继续跑完并落库，客户端可凭 `Last-Event-ID`（或 `?after=<seq>`）重连补齐。
`Run.stream()` 先重放内存缓冲再续实时流，快照与订阅注册之间无 `await`，因此不丢帧不重复。

### 审批与提问

```python
# runner._approval_hook
iid, future = state.interactions.open()
run.publish("interaction_required", {"id": iid, "kind": "approval", "tool": ..., "args": ...})
return await state.interactions.wait(iid, future, default=False)   # 超时 → 拒绝（安全默认）
```

客户端 `POST /interactions/{id}` `{"approved": true}` → `future.set_result` 唤醒。
一次消费：重复回填返回 404。

### 并发

同一会话同时只允许一个 run：已有活跃 run 时新 `POST /turns` **不再 409**，而是登记为该 run 的**插话**（软打断，202）；
`AgentLoop(should_interrupt=…)` 在当前工具跑完后提前收尾（`stop_reason="interrupted"`），`done` 帧带回插话由前端续发。
`run_conflict` 仅剩「run 已终态但尚未 `release`」的极小收尾窗口兜底（`finish` → `release` 之间无 await）。
run 启动后**重新 `manager.open()`** 拿最新会话状态，保证串行一致。
`RunRegistry` 另有 `active_count()` / `list_active()`（供 `/runs` 与 `/healthz`）与
`set_ttl()`（配置热重建时更新回收 TTL）；`InteractionRegistry.set_timeout()` 同理。
轻量开关端点的读-改-写由 `AppState.config_lock`（`asyncio.Lock`）串行化，避免并发丢更新。

### 配置热重建（`apply_config`）

`AppState.apply_config(config, *, reload_mcp=True)` **原地重建**各部件（provider / registry /
executor / manager / prompt / tracer / title 生成器），**不换 `AppState` 对象**——这样路由通过
`StateDep` 拿到的引用始终有效。流程：先建新部件（`ConfigError` 在此抛出，旧状态无损）→ 取消后台任务、
关闭旧 MCP → 逐个替换 → 重置进程级 shell / sandbox / net 策略 → 更新 runs/interactions 的 TTL 与超时 →
按需重连 MCP（连不上只记 warning）。`PUT /config` / `POST /config/reload` 即它的两个出口（见
[docs/api-service-design.md](../docs/api-service-design.md) §4.7 / §8）。

`AppState.reload_mcp()` 是它的**轻量版**（`POST /mcp/reload` 出口）：关旧连接 → 用当前 `state.config`
重建 registry / executor（清掉已删 server 的残留工具，**不动 provider**）→ 重连；与 `apply_config`
共用同一段重连逻辑。控制台「保存并重连」走的是前者（写盘 + 全量重建），只有想丢掉残留工具时才是后者。

技能 / MCP 的**启用开关**（`PATCH /skills/{name}` / `PATCH /mcp/{name}`）比 `reload_mcp` 更轻：只翻
`harness.yaml` 的一个字段（`skills.disabled` / `mcp.servers[].enabled`）并只热重建对应部分（技能重扫过滤面 /
MCP 重连），**不重建 provider、不要求无活跃 run**；读-改-写由 `AppState.config_lock` 串行化。

## 启动

```bash
pip install -e .        # 依赖见根目录 pyproject.toml
python main.py          # 读 harness.yaml 的 server.host/port；--port / --reload 可覆盖
# 内置控制台：http://127.0.0.1:8000/ui/
```

配置由 [`harness/config`](../harness/config/README.md) 统一管理：

- **结构**写在项目根 [`harness.yaml`](../harness.yaml)（模型、工具开关、shell 白名单、MCP 列表、观测、server 段）
- **密钥**写在 `.env`（`harness.yaml` 里以 `${OPENAI_API_KEY}` 引用），**不要提交仓库**
- 临时覆盖用环境变量（`HARNESS_MODEL` / `HARNESS_DATA_DIR` / `HARNESS_CONFIG` …）

`create_app(config)` 接受 `HarnessConfig`；缺省等价于 `create_app(load_config())`。

MCP 连接发生在 lifespan：`await state.start()`（`build_state` 是同步的，
因为测试用的 `httpx.ASGITransport` 不触发 lifespan）。

lifespan 还负责模型连接的**预热 / 空闲保活**（`state.start_warmup()` / `state.start_keepalive()`）：
启动时打一次 mini ping 建好连接池；`server.keepalive_s > 0` 时再按间隔、且**空闲（无活跃 run）**时继续保活。
两项**默认关闭**，且只对**支持前缀缓存的 provider**才有实际收益——对不做缓存的 provider（如 agnes），
其 TTFB 瓶颈在服务端 prefill + 排队，连接池那点收益会被淹没。预热失败静默。

## 内置控制台

`server/static/index.html`，单文件、零依赖，与 API 同源（无需 CORS）。
访问 `http://127.0.0.1:8000/ui/`；设计与交互细节见
[前端控制台设计文档](../docs/frontend-console-design.md)。

**三栏布局**：左侧会话列表（新建 / 搜索 / 双击重命名 / 悬停项内「重命名 ✎ / 删除 ✕」按钮 / 运行中脉冲点），中间对话区
（标题 / 权限 chips / 消息流 / 输入区），右侧 **8 标签抽屉**：

| 标签 | 内容 | 主要端点 |
|---|---|---|
| 用量 | 本轮 token、会话累计 `usage_totals`、`max_total_tokens` 预算 | `GET /sessions/{id}` |
| 配置 | `config`（可编辑）+ `effective`（只读）分列、模型连通性测试、保存 / 重载 | `GET/PUT /config`、`POST /config/reload`、`POST /llm/ping` |
| MCP | 结构化表单增删改 `mcp.servers`、**「粘贴 JSON」导入 Claude/Cursor 风格的 `mcpServers`（同名覆盖、其余追加，仅填充表单）**、逐条「测试连接」、保存即重连；**卡片开关一键启用 / 停用**（只翻 `enabled` 并就近重连，不必保存） | `GET /mcp`、`POST /mcp/test`、`POST /mcp/reload`、`PATCH /mcp/{name}` |
| 技能 | 技能列表 + 编辑器（新建 / 编辑 / 删除）、**逐条启用 / 停用开关**、**zip 上传（冲突时确认后覆盖）**、草稿校验、目录级 lint、重新扫描 | `GET/POST /skills`、`GET/PUT/DELETE /skills/{name}`、`PATCH /skills/{name}`、`POST /skills/upload`、`POST /skills/validate`、`GET /skills/lint`、`POST /skills/reload` |
| 工具 | 工具列表与启停（改 `config.tools.disabled` 后 `PUT /config`） | `GET /tools`、`PUT /config` |
| 记忆 | `MEMORY.md` 整文编辑 | `GET/PUT /memory` |
| 调用链 | 本次 run 的 span 树（递归渲染） | `GET /runs/{id}/trace?source=memory` |
| 状态 | 版本 / 运行时长 / 工具与技能数 / MCP 连接 / 活跃 run | `GET /healthz`、`GET /mcp` |

对话交互：

- 顶部**会话设置**：`工作区`（点按钮弹服务端目录浏览 `/fs/browse` 逐级点选，也可手填绝对路径）
  + `权限` 三档下拉（只读 / 读写·需批准 / 读写·免批准）。两者按会话保存在会话 `metadata` 里，
  改动即 `PATCH /sessions/{id}` 落库；有 run 在跑时被 409 挡下并提示。
- 会话设了 `permission` 档位时，`POST /turns` 的 `scopes` 传空数组，**由服务端按档位推导**
  （单一真源在 `harness/tool/permission.py`，前端只传档位名）
- `text_delta` → 打字机逐字输出；`message` → 整段文本兜底校准；流式期间纯文本渲染，`done` 时一次性 Markdown
- `tool_finished` → 工具结果卡片；`interaction_required` → 审批 / 提问卡片，点按钮即 `POST /interactions/{id}` 回填
- **用量与上下文面板**：展示四段分区（system / head / summary / active）条数与**估算** token、距压缩水位线余量；
  可**手动压缩**（`POST /sessions/{id}/compact`）；打开会话时用 `active_run_id` 自动挂上仍在跑的 run
- SSE 断线按 `Last-Event-ID` / `?after=<seq>` 指数退避重连

> 权限域与审批是**两道门**：scopes 决定「能不能用这个工具」，审批决定「这一次允不允许」。
> 没勾 `shell:run` 时 `shell.run` 直接返回 `permission_denied`（并被回喂模型）；勾了之后仍需在卡片上点「批准」。

## 测试

```bash
python -m server.test.test_api   # 或 pytest server/test
```

55 条测试**全离线**（`FakeProvider` + 内存会话 + `httpx.ASGITransport`），覆盖：
会话 CRUD、非流式 / 流式对话、SSE 事件顺序与文本增量拼接、`after` 续传、
run 冲突（改名 / 压缩被 409）、审批放行与拒绝（拒绝以结构化错误回喂模型）、取消、自省端点、错误体形状、
LLM 错误不进 HTTP 状态码（仍 200，错误在 `result.error`）、
循环保护（`loop_detected` 沿 SSE 到前端）、
长期记忆（记忆进 system / `memory.save` 经服务写盘 / 工具出现在 `/tools`）、
会话改名与手动压缩（`PATCH` / `POST …/compact`、run 中 409、`usage_totals` 累计）、
配置读写与热重建（`GET/PUT /config`、`/config/reload`、脱敏与还原、明文密钥 422、有 run 409、写盘与备份）、
记忆编辑（`GET/PUT /memory`、超限 400、未启用 409）、`GET /mcp`、`POST /llm/ping`、`GET /runs`、
**技能 CRUD 与 lint（`POST/GET/PUT/DELETE /skills`、`/skills/lint`、`/skills/validate`、未启用 400 `skills_disabled`）**、
**技能开关（落盘 + 过滤 + 运行中可切、`skill_not_found` 404 / `config_unavailable` 409）**、
**zip 上传（成功 / 冲突 `skill_conflict` / 覆盖 / slip 被拒 `zip_unsafe_entry`）**、
**MCP 测试与重连（`POST /mcp/test` 缺 name → 422 `invalid_server`；`POST /mcp/reload`）**、
**MCP 开关（落盘 + 声明保留 + `mcp_server_not_found` 404）**、
**软打断（第二次提问 202 复用 run、`done` 帧带 `interrupts`、打断释放挂起审批、无打断时 payload 不含 `interrupts`（回归）、非流式打断 202）**、
**会话设置（PATCH metadata 的 workspace/permission 校验与合并、档位覆盖请求体 scopes、`write_free` 旁路审批、无档位会话仍按请求体授权）**、
**`GET /fs/browse`（目录列举、非法路径 422 `invalid_path`、不返回文件内容）**、
run 级 span 聚合（`/runs/{id}/trace` 的 memory / file 两个来源、截断时文件给全量、缺数据不报错）。

## 已知限制

- run 与事件缓冲在**进程内存**，重启即丢（会话历史仍持久化在 JSONL）。
- 取消后当轮用户消息已落库但无 assistant 回复，下轮会带着这条消息。
- 无鉴权 / 多租户 / 限流。`GET /fs/browse` 等于「任意目录枚举」，与 `PUT /config` 同风险等级——靠默认 `server.host=127.0.0.1` 兜底，别暴露公网。
- `POST /skills/upload` 等于「任意 zip 解包落盘」（在 `skills.root` 内），与 `PUT /config` **同风险等级**——靠默认 `server.host=127.0.0.1` 兜底，别暴露公网。
- 会话级工作区**不约束 MCP 工具**：MCP 由外部 server 自己访问文件，不走沙箱（默认也无 scopes / 无审批）；要收口请给 MCP server 配 `default_scopes` / `default_requires_approval`。
