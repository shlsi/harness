# 前端控制台设计文档

> 定位：**harness 的单一管理界面**（对话 + 用量 + 配置 + 运维） · 技术栈：**原生 HTML/CSS/JS 单文件** · 状态：**已实现**
> 相关：[API 服务层设计](api-service-design.md) · [会话管理设计](session-management-design.md) · [配置系统设计](configuration-system-design.md) · [Skills 系统设计](skills-system-design.md)

---

## 1. 定位与非目标

### 定位

把框架能力收敛到一个**同源、零构建、离线可用**的控制台里：

- **对话**：多会话、历史回读、SSE 打字机、工具调用与结果、审批/提问、取消、图片上传、**软打断**（运行中「打断并发送」，当前工具跑完即收尾并续发）
- **会话工作区与权限**：按会话选 agent 的工作区目录（服务端目录点选）+ 三档权限（只读 / 读写·需批准 / 读写·免批准）
- **用量**：本轮 token、会话累计 token、`max_total_tokens` 预算
- **上下文**：四段分区（system / head / summary / active）的条数与**估算** token、距压缩阈值、手动压缩
- **配置中心**：读写 `harness.yaml` 并**重建运行时**（模型、压缩参数、agent 参数、工具开关…）
- **MCP 管理**：结构化表单增删改 `mcp.servers`、逐条「测试连接」、保存即重连、**逐 server 启用/停用开关**（per-card，即改即生效）
- **技能管理**：技能全 CRUD（新建 / 编辑 / 删除）、**zip 上传**（含冲突覆盖确认）、**逐技能启用/停用开关**、草稿校验（lint）、目录级 lint、重新扫描
- **运维**：工具启停、长期记忆编辑、调用链（span 树）、服务状态

### 非目标（本期不做）

| 不做 | 说明 |
|---|---|
| 前端框架 / 构建链 | 用户明确选择「单文件继续扩展」（§3）；不引 CDN、不引 npm |
| 语法高亮 | 用等宽字体 + 底色 + 语言标签替代（零依赖） |
| 离线编辑队列 | 前端不做本地写入队列；断网时只提示，不缓存待提交的写操作 |
| 鉴权 | 见 §8 已知限制（`server.auth_token` 列为后续项） |

---

## 2. 挂载与文件

| 项 | 值 |
|---|---|
| 文件 | `server/static/index.html`（唯一文件：内联 `<style>` + 内联 `<script>`，无外部资源） |
| 挂载 | `StaticFiles(directory=STATIC_DIR, html=True)` → `/ui`；`/` 302 → `/ui/`（见 `server/app.py`） |
| 同源 | 与 API 同端口，省去 CORS |

**运行**：`python main.py`（读 `harness.yaml` 的 `server.host/port`）→ 浏览 `http://127.0.0.1:8000/ui/`

---

## 3. 单文件形态与升级路径

用户决策：**继续扩展单文件**（约 2000 行）。为控制膨胀：

- 强分区注释（`0. utils` / `1. state` / `2. api` / `3. 消息渲染` / … / `12. 面板调度`）
- 命名前缀：`render*`（渲染）、`load*`（拉取）、`bind*` / `on*`（事件）
- 纯函数（`renderMarkdown` / `readConfigForm`）不依赖 DOM 全局，便于将来外移

> **升级路径**：若超过约 2500 行，建议拆为 `index.html + app.css + app.js`（仍**无构建**，用 `<link>` / `<script>` 引入，`StaticFiles` 已能直接托管）。届时上半文件几乎可原样搬到 `app.css`，脚本部分按分区切成若干 `.js`。

---

## 4. 信息架构

