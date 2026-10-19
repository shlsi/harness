"""执行层：统一的工具调用管线。

    lookup → validate → authorize → run(超时/重试) → normalize → trace → return

失败不抛异常，统一返回带结构化错误的 ToolResult，便于回喂模型自纠。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Iterable

from pydantic import ValidationError
from jsonschema.validators import validator_for

from .normalizer import normalize
from .policy import Policy
from .registry import RegisteredTool, ToolRegistry
from .sandbox import SandboxViolation, use_sandbox
from .schema import (
    ApprovalRequired,
    Context,
    ErrorInfo,
    ErrorType,
    PermissionDenied,
    ToolNotFound,
    ToolResult,
)
from .telemetry import Tracer


def _schema_errors(schema: dict[str, Any], args: Any) -> list[dict[str, str]]:
    """用工具声明的 JSON Schema 校验参数（用于外部工具），返回字段级错误。"""
    validator_cls = validator_for(schema)
    errors: list[dict[str, str]] = []
    for err in sorted(validator_cls(schema).iter_errors(args), key=lambda e: list(e.absolute_path)):
        if err.validator == "required" and isinstance(err.instance, dict):
            # required 校验报在根路径，需拆出具体缺失字段
            for name in err.validator_value:
                if name not in err.instance:
                    errors.append({"field": str(name), "msg": f"缺少必填字段: {name}"})
            continue
        field = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append({"field": field, "msg": err.message})
    return errors


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: Policy | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or Policy()
        self.tracer = tracer or Tracer()

    # ---- 单次调用 ----
    async def call(
        self,
        tool_name: str,
        raw_args: dict[str, Any] | None,
        ctx: Context,
        *,
        tool_call_id: str | None = None,
    ) -> ToolResult:
        raw_args = raw_args if raw_args is not None else {}
        digest = Tracer.digest(raw_args)
        started = time.perf_counter()

        # 1. lookup
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFound as exc:
            return self._emit(
                ctx,
                tool_name,
                tool_call_id,
                started,
                digest,
                ToolResult.failure(
                    ErrorInfo(
                        ErrorType.TOOL_NOT_FOUND,
                        str(exc),
                        hint=f"可用工具: {', '.join(self.registry.names())}",
                    )
                ),
            )

        # 2. validate
        if not isinstance(raw_args, dict):
            return self._emit(
                ctx,
                tool_name,
                tool_call_id,
                started,
                digest,
                ToolResult.failure(
                    ErrorInfo(
                        ErrorType.INVALID_ARGS,
                        "arguments 必须是 JSON 对象",
                        hint="请提供形如 {\"key\": value} 的参数对象",
                    )
                ),
            )
        if tool.input_model is not None:
            try:
                validated = tool.input_model(**raw_args)
            except ValidationError as exc:
                fields = [
                    {"field": ".".join(str(x) for x in err["loc"]) or "(root)", "msg": err["msg"]}
                    for err in exc.errors()
                ]
                return self._emit(
                    ctx, tool_name, tool_call_id, started, digest,
                    ToolResult.failure(
                        ErrorInfo(
                            ErrorType.INVALID_ARGS,
                            "参数校验失败",
                            hint="请修正 details.fields 中列出的字段后重试",
                            details={"fields": fields},
                        )
                    ),
                )
            call_args = {
                name: getattr(validated, name) for name in tool.input_model.model_fields
            }
        else:
            # 外部工具：按声明的 JSON Schema 校验
            fields = _schema_errors(tool.spec.input_schema, raw_args)
            if fields:
                return self._emit(
                    ctx, tool_name, tool_call_id, started, digest,
                    ToolResult.failure(
                        ErrorInfo(
                            ErrorType.INVALID_ARGS,
                            "参数校验失败",
                            hint="请修正 details.fields 中列出的字段后重试",
                            details={"fields": fields},
                        )
                    ),
                )
            call_args = dict(raw_args)

        # 3. authorize（审批请求只暴露模型可见参数，不含注入的 ctx）
        try:
            await self.policy.authorize(tool, call_args, ctx)
        except (PermissionDenied, ApprovalRequired) as exc:
            etype = (
                ErrorType.APPROVAL_REQUIRED
                if isinstance(exc, ApprovalRequired)
                else ErrorType.PERMISSION_DENIED
            )
            return self._emit(
                ctx,
                tool_name,
                tool_call_id,
                started,
                digest,
                ToolResult.failure(
                    ErrorInfo(
                        etype,
                        str(exc),
                        hint="需要补充权限域或提供审批回调",
                        details=getattr(exc, "details", {}),
                    )
                ),
            )

        # 4~6. run（超时 + 幂等重试）
        attempts = tool.spec.max_retries + 1 if tool.spec.idempotent else 1
        last_error: ErrorInfo | None = None
        for attempt in range(1, attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    self._run_tool(tool, call_args, ctx), timeout=tool.spec.timeout_s
                )
                content = normalize(raw, tool.spec)
                return self._emit(
                    ctx,
                    tool_name,
                    tool_call_id,
                    started,
                    digest,
                    ToolResult.success(content, raw=raw, attempts=attempt),
                )
            except asyncio.TimeoutError:
                last_error = ErrorInfo(
                    ErrorType.TIMEOUT,
                    f"工具执行超时(> {tool.spec.timeout_s}s)",
                    hint="请缩小输入范围或调大 timeout_s",
                    retryable=tool.spec.idempotent,
                )
            except SandboxViolation as exc:
                # 越界是确定性失败：不重试，直接回喂模型自纠
                return self._emit(
                    ctx,
                    tool_name,
                    tool_call_id,
                    started,
                    digest,
                    ToolResult.failure(
                        ErrorInfo(
                            ErrorType.SANDBOX_DENIED,
                            str(exc),
                            hint="请改用工作区内的路径；确需访问请调整 tools.sandbox 配置",
                            retryable=False,
                        )
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 工具异常统一转结构化错误
                last_error = ErrorInfo(
                    ErrorType.EXECUTION_ERROR,
                    f"{type(exc).__name__}: {exc}",
                    hint="请检查参数或工具实现",
                    retryable=tool.spec.idempotent,
                    details={"exception": type(exc).__name__},
                )
            if attempt < attempts:
                await asyncio.sleep(min(0.2 * (2 ** (attempt - 1)), 5.0))

        assert last_error is not None
        return self._emit(
            ctx, tool_name, tool_call_id, started, digest, ToolResult.failure(last_error)
        )

    @staticmethod
    async def _run_tool(tool: RegisteredTool, call_args: dict[str, Any], ctx: Context) -> Any:
        # 本次调用的沙箱策略在此生效（`ctx.sandbox` 为 None 时 no-op，沿用进程级默认）。
        # 放在这里而不是 `call()` 外层，是因为只有真正执行工具时才需要覆盖；
        # `asyncio.to_thread` 会 `copy_context()`，因此同步工具（fs.* / net.*）也能看到。
        with use_sandbox(ctx.sandbox):
            # 外部工具：交给适配器
            if tool.adapter is not None:
                return await tool.adapter.invoke(tool.spec, call_args, ctx)

            kwargs = dict(call_args)
            if tool.ctx_param is not None:
                kwargs[tool.ctx_param] = ctx
            if tool.is_async:
                return await tool.fn(**kwargs)
            # 同步工具放到线程池，避免阻塞事件循环。
            return await asyncio.to_thread(tool.fn, **kwargs)

    # ---- 批量调用（对接模型一轮返回的多个 tool_calls）----
    async def execute_tool_calls(
        self, tool_calls: Iterable[dict[str, Any]], ctx: Context
    ) -> list[dict[str, Any]]:
        """并发执行一轮中的所有 tool_calls，并按 tool_call_id 归位结果。

        返回可直接追加到消息列表的 tool 消息，顺序与入参一致。
        """
        parsed = [self._parse_call(tc) for tc in tool_calls]
        results = await asyncio.gather(
            *(
                self.call(name, args, ctx, tool_call_id=cid)
                for cid, name, args in parsed
            )
        )
        return [
            {"role": "tool", "tool_call_id": cid, "content": result.to_model_content()}
            for (cid, _, _), result in zip(parsed, results)
        ]

    @staticmethod
    def _parse_call(tc: dict[str, Any]) -> tuple[str, str, Any]:
        cid = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name") or ""
        raw = fn.get("arguments", tc.get("arguments", {}))
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                raw = {}
            else:
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    raw = text  # 保留原串，交给 call 报 invalid_args
        return cid, name, raw

    # ---- 内部：记录 span 并回填 meta ----
    def _emit(
        self,
        ctx: Context,
        tool_name: str,
        tool_call_id: str | None,
        started: float,
        digest: str,
        result: ToolResult,
    ) -> ToolResult:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        result.meta.setdefault("trace_id", ctx.trace_id)
        result.meta.setdefault("tool", tool_name)
        result.meta.setdefault("duration_ms", duration_ms)
        if tool_call_id:
            result.meta.setdefault("tool_call_id", tool_call_id)
        self.tracer.record(
            trace_id=ctx.trace_id,
            tool=tool_name,
            tool_call_id=tool_call_id,
            ok=result.ok,
            duration_ms=duration_ms,
            args_digest=digest,
            error_type=result.error.type if result.error else None,
            result_chars=len(result.content) if isinstance(result.content, str) else 0,
        )
        return result
