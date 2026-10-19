# 配置参考（harness.yaml）

> 定位：**`harness.yaml` 的逐字段操作手册（每个字段是什么、怎么改、改了何时生效、哪些不许模型改）** · 受众：**人 + AI 模型**
> 相关：[配置系统设计文档](configuration-system-design.md) · [harness-config 技能设计文档](harness-config-skill-design.md) · [配置模块说明](../harness/config/README.md) · [项目 TODO](../todo.md)

---

## 1. 这份文档是什么

这是一份**全局配置参考 / 操作手册**，读者是**人 + AI 模型**两者：

- **人**：直接阅读，对照自己的机器找字段、抄配方。
- **模型**：用 `fs.read docs/configuration-reference.md` 读它，据此安全地编辑 `harness.yaml`。

它与 [配置系统设计文档](configuration-system-design.md) 的分工：

| 文档 | 回答的问题 |
|---|---|
| `configuration-system-design.md` | **为什么这么设计**：分层 / `${ENV}` 插值 / 校验与报错 / 装配（build）/ 写入与热重建 |
| **本文（configuration-reference.md）** | **每个字段是什么、怎么改、改了何时生效、哪些不许模型改** |

因此本文**不重复**插值规则、校验报错、装配细节、写入与热重建的机制说明——那些只在需要时给**指针**（"见 configuration-system-design.md §5/§6/§7/§9"）。

> 默认配置路径是**项目根 `harness.yaml`**，可用 `HARNESS_CONFIG` 指定别的路径；文件不存在时不报错（等价于全默认值）。密钥真值放 `.env`，配置文件里只写 `${VAR}` 引用。这两条的机制见 configuration-system-design.md §4/§5。

---

## 2. harness.yaml 最小骨架

下面是一份**可直接复制**的完整骨架，9 段齐全、每段带中文注释。字段顺序与本仓库现在真实在用的文件一致；这里**去掉了具体业务值**，写成通用版。

```yaml
# harness.yaml — 结构配置（可提交仓库；密钥只写引用，真值放 .env）
llm: {model: <模型名>, api_key: ${OPENAI_API_KEY}, base_url: <OpenAI 兼容端点>, timeout_s: 60, max_retries: 2}
agent: {max_steps: 12, max_total_tokens: 200000, stream: true, repeat_warn_at: 3, repeat_stop_at: 6, base_instructions: ''}
session: {compact_threshold: 40, keep_head: 0, keep_tail: 16, enable_compaction: true, auto_title: true, title_max_chars: 24}
skills: {root: skills, max_body_chars: 60000}
memory: {enabled: true, path: MEMORY.md, max_chars: 4000, auto_extract: false}
tools: {enabled: [], disabled: [], policy: {enforce_scopes: true, approval_for_destructive: true}, shell: {allowed_commands: [], deny_patterns: []}, sandbox: {enabled: false, root: ., write_paths: [], deny_paths: [], readonly_mounts: [], writable_mounts: [], denied_domains: []}, net: {max_bytes: 200000, max_download_bytes: 20000000}}
mcp: {servers: []}
observability: {tracer_sink: jsonl, tracer_path: data/traces.jsonl, tracer_max_spans: 500}
server: {host: 127.0.0.1, port: 8000, data_dir: data/sessions, heartbeat_s: 15, interaction_timeout_s: 300, run_ttl_s: 600, warmup_on_start: false, keepalive_s: 0}
```

上面这种「一段一行」的写法紧凑好读；控制台回写时会把键序规范化。若你更习惯缩进展开的多行写法，展开后与它完全等价（例如本仓库当前文件就是展开形式）。9 段各自的字段含义、默认值与可否修改见 §3、§5。

---

## 3. 字段速查

> 「生效方式」列的口径：`重载或重启` = 控制台点「重载」（`POST /config/reload`）或重启服务即可；`必须重启` = 重载不够，得重启进程。
> 「模型可改吗」列的口径见 §5，`✅` / `❌` 均指**是否列入**允许 / 禁止清单。

