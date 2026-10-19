# 工具系统（harness/tool）

> 全局设计见 [docs/agent-tool-system-design.md](../../docs/agent-tool-system-design.md)。
> 子模块文档：[adapter/](adapter/README.md) · [builtin/](builtin/README.md) · [test/](test/README.md)。
> 本文档描述本模块的**实际实现与 API**。

## 1. 模块职责

为 Agent 提供统一的工具**定义 → 注册 → 发现 → 执行 → 鉴权 → 结果归一化 → 追踪**能力。
新增一个工具只需写一个带类型注解的普通函数并加 `@tool` 装饰器。

当前实现范围：设计文档的 **M1（契约+注册）+ M2（执行管线）**，已落地 **P0 内置工具集**（见 §8），
并已接入 **MCP 外部工具**（官方 mcp SDK，见 §9）。

## 2. 目录结构

```
harness/tool/
├─ __init__.py            # 公共导出（导入即注册内置工具；不依赖 mcp SDK）
├─ schema.py              # 契约层：ToolSpec / ToolResult / ErrorInfo / Context / 异常
├─ registry.py            # 注册层：ToolRegistry + @tool 装饰器 + Context 注入 + 外部工具注册
├─ executor.py            # 执行层：call() / execute_tool_calls()（本地 pydantic 校验 / 外部 jsonschema 校验）
├─ policy.py              # 安全层：scopes 校验 + 危险操作审批
├─ permission.py          # 三档会话权限：档位 → scopes + 是否旁路审批（只读 / 读写·需批准 / 读写·免批准）
├─ sandbox.py             # 安全层：工作区边界 + 路径权限（进程级默认 + 每调用覆盖，见 docs/sandbox-design.md）
├─ normalizer.py          # 结果层：内容转换与截断
├─ telemetry.py           # 观测层：Tracer + build_tree/summarize_spans + 当前 trace/根 span 的 contextvar
├─ loader.py              # 插件加载：load_mcp_servers()
├─ adapter/               # 外部工具适配器（见 adapter/README.md）
├─ builtin/               # 内置工具集（见 builtin/README.md）
└─ test/                  # 冒烟测试（见 test/README.md）
```

## 3. 执行管线

```
lookup → validate → authorize → run(超时/重试) → normalize → trace → ToolResult
```

- **validate**：由函数签名自动生成 Pydantic 模型；失败返回结构化 `invalid_args`（含字段级错误），可直接回喂模型自纠。
- **authorize**：`spec.scopes ⊆ ctx.scopes`；`destructive` 或 `requires_approval=True` 触发审批回调。
- **run**：`asyncio.wait_for` 控超时；同步工具走 `asyncio.to_thread`，不阻塞事件循环；仅 `idempotent=True` 才重试（指数退避）。
  工具内抛 `SandboxViolation` 时**直接返回 `sandbox_denied`、不重试**（确定性失败）。
- **失败不抛异常**：统一返回 `ToolResult(ok=False, error=ErrorInfo(...))`，避免中断 Agent 循环。

## 4. 核心 API

### 4.1 定义工具

```python
from harness.tool import tool

@tool(
    name="fs.read",                       # 全局唯一，建议 namespace.action
    description="读取文本文件内容（只读）…",  # 决定模型是否选用，需说明用途与副作用
    side_effect="read",                   # read | write | destructive
    scopes=["fs:read"],                   # 权限域
    timeout_s=30.0, max_retries=0, idempotent=False,
    result_max_chars=8000, tags=["fs"],
)
def read_file(path: str, max_bytes: int = 200_000) -> str:
    ...
```

入参 JSON Schema 由签名 + 类型注解自动生成，也可传 `input_schema` 覆盖。

**Context 注入**：形参类型标注为 `Context`（或命名为 `ctx`）时，该参数不会被暴露给模型，
而是由 Executor 在执行时注入会话上下文。用于需要访问会话状态的工具（如 `todo.write`、`ask_user`）：

