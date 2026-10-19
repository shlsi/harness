# adapter — 外部工具适配器子模块

> 上级模块：[tool/README.md](../README.md)。

## 职责

把「非本地 Python 函数」的工具（MCP 服务、独立进程等）接入统一的注册与执行管线。
本地函数走 `@tool` 装饰器；外部工具走本模块的适配器 + `ToolRegistry.register_external()`。

## 目录结构

```
adapter/
├─ __init__.py     # 只导出与 mcp SDK 无关的基础类型，保证未装 mcp 时可用
├─ base.py         # ToolAdapter 协议
└─ mcp.py          # MCP 适配器实现
```

## base.py — 统一适配协议

```python
class ToolAdapter(Protocol):
    def describe(self) -> list[ToolSpec]: ...                              # 声明该适配器提供的工具
    async def invoke(self, spec: ToolSpec, args: dict, ctx: Context) -> Any: ...  # 实际调用
```

约束：
- 一个适配器实例绑定**一个**工具，`invoke` 内的异常由 Executor 统一转成结构化错误。
- 返回值走统一的 `normalizer`（截断、序列化），无需自行处理。

## mcp.py — MCP 适配器

| 名称 | 说明 |
|---|---|
| `MCPServerConfig` | server 连接与权限配置（传输、命令/URL、前缀、默认权限、按工具覆盖） |
| `MCPClient` | 维护长连接：握手 → `tools/list`（含分页）→ `call_tool`；`aclose()` 释放 |
| `MCPToolAdapter` | 单个 MCP 工具的适配器，`invoke` 转调 `MCPClient.call_tool` |
| `build_tool_specs` | 把 MCP 工具描述转成 `(ToolSpec, MCPToolAdapter)` 列表 |

要点：
- 传输：`stdio`（子进程）/ `http`（streamable-http）。
- 命名：注册名 = `{前缀}.{原始工具名}`，前缀默认取 `MCPServerConfig.name`。
- 结果映射：`content` 中的 text / image / resource 逐块转文本；`is_error=True` → 抛异常 → `execution_error`。
- 权限：**默认宽松**（`read`、免审批），可用 `default_*` / `tool_overrides` 加严。

### 依赖

官方 `mcp` SDK（`pip install mcp`），当前验证版本为 **mcp 2.x**。

> ⚠️ mcp 2.x 与 1.x API 差异较大：`FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`）、
> HTTP 客户端为 `streamable_http_client`、`list_tools` / `call_tool` 均为 async。

`adapter/__init__.py` 不导入 mcp，因此未安装 mcp 时工具系统仍可正常使用。

## 接入方式

`MCPServerConfig` 的加载由上级 [loader.py](../loader.py) 的 `load_mcp_servers()` 完成：
逐个连接、发现工具、注册进 registry，任一失败则回滚并关闭已建连接。

## 新增适配器指引（如 ProcessTool）

1. 在 `adapter/` 下新建模块，实现 `describe()` + `invoke()`。
2. 用 `registry.register_external(spec, adapter)` 注册；`spec.input_schema` 必填（用于校验）。
3. 在 `spec` 中声明 `side_effect` / `scopes` / `requires_approval`（外部协议通常不自带，需显式给）。
4. 若需要连接/资源生命周期，参照 `load_mcp_servers` 提供配套 loader 与 `aclose()`。