### 3.1 `llm` | `LLMConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `model` | str | `"gpt-4o-mini"` | 模型名 | 重载或重启 | ✅ |
| `api_key` | str \| None | `None` | 密钥；**只写 `${VAR}` 引用**，真值放 `.env` | 重载或重启 | ❌ |
| `base_url` | str \| None | `None` | OpenAI 兼容端点 | 重载或重启 | ✅ |
| `timeout_s` | float | `60.0` | 单次请求超时（秒） | 重载或重启 | ✅ |
| `max_retries` | int | `2` | 失败重试次数 | 重载或重启 | ✅ |
| `extra_headers` | dict | `{}` | 额外请求头（可塞 `Authorization`，故禁用） | 重载或重启 | ❌ |

### 3.2 `agent` | `LoopConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `max_steps` | int | `12` | 单轮最多循环步数 | 重载或重启 | ✅ |
| `max_total_tokens` | int \| None | `None` | 累计 token 预算；`None` = 不限 | 重载或重启 | ✅ |
| `temperature` | float \| None | `None` | 采样温度 | 重载或重启 | ✅ |
| `max_tokens` | int \| None | `None` | 单次生成上限 | 重载或重启 | ✅ |
| `tool_choice` | str \| None | `"auto"` | 工具选择策略 | 重载或重启 | ✅ |
| `model` | str \| None | `None` | 覆盖 Provider 默认模型 | 重载或重启 | ✅ |
| `stream` | bool | `False` | 是否逐增量流式输出 | 重载或重启 | ✅ |
| `repeat_warn_at` | int | `3` | 同一工具+同参数累计到就提醒模型；`0` 关闭 | 重载或重启 | ✅ |
| `repeat_stop_at` | int | `6` | 累计到就终止本轮；`0` 关闭 | 重载或重启 | ✅ |
| `base_instructions` | str | `""` | 拼进 system prompt 最前面 | 重载或重启 | ✅ |

### 3.3 `session` | `SessionConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `compact_threshold` | int | `40` | 固定头 + 活跃区超过这么多条触发压缩 | 重载或重启 | ✅ |
| `keep_head` | int | `0` | 首次压缩时把活跃区开头这么多条固化进固定头；`0` = 不保护 | 重载或重启 | ✅ |
| `keep_tail` | int | `16` | 压缩后至少保留最近这么多条 | 重载或重启 | ✅ |
| `max_active_tokens` | int \| None | `None` | 可选：按估算 token 触发压缩 | 重载或重启 | ✅ |
| `enable_compaction` | bool | `True` | 是否启用压缩 | 重载或重启 | ✅ |
| `auto_title` | bool | `True` | 首轮问答结束后用 LLM 总结标题 | 重载或重启 | ✅ |
| `title_max_chars` | int | `24` | 生成标题的字数上限 | 重载或重启 | ✅ |

### 3.4 `skills` | `SkillsConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `root` | str | `"skills"` | 技能目录；**空字符串 = 关闭技能功能** | 重载或重启 | ❌ |
| `disabled` | list[str] | `[]` | 停用名单：目录仍在、控制台可见，但不进 system prompt 的技能目录、不可 `skill.load` / `skill.read` | 重载或重启 | ✅ |
| `max_desc_chars` | int | `300` | 技能描述字数上限 | 重载或重启 | ❌ |
| `max_body_chars` | int | `60000` | 技能正文（`SKILL.md`）字数上限 | 重载或重启 | ❌ |
| `max_resource_chars` | int | `100000` | 技能资源文件字数上限 | 重载或重启 | ❌ |

### 3.5 `memory` | `MemoryConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `enabled` | bool | `True` | 是否启用长期记忆 | 重载或重启 | ✅ |
| `path` | str | `"MEMORY.md"` | 记忆文件（相对项目根）；不存在视为空记忆 | 重载或重启 | ✅ |
| `max_chars` | int | `4000` | 记忆字符上限（超限拒绝写入） | 重载或重启 | ✅ |
| `auto_extract` | bool | `False` | 压缩时抽到的候选记忆是否直接写入 | 重载或重启 | ✅ |