```python
@tool(name="todo.write", description="…", side_effect="write", tags=["todo"])
def write_todos(ctx: Context, todos: list[dict]) -> str:
    ...  # ctx 不会出现在 to_openai_tools() 的 parameters 中
```

### 4.2 注册表

```python
from harness.tool import ToolRegistry, default_registry

default_registry.names()                      # 全部工具名
default_registry.list(tags=["write"])         # 按标签筛选
default_registry.to_openai_tools()            # 导出 function calling 定义
default_registry.to_openai_tools(["fs.read"]) # 按需注入（控制 context）
default_registry.register_external(spec, adapter)  # 注册外部工具（MCP 等）
```

同名注册直接抛错（fail-fast），不静默覆盖。

### 4.3 执行

```python
from harness.tool import Executor, Context, default_registry

executor = Executor(default_registry)
ctx = Context(scopes={"fs:read"})

# 单次调用
result = await executor.call("fs.read", {"path": "a.txt"}, ctx)
result.ok, result.content, result.error, result.meta

# 一轮多个 tool_calls：并发执行并按 tool_call_id 归位
messages = await executor.execute_tool_calls(model_tool_calls, ctx)
# -> [{"role": "tool", "tool_call_id": ..., "content": ...}, ...]
```

### 4.4 上下文与权限

```python
Context(
    trace_id="...",                       # 默认自动生成
    scopes={"fs:read", "fs:write"},       # 会话被授予的权限域
    session_id="...",                     # 会话标识（todo 等按会话存状态）
    approval=my_approval_hook,            # async (ApprovalRequest) -> bool
    ask=my_ask_hook,                      # async (AskRequest) -> str
    sandbox=my_sandbox_policy,            # 本次调用的工作区策略；None = 沿用进程级默认
)
```

`approval` 缺失时，`destructive` 工具返回 `approval_required`；回调返回 `False` 则返回 `permission_denied`。
`ask` 缺失时，`ask_user` 返回 `execution_error` 并提示未提供回调。
**删除类操作不直接执行**，由审批回调转为人工确认后手动处理。
`approval` 传一个恒真回调 = 该轮「免批准」（控制台的 `write_free` 档就是这么做的）。

**`Context.derive_child(...)`**（为多 Agent 预留的隔离边界）：派生新上下文——
新 `trace_id`、scopes 可收窄（不传则继承）、`approval` / `ask` / `sandbox` 透传
（**沙箱必须透传**，否则派生上下文会静默回落到进程级沙箱、逃出会话工作区）、
`metadata` 记录 `parent_trace_id` 以关联父子调用链。

### 4.5 沙箱（工作区边界）

`harness/tool/sandbox.py` 把「模型能碰哪些路径」收成一份策略。
**默认关闭**：`resolve_path()` 原样返回入参，`fs.*` / `shell.run` 行为与引入前逐字节一致。

```python
from harness.tool import configure_sandbox, use_sandbox, resolve_path, check_command

configure_sandbox(enabled=True, root=".", deny_paths=[".env", "**/.git/**"])   # 进程级默认

resolve_path("src/a.py", mode="write")   # -> <root>/src/a.py
resolve_path("C:/Windows/win.ini")       # -> SandboxViolation

# 每调用覆盖（控制台按会话选的工作区走这条；None 是 no-op，不是"覆盖成 None"）
with use_sandbox(SandboxPolicy(enabled=True, root="/tmp/ws")):
    resolve_path("a.txt", mode="read")   # -> /tmp/ws/a.txt
```

**两级作用域**：`configure_sandbox` 写的是**进程级默认**；`use_sandbox(policy)` 在 `with` 块内
**临时覆盖**（`ContextVar` 承载，按 task / 线程隔离，并发工具调用互不串味）。
接线只有一处：`Executor._run_tool` 用 `with use_sandbox(ctx.sandbox)` 包住工具执行——
因此**所有工具函数的签名都不用带 `ctx`**。同步工具走 `asyncio.to_thread`，
其内部 `copy_context()` 会把覆盖值带进线程。

