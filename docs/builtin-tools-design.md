# 内置工具扩展设计文档（P1 / P2）

> 定位：**补齐 `net.*` / `fs.*` 增删改 / `time.now` / `code.run`** · 技术栈：**Python（零新增依赖，标准库 `urllib`）** · 状态：**已实现**
> 相关：[工具系统设计文档](agent-tool-system-design.md) · [沙箱设计文档](sandbox-design.md) · [配置系统设计文档](configuration-system-design.md) · [项目 TODO](../todo.md)

---

## 1. 本期范围

[tool/README.md](../harness/tool/README.md) §8 的 P1 / P2 规划表里，本期落地这些：

| 工具 | 文件 | side_effect | scopes | 审批 |
|---|---|---|---|---|
| `net.fetch` | `builtin/net.py` | read | `net:read` | 否 |
| `net.download` | `builtin/net.py` | write | `net:read` `fs:write` | 否 |
| `fs.mkdir` | `builtin/fs.py` | write | `fs:write` | 否 |
| `fs.copy` | `builtin/fs.py` | write | `fs:write` | 否 |
| `fs.move` | `builtin/fs.py` | write | `fs:write` | 否 |
| `fs.delete` | `builtin/fs.py` | destructive | `fs:write` | **是** |
| `time.now` | `builtin/clock.py` | read | — | 否 |
| `code.run` | `builtin/code.py` | destructive | `code:run` | **是** |

**本期不做**：

- `net.search` —— 必须依赖某家搜索 API（Tavily / Brave / SerpAPI 等），没有 key 就是死代码，
  且无法离线测试。等定了 provider 再单独开一轮（届时按"可插拔 endpoint + 通用 JSON 契约"设计）。
- OS 级进程隔离（容器 / Job Object）—— 见 §6 的诚实限制。

---

## 2. 四个决策

| 议题 | 结论 | 理由 |
|---|---|---|
| `net.search` | **不做** | 无 key 即死代码，无法离线自测 |
| 域名策略 | **默认放行 + 黑名单** | 黑名单（SSRF 基线）**始终生效**，不需要先开沙箱；想收紧再追加 |
| `fs.delete` | **审批后真删** | 走既有审批通道（服务里就是人在 UI 点确认），不新增交互形态 |
| `code.run` | **应用层沙箱 + 环境清理** | 与 `shell.run` 同一信任模型，额外做路径限定与密钥隔离 |

---

## 3. 网络域名策略（`sandbox.py`）

[沙箱设计文档](sandbox-design.md) §1 把「网络：域名白名单」列在非目标里（当时 `net.*` 还不存在）。
现在它归位到 `sandbox.py` —— 沙箱就是"边界层"，管**能碰哪些路径**与**能连哪些域名**。

### 3.1 默认黑名单（SSRF 基线，不可关闭）

```python
DEFAULT_DENIED_DOMAINS = (
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
    "169.254.169.254",            # 云元数据服务
    "metadata.google.internal",
    "*.local", "*.internal", "*.localhost",
)
```

匹配规则（deny 语义下**必须连带子域**）：

| pattern | 命中 |
|---|---|
| 无通配字符（如 `localhost`） | host 等于它，**或** host 以 `.localhost` 结尾 |
| 含通配字符（如 `*.internal`） | 复用路径匹配那套 globstar 引擎 |

`check_url(url)`：先**只允许 `http` / `https`**，再取 host 跑黑名单，命中即 `SandboxViolation`。

> **为什么默认列表不可配"覆盖"，只能"追加"**：允许覆盖等于允许把 SSRF 基线删掉。
> 配置项叫 `extra_denied_domains`，语义是"在内置默认之外追加"。

### 3.2 重定向要逐跳复查

`urllib` 默认跟随 302/301，攻击者可用「允许的域名 → 内网地址」跳一次绕过入口检查。
所以装一个自定义 `HTTPRedirectHandler`，**每一跳都重新 `check_url`**，越界直接拒绝。

### 3.3 诚实的限制

| 限制 | 说明 |
|---|---|
| 不防 DNS rebinding | 只校验**域名字符串**，不解析 IP；`evil.com` 解析到 `10.0.0.1` 拦不住 |
| 不防 IP 直连的私网段 | `http://10.0.0.1/` 不在默认黑名单里（要拦就把网段加进 `extra_denied_domains`） |
| 黑名单是"域名"，不是"网络策略" | 真正的出网管控需要防火墙 / 代理，不在应用层 |

---

## 4. `net.fetch` / `net.download`