### 3.6 `tools` | `ToolsConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `enabled` | list[str] | `[]` | 工具白名单；**留空 = 全开**，支持 fnmatch 通配 | 重载或重启 | ✅（仅追加） |
| `disabled` | list[str] | `[]` | 工具黑名单；**优先级高于 `enabled`** | 重载或重启 | ✅（仅追加） |
| `policy.enforce_scopes` | bool | `True` | 是否强制权限域（scopes）检查 | 重载或重启 | ❌ |
| `policy.approval_for_destructive` | bool | `True` | 破坏性工具是否需要审批 | 重载或重启 | ❌ |
| `shell.allowed_commands` | list[str] | `[]` | `shell.run` 命令白名单；**留空 = 用内置默认白名单** | 重载或重启 | ❌ |
| `shell.deny_patterns` | list[str] | `[]` | `shell.run` 禁止的正则模式 | 重载或重启 | ❌ |
| `sandbox.enabled` | bool | `False` | **opt-in**；关闭时连 `deny_paths` 都不生效 | 重载或重启 | ❌ |
| `sandbox.root` | str | `"."` | 工作区根（相对启动目录） | 重载或重启 | ❌ |
| `sandbox.write_paths` | list[str] | `[]` | root 内写白名单 glob；空 = 全可写 | 重载或重启 | ❌ |
| `sandbox.deny_paths` | list[str] | `[]` | **最高优先级**黑名单 glob（读写都拒） | 重载或重启 | ❌ |
| `sandbox.readonly_mounts` | list[str] | `[]` | root 之外的额外只读目录 | 重载或重启 | ❌ |
| `sandbox.writable_mounts` | list[str] | `[]` | root 之外的额外可写目录 | 重载或重启 | ❌ |
| `sandbox.denied_domains` | list[str] | `[]` | **追加**到内置 SSRF 基线黑名单 | 重载或重启 | ❌ |
| `net.max_bytes` | int | `200000` | `net.fetch` 单次读取上限 | 重载或重启 | ✅ |
| `net.max_download_bytes` | int | `20000000` | `net.download` 单文件上限 | 重载或重启 | ✅ |

### 3.7 `mcp.servers[]` | `McpServerConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `name` | str | **必填** | 同时决定工具前缀（未设 `tool_prefix` 时） | 重载或重启 | ✅ |
| `enabled` | bool | `True` | `false` → 声明保留、不连接、不注册工具 | **热生效**（见下） | ✅（建议走控制台） |
| `transport` | str | `"stdio"` | `stdio` 或 `http` | 重载或重启 | ✅ |
| `command` | str \| None | `None` | stdio 必填：可执行文件 | 重载或重启 | ✅ |
| `args` | list[str] | `[]` | 命令参数 | 重载或重启 | ✅ |
| `env` | dict[str,str] | `{}` | 子进程环境；**值不算密钥、会明文落盘**，敏感值写 `${VAR}` | 重载或重启 | ✅ |
| `cwd` | str \| None | `None` | 子进程工作目录 | 重载或重启 | ✅ |
| `url` | str \| None | `None` | http 必填：服务地址 | 重载或重启 | ✅ |
| `tool_prefix` | str \| None | `None` | 工具名前缀；默认取 `name` | 重载或重启 | ✅ |
| `default_side_effect` | str | `"read"` | 默认副作用等级 | 重载或重启 | ✅ |
| `default_requires_approval` | bool | `False` | 默认是否需审批 | 重载或重启 | ✅ |
| `default_scopes` | list[str] | `[]` | 默认权限域 | 重载或重启 | ✅ |
| `default_timeout_s` | float | `60.0` | 默认调用超时 | 重载或重启 | ✅ |
| `tool_overrides` | dict | `{}` | 按工具覆盖，优先级高于 server 级默认（可覆盖 `side_effect` / `requires_approval` / `scopes` / `timeout_s` 等） | 重载或重启 | ✅ |

> **只启停单个 server** 时，改 `enabled` 还要重载；而控制台卡片上的开关走 `PATCH /mcp/{name}`，是**热生效**的——不重载、不重启。所以只是启停的话，**让用户点控制台开关更划算**。

