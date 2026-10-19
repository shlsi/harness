"""安全与权限层：scopes 校验 + 危险操作审批。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .schema import ApprovalRequest, ApprovalRequired, Context, PermissionDenied

if TYPE_CHECKING:
    from .registry import RegisteredTool


class Policy:
    """统一的授权策略。

    - enforce_scopes：校验工具声明的 scopes 是否被会话授予。
    - approval_for_destructive：destructive 操作强制走人工确认。
    """

    def __init__(
        self,
        *,
        enforce_scopes: bool = True,
        approval_for_destructive: bool = True,
    ) -> None:
        self.enforce_scopes = enforce_scopes
        self.approval_for_destructive = approval_for_destructive

    async def authorize(
        self, tool: "RegisteredTool", args: dict[str, Any], ctx: Context
    ) -> None:
        spec = tool.spec

        if self.enforce_scopes and spec.scopes:
            missing = [s for s in spec.scopes if s not in ctx.scopes]
            if missing:
                raise PermissionDenied(
                    f"缺少权限域: {', '.join(missing)}",
                    details={"required": list(spec.scopes), "granted": sorted(ctx.scopes)},
                )

        needs_approval = spec.requires_approval or (
            self.approval_for_destructive and spec.side_effect == "destructive"
        )
        if needs_approval:
            if ctx.approval is None:
                raise ApprovalRequired(spec.name)
            request = ApprovalRequest(
                tool=spec.name,
                args=args,
                reason=f"工具 {spec.name} 属于 {spec.side_effect} 操作，需要确认",
            )
            granted = await ctx.approval(request)
            if not granted:
                raise PermissionDenied(
                    f"工具 {spec.name} 的调用被拒绝",
                    details={"reason": "approval_denied"},
                )