```
#app (grid: 264px | 1fr | 0 或 352px)
├─ aside#sidebar            会话列表：新建 / 搜索 / 双击重命名 / 悬停项内「重命名 / 删除」按钮 / 运行中脉冲点 / 底部状态
├─ main#chat                标题（双击重命名）/ 会话设置 / 消息流 / 输入区
│   ├─ #chatHead            标题 + run 状态（aria-live）+ 抽屉开关 ▤
│   ├─ #policy              会话设置：工作区（目录点选）+ 三档权限下拉
│   ├─ #log                 消息气泡 / 工具折叠块 / 审批卡片 / 空状态引导
│   └─ #composer            图片 chips + 上传 + 多行输入（自动增高）+ 停止 + 发送
├─ aside#drawer             8 标签抽屉（可折叠，宽屏为列，窄屏为浮层）
│   └─ 用量 | 配置 | MCP | 技能 | 工具 | 记忆 | 调用链 | 状态
└─ #toasts                  右下角提示（aria-live）
```

**视觉**：`:root` 设计变量（配色 / 间距 / 圆角 / 阴影）→ `body[data-theme]` 深浅主题（跟随 `prefers-color-scheme`，用户切换写 localStorage）；`.msg` 气泡、`details.tool` 折叠块、`.card` 面板、`.skeleton` 骨架屏、`.empty` 空状态、`:focus-visible` 焦点环、`aria-live`。

**响应式**：`≤1100px` 抽屉变右侧**浮层**（此时隐藏 `▤`，用抽屉内 `×` 或 `Esc` 关闭）；`≤760px` 侧栏变 off-canvas（`☰` 开关）、输入区纵向排列。

---

## 5. 状态与持久化

```js
const S = { sessionId, sessions[], runId, running, lastSeq, detail, usageTurn, lastRunId,
            config, effective, envOverrides[], theme, tab, drawer, files[], toolIndex{},
            interruptQueue[], workspace, permission };   // 后两个是「本会话设置」，会话 metadata 优先、否则用本地默认值
```

localStorage 键（前缀 `harness.`）：`sid`（上次会话）、`theme`、`tab`、`drawer`、
`workspace` / `permission`（**新会话的默认值**；已在的会话以自身 metadata 为准）。

- 启动：`loadConfig()` → `loadTools()` → `setDrawer/showTab` → `loadSessions()` → `loadHealth()` → 恢复上次会话（`openSession`）。
  **配置必须最先拉**：MCP / 技能面板直接用 `S.config` 渲染表单，若晚于首次渲染就会残留「配置不可用」占位符（该时序 bug 已在浏览器验收中复现并修复）。
- `tab` 做**白名单校验**（`TABS`，8 项），非法值回落 `usage`——避免历史脏值导致面板空白。
- 动态文本一律走 `textContent`；仅 `renderMarkdown()` 用 `innerHTML`，且**先转义**（§6）。
- `S.scopes` 是**改造前的遗留回落值**（默认 `["fs:read"]`）：会话没设 `permission` 时才用它，
  保证旧会话行为逐字节不变。

---

## 6. Markdown 渲染（零依赖、安全）

不变量：**先 `esc()` 转义整段，再做替换**——因此模型输出里的 `<script>` 只会是字面量。

顺序：

1. 提取围栏代码块 → 存进数组，正文位置替换为 `\u0000B<i>\u0000` 占位符（该字符用户无法产出，也不会被后续内联规则命中）
2. `esc()` 整段
3. 逐行块级解析：`#` 标题 / 有序无序列表 / `>` 引用（已转义为 `&gt;`）/ `---` 分隔线 / 空行分段 / 其余成段（段内 `<br>`）
4. 行内替换：行内代码 → `**粗**` → `*斜*` → `~~删除~~` → `[文本](链接)`（`href` 经 `safeUrl()` 白名单 `https?:|mailto:|/|#`，否则退化为 `#`）
5. 还原占位符为 `<pre><code>`（内容二次 `esc()`，语言名只允许 `[A-Za-z0-9+#.-]`）

边界与取舍：**不支持原始 HTML / 表格 / 嵌套列表 / 脚注**；未闭合语法保持字面量。流式期间用纯文本 + `textContent` 渲染（视觉稳定），`done` 时一次性 `renderMarkdown`。

---

## 7. 端点映射（前端 → 后端）