### 3.8 `observability` | `ObservabilityConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `tracer_sink` | str | `"none"` | `none` \| `jsonl` \| `otel` | 重载或重启 | ❌ |
| `tracer_path` | str | `"data/traces.jsonl"` | 仅 `jsonl` sink 使用 | 重载或重启 | ❌ |
| `tracer_max_spans` | int | `500` | 内存中保留的 span 上限 | 重载或重启 | ❌ |
| `otel_endpoint` | str \| None | `None` | 仅 `otel` sink 使用 | 重载或重启 | ❌ |
| `otel_service_name` | str | `"harness"` | 仅 `otel` sink 使用 | 重载或重启 | ❌ |

### 3.9 `server` | `ServerConfig`

| 字段 | 类型 | 默认值 | 说明 | 生效方式 | 模型可改吗 |
|---|---|---|---|---|---|
| `host` | str | `"127.0.0.1"` | 监听地址 | **必须重启**（重载不会重新 bind） | ❌ |
| `port` | int | `8000` | 监听端口 | **必须重启**（重载不会重新 bind） | ❌ |
| `data_dir` | str | `"data/sessions"` | 会话存储目录 | **必须重启**（建 app 时装配） | ❌ |
| `heartbeat_s` | float | `15.0` | 心跳间隔 | 重载即可 | ❌ |
| `interaction_timeout_s` | float | `300.0` | 交互（审批/提问）超时 | 重载即可 | ❌ |
| `run_ttl_s` | float | `600.0` | run 记录保留时长 | 重载即可 | ❌ |
| `warmup_on_start` | bool | `false` | 启动后预热一次模型连接（省 TLS/连接池建立）。**仅对支持前缀缓存的 provider 有意义** | 重载即可 | ❌ |
| `keepalive_s` | float | `0.0` | 空闲保活间隔；`>0` 时每隔该时长、且无活跃 run 时打一次 mini ping。`0`=关闭 | 重载即可 | ❌ |

---

## 4. 改配置的标准流程

模型（或人）改 `harness.yaml` 时走这 6 步：

1. **确认当前会话是「读写」档**：`fs.edit` / `fs.write` 需要 `fs:write` 权限域，只读档会被权限域直接拒绝（`permission_denied`）。若不是读写档，**先让用户切档**再动手。
2. **开了沙箱时，确认工作区覆盖 `harness.yaml`**：`tools.sandbox.enabled: true` 时，相对路径以 `sandbox.root` 为基准，且 `deny_paths` 命中即拒。要能改到 `harness.yaml`，它得在工作区内、且不在 `deny_paths` 里。
3. **`fs.read` 通读全文**：确认现状与注释，再决定改哪里。
4. **`fs.copy` 备份为 `harness.yaml.bak`**：改坏了靠它救（见 §8）。注意 `fs.copy` 目标已存在会直接报错，不静默覆盖。
5. **`fs.edit` 局部替换**：`old_string` 必须在文件里**唯一**；**不要**用 `fs.write` 整文件重写——会丢注释，也容易写坏缩进/结构。
6. **再 `fs.read` 复核**，然后**提醒用户重启服务或到控制台点「重载」**：模型自己没能力让改动生效（见 §7.4）。

---

## 5. 允许 / 禁止修改的清单

### 5.1 允许模型改（12 项）

| # | 能力 | 目标字段 |
|---|---|---|
| 1 | 新增 MCP server（stdio） | `mcp.servers[]`：`name` / `transport: stdio` / `command` / `args` / `env` / `cwd` / `tool_prefix` |
| 2 | 新增 MCP server（http） | 同上，`transport: http` + `url` |
| 3 | 修改 MCP server | 任意字段，含 `default_side_effect` / `default_scopes` / `default_requires_approval` / `default_timeout_s` / `tool_overrides` |
| 4 | 删除 MCP server | 从 `mcp.servers` 移除整条 |
| 5 | MCP 启停 | `mcp.servers[].enabled`（**建议改走控制台开关，热生效**） |
| 6 | 技能启停 | `skills.disabled: [name, ...]` |
| 7 | 工具开关（**只允许"加"**） | 只允许往 `tools.disabled` / `tools.enabled` **追加**，不允许删条目 |
| 8 | 换模型 / 端点 | `llm.model` / `llm.base_url` / `llm.timeout_s` / `llm.max_retries`（**不含 `api_key` 与 `extra_headers`**） |
| 9 | 循环参数 | `agent.*` 全部字段 |
| 10 | 会话 / 压缩参数 | `session.*` 全部字段 |
| 11 | 长期记忆 | `memory.*` 全部字段 |
| 12 | 网络体积上限 | `tools.net.max_bytes` / `tools.net.max_download_bytes`（只是体积上限，不是安全边界；域名黑名单 `sandbox.denied_domains` 仍属禁止项） |

