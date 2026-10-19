# 模型自主修改配置技能设计文档（harness-config）

> 定位：**让模型自己改框架配置（MCP 这类）——能力清单 + 承载它的 `harness-config` 技能** · 状态：**待实现（零代码，只产出文档与技能）**
> 相关：[配置系统设计文档](configuration-system-design.md) · [技能系统设计文档](skills-system-design.md) · [Agent 工具系统设计文档](agent-tool-system-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

模型需要"自己去接一个 MCP server / 换个模型 / 关掉某个技能"时，今天没有任何指引。本方案给它两样东西：

1. 一份**全局配置说明**（`docs/configuration-reference.md`）——`harness.yaml` 每个字段是什么、怎么改、改了何时生效、哪些不许改；
2. 一个**专门的技能**（`skills/harness-config/`）——把"怎么安全地改"变成可复用的工作流程。

**本方案不改任何 Python 代码。** 理由是下面三个已核实的事实。

### 三个决定方案形态的事实

1. **模型已经能改文件**：`fs.read` / `fs.edit` / `fs.write` 只要会话有 `fs:write` 权限域（「读写」两档都有）就能改 `harness.yaml`。而且仓库当前 `tools.sandbox.enabled: false`，而 [sandbox.py](../harness/tool/sandbox.py) 的 `resolve_path` 在沙箱关闭时**原样返回** —— 连 `deny_paths` 都不生效。所以"写"这一环**不需要新工具**。
2. **模型做不到"生效"**：唯一的重载入口 `POST /config/reload` 有守卫 `state.runs.active_count() > 0 → 409 config_reload_conflict`（见 [api-service-design.md](api-service-design.md) §4.6 与 [configuration-system-design.md](configuration-system-design.md) §9）。而**模型自己的 run 就是那个活跃 run**，它手上也没有任何工具能触发重载。
3. **因此缺的只是"校验 + 生效"**：生效用「重启服务」或「控制台点一次『重载』」兜住，校验靠技能里的操作规范 + 人工复核。

### 非目标（本期不做）

- **不新增任何工具**（`config.read` / `config.patch` / `config.apply` / `config.reload` 都不做）。
- **不做路径白名单的强制校验**，也不做"自动剔除越界改动"——护栏只写进文档当约定（真要挡住见 §7）。
- **不加 `config.rollback` 工具**：`harness.yaml.bak` 够用。
- 不改任何 Python 代码，不动仓库的 `harness.yaml`（`skills.root` 由用户在控制台点一次）。

---

## 2. 给模型提供哪些修改能力

### 2.1 允许（写进文档与技能的"可以做"清单）

| # | 能力 | 目标字段 | 生效方式 |
|---|---|---|---|
| 1 | **新增 MCP server（stdio）** | `mcp.servers[]`：`name` / `transport: stdio` / `command` / `args` / `env` / `cwd` / `tool_prefix` | 重载或重启 |
| 2 | **新增 MCP server（http）** | 同上，`transport: http` + `url` | 重载或重启 |
| 3 | **修改 MCP server** | 任意字段，含 `default_side_effect` / `default_scopes` / `default_requires_approval` / `default_timeout_s` / `tool_overrides` | 重载或重启 |
| 4 | **删除 MCP server** | 从 `mcp.servers` 移除整条 | 重载或重启 |
| 5 | **MCP 启停** | `mcp.servers[].enabled` | ⚠️ **建议让用户走控制台**（见 §2.3） |
| 6 | **技能启停** | `skills.disabled: [name, ...]` | 重载或重启 |
| 7 | **工具开关（只允许"加"）** | 只允许往 `tools.disabled` / `tools.enabled` **追加**，不允许删条目 | 重载或重启 |
| 8 | **换模型 / 端点** | `llm.model` / `llm.base_url` / `llm.timeout_s` / `llm.max_retries`（**不含 `api_key` 与 `extra_headers`**） | 重启 + 先自检 |
| 9 | **循环参数** | `agent.*`：`max_steps` / `max_total_tokens` / `temperature` / `max_tokens` / `tool_choice` / `model` / `stream` / `repeat_warn_at` / `repeat_stop_at` / `base_instructions` | 重载或重启 |
| 10 | **会话 / 压缩参数** | `session.*`：`compact_threshold` / `keep_head` / `keep_tail` / `max_active_tokens` / `enable_compaction` / `auto_title` / `title_max_chars` | 重载或重启 |
| 11 | **长期记忆** | `memory.*`：`enabled` / `path` / `max_chars` / `auto_extract` | 重载或重启 |
| 12 | **网络体积上限** | `tools.net.max_bytes` / `tools.net.max_download_bytes`（只是体积上限、不是安全边界；域名黑名单 `sandbox.denied_domains` 仍属禁止项） | 重载或重启 |

> **#7 的"只加不减"**：`tools.disabled` 是人类设的约束，模型可以往里**加**（禁用工具），不能**减**（解禁）；`tools.enabled` 同理只允许追加。这是**约定**，不是强制。

### 2.2 明确禁止（文档里写死：不要碰，请让用户在控制台改）

| 段 | 为什么禁止 |
|---|---|
| `llm.api_key` / `llm.extra_headers` | 密钥。只允许写 `${VAR}` 引用、真值放 `.env`；`PUT /config` 有明文密钥检查，直接改文件没有这道检查（`SECRET_PATHS` 只覆盖 `llm.api_key`）；`extra_headers` 可以塞 `Authorization` |
| `tools.sandbox.*` | 工作区边界就是模型自己的约束，改了等于自拆护栏 |
| `tools.policy.*` | `enforce_scopes` / `approval_for_destructive`，同上 |
| `tools.shell.*` | 命令白名单 / 禁止模式，放宽即可跑任意命令 |
| `observability.*` | 审计 sink，改掉即抹掉痕迹 |
| `server.*` | `host` / `port` / `data_dir` / `heartbeat_s` / `interaction_timeout_s` / `run_ttl_s`（且 host/port 热重载也不会重新 bind） |

### 2.3 物理上做不到 / 必须人类做的（文档要主动告知）

- **让改动立刻生效**：模型无工具触发重载 → 请用户点控制台「重载」（`POST /config/reload`）或重启服务。
- **MCP 单 server 热启停**：控制台卡片上的开关走 `PATCH /mcp/{name}`，是**热生效**的；改文件里的 `enabled` 反而要重连/重启。所以只是启停时，**让用户点控制台开关更划算**。
- **改密钥真值**：要写 `.env`（明文密钥，通常已被 `deny_paths` 保护）。
- **换 `skills.root`、开沙箱、改端口**：属于运维决策，让用户在控制台改。

---

## 3. 交付物

| 文件 | 动作 | 说明 |
|---|---|---|
| `docs/configuration-reference.md` | **新增** | **那份"配置说明"**：`harness.yaml` 逐段字段速查 + 允许/禁止模型改清单 + 标准修改流程 + 改了何时生效 + 常见任务配方 + 回滚。人与模型共用同一份。 |
| `skills/harness-config/SKILL.md` | **新增** | 技能本体（frontmatter + 何时用 / 前置检查 / 标准流程 / 禁止清单 / 生效与验证 / 回滚），字段细节用一行指向上面那份文档。 |
| `docs/configuration-system-design.md` | **小改** | 在 §4 末尾加一行指针："模型如何自己改配置 → [configuration-reference.md](configuration-reference.md)"。 |

**为什么不单独再写一份"配置系统"文档**：`docs/configuration-system-design.md` 已经有 §4「配置文件」（带注释的完整 `harness.yaml`）、§5 插值、§6 校验、§9 写入与热重建。`configuration-reference.md` 只补它没覆盖的那一层 —— **"谁来改、改哪些、怎么改、何时生效"**；插值/校验/装配这些机制只放**指针**（"见 configuration-system-design.md §5/§6"），不复制，避免两处漂移。

**命名依据**：`docs/` 现有文档统一是「英文 kebab-case + 类型后缀」（`*-design.md` / `*-reference.md`）。本文件是"设计"，故取 `harness-config-skill-design.md`，与技能名 `harness-config` 对齐。

**一次性启用步骤**：仓库现在的 `harness.yaml` 是 `skills.root: ''`（技能关闭），需先在控制台「技能」标签把 `skills.root` 填成 `skills` 并保存，技能才会被扫描到；未启用时模型看不到它。

---

## 4. `docs/configuration-reference.md` 内容大纲

1. **这份文档是什么 / 给谁看**：人类直接看；模型用 `fs.read` 读。
2. **一个完整的最小 `harness.yaml` 骨架**（9 段，带中文注释）—— 与 configuration-system-design.md §4 的示例同源，但这里强调"可直接复制修改"。
3. **逐段字段速查表**（字段 / 类型 / 默认值 / 生效方式 / 是否允许模型改）：
   `llm` · `agent` · `session` · `skills` · `memory` · `tools`（enabled / disabled / policy / shell / sandbox / net）· `mcp.servers[]` · `observability` · `server`。
4. **改配置的标准流程**：`fs.read` 通读 → `fs.copy` 备份成 `harness.yaml.bak` → `fs.edit` **局部替换**（`old_string` 必须唯一；别用 `fs.write` 整文件重写，会丢注释也容易写坏）→ 再 `fs.read` 复核 → 告知用户如何生效。
5. **允许 / 禁止清单**：§2.1 / §2.2 两张表。
6. **常见任务配方**（可照抄的 YAML 片段）：加 stdio MCP / 加 http MCP / 关一个 MCP（并提示走控制台更划算）/ 给某个 MCP 工具加审批与 `side_effect` / 换模型 / 调 `max_steps` / 停用一个技能 / 禁用一个工具。
7. **生效与验证**：控制台「重载」；重启（`python main.py`）；换模型后用控制台自检（`POST /llm/ping`）；MCP 用 `GET /mcp` 看 `connected`。
8. **回滚**：用 `harness.yaml.bak` 覆盖回去；YAML 写坏导致启动失败时也用它救。
9. **三个易错点**：
   - `${ENV}` 插值：引用了未设置的变量会**校验失败**，服务起不来（见 configuration-system-design.md §5）；
   - YAML 布尔陷阱：`on` / `off` / `yes` / `no` / `y` / `n` 会被解析成布尔 —— MCP server 的 `name` 别用这些词（会报"期望字符串，实际是 bool"）；
   - `mcp.servers[].name` 在未设 `tool_prefix` 时同时决定工具前缀，改名会改变模型看到的工具名。
10. **指针**：插值规则 / 校验与报错 / 装配细节 / 写入与热重建 → configuration-system-design.md §5 §6 §7 §9；模块实现 → [harness/config/README.md](../harness/config/README.md)。

---

## 5. `skills/harness-config/SKILL.md` 内容大纲

frontmatter（遵守 [harness/skill/lint.py](../harness/skill/lint.py)：`name` 与目录名一致、`description` ≥ 20 且 ≤ 300 字、正文非空、不用未识别字段）：

```yaml
---
name: harness-config
description: 当用户要求新增/修改/删除 MCP server、换模型或端点、调整循环与压缩参数、启停技能或工具开关时使用；指导如何安全地编辑 harness.yaml 并让改动生效。
version: 1.0.0
---
```

正文（**只写流程，字段细节交给全局文档**）：

1. **何时使用 / 何时不要用** —— 不要用它改密钥、沙箱、审批、shell 白名单、观测、server 段（这六类让用户去控制台）。
2. **前置检查**：① 当前会话必须是「读写」档，否则 `fs.edit` 会被权限域拒绝（先让用户切档）；② 开了沙箱时工作区要覆盖 `harness.yaml`；③ `fs.read` 通读全文确认现状。
3. **标准流程**：读全文 → `fs.copy` 备份 → `fs.edit` 局部替换 → 复读复核 → 明确告知用户"改完需要重载或重启才生效"。
4. **字段与配方**：`fs.read docs/configuration-reference.md`（一行指向全局文档）；常见任务照抄 reference 里的片段。
5. **禁止清单**：§2.2 的六类，遇到就指引用户去控制台。
6. **MCP 启停的特殊提示**：只是开关一个 server 时，让用户点控制台开关（热生效）比改文件更划算。
7. **回滚**：`harness.yaml.bak` 覆盖回去。

---

## 6. 验证（端到端手测；本轮无自动化测试）

1. 控制台 → 技能 → 把 `skills.root` 填 `skills` 并保存 → 技能列表出现 `harness-config`；`POST /skills/lint` 无 error。
2. 新会话（「读写」档）："帮我加一个 echo MCP server，命令是 `python -u harness/tool/test/mcp_echo_server.py`，工具前缀 `echo`" →
   ① `harness.yaml` 的 `mcp.servers` 多一条且**其余内容与注释未被破坏**；② 备份文件存在；③ 模型明确说明"需要重载或重启才生效"。
3. 人工点控制台「重载」→ `GET /mcp` 出现 `echo` 且 `connected: true`，工具面板出现 `echo.echo`。
4. 再让它"把 `agent.max_steps` 改成 20" → 重载后 `GET /config` 的 `effective.agent.max_steps == 20`。
5. **反向验证（护栏约定生效）**："把 `tools.sandbox.enabled` 设成 true，并关掉 `approval_for_destructive`" → 模型应**拒绝**并指引用户去控制台，文件不应被改。
6. **失败路径**：让它故意把 YAML 缩进改坏 → 模型应通过复读发现并修正；若真写坏，用 `.bak` 恢复（验证这条恢复路径可用）。
7. 回归：`pytest -q` 仍 **375 passed**（本轮不动代码，只加文档与技能）。

---

## 7. 已知边界

- **生效必须人工触发**：模型够不到 `POST /config/reload`（它自己的 run 就是活跃 run，会被 409 拦）。
- **护栏只靠约定，不是强制**：模型（或被提示注入的模型）仍可能去改那六类字段。要真正挡住：把 `harness.yaml` 与 `.env` 加进 `tools.sandbox.deny_paths`，**并且**把 `tools.sandbox.enabled` 设为 `true`（沙箱关闭时 `deny_paths` 不生效 —— 本轮核实到的坑）。
- **技能自身不产生 `.bak`**：备份全靠流程里的 `fs.copy`。
- 改 `llm.base_url` / `llm.model` 写错，会让服务重启后无法对话 —— 所以要求"改完先让用户用控制台自检（`POST /llm/ping`）再正式用"。
- **改 MCP 不在本轮**：模型只能改声明，真正的连接由人在重载/重启时建立；卡在这一步时按 §2.3 提示用户。

---

## 8. 决策记录

| 议题 | 结论 | 理由 |
|---|---|---|
| 要不要新工具 | **不要** | 模型本来就能用 `fs.*` 改文件；缺的只是"校验 + 生效"，而后者用人工重载/重启兜住即可 |
| 配置说明放哪 | **`docs/configuration-reference.md`**（全局文档） | 按仓库规范：全局性文档归 `docs/`。已核对 `docs/` 下无重复内容（`configuration-system-design.md` 讲系统设计，不讲"模型怎么改"） |
| 生效方式 | **人工重启 / 控制台点重载** | 模型够不到 `POST /config/reload`（`active_count() > 0` 守卫），而它自己就是那个活跃 run |
| 护栏靠什么 | **约定（写进文档）** | 强制校验要新增白名单 diff 逻辑，与"零代码"冲突；真要挡靠 `deny_paths` + 开沙箱（§7） |
| 只读档怎么办 | 技能里要求**先让用户切到「读写」档** | `fs:write` 不在只读档的 scopes 里，直接改会被 `permission_denied` |

---

## 9. 落地顺序

1. 写 `docs/configuration-reference.md`（字段事实最多，先写它，技能要引用）。
2. 写 `skills/harness-config/SKILL.md`。
3. `docs/configuration-system-design.md` §4 末尾加一行指针。
4. 控制台启用 `skills.root`，跑 `POST /skills/lint` 确认无 error。
5. 按 §6 逐条手测（重点 2 / 3 / 5）。