| 面板 / 功能 | 端点 |
|---|---|
| 会话列表 / 新建 / 删除 | `GET /sessions`、`POST /sessions`、`DELETE /sessions/{id}` |
| 打开会话（详情 + 历史） | `GET /sessions/{id}`、`GET /sessions/{id}/messages` |
| 重命名 | `PATCH /sessions/{id}` `{title}` |
| 会话设置（工作区 / 权限档位） | `PATCH /sessions/{id}` `{metadata:{workspace,permission}}`；新建时随 `POST /sessions` 提交 |
| 工作区目录点选 | `GET /fs/browse?path=…` |
| 发消息 + 打字机 | `POST /sessions/{id}/turns` `{input, images?, stream:true, scopes[]}` → `GET /runs/{id}/events`（SSE）。会话设了 `permission` 时 `scopes` 传 `[]`（由服务端按档位推导） |
| 取消 | `POST /runs/{id}/cancel` |
| 手动压缩 | `POST /sessions/{id}/compact` `{force:true}` |
| 用量 / 上下文 | `GET /sessions/{id}` 的 `usage_totals / context / compaction / budget / active_run_id` |
| 配置读写 | `GET /config`、`PUT /config` `{config, reload}`、`POST /config/reload` |
| 模型连通性测试 | `POST /llm/ping` |
| 工具 / 启停 | `GET /tools`；启停＝改 `config.tools.disabled` 后 `PUT /config` |
| MCP 列表 / 编辑 | `GET /mcp`；编辑＝改 `config.mcp.servers` 后 `PUT /config` `{reload:true}` |
| MCP 启用 / 停用 | `PATCH /mcp/{name}` `{enabled}`（即改即生效，不重建部件；前端随后刷新工具表） |
| MCP 测试连接 | `POST /mcp/test` `{server}` → `{ok, tools, latency_ms, error}` |
| MCP 重连 | `POST /mcp/reload` |
| 技能列表 / 详情 | `GET /skills`、`GET /skills/{name}`（详情含 `body`） |
| 技能新建 / 编辑 / 删除 | `POST /skills`（201）、`PUT /skills/{name}`、`DELETE /skills/{name}`（204） |
| 技能启用 / 停用 | `PATCH /skills/{name}` `{enabled}`（翻 `skills.disabled`，即改即生效，运行中也能切） |
| 技能 zip 上传 | `POST /skills/upload`（**raw body** = zip 字节；冲突返回 `skill_conflict`，`?overwrite=true` 覆盖） |
| 技能校验 | `POST /skills/validate`（草稿 lint，**不落盘**）、`GET /skills/lint`（目录级）、`POST /skills/reload` |
| 记忆 | `GET /memory`、`PUT /memory` `{content}` |
| 调用链 | `GET /runs/{id}/trace?source=memory`（`tree` 递归渲染 span） |
| 服务状态 | `GET /healthz` |
| 审批 / 提问 | SSE `interaction_required` → `POST /interactions/{id}` `{approved}` / `{answer}` |

字段与错误码的权威定义见 [api-service-design.md](api-service-design.md) §4。

---

## 8. 关键交互实现

