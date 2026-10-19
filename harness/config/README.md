# config — 配置系统

> 状态：**已实现（M1–M4）**。设计见 [docs/configuration-system-design.md](../../docs/configuration-system-design.md)。

## 职责

把散在各模块的配置（dataclass 构造参数 / 模块常量）收敛成一份 **YAML + `${ENV}` 插值**，
并作为**唯一装配点**把配置变成运行期对象。

```
harness.yaml（结构）+ .env（密钥）+ 环境变量（临时覆盖）
        ↓ loader：解析 → 插值 → 环境变量覆盖 → 校验
   HarnessConfig（纯数据，可整个 print 出来看全貌）
        ↓ build：装配
provider / registry / policy / session / skills / MCP / tracer
```

CLI、`server`、测试都调 `load_config()` + `build_*()`，不各写一份装配代码。

## 目录结构

```
harness/config/
├─ README.md      # 本文档
├─ __init__.py    # 公共导出
├─ schema.py      # HarnessConfig + 分节 dataclass + ConfigError（纯数据，零第三方依赖）
├─ loader.py      # YAML 解析 + ${ENV} 插值 + 环境变量覆盖 + 校验（read_yaml / build_from_dict）
├─ dotenv.py      # 极简 .env 读取
├─ build.py       # 唯一装配点
├─ admin.py       # 控制台配置管理：脱敏 / 还原 / 校验 / 键序 / 回写（不做装配）
├─ sink.py        # trace 的 IO 层：JsonlTracerSink（写）+ read_spans / read_tree（读）
├─ otel_sink.py   # OtelTracerSink（可选依赖，延迟导入）
└─ test/          # 57 条全离线测试（含 test_admin.py 12 条）
```

## 快速上手

```python
from harness.config import load_config, build_registry, build_provider, build_tracer

config = load_config()                 # 缺省读 harness.yaml + .env + 环境变量
config.llm.model                       # 'agnes-3.0-flash'
registry = build_registry(config)      # 已按 tools.enabled/disabled 裁剪好
provider = build_provider(config.llm)
tracer = build_tracer(config.observability)
```

换配置文件：`load_config("prod.yaml")` 或设 `HARNESS_CONFIG=prod.yaml`。

## 配置优先级

```
显式 env 参数（load_config(env=...)）  ← 完全替换环境层，测试/复现用
  > 真实进程环境变量
  > .env
  > harness.yaml
  > schema.py 代码默认值
```

环境变量到配置路径的映射（既有语义，保持兼容）：

| 环境变量 | 配置路径 |
|---|---|
| `OPENAI_API_KEY` | `llm.api_key` |
| `OPENAI_BASE_URL` | `llm.base_url` |
| `HARNESS_MODEL` | `llm.model` |
| `HARNESS_MAX_STEPS` | `agent.max_steps` |
| `HARNESS_SKILLS_DIR` | `skills.root`（空串 → 关闭 skill catalog） |
| `HARNESS_DATA_DIR` | `server.data_dir` |
| `HARNESS_INTERACTION_TIMEOUT_S` | `server.interaction_timeout_s` |
| `HARNESS_HEARTBEAT_S` | `server.heartbeat_s` |
| `HARNESS_RUN_TTL_S` | `server.run_ttl_s` |
| `HARNESS_CONFIG` | 配置文件路径本身 |
| `HARNESS_ENV_FILE` | `.env` 路径本身 |

## 三件容易踩的事

1. **插值在解析之后**：`api_key: ${OPENAI_API_KEY}` 是先把 YAML 解析成字符串、再替换。
   若把变量值拼进 YAML 文本，密钥里的 `#` 会被当注释、`:` 可能被当映射分隔符。
2. **未知字段报错**：`max_step`（少个 s）会直接抛 `ConfigError` 并指出路径，不会静默用默认值。
3. **`${VAR}` 缺失报错**：避免空串被当作密钥一路传下去；要可选就写 `${VAR:-默认值}`。

## 工具开关

```yaml
tools:
  enabled: ["fs.*", "shell.run"]    # 留空 = 全开；fnmatch 通配
  disabled: ["fs.write"]            # 优先级高于 enabled
```

实现是**组装期裁剪**（[build.py](build.py) 的 `build_registry`）：
被禁用的工具根本不进 `ToolRegistry`，Executor 的 lookup 自然返回 `tool_not_found`，
核心类不必懂「工具开关」。skill 工具（`skill.load` / `skill.read`）同样受开关约束。

只读模式示例：`disabled: ["fs.write", "fs.edit", "shell.run", "todo.write"]`。

## 技能停用名单

```yaml
skills:
  root: skills
  disabled: ["legacy-skill"]   # 停用：目录仍在、管理端点仍可见，但不进 catalog、不可 load/read
```

`disabled` 是**持久真源**（写在 `harness.yaml`），运行期镜像在 `SkillRegistry._disabled`，
由 `set_disabled()` 热切换——控制台 `PATCH /skills/{name}` 就用它，**不重建任何部件**。
停用只过滤「曝光面」：`scan()` 照旧全扫，所以管理端点仍能看到停用项（否则无法重新启用）。

