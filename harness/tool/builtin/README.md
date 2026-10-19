# builtin — 内置工具子模块

> 上级模块：[tool/README.md](../README.md)。

## 职责

存放框架自带、开箱即用的工具。导入 `harness.tool` 即触发注册（写入 `default_registry`），
无需手动注册。

## 目录结构

```
builtin/
├─ __init__.py     # 导入即注册；显式列出各域模块
├─ fs.py           # 文件工具（读 / 写 / 改 / 列 / 找 / 搜 / 建目录 / 复制 / 移动 / 删除）
├─ shell.py        # 命令执行（白名单 + 禁止模式 + 审批 + 沙箱 cwd/路径扫描）
├─ net.py          # 网络（抓取 / 下载；域名黑名单 + 体积上限）
├─ code.py         # 代码片段执行（应用层沙箱 + 环境变量 allowlist）
├─ clock.py        # 当前时间
├─ todo.py         # 任务清单
└─ interaction.py  # 向用户提问
```

## 已实现工具

| 工具 | 文件 | side_effect | scopes | 审批 | 幂等 | 说明 |
|---|---|---|---|---|---|---|
| `fs.read` | fs.py | read | fs:read | 否 | 否 | 读文本文件，`max_bytes` 截断 |
| `fs.write` | fs.py | write | fs:write | 否 | 是 | 覆盖写入，自动创建父目录 |
| `fs.edit` | fs.py | write | fs:write | 否 | 否 | 精确替换，`old_string` 必须唯一 |
| `fs.list` | fs.py | read | fs:read | 否 | 否 | 列目录，`depth` 控递归 |
| `fs.glob` | fs.py | read | fs:read | 否 | 否 | 按 glob 模式找文件 |
| `fs.grep` | fs.py | read | fs:read | 否 | 否 | 正则内容检索，返回 file/line/text |
| `fs.mkdir` | fs.py | write | fs:write | 否 | 是 | 创建目录（父目录自动创建） |
| `fs.copy` | fs.py | write | fs:write | 否 | 否 | 复制文件/目录；目标已存在报错 |
| `fs.move` | fs.py | write | fs:write | 否 | 否 | 移动/重命名；两端都过沙箱 |
| `fs.delete` | fs.py | destructive | fs:write | **是** | 否 | 删除文件/目录；目录需 `recursive=True` |
| `shell.run` | shell.py | destructive | shell:run | **是** | 否 | 命令执行，返回 exit/stdout/stderr |
| `net.fetch` | net.py | read | net:read | 否 | 否 | 抓 URL，HTML→纯文本，按上限截断 |
| `net.download` | net.py | write | net:read fs:write | 否 | 否 | 下载到文件；超限中止，原子落位 |
| `code.run` | code.py | destructive | code:run | **是** | 否 | 执行脚本片段；cwd/临时文件在工作区内 |
| `time.now` | clock.py | read | — | 否 | 否 | 返回 ISO 时间（带本地时区） |
| `todo.write` | todo.py | write | — | 否 | 否 | 覆盖式写会话待办清单 |
| `ask_user` | interaction.py | read | — | 否 | 否 | 走 `Context.ask` 回调提问 |

### shell.run 安全策略

三层防护：

1. **白名单** `ALLOWED_COMMANDS`：git / python / pytest / npm / node / go / cargo / echo 等。
2. **禁止模式** `DENY_PATTERNS`：`rm -rf`、`del /f`、`rd /s`、`format`、`mkfs`、`dd if=`、
   `shutdown`、`reboot`、`reg delete`、`taskkill`、fork bomb、`curl|sh`、`wget|sh`、重定向到块设备等。
3. **人工审批**：`requires_approval=True`，需 `Context.approval` 放行。

命令首 token 不在白名单或命中禁止模式，均返回结构化错误（不执行）。

`code.run` 用 `create_subprocess_exec`（**不经 shell**，无注入面）；
解释器（python / node）同样取自上述白名单，且**只**从 allowlist 继承环境变量。

### 沙箱接入

所有碰路径的工具都过 `sandbox.resolve_path()`，`net.*` 过 `sandbox.check_url()`：

| 类别 | 工具 | 约束 |
|---|---|---|
| 读 | `fs.read` / `fs.list` / `fs.glob` / `fs.grep` | `mode="read"`；后三者越界结果静默丢弃 |
| 写 | `fs.write` / `fs.edit` / `fs.mkdir` / `fs.copy` / `fs.move` / `fs.delete` | `mode="write"`；`copy`/`move` 两端都校验 |
| 执行 | `shell.run` | `cwd` 收敛 + 命令串路径 token 扫描 |
| 网络 | `net.fetch` / `net.download` | 域名黑名单（基线始终生效）+ 落盘路径过 `mode="write"` |

沙箱**默认关闭**，关闭时 `resolve_path()` 原样返回入参（零行为变化）；
域名黑名单**不受该开关影响**，始终生效。详见 [docs/sandbox-design.md](../../docs/sandbox-design.md)。

### 会话状态

`todo.write` 的清单按 `Context.session_id` 存于进程内（`_STORE`），未设置时落到 `"default"`。
后续可替换为持久化后端。

## 新增内置工具指引

1. 在对应域的模块（或新建 `net.py` 等）中定义函数，加 `@tool` 装饰器。
2. 必须显式声明：`name`（`namespace.action`）、`description`（说明用途与**副作用**）、
   `side_effect`、`scopes`、必要时 `requires_approval`、`idempotent`、`timeout_s`、`result_max_chars`。
3. 需要会话上下文时，把首个形参标注为 `Context`（框架注入，不暴露给模型）。
4. 失败直接 `raise`（框架会转成结构化 `execution_error`），不要自行吞异常。
5. 若新增模块，记得在 `builtin/__init__.py` 中 import 以触发注册。
6. 在 `tests/` 中补测试，并同步更新本文件与 [tool/README.md](../README.md) §8。

## 规划（P0–P3）

完整规划见 [tool/README.md](../README.md) §8。摘要：

- **P0（已实现）**：上方全部。
- **P1 / P2（已实现）**：`net.fetch`、`net.download`、`fs.mkdir/copy/move/delete`、`time.now`（`clock.py`）、`code.run`（`code.py`）。
- **未实现**：`net.search`（需先定 provider）、`agent.dispatch`、`process.*`。

> 删除类操作走**强制审批**：`fs.delete` 是 `destructive` + `requires_approval`，
> 服务里即"人在 UI 点确认"；批准后才真删。目录需显式 `recursive=True`，且拒绝删除工作区根自身。
>
> `code.run` 是**应用层沙箱**（路径限定 + 解释器白名单 + 子进程环境变量 allowlist），
> **不是 OS 级隔离**——详见 [docs/builtin-tools-design.md](../../docs/builtin-tools-design.md) §6。