### 4.1 `net.fetch`

```python
net.fetch(url, max_bytes=200_000) -> str
```

- 只允许 `http` / `https`；先 `check_url`。
- 同步阻塞（`urllib`），由 Executor 走 `asyncio.to_thread`，不阻塞事件循环。
- **读取上限**：按 chunk 读，超过 `max_bytes` 就停止并标注"已截断"（防大文件吃满上下文）。
- **HTML → 纯文本**：用标准库 `html.parser.HTMLParser` 扒标签，跳过 `<script>` / `<style>`。
  零依赖，因此**不做完整的 markdown 转换**（那要 `html2text` / `markdownify`）。
- 编码：优先 `Content-Type` 里的 `charset`，否则 utf-8 + `errors="replace"`。
- 返回结构：`{url, final_url, status, content_type, text}`（`final_url` 让模型看到重定向去了哪）。

### 4.2 `net.download`

```python
net.download(url, path, max_bytes=20_000_000) -> str
```

- `path` 过 `resolve_path(mode="write")` —— **落盘位置受沙箱约束**（沙箱关闭时不受限，与其它 `fs.*` 一致）。
- 边下边写临时文件，超 `max_bytes` 立即中止并**删掉半成品**（不留垃圾）。
- 下完 `os.replace` 原子落位（与 [记忆系统](memory-system-design.md) 同一手法）。

---

## 5. `fs.*` 增删改

全部复用 [sandbox.py](../harness/tool/sandbox.py) 的 `resolve_path`：

| 工具 | 关键点 |
|---|---|
| `fs.mkdir` | `parents=True, exist_ok=True` → `idempotent=True` |
| `fs.copy` | `src` 过 `mode="read"`、`dst` 过 `mode="write"`；目录用 `copytree`，文件用 `copy2`；`dst` 已存在直接报错（不静默覆盖） |
| `fs.move` | 同上双向校验；`shutil.move`；**两端都必须在边界内**（不能把文件搬出工作区） |
| `fs.delete` | 目录需显式 `recursive=True`，否则报错；**拒绝删除工作区根自身**；`destructive` + `requires_approval` |

`fs.delete` 的 `recursive=False` 时非空目录报错，是为了让"手滑删掉整个目录"变成一次显式选择。

---

## 6. `code.run`：应用层沙箱 + 环境清理

### 6.1 它比 `shell.run` 多做了什么

| 维度 | `shell.run` | `code.run` |
|---|---|---|
| 输入 | 一整条命令行字符串 | `language` + `code`（结构化） |
| 执行 | `create_subprocess_shell`（走 shell 解析） | `create_subprocess_exec`（**不经 shell**，无注入面） |
| 工作目录 | 沙箱开启时收敛到 root | **总是**落在工作区内的临时目录 |
| 环境变量 | 全量继承（含 `OPENAI_API_KEY`） | **allowlist 清理**（见 6.3） |
| 清理 | 无 | `finally` 里删掉临时目录 |

### 6.2 语言与解释器

```python
LANGUAGES = {
    "python":     ("python", "python3", "py"),
    "node":       ("node",),
    "javascript": ("node",),
}
```

按顺序取第一个**既在 shell 白名单里、又能被 `shutil.which` 找到**的候选；
一个都不可用就返回结构化错误（提示"请把解释器加入 tools.shell.allowed_commands"）。

### 6.3 环境变量 allowlist

```python
SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
    "VIRTUAL_ENV", "PYTHONPATH", "NODE_PATH",
}
```

**为什么用 allowlist 而不是 denylist**：denylist 靠猜密钥变量名（`*_KEY` / `*_TOKEN`），
漏掉一个就是泄漏；allowlist 是"默认不给"，可审计。代价是新工具可能需要补名单——这是显式成本，可接受。

### 6.4 临时目录

`data/tmp/`（与 `data/sessions` / `data/traces.jsonl` 同级的运行期目录），在其下 `mkdtemp`。
经 `resolve_path(mode="write")` 校验：**沙箱开启时它必须落在工作区内**，否则直接报错
（这时应把 `sandbox.root` 指到包含它，或把工作区设对）。

---

## 7. 配置

```yaml
tools:
  sandbox:
    # …（原有字段，见 sandbox-design.md §6）
    denied_domains: []        # 在内置 SSRF 基线之外**追加**（不能覆盖基线）
  net:
    max_bytes: 200000         # net.fetch 单次读取上限
    max_download_bytes: 20000000  # net.download 单文件上限
```