> **第 7 项的"只加不减"**：`tools.disabled` 是人类设的约束，模型可以往里**加**（禁用工具）、不能**减**（解禁）；`tools.enabled` 同理只允许追加。这是**约定**，不是强制。

### 5.2 禁止模型改（6 类）

| 段 | 为什么禁止 |
|---|---|
| `llm.api_key` / `llm.extra_headers` | **密钥**。只允许写 `${VAR}` 引用、真值放 `.env`；控制台的 `PUT /config` 有明文密钥检查，而**直接改文件没有这道检查**；`extra_headers` 可以塞 `Authorization` |
| `tools.sandbox.*` | 工作区边界就是模型自己的约束，改了等于**自拆护栏** |
| `tools.policy.*` | `enforce_scopes` / `approval_for_destructive`，同上 |
| `tools.shell.*` | 命令白名单 / 禁止模式，放宽即可**跑任意命令** |
| `observability.*` | 审计 sink，改掉即**抹掉痕迹** |
| `server.*` | `host` / `port` / `data_dir` / `heartbeat_s` / `interaction_timeout_s` / `run_ttl_s` / `warmup_on_start` / `keepalive_s` |

> **护栏只靠约定，不是强制**：模型（或被提示注入的模型）仍可能去改这六类字段。要真正挡住见 §7.4。

---

## 6. 常见任务配方

### 6.1 加一个 stdio MCP server

```yaml
mcp:
  servers:
    - name: filesystem
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      tool_prefix: mcpfs
```

改完**重载或重启**生效；随后在控制台用 `GET /mcp` 看它 `connected` 是否变 `true`。

### 6.2 加一个 http MCP server

```yaml
mcp:
  servers:
    - name: remote-docs
      transport: http
      url: https://example.com/mcp
```

改完**重载或重启**生效；`GET /mcp` 确认 `connected`。

### 6.3 关掉一个 MCP server

```yaml
mcp:
  servers:
    - name: remote-docs
      transport: http
      url: https://example.com/mcp
      enabled: false        # 声明保留，但不连接、不注册工具
```

> **只是启停的话，让用户点控制台卡片上的开关更划算**——那条路走 `PATCH /mcp/{name}`，是**热生效**的（不用重载也不用重启）；改文件反而要重载。

### 6.4 给某个 MCP 工具加审批与 `side_effect`

```yaml
mcp:
  servers:
    - name: filesystem
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      default_side_effect: read
      default_requires_approval: false
      tool_overrides:
        write_file:
          side_effect: destructive
          requires_approval: true
```

`tool_overrides` 按工具覆盖，优先级高于 server 级默认。改完**重载或重启**生效。

### 6.5 换模型 / 端点

```yaml
llm: {model: <新模型名>, base_url: <新端点>, timeout_s: 60, max_retries: 2}
```

> `api_key` 只写 `${VAR}` 引用，真值在 `.env`。改完**重载或重启**生效；换模型/端点后**先让用户用控制台自检**（`POST /llm/ping`）再正式用——写错会让服务无法对话。

### 6.6 调 `max_steps`

```yaml
agent: {max_steps: 20}
```

改完**重载或重启**生效。

### 6.7 停用一个技能

```yaml
skills:
  disabled: [some-skill]     # 目录仍在、控制台可见，但不进技能目录、不可 load/read
```

改完**重载或重启**生效（或让用户在控制台点该技能的开关，走 `PATCH /skills/{name}`）。

### 6.8 禁用一个工具

```yaml
tools:
  disabled: [fs.delete, net.*]   # 支持 fnmatch 通配
```

改完**重载或重启**生效。注意**只允许追加**，不能删条目。

---

## 7. 生效与验证

### 7.1 三种生效方式

