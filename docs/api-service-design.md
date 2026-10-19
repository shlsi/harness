# FastAPI 服务层设计文档

> 定位：**harness 的对外 HTTP / SSE 服务（组合根）** · 技术栈：**Python + FastAPI + sse-starlette** · 状态：**已实现 M1–M4**（含真流式）
> 模块文档：[server/README.md](../server/README.md)
> 相关：[Agent Loop 设计文档](agent-loop-design.md) · [会话管理设计文档](session-management-design.md) · [工具系统设计文档](agent-tool-system-design.md) · [Skills 系统设计文档](skills-system-design.md) · [LLM 接入层设计文档](llm-system-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

`harness/` 是**库**：无状态的运行时（Loop 无状态、Session 可重放、工具进程内回调）。
`server/` 是**宿主 / 组合根**：负责装配 `provider / registry / executor / session store`，
并把两个「进程内回调」——`Context.approval`、`Context.ask`——升级成**跨进程的真实传输通道**。

这是 [todo.md](../todo.md) P1 的落点：没有服务层，`shell.run` 的审批与 `ask_user` 在真实部署里根本用不了。

### 依赖方向（单向）

```
server  ──依赖──▶  harness
harness ──✗──────▶  server
```

`harness/` 不感知 HTTP，任何模块都不导入 `server.*`。所有 framework 能力通过既有契约进入服务层。

### 非目标（本期不做）

- 不做鉴权 / 多租户 / 限流。
- 不做前端 UI（只提供可被前端消费的接口）。
- 不做分布式：run 状态与事件缓冲在**进程内存**中，多副本需自行加粘性会话。
- 不做 run 状态的持久化（会话历史持久化在 `harness/session` 的 JSONL，run 本身是一次性的）。
- 不做 LLM 流式 delta：`harness/llm` 本期非流式，`model_response` 事件是**整段**文本（见 §5.3 说明）。

---

## 2. 在架构中的位置

```
        ┌──────────────────────── server（本模块） ────────────────────────┐
        │  HTTP 路由 → RunRegistry → 组合根 AppState                        │
        │                     │                                            │
        │   GET /runs/{id}/events ──SSE──▶ 浏览器                           │
        └─────────────────────┬────────────────────────────────────────────┘
                              │ SessionManager.run_turn(session, text, agent, ctx)
        ┌─────────────────────▼────────────────────────────────────────────┐
        │  harness/session  →  harness/agent(AgentLoop)  →  harness/llm      │
        │                              │                                   │
        │                      harness/tool Executor                       │
        │                              │                                   │
        │                Policy.authorize → ctx.approval ─┐                │
        │                ask_user          → ctx.ask     ─┼─▶ 回填到本模块   │
        └────────────────────────────────────────────────┼─────────────────┘
                                                         │
                              POST /interactions/{id} ───┘
```

关键洞察：**Loop 无状态 + `on_event` 回调 + `Context.approval/ask` 注入**——服务层不需要改动 harness 任何一行，
只是「换个宿主来构造 AgentLoop、换个回调实现」。

---

## 3. 目录结构

```
server/
├─ __init__.py
├─ app.py             # create_app()：app 工厂 + 路由挂载
├─ state.py           # AppState：组合根，持有 provider/registry/executor/manager/skill
├─ config.py          # （已删除）配置统一到 harness/config，见 configuration-system-design.md
├─ schema.py          # Pydantic 请求 / 响应模型
├─ errors.py          # 统一错误体 + ServiceError → HTTPException 映射
├─ deps.py            # FastAPI Depends：取 AppState / 取 Session
├─ runner.py          # RunRegistry + Run：后台任务 / 事件缓冲 / 订阅 / 取消
├─ sse.py             # 事件序列化 + SSE 帧编码 + 心跳
├─ interaction.py     # InteractionRegistry：approval / ask 的 Future 通道
├─ static/
│  └─ index.html      # 单文件控制台（/ui），见 frontend-console-design.md
├─ routes/
│  ├─ session.py      # /sessions、PATCH、/compact
│  ├─ turn.py         # /sessions/{id}/turns（stream 开关；已有活跃 run 时软打断 → 202）
│  ├─ run.py          # /runs/{id}、/runs/{id}/events、/runs/{id}/cancel、/runs/{id}/trace
│  ├─ interaction.py  # /interactions/{id}
│  ├─ config.py       # /config（读 / 写 / 重载）
│  ├─ memory.py       # /memory
│  ├─ mcp.py          # /mcp、/mcp/test、/mcp/reload、PATCH /mcp/{name}（启用开关）
│  ├─ skill.py        # /skills 全套（CRUD + lint + validate + reload + zip 上传 + 启用开关）
│  ├─ fs.py           # /fs/browse（目录点选，供选工作区）
│  └─ meta.py         # /healthz、/tools、/llm/ping、/runs
└─ test/
```

---

## 4. 接口设计

统一约定：

- 请求与响应均为 `application/json`（SSE 端点除外）。
- 时间戳统一 **epoch 秒（float）**，与 `SessionMeta.created_at` 一致，避免多套时间格式。
- 错误体统一（见 §4.6）。

> **内置控制台**：`GET /ui/` 提供一个单文件 HTML 控制台——三栏（会话列表 / 对话 / 抽屉）+
> 6 标签抽屉（用量 / 配置 / 工具 / 记忆 / 调用链 / 状态），与 API 同源因此不需要 CORS
> （源码 `server/static/index.html`）。根路径 `/` 会 302 跳到 `/ui/`。
> 设计见 [前端控制台设计文档](frontend-console-design.md)。

### 4.1 会话（Session）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/sessions` | `{title?, system?, metadata?}` | `SessionDetail` (201) |
| GET | `/sessions` | — | `SessionSummary[]` |
| GET | `/sessions/{id}` | — | `SessionDetail` |
| GET | `/sessions/{id}/messages` | `?after=<n>`（可选） | `{messages: Message[]}` |
| PATCH | `/sessions/{id}` | `{title?}` 或 `{metadata?}`（至少一个） | `SessionDetail`（改名 / 会话设置，见 §4.8） |
| POST | `/sessions/{id}/compact` | `{force?:false}` | `CompactResult`（见 §4.8） |
| DELETE | `/sessions/{id}` | — | 204 |

- `POST /sessions` → `SessionManager.create()`；`system` 为 `Message[]`（默认空，由 `PromptBuilder` 注入默认系统提示）。
  `metadata` 里的 `workspace` / `permission` 会**先校验再落库**（与控制台设置同一套规则，见 §4.8）。
- `GET /sessions` → `SessionManager.list()`，直接映射 `SessionMeta`（含累计用量 `usage_totals`）。
- `GET /sessions/{id}` → `SessionManager.open()` 后拼 `SessionDetail`（分区统计 + 上下文占用 + 压缩水位 + token 预算）。
- `GET /sessions/{id}/messages` → `session.build_messages()` 的**只读视图**（固定区+固定头+压缩区+活跃区），用于前端渲染完整上下文。
- `PATCH /sessions/{id}` → 改标题与/或**合并式**改 metadata（各产一条 `meta(…)` 事件）；`POST /sessions/{id}/compact` → 手动压缩。**两者在会话有活跃 run 时均返回 409 `run_conflict`**，详见 §4.8。

```jsonc
// SessionSummary（= SessionMeta.to_dict()）
{ "session_id": "ab12…", "title": "解标", "turns": 3,
  "created_at": 1792308714.2, "updated_at": 1792308811.9,
  "metadata": {},
  "usage_totals": {"prompt_tokens": 3200, "completion_tokens": 640, "total_tokens": 3840} }

// SessionDetail = SessionSummary + 分区统计 + 上下文占用 + 压缩水位 + 预算
{ "...": "同上",
  "active_count": 7, "head_count": 0, "has_summary": true, "system_count": 1,
  "active_run_id": "9f…",                                   // 无活跃 run → null
  "context": {
    "system":  {"count": 1, "tokens": 420},
    "head":    {"count": 0, "tokens": 0},
    "summary": {"count": 0, "tokens": 0},
    "active":  {"count": 7, "tokens": 1680},
    "conversation_count": 7, "conversation_tokens": 1680,   // 固定头 + 活跃区
    "message_count": 8, "total_tokens": 2100                // 实际发给模型的全量
  },
  "compaction": {
    "enabled": true, "compact_threshold": 40, "keep_head": 0, "keep_tail": 16,
    "max_active_tokens": null, "over_threshold": false,
    "remaining_messages": 33,                               // 距条数水位线的余量（可为负）
    "remaining_tokens": null                                // 距 token 水位线的余量（未设上限 → null）
  },
  "budget": {"max_total_tokens": 200000, "used_total_tokens": 3840, "remaining": 196160} }
```

> **`context` 各段的 `tokens` 是估算值**（`estimate_tokens()`：中文≈1 字/token、ASCII≈4 字符/token、图片按固定值），仅用于展示与阈值判断，不追求精确。
> `conversation_*` 只统计**固定头 + 活跃区**（即参与压缩判断的对话消息，不含固定区与压缩区）；
> `total_tokens` 覆盖实际发给模型的全部消息（含固定区 / 压缩区）。
> `budget` 来自会话累计用量（`usage_totals.total_tokens`）与 `agent.max_total_tokens` 预算。

### 4.2 一轮对话（Turn）

```
POST /sessions/{id}/turns
```

请求体：

```jsonc
{
  "input": "帮我看下这个报错",        // string 或 OpenAI 多模态 parts 数组
  "images": [],                     // 可选：附带的图片 URL / data URL
  "stream": true,                   // true → 返回 run 句柄；false → 阻塞返回结果
  "scopes": ["fs:read"],            // 可选：授予本轮的权限域
  "metadata": {}                    // 可选：透传到 Context.metadata
}
```

**`stream` 开关的两种形态（同一端点）：**

| stream | 行为 | 状态码 | 响应 |
|---|---|---|---|
| `false` | 阻塞至本轮结束 | 200 | `TurnResult` |
| `true` | 立即返回 run 句柄，改由 `GET /runs/{id}/events` 拉流 | 202 | `RunHandle` |
| 任意 | **已有活跃 run → 软打断**（登记插话，不新建 run） | **202** | `InterruptHandle` |

```jsonc
// TurnResult（stream=false）
{ "run_id": "9f…", "session_id": "ab12…",
  "final_text": "这是报错原因…", "stop_reason": "final",
  "usage": {"prompt_tokens": 1200, "completion_tokens": 210, "total_tokens": 1410},
  "steps": 3, "error": null,
  "interrupts": [ {"input": "…", "images": []} ] }   // 仅在非空时出现（见下）

// RunHandle（stream=true）
{ "run_id": "9f…", "session_id": "ab12…",
  "events_url": "/runs/9f…/events", "status": "running" }

// InterruptHandle（已有活跃 run）
{ "run_id": "9f…",               // **当前（被插话的）run 的 id**，不是新 run
  "session_id": "ab12…", "events_url": "/runs/9f…/events",
  "status": "running", "interrupt": true,
  "released_interactions": ["2b…", "7c…"] }   // 被顺手释放的挂起交互 id 列表
```

- `input` 为字符串时 → `Message.user(input, images)`；为数组时按 OpenAI `content` parts 原样使用。

**软打断（用户插话）**：当该会话已有活跃 run 时，`POST /sessions/{id}/turns` **不再新建 run、也不再 409**，
而是把本次输入登记为该 run 的**待处理插话**——该 run 会在「当前正在执行的工具跑完之后」提前收尾
（`stop_reason="interrupted"`），并把插话原样放进 `done` 帧，由**前端续发**（每条插话开一个新 run）。

- **返回 202**（见上 `InterruptHandle`）：`Run.status` **没有**新增取值（仍是 `running/finished/error/cancelled`），
  打断只靠 `summary.stop_reason == "interrupted"` 表达——因为 `server/routes/turn.py` 的**非流式分支只认 `FINISHED` 与 `"cancelled"`**，
  新增状态会让打断变成 500。
- **非流式（`stream:false`）也一律 202**：非流式调用方等的是「自己这条输入的答复」，而打断只是排队，
  `await run.done.wait()` 等到的会是**上一条**的答复，返回它反而是错的。
- **`run_conflict`（409）收窄**：仅剩「run 已终态但尚未 `release`」这个极小收尾窗口兜底
  （`finish()` → `finally: release()` 之间**没有 await**，正常到不了这里，见 §7）。
- **打断会顺手释放该 run 上挂起的审批 / 提问**（用各自默认值：审批 `False`、提问 `""`），
  否则模型会卡在审批上等满 `interaction_timeout_s`（默认 300s），用户体感是「打断没反应」。
  响应里的 `released_interactions` 即被释放的 id 列表；被释放后再 `POST /interactions/{id}` 回填返回 **404**（一次消费）。
- **部分历史会落库**：软打断是**正常返回**（不是抛异常），所以 `SessionManager.run_turn` 里的
  `session.record(result, sent)` 照常执行，assistant(tool_calls) 与 tool 结果**成对落库**，下一轮会话合法。
  对照**硬取消**（`POST /runs/{id}/cancel` → `task.cancel()`）：`record` 不执行，本轮新增的 assistant / tool 消息不落库
  （既有行为，见 §7「取消的副作用」）——两种收尾的措辞不要混。
- **能力边界**：软打断是**协作式**的，只在「当前模型回复已结束、其工具已全部执行完」这个时刻生效
  （`AgentLoop` 在 `STEP_FINISHED` 之后、循环保护判定之前检查，用户意图优先）；
  **无法**打断正在进行的模型生成与正在执行的工具（`executor.execute_tool_calls` 是 `gather`，不加取消）
  ——这正是需求要的「等工具跑完」。

### 4.3 Run

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/runs` | 并发看板：`?status=running` → 当前运行中的 run 列表（`run_id` / `session_id` / `status` / `trace_id` / `events`） |
| GET | `/runs/{id}` | 状态快照：`status` / `stop_reason` / `usage` / 已产生事件数 |
| GET | `/runs/{id}/events` | **SSE 事件流**（见 §5） |
| POST | `/runs/{id}/cancel` | 取消：`asyncio.Task.cancel()`，返回 200 `{status:"cancelled"}` |

`GET /runs/{id}` 让非 SSE 客户端（脚本 / 轮询）也能拿到结果，不必实现 EventSource。

`GET /runs` 的 `status` **只支持 `running`**（已结束的 run 按 run_id 查询），其他取值 → 400 `invalid_status`。
它供控制台的并发看板使用：列出每个活跃 run 的会话与事件数，便于观察/取消。

`POST /runs/{id}/cancel` 是**硬取消**（`task.cancel()`）：用户明确「我不要这轮了」，因此 `done` 帧**不带** `interrupts`，
不该被自动续发；本轮新增的 assistant / tool 消息也不落库（见 §4.2 软打断的对照与 §7）。

### 4.4 交互回填（Interaction）

```
POST /interactions/{id}
```

用于回应 SSE 推来的 `interaction_required` 事件（审批 / 提问）。

```jsonc
// 审批
{ "approved": true }
// 提问
{ "answer": "B" }
```

响应：`{ "ok": true }`；若 `id` 不存在或已超时 → 404。

### 4.5 自省与探活（Meta）

| 方法 | 路径 | 响应 |
|---|---|---|
| GET | `/healthz` | 服务探活快照（见下） |
| GET | `/tools` | 全部内置工具 + 当前注册的扩展工具（**含被禁用者**，带 `enabled` 标记） |
| POST | `/llm/ping` | 最小一次模型调用，验证 `base_url` / key / 模型名是否可用（见下） |

> 技能相关端点在 **§4.10 技能（Skills）**，`/runs` 在 §4.3。

`GET /healthz`（探活 + 概况，供控制台状态面板）：

```jsonc
{ "status": "ok", "version": "0.1.0", "uptime_s": 12.5,
  "tools": 36, "skills": 5, "skills_enabled": 4,
  "config_path": "harness.yaml", "data_dir": "data/sessions",
  "mcp": {"connected": 1, "total": 1}, "active_runs": 0 }
```

> **`skills` 口径**：目录里有多少个技能（**含停用**）；另给 `skills_enabled`（已启用数），
> 便于排查「模型为什么看不到某个技能」。`mcp.total` 同样是**声明数**（含停用），`connected` 只算真正连上的。

`GET /tools` **返回全部工具全集**（内置工具 + 当前注册的扩展工具，如 `skill.*` / `memory.*` / `mcp.*`），
**被禁用的工具也会出现**，只是 `enabled:false`——`enabled` 由后端 `is_tool_allowed()` 按 `tools.enabled` / `tools.disabled` 现算：

```jsonc
{ "name": "fs.write", "description": "…", "side_effect": "write", "scopes": ["fs:write"],
  "requires_approval": false, "tags": [],
  "input_schema": {…}, "output_schema": {…},
  "timeout_s": 30, "max_retries": 0, "idempotent": false,
  "concurrency": "serial", "result_max_chars": 20000,
  "enabled": false }
```

`POST /llm/ping`：用 `provider.complete()` 发一次最小调用（`max_tokens=1`）。

```jsonc
// 成功
{ "ok": true, "model": "agnes-3.0-flash", "latency_ms": 231.4, "reply": "…" }
// 失败（连通性测试失败是「结果」而非服务错误：仍返回 200）
{ "ok": false, "model": "agnes-3.0-flash", "latency_ms": 502.1, "error": "…" }
```

### 4.6 错误模型

统一错误体，**所有**非 2xx 响应同构：

```jsonc
{ "error": { "code": "session_not_found",
             "message": "会话不存在: ab12…",
             "details": {} } }
```

| code | HTTP | 触发 |
|---|---|---|
| `invalid_request` | 422 | Pydantic 校验失败（含字段级 details） |
| `session_not_found` | 404 | `SessionNotFound` |
| `run_not_found` | 404 | run_id 未知或已过期回收 |
| `run_conflict` | 409 | 会话已有活跃 run（改名 / 压缩拒绝）；新对话已改走**软打断 202**，此码仅剩极小收尾窗口兜底（见 §4.2） |
| `interaction_not_found` | 404 | interaction_id 未知 / 超时 / 已消费 |
| `internal_error` | 500 | 未捕获异常（不泄露栈，记日志） |
| `skills_disabled` | 400 | 技能未启用（`skills.root` 为空），无法重载 / 增删改 |
| `skill_not_found` | 404 | `SkillNotFound`（按名取 / 改 / 删时目录或 `SKILL.md` 不存在） |
| `skill_invalid` | 400 | 技能名非法 / 路径越界 / frontmatter 与正文校验失败（写操作统一归此码） |
| `invalid_server` | 422 | `POST /mcp/test` 缺 `server.name`（`details.path="name"`） |
| `mcp_server_not_found` | 404 | `PATCH /mcp/{name}` 的 name 未在 `mcp.servers` 中声明 |
| `invalid_workspace` | 422 | 会话设置里的 `workspace` 不是存在的目录（`details.path="workspace"`） |
| `invalid_permission` | 422 | 会话设置里的 `permission` 不是三档之一（`details.path="permission"`） |
| `invalid_path` | 422 | `GET /fs/browse` 的 `path` 不存在 / 不是目录 / 无权限（`details.path` 回显该路径） |
| `config_unavailable` | 409 | app 未持有配置上下文（如测试注入的 state），无法读写配置 |
| `config_invalid` | 422 | 配置非法（YAML 校验失败 / 明文密钥；`details.path` 指向字段） |
| `config_reload_conflict` | 409 | 有 run 正在执行，配置重建会中断它 |
| `memory_disabled` | 409 | 长期记忆未启用（`memory.enabled=false`），不能整文写入 |
| `memory_full` | 400 | 记忆超过 `max_chars`（`details.max_chars`） |
| `invalid_status` | 400 | `GET /runs` 的 `status` 只支持 `running` |

> **注意**：LLM 调用失败**不是** HTTP 错误。`AgentLoop` 一律返回 `stop_reason="error"`，
> 服务层照常 200/SSE `error` 事件返回，`TurnResult.error` 携带结构化信息——保持与 harness 契约一致。

### 4.7 配置（Config）

配置读写与运行时重建。数据加工（脱敏 / 还原 / 排序 / 回写）在
[harness/config/admin.py](../harness/config/admin.py)，装配（重建部件）在
[server/state.py](../server/state.py) 的 `AppState.apply_config()`。语义详见
[配置系统设计文档 §9](configuration-system-design.md)。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/config` | — | `ConfigSnapshot`（无配置上下文 → `editable:false` / `config:null`） |
| PUT | `/config` | `{config, reload?:true}` | `ConfigSnapshot`（含 `warnings`） |
| POST | `/config/reload` | — | `ConfigSnapshot`（从磁盘重读，不写盘） |

```jsonc
// ConfigSnapshot
{ "config_path": "harness.yaml",
  "editable": true,
  "config": { "llm": {"api_key": "${OPENAI_API_KEY}", "model": "agnes-3.0-flash"}, "…": "磁盘原始 YAML（已脱敏，可编辑）" },
  "effective": { "llm": {"api_key": "***", "model": "agnes-3.0-flash"}, "…": "运行时真正生效值（只读，已脱敏）" },
  "env_overrides": [ {"env": "HARNESS_MODEL", "path": "llm.model", "value": "…", "active": true} ],
  "sections": ["llm", "agent", "session", "skills", "memory", "tools", "mcp", "observability", "server"],
  "backup_path": "harness.yaml.bak" }
```

- **脱敏**：敏感字段（`llm.api_key`）里的 `${VAR}` 引用原样保留，其余字面量替换为 `***`；`effective` 同样脱敏。
- **`config` vs `effective`**：前者是磁盘上的原始 YAML（可编辑来源），后者是「插值 + 环境覆盖 + 校验」后运行时真正生效的值。
- **`env_overrides`**：真正生效的环境变量覆盖——这些项**改文件是无效的**（环境层优先级更高）。
- `PUT /config` 提交 `config`；敏感字段提交 `***` / 空 / `null` 表示**保持原值**（后端 `restore_secrets` 还原旧值）。
  成功 → 写盘 + **原地重建**运行时 + 返回同 GET（`warnings` 携带非致命提示，如 MCP 连不上）。
- 冲突与校验：有 run 正在执行 → 409 `config_reload_conflict`；明文密钥或非法值 → 422 `config_invalid`（`details.path` 指向字段）。
- `POST /config/reload`：在编辑器里手改了 YAML 后重载——**只重读磁盘**，不写盘。
- 无配置上下文（测试注入的 state）→ `editable:false` / `config:null`；写配置 → 409 `config_unavailable`。

### 4.8 会话设置与压缩（Session Settings / Compact）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| PATCH | `/sessions/{id}` | `{title?}` / `{metadata?}`（至少一个） | `SessionDetail` |
| POST | `/sessions/{id}/compact` | `{force?:false}` | `CompactResult` |

- `PATCH` 的 `title` → `Session.rename(title)`：只产出一条 `meta(title=…)` 事件，消息序列不动。
- `PATCH` 的 `metadata` → `Session.set_metadata(**kv)`：**合并**语义（未提到的键保持原样），
  产出一条 `meta(metadata=…)` 事件；replay 时对 `session.meta.metadata` 做 `update`。
  控制台的「工作区 + 三档权限」就走这里。
- `POST …/compact` → `SessionManager.compact(session, force)`：手动触发压缩；`force=true` 跳过水位线直接压，`force=false` 等价于自动水位线判断。
- **会话有活跃 run 时两者均 409 `run_conflict`**（改名 / 改设置 / 压缩都会改写会话，不能与进行中的 run 并发）。

两个约定键（其余键原样透传）：

| 键 | 取值 | 校验 |
|---|---|---|
| `workspace` | 绝对路径字符串 | 必须是**存在的目录** → 否则 422 `invalid_workspace`；存 `Path(...).resolve()` 后的规范化路径；空串 = 清除该设置（改回进程级默认工作区） |
| `permission` | `read` / `write` / `write_free` | 必须命中三档之一 → 否则 422 `invalid_permission`；空串 = 清除（则该会话回落为「按请求体的 `scopes` 授权」，即改造前的行为） |

```jsonc
// 请求：改名 + 设置一起改
{ "title": "重构任务", "metadata": { "workspace": "D:/work/proj", "permission": "write" } }
// 响应：SessionDetail，其中
{ "metadata": { "workspace": "D:\\work\\proj", "permission": "write" } }
```

**三档权限 → 运行时行为的映射**（单一真源：[harness/tool/permission.py](../harness/tool/permission.py)）：

| 档位 | 授予的 scopes | 审批 |
|---|---|---|
| `read` | `fs:read`, `net:read` | 无需（没有写 / 危险工具可用） |
| `write` | 上述 + `fs:write`, `shell:run`, `code:run`, `memory:write` | `requires_approval=True` 或 `side_effect="destructive"` 的工具弹审批卡片（即默认行为） |
| `write_free` | 同 `write` | **全部旁路**，不弹卡片；仍记一条 `approval.auto` 诊断 span 供事后审计 |

**优先级（重要）**：`permission` 是**会话级覆盖，高于进程级配置**。

- 设了 `permission` → 本轮 scopes **由档位推导**，请求体里的 `scopes` 被**忽略**（单一真源，避免两个真源打架）。
- 没设 `permission`（含改造前建的旧会话）→ 仍按请求体的 `scopes` 授权，行为与改造前完全一致。

**工作区 → 沙箱策略**由 `server/runner.py::session_sandbox()` 翻译，规则见
[沙箱设计 §6.1](sandbox-design.md)：没设工作区 → `Context.sandbox=None`（沿用进程级默认）；
设了 → 以它为 `root` 开沙箱，并继承全局的 `deny_paths` / 额外挂载点 / 域名黑名单。

```jsonc
// CompactResult
{ "session_id": "ab12…",
  "compacted": true,              // 是否真的压掉了消息
  "forced": true,                 // 是否 force（前端手动压缩）
  "dropped": 24,                  // 从活跃区头部折叠掉的条数
  "summary_chars": 812,           // 压缩区摘要长度（字符）
  "active_before": 31,            // 压缩前活跃区条数
  "reason": "below_threshold",    // 未压缩时的原因：未到水位线；force 且无内容可压 → "nothing_to_drop"
  "warning": "enable_compaction=false，本次为手动强制压缩",   // 仅在强制压缩且压缩关闭时出现
  "detail": { "…": "压缩后的 SessionDetail（同 §4.1）" } }
```

### 4.9 记忆与 MCP（Memory / MCP）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/memory` | — | `MemoryView` |
| PUT | `/memory` | `{content}` | `MemoryView` |
| GET | `/mcp` | — | `McpView` |
| POST | `/mcp/test` | `{server}` | `McpTestResult` |
| POST | `/mcp/reload` | — | `McpView` + `warnings` |
| PATCH | `/mcp/{name}` | `{enabled}` | `McpView` + `warnings`（切换单个 server 的启用开关，见下） |

```jsonc
// MemoryView（MEMORY.md 的查看与整文编辑）
{ "enabled": true, "path": "MEMORY.md", "max_chars": 4000,
  "content": "# 长期记忆\n\n## 用户偏好\n- …",   // 文件原文（未启用 → ""）
  "chars": 128, "remaining_chars": 3872,
  "sections": [ {"title": "用户偏好", "entries": ["…"]} ],
  "entries": ["…"] }

// McpView（声明 + 已连接状态）
{ "loaded": true,                                // lifespan 里 state.start() 之后为 true
  "servers": [ { "name": "filesystem", "enabled": true, "transport": "stdio", "command": "npx", "url": null,
                 "tool_prefix": "mcpfs", "connected": true, "tool_count": 3,
                 "tools": ["mcpfs.read_file", "…"],
                 "declared_side_effect": "read", "requires_approval": false, "scopes": [] } ] }

// McpTestResult（POST /mcp/test：只探测，不落盘、不改运行时）
{ "ok": true, "tools": ["mcpfs.read_file", "…"], "latency_ms": 412, "error": null }
// 失败：{ "ok": false, "tools": [], "latency_ms": null, "error": "Connection closed" }
```

> **`enabled` 来自配置声明**（`mcp.servers[].enabled`；无配置上下文默认 `true`）。`servers` 列**全部声明**（含停用），
> 停用项 `connected:false` 且 `tools` 为空；`healthz` 的 `mcp.total` 是声明数（含停用），`connected` 只算真正连上的。

- `PUT /memory` → `MemoryStore.replace(content)`：**整文替换**（解析 → 校验上限 → 原子替换，失败则文件不变）。
- 未启用长期记忆（`memory.enabled=false`）→ 写 409 `memory_disabled`；`GET` 返回 `enabled:false` 与空内容。
- 超过 `max_chars` → 400 `memory_full`（`details.max_chars`）。
- `POST /mcp/test`：先校验 `server.name` 非空（否则 422 `invalid_server`，`details.path="name"`），再用 `build_dataclass(McpServerConfig, …)` 转成运行时声明（`ConfigError` / `TypeError` → 422 `config_invalid`），随后用**临时** `ToolRegistry` 连接一次（`asyncio.wait_for(…, 20s)`）。连接失败**不是** HTTP 错误，而是 `ok:false` + `error`。
- `POST /mcp/reload`：有活跃 run → 409 `run_conflict`；否则关旧连接 → 用当前 `state.config` 重建 registry / executor（清掉已删 server 的残留工具，**不动 provider**）→ 重连。响应体同 `GET /mcp`，外加 `warnings[]`（逐条连接失败原因）。
- `PATCH /mcp/{name}`（`{"enabled": bool}`）：只翻 `mcp.servers[].enabled` **一个字段**并**只重连 MCP**
  （`AppState.set_mcp_enabled`：先同步 `state.config.mcp.servers` 再 `reload_mcp()`，**不碰 provider / 会话 / loop_config**），
  因此**不重建全部部件、不要求「无活跃 run」**——运行中也能切（但仍会多回一条 warning 提示正在调用的 MCP 工具可能因连接重建而失败）。
  响应体与 `GET /mcp` 同构 + `warnings`；错误：未声明的 server → 404 `mcp_server_not_found`；无配置上下文 → 409 `config_unavailable`；缺 `enabled` → 422 `invalid_request`。
  轻量开关与 `PUT /config` 的分工见 §7。
- **`warnings` 里可能出现「关闭时底层库报错（已忽略）」**：MCP stdio 客户端的 anyio cancel scope 必须在创建它的同一 task 里退出，
  宿主在 lifespan 任务里连接、在请求任务里关闭会触发 `RuntimeError`——现已捕获为告警而非 500，详见 §12「本轮修复」。

### 4.10 技能（Skills）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/skills` | — | `SkillMeta[]`（**含已停用**，每项带 `enabled`；**未启用 → `[]`**，不报错） |
| GET | `/skills/lint` | — | `{issues:[LintIssue], enabled}` |
| POST | `/skills/validate` | `SkillValidateRequest` | `{issues:[LintIssue]}` |
| POST | `/skills/reload` | — | `{skills:SkillMeta[], warnings:[]}` |
| POST | `/skills/upload` | raw zip 字节（`?overwrite=false`） | **201** `{installed, skipped, warnings, skills}`（见下） |
| POST | `/skills` | `SkillCreateRequest` | **201** `{skill: SkillMetaWithBody, issues}` |
| GET | `/skills/{name}` | — | `SkillMetaWithBody` |
| PUT | `/skills/{name}` | `SkillUpdateRequest` | `{skill: SkillMetaWithBody, issues}` |
| PATCH | `/skills/{name}` | `{enabled}` | `{skill: SkillMetaWithBody, disabled:[names]}`（启用开关，见下） |
| DELETE | `/skills/{name}` | — | **204**（无响应体） |

```jsonc
// SkillCreateRequest
{ "name": "release-check", "description": "发版前检查清单…",
  "body": "## 何时使用\n…",            // 缺省为后端模板正文
  "allowed_tools": ["fs.read", "shell.run"], "version": "1.0.0" }

// SkillUpdateRequest：同上但**无 name**（name = 路径参数 = 目录名，不可改）
// SkillValidateRequest：同上但 name 可选（缺省 "draft"，只用于组装 frontmatter 后 lint）

// SkillMetaWithBody
{ "name": "release-check", "description": "…", "version": "1.0.0",
  "allowed_tools": ["fs.read"], "path": "skills/release-check",
  "enabled": true,                                    // 是否启用（停用项不出现在 system prompt catalog）
  "body": "---\nname: …\n---\n\n## 何时使用\n…" }   // 仅详情 / 写操作响应带 body

// LintIssue
{ "skill": "release-check", "level": "warning", "code": "unknown-tool", "message": "…" }
```

- **未启用技能（`skills.root` 为空）**：`GET /skills` 返回 `[]`；**写操作一律 400 `skills_disabled`**（保持既有契约，与 `memory_disabled` 的 409 刻意不同）。
- **启用 / 停用（`PATCH /skills/{name}`）**：只翻 `harness.yaml` 的 `skills.disabled` 一个字段并热切换过滤面
  （`SkillRegistry.set_disabled`），**不重建任何部件、不要求无活跃 run**（运行中也能一键开关，与 `PUT /config` 的 409 `config_reload_conflict` 不同）。
  停用只是把技能移出 system prompt 的 catalog、且不可被 `skill.load` / `skill.read` 读；列表与详情仍能看到（`GET /skills` 含停用项，只是 `enabled:false`）。
  返回 `{skill: {…, enabled}, disabled: [names]}`。错误：技能未启用 → 400 `skills_disabled`；名字不存在（**已停用的也算「存在」**）→ 404 `skill_not_found`；无配置上下文 → 409 `config_unavailable`；缺 `enabled` → 422 `invalid_request`。
- **路由顺序**：`/lint`、`/validate`、`/reload`、`/upload` 必须声明在 `GET /{name}` **之前**，否则会被当成 skill 名。
- **写操作安全边界**（`harness/skill/writer.py`）：`name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`，解析后目录必须落在 `skills.root` 之内（防 `..`/绝对路径/符号链接逃逸）；`build_meta` 强制 `name == 目录名`。越界或非法 → 400 `skill_invalid`。
- 覆盖已有技能时先备份 `SKILL.md.bak`，再 `.tmp` + `os.replace` 原子替换。
- `POST /skills/validate` 把草稿写进临时目录后复用 `lint_skills()`，`finally` 清理——**不触碰真实技能目录**。
- **zip 上传（`POST /skills/upload`）**：请求体是 **raw zip 字节**（**不是 multipart**；故意不引入 `python-multipart` 依赖，
  `Content-Length` 先做早拒）。自动识别两种结构：根层 `SKILL.md`（单技能）或一级子目录各含 `SKILL.md`（多技能），
  两者并存 → `zip_ambiguous`（不猜）。**整包原子**：先在 `root` 同级 staging 里解包 + 校验 + 算冲突，全部通过才逐个 `os.replace` 进 `root`——
  因此有同名冲突且未开 `overwrite` 时 **root 里一个字节都没写**。成功 **201**：
  `{installed: [{name, files, overwritten}], skipped: [噪声条目名], warnings, skills: [...]}`（`skipped` 是 macOS/版本控制产物等噪声项）。
  安全校验与限额在 [`harness/skill/pack.py`](../harness/skill/pack.py)：`zip_*` 系列抛 `PackError(code, message, details)`，
  **HTTP 状态由 server 层映射**（`server/routes/skill.py` 的 `_PACK_STATUS`）。错误码见下表。

**新端点错误码总表**（`/skills/upload`、`PATCH /skills/{name}`、`PATCH /mcp/{name}`）：

| 端点 | code | HTTP | details |
|---|---|---|---|
| `POST /skills/upload` | `skills_disabled` | 400 | `{}`（技能未启用，沿用既有码） |
| | `zip_invalid` | 400 | `{}`（不是 zip / 解压失败） |
| | `zip_too_large` | 413 | `{"limit": "zip_bytes\|entries\|total_bytes\|file_bytes\|skills\|ratio", "max": N, "actual": M}` |
| | `zip_unsafe_entry` | 422 | `{"entry": "<原样条目名>", "reason": "absolute\|drive\|ads\|traversal\|bad_suffix\|reserved_name\|bad_char\|too_deep\|symlink\|special_file\|case_conflict"}` |
| | `zip_ambiguous` | 422 | `{"entries": [...]}`（根层 `SKILL.md` 与子目录技能并存） |
| | `zip_no_skill` | 422 | `{}`（包内找不到 `SKILL.md`） |
| | `skill_invalid` | 422 | `{"skills": [{"name","reason"}], "issues": [LintIssue…]}` |
| | `skill_conflict` | 409 | `{"conflicts": ["a","b"], "overwrite_hint": true}`（**整包原子**：有冲突且未开 `overwrite` 时 root 里一个字节都没写） |
| `PATCH /skills/{name}` | `skills_disabled` / `skill_not_found` / `config_unavailable` / `invalid_request` | 400 / 404 / 409 / 422 | `{}` |
| `PATCH /mcp/{name}` | `mcp_server_not_found` / `config_unavailable` / `invalid_request` | 404 / 409 / 422 | `{}` |

> **说明**：`zip_*` 系列由 `harness/skill/pack.py` 抛出 `PackError(code, message, details)`，**HTTP 状态由 server 层的 `_PACK_STATUS` 映射**。

### 4.11 目录浏览（FS Browse）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/fs/browse` | `?path=<绝对路径>`（可省） | `BrowseResult` |

**为什么需要它**：控制台要按会话选「agent 的工作区」，而这个值必须是**服务端可用的绝对路径**；
浏览器出于安全**拿不到本机绝对路径**（`<input webkitdirectory>` 只给相对路径，
File System Access API 给的是句柄而非路径）。所以只能由服务端列举目录、前端逐级点选。

```jsonc
// BrowseResult
{ "path": "C:\\Users\\Lenovo\\Desktop\\agent",
  "parent": "C:\\Users\\Lenovo\\Desktop",     // 已是根（如 "C:\\"）→ null，前端据此置灰「上一级」
  "entries": [ { "name": "server", "kind": "dir" }, { "name": "README.md", "kind": "file" } ],
  "truncated": false }                        // 条目超过 500 时置 true
```

- `path` 省缺时的起点：沙箱开着 → 进程级工作区 `root`；否则 → 用户主目录。
- 排序：目录在前、文件在后，各自按名字（大小写不敏感）排序。
- **只返回条目名与类型，不返回文件内容**；`kind` 用 `entry.is_dir(follow_symlinks=False)` 判定
  （不跟随符号链接）。路径非法 / 无权限 → 422 `invalid_path`。
- **安全边界**：本端点等价于「任意目录枚举」，与 `PUT /config` 同风险等级——暴露到 `0.0.0.0`
  即等于开放目录列表。默认 `server.host=127.0.0.1`，且**刻意不设白名单**（否则「选任意工作区」就没意义了）。

---

## 5. SSE 方案

### 5.1 为什么是「POST 建 run + GET 拉流」

浏览器原生 `EventSource` **只支持 GET**，而一轮对话需要 POST body。两种取舍：

| 方案 | 优点 | 缺点 |
|---|---|---|
| POST 直接返回 `text/event-stream` | 单请求、最简单 | 无法用原生 `EventSource`（需 fetch+ReadableStream）；断线不能续传 |
| **POST 建 run + GET 拉流（选定）** | 可用原生 `EventSource`；**断线可续传**；run 与连接解耦，断开不取消 agent | 需 run 注册表与事件缓冲 |

选定后者。

### 5.2 事件桥（复用 `LoopEvent`）

`AgentLoop.__init__` 接受 `on_event: EventHook`——这正是当初为 SSE 留的口。服务层为**每个 run 新建一个 `AgentLoop`**
（Loop 无状态，构造成本极低），把 `on_event` 指向该 run 的发布器：

```python
def make_agent(state: AppState, run: Run) -> AgentLoop:
    return AgentLoop(
        state.provider, state.executor, state.registry,
        config=state.loop_config, prompt=state.prompt, tracer=state.tracer,
        on_event=lambda event: run.publish(event),   # 同步回调：put_nowait，不阻塞
    )
```

`on_event` 是**同步**回调（内部 `deque.append` + 唤醒订阅者），即使异步也无需——`AgentLoop._emit` 会 await 返回值，
同步回调天然满足；且回调异常被 Loop 吞掉，**前端推送失败不会中断 Agent**（已是既有保证）。

### 5.3 事件映射表

`LoopEvent.type` 直接作为 SSE 的 `event:` 名，**零映射层命名分歧**：

| LoopEvent.type | SSE `event:` | `data` 载荷 | 前端用途 |
|---|---|---|---|
| `run_started` | `run_started` | `{run_id, session_id}` | 开始 loading |
| `text_delta` | `text_delta` | `{step, text}` | 打字机效果（逐增量） |
| `model_response` | `message` | `{step, text, tool_calls[]}` | 追加/校准一条 assistant 气泡 |
| `tool_finished` | `tool_finished` | `{step, tool_results[]}` | 渲染工具调用卡片 |
| `step_finished` | `step_finished` | `{step, usage}` | 进度 / token 计数 |
| `run_finished` | `done` | `{stop_reason, final_text, usage, interrupts?}` | 结束收尾（**唯一终止事件**）；`interrupts` 仅在非空时出现 |
| `error` | `run_error` | `{error: {type, message, retryable}}` | 错误提示 |
| —（服务层补充） | `interaction_required` | `{id, kind, tool?, args?, question?, options?}` | 弹审批 / 提问框 |

> **事件命名注意**：LLM 错误事件名是 `run_error`，**不能叫 `error`**——
> `EventSource` 内建的连接错误事件类型就叫 `error`，同名会让前端无法区分
> 「服务端业务错误」与「连接断开」。`done` 是唯一终止事件：正常结束、取消、
> 内部异常都最终发一条 `done`（`stop_reason` 分别为 `final` / `cancelled` / `error`），
> 前端只需监听一个事件来收流。
>
> **软打断的 `interrupts`**：`done` 帧（以及非流式的 `turn_result()`）新增可选键 `interrupts: [{input, images}]`，
> **仅在非空时出现**——因此无打断时 payload 与改造前**逐字节一致**（向后兼容）。软打断收尾的
> `stop_reason` 是 `interrupted`；以 `run_finished` 正常收尾时插话由 `publish_loop_event` 带上，
> 兜底的 `finish()` 也会带（但用户主动 `POST /runs/{id}/cancel` 的**硬取消不带**，见 §4.2 / §4.3）。

> **真流式（已补齐）**：`harness/llm` 已支持 `astream`（`StreamEventType.TEXT_DELTA`）；
> `AgentLoop(LoopConfig(stream=True))` 逐增量发 `text_delta`（`harness.yaml` 的 `agent.stream: true`）。
> `text_delta` 与 `message` **同时存在**：前者给打字机效果，后者一次给出整段回复，
> 消费方按需选一种渲染即可（二者内容一致）。

### 5.4 帧格式与续传

标准 SSE 帧，带单调递增 `id`：

```
id: 7
event: tool_finished
data: {"step":1,"tool_results":[{"tool_call_id":"call_1","ok":true,"content":"…"}]}

```

- `id` = 该 run 内的**事件序号**（从 1 递增），是 `Last-Event-ID` 的续传依据。
- 每个 run 的**全部事件都留在内存环形缓冲**（run 生命周期短，不设上限；结束后按 TTL 回收）。
- 客户端断线重连时浏览器自动带上 `Last-Event-ID`，服务端从 `seq > last_id` 处**重放缓冲**再无缝续上实时流。
- 也支持显式 `?after=<seq>`，供非浏览器客户端使用。

### 5.5 心跳与断开

- **心跳**：空闲超过 `heartbeat_s`（默认 15s）发一个注释帧 `: ping`，防反向代理 / 网关因空闲超时断流。
  实现：`EventSourceResponse(..., ping=heartbeat_s)`（sse-starlette 在无数据时自动发 ping，
  因此订阅队列可以直接阻塞在 `queue.get()` 上）。
- **客户端断开**：SSE 生成器随连接断开而结束，**仅移除订阅者，不取消 run**——agent 继续跑完并落库，
  客户端可凭 `Last-Event-ID` 重连补齐。这是「POST 建 run + GET 拉流」的核心收益。
- **服务端终止**：进程退出时 run 丢失（非目标，见 §1）。

### 5.6 时序（含审批）

```
客户端                       server                        harness
  │ POST /turns {stream:true}   │                             │
  │────────────────────────────▶│ 建 run，spawn asyncio.Task   │
  │◀── 202 {run_id, events_url} │                             │
  │ GET /runs/{id}/events       │                             │
  │────────────────────────────▶│                             │
  │◀── event: run_started ──────│                             │
  │◀── event: message ──────────│  agent.run() ──▶ model      │
  │                             │◀── tool_calls: shell.run     │
  │                             │  Policy.authorize → ctx.approval
  │◀── event: interaction_required {"id":"x","kind":"approval"} │
  │ POST /interactions/x        │  InteractionRegistry.resolve │
  │  {"approved":true}          │─────────────────────────────▶ Future 唤醒
  │◀── 200 {ok:true}            │                             │
  │◀── event: tool_finished ────│                             │
  │◀── event: message ──────────│  第二轮模型回复              │
  │◀── event: done ─────────────│  run 结束，会话落库          │
```

---

## 6. 审批与提问通道

`Context.approval` / `Context.ask` 是 harness 既有的注入点（`Policy.authorize` 与 `ask_user` 工具会调用）。
服务层实现两个 hook，把「进程内 await」桥接成「SSE 事件 + POST 回填」：

```python
class InteractionRegistry:
    """interaction_id → _Pending(future, run_id, default)。审批与提问共用一套通道。"""

    def __init__(self, *, timeout_s: float = 300.0) -> None:
        self._pending: dict[str, _Pending] = {}
        self._timeout_s = timeout_s

    def open(self, *, run_id: str | None = None, default=None) -> tuple[str, asyncio.Future]:
        iid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        # default：超时 / 被软打断时的兜底值（审批 False、提问 ""）
        self._pending[iid] = _Pending(fut, run_id, default)
        return iid, fut

    def resolve(self, iid: str, value) -> bool:
        pending = self._pending.pop(iid, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(value)   # 由 POST /interactions/{id} 调用
        return True

    def resolve_for_run(self, run_id: str) -> list[str]:
        """软打断：用各自 default 兑现该 run 上全部未决交互，返回被释放的 id。"""
        released: list[str] = []
        for iid, pending in list(self._pending.items()):
            if pending.run_id != run_id:
                continue
            self._pending.pop(iid, None)
            if not pending.future.done():
                pending.future.set_result(pending.default)
            released.append(iid)
        return released

    async def wait(self, iid: str, fut: asyncio.Future, *, default):
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(iid, None)
            return default             # 审批超时 → 拒绝；提问超时 → 见下
```

装配进 `Context`（在 `Run` 启动时）：

```python
async def approval_hook(req: ApprovalRequest) -> bool:
    iid, fut = state.interactions.open(run_id=run.id, default=False)
    run.publish("interaction_required",
                {"id": iid, "kind": "approval", "tool": req.tool, "args": req.args})
    return await state.interactions.wait(iid, fut, default=False)   # 超时即拒绝（安全默认）

async def ask_hook(req: AskRequest) -> str:
    iid, fut = state.interactions.open(run_id=run.id, default="")
    run.publish("interaction_required",
                {"id": iid, "kind": "ask", "question": req.question, "options": list(req.options)})
    return await state.interactions.wait(iid, fut, default="")
```

要点：

- **安全默认**：审批超时 / 连接断开 → 返回 `False`（拒绝）。宁可拒绝，不可静默放行 destructive 操作。
- **提问超时**：返回空串，由 `ask_user` 工具自行转成失败结果（进度不阻塞）。`AskHook` 返回 `str`，不抛异常，保持 Executor 的「失败不抛异常」契约。
- **一次消费**：`resolve` 后即从 `_pending` 移除，重复 POST 返回 404，避免重放。
- **打断即释放挂起交互**：软打断（用户插话）时调用 `resolve_for_run(run_id)`，用**各自 `default`**（审批 `False`、提问 `""`）
  立即兑现该 run 上全部未决交互——否则模型会卡在审批上等满 `interaction_timeout_s`（默认 300s），用户体感是「打断没反应」。
  被释放的 id 通过响应里的 `released_interactions` 回给前端，之后 `POST /interactions/{id}` 回填返回 404（已消费）。
- `interaction_id` 与 `run_id` 解耦：一个 run 可产生多次交互（多个审批 / 多个提问）。

---

## 7. 并发与一致性

- **每会话一把锁**：`AppState.locks: dict[session_id, asyncio.Lock]`（配合 `run_conflict` 检查）。
  同一会话并发两轮会互相踩 `Session.active`，必须串行。
- **活跃 run 唯一 + 软打断**：`RunRegistry` 记录 `session_id → run_id`；同一会话已有活跃 run 时新 `POST /turns`
  **不再 409**，而是把输入登记为该 run 的插话（202，见 §4.2）。`run_conflict`(409) 收窄为「run 已终态但尚未 `release`」
  的极小收尾窗口兜底。**前提**：`run.finish(...)` 与 `finally: state.runs.release(run)` 之间**没有 await**
  （`_maybe_schedule_title` 是同步函数，只 `create_task`），且 `done` 帧是塞进订阅队列、要等事件循环切换才真正写给客户端，
  所以浏览器收到 `done` 时 `_active[session_id]` 必已清空 → 前端在 `done` 里**立刻**续发新 turn 不会撞 409。
  **若以后有人在中间插 await 就会引入竞态**（须重新评估此前提）。
- **轻量开关的读-改-写加锁**：`AppState.config_lock`（`asyncio.Lock`）串行化 `PATCH /skills/{name}` 与 `PATCH /mcp/{name}`
  两个轻量 PATCH 的「读 YAML → 改字段 → 校验 → 原子落盘」，避免并发丢更新。
  轻量 PATCH 与 `PUT /config` 的分工：前者只翻**一个字段** + 只热重建**对应部分**（技能重扫过滤面 / MCP 重连），
  **不要求无活跃 run**；后者是**整份配置落盘 + 全量重建**（provider 也会重连），仍返回 409 `config_reload_conflict`。
- **会话读取时机**：run 启动后**重新 `SessionManager.open(id)`**（而非复用请求期的对象），
  保证在锁保护下拿到最新状态后再 `run_turn`。
- **落库原子性**：`SessionManager.run_turn` 内部已完成「追加 user → 压缩 → 记录 assistant/tool」的全部 `commit`，
  服务层不重复写库，只负责编排与传输。
- **取消的副作用**：`cancel` 后当轮**用户消息已落库但无 assistant 回复**，下轮会带着这条孤立的 user 消息。
  已知行为，前端取消时应提示；后续可在取消时补一条 `system` 说明（本期不做）。
- **订阅者隔离**：每个 SSE 连接一个独立 `asyncio.Queue`；慢客户端只影响自身（队列可设上限并丢弃旧帧）。

---

## 8. 配置与启动

配置统一由 [配置系统](configuration-system-design.md) 管理，服务层不再自带配置实现：

- **结构** → 项目根 `harness.yaml`（`llm` / `agent` / `session` / `skills` / `memory` / `tools` / `mcp` / `observability` / `server` 九节）
- **密钥** → `.env`（`harness.yaml` 中以 `${OPENAI_API_KEY}` 引用）
- **临时覆盖** → 环境变量，映射表见[配置系统 §5](configuration-system-design.md)

`server` 段的字段与默认值：

| 配置项 | 环境变量 | 默认 |
|---|---|---|
| `server.host` | — | `127.0.0.1` |
| `server.port` | — | `8000` |
| `server.data_dir` | `HARNESS_DATA_DIR` | `data/sessions` |
| `server.heartbeat_s` | `HARNESS_HEARTBEAT_S` | `15` |
| `server.interaction_timeout_s` | `HARNESS_INTERACTION_TIMEOUT_S` | `300` |
| `server.run_ttl_s` | `HARNESS_RUN_TTL_S` | `600` |

启动（根目录 `main.py`，读 `harness.yaml` 的 `server.host/port`）：

```bash
python main.py              # 用配置里的 host / port
python main.py --port 9000  # 覆盖端口
python main.py --reload     # 开发用：代码改动自动重载
# 打开 http://127.0.0.1:8000/ui/
```

也可以直接跑 uvicorn（`main.py` 就是它的薄包装）：

```bash
uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8000
```

`create_app(config=None, *, state=None)` 是**工厂函数**：

- `config` 缺省等价于 `load_config()`（`HARNESS_CONFIG` 可指向别的 YAML）
- `state` 用于测试注入 fake provider；注入时 lifespan 里的 `await state.start()` 不会执行
  （`httpx.ASGITransport` 不跑 lifespan），因此**不会连接 MCP**

### 配置热重建（`PUT /config`）

配置不只在启动时读一次——控制台可**写回 `harness.yaml` 并原地重建运行时**（端点见 §4.7）。
数据加工（脱敏 / 还原 / 校验 / 回写）在 [harness/config/admin.py](../harness/config/admin.py)，
装配（重建部件）在 [server/state.py](../server/state.py) 的 `AppState.apply_config()`。

`PUT /config` 的处理链：

```
读旧 YAML → restore_secrets（`***`/空/None = 未改动 → 还原旧值）
         → is_plaintext_secret（明文密钥 → 422 config_invalid）
         → validate（插值 + 环境覆盖 + 校验；出错 422 config_invalid，details.path 指向字段）
         → dump_raw（备份 .bak → 写 .tmp → os.replace 原子替换；文件头写「由控制台生成」）
         → apply_config（原地重建 provider/registry/manager/prompt/tracer/…）
         → 失败回滚文件（dump_raw(previous, backup=False)）+ 422
```

- **重建 vs 热更**：改一行配置会**重建全部运行时部件**，而非字段级热更——简单且语义清晰。
  重建**不换 `AppState` 对象**（原地替换各部件），因此路由通过依赖注入持有的引用始终有效。
- **进程级策略一并重置**：`tools.shell` / `tools.sandbox` / `tools.net` 是进程级全局策略，
  重建时按新配置重新应用（旧策略被覆盖）。
- **有 run 时拒绝**：任何 run 正在执行 → 409 `config_reload_conflict`（重建会中断它）；
  前端据此提示「取消全部 run 并重试」。
- **失败回滚**：`apply_config` 抛 `ConfigError`（如启用了未安装的 `otel` sink）时，
  文件回滚为写盘前内容并返回 422，旧配置继续生效。
- **`.env` 与 `harness.yaml` 的职责边界**：明文密钥只放 `.env`，`harness.yaml` 里只写 `${VAR}` 引用；
  提交明文密钥 → 422（`is_plaintext_secret` 拦截）。`env_overrides` 会列出**改文件也无效**的环境变量覆盖。
- **写盘会丢注释与键序**：回写用 `yaml.safe_dump`（零新依赖），注释与手工键序被规范化；
  旧文件备份到 `<file>.bak`，新文件头插入固定说明（`MANAGED_HEADER`）。
- `POST /config/reload`：只在编辑器里手改了 YAML 时用——从磁盘重读并重建，**不写盘**。
- **MCP 单独重连**：`POST /mcp/reload`（§4.9）走 `AppState.reload_mcp()`——关旧连接 → 用**当前** `state.config` 重建 registry/executor → 重连；与 `apply_config` 共用同一段重连逻辑，差别是**不动 provider**、不写盘。

---

## 9. 里程碑

| 里程碑 | 内容 | 交付 |
|---|---|---|
| M1 | 骨架：`create_app` / `AppState` / 错误体 / 配置；会话 CRUD | ✅ `POST/GET/DELETE /sessions` 可用 |
| M2 | 对话 + SSE：`RunRegistry` / 事件桥 / `GET /runs/{id}/events` / `stream` 开关 | ✅ 一轮对话可流式观察 |
| M3 | 交互通道：`InteractionRegistry` + `/interactions/{id}`；审批与 `ask_user` 真实可用 | ✅ 解锁 P1 目标 |
| M4 | 自省与测试：`/tools` `/skills` `/healthz`；SSE 端到端测试（fake provider，离线） | ✅ 全链路可测（`server/test/test_api.py` 现 55 条） |
| M5 | **真流式**：`harness/llm` 增加 `astream` → `text_delta` 事件 → SSE 逐增量推送 | ✅ 已补齐（见 §5.3） |

---

## 10. 与 harness 既有契约的对齐表

| 服务层要素 | 复用的 harness 契约 | 位置 |
|---|---|---|
| run 的事件源 | `LoopEvent` / `EventType` / `on_event` | `harness/agent/events.py` |
| 一轮对话编排 | `SessionManager.run_turn(session, text, agent, ctx)` | `harness/session/manager.py` |
| 会话读写 | `SessionManager.create/open/list/delete` | `harness/session/manager.py` |
| 上下文注入 | `Session.context(scopes=…, approval=…, ask=…)` | `harness/session/schema.py` |
| 审批请求 | `ApprovalRequest(tool, args, reason)` + `ApprovalHook` | `harness/tool/schema.py` |
| 提问请求 | `AskRequest(question, options)` + `AskHook` | `harness/tool/schema.py` |
| 审批触发点 | `Policy.authorize` → `ctx.approval` | `harness/tool/policy.py` |
| 消息序列化 | `Message.to_dict()` / `ToolCall.to_dict()` | `harness/llm/schema.py` |
| 工具自省 | `ToolRegistry.list()` | `harness/tool/registry.py` |
| 技能自省 | `SkillRegistry.list_meta()` / `.reload()` | `harness/skill/registry.py` |
| 技能写入 | `write_skill / read_skill / delete_skill / lint_draft` | `harness/skill/writer.py` |
| 技能静态检查 | `lint_skills(root, known_tools=…)` | `harness/skill/lint.py` |
| 技能 zip 安装 | `install_zip(root, data, overwrite=…)` / `PackError` | `harness/skill/pack.py` |
| 技能启用开关 | `SkillRegistry.set_disabled(names)` / `is_enabled` / `all_names` | `harness/skill/registry.py` |
| 对话软打断探测 | `AgentLoop(should_interrupt=…)` / `StopReason.INTERRUPTED` | `harness/agent/loop.py`、`harness/agent/schema.py` |
| MCP 声明 → 运行时 | `build_dataclass(McpServerConfig, …)` / `to_runtime_mcp()` | `harness/config/loader.py`、`harness/config/build.py` |
| 三档权限 → scopes | `scopes_for(level)` / `approval_bypass(level)` | `harness/tool/permission.py` |
| 会话工作区 → 沙箱 | `session_sandbox(session)` / `session_scopes(session, scopes)` | `server/runner.py` |
| 沙箱每调用覆盖 | `use_sandbox(policy)` / `Context.sandbox` | `harness/tool/sandbox.py`、`harness/tool/executor.py` |
| 会话元数据写入 | `Session.set_metadata(**kv)` | `harness/session/schema.py` |

> **`Usage` 的序列化**：`Usage` 没有 `to_dict()`（保持 harness 不改），
> 由 `server/sse.py` 的 `usage_dict()` 补齐，所有 `usage` 字段统一走它。

---

## 11. 对既有设计的回填确认

本条用于回应此前「为多 Agent 预留的 4 条缝」，服务层已验证其中 2 条真实可用：

- **Loop 无状态** → 每个 run 新建 `AgentLoop` 注入不同 `on_event`，互不干扰（§5.2）。✅
- **审批走注入** → `Context.approval` 由服务层替换为跨进程通道（§6）。✅
- `Context.derive_child()` 与 `Tracer.record(parent_span_id=…)` 属多 Agent 场景，本期不涉及。

---

## 12. 本轮修复（AnyIO 关闭竞态）

**现象**：`POST /mcp/reload`（以及任何走 `AppState.reload_mcp()` 的路径，包括新的 `PATCH /mcp/{name}`）
在**首次**关闭一个由 lifespan 建立的 MCP 连接时会 500：
`RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`。

**原因**：MCP 的 stdio 客户端内部用 anyio 的 task group，而 anyio 的 cancel scope **必须在创建它的同一个 task 里退出**；
宿主在 **lifespan 任务**里连接、在**请求任务**里关闭，必然踩到（跨 task 退出）。

**修法**（`harness/tool/adapter/mcp.py` + `harness/tool/loader.py`）：

- `MCPClient.aclose()` 捕获该 `RuntimeError` 并**返回一条告警**（此时子进程已终止、连接实际已断，
  不抛出去让请求 500）。
- `LoadedServers.aclose()` 改为**逐个关闭、收集告警、不因单个失败而中断**；告警汇入 `reload_mcp()` /
  `apply_config()` 的 `warnings`，最终随 `GET /mcp`、`POST /mcp/reload`、`PATCH /mcp/{name}` 的响应体返回。
- 对 `POST /mcp/test`（临时连接、同一 task 内建立并关闭）无影响。
