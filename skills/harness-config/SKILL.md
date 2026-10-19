---
name: harness-config
description: 当用户要求新增/修改/删除 MCP server、更换模型或端点、调整循环与压缩参数、启停技能或禁用工具时使用；指导如何安全地编辑 harness.yaml，并在改完后提醒用户重载或重启服务让改动生效。
allowed-tools: [fs.read, fs.edit, fs.copy]
version: 1.0.0
---

# harness-config —— 改框架自身的配置

框架的所有运行期配置都在这一个文件里：**项目根目录的 `harness.yaml`**（9 段：`llm` / `agent` / `session` / `skills` / `memory` / `tools` / `mcp` / `observability` / `server`）。
你已经有 `fs.read` / `fs.edit` / `fs.copy`，所以**不需要任何专门工具**——按下面的流程改文件即可。

字段级的速查（每个字段的类型、默认值、生效方式、是否允许改）在**全局文档**里，需要时读它：

```
fs.read docs/configuration-reference.md
```

## 何时使用

- 用户要接一个新 MCP server（stdio 或 http），或改 / 删已有的 MCP server
- 用户要换模型 / 换端点（`llm.model`、`llm.base_url`）
- 用户要调循环参数（`agent.max_steps`、`temperature`…）或压缩参数（`session.*`）
- 用户要停用/启用某个技能（`skills.disabled`），或禁用某个工具（`tools.disabled`）
- 用户要开关长期记忆（`memory.enabled`）

## 何时不要用（这六类请让用户去控制台改）

| 段 | 为什么你不能改 |
|---|---|
| `llm.api_key` / `llm.extra_headers` | 密钥。只允许写 `${VAR}` 引用、真值在 `.env`；控制台的保存有明文密钥检查，**你直接改文件没有这道检查** |
| `tools.sandbox.*` | 这是你的工作区边界——你改了就等于拆自己的约束 |
| `tools.policy.*` | `enforce_scopes` / `approval_for_destructive`，同上 |
| `tools.shell.*` | 命令白名单/禁止模式，放宽即可跑任意命令 |
| `observability.*` | 审计 sink，改掉即抹掉痕迹 |
| `server.*` | host / port / data_dir / 超时与 TTL，属于运维决策 |

用户明确要求改这六类时：**说明原因并请他到控制台（或直接编辑该文件）处理**，不要动手。

## 前置检查（先做，再改）

1. **当前会话必须是「读写」档。** 只读档只有 `fs:read` / `net:read` 两个权限域，`fs.edit` / `fs.copy` 会被 `permission_denied` 直接拒。如果你发现改不动，先告诉用户把会话权限切成「读写 · 需批准」或「读写 · 免批准」，别反复重试。
2. **如果开了沙箱**（`tools.sandbox.enabled: true`），当前工作区必须包含 `harness.yaml`；否则 `fs.*` 会被 `sandbox_denied` 拦住。
3. **先通读**：`fs.read harness.yaml`，看清现有内容再动。

## 标准流程（6 步，不要跳）

1. `fs.read harness.yaml` —— 通读全文，确认要改的位置。
2. `fs.copy harness.yaml → harness.yaml.bak` —— 先备份（注意：目标文件已存在时 `fs.copy` 会报错，那就换个名字，别硬覆盖）。
3. `fs.edit` —— **只做局部替换**：`old_string` 必须在文件里唯一出现（不唯一就多带几行上下文）。**不要**用 `fs.write` 整文件重写：会丢注释、也极易写坏。
4. `fs.read` —— 复核改后的片段：缩进层级、`${VAR}` 引用、引号、逗号都要看一眼。
5. 字段名/嵌套层级不确定时，`fs.read docs/configuration-reference.md` 查速查表与可照抄的片段。
6. 按下一节**提醒用户怎么让改动生效**。

## 生效（重要：这件事你自己做不到）

`POST /config/reload` 有守卫：**有活跃 run 时返回 409 `config_reload_conflict`，而你现在这个 run 就是那个活跃 run** —— 所以你无法触发重载。改完必须**明确提醒用户**，并给出具体做法：

- 到控制台点「**重载**」按钮（等价于 `POST /config/reload`）；或
- **重启服务**：在项目根目录跑 `python main.py`（读 `harness.yaml` 的 `server.host/port`）
- **例外（更省事）**：如果只是**启停某一个 MCP server**，让用户点控制台 MCP 卡片上的开关——那条路是**热生效**的，不用重载也不用重启，比改文件划算。遇到这种诉求，先建议他这么做。
- 改 `server.host` / `server.port` / `server.data_dir` 时**只能重启**（重载不会重新 bind / 不会换存储）。

再告诉用户怎么**验证**：

| 改了什么 | 怎么验 |
|---|---|
| `llm.*` / `agent.model` | 控制台的 LLM 自检（`POST /llm/ping`）——**换模型/端点写错会让服务无法对话，务必让他先自检** |
| `mcp.servers` | `GET /mcp` 看该项 `connected`，以及工具是否出现在工具列表 |
| 其它 | `GET /config` 的 `effective`（运行期真正生效的值） |

## 出错了怎么回滚

用 `harness.yaml.bak` 覆盖回去（`fs.copy` 或 `fs.edit` 都行）。YAML 写坏导致服务起不来时，也是用它救。
如果连 `.bak` 都没有，就按用户的要求把改动**改回原样**——你改之前已经 `fs.read` 过全文，应该记得原内容。

## 三个必须避开的坑

1. **`${ENV}` 插值**：配置里可以写 `${OPENAI_API_KEY}` 这类引用，由 `.env` 与进程环境变量插值。**引用了不存在的变量会导致校验失败、服务起不来**——不要凭空引入新的 `${...}`。
2. **YAML 布尔陷阱**：`on` / `off` / `yes` / `no` / `y` / `n` / `true` / `false` 会被 YAML 当成布尔值。MCP server 的 `name` **不要**用这些词，否则会报"期望字符串，实际是 bool"。
3. **`mcp.servers[].name` 在没设 `tool_prefix` 时同时决定工具前缀**（`name: echo` → 工具 `echo.xxx`）。**改名等于换掉一整套工具名**，要改就同时确认 `tool_prefix`。

## 边界（要主动向用户说明）

- **护栏只靠约定，不是强制**：上面那六类字段是"约定不要改"，不是系统强制拦住的。要真正挡住，把 `harness.yaml` 与 `.env` 加进 `tools.sandbox.deny_paths`，**并且**把 `tools.sandbox.enabled` 设为 `true`（沙箱关闭时 `deny_paths` 根本不生效）。
- **改 MCP 只是改了"声明"**：真正的连接在用户重载/重启时才建立；在那之前 `GET /mcp` 里它还是"未连接"。