| 交互 | 做法 |
|---|---|
| **SSE 断线续传** | 记 `lastSeq`（取自 `e.lastEventId`），断线后指数退避（0.4/0.8/1.6/3.2s，最多 4 次）用 `?after=lastSeq` 重连；状态条显示「连接中断，重连中 (n/4)」 |
| **重连正在跑的 run** | `GET /sessions/{id}` 的 `active_run_id` 非空 → 自动挂流；发送按钮**不再禁用**（运行中即「打断并发送」，见下） |
| **对话软打断（打断并发送）** | `setRunning(on)` 不禁用发送，只把文案切成「打断并发送」/「发送」；`send()` 去掉了 `if (S.running) return;` 早退。收到 `202` 且响应 `handle.interrupt === true` → toast「已收到，当前工具执行完就回应」并**不重建 EventSource**（否则会断掉正在跑的流）；该输入由后端在 `done.interrupts` 里带回 |
| **插话续发** | `done` 读 `d.interrupts`，与本地残留 `S.interruptQueue` 合并（**先旧的、后新收的**）→ `drainInterrupts`：多条先 toast「N 条排队输入将依次发送」，**每次只续发队首一条**（每条开新 turn、更新 `S.runId` 并重建 EventSource），其余留队列等下一轮 `done`；`stop_reason === 'cancelled'`（用户主动停止）时**清空队列、不续发**。`STOP_LABEL.interrupted = '已打断 · 等你的新输入'`（有排队输入时会几乎立刻被「运行中…」覆盖，属预期） |
| **中文输入法** | `compositionstart/end` + `e.isComposing` + `keyCode===229` 三重判断，避免选词回车误发；`Shift+Enter` 换行 |
| **图片** | FileReader → data URL 预览 chips（可移除、单张 ≤1.5MB、最多 4 张）→ `POST /turns` 的 `images` |
| **配置表单** | 按值类型选控件：bool→开关、number→数字框、`string[]`→一行一项文本域、对象/数组→JSON 文本域（实时 `JSON.parse` 校验）、`api_key`→密码框；`***`/空＝「不改」（后端 `restore_secrets` 还原旧值） |
| **保存冲突** | `409 config_reload_conflict` → 弹确认「取消全部 run 并重试」；`422 config_invalid` → 按 `details.path` 定位输入框标红并滚动 |
| **标题刷新** | 不在 SSE 里加事件（`done` 后 `Run.stream` 已收流）；改为 `done` 后 0.9s / 2.4s 各 `GET /sessions/{id}` 一次，给后台标题任务留完成窗口 |
| **MCP 编辑** | 表单**渲染一次**，读值时按 `data-i`/`data-k`/`data-kind` 从 DOM 汇总（避免每次输入重渲染导致失焦）；`args`/`scopes` 一行一项、`env` 每行 `KEY=VALUE`、`tool_overrides` 走 JSON 文本框（前端先 `JSON.parse` 校验）；保存＝把 `S.config.mcp.servers` 整段替换后 `PUT /config`，后端重连失败的警告以 warn toast 逐条提示 |
| **MCP 测试连接** | `POST /mcp/test` 用**临时** registry 起一次连接（20s 超时），只探测不落盘；按钮进「测试中…」置灰，`finally` 复原 |
| **MCP 启用 / 停用** | 每张卡片头复选框带 `data-i` / `data-k="enabled"` / `data-kind="bool"`，因此**也会被 `_readMcpForm()` 采集**——点「保存并重连」不会把 `enabled` 静默重置（回归项）。`toggleMcp(index, name, enabled)` → `PATCH /mcp/{name}` 并刷新工具表；`GET /mcp` 里没有该 name（尚未保存）时点开关会提示先保存；徽标新增「已停用」态；新增 server 默认带 `enabled: true` |
| **技能编辑** | 编辑器为抽屉内覆盖层：`name` 只读（=目录名，后端强制）、`description`/`version`/`allowed-tools`/`body` 可改；「校验」＝`POST /skills/validate`（不落盘），保存＝`POST`（新建）/`PUT`（覆盖） |
| **技能上传 zip** | 工具栏「⇪ 上传 zip」触发隐藏的 `<input type="file" accept=".zip">`；`uploadSkillZip(file, {overwrite})` 走 `api.upload()`（raw body，复用同一套错误解析）`POST /skills/upload`；收到 `skill_conflict` → `confirmDialog` 列出冲突名单 → 确认后带 `?overwrite=true` 自动重传 |
| **技能启用 / 停用** | 每行右侧复选框，停用后行变灰 + 「已停用」标签；`toggleSkill(name, enabled)` → `PATCH /skills/{name}` → toast + 重新加载面板（翻 `skills.disabled`，即改即生效） |
| **技能列表 lint** | 每张卡片显示该技能自身的 issue（来自创建/更新响应里的 `issues`）；「跑一遍 lint」＝`GET /skills/lint` 汇总目录级问题 |
| **工作区选择** | 值来源优先级：会话 `metadata.workspace` → localStorage（新会话默认值）。点按钮弹目录浏览覆盖层（`GET /fs/browse`）：`↑` 用**服务端返回的 `parent`**（前端自己拼父路径会把 `C:\Users` 算成 `C:`，被解析成进程 cwd）、点目录逐级进入、也可手填绝对路径；「选择当前目录 / 用配置默认 / 取消」三个出口 |
| **三档权限** | 一个 `<select>`（`read` / `write` / `write_free`），标签映射在 `PERMISSION_LABELS`；改动即 `PATCH /sessions/{id}` 落库。会话有活跃 run 时后端 409 → toast「会话正在回复，稍后再改」并**以服务端为准重新拉一次**回滚显示 |
| **危险操作** | 删除会话 / 技能 / MCP server 均二次确认（页内 `.overlay`，非原生 `dialog`）；`side_effect=destructive` 工具在列表与审批卡片上以 `--danger` 色标记 |