> **为什么不写进技能目录 / SKILL.md frontmatter**：开关是「这台机器怎么用」而非技能自身的元数据；
> 写进技能目录会污染用户目录，改 frontmatter 则要动用户文件且容易丢未知字段。

## shell 白名单

```yaml
tools:
  shell:
    allowed_commands: ["python", "git", "pytest"]
    deny_patterns: ['\brm\s+-rf']
```

留空则沿用 [shell.py](../tool/builtin/shell.py) 里的 `DEFAULT_ALLOWED_COMMANDS` /
`DEFAULT_DENY_PATTERNS`。

> **这是进程级全局状态**（`shell.configure_shell_policy()`）：同一进程内所有 Agent 共用一份策略。
> 沙箱层已落地（见下节）但**同样选了进程级**，两者暂未合并。

## 沙箱（工作区边界 + 路径权限）

```yaml
tools:
  sandbox:
    enabled: false            # 默认关（opt-in）：开启会改变相对路径的基准（cwd → root）
    root: "."                 # 工作区根，相对启动目录
    write_paths: []           # root 内写白名单 glob；留空 = root 内全部可写
    deny_paths: []            # 最高优先级黑名单 glob（读写都拒），如 [".env", "**/.git/**"]
    readonly_mounts: []       # root 之外的额外只读目录
    writable_mounts: []       # root 之外的额外可写目录
```

装配期调一次 `build_sandbox(config)`（`server/state.py::build_state` 里，与
`build_shell_policy` 并列），把配置应用到进程级沙箱策略；`fs.*` 与 `shell.run`
在执行时读这份策略，越界返回 `sandbox_denied`。

**默认关闭意味着零影响**：不配 `tools.sandbox` 时 `resolve_path()` 原样返回入参，
既有行为逐字节不变。详见 [docs/sandbox-design.md](../../docs/sandbox-design.md)。

`denied_domains` 是同一节里的**域名**维度（追加到 SSRF 基线，基线不可覆盖、始终生效）——
`net.fetch` / `net.download` 用它，但**不需要先开 `enabled`**。

## 网络上限

```yaml
tools:
  net:
    max_bytes: 200000          # net.fetch 单次读取上限
    max_download_bytes: 20000000   # net.download 单文件上限
```

装配期 `build_net_policy(config)` 把配置应用到进程级策略（`net.current_net_policy()`）。
`net.download` 超过上限会**中止并删掉半成品**，不留垃圾文件。
域名策略在 `tools.sandbox.denied_domains`，不在这里。

## MCP server 列表

```yaml
mcp:
  servers:
    - name: filesystem
      enabled: true          # false → 声明保留（管理端点 / healthz 仍可见），但不连接、不注册工具
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      tool_prefix: mcpfs
      tool_overrides:
        write_file: { side_effect: destructive, requires_approval: true }
```

`enabled: false` 的声明**保留**（`/mcp` 与 `/healthz` 仍能看到），但不连接、不注册工具——
是否连接是**宿主的生命周期决策**（`AppState._enabled_mcp_configs()` 在 `start()` 前过滤）。
运行期 `MCPServerConfig` 刻意**不带** `enabled`（它是传输描述符）。放在 `name` 之后，
是为了 `ordered_config()` 写出的 YAML 键序可读。

装配分两段（MCP 连接是异步的，而 `create_app` 必须同步可用——测试用的
`httpx.ASGITransport` 不触发 lifespan）：

```python
state = build_state(config)   # 同步：工具/provider/session/tracer 就绪，尚未连 MCP
await state.start()           # 异步：连接 MCP 并把其工具注册进 registry
```

## 观测

```yaml
observability:
  tracer_sink: jsonl            # none | jsonl | otel
  tracer_path: data/traces.jsonl
  tracer_max_spans: 500         # 内存中保留的 span 上限（防长跑膨胀）
  # 仅 otel 时生效；endpoint 留空则用 OTEL_EXPORTER_OTLP_ENDPOINT
  otel_endpoint: http://127.0.0.1:4318
  otel_service_name: harness
```

三种 sink 都只是 `Callable[[dict], None]`，`Tracer` 不关心具体是谁：

| sink | 行为 |
|---|---|
| `none`（默认） | 只留内存索引（`Tracer.spans`），不落盘 |
| `jsonl` | `JsonlTracerSink` 一线一 span 追加写、带锁（工具并发执行）、写盘失败静默 |
| `otel` | `OtelTracerSink` 桥成 OTel span 经 OTLP/HTTP 导出（需 `pip install -e ".[otel]"`，未装时报 `ConfigError`） |

读侧与写侧同模块（IO 归一处）：

```python
from harness.config import read_spans, read_tree

read_spans("data/traces.jsonl", trace_id="abc")   # 按 trace 过滤；None = 全部
read_tree("data/traces.jsonl", trace_id="abc")    # 读回并组树
```

