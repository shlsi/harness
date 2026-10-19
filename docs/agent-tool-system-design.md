# Agent 工具系统设计文档

> 定位：**可扩展框架** · 技术栈：**Python** · 状态：设计稿（待评审）

本系统为 Agent 提供统一的工具（Tool）定义、注册、发现、执行、权限与可观测能力，
目标是「新增一个工具只需写一个函数」，同时保证执行安全、结果可回喂模型、外部工具
（进程隔离 / MCP）可平滑接入。

---

## 1. 设计目标与非目标

### 1.1 目标
- **契约统一**：所有工具用同一份声明（Schema）描述，既能给模型做 function calling，又能做入参校验。
- **可扩展**：支持本地函数、独立进程、MCP 协议三类来源，统一注册发现。
- **安全可控**：只读/写入、超时、沙箱、人工确认（approval）在框架层统一收口。
- **面向 LLM**：结果可裁剪/摘要，错误可自纠，不因单次失败中断整个会话。
- **可观测**：每次调用可追溯（入参、耗时、结果、tokens、失败原因）。

### 1.2 非目标（本期不做）
- 不做完整的多租户计费与配额系统。
- 不做 GUI/控制台（仅提供日志与 Trace 数据）。
- 不做模型推理层（只对接 function calling 协议）。

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                          Agent Loop                           │
│   产出 tool_calls ─────────────► 接收 tool_results             │
└───────────────▲───────────────────────────┬───────────────────┘
                │                            │
        ┌───────┴────────┐          ┌────────▼─────────┐
        │  Tool Router   │          │ Result Normalizer│
        │ (选工具/组装schema)│        │ (截断/摘要/错误格式化)│
        └───────┬────────┘          └────────▲─────────┘
                │                            │
        ┌───────▼────────────────────────────┴─────────┐
        │                 Executor                       │
        │ validate → authorize → run → normalize         │
        │ (超时 / 重试 / 并发 / 幂等 / approval)          │
        └───────┬───────────────────────────────┬────────┘
                │                               │
        ┌───────▼────────┐              ┌───────▼────────┐
        │   Registry     │              │   Sandbox /    │
        │ (注册/发现/分组) │              │   Policy       │
        └───────┬────────┘              └────────────────┘
                │
   ┌────────────┼─────────────┬──────────────┐
   ▼            ▼             ▼              ▼
LocalTool   ProcessTool    MCPTool     (扩展点…)
```

分层与依赖方向：上层依赖下层，下层不感知上层。

| 层 | 模块 | 职责 |
|---|---|---|
| 契约层 | `schema` | Tool 声明、JSON Schema、元信息 |
| 注册层 | `registry` | 注册、发现、分组、按需注入 |
| 执行层 | `executor` | 校验、鉴权、执行、超时重试、并发 |
| 安全层 | `policy` / `sandbox` | 权限、确认、隔离 |
| 结果层 | `normalizer` | 截断、摘要、错误格式化 |
| 观测层 | `telemetry` | 日志、Trace、指标 |
| 扩展层 | `loader` / `adapter` | 插件加载、MCP/进程适配 |

---

## 3. 工具契约（Tool Schema）

### 3.1 数据结构

```python
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

@dataclass(frozen=True)
class ToolSpec:
    name: str                      # 全局唯一，建议 namespace.action，如 fs.read
    description: str               # 给 LLM 的语义说明，决定模型是否选用
    input_schema: dict[str, Any]   # JSON Schema（对象类型）
    output_schema: dict[str, Any] | None = None
    # 执行策略
    timeout_s: float = 30.0
    max_retries: int = 0
    idempotent: bool = False
    concurrency: str = "parallel"  # parallel | serial
    # 安全策略
    side_effect: str = "read"      # read | write | destructive
    requires_approval: bool = False
    scopes: list[str] = field(default_factory=list)  # 权限域，如 ["fs:read"]
    # 面向模型的提示
    result_max_chars: int = 8000
    tags: list[str] = field(default_factory=list)
```

### 3.2 描述说明（description）写作规范
模型选工具的准确率 80% 取决于 description。规范：
- 一句话说明「做什么」，一句话说明「什么时候用 / 不用」。
- 明确副作用（是否会修改数据、是否可逆）。
- 参数说明写在 `input_schema` 各字段 `description` 里，不要塞进主描述。

### 3.3 入参校验
- 使用 JSON Schema 校验（`jsonschema` 库）。
- 校验失败 → 返回**结构化错误**（字段路径 + 期望类型），直接回喂模型让其自纠。

---

## 4. 注册与发现（Registry）

### 4.1 装饰器注册

```python
@tool(
    name="fs.read",
    description="读取文本文件内容，只读。用于查看代码/配置。",
    side_effect="read",
    scopes=["fs:read"],
)
def read_file(path: str, max_bytes: int = 200_000) -> str:
    """path: 绝对路径。max_bytes: 最大读取字节数。"""
    ...