| 概念 | 说明 |
|---|---|
| `root` | 唯一边界；判定前 `resolve()`，解 `..` 与符号链接 |
| `write_paths` | root 内写白名单 glob（留空 = root 内全可写）；读不受它影响 |
| `deny_paths` | 最高优先级黑名单 glob，**读写都拒**；pattern 不含 `/` 时按文件名匹配 |
| `readonly_mounts` / `writable_mounts` | root 之外的额外挂载（只读 / 可写） |

- `**` 跨目录（globstar）：`**/.git/**` 能匹配根层与任意深度。
- `fs.glob` / `fs.grep` / `fs.list` 的**越界结果静默丢弃**，不因个别条目让整次调用失败。
- `shell.run`：`cwd` 收敛到 root；命令串里像路径的 token（`C:\` / `/` / `..`）逐个校验，越界即拒。
- 配置入口：`harness.yaml` 的 `tools.sandbox`（见 [配置系统设计文档](../config/README.md)）。
- **不是 OS 级隔离**：子进程自身行为不受约束。完整设计见 [docs/sandbox-design.md](../../docs/sandbox-design.md)。

### 4.6 三档会话权限（`permission.py`）

控制台里用户不该自己拼 `fs:read` + `fs:write` + `shell:run`，而应从三个语义明确的档位里选一个。
「档位 → 权限域 + 审批行为」的映射**只有这一处**（否则前端 / 服务层 / 文档三份定义早晚漂移）：

```python
from harness.tool.permission import PERMISSIONS, scopes_for, approval_bypass, normalize

