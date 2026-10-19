# test — 工具系统测试子模块

> 上级模块：[tool/README.md](../README.md)。

## 运行

```bash
# 在项目根目录（agent/）执行
python -m harness.tool.test.test_tool_system
# 或
pytest harness/tool/test
```

测试纯标准库 + pytest 兼容，既可 `python -m` 直接跑，也可用 pytest 收集。
共享 fixture（进程级全局状态隔离 `isolate_process_globals`、临时工作区 `tmp_workspace`）
见仓库根 [conftest.py](../../../conftest.py)。

## 目录结构

```
test/
├─ __init__.py
├─ test_tool_system.py     # 全量冒烟测试
└─ mcp_echo_server.py      # 测试夹具：最小 MCP server（stdio）
```

## test_tool_system.py 覆盖范围

| 分组 | 覆盖点 |
|---|---|
| 注册与 schema | 工具名导出、`required` 字段、按标签筛选 |
| 执行管线 | 正常读取、未知工具、参数缺失、权限不足、工具内部异常 |
| 结果处理 | 超长结果截断 |
| 重试 | 幂等工具重试成功、非幂等不重试 |
| 审批 | 缺回调 → `approval_required`、批准通过、拒绝 → `permission_denied` |
| 批量调用 | 一轮多 tool_calls 并发执行并按 `tool_call_id` 归位 |
| 文件工具 | `fs.edit` 唯一匹配、`fs.list` / `fs.glob` / `fs.grep` |
| shell | 白名单拦截、禁止模式拦截、需审批、正常 `echo` |
| 会话工具 | `todo.write` 正常/非法 status、`ask_user` 有/无回调、`ctx` 不暴露给模型 |
| 外部工具 | `register_external` 调用与 schema 校验、缺 `input_schema` 报错 |
| MCP 端到端 | stdio 子进程拉起 `mcp_echo_server.py`，发现 + 调用 + 默认权限；`tool_overrides` 加严 |
| 沙箱（路径） | 关闭时原样透传、root 内读写、相对路径基准、越界读/写、`..` 穿越、写白名单、黑名单优先级、只读/可写挂载、列目录过滤、越界不重试、shell cwd 与命令串扫描 |
| 沙箱（域名） | SSRF 基线（7 个目标）、正常域名、非 http/https、追加不覆盖基线、越界回 `sandbox_denied` |
| `net.*` | 假响应下 HTML 剥标签、按上限截断、原子落盘、超限中止不留半成品、落盘受沙箱约束 |
| `fs.*` 增删改 | mkdir/copy/move/delete 串联、拒绝覆盖已存在目标、move 不能越界、delete 需审批 / 目录需 `recursive` / 拒绝删根 |
| `code.run` | 跑通 python、cwd 在工作区临时目录、**密钥环境变量不外泄**、临时目录必清、语言与解释器白名单校验 |

> 测试**全离线**：`net.*` 用假响应替换 `net._open`（域名校验单独在 `check_url` 层测），
> 不发起真实网络请求。

## 夹具说明

`mcp_echo_server.py` 是一个最小 MCP server（基于 mcp SDK 的 `MCPServer`，stdio 传输），
暴露一个 `echo` 工具。测试以子进程方式拉起它（`command=sys.executable`），
验证「连接 → tools/list → tools/call → 结果映射」的完整链路。

> 该测试会启动子进程，依赖已安装 `mcp` SDK；未安装时会失败。

## 约定

- 每个测试独立建 `ToolRegistry`，避免相互污染；需要内置工具时用 `default_registry`。
- 同步测试内部用 `asyncio.run` 驱动异步接口（`_run` 辅助函数）。
- 新增工具时，在对应分组补测试，并同步更新 [builtin/README.md](../builtin/README.md) 与
  [tool/README.md](../README.md)。