```

实现要点：
- `@tool` 装饰器解析函数签名 + 类型注解 → 自动生成 `input_schema`。
- 同时把工具挂到模块级注册表（或通过 `Registry.register()` 显式注册）。

### 4.2 Registry 接口

```python
class ToolRegistry:
    def register(self, spec: ToolSpec, fn: Callable) -> None: ...
    def get(self, name: str) -> RegisteredTool: ...
    def list(self, tags: list[str] | None = None,
             scopes: list[str] | None = None) -> list[ToolSpec]: ...
    def to_openai_tools(self, names: list[str] | None = None) -> list[dict]: ...
```

- **按需注入**：`to_openai_tools(names=...)` 只暴露当前任务相关工具，控制 context 体积。
- **命名空间**：`fs.*` / `net.*` / `db.*`，避免命名冲突。
- **冲突策略**：同名注册直接报错（fail-fast），不静默覆盖。

---

## 5. 执行层（Executor）

### 5.1 统一执行管线

```
call(tool_name, raw_args, ctx) ->
  1. lookup        : 从 Registry 取 RegisteredTool，不存在 → ToolNotFound
  2. validate      : JSON Schema 校验，失败 → InvalidArgs(结构化错误)
  3. authorize     : Policy 检查 scopes / side_effect / approval
  4. dry-run?      : 若 requires_approval → 暂停，发起确认（人类/策略）
  5. run           : 带超时执行（本地直接调用 / 进程 / MCP）
  6. retry         : 仅当 idempotent 且可重试错误
  7. normalize     : 结果截断/摘要/格式化
  8. emit trace    : 记录入参/耗时/结果/错误
  9. return        : ToolResult
```

### 5.2 结果类型

```python
@dataclass
class ToolResult:
    ok: bool
    content: str | dict          # 回喂给模型的内容
    raw: Any | None = None       # 原始结果（可能落文件）
    error: ToolError | None = None
    meta: dict = field(default_factory=dict)  # 耗时、tokens、trace_id
```

### 5.3 并发与超时
- 同一条消息里的多个 tool_calls：默认可并行；标记 `concurrency="serial"` 的串行（如写同一文件）。
- 超时用 `asyncio.wait_for`；超时视为可重试错误（若幂等）。
- 重试采用指数退避，最多 `max_retries` 次。

### 5.4 幂等
- `idempotent=True` 的工具才允许自动重试。
- 写入类工具建议传入幂等键（如 `request_id`），由工具自身保证。

---

## 6. 安全与权限（Policy / Sandbox）

### 6.1 分级
| side_effect | 默认策略 |
|---|---|
| `read` | 允许，只受 scopes 限制 |
| `write` | 需 scopes 授权，记录审计日志 |
| `destructive` | 强制 `requires_approval=True`，需人工确认 |

### 6.2 权限模型
- 会话级 `ctx.scopes`：本次会话被授予的权限域集合。
- `authorize` 校验：`spec.scopes ⊆ ctx.scopes`，否则返回 `PermissionDenied`。
- 确认机制（approval）：`requires_approval` 的工具执行前挂起，产出 `ApprovalRequest`，
  由外部（人类/规则）批准后以 grant token 继续。**注意：删除类操作不直接执行，交由用户手动执行。**

### 6.3 沙箱

已实现（`harness/tool/sandbox.py`，默认关闭 / opt-in，设计见 [sandbox-design.md](sandbox-design.md)）：

- 文件：**单工作区** `root` 为唯一边界（判定前 `resolve()`，解 `..` 与符号链接）；
  root 内默认可读写，`write_paths` 收窄写白名单，`deny_paths` 黑名单最高优先（读写都拒）；
  root 外可用 `readonly_mounts` / `writable_mounts` 显式挂载。越界返回 `sandbox_denied`。
- 命令：不做 shell 拼接白名单之外的语法解析；`shell.run` 收敛 `cwd` 并扫描命令串里的路径 token（fail-closed）；
  `code.run` 用 `create_subprocess_exec`（不经 shell）+ 环境变量 allowlist + 临时目录限定在工作区内。
- 网络：**默认放行 + SSRF 基线黑名单**（`localhost` / `169.254.169.254` / `*.internal` 等始终拒绝），
  `check_url()` 只放行 http/https 并逐跳复查重定向；配置只能追加（`tools.sandbox.denied_domains`）。
- **非 OS 级隔离**：只拦"经工具传入的路径 / URL"，子进程自身行为不受约束
  （见 [sandbox-design.md](sandbox-design.md) §8、[builtin-tools-design.md](builtin-tools-design.md) §3.3）。

---

## 7. 结果处理（面向 LLM）

- **截断**：超过 `result_max_chars` 截断，并附「已截断，完整结果见 <file/ref>」。
- **大结果落地**：超大输出写入临时文件，只回喂摘要 + 路径引用，避免吃满 context。
- **错误格式**：统一 `{ok:false, error:{type, message, hint, retryable}}`，
  让模型能根据 `hint` 自纠或换工具，而非抛出异常中断循环。
- **结构化优先**：能返回结构化数据就不返回自然语言长文。

---

## 8. 可观测性（Telemetry）

每次调用记录一条 span：

```json
{
  "trace_id": "...", "span_id": "...",
  "tool": "fs.read", "args_digest": "...",
  "start_ms": 0, "duration_ms": 12,
  "ok": true, "error_type": null,
  "result_chars": 1024, "tokens_in": 120, "tokens_out": 256
}
```

- 日志分级：DEBUG（完整入参/结果）、INFO（元信息）、WARN/ERROR（失败）。
- Trace 贯穿一次 agent 会话；可用于回放与评测。

---

## 9. 扩展机制

### 9.1 三类工具来源

| 类型 | 隔离性 | 适用 |
|---|---|---|
| `LocalTool` | 同进程 | 大多数内置工具，低延迟 |
| `ProcessTool` | 子进程 | 高权限/易崩溃/依赖复杂 |
| `MCPTool` | 外部进程（协议） | 接入第三方/远端工具 |

### 9.2 统一适配接口

```python
class ToolAdapter(Protocol):
    async def invoke(self, spec: ToolSpec, args: dict, ctx: Context) -> Any: ...
    def describe(self) -> list[ToolSpec]: ...   # 用于 MCP 动态发现
