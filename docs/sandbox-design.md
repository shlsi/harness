# 沙箱设计文档

> 定位：**工作区边界 + 路径权限（进程级，收口 `fs.*` 与 `shell.run`）** · 技术栈：**Python（零依赖，纯标准库）** · 状态：**已实现 M1–M3**
> 相关：[工具系统设计文档](agent-tool-system-design.md) · [配置系统设计文档](configuration-system-design.md) · [Agent Loop 设计文档](agent-loop-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

补齐 [todo.md](../todo.md) P2 的「沙箱」：给所有**碰路径**的工具加一道边界，让模型够不着工作区之外。

[工具系统设计文档](agent-tool-system-design.md) §6.3 只留了设想（路径白名单 / 只读目录 / 域名白名单），§10 标注
「`sandbox` 层尚未实现」；[shell.py](../harness/tool/builtin/shell.py) 自己也写着「沙箱层落地后应迁到那里」。
本模块就是那一层。

### 现状缺口

| 缺口 | 现状 |
|---|---|
| `fs.*` 无边界 | 六个工具全部 `Path(path)` 直取，**绝对路径可读写全盘** |
| `shell.run` 只有白名单 | 白名单 + 禁止模式拦的是「命令名」，命令里的路径参数不受约束 |
| 没有「工作区」概念 | 相对路径隐式基于进程 cwd，无法表达"这就是我的项目范围" |

### 非目标（本期不做）

- **不做 OS 级隔离**（容器 / Job Object / seccomp / 用户降权）。本期是**应用层**边界，
  只能拦"通过工具走的路径"，拦不住子进程自己干的事——这一点在 §8 如实标注。
- **域名策略只做黑名单**（默认放行 + SSRF 基线），不做域名白名单 / 出网代理 / IP 网段管控；
  也不防 DNS rebinding（只校验域名字符串）。实现见 §4.2，细节见
  [内置工具扩展设计文档](builtin-tools-design.md) §3。
- 不做对 `shell.run` 命令串的完整语法解析（那是 shell 的事），只做**保守的路径 token 扫描**。
- 不做多工作区（决策记录里有理由）。
- 不做 Windows ACL / 权限位模拟；只做「可读 / 可写」两档。

---

## 2. 三个必须先说清的判断

### 2.1 边界只划一次，粒度在工作区内

「工作区」不是一堆散落的路径白名单，而是：

```
工作区 = root（唯一边界）+ 权限表（在 root 内用 glob 声明读写的细分）
```

这样模型心里只有一个坐标：**「在不在工作区里」**。越界就是越界，理由单一、报错好懂。

### 2.2 默认关（opt-in），不静默改变既有行为

沙箱默认 `enabled: false`。理由：

- 打开后**相对路径的基准会从进程 cwd 变成 `root`**，这是语义变化，不该在升级时无声发生。
- 已有一批测试与用法依赖"绝对路径读全盘"。

于是 `enabled: false` 时 `resolve_path()` **原样返回 `Path(path)`**，行为与今天**逐字节一致**（见 §10）。

### 2.3 校验的输入必须是 `resolve()` 之后的真实路径

`..` 与符号链接是最常见的绕过手法：

```python
Path("C:/ws/../../../Windows/System32")   # 字符串层面"在 ws 里"，实际在外面
Path("C:/ws/link/passwd")                 # link -> C:/Windows，实际在外面
```

所以判定前**一律 `Path.resolve()`**（解 `..`、解符号链接、拿到绝对路径），
拿真实路径去比。写一个**尚不存在**的文件时，`resolve()` 也会解析到"最深的已存在祖先"，
因此 `root/a/../b.txt` 这种也能正确判定。

---

## 3. 工作区模型

### 3.1 三段判定

对一次路径解析，按顺序判：

| 顺序 | 条件 | 结果 |
|---|---|---|
| 1 | 命中 `deny_paths` | **拒绝**（读也拒，最高优先级） |
| 2 | 在 `root` 内 | 读：允许；写：`write_paths` 为空则允许，否则须命中 |
| 3 | 在 `writable_mounts` 内 | 读写都允许（仍受 1 约束） |
| 4 | 在 `readonly_mounts` 内 | 读允许；写**拒绝** |
| 5 | 其它 | **拒绝** |

> `deny` 放最前面，是为了让「黑名单永远赢」——否则挂载进来的敏感目录会把 root 内的规则带过去。

### 3.2 `deny` 的匹配目标

glob 匹配目标随路径位置切换，避免 `**/.env` 与 `.env` 两种写法互相打架：

| 路径位置 | 匹配目标 |
|---|---|
| 在 `root` 内 | **相对 root 的 posix 路径**（如 `.git/config`） |
| 不在 `root` 内 | **绝对 posix 路径**（如 `C:/Windows/x`） |
| pattern **不含 `/`** | 改用**文件名**（basename）匹配 |

所以：

```yaml
deny_paths:
  - ".env"          # 任意目录下的 .env（按文件名匹配）
  - "**/.git/**"    # root 内任意深度的 .git
  - "**/*.pem"
```

### 3.3 相对路径的基准

`enabled: true` 时，相对路径基于 `root` 解析（`root` 自身相对**进程启动目录**解析一次并固化）。
`enabled: false` 时不做任何处理，沿用 Python 的默认（相对 cwd）。

---

## 4. 契约

```python
# harness/tool/sandbox.py
class SandboxViolation(Exception):
    """路径越界 / 命令引用越界路径。由 Executor 转成结构化错误回喂模型。"""

@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool
    root: Path                          # 已 resolve 的绝对路径
    write_paths: tuple[str, ...]        # root 内写白名单 glob；空 = root 内全部可写
    deny_paths: tuple[str, ...]         # 最高优先级黑名单 glob
    readonly_mounts: tuple[Path, ...]   # root 外额外只读
    writable_mounts: tuple[Path, ...]   # root 外额外可写
    extra_denied_domains: tuple[str, ...]   # 追加到 SSRF 基线黑名单

def configure_sandbox(**kwargs) -> SandboxPolicy   # 覆盖**进程级默认**策略，返回覆盖前的
def current_sandbox() -> SandboxPolicy             # 每调用覆盖优先，否则进程级默认
def use_sandbox(policy | None) -> ContextManager   # 在 with 块内临时覆盖（None = no-op）
def resolve_path(path: str | Path, *, mode: Literal["read", "write"]) -> Path
def check_command(command: str, cwd: Path) -> None   # shell 用：越界抛 SandboxViolation
def check_url(url: str) -> str                       # net 用：返回 host，命中黑名单抛 SandboxViolation
```

`configure_sandbox` 与 [shell.configure_shell_policy](../harness/tool/builtin/shell.py) **同款**：
进程级全局 + 返回旧值（便于测试还原）。**但它只是"默认值"**——见下节。

### 4.1 两级作用域：进程级默认 + 每调用覆盖

控制台允许**按会话**选工作区（见 [前端控制台设计](frontend-console-design.md)），
而 `fs.*` / `shell.run` / `code.run` / `net.*` 这些消费者**都没有 `ctx` 形参**
（`fs.read(path, max_bytes, encoding)` 这类签名不该为了沙箱被污染）。
于是引入一个 `contextvars.ContextVar` 承载「本次工具调用的策略」：

```python
_policy: SandboxPolicy = SandboxPolicy()             # 进程级默认（装配期由配置写入）
_override: ContextVar[SandboxPolicy | None] = ...    # 每调用覆盖

def current_sandbox() -> SandboxPolicy:
    return _override.get() or _policy                # 覆盖优先

@contextmanager
def use_sandbox(policy: SandboxPolicy | None):
    if policy is None:      # 关键：None 是 **no-op**，不是"覆盖成 None"
        yield; return       # 否则会抹掉外层 run 级策略、静默回落到进程全局
    token = _override.set(policy)
    try: yield
    finally: _override.reset(token)
```

宿主在唯一的一处接线——[`Executor._run_tool`](../harness/tool/executor.py)：

```python
with use_sandbox(ctx.sandbox):        # ctx.sandbox 来自会话设置；None → 沿用进程级默认
    return await tool.fn(**kwargs)    # 同步工具走 asyncio.to_thread，也会带上这份 context
```

**为什么这能工作**：

- `asyncio.to_thread` 内部 `contextvars.copy_context()`（CPython ≥3.9，本仓库要求 ≥3.10），
  所以**同步工具**（`fs.*` / `net.*`）在线程池里也能看到覆盖值。
- `Executor.execute_tool_calls` 用 `asyncio.gather` 并发跑一轮的多个 tool_call；
  每个调用被包成独立 Task，Task 创建时复制 context，因此在 `_run_tool` 内 set/reset
  **天然按工具隔离**，并发会话不会串味。
- 只替换 4 个直接读全局的位置（`_check` / `resolve_path` / `check_command` / `_domain_denied`）；
  `_targets` / `_first_match` 本来就收 `policy` 形参，`_match` 的 `@lru_cache` 键是
  `(target, pattern)` 不含 policy，均无需改动。

**热重建与快照**：`apply_config` 重建的是进程级默认；已在跑的 run 持有 `ctx.sandbox` 快照，
因此热重建**不会中途改变在跑 run 的边界**（更安全）。新 run 读新配置。


### 4.2 域名（`check_url`）

网络是第二条边界，与路径**共用同一个策略对象**，但**开关独立**：

```python
DEFAULT_DENIED_DOMAINS = (
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
    "169.254.169.254",              # 云元数据服务
    "metadata.google.internal",
    "*.local", "*.internal", "*.localhost",
)
```

- **基线始终生效**，与 `enabled` 无关——`net.*` 装好就带 SSRF 防护。
- 配置项是 `denied_domains`（映射到 `extra_denied_domains`），语义**只能是"追加"**：
  允许覆盖等于允许把基线删掉。
- 只允许 `http` / `https`；无通配字符的 pattern 连带子域（`localhost` 也拦 `a.localhost`）。
- HTTP 重定向由 `net.py` 的自定义 handler **逐跳复查**，防"允许的域名 → 内网"一次跳转绕过。

---

## 5. 接入

### 5.1 `fs.*`（六个工具）

```python
p = resolve_path(path, mode="read")     # fs.read / fs.list / fs.glob / fs.grep
p = resolve_path(path, mode="write")    # fs.write / fs.edit
```

- **`fs.glob` / `fs.grep` 的结果要二次过滤**：`base.glob()` 可能产出指向外部的符号链接，
  逐个 `resolve_path(hit, mode="read")`，越界的**静默丢弃**（不进结果，也不报错——
  报错会让一次全局检索整体失败，丢几个结果更合理）。
- `fs.list` 只列一层/若干层名字，`iterdir()` 产出的条目同样逐个过滤。

### 5.2 `shell.run`

两处收敛（用户选定的「cwd + 扫描命令串路径」）：

1. **`cwd` 收敛**：`sandbox.enabled` 时 `cwd` 缺失则取 `root`，给了则 `resolve_path(cwd, mode="read")`。
2. **命令串扫描**：把命令按空白与 shell 分隔符切 token，挑出**像路径**的 token，逐个判定。
   越界即拒（fail-closed）。

「像路径」的判定（保守，宁可漏报也不误伤普通参数）：

```python
^[A-Za-z]:[\\/]      # C:\  C:/
^[\\/]               # 绝对路径
^\.\.$ 或 ^\.\.[\\/] # .. 及 ../x
```

> **已知漏报**：`foo/bar` 这类相对路径不判（它必然在工作区内，无需判）；
> `--flag=../x` 的 `=` 之后也不展开（否则 `--grep=..` 会被误判）；变量、命令替换、
> 重定向目标同样不展开。这是"应用层扫描"的天花板，不是 bug——见 §8。

### 5.3 错误回喂

新增错误类型：

```python
ErrorType.SANDBOX_DENIED = "sandbox_denied"
```

`Executor` 在 `run` 阶段**单独捕获** `SandboxViolation`（放在泛 `Exception` 之前），
转成结构化错误**直接返回、不重试**（重试同一个越界路径没有意义）：

```json
{
  "ok": false,
  "error": {
    "type": "sandbox_denied",
    "message": "路径越界: C:/Windows/System32/x 不在工作区内（root=C:/Users/Lenovo/Desktop/agent）",
    "hint": "请改用工作区内的路径；确需访问请调整 tools.sandbox 配置",
    "retryable": false
  }
}
```

模型能据此自纠（换成工作区内路径），而不是拿到一个笼统的 `execution_error`。

---

## 6. 配置与装配

```yaml
tools:
  sandbox:
    enabled: false         # 默认关（opt-in）
    root: "."              # 工作区根，相对启动目录
    write_paths: []        # root 内写白名单 glob；留空 = root 内全部可写
    deny_paths: []         # 最高优先级黑名单 glob，读写都拒
    readonly_mounts: []    # root 之外的额外只读目录
    writable_mounts: []    # root 之外的额外可写目录
    denied_domains: []     # 在内置 SSRF 基线之外**追加**（不能覆盖基线）
```

例子（给一个"项目可写、文档只读、敏感文件锁死"的工作区）：

```yaml
tools:
  sandbox:
    enabled: true
    root: "."
    write_paths: ["src/**", "tests/**", "docs/**", "*.md"]
    deny_paths: [".env", "**/.git/**", "**/*.pem", "**/secrets/**"]
    readonly_mounts: ["~/notes"]
```

装配（与 `build_shell_policy` 同一模式）：

```python
# harness/config/build.py
def build_sandbox(config: HarnessConfig) -> SandboxPolicy:
    """把 YAML 里的 tools.sandbox 应用到进程级沙箱策略。"""
```

`server/state.py::build_state` 在 `build_shell_policy(config)` 旁边调一次。

**为什么放 `tools.sandbox` 而不是顶层 `workspace`**：沙箱只作用于工具（`fs.*` / `shell.run`），
`tools.policy` / `tools.shell` 已经确立了"安全配置挂在 tools 下"的先例。单消费者就该贴着消费者放。

### 6.1 会话级工作区（控制台选的那个）

上面的 `tools.sandbox` 是**进程级默认**：没选工作区的会话就用它。控制台还可以**按会话**选一个
工作区，存在 `SessionMeta.metadata["workspace"]`（见 [API 服务层设计 §4.1](api-service-design.md)），
由 [`server/runner.py::session_sandbox`](../server/runner.py) 在每轮 run 开始时翻译成
`SandboxPolicy`，塞进 `Context.sandbox`：

| 会话设置 | 生效策略 |
|---|---|
| 没设 `workspace` | `None` → `use_sandbox(None)` no-op → **完全沿用进程级默认**（含 `enabled: false` 的"不设边界"语义） |
| 设了 `workspace` | `SandboxPolicy(enabled=True, root=<workspace>)`，且**继承**全局的 `deny_paths` / 额外挂载点 / 域名黑名单 |

两点刻意的设计：

- **`write_paths` 置空**（工作区内全可写）：写不写得了由**权限档位**的 scopes 决定，
  不该让人在两个地方各配一半。想在工作区内再收窄，仍可用全局 `write_paths`——
  但那样它会对所有会话生效，这是 `deny_paths` 继承、`write_paths` 不继承的原因。
- **`deny_paths` 必须继承**：黑名单（`.env` / `**/*.pem`）是安全基线，
  不能因为"换了个工作区"就失效。

> 工作区快照在 run 开始时取一次；run 进行中改会话设置会被 `409 run_conflict` 挡下，
> 因此不存在"跑到一半边界变了"的情况。

---

## 7. 里程碑

| 里程碑 | 内容 | 交付 |
|---|---|---|
| M1 | `sandbox.py`（`SandboxPolicy` / `SandboxViolation` / `resolve_path` / `configure_sandbox`）+ `ErrorType.SANDBOX_DENIED` + `Executor` 识别 + `fs.*` 六个工具接入与结果过滤 | ✅ tool **+12 条**：关闭时原样透传、root 内读写、相对路径基准、越界读/写、`..` 穿越、写白名单、黑名单优先级、只读/可写挂载、列目录过滤越界条目、越界不重试 |
| M2 | `shell.run` 接入：`cwd` 收敛 + `check_command` 路径 token 扫描 | ✅ tool **+3 条**：cwd 默认落在 root、命令串越界路径被拒、非路径 token 不误报 |
| M3 | `SandboxConfig` 配置节 + `build_sandbox` + `server/state.py` 接线 + 文档同步 | ✅ config **+3 条**：默认关闭、`build_sandbox` 应用策略（写白名单/只读挂载越界）后还原、关闭时不改路径语义 |
| M4 | **会话级工作区**：`ContextVar` 两级作用域（§4.1）+ `Context.sandbox` + `Executor` 接线 + `PATCH /sessions/{id}` 存 metadata + `GET /fs/browse` 目录点选 | ✅ tool **+6 条**（覆盖生效与复位 / `None` no-op / `to_thread` 传递 / 并发隔离 / 越界拦截 / `derive_child` 透传）、server **+5 条**（metadata 校验、档位覆盖请求体、免批准、旧会话回落、`/fs/browse`） |

全量测试：tool 80、skill 61、llm 27、agent 43、session 35、memory 17、config 57、server 55 = **375 条，全部离线**。

另有一次 `build_state` 端到端接线验证（临时配置开沙箱）：工作区外读 → `sandbox_denied`、
工作区内写 → 落盘成功、命中 `deny_paths` 的 `.env` → `sandbox_denied`。

> **后续追加（M4 域名层）**：`net.*` 工具落地时把「域名」这条边界补进本层——
> `DEFAULT_DENIED_DOMAINS`（SSRF 基线，始终生效）+ `extra_denied_domains`（配置只能追加）
> + `check_url()`（仅 http/https）。测试：tool +5 条、config +3 条。
> 详见 [内置工具扩展设计文档](builtin-tools-design.md) §3。

---

## 8. 诚实的限制

| 限制 | 说明 |
|---|---|
| **不是 OS 级沙箱** | 只拦"经工具传入的路径"。子进程（`shell.run`）自己 `open()` 什么，本层管不到 |
| `shell.run` 是白名单 + 扫描 | 变量展开、命令替换、管道、重定向目标都不解析；扫描**fail-closed**，可能误拒 |
| 进程级全局 | 与 [shell 策略](../harness/tool/builtin/shell.py) 现状一致；多会话共享同一份策略。多租户/多副本需迁到 `Context` 或外部策略服务 |
| 大小写 | Windows 路径不区分大小写，但 `Path.resolve()` 不统一大小写；`deny` glob 在 Windows 上按原样大小写匹配。已知边角，暂不处理 |
| 网络 | 只做**域名黑名单**：不防 DNS rebinding（不解析 IP）、不拦 `http://10.0.0.1/` 这类私网 IP 直连（要拦就加进 `denied_domains`）。真正的出网管控属于防火墙 / 代理 |

---

## 9. 兼容性

- **默认 `enabled: false`**：不配沙箱时，`resolve_path()` 原样返回入参，`fs.*` / `shell.run`
  的可观测行为与今天**完全一致**（含 `fs.glob` 返回相对路径这一细节）。
- 新增配置节 `tools.sandbox`，缺省即"关闭"。
- 新增 `ErrorType.SANDBOX_DENIED` 常量：不影响既有错误类型。
- **不新增依赖**：只用标准库（`pathlib` / `fnmatch` / `re`）。

---

## 10. 决策记录

| 议题 | 结论 |
|---|---|
| 默认开关 | **默认关**（opt-in）——打开会改变相对路径基准，不该静默升级 |
| 边界模型 | **单工作区**：一个 `root` 作为唯一边界，读写粒度在 root 内用 glob 细分 |
| root 内默认权限 | **默认可读写**；`write_paths` 非空时收窄为写白名单；`deny_paths` 最高优先级 |
| root 之外 | **默认一律拒**，但允许 `readonly_mounts` / `writable_mounts` 显式挂载 |
| 状态位置 | **进程级全局**（与 `configure_shell_policy` 同款）；多租户演进方向是挂 `Context` |
| 绕过防护 | 判定前 `resolve()` 解 `..` 与符号链接，拿真实路径比较 |
| shell 约束 | `cwd` 收敛 + 命令串路径 token 扫描（fail-closed），并如实标注非 OS 级隔离 |
| 越界错误 | 新增 `sandbox_denied` 错误类型，**不重试**、带 hint 回喂模型自纠 |
| glob/grep 越界结果 | **静默丢弃**，不因个别越界让整次检索失败 |
| 域名 | **默认放行 + SSRF 基线黑名单**；基线**始终生效**（与 `enabled` 无关），配置只能追加 |
| 域名实现位置 | `sandbox.py`（边界层管路径与域名），`net.py` 只管 HTTP 细节与体积上限 |
| 重定向 | 自定义 handler **逐跳复查**，防跳转绕过 |
| 配置位置 | `tools.sandbox`（贴着唯一消费者），不做顶层 `workspace` 节 |

### 仍待确认

- [ ] `root` 之外要不要支持**按工具区分**（如 `fs.read` 可挂只读、`shell.run` 完全不挂）？
- [ ] 是否需要 `--force`/一次性放行机制（模型申请、人批准后临时越界一次）？
- [ ] 多副本部署时策略外置（配置文件 + 文件锁，或独立策略服务）？