PERMISSIONS          # ("read", "write", "write_free")——顺序即 UI 展示顺序
scopes_for("read")   # ("fs:read", "net:read")
scopes_for("write")  # 上述 + ("fs:write", "shell:run", "code:run", "memory:write")
approval_bypass("write_free")   # True —— 宿主应把审批 hook 换成恒真回调
normalize("yolo")    # -> PermissionError_（非法档位）
```

| 档位 | scopes | 审批 |
|---|---|---|
| `read` | `fs:read`, `net:read` | 无需（没有写 / 危险工具可用） |
| `write` | 上述 + `fs:write`, `shell:run`, `code:run`, `memory:write` | 沿用 `Policy` 判定（`requires_approval` 或 `destructive`） |
| `write_free` | 同 `write` | **全部旁路**；宿主换恒真 hook，`Policy` 本身不改 |

两个容易踩的点：

- `todo.write` / `ask_user` 声明了**空 scopes**，三档下都可用——它们是会话内交互，不触碰外部状态。
- **MCP 工具不受档位约束**：适配器产生的工具默认 `scopes=()` 且 `requires_approval=False`。
  要收口请给 MCP server 配 `default_scopes` / `default_requires_approval`。

## 5. 调用标识约定

| id | 来源 | 用途 |
|---|---|---|
| `tool_call_id` | 模型给出，原样透传 | 与 LLM 协议对齐，结果归位 |
| `trace_id` | 框架生成（`Context`） | 会话级追溯；`Tracer.record` 会从 contextvar 兜底 |
| `span_id` | 框架生成（`Tracer`） | 单次执行追溯 |
| `parent_span_id` | 不传时兜底为「当前 run 的根 span」 | 嵌套 / 多 Agent 的父子关联；因而一次 run 的 span 天然成树 |
| `args_digest` | 入参 sha256 前 12 位 | 日志关联 / 去重键 |

> `tool_call_id` 只用于协议对齐，绝不当内部主键；重试会产生新的 `span_id`。
>
> **观测的两个 contextvar**（`harness/tool/telemetry.py`）：`AgentLoop.run` 在 run 边界
> `set_current_trace(ctx.trace_id)` + `set_current_parent(run_span_id)`，于是**拿不到 `Context` 的
> Provider** 也能把 span 关联到本次 run。查询用 `Tracer.spans_for / tree / summary`；
> 离线读日志用 `harness.config.read_spans / read_tree`。详见
> [docs/observability-design.md](../../docs/observability-design.md)。

## 6. 错误类型（ErrorType）

| 类型 | 含义 | 可重试 |
|---|---|---|
| `tool_not_found` | 工具不存在 | 否 |
| `invalid_args` | 参数校验失败 | 否 |
| `permission_denied` | 权限不足 / 审批拒绝 | 否 |
| `approval_required` | 需要人工确认但无回调 | 否 |
| `sandbox_denied` | 路径越界 / 命令引用越界路径（`sandbox` 层） | 否 |
| `timeout` | 执行超时 | 幂等时是 |
| `execution_error` | 工具内部异常 | 幂等时是 |

## 7. 测试

```bash
python -m harness.tool.test.test_tool_system   # 或 pytest
```

详见 [test/README.md](test/README.md)。

## 8. 内置工具规划（P0–P3）

按优先级分档推进，`builtin/` 下按域拆分文件。约定：`namespace.action` 命名；工具只做一件事。
详见 [builtin/README.md](builtin/README.md)。

### P0 核心（已实现）

| 工具 | 文件 | side_effect | scopes | 审批 |
|---|---|---|---|---|
| `fs.read` | fs.py | read | fs:read | 否 |
| `fs.write` | fs.py | write | fs:write | 否 |
| `fs.edit` | fs.py | write | fs:write | 否 |
| `fs.list` | fs.py | read | fs:read | 否 |
| `fs.glob` | fs.py | read | fs:read | 否 |
| `fs.grep` | fs.py | read | fs:read | 否 |
| `shell.run` | shell.py | destructive | shell:run | **是** |
| `todo.write` | todo.py | write | — | 否 |
| `ask_user` | interaction.py | read | — | 否 |

`shell.run` 安全策略：**白名单 + 禁止模式 + 审批**三层。
- 白名单（`ALLOWED_COMMANDS`）：git / python / pytest / npm / node / go / cargo 等常用命令。
- 禁止模式（`DENY_PATTERNS`）：`rm -rf`、`del /f`、`format`、`mkfs`、`shutdown`、`curl|sh` 等，命中即拦截。
- 命中策略的工具统一走 `policy` 审批（`requires_approval=True`）。

### P1 / P2 扩展（已实现）

| 工具 | 文件 | side_effect | scopes | 审批 |
|---|---|---|---|---|
| `net.fetch` | net.py | read | `net:read` | 否 |
| `net.download` | net.py | write | `net:read` `fs:write` | 否 |
| `fs.mkdir` | fs.py | write | `fs:write` | 否 |
| `fs.copy` | fs.py | write | `fs:write` | 否 |
| `fs.move` | fs.py | write | `fs:write` | 否 |
| `fs.delete` | fs.py | destructive | `fs:write` | **是** |
| `time.now` | clock.py | read | — | 否 |
| `code.run` | code.py | destructive | `code:run` | **是** |

- **域名策略**（`sandbox.check_url`，见 §4.5）：默认放行 + SSRF 基线黑名单（`localhost`、`127.0.0.1`、
  `169.254.169.254`、`*.internal` / `*.local` 等**始终拒绝**，配置只能追加）；只允许 http/https；
  重定向**逐跳复查**。`net.download` 边下边写、超限中止且不留半成品、`os.replace` 原子落位。
- **`code.run`**：`language` + `code` 结构化入参，`create_subprocess_exec`（**不经 shell**）；
  临时脚本与 cwd 落在工作区内 `data/tmp/`；解释器取自 shell 白名单（python / node）；
  **子进程环境变量走 allowlist**（不继承 `OPENAI_API_KEY` 等）；`finally` 必清临时目录。
  **是应用层沙箱，不是 OS 级隔离**（见 [docs/builtin-tools-design.md](../../docs/builtin-tools-design.md) §6）。
- **`fs.delete`**：目录必须显式 `recursive=True`；**拒绝删除工作区根目录**。
- 设计文档：[docs/builtin-tools-design.md](../../docs/builtin-tools-design.md)。

### 待实现

| 工具 | 用途 | 备注 |
|---|---|---|
| `net.search` | 网络搜索 | 需先定 provider（Tavily / Brave / …），无 key 即死代码 |
| `agent.dispatch` | 派发子 agent 处理独立子任务 | 属编排层（P3） |
| `process.*` | 长驻进程管理（启动/查询/终止） | 高权限隔离（P3） |
| `mcp.*` | 桥接 MCP 外部工具 | **已实现**：stdio + streamable-http，见 §9 |

## 9. MCP 工具接入

依赖：官方 `mcp` SDK（`pip install mcp`）。**未安装也不影响工具系统本身**——
`harness.tool.__init__` 不导入 mcp，需显式 `from harness.tool.loader import load_mcp_servers`。

### 9.1 用法

```python
from harness.tool import ToolRegistry, Executor, Context
from harness.tool.adapter.mcp import MCPServerConfig
from harness.tool.loader import load_mcp_servers

