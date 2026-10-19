"""请求体的 Pydantic 模型。

响应统一用 dict 组装（形状见 docs/api-service-design.md §4），避免维护两套同构模型。
"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    title: str | None = None
    system: list[dict[str, Any]] | None = None  # Message[]，缺省由 PromptBuilder 注入
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnRequest(BaseModel):
    input: Union[str, list[dict[str, Any]]] = Field(
        description="用户输入：纯文本，或 OpenAI 多模态 content parts"
    )
    images: list[str] = Field(default_factory=list, description="图片 URL 或 data URL")
    stream: bool = False
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractionRequest(BaseModel):
    approved: bool | None = None
    answer: str | None = None


class UpdateSessionRequest(BaseModel):
    """PATCH 会话：改名与/或合并元数据（两者都可不传，但至少要传一个）。

    `metadata` 是**合并**语义，控制台用它按会话保存 `workspace`（工作区目录）与
    `permission`（三档权限，见 `harness/tool/permission.py`）。
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    metadata: dict[str, Any] | None = None


class CompactSessionRequest(BaseModel):
    force: bool = False
