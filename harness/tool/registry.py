"""注册与发现层：ToolRegistry + @tool 装饰器。

负责把「一个普通函数」变成「带 Schema 的受管工具」，并提供按需导出
function calling 定义的能力（控制注入给模型的工具数量）。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, get_type_hints

from pydantic import BaseModel, create_model

from .schema import Context, ToolNotFound, ToolSpec

if TYPE_CHECKING:
    from .adapter.base import ToolAdapter


@dataclass
class RegisteredTool:
    """注册表中的条目：本地工具持有 fn + 入参模型；外部工具持有 adapter。"""

    spec: ToolSpec
    fn: Callable[..., Any] | None = None
    input_model: type[BaseModel] | None = None
    is_async: bool = False
    # 需要由 Executor 注入的 Context 形参名（无则为 None）。
    ctx_param: str | None = None
    # 外部工具适配器（MCP / 进程等），与 fn 二选一。
    adapter: "ToolAdapter | None" = None


def _resolve_annotations(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(fn)
    except Exception:
        # 注解无法解析时退回原始签名，避免注册阶段直接失败。
        return {}


def _is_context_param(name: str, annotation: Any) -> bool:
    """判定该形参是否为框架注入的 Context（类型标注为 Context，或名为 ctx）。"""
    if isinstance(annotation, type) and issubclass(annotation, Context):
        return True
    return name == "ctx" and annotation is inspect.Parameter.empty


def build_input_model(
    tool_name: str, fn: Callable[..., Any]
) -> tuple[type[BaseModel], str | None]:
    """由函数签名生成入参模型；返回 (模型, 需注入的 Context 形参名)。

    Context 形参不进入模型、不作为工具参数暴露给模型，由 Executor 运行时注入。
    """
    sig = inspect.signature(fn)
    hints = _resolve_annotations(fn)
    fields: dict[str, tuple[Any, Any]] = {}
    ctx_param: str | None = None
    for pname, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise TypeError(f"工具 {tool_name} 不支持可变参数 *args/**kwargs: {pname}")
        ann = hints.get(pname, param.annotation)
        if _is_context_param(pname, ann):
            ctx_param = pname
            continue
        if ann is inspect.Parameter.empty:
            ann = Any
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[pname] = (ann, default)

    parts = [p.capitalize() for p in re.split(r"[^0-9a-zA-Z]+", tool_name) if p]
    model_name = ("".join(parts) or "Tool") + "Input"
    return create_model(model_name, **fields), ctx_param


class ToolRegistry:
    """工具注册表。同名注册直接报错（fail-fast），不静默覆盖。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, fn: Callable[..., Any]) -> RegisteredTool:
        if spec.name in self._tools:
            raise ValueError(f"工具名冲突: {spec.name} 已注册")
        input_model, ctx_param = build_input_model(spec.name, fn)
        if not spec.input_schema:
            spec = replace(spec, input_schema=input_model.model_json_schema())
        tool = RegisteredTool(
            spec=spec,
            fn=fn,
            input_model=input_model,
            is_async=inspect.iscoroutinefunction(fn),
            ctx_param=ctx_param,
        )
        self._tools[spec.name] = tool
        return tool

    def register_external(self, spec: ToolSpec, adapter: "ToolAdapter") -> RegisteredTool:
        """注册外部工具（MCP / 进程等）：无 Python 签名，直接以 spec.input_schema 为准。"""
        if spec.name in self._tools:
            raise ValueError(f"工具名冲突: {spec.name} 已注册")
        if not spec.input_schema:
            raise ValueError(f"外部工具 {spec.name} 必须提供 input_schema")
        tool = RegisteredTool(spec=spec, adapter=adapter, is_async=True)
        self._tools[spec.name] = tool
        return tool

    def register_tool(self, registered: RegisteredTool) -> RegisteredTool:
        """复用已有 `RegisteredTool`（spec + fn + 入参模型）注册，不重建任何东西。

        用途：组装期裁剪工具集（配置里的工具开关）——把 `default_registry` 里选中的条目
        搬进一个新的独立 registry，从而被禁用的工具根本不进表。
        """
        if registered.spec.name in self._tools:
            raise ValueError(f"工具名冲突: {registered.spec.name} 已注册")
        self._tools[registered.spec.name] = registered
        return registered

    def get(self, name: str) -> RegisteredTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFound(name)
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list(
        self,
        *,
        names: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        scopes: Iterable[str] | None = None,
    ) -> list[ToolSpec]:
        """按名称/标签/权限域筛选工具声明。"""
        name_set = set(names) if names is not None else None
        tag_set = set(tags) if tags is not None else None
        scope_set = set(scopes) if scopes is not None else None
        out: list[ToolSpec] = []
        for tool in self._tools.values():
            spec = tool.spec
            if name_set is not None and spec.name not in name_set:
                continue
            if tag_set is not None and not tag_set.intersection(spec.tags):
                continue
            if scope_set is not None and not scope_set.intersection(spec.scopes):
                continue
            out.append(spec)
        return out

    def to_openai_tools(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """导出 OpenAI function calling 的 tools 定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
            for spec in self.list(names=names)
        ]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


# 全局默认注册表：内置工具默认注册到这里。
default_registry = ToolRegistry()


def tool(
    name: str,
    description: str,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
    max_retries: int = 0,
    idempotent: bool = False,
    concurrency: str = "parallel",
    side_effect: str = "read",
    requires_approval: bool = False,
    scopes: Iterable[str] | None = None,
    result_max_chars: int = 8000,
    tags: Iterable[str] | None = None,
    registry: ToolRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数注册为工具。函数签名 + 类型注解会自动生成入参 Schema。"""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolSpec(
            name=name,
            description=description,
            input_schema=dict(input_schema or {}),
            output_schema=output_schema,
            timeout_s=timeout_s,
            max_retries=max_retries,
            idempotent=idempotent,
            concurrency=concurrency,  # type: ignore[arg-type]
            side_effect=side_effect,  # type: ignore[arg-type]
            requires_approval=requires_approval,
            scopes=tuple(scopes or ()),
            result_max_chars=result_max_chars,
            tags=tuple(tags or ()),
        )
        target = registry if registry is not None else default_registry
        registered = target.register(spec, fn)
        fn.__tool__ = registered  # type: ignore[attr-defined]
        return fn

    return decorator