新增 `NetConfig`，装配点 `build_net_policy(config)`（与 `build_sandbox` / `build_shell_policy` 同款，
进程级全局 + 返回旧值）；`server/state.py::build_state` 里并列调一次。

> 域名策略放在**配置的** `tools.sandbox` 下（沙箱是边界层），**实现**也在 `sandbox.py`；
> `tools.net` 只放体积上限与解析相关的东西。两个节各管一件事，不重叠。

---

## 8. 里程碑

| 里程碑 | 内容 | 交付 |
|---|---|---|
| M1 | `sandbox.check_url` + `DEFAULT_DENIED_DOMAINS` + `extra_denied_domains` 字段；`NetConfig` + `build_net_policy` | ✅ tool **+5 条**：SSRF 基线（7 个目标）、正常域名、非 http/https、追加不覆盖基线、`net.fetch` 越界回 `sandbox_denied`；config **+3 条**：默认值 / 应用与还原 / 追加语义 |
| M2 | `builtin/net.py`（`net.fetch` / `net.download`）+ `builtin/clock.py`（`time.now`） | ✅ tool **+6 条**：HTML 剥标签、按 `max_bytes` 截断、原子落盘、超限中止且不留半成品、落盘受沙箱约束、`time.now` 字段 |
| M3 | `fs.mkdir` / `fs.copy` / `fs.move` / `fs.delete` | ✅ tool **+6 条**：四个工具串起来跑通、目标已存在拒绝覆盖、move 不能越界、delete 需审批、目录需 `recursive`、拒绝删工作区根 |
| M4 | `builtin/code.py`（`code.run`）+ 配置/前端权限勾选 + 文档同步 | ✅ tool **+6 条**：跑通 python、cwd 落在工作区临时目录、**密钥环境变量不外泄**、临时目录必清、语言不支持报错、解释器必须白名单 |

全量测试：tool 73、skill 15、llm 27、agent 38、session 25、memory 16、config 41、server 21 = **256 条，全部离线**。

另外两次 `build_state` 端到端验证：

1. 开沙箱 + 追加 `blocked.example`：`fs.mkdir/copy` 成功；越界与两个黑名单域名 → `sandbox_denied`；
   **`net.fetch("https://example.com")` 真实网络请求成功**。
2. 带审批回调：`fs.delete` 真删成功；`code.run` 跑通且 `data/tmp` 无残留；子进程 `os.environ` 里
   **看不到 `OPENAI_API_KEY`**。

---

## 9. 兼容性

- **不新增依赖**：只用标准库（`urllib` / `html.parser` / `shutil` / `tempfile` / `datetime` / `socket`）。
- 新增工具同样受 `tools.enabled` / `tools.disabled` 约束——老配置想保持"工具集合不变"只需
  `disabled: ["net.*", "code.*", "fs.move", "fs.copy", "fs.mkdir", "fs.delete", "time.*"]`。
- 域名黑名单**默认就生效**（`net.*` 是新工具，不存在行为回归）。
- `fs.*` 既有六个工具的行为不变；新增四个同样是 `resolve_path` 的消费者。

---

## 10. 决策记录

| 议题 | 结论 |
|---|---|
| `net.search` | 本期不做（无 key 即死代码、无法离线测） |
| HTTP 客户端 | **标准库 `urllib`**，不新增依赖；`net.fetch` 用 `html.parser` 扒纯文本，不引入 `markdownify` |
| 域名策略 | **默认放行 + 黑名单**；内置 SSRF 基线**始终生效**，配置只能追加 |
| 域名归属 | 放 `sandbox.py`（边界层管路径与域名），不放 `net.py` |
| 重定向 | 自定义 handler **逐跳复查** |
| `fs.move`/`fs.copy` | 两端都过沙箱（不能借"移动"把文件搬出工作区） |
| `fs.delete` | `recursive` 显式；**拒绝删工作区根**；审批后真删 |
| `code.run` 隔离级别 | **应用层沙箱**（路径限定 + 解释器白名单 + 环境清理），非 OS 级 |
| `code.run` 环境变量 | **allowlist**（默认不给），不用靠猜变量名的 denylist |
| `code.run` 执行方式 | `create_subprocess_exec`（不经 shell，无注入面） |

### 仍待确认

- [ ] 真要做 OS 级隔离时的路线：Windows Job Object + 低权限用户 / 容器化 —— 需要独立一轮。
- [ ] `net.search` 的 provider 与通用 JSON 契约。
- [ ] `extra_denied_domains` 是否也要支持 IP 网段（如 `10.0.0.0/8`）？
