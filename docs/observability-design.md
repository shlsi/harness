# 观测落地设计文档

> 定位：**harness 的观测层（span 采集 → 落地 → 查询 / 导出）** · 技术栈：**Python（核心零依赖）+ OpenTelemetry（可选）** · 状态：**已实现 M1–M3**
> 相关：[配置系统设计文档](configuration-system-design.md) · [Agent Loop 设计文档](agent-loop-design.md) · [FastAPI 服务层设计文档](api-service-design.md) · [会话管理设计文档](session-management-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

补齐 [todo.md](../todo.md) 里的「观测落地」：`Tracer` 已有 JSONL sink，但**只有写、没有读**，
且 span 之间**串不成一条链**——`llm.complete` 连 `trace_id` 都没有。本模块把它补成一个闭环：

```
AgentLoop / Provider / Executor
        │ record(span)
        ▼
   Tracer（内存索引） ──sink──▶ traces.jsonl ──read_spans()──▶ 离线分析 / 导出 / 评测
        │
        └──▶ OTel（可选）──▶ Jaeger / Tempo
```

一句话：**观测层 = 一份 append-only 的 span 日志 + 一个内存索引**。
同一份 span 数据，三个视图：

| 视图 | 面向谁 | 落点 |
|---|---|---|
| 日志文件 `data/traces.jsonl` | 离线分析 / 导出 / 评测 / 外部 collector | 已有 [JsonlTracerSink](../harness/config/sink.py)（只写）→ M2 补读 |
| 内存索引 `Tracer` | 在线 API（低延迟） | 已有 `spans` 列表 → M1 补查询 |
| OTel span | Jaeger / Tempo 等 | M3 补（可选依赖） |

### 非目标（本期不做）

- **不做 run 状态持久化**（重启后恢复在跑的 run）——沿用 [api-service-design.md](api-service-design.md) §1 的既有取舍：
  run 是一次性的，`Run.frames` 只在内存；断线补齐由 `Last-Event-ID` + 内存缓冲覆盖。真要做（多副本 / 长跑任务）另开命题。
- **不把 trace 当状态源**——span 记的是「调用」，不是「状态变更」；凭它重建不出会话状态。
  状态恢复归 `harness/session` 的 append-only 事件日志（`Session = replay(events)`）。
- **不做采样策略**——接 OTel 后可自行在 SDK 层配 `Sampler`，本模块不引入。
- **不做 metrics / logs**——只做 traces（span）。
- **不做前端 trace 可视化页面**——只提供 JSON API。
- **不改 span 的线上字段名**（见 §4 的取舍说明）。

---

## 2. 现状与缺口

### 现状：三处已在打 span，但串不成链

| 发出点 | 位置 | 带了什么 | 缺什么 |
|---|---|---|---|
| `agent.run` | [loop.py](../harness/agent/loop.py) `_finish` | `trace_id` / `stop_reason` / `steps` / `total_tokens` | **`duration_ms`** |
| 工具调用 | [executor.py](../harness/tool/executor.py) `_emit` | `trace_id` / `tool_call_id` / `ok` / `duration_ms` / `args_digest` / `error_type` | — |
| `llm.complete` | [openai_compat.py](../harness/llm/openai_compat.py) `_record` | `model` / `ok` / `duration_ms` / tokens / `stream` | **`trace_id`** ← 关键 |

`Context.trace_id` 的链路其实已铺好：`Context`（[tool/schema.py](../harness/tool/schema.py)）自带 `trace_id`，
`derive_child` 为多 Agent 记录 `parent_trace_id`，`ctx.metadata["parent_span_id"]` 支持嵌套。
**断点只在 Provider**——它拿不到 `Context`，所以 LLM span 上没有任何关联键。

### 缺口与本期对策

| # | 缺口 | 影响 | 本期 |
|---|---|---|---|
| 1 | **Provider 没接 tracer**（`build_provider` 不传），`llm.complete` span 既没有 `trace_id`、也没人收 | 一次 run 的 span 里**根本没有模型请求**——只有 `agent.run` | ✅ §5.1 |
| 2 | `agent.run` span 无 `duration_ms` | 无法反推起止（OTel 必需） | ✅ §5.1 |
| 3 | 只能写、不能读 `traces.jsonl` | 无法离线聚合 / 导出 / 重算 | ✅ §6（M2） |
| 4 | 服务层无 span 出口 | 前端 / 运维拿不到某次 run 的调用链 | ✅ §5.3 |
| 5 | 无 OTel 桥接 | span 进不了 Jaeger / Tempo | ✅ §7（M3） |
| 6 | 内存 `spans` 无按 trace 取回的方法 | 只能 `tracer.spans[-1]` 这种用法 | ✅ §5.2 |

> 缺口 1 是**真机验证时发现的**：单测用的 `FakeProvider` 不记 span，所以「Provider 没接 tracer」
> 在离线测试里完全看不出来——只有真模型跑一次、去看 `/runs/{id}/trace` 才会发现只剩一个 `agent.run`。
> 补了一条 `build_provider` 的接线断言（`test_build_provider_receives_tracer`）防回归。

---

## 3. 分层与目录

```
harness/tool/telemetry.py     # Tracer：纯内存（仅标准库），无 IO —— 本次加 contextvar + 查询方法
harness/config/sink.py        # trace 的 IO 层：JsonlTracerSink（写）+ read_spans（读，M2）
harness/config/otel_sink.py   # OTel 桥接（M3，可选依赖；未装则模块不导入）
harness/config/build.py       # build_tracer：按 observability 配置挑 sink
server/                       # 出口：GET /runs/{id}/trace + done 事件带 trace_id
```

依赖方向不变：`server → harness`；`harness/tool/telemetry.py` 仍**零第三方依赖**（新增 `contextvars` 是标准库）。

---

## 4. Span 数据契约

span 是**普通 dict**（schemaless），约定字段如下：

```python
{
    "span_id": "3f9a1c2b7d40",      # Tracer 生成，12 hex
    "parent_span_id": "...",        # 可选；M1 起子 span 自动挂到 run 根 span（见 §5.1）
    "ts": 1792317827.412,           # 结束时刻（epoch 秒）
    "duration_ms": 128.4,           # 本次补齐：所有 span 都应带（OTel 反推 start = ts - duration/1000）
    "trace_id": "...",              # 必带（M1 起由 Tracer 兜底，见 §5.1）
    "tool": "agent.run" | "llm.complete" | "fs.read" | ...,   # span 名
    "ok": True,
    ...                             # 其余为各发出点自定属性（model / stop_reason / error_type / args_digest …）
}
```

> **取舍：`tool` 字段名不改。** 它历史地承载了三种值（`agent.run` / `llm.complete` / 真实工具名），
> 语义上更像「span 名」而非「工具名」，改叫 `kind` 更准确。
> 但改名会同时破坏既有 `traces.jsonl` 与 [test_agent_loop.py](../harness/agent/test/test_agent_loop.py) 的断言，
> 收益不抵成本，故**保留 `tool` 作为 span 名**，在此如实记录这处命名债。
> 若要清理，单独开一个「span schema v2」里程碑，带格式迁移。

---

## 5. M1：请求级 span 聚合（零新依赖）

### 5.1 trace 贯通：`contextvars` 兜底

Provider 的 `_record` 拿不到 `Context`，**不改 `ChatRequest` 契约**，也不给 Provider 注入新依赖——
在 `Tracer.record` 里**兜底**取当前 trace 与当前根 span：

```python
# harness/tool/telemetry.py
_current_trace: ContextVar[str | None] = ContextVar("harness_trace_id", default=None)
_current_parent: ContextVar[str | None] = ContextVar("harness_span_parent", default=None)
_NOT_GIVEN: Any = object()          # 区分「没传 parent」与「显式传 None」

class Tracer:
    def record(self, *, parent_span_id=_NOT_GIVEN, span_id=None, **fields):
        if "trace_id" not in fields:                    # ← 兜底：补 trace
            trace_id = current_trace_id()
            if trace_id is not None:
                fields["trace_id"] = trace_id
        if parent_span_id is _NOT_GIVEN:                # ← 兜底：挂到 run 根 span
            parent_span_id = current_parent_span()
        span = {"span_id": span_id or uuid4().hex[:12], "ts": time.time(), **fields}
        ...
```

谁负责 set？**`AgentLoop.run`**——它是「一次 run」的边界，`ctx.trace_id` 就在手上：

```python
async def run(self, messages, ctx=None):
    ctx = ctx or Context()
    run_span_id = uuid4().hex[:12]          # 先占号：子 span 会引用它
    trace_token, parent_token = set_current_trace(ctx.trace_id), set_current_parent(run_span_id)
    try:
        return await self._run_inner(messages, ctx, run_span_id)
    finally:
        reset_current_parent(parent_token)  # 不跨 run 泄漏
        reset_current_trace(trace_token)
```

好处：**Provider 与 Executor 一行都不用改**，且任何宿主（CLI / 测试 / server）只要走
`AgentLoop.run` 就自动获得链路关联。

> **让 span 成树，而不是一堆散点。** run 根 span 的 id 是**先占号**的：
> 子 span（工具 / LLM）记录时它还没落地，但 `parent_span_id` 已指向它，
> `_finish` 再以同一个 id 记录根 span。于是 `tree()` 出来是一棵以 `agent.run` 为根的树。
> 根 span 记录时**显式传** `parent_span_id=ctx.metadata.get("parent_span_id")`（多 Agent 的父链，可能为
> `None`）——不走兜底，否则它会把自己认作父亲。

同时给 `agent.run` 补 `duration_ms`：`_run_inner` 开头记 `started = time.perf_counter()`，
传给 `_finish(..., started=started, run_span_id=...)`，span 里写
`duration_ms=round((time.perf_counter() - started) * 1000, 2)`。

### 5.2 按 trace 取回：三个查询方法

```python
class Tracer:
    def spans_for(self, trace_id: str) -> list[dict]: ...   # 过滤 trace_id，保持记录顺序
    def tree(self, trace_id: str) -> list[dict]: ...        # 按 parent_span_id 组树
    def summary(self, trace_id: str) -> dict: ...           # {span_count, error_count, duration_ms, stop_reason}
```

组树与汇总都抽成了**模块级纯函数**（`build_tree` / `summarize_spans`），
内存视图与文件视图共用同一份实现，不漂移。

`build_tree` 只依赖 `span_id` / `parent_span_id` 两个键，找不到父节点的挂到根，
因此即使父 span 因 `max_spans` 被裁掉，也不会丢子节点。

> **`max_spans` 与截断**：`max_spans`（默认 500）裁的是**内存**视图，sink 早已外发、文件是全的。
> 所以内存视图**可能截断**长 run 的早期 span。这是刻意的：
> - 在线查询（要快）→ 内存视图，接受截断；
> - 要全量 → `source=file` 走 M2 的 `read_spans()`（文件是完整的）。

### 5.3 服务层出口

| 端点 | 返回 |
|---|---|
| `GET /runs/{run_id}/trace` | `{run_id, trace_id, summary, spans, tree}`（内存索引） |
| `GET /runs/{run_id}/trace?source=file` | 同上，但从 `tracer_path` 读全量（M2） |

`summary` = `{span_count, error_count, duration_ms, stop_reason}`。

`summary.stop_reason` 直接取自 run 根 span，取值与 [StopReason](../harness/agent/schema.py) 一致：
`final` / `max_steps` / `token_budget` / `loop_detected` / **`interrupted`**（用户插话的软打断）/ `error`。
软打断的 run 同样是**正常收尾**：`_finish()` 照记 run 根 span，`ok=True`（因为 `stop_reason != "error"`），
无需新埋点——`stop_reason` 是 `Tracer.record(RUN_SPAN, stop_reason=...)` 的现成字段。
按此筛选即「只取被用户打断的 run」，例如读侧 `read_tree(path)` 后再按 `summary.stop_reason == "interrupted"` 过滤。

另有 `done` 事件补 `trace_id`（前端拿到即可深链到 trace 端点）。
`span_count` 只放在 trace 端点里，**不放进 `done`**——`Run` 不必为此持有 tracer 引用，保持解耦。

关联方式：`Run` 新增 `trace_id` 字段，在 [runner.py](../server/runner.py) 的 `run_turn` 里由
`ctx.trace_id` 赋值；`run_started` / `done` 两个事件从 `run.trace_id` 取。
（`Context.trace_id` 每次 `session.context()` 新建，故 `trace_id` ≈ 一次 turn / run 的 id。）

span 为空时端点返回 `spans: []`、`trace_id: null`，**不报错**（观测缺数据不是错误）。

---

## 6. M2：trace 读侧（离线）

在 [sink.py](../harness/config/sink.py)（trace 的 IO 层）补读函数，与写侧同模块：

```python
def read_spans(path: Path | str, *, trace_id: str | None = None) -> list[dict]:
    """从 traces.jsonl 读回 span。按 trace_id 过滤（None = 全部）。

    与 JsonlSessionStore.load_events 同款健壮性：跳过空行与崩溃残留的半行。
    """

def read_tree(path, *, trace_id: str | None = None) -> list[dict]:
    """读回并组树（复用 build_tree 纯函数）。"""
```

要点：

- **流式读**（逐行 `json.loads`），不把整个文件读进内存——traces 文件会长。
- 坏行**跳过不炸**：崩在写一半会留下半行 JSON，这与 session 的 JSONL 是同一类问题、同一套对策。
- 组树抽成**模块级纯函数** `build_tree(spans)`（在 `harness/tool/telemetry.py`），
  内存视图与文件视图共用一份实现，避免两套组树逻辑漂移。

于是 `?source=file` 生效：在线快取内存、要全量读文件，两者共用同一套组树代码。
`source` 只接受 `memory` / `file`，其余返回 400 `invalid_source`；
未配 jsonl sink 时访问 `source=file` 返回 400 `trace_file_not_configured`（而不是静默给空）。

---

## 7. M3：OTel 桥接（可选依赖）

### 7.1 形态

`Tracer` 的 sink 契约是 `Callable[[dict], None]`，OTel 只是**又一个 sink**——不必改 Tracer：

```python
# harness/config/otel_sink.py
class OtelTracerSink:
    """把 span dict 桥成 OTel span，经 BatchSpanProcessor 导出到 OTLP。"""
    def __init__(self, *, service_name: str = "harness", endpoint: str | None = None,
                 exporter: Any = None, provider: Any = None) -> None: ...
    def __call__(self, span: dict[str, Any]) -> None: ...
```

- 全部逻辑包在 `try/except Exception: pass` 内 —— 观测失败绝不冒泡（与 JSONL sink 同一纪律）。
- `opentelemetry-*` **延迟导入**：未装依赖时，仅 `tracer_sink: otel` 会在**装配期**报清晰错误
  （`ConfigError`，附 `pip install -e ".[otel]"` 提示），其余配置（`none` / `jsonl`）完全不受影响。
- `exporter` / `provider` 两个参数是给测试用的注入口：注入 `InMemorySpanExporter` 时改用
  `SimpleSpanProcessor`（同步导出，断言立即可见）；留空则自建 `BatchSpanProcessor` + OTLP。

### 7.2 两个真问题（M3 的核心难点）

1. **我们的 id 不是 OTel 合法 id**：`span_id` 是 12 hex，OTel `SpanId` 要 16 hex、`TraceId` 要 32 hex。
   → 用**确定性哈希**派生：`derive_id(x)` = `sha256(x)` 取前 16 / 32 位十六进制再转 int。
   **确定性**是关键：同一个源 id 永远映射到同一个 OTel id，父子才能对上号。
2. **SDK 默认自己生成 id，父子会错位**：`start_span` 会自己生成 `SpanId`，
   那样我们派生的父 id 就找不到对应的真实 span。
   → 给 `TracerProvider` 传一个自定义 `IdGenerator`：它从 **contextvar** 读「本次要用的 id」，
   读不到才回退随机。`_emit` 在调用 `start_span` 前把派生 id 写进 contextvar，
   因此**父子关联不依赖 span 的到达顺序**（`agent.run` 最后才记录，子 span 早就发出去了）。

### 7.3 配置

```yaml
observability:
  tracer_sink: otel              # none | jsonl | otel
  otel_service_name: harness
  # 留空则沿用 SDK 的环境变量约定（OTEL_EXPORTER_OTLP_ENDPOINT）
  otel_endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}
```

> 没有 `otel_insecure`：走的是 **OTLP/HTTP** exporter，http / https 由 `otel_endpoint` 的
> **URL scheme** 决定，多一个开关只会造成两个地方互相打架。

依赖放 **extra**，核心链路零新增依赖：

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.27", "opentelemetry-sdk>=1.27",
        "opentelemetry-exporter-otlp-proto-http>=1.27"]