registry = ToolRegistry()
config = MCPServerConfig(
    name="fs",                          # 注册名前缀：工具会变成 fs.<原始名>
    transport="stdio",                  # stdio | http
    command="python",
    args=["-m", "my_mcp_server"],
)
loaded = await load_mcp_servers([config], registry)   # 连接 + 发现 + 注册
try:
    executor = Executor(registry)
    result = await executor.call("fs.read_file", {"path": "a.txt"}, Context())
finally:
    await loaded.aclose()               # 关闭所有 MCP 连接
```

HTTP 传输：`MCPServerConfig(name="x", transport="http", url="https://host/mcp")`。

> **`aclose()` 的返回值**：`LoadedServers.aclose()` 逐个关闭并返回告警列表（不是抛异常）。
> 原因：MCP 的 stdio 客户端内部用 anyio 的 task group，而 anyio 的 cancel scope **必须在创建它的
> 同一个 task 里退出**——宿主通常在 lifespan 任务里连接、在请求任务里关闭（重连 / 停用 MCP），
> 此时 anyio 会抛 `RuntimeError`；但那时子进程已终止、连接实际已断，所以记为告警而不让请求 500。
> 单个 client 关不掉也不阻断其余 client。

### 9.2 执行路径

MCP 工具经 `register_external(spec, adapter)` 注册，无 Python 签名：
- **校验**：用 MCP 提供的 `inputSchema` 走 `jsonschema`（本地工具仍走 pydantic）。
- **执行**：`Executor` 调用适配器 → `session.call_tool(name, args)`，并统一套超时/重试/追踪。
- **结果**：`content` 中的 text / image / resource 转为文本；`is_error=True` 转为 `execution_error`。

### 9.3 权限（默认宽松）

按既定策略，MCP 工具**默认宽松**：`side_effect="read"`、`requires_approval=False`、`scopes=()`。
需要加严时在配置里覆盖：

```python
MCPServerConfig(
    name="fs",
    command="python", args=["-m", "my_mcp_server"],
    # server 级默认
    default_side_effect="write",
    default_requires_approval=False,
    default_scopes=["fs:write"],
    # 按工具覆盖，优先级高于 server 级默认
    tool_overrides={
        "write_file": {"side_effect": "destructive", "requires_approval": True},
    },
)
```

> 注意：MCP 协议本身不声明 `side_effect`/`scopes`，默认宽松意味着外部工具会绕过内置工具的严格默认值，
> 生产环境建议用 `tool_overrides` 逐个标注高危工具。

## 10. 后续架构扩展（尚未实现）

- `ProcessTool` 适配器（长驻进程隔离）
- 插件发现（entry points / 目录扫描）
- 沙箱（路径/域名/命令白名单）下沉到统一 `sandbox` 层
- 大结果落地为文件 + 引用
- Trace 外发（JSONL / OpenTelemetry）