| 方式 | 怎么做 | 适用 |
|---|---|---|
| **控制台「重载」** | 点控制台「重载」按钮 = `POST /config/reload`（从磁盘重读、重建运行时，**不写盘**） | 改 `harness.yaml` 后最常用 |
| **重启服务** | 停掉旧进程后 `python main.py`（读配置的 `server.host/port`，也可 `--host/--port` 覆盖） | 改 `server.host` / `server.port`（重载**不会**重新 bind）、`server.data_dir`（建 app 时装配）时必须用 |
| **控制台开关** | 逐 server / 逐技能开关（`PATCH /mcp/{name}` / `PATCH /skills/{name}`） | 只是启停某一项，**热生效**，不需重载 |

> 口径回顾：`llm.*` / `agent.*` / `session.*` / `skills.*` / `memory.*` / `tools.*` / `mcp.*` / `observability.*` 一律**重载或重启**；`server.heartbeat_s` / `server.interaction_timeout_s` / `server.run_ttl_s` / `server.warmup_on_start` / `server.keepalive_s` **重载即可**。

### 7.2 验证手段

| 验证项 | 怎么做 |
|---|---|
| 模型连通性 | 控制台 LLM 自检 = `POST /llm/ping` |
| MCP 连接状态 | `GET /mcp`，看各 server 的 `connected` |
| 运行期生效值 | `GET /config`，看 `effective` 分列（确认改动的确是运行期在用的值） |

### 7.3 生效机制细节

重建范围、是否要求无活跃 run、写盘链路等机制细节，见 configuration-system-design.md §9（写入与热重建）。

### 7.4 两个必须知道的边界

- **模型自己没能力触发重载**：`POST /config/reload` 有守卫 `state.runs.active_count() > 0 → 409 config_reload_conflict`，而**模型自己的 run 就是那个活跃 run**——所以模型改完必须**提醒用户**去控制台点「重载」或重启服务。它手上也没有任何工具能触发重载。
- **护栏只靠约定，不是强制**：模型（或被提示注入的模型）仍可能去改 §5.2 那六类字段。要真正挡住：把 `harness.yaml` 与 `.env` 加进 `tools.sandbox.deny_paths`，**并且**把 `tools.sandbox.enabled` 设为 `true`（沙箱关闭时 `deny_paths` 不生效）。

---

## 8. 回滚

- 正常回滚：拿 `harness.yaml.bak` 覆盖回 `harness.yaml` 即可（备份在 §4 第 4 步用 `fs.copy` 生成）。
- 救急：**YAML 写坏导致服务起不来**时，也用 `harness.yaml.bak` 覆盖回去再重启。
- 控制台路径：数据加工层在写盘前会自动备份 `<file>.bak`（机制见 configuration-system-design.md §9.1）。

---

## 9. 三个易错点

1. **`${ENV}` 插值**：配置里可以写 `${OPENAI_API_KEY}` 这类引用，由 `.env` + 进程环境变量插值；**引用了未设置的变量会导致校验失败、服务起不来**（机制见 configuration-system-design.md §5）。
2. **YAML 布尔陷阱**：`on` / `off` / `yes` / `no` / `y` / `n` / `true` / `false` 会被 YAML 解析成布尔值 —— 所以 MCP server 的 `name` **不要**用这些词，否则校验会报「期望字符串，实际是 bool」（真机踩过）。
3. **`mcp.servers[].name` 在未设 `tool_prefix` 时同时决定工具前缀**（如 `name: echo` → 工具名 `echo.xxx`）：**改名会改变模型看到的工具名**，等于换了一套工具。

---

## 10. 延伸阅读

- **插值规则 / 校验与报错 / 装配（build）/ 写入与热重建** → [配置系统设计文档](configuration-system-design.md) §5 / §6 / §7 / §9
- **配置模块实现与目录结构** → [配置模块说明](../harness/config/README.md)
- **「让模型自己改配置」的能力清单与承载技能** → [harness-config 技能设计文档](harness-config-skill-design.md)
- **服务端配置读写 / 热重建路由** → [API 服务层设计文档](api-service-design.md)
- **工作区边界与路径权限** → [沙箱设计文档](sandbox-design.md)
- **`server.*` 与运行期部件** → [FastAPI 服务层设计文档](api-service-design.md)
- **项目进度** → [项目 TODO](../todo.md)