```

测试用 `opentelemetry.sdk.trace.export.in_memory_span_exporter.InMemorySpanExporter`，**全离线**，
断言：span 名、父子关系（且**故意让子 span 先到**）、`duration_ms` 反推的起止时间、坏 span 静默。
未安装 extra 的环境里这几条测试会打印 `SKIP` 后跳过（不失败）。

---

## 8. 与 server 的关系

```
harness/tool/telemetry.py   Tracer（内存索引 + contextvar：当前 trace / 当前根 span）
        ▲ record()
AgentLoop.run ──set_current_trace(ctx.trace_id) + set_current_parent(run_span_id)──┐
        │                                                                          │
Provider._record / Executor._emit ─────────────────────────────────────────────────┘
        │                       （自动带上 trace_id 与 parent_span_id）
        │ sink
        ├──▶ traces.jsonl ──read_spans()──▶ GET /runs/{id}/trace?source=file
        ├──▶ OtelTracerSink ──▶ Jaeger / Tempo
        └──▶ Tracer.spans_for(trace_id) ──▶ GET /runs/{id}/trace（默认，内存）
```

`server/` 侧只多四处小改：`Run.trace_id` 字段、`run_turn` 里赋值、
`publish_loop_event` 给 `run_started` / `done` 补 `trace_id`、`routes/run.py` 加一个只读端点。
**harness 不感知 HTTP**的边界不变。

### 8.1 宿主侧的两条额外记录

- **`approval.auto` 诊断 span**：会话权限档位为「读写·免批准」时，审批会被**旁路**——
  宿主记一条 `tool="approval.auto"`、`target=工具名` 的 span，便于事后审计「这次放行没问过用户」。
  它显式传 `trace_id=run.trace_id`、未传 `parent_span_id`，因此由 `Tracer.record` 的兜底挂到 run 根 span 下。
- **MCP 关闭告警**：跨任务关闭 MCP 长连接时 anyio 会抛
  `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`。
  现在 `MCPClient.aclose()` 内**吞掉**该错误并返回告警字符串，`LoadedServers.aclose()` 逐个关闭、
  收集告警、**不因单个失败中断**。这属于**连接生命周期**，不是 trace 的一部分
  （告警从 `PATCH /mcp/{name}` / `PUT /config` 响应的 `warnings` 返回）。

---

## 9. 里程碑

| 里程碑 | 内容 | 交付 |
|---|---|---|
| M1 | `contextvars` 兜底（trace + 根 span）+ **Provider 接线 tracer** + `agent.run` 补 `duration_ms` + `spans_for/tree/summary` + `/runs/{id}/trace` + `done` 带 `trace_id` | ✅ agent 6 条 + server 3 条：trace 贯通到 llm span / 组树含乱序孤儿 / duration_ms / 端点返回 / 文件源 / 缺数据不报错 |
| M2 | `read_spans` / `read_tree`（抽 `build_tree` 纯函数）+ `?source=file` | ✅ config 4 条 + server 2 条：坏行跳过 / 按 trace 过滤 / 内存与文件组树一致 / 截断时文件给全量 / 两个 400 |
| M3 | `otel_sink.py` + `[otel]` extra + 配置两字段 + `build_tracer` 分支 | ✅ config 3 条（`InMemorySpanExporter`）：span 名 / 父子（子 span 先到）/ 起止 / 坏 span 静默 |

全量测试：tool 80、skill 61、llm 27、agent 43、session 35、memory 17、config 57、server 55 = **375 条，全部离线**。

> 另做了一次**真机端到端验证**（agnes-3.0-flash + 真实服务）：
> 一次带工具调用的 run 得到 4 个 span（`llm.complete` → `fs.list` → `llm.complete` → `agent.run`），
> 树以 `agent.run` 为根、3 个子节点，LLM span 的 `parent_span_id` 正是 run 根 span 的 `span_id`。

---

## 10. 兼容性

- `Tracer.record` 的兜底**只在 contextvar 有值时才写 `trace_id` / `parent_span_id`**：
  不在 run 内（如单测直接 `tracer.record(...)`）行为与今天完全一致。
- `record(parent_span_id=...)` 的默认值由 `None` 换成哨兵 `_NOT_GIVEN`：
  **显式传 `None` 仍是「当根」**，只有「不传」才走兜底 —— 语义向后兼容。
- span dict **没有新增必填字段名**，只补 `duration_ms` / `trace_id` 两个值的来源；既有 `traces.jsonl` 仍可读
  （老行缺 `duration_ms` 时，读侧/OTel 侧按 `duration_ms=0` 处理）。
- JSONL sink、`tracer_max_spans`、`tracer_path` 语义**不变**；`observability` 新增两个键，**缺省不影响** `none` / `jsonl`。
- `AgentLoop` 内部多了 `_run_inner`（`run` 变成极薄的包装）——`_` 前缀，非公开契约。
- `/runs/{id}/trace` 是**纯新增**端点，不改既有响应；`run_started` / `done` 事件只增字段。

---

## 11. 决策记录

| 议题 | 结论 |
|---|---|
| 观测层的形态 | **append-only span 日志 + 内存索引**，不引入新存储 |
| trace 贯通方式 | **`contextvars` 兜底**（在 `Tracer.record` 里），Provider / Executor 零改动，不改 `ChatRequest` |
| 谁 set 当前 trace / 根 span | `AgentLoop.run`（一次 run 的边界），`finally` 复位不跨 run 泄漏 |
| span 成树的方式 | run 根 span **先占号**，子 span 记 `parent_span_id` 指向它；根 span 记 `parent_span_id` 时**不走兜底** |
| 在线 vs 离线聚合 | **在线用内存索引**（快、可能被 `max_spans` 截断）；**离线读文件**（全量）；共用一套组树 / 汇总代码 |
| 读侧落点 | 与 `JsonlTracerSink` 同模块（IO 归一处），不新建模块 |
| `done` 事件带什么 | 只带 `trace_id`；`span_count` 放 trace 端点（避免 `Run` 持有 tracer 引用） |
| `source=file` 不可用时 | 返回 400（而不是静默给空）——让「没配 jsonl sink」这件事可见 |
| 是否持久化 run 状态 | **不做**（沿用既有非目标）；状态恢复归 session 事件日志，与 trace 分离 |
| OTel 依赖 | **可选 extra `[otel]`**，延迟导入，核心零新增依赖 |
| OTel 父子关联 | 自定义 `IdGenerator` 读 contextvar 里预置的派生 id → **顺序无关** |
| OTel 传输 | **OTLP/HTTP**；不加 `otel_insecure`，http/https 由 endpoint 的 scheme 决定 |
| `tool` 字段名 | **保留**（兼作 span 名），记录为命名债，不在本期做格式迁移 |
| 软打断 run 的 span | run 根 span 照记，`stop_reason="interrupted"`、`ok=True`（非 `error`），无需新埋点；`summary.stop_reason` 随之可取 `interrupted` |
| 免批准旁路留痕 | 权限档位「读写·免批准」时记一条 `approval.auto` 诊断 span（`target=工具名`），便于事后审计 |
| MCP 关闭告警 | `MCPClient.aclose()` 吞掉跨任务 anyio `RuntimeError` 并返回告警；`LoadedServers.aclose()` 逐个关闭、收集告警、不中断（属连接生命周期，不入 trace） |
| span 缺数据时 | 端点返回空列表而非报错（观测缺数据不是错误） |

### 仍待确认

- [ ] 是否要做**采样**（`Sampler`）——长跑服务全量落盘可能偏大，本期先不引入。
- [ ] 是否要 **OTel metrics**（token 计数、工具耗时直方图）——本期只做 traces。
- [ ] `traces.jsonl` 的**轮转 / 上限**——当前单文件只追加，长期会很大，是否需要按天切分？
- [ ] 前端要不要一个 trace 展示页（现在只提供 JSON）？