---

## 9. 与后端契约的对应关系（重要）

前端**不重新实现**后端已有的判断逻辑，避免语义漂移：

- 工具「启用」判定：由后端 `GET /tools` 用 `is_tool_allowed()` 现算（前端不复制 fnmatch 语义）
- 权限档位 → scopes / 审批：由后端 `harness/tool/permission.py` 单点映射（前端只传档位名，不拼 scopes）
- 工作区 → 沙箱边界：由后端 `session_sandbox()` 构造 `SandboxPolicy`（前端只传目录路径）
- 距压缩阈值：由后端算好 `remaining_messages` / `remaining_tokens`
- token **估算**：`estimate_tokens()` 在后端（中文≈1 字/token、ASCII≈4 字符/token、图片按固定值）；前端只展示，且文案明确写「估算值」
- 「配置写了什么」与「运行时真正用什么」：`GET /config` 的 `config`（脱敏原始 YAML，可编辑）与 `effective`（运行时生效值，只读）分列展示
- 插话续发：前端**不自行判断该不该打断**（是否打断由后端探测），只消费 `done.interrupts`；合并顺序固定为「先旧的（本地残留队列）、后新收的（本轮 `interrupts`）」，并**每次只续发队首一条**、其余留队列等下一轮 `done`；`stop_reason === 'cancelled'` 时后端刻意不回传 `interrupts`，前端一并清空队列、不续发

---

## 10. 已知限制

| 限制 | 影响 / 缓解 |
|---|---|
| 无鉴权 | `PUT /config` 与 `GET /fs/browse`（目录枚举）一旦暴露到 `0.0.0.0` 就等于开放配置与目录列表；默认 `server.host=127.0.0.1`，页面与文档提示不要暴露公网；`server.auth_token` 列为后续项 |
| 工作区不约束 MCP 工具 | MCP 工具由外部 server 自己访问文件，不经沙箱（默认也无 scopes / 无审批）；要收口请给 MCP server 配 `default_scopes` / `default_requires_approval` |
| 单文件约 2000 行 | 见 §3 升级路径 |
| token 为估算 | 面板文案标注；预算告警用真实 `usage_totals` |
| 无语法高亮 | 等宽字体 + 语言标签 |
| 写配置丢注释/键序 | 后端 `dump_raw` 会规范化，旧文件备份 `.bak`；前端在配置面板顶部明示 |
| 窄屏 `≤1100px` | 抽屉为浮层，会覆盖聊天区右侧（设计如此）；关闭用 `×` 或 `Esc` |
| 取消/异常轮次不计入累计用量 | 用量面板以「usage 事件 vs 轮次」的差异提示（当前仅提示文案，不做强校验） |
| `POST /skills/upload` 等于任意 zip 落盘 | 该端点把上传的 zip 解压写入 `skills.root`（仅做基础校验）；与 `PUT /config`、`GET /fs/browse` 同属「无鉴权 + 默认仅本机」风险面，切勿暴露公网 |

---

## 验收

技能启用/停用、MCP per-card 开关（含「保存并重连后开关不被重置」回归）、技能 zip 上传与覆盖确认、
「打断并发送 + 自动续发」均已在本机浏览器实测通过，Console 无红色 `error`。
