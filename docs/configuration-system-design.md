# 配置系统设计文档

> 定位：**harness 的统一配置层（YAML + `${ENV}` 插值 → 装配成各模块对象）** · 技术栈：**Python + PyYAML** · 状态：**已实现 M1–M4**
> 模块文档：[harness/config/README.md](../harness/config/README.md)
> 相关：[观测落地设计文档](observability-design.md) · [记忆系统设计文档](memory-system-design.md) · [工具系统设计文档](agent-tool-system-design.md) · [LLM 接入层设计文档](llm-system-design.md) · [Agent Loop 设计文档](agent-loop-design.md) · [会话管理设计文档](session-management-design.md) · [FastAPI 服务层设计文档](api-service-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

现在「一个能跑起来的 Agent」需要的配置散落在 **7 个 dataclass / 构造参数 / 模块常量**里，
而且其中**一半改不了源码就改不动**。本模块把它收敛成：

```
harness.yaml（结构）  +  .env（密钥）  +  环境变量（临时覆盖）
        └──────────────┬──────────────┘
                 loader：插值 → 深合并 → 校验
                       ↓
              HarnessConfig（纯数据，可 print 出来看全貌）
                       ↓
                 build：装配成运行期对象
                       ↓
   provider / tool registry / policy / session manager / skills / MCP / tracer
```

`build` 是**唯一**把配置变成对象的地方——CLI、FastAPI、测试都调它，不各写一份装配代码。

### 非目标（本期不做）

- 不做**自动**配置热重载（不监听文件变化）；但控制台可**手动**触发重建（`PUT /config` 写盘重建 /
  `POST /config/reload` 重读重建，见 §9）。
- 不做远程配置中心 / 配置加密 / KMS。
- 不做 profile 多环境继承（`dev`/`prod` 覆盖）——留待需要时再加一层 `profiles:`。
- 不做 CLI（`--config` / `--set` 语法）——优先级里给它留了位置，但本期只实现到「环境变量」这一层。
- 沙箱只做**配置与装配**：工作区边界与路径权限已落地（`tools.sandbox`，见 §7.2b），
  但**不是 OS 级隔离**——只拦"经工具传入的路径"。

---

## 2. 现状与缺口

### 现状：配置面散落

| 位置 | 载体 | 可配项 |
|---|---|---|
| `LLMConfig` | dataclass | model / api_key / base_url / timeout_s / max_retries / extra_headers |
| `LoopConfig` | dataclass | max_steps / max_total_tokens / temperature / max_tokens / tool_choice / model / stream / repeat_warn_at / repeat_stop_at / base_instructions |
| `SessionConfig` | dataclass | compact_threshold / keep_head / keep_tail / max_active_tokens / enable_compaction / auto_title / title_max_chars |
| `SkillRegistry(...)` | 构造参数 | root / max_desc_chars / max_body_chars / max_resource_chars |
| `Policy(...)` | 构造参数 | enforce_scopes / approval_for_destructive |
| `MCPServerConfig` | dataclass | 传输、命令、**按工具权限覆盖**（字段最全，但只能手写 Python） |
| `ServiceConfig` | `.env` + 环境变量 | 模型、密钥、data_dir、心跳、交互超时、run TTL、max_steps |
| `builtin/shell.py` | **模块常量** | `ALLOWED_COMMANDS` / `DENY_PATTERNS` ← 改不了 |

### 缺口与本期对策

| # | 缺口 | 影响 | 本期 |
|---|---|---|---|
| 1 | shell 白名单 / 禁止模式是模块常量 | 加个命令要改源码 | ✅ 可配（§7.2） |
| 2 | 无工具开关 | 做不了「只读模式」 | ✅ `tools.enabled/disabled`（§7.1） |
| 3 | MCP server 必须手写 Python | 接外部工具成本高 | ✅ YAML 列表（§7.3） |
| 4 | 无工作区根目录 | `fs.*` 能读全盘 | ✅ `tools.sandbox`（§7.2b） |
| 5 | Tracer 有 sink 钩子但无实现 | 观测落不了地 | ✅ JSONL sink（§7.4） |
| 6 | 无 profile / 无校验 / 报错定位差 | 错字段报 Python 类型错误 | 校验 ✅ / profile ⏳ |

---

## 3. 分层与目录

```
harness/config/
├─ __init__.py
├─ schema.py      # HarnessConfig 及分节 dataclass（纯数据，无 IO、无 IO 依赖）
├─ loader.py      # YAML 读取 + ${ENV} 插值 + 环境变量覆盖 + 深合并 + 校验
├─ dotenv.py      # 极简 .env 读取（从 server/config.py 迁来，去掉零散实现）
├─ build.py       # HarnessConfig → 运行期对象（唯一装配点）
├─ sink.py        # JsonlTracerSink（观测落地）
└─ test/
```

依赖方向：`harness/config → harness.{llm,agent,session,tool,skill}`（单向）。
`server/config.py` **已删除**——服务层直接消费 `HarnessConfig`（`create_app(config: HarnessConfig)`），
`.env` 读取搬到 `harness/config/dotenv.py`，配置不再有第二份实现。

---

## 4. 配置文件

默认路径 **项目根 `harness.yaml`**，可用 `HARNESS_CONFIG` 指定别的路径；文件不存在则不报错（等价于全默认值）。

```yaml
# harness.yaml — 结构配置（可提交仓库；密钥只写引用，见 .env）
llm:
  model: agnes-3.0-flash
  base_url: https://api.agnes-ai.cn/v1
  api_key: ${OPENAI_API_KEY}     # 插值；变量缺失直接报错
  timeout_s: 60
  max_retries: 2

agent:
  max_steps: 12
  stream: true
  max_total_tokens: 200000       # 空则不限
  base_instructions: ""          # 基础系统指令（拼进 system prompt 最前面）；空 = 只由技能目录 + 记忆构成
  # temperature: 0.2

session:
  compact_threshold: 40
  keep_head: 0
  keep_tail: 16
  enable_compaction: true
  auto_title: true               # 首轮问答结束后用 LLM 总结标题（失败回退截断首条用户消息）
  title_max_chars: 24            # 生成标题的字数上限

skills:
  root: skills                   # 空字符串 → 关闭 skill catalog
  disabled: []                   # 停用名单：目录仍在、管理端点仍可见，但不进 catalog、不可 load/read
  max_body_chars: 60000

memory:
  enabled: true
  path: MEMORY.md                # 跨会话长期记忆；内容注入 system prompt
  max_chars: 4000                # 超限时 memory.save 报错（不静默截断）
  auto_extract: false            # 压缩时抽到的候选记忆是否直接写入

tools:
  enabled: ["fs.*", "shell.run", "todo.*", "ask_user"]   # 白名单，支持通配；留空 = 全开
  disabled: []                                            # 只读模式：["fs:write", "fs:edit", "shell.run"]
  policy:
    enforce_scopes: true
    approval_for_destructive: true
  shell:
    allowed_commands: ["python", "git", "pytest", "npm"]   # 留空 = 用内置默认白名单
    deny_patterns: ["\\brm\\s+-rf"]
  sandbox:                                                 # 工作区边界 + 路径权限（默认关闭）
    enabled: false                                         # opt-in：开启会改变相对路径的基准（cwd → root）
    root: "."                                              # 工作区根，相对启动目录
    write_paths: []                                        # root 内写白名单 glob；留空 = 全可写
    deny_paths: []                                         # 最高优先级黑名单 glob（读写都拒）
    readonly_mounts: []                                    # root 之外的额外只读目录
    writable_mounts: []                                    # root 之外的额外可写目录
    denied_domains: []                                     # 在 SSRF 基线之外追加域名（只能追加）
  net:
    max_bytes: 200000                                      # net.fetch 单次读取上限
    max_download_bytes: 20000000                           # net.download 单文件上限

# 空列表则不连接任何 MCP server
mcp:
  servers:
    - name: filesystem
      enabled: true                # false → 声明保留（管理端点 / healthz 仍可见），但不连接、不注册工具
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      tool_prefix: mcpfs
      default_side_effect: read
      default_requires_approval: false
      default_scopes: []
      default_timeout_s: 60
      tool_overrides:
        write_file:
          side_effect: destructive
          requires_approval: true
    - name: remote-docs
      transport: http
      url: https://example.com/mcp

observability:
  tracer_sink: jsonl             # none | jsonl | otel（otel 见 observability-design.md，需装 [otel] extra）
  tracer_path: data/traces.jsonl
  tracer_max_spans: 500          # 内存中保留的 span 上限（防长跑膨胀）

server:
  host: 127.0.0.1
  port: 8000
  data_dir: data/sessions
  heartbeat_s: 15
  interaction_timeout_s: 300
  run_ttl_s: 600
```

### 两个开关字段

> **模型自己改配置**：让 AI 直接编辑 `harness.yaml`（MCP / 模型 / 循环参数 / 技能与工具开关等）、
> 改完由用户重载或重启生效 —— 字段速查与「允许 / 禁止改」清单见
> [配置参考（harness.yaml）](configuration-reference.md)，方案见
> [harness-config 技能设计文档](harness-config-skill-design.md)。本节以下仍只讲这两类字段的**机制**。

| 字段 | 语义 | 真源 / 运行期镜像 |
|---|---|---|
| `skills.disabled` | **停用名单**：技能目录仍在、管理端点仍可见，但不进 catalog、不可 `load` / `read` | 持久真源写在 `harness.yaml`；运行期镜像在 `SkillRegistry._disabled`，由 `set_disabled()` 热切换 |
| `mcp.servers[].enabled` | `false` 时**声明保留**（管理端点与 `/healthz` 仍能看到），但不连接、不注册工具 | 放在 `name` 之后，使 `ordered_config()` 写出的 YAML 键序可读（`enabled` 紧跟 `name`） |

`enabled` 的类型校验沿用既有的 `build_dataclass` / `coerce`：`enabled: maybe` → `ConfigError`，
`path` 为 `mcp.servers[0].enabled`。

> `build_skills` 把 `disabled=config.skills.disabled` 传给 `SkillRegistry(...)`；
> 而 `build_mcp_configs` / `to_runtime_mcp` **不变**——运行期 `MCPServerConfig`
> （[harness/tool/adapter/mcp.py](../harness/tool/adapter/mcp.py)）刻意**不带** `enabled`，
> 因为它是**传输描述符**；是否连接是**宿主（`server/`）生命周期决策**
> （`AppState._enabled_mcp_configs()` 在 `start()` 前过滤）。

### 与 `.env` 的分工

| 文件 | 放什么 | 能否提交 |
|---|---|---|
| `harness.yaml` | **结构**（模型名、工具开关、MCP 列表、目录…） | ✅ 可以（密钥处只写 `${VAR}`） |
| `.env` | **密钥与机器相关值** | ❌ 不提交 |

`.env` 在 `harness.yaml` **之前**加载进环境，因此配置文件里的 `${OPENAI_API_KEY}` 能取到它。

---

## 5. 优先级与插值

### 优先级（高 → 低）

```
CLI --set（本期不实现，位置预留）
  > 真实进程环境变量
  > .env
  > harness.yaml
  > schema.py 里的代码默认值
```

即：`harness.yaml` 提供结构底稿 → `.env`/环境变量按**已知字段映射**覆盖它 → 缺的字段落到默认值。

### 环境变量映射（保持向后兼容）

现有 `HARNESS_*` / `OPENAI_*` 变量继续有效，映射到配置路径：

| 环境变量 | 配置路径 |
|---|---|
| `OPENAI_API_KEY` | `llm.api_key` |
| `OPENAI_BASE_URL` | `llm.base_url` |
| `HARNESS_MODEL` | `llm.model` |
| `HARNESS_MAX_STEPS` | `agent.max_steps` |
| `HARNESS_SKILLS_DIR` | `skills.root`（空串 → 关闭） |
| `HARNESS_DATA_DIR` | `server.data_dir` |
| `HARNESS_INTERACTION_TIMEOUT_S` | `server.interaction_timeout_s` |
| `HARNESS_HEARTBEAT_S` | `server.heartbeat_s` |
| `HARNESS_RUN_TTL_S` | `server.run_ttl_s` |
| `HARNESS_CONFIG` | 配置文件路径本身 |
| `HARNESS_ENV_FILE` | `.env` 路径本身 |

### `${ENV}` 插值规则

**先解析 YAML，再对字符串值做插值**（不是把变量值拼进 YAML 文本）：

```python
# 配置里写：  api_key: ${OPENAI_API_KEY}
# 环境里是：  OPENAI_API_KEY='abc#not-comment:still-value'
# 若先拼文本 → YAML 会把 # 当注释截断、把 :  当映射分隔符，配置直接坏掉
# 解析后插值  → 拿到完整字符串，结构不受影响
```

| 写法 | 行为 |
|---|---|
| `${VAR}` | 取环境变量；**缺失即报错**（fail fast，避免把空串当密钥静默传下去） |
| `${VAR:-default}` | 缺失时用 `default` |

---

## 6. 校验与报错

`loader.load()` 在返回前做一次全量校验，**错误信息带 YAML 字段路径**：

```
配置错误 harness.yaml → tools.enabled: 期望字符串列表，实际是 str（"fs.read"）
配置错误 harness.yaml → mcp.servers[0]: 缺少必填字段 name
配置错误 harness.yaml → llm.timeout_s: 期望数字，实际是 "60s"
配置错误 harness.yaml → unknown_section: 未知配置节（可用：llm/agent/session/skills/tools/mcp/observability/server）
```

规则：

- **未知键报错**（而不是静默忽略）——打错一个 `max_step` 不该悄悄用默认值跑。
- 类型可宽松转换（`"60"` → `60`），转换失败才报错。
- `enabled` / `allowed_commands` 等留空 = 语义化默认（全开 / 内置白名单），不是错误。
- 报错统一抛 `ConfigError`（`harness/config/schema.py` 定义），带 `path` 与 `message` 两个字段，便于 CLI 友好打印。

实现方式：对每节做**声明式字段表**（字段名 → 类型 / 默认值 / 子结构），遍历 YAML 键比对，
不引入 pydantic 依赖（`harness` 侧目前只用 pydantic 于工具入参建模；配置层保持零第三方依赖，仅需 PyYAML）。

---

## 7. 装配（build.py）

### 7.1 工具开关

**不在 `ToolRegistry` 里加过滤逻辑**（避免把业务策略塞进核心类），而是**组装期裁剪**：

```python
# ToolRegistry 新增一个小方法（约 8 行）
def register_tool(self, registered: RegisteredTool) -> RegisteredTool:
    """复用已有 RegisteredTool（spec + fn + input_model）注册，不重建入参模型。"""
```

`build.py` 于是可以明确地挑工具：

```python
registry = ToolRegistry()
for spec in default_registry.list():                 # 内置工具都是现成的 RegisteredTool
    if allowed(spec.name):                           # fnmatch 通配匹配 enabled/disabled
        registry.register_tool(default_registry.get(spec.name))
for name in register_skill_tools(registry, skills):  # skill 工具同理受 allowed 约束
    ...
```

匹配语义：`enabled` 留空 → 全开；否则仅名字匹配其中任一 glob 的才启用；`disabled` 优先级更高，
命中即剔除。glob 用 `fnmatch.fnmatchcase`（`fs.*` 匹配 `fs.read`）。

> **为什么不改 `ToolRegistry.get()`**：裁剪后的 registry 里根本不存在被禁用的工具，
> Executor 的 `lookup` 自然返回 `tool_not_found`，无需在核心类里再加一层判断。

### 7.2 shell 白名单可配

`shell.py` 现在的 `ALLOWED_COMMANDS` / `DENY_PATTERNS` 是模块常量、`_guard()` 直接读。
本期做法：**常量降级为「默认值」，新增进程级 `configure_shell_policy()`**：

```python
# harness/tool/builtin/shell.py
DEFAULT_ALLOWED_COMMANDS = frozenset({...})     # 原常量改名为默认值
DEFAULT_DENY_PATTERNS = (...)                   # idem
_policy = ShellPolicy(DEFAULT_ALLOWED_COMMANDS, DEFAULT_DENY_PATTERNS)

def configure_shell_policy(*, allowed_commands=None, deny_patterns=None) -> ShellPolicy:
    """覆盖本进程的 shell 白名单/禁止模式；返回旧值（便于测试还原）。"""
```

`build.py` 启动时调用一次。

> **诚实的取舍**：这是**进程级全局状态**，多个 AppState 共用一份 shell 策略。
> 更好的归属是沙箱层——沙箱已落地（`harness/tool/sandbox.py`，见 [sandbox-design.md](sandbox-design.md)），
> 但它同样选择了**进程级全局**（`configure_sandbox`），因此 shell 策略暂未迁走：
> 两者现在是一对同款的进程级策略，迁移可在多租户需求出现时一并做。

### 7.2b 沙箱（工作区边界 + 路径权限）

```python
# harness/config/build.py
def build_sandbox(config: HarnessConfig) -> SandboxPolicy:
    """把 YAML 里的 tools.sandbox 应用到进程级沙箱策略。"""
```

`server/state.py::build_state` 在 `build_shell_policy(config)` 旁边调一次。
`fs.*` 与 `shell.run` 在执行时读这份策略；越界返回 `sandbox_denied`。
**默认 `enabled: false`**，不配就完全不影响既有行为（`resolve_path()` 原样返回入参）。
详见 [沙箱设计文档](sandbox-design.md)。

**这一节只是「进程级默认」。** 控制台可以**按会话**选工作区与权限档位，它们存在会话的
`metadata` 里（不写回 `harness.yaml`），在每轮 run 开始时覆盖默认值：

| 会话设置 | 相对 `harness.yaml` 的优先级 |
|---|---|
| `metadata.workspace` | **高于** `tools.sandbox.enabled` / `root`：设了就为该会话开沙箱、`root` 用它；`deny_paths` / 挂载点 / 域名黑名单仍**继承** `harness.yaml`（安全基线不该因换工作区失效）。没设 → 完全沿用 `harness.yaml`（含 `enabled:false` 的无边界语义） |
| `metadata.permission` | **高于** `tools.policy.enforce_scopes` / `approval_for_destructive`：`read`/`write`/`write_free` 直接决定 scopes 与是否旁路审批（映射见 [harness/tool/permission.py](../harness/tool/permission.py)）。没设 → 沿用请求体 `scopes` 与 `tools.policy` 的判定（改造前行为） |

> 取舍说明：会话级设置**刻意不落 `harness.yaml`**——它是「这次对话的用法」，不是「这台机器的策略」；
> 写成全局配置会让"换个会话换个工作区"变成"改文件 + 重建运行时"。
> 反过来，会话级设置也**不能**放宽安全基线（`deny_paths` / SSRF 名单 / shell 白名单仍由 `harness.yaml` 决定）。

**域名**是同一个策略对象的第二个维度，但**开关独立**：`denied_domains` 追加到 SSRF 基线
（`localhost` / `169.254.169.254` / `*.internal` 等），基线**始终生效**、不可覆盖。

### 7.2c 网络上限

```python
# harness/config/build.py
def build_net_policy(config: HarnessConfig) -> NetPolicy:
    """把 YAML 里的 tools.net 应用到进程级网络上限。"""
```

`net.fetch` / `net.download` 在执行时读 `current_net_policy()`。
域名策略不在这一节（它在 `tools.sandbox`）——两个节各管一件事。
详见 [内置工具扩展设计文档](builtin-tools-design.md) §7。

### 7.3 MCP server 列表

装配拆成**同步 + 异步两段**，因为 MCP 连接是异步的，而 `create_app` 必须同步可用
（测试用 `httpx.ASGITransport` 不跑 lifespan）：

```
build_state(config)   → 同步：provider/registry/executor/session/skills/tracer 就绪；
                        把 mcp 配置挂在 AppState.mcp_configs 上（全量声明），尚未连接
await state.start()   → 异步：load_mcp_servers(_enabled_mcp_configs(), registry)，
                        只连 enabled 的声明；连接成功后才注册 MCP 工具；失败则整体报错退出（fail fast）
```

`state.start()` 由 `create_app` 的 lifespan 调用；测试注入自己的 `AppState` 时不会触发。
`_enabled_mcp_configs()` 是宿主侧的过滤点：`mcp_configs` 保持「全量声明」语义
（`/mcp` 与 `/healthz` 仍能看到停用项），只有 `enabled: true` 的才真正连接。
`state.aclose()` 里追加 `loaded_servers.aclose()`（关闭 MCP 长连接）。

### 7.4 观测 sink

`Tracer(sink=callable)` 已是现成钩子，只需一个落地的 sink：

```python
# harness/config/sink.py
class JsonlTracerSink:
    """把 span 追加写进 JSONL（一线一 span），失败静默（观测不能影响主流程）。"""
    def __init__(self, path: Path) -> None: ...
    def __call__(self, span: dict[str, Any]) -> None: ...
```

`tracer_sink: none` → `Tracer()`；`jsonl` → `Tracer(sink=JsonlTracerSink(path))`；
`otel` → `Tracer(sink=OtelTracerSink(...))`（延迟导入，未装 `[otel]` extra 时报 `ConfigError`）。
三种 sink 都只是 `Callable[[dict], None]`——`Tracer` 不关心具体是谁。

顺带补一个 **`tracer_max_spans`**（默认 500）：`Tracer.spans` 在长跑服务里会无限增长，
是当前实现的一个真实隐患；超过上限丢弃最旧的 span（sink 已在写入时外发，不丢数据）。

读侧与写侧同在 [sink.py](../harness/config/sink.py)：`read_spans(path, trace_id=...)` /
`read_tree(...)` 从 JSONL 读回 span（流式读、坏行跳过），供离线分析与
[观测落地](observability-design.md) 的 `GET /runs/{id}/trace?source=file` 使用。

### 7.5 长期记忆

记忆不是"配置"，但**它的开关、落点与上限都是配置**：

```python
memory   = build_memory(config)                        # enabled / path / max_chars
registry = build_registry(config, skills, memory)      # 注册 memory.*（受工具开关约束）
prompt   = build_prompt(skills, memory, config.agent.base_instructions)   # 拼进 system
manager  = build_session(config, memory, tracer, provider)   # 压缩时抽候选
```

> `agent.base_instructions`（新增字段，默认空）由 `PromptBuilder` 拼进 system prompt 的**最前面**，
> 排在技能目录与长期记忆之前；Loop 自身不读它。`build_prompt(skills, memory, base_instructions="")`
> 是它的装配入口。

一个要点：`build_session` 需要**可用的 provider**（压缩器固定为 `LLMCompressor`）——
provider 缺失时直接抛 `ConfigError`，不存在「无压缩器」的静默降级。开了
`memory.auto_extract` 时压缩会**顺带抽取**候选记忆（同一次模型调用），否则只产摘要。
详见 [记忆系统设计文档](memory-system-design.md)。

---

## 8. 与 server 的关系

```
harness/config/  ← 通用配置（CLI / server / 测试共用）
      ↓ load_config() → HarnessConfig
      ↓ build_* 装配（唯一装配点）
server/state.py build_state(config: HarnessConfig)  →  AppState
      ↓ lifespan: await state.start()   ← 此处才连 MCP（异步）
server/app.py create_app(config=None)  ← 缺省 load_config()
```

原先 `server/state.py` 手写的装配全部消失，改为调 `harness.config.build.*`：

| 原先 server/state.py 手写 | 现在 |
|---|---|
| `OpenAICompatProvider(LLMConfig(...))` | `build_provider(cfg.llm)` |
| 往 `default_registry` 打补丁 + `registry.has()` 防重复 | `build_registry(cfg, skills)` 组装**独立** registry |
| `SessionManager(JsonlSessionStore(...), config=SessionConfig())` | `build_session(cfg)` |
| `LoopConfig(stream=..., max_steps=...)` | 直接用 `cfg.agent` |
| `Tracer()` | `build_tracer(cfg.observability)` |
| `ServiceConfig`（`.env` + 环境变量） | **已删除**，改为 `HarnessConfig` 的 `server` / `llm` / `agent` / `skills` 分节 |

> **附带修掉的问题**：原先 `build_state` 往 `default_registry`（模块级单例）里注册 skill 工具，
> 靠 `registry.has()` 防重复——多份配置共用同一进程时会互相污染。现在组装独立 registry，
> 这个补丁彻底不需要了。

---

## 9. 配置写入与热重建

配置不只在启动时读一次：控制台（`GET|PUT /config`、`POST /config/reload`）可**读写 `harness.yaml`
并原地重建运行时**。数据加工集中在 [admin.py](../harness/config/admin.py)，它**只碰文件与数据，不做装配**——
装配在 [server/state.py](../server/state.py) 的 `AppState.apply_config()`（见 §8）。

### 9.1 admin 的职责与函数

| 函数 / 常量 | 职责 |
|---|---|
| `SENTINEL = "***"` / `SECRET_PATHS = (("llm","api_key"),)` | 脱敏占位符与需脱敏的字段路径（目前只有 `llm.api_key`） |
| `redact(data)` | 把敏感字段替换成占位符：`${VAR}` 引用**原样保留**，其余字面量一律 `***`（返回深拷贝，不改原对象） |
| `restore_secrets(new, old)` | 提交里 `***` / 空 / `None` 表示「未改动」→ 还原为旧值；明确改了引用则保留新值 |
| `is_plaintext_secret(data)` | 返回第一个「明文（既非 `${VAR}` 也非占位/空）」的敏感字段路径，用于拦截落盘 |
| `current_env(env_file=None)` | 当前生效的环境层：`.env` + `os.environ`（后者覆盖前者） |
| `env_override_report(env)` | 列出真正生效的环境变量覆盖（这些项**改文件也无效**），敏感值脱敏 |
| `validate(data, *, env=None)` | 插值 + 环境覆盖 + 校验 → `HarnessConfig`（出错抛 `ConfigError`，带 `path`） |
| `ordered_config(data, cls=HarnessConfig)` | 按 dataclass 字段序**递归重排**，让 dump 出的 YAML 键序稳定可读 |
| `dump_raw(data, path, *, backup=True)` | 回写 YAML：`backup` 时先复制 `<file>.bak`，写 `.tmp` 再 `os.replace` **原子替换**，文件头插入 `MANAGED_HEADER` |
| `read_yaml(path)` / `backup_path(path)` | 读原始 YAML（不插值、不叠环境）/ 计算 `.bak` 路径 |

### 9.2 写回流程（`PUT /config`）

```
读旧 YAML → restore_secrets（占位/空/None = 未改动 → 还原旧值）
         → is_plaintext_secret（明文密钥 → 422 config_invalid）
         → validate（插值 + 环境覆盖 + 校验；出错 422 config_invalid，details.path 指向字段）
         → dump_raw（备份 .bak → 写 .tmp → os.replace 原子替换）
         → apply_config（原地重建 provider/registry/manager/prompt/tracer/…）
         → 失败回滚文件（dump_raw(previous, backup=False)）+ 422
```

`POST /config/reload` 是它的只读版：**只从磁盘重读并重建**，不写盘——用于在编辑器里手改 YAML 之后。

### 9.3 重建 vs 热更

- **全量重建，不做字段级热更**：改一行配置会重建全部运行时部件（provider / registry / executor /
  manager / prompt / tracer / title 生成器…）。理由：语义简单，不会出现「一半新一半旧」的中间态。
- **不换 `AppState` 对象**：`apply_config` **原地替换**各部件字段，因此路由通过依赖注入
  （`StateDep`）拿到的引用始终有效。
- **进程级策略一并重置**：`tools.shell` / `tools.sandbox` / `tools.net` 是进程级全局策略，
  重建时按新配置重新应用（旧的被覆盖）。
- **有 run 时拒绝**：任何 run 正在执行 → 409 `config_reload_conflict`（重建会中断它）。
- **失败回滚**：`apply_config` 抛 `ConfigError`（如启用了未安装的 `otel` sink）时，文件回滚为写盘前内容。

### 9.4 `.env` 与 `harness.yaml` 的职责边界

- **明文密钥只放 `.env`**；`harness.yaml` 里只写 `${VAR}` 引用（如 `api_key: ${OPENAI_API_KEY}`）。
  提交明文密钥 → 422（`is_plaintext_secret` 拦截）——避免密钥被写进可提交的配置文件。
- **写盘会丢注释与键序**：回写用 `yaml.safe_dump`（零新依赖），原文件的注释与手工键序会被**规范化**；
  旧文件备份到 `<file>.bak`，新文件头插入固定说明（`MANAGED_HEADER`）。
- 环境变量优先于文件：`env_overrides` 会列出这些覆盖项，提示「即使改了文件也不生效」。

### 9.5 为何不从 `harness.config.__init__` 导出 admin

`admin.py` 导入 `loader` / `schema`，而 `__init__.py` 也导入 `loader`——若在 `__init__` 里再导出
`admin` 会形成**循环导入**。因此路由直接 `from harness.config.admin import ...`，
`harness.config.__init__` 不暴露 admin。

### 9.6 轻量开关：`PATCH` 与 `PUT /config` 的分工

除整份回写外，控制台还有两个**只翻一个字段**的轻量端点（`PATCH /skills/{name}` / `PATCH /mcp/{name}`），
都走 `AppState.patch_config(mutate)`：读 `harness.yaml` → 原地改字段 → `validate` →
`ordered_config` + `dump_raw` 原子落盘，**不重建任何部件**。

| 维度 | 轻量 `PATCH` | 全量 `PUT /config` |
|---|---|---|
| 改动范围 | 只翻一个字段（`skills.disabled` / `mcp.servers[i].enabled`） | 整份配置落盘 |
| 重建范围 | 只热重建对应部分（技能重扫过滤面 / MCP 重连），**不重建 provider / 会话 / loop_config** | **全量重建**（provider 也会重连） |
| 活跃 run | **不要求**无活跃 run（与 §9.3 的 409 形成对照） | 有活跃 run → 409 `config_reload_conflict` |
| 并发 | `AppState.config_lock`（`asyncio.Lock`）串行化读-改-写，避免丢更新 | 走 §9.2 处理链 |

两个端点各自的动作：

- `AppState.set_skill_enabled(name, enabled)`：只改 `skills.disabled` → 同步运行期镜像
  `self.config.skills.disabled` → `self.skills.set_disabled(...)` 热切换过滤面
  （`state.prompt` 持引用，无需重建）。
- `AppState.set_mcp_enabled(name, enabled)`：只改 `mcp.servers[i].enabled` → **先**同步
  `self.config.mcp.servers`（`reload_mcp()` 里 `build_registry` 与 `_enabled_mcp_configs()`
  都读 `self.config`，顺序反了开关无效）→ `await self.reload_mcp()`（只重连 MCP，
  不碰 provider / 会话 / loop_config）。

> **前端注意**：`PUT /config` 会把整份 `mcp.servers` 覆盖掉，所以 MCP 表单必须把 `enabled` 一起提交
> （前端已把它做成带 `data-*` 的表单控件，`_readMcpForm()` 会采集），否则用户刚停用的 server 一保存就复活。

**为什么开关不放进技能目录 / SKILL.md frontmatter**：开关状态是「这台机器怎么用」，属于配置中心的一份数据。
放 `harness.yaml` 既能「在配置中心里一眼看到」，又与其它配置走**同一条写入链路**。
不写进技能目录——那会**污染用户的技能目录**；不写进 SKILL.md frontmatter——那要求**改写用户文件**，且回写时
**容易丢未知字段**。所以 `skills.disabled` 是**持久真源**，运行期只是它的镜像；`mcp.servers[].enabled` 同理。

---

## 10. 里程碑

| 里程碑 | 内容 | 交付 |
|---|---|---|
| M1 | `schema.py`（分节 dataclass）+ `loader.py`（YAML + `${ENV}` + env 覆盖 + 校验）+ `dotenv.py` | ✅ 单测：插值 / 优先级 / 未知键报错 / 类型报错 |
| M2 | `build.py`：llm / agent / session / skills + **工具开关**（新增 `ToolRegistry.register_tool`） | ✅ 单测：白名单通配 / disabled 优先 / 只读模式 |
| M3 | **shell 白名单可配** + **MCP server 列表**（`state.start()` 异步连接） | ✅ 单测：自定义白名单生效 / MCP 配置解析 |
| M4 | **观测 sink（JSONL）** + server 接入（`ServiceConfig` 删除、`build_state` 改造）+ 文档 | ✅ 全量测试跑通；`harness.yaml` 模板落地 |
| M5 | **沙箱**（`tools.sandbox` + `build_sandbox` + `build_state` 接线） | ✅ config 3 条：默认关闭 / 应用策略后还原 / 关闭不改路径语义 |
| M6 | **域名黑名单追加 + `tools.net` 体积上限**（`build_net_policy`） | ✅ config 3 条：`denied_domains` 只能追加（基线仍在）/ `tools.net` 默认值 / `build_net_policy` 应用与还原 |
| M7 | **配置开关字段**（`skills.disabled` / `mcp.servers[].enabled`）+ **轻量开关端点**（`PATCH /skills/{name}` / `PATCH /mcp/{name}`，只翻字段 + 只热重建对应部分） | ✅ config 新增 3 条（config 全套 **57 条全离线**）：`enabled` / `disabled` 默认值 + 运行期描述符不带 `enabled` / `enabled` 非布尔报错（path 正确）/ `dump_raw` 后新键保留且 `enabled` 紧跟 `name` |

---

## 11. 兼容性

- `harness.yaml` **可以不存在**：缺失时行为与今天完全一致（全默认值 + `.env` + 环境变量）。
- 现有 `HARNESS_*` / `OPENAI_*` 环境变量与 `.env` 语义不变（§5 映射表），
  且 `.env` 的解析逻辑原样搬到 `harness/config/dotenv.py`（同名 `load_env_file`）。
- **破坏性变更**：`server/config.py` 与其 `ServiceConfig` **已删除**——
  它是服务层的内部实现，无外部引用。给服务换配置请传 `HarnessConfig`
  （`create_app(load_config("my.yaml"))`）或设 `HARNESS_CONFIG`。
- 各模块 dataclass（`LLMConfig` / `LoopConfig` / `SessionConfig` / `MCPServerConfig`）基本**未改**，
  配置层只负责「dict → 这些 dataclass」；仅新增**可选尾字段** `agent.base_instructions`、
  `session.auto_title` / `session.title_max_chars`（均带默认值，旧配置不受影响）。
- `SkillsConfig` 新增 `disabled`（默认 `[]`）；`McpServerConfig` 在 `name` 之后新增 `enabled: bool = True`
  （**非尾字段**，但带默认值且旧配置不写即启用，因此**旧配置逐字节兼容**）。
  新增字段都落在 `harness.yaml`（§4），不进技能目录 / SKILL.md frontmatter。

---

## 12. 决策记录

| 议题 | 结论 |
|---|---|
| 配置文件格式 | **YAML**（可读性 + 支持注释；`pyyaml` 已是既有依赖） |
| 模块落点 | **新建 `harness/config/`**（CLI / server / 测试共用；harness 作为库也能被复用） |
| 本期范围 | 工具开关 + shell 白名单可配 + MCP server 列表 + 观测 sink（四项全做）；后续补沙箱（M5） |
| 密钥处理 | **配置文件只写 `${VAR}` 引用**，明文只留在 `.env` / 环境变量 |
| 插值时机 | **解析 YAML 之后**对字符串值插值（拼文本会被密钥里的 `#` / `:` 破坏结构） |
| 工具开关实现 | 组装期裁剪（`register_tool` 复用），**不改** `ToolRegistry` 的查询语义 |
| shell 白名单实现 | 进程级 `configure_shell_policy()`；沙箱层已落地但同样选进程级，两者暂未合并（§7.2） |
| 沙箱配置位置 | `tools.sandbox`（贴着唯一消费者），不做顶层 `workspace` 节；**默认关闭** |
| 未知配置键 | **报错**（不静默忽略） |
| 校验依赖 | 自制声明式字段表（**不引入 pydantic**），仅依赖 PyYAML |
| `env` 参数语义 | 显式传入时**完全替换**环境层（不读 `.env` / `os.environ`），保证可复现 |
| MCP 配置数据结构 | 在 `config/schema.py` 另立一份纯数据，`build.py` 映射到适配器类型——让「读配置」不必拉入 mcp SDK |
| 配置回写 | 控制台 `PUT /config` 写回 `harness.yaml`（脱敏 / 还原 / 校验 / 备份 / 原子替换）并**原地重建**运行时；写盘会规范化注释与键序（§9） |
| 重建语义 | 全量重建各部件（不换 `AppState` 对象），进程级 shell / sandbox / net 策略一并重置；有 run 时 409（§9.3） |
| admin 导出 | **不从 `harness.config.__init__` 导出**（避免循环导入），路由直接 `from harness.config.admin import ...`（§9.5） |
| 开关状态落点 | `harness.yaml`（`skills.disabled` / `mcp.servers[].enabled`）——与其它配置走**同一条写入链路**；**不写**技能目录或 SKILL.md frontmatter（会污染用户目录、改写用户文件且易丢未知字段）（§9.6） |
| 开关字段与运行期的分工 | `skills.disabled` 是真源、运行期只镜像到 `SkillRegistry`；`mcp.enabled` 是宿主生命周期决策，运行期 `MCPServerConfig` **不带** `enabled`（§4） |
| 轻量开关端点 | `PATCH /skills/{name}` / `PATCH /mcp/{name}` 只翻一个字段 + 只热重建对应部分（技能过滤面 / MCP 重连），**不要求无活跃 run**；与 `PUT /config` 全量重建 / 有 run 时 409 形成对照（§9.6） |

### 仍待确认

- [ ] 是否需要 `profiles:`（dev / prod 覆盖）？现在靠多个 yaml 文件 + `HARNESS_CONFIG` 也能凑合。
- [x] `observability.tracer_sink` 是否需要 OTel？→ **已实现**，作为可选 sink（`none` / `jsonl` / `otel`），
      见 [observability-design.md](observability-design.md)。
- [ ] 配置里是否需要「工作区根目录」（缺口 4）？它属沙箱层，但配置文件是它的自然入口。