```

- 插件加载：入口点（entry points）或目录扫描 `tools/*.py`，启动时注册。
- 版本与灰度：`ToolSpec` 可带 `version`，Registry 支持按版本共存与灰度分流。

> **实现进度**：MCP 适配器已落地（`adapter/mcp.py` + `loader.load_mcp_servers`，官方 mcp SDK，
> 支持 stdio 与 streamable-http）。外部工具经 `registry.register_external(spec, adapter)` 注册，
> 参数校验改走 `jsonschema`。细节见 [harness/tool/README.md](../harness/tool/README.md) §9。

---

## 10. 目录结构（实际）

```
agent/
├─ AGENTS.md                 # 工程约定
├─ todo.md                   # 缺口清单与推进顺序
├─ docs/                     # 全局设计文档
└─ harness/
   ├─ agent/                 # Agent Loop（编排层）
   ├─ tool/                  # 工具系统
   │  ├─ schema.py           # ToolSpec / ToolResult / ErrorInfo / Context
   │  ├─ registry.py         # ToolRegistry + @tool 装饰器 + 外部工具注册
   │  ├─ executor.py         # 执行管线（本地 pydantic / 外部 jsonschema 校验）
   │  ├─ policy.py           # 权限 / 审批
   │  ├─ sandbox.py          # 工作区边界 + 路径权限（默认关闭）
   │  ├─ normalizer.py       # 结果截断 / 错误格式化
   │  ├─ telemetry.py        # 日志 / Trace
   │  ├─ loader.py           # 插件加载（MCP）
   │  ├─ adapter/            # base.py（ToolAdapter）/ mcp.py
   │  ├─ builtin/            # 内置工具集（fs / shell / todo / interaction）
   │  └─ test/
   ├─ skill/                 # 技能系统（渐进式披露）
   └─ llm/                   # 模型接入（OpenAI Chat Completions 兼容）
```

> `sandbox` 层已落地（`harness/tool/sandbox.py`，默认关闭）：工作区边界与路径权限收口 `fs.*` / `shell.run`，
> 越界返回 `sandbox_denied`。设计见 [sandbox-design.md](sandbox-design.md)。

---

## 11. 落地路线（里程碑）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 契约+注册 | `schema` + `registry` + `@tool` | ✅ 已完成 |
| M2 执行管线 | `executor` + 校验 + 错误回喂 | ✅ 已完成 |
| M3 权限+确认 | `policy` + approval | ✅ 已完成 |
| M4 结果+观测 | `normalizer` + `telemetry` | ✅ 已完成 |
| M5 扩展 | `ProcessTool` + `MCPTool` + loader | ⏳ MCP 已完成；ProcessTool 待实现 |
| M6 安全 | `sandbox`（工作区边界 + 路径权限 + shell 收敛） | ✅ 已完成（[sandbox-design.md](sandbox-design.md)） |

**最小可用系统 = M1 + M2**，其余按需叠加。

---

## 12. 关键设计取舍（待确认）

1. **同步 vs 异步**：建议核心接口用 `async`，为 MCP/网络工具留余地；本地同步工具用适配器包装。
2. **Schema 来源**：建议「类型注解自动生成 + 允许手写覆盖」，兼顾便利与精确。
3. **审批交互**：审批回调由宿主（Agent Loop）注入，框架只负责「暂停 + 产出请求 + 接受授权」。
4. **错误是否中断会话**：默认不中断，返回结构化错误让模型自纠；仅权限/沙箱违规可升级中断。

---

## 附：待评审问题清单
- [ ] 是否引入 Pydantic 作为 Schema 与校验底座？（推荐）
- [ ] 审批默认策略：写操作是否一律需确认，还是按 scopes 放行？
- [ ] 是否需要多版本共存与灰度，还是先单版本？
- [ ] Trace 落地方案：本地 JSONL vs OpenTelemetry？