两者都**流式逐行读**（traces 文件会长），**坏行跳过不炸**（崩溃残留的半行），
文件不存在返回 `[]`。服务层 `GET /runs/{id}/trace?source=file` 就是它的出口。
详见 [docs/observability-design.md](../../docs/observability-design.md)。

## 长期记忆

```yaml
memory:
  enabled: true
  path: MEMORY.md         # 跨会话长期记忆；内容注入 system prompt，不存在视为空记忆
  max_chars: 4000         # 超限时 memory.save 报错（不静默截断）
  auto_extract: false     # 压缩时抽到的候选记忆是否直接写入（默认只记 span）
```

装配期做三件事：`build_memory`（造 store）、`build_registry(…, memory)`（注册
`memory.save` / `memory.forget`，同样受工具开关约束）、`build_prompt(skills, memory)`
（把记忆拼进 system）。`build_session(…, memory, tracer, provider)` 则把压缩时的候选记忆
接到落点：**只有 `auto_extract: true` 才换成 `LLMCompressor`**，不开启就不会多出模型调用。
详见 [docs/memory-system-design.md](../../docs/memory-system-design.md)。

## 配置管理（admin）：写入与热重建

[admin.py](admin.py) 是控制台 `GET|PUT /config` 背后的实现——**只碰文件与数据，不做装配**：

| 函数 / 常量 | 职责 |
|---|---|
| `SENTINEL` / `SECRET_PATHS` | 脱敏占位符 `***` 与需脱敏字段（`llm.api_key`） |
| `redact(data)` | `${VAR}` 引用保留、其余字面量 → `***`（返回深拷贝，不改原对象） |
| `restore_secrets(new, old)` | 提交 `***` / 空 / `None` = 未改动 → 还原旧值；明确改了引用则保留新值 |
| `is_plaintext_secret(data)` | 报出第一个「明文密钥」字段路径（用于拦截落盘） |
| `current_env()` / `env_override_report(env)` | 当前环境层 / 真正生效的环境变量覆盖（这些项**改文件无效**） |
| `validate(data, *, env=None)` | 插值 + 环境覆盖 + 校验 → `HarnessConfig`（复用 loader） |
| `ordered_config(data)` | 按 dataclass 字段序**递归重排**，dump 出的 YAML 键序稳定 |
| `dump_raw(data, path, *, backup=True)` | 备份 `.bak` → 写 `.tmp` → `os.replace` **原子替换**；文件头写 `MANAGED_HEADER` |
| `backup_path(path)` / `read_yaml(path)` | `.bak` 路径 / 读原始 YAML（不插值、不叠环境） |

`PUT /config` 的处理链：读旧 YAML → `restore_secrets` → `is_plaintext_secret`（明文 → 422）→
`validate`（非法 → 422，`details.path` 指向字段）→ `dump_raw`（写盘 + 备份）→ 由
[server/state.py](../server/state.py) 的 `AppState.apply_config()` **原地重建**运行时；失败回滚文件。
`POST /config/reload` 只重读磁盘、不写盘。

> **为何不从 `harness.config.__init__` 导出 admin**：`admin.py` 导入 `loader`，而 `__init__.py` 也导入
> `loader`，再导出 admin 会形成**循环导入**。因此路由直接 `from harness.config.admin import ...`。
>
> 设计详见 [docs/configuration-system-design.md](../../docs/configuration-system-design.md) §9。

## 测试

```bash
python -m harness.config.test.test_config   # 或 pytest harness/config/test
python -m harness.config.test.test_admin    # 配置管理（脱敏 / 回写）测试
```

57 条测试**全离线**（临时 YAML + 显式 env，不读真实环境变量与 `.env`），覆盖：
默认值 / 优先级 / 插值（含 `#` `:` 不被破坏）/ 缺失变量报错 / 未知字段与未知节报错 /
类型报错带路径 / 空值语义 / 工具开关（通配、disabled 优先、只读模式、表隔离）/
shell 策略覆盖与还原 / 沙箱（默认关闭、策略应用与还原、关闭时不改路径语义、域名只能追加）/
网络上限（默认值、应用与还原）/
技能停用名单（`disabled` 默认值、落盘保留）/ MCP 解析与映射
（`enabled` 默认值、运行期描述符不带 `enabled`、非布尔报错（path 正确）、`dump_raw` 后键序）/
JSONL sink 落盘 / `tracer_max_spans` /
Provider 与 tracer 的接线 / trace 读侧（按 trace 过滤、坏行跳过、缺失文件、内存与文件组树一致）/
长期记忆（开关、工具注册、system 注入、压缩器选择）/
**配置管理 admin（脱敏保留 `${VAR}` 与深拷贝、`restore_secrets` 还原、明文检测、键序重排、
环境变量覆盖报告、`dump_raw` 回写往返与 `.bak` 备份）** /
OTel sink（用 `InMemorySpanExporter` 断言父子与起止；未装 `[otel]` 时打印 SKIP 跳过）/
仓库自带 `harness.yaml` 可解析。
