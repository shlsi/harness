"""技能资源：列表 / 详情 / 新建 / 编辑 / 删除 / 校验 / 重载 / zip 上传 / 启用开关。

写操作全部限制在 `skills.root` 之内（见 `harness/skill/writer.py` 与 `harness/skill/pack.py`）。
技能未启用（`skills.root` 为空）时，写操作返回 409 `skills_disabled`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from harness.skill import PackError, SkillMeta, SkillNotFound, SkillRegistry
from harness.skill.lint import LintIssue, lint_skills
from harness.skill.pack import InstallLimits, install_zip
from harness.skill.writer import (
    FRONTMATTER_TEMPLATE_BODY,
    delete_skill,
    lint_draft,
    read_skill,
    write_skill,
)

from ..deps import StateDep, require_config
from ..errors import ServiceError, not_found

router = APIRouter(prefix="/skills", tags=["skill"])

MAX_UPLOAD_BYTES = InstallLimits().max_zip_bytes

# zip 安装的稳定 code → HTTP 状态（harness 只给码，状态由宿主定）
_PACK_STATUS = {
    "zip_invalid": 400,
    "zip_too_large": 413,
    "zip_unsafe_entry": 422,
    "zip_ambiguous": 422,
    "zip_no_skill": 422,
    "skill_invalid": 422,
    "skill_conflict": 409,
}


class SkillCreateRequest(BaseModel):
    name: str
    description: str
    body: str = FRONTMATTER_TEMPLATE_BODY
    allowed_tools: list[str] = Field(default_factory=list)
    version: str | None = None


class SkillUpdateRequest(BaseModel):
    description: str
    body: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    version: str | None = None


class SkillValidateRequest(BaseModel):
    name: str = "draft"
    description: str
    body: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    version: str | None = None


class SkillToggleRequest(BaseModel):
    enabled: bool


def _meta_dict(
    meta: SkillMeta, *, body: str | None = None, enabled: bool | None = None
) -> dict:
    data: dict[str, Any] = {
        "name": meta.name,
        "description": meta.description,
        "version": meta.version,
        "allowed_tools": list(meta.allowed_tools),
        "path": str(meta.path),
    }
    if body is not None:
        data["body"] = body
    if enabled is not None:
        data["enabled"] = enabled
    return data


def _issue_dict(issue: LintIssue) -> dict:
    return {"skill": issue.skill, "level": issue.level, "code": issue.code, "message": issue.message}


def _registry(state: StateDep) -> SkillRegistry:
    if state.skills is None:
        raise ServiceError(
            "skills_disabled", "技能功能未启用（skills.root 为空）；请先在配置里设置 skills.root"
        )
    return state.skills


def _this_skill_issues(state: StateDep, name: str) -> list[dict]:
    known = state.registry.names()
    issues = lint_skills(state.skills.root, known_tools=known)
    return [_issue_dict(i) for i in issues if i.skill == name]


@router.get("")
async def list_skills(state: StateDep) -> list[dict]:
    if state.skills is None:
        return []
    registry = state.skills
    return [
        _meta_dict(meta, enabled=registry.is_enabled(meta.name))
        for meta in registry.list_meta(include_disabled=True)
    ]


@router.get("/lint")
async def lint_all(state: StateDep) -> dict:
    """对整个技能目录跑 lint（含「根目录不存在」这类整体问题）。"""
    root = state.skills.root if state.skills is not None else ""
    if state.skills is None:
        return {"issues": [], "enabled": False}
    issues = lint_skills(root, known_tools=state.registry.names())
    return {"issues": [_issue_dict(i) for i in issues], "enabled": True}


@router.post("/validate")
async def validate_draft(body: SkillValidateRequest, state: StateDep) -> dict:
    """对**尚未落盘**的草稿跑 lint（前端「保存前检查」用）。"""
    issues = lint_draft(
        name=body.name,
        description=body.description,
        body=body.body,
        allowed_tools=body.allowed_tools,
        version=body.version,
        known_tools=state.registry.names(),
    )
    return {"issues": [_issue_dict(i) for i in issues]}


@router.post("/reload")
async def reload_skills(state: StateDep) -> dict:
    """重新扫描技能目录。

    **不需要换会话**：skill 目录是每轮 run 现读的（`PromptBuilder.system_prompt()`），
    所以重载后**下一轮对话**就用上了新目录，已存在的会话同样生效。
    """
    registry = _registry(state)
    registry.reload()
    return {
        "skills": [
            _meta_dict(meta, enabled=registry.is_enabled(meta.name))
            for meta in registry.list_meta(include_disabled=True)
        ],
        "warnings": registry.warnings,
    }


@router.post("/upload", status_code=201)
async def upload_skills(request: Request, state: StateDep, overwrite: bool = False) -> dict:
    """上传一个 zip 包并安装其中的技能。

    请求体是 **raw zip 字节**（不是 multipart）——这样不必引入 `python-multipart` 依赖，
    前端直接 `fetch('/skills/upload?overwrite=true', {method:'POST', body: file})`。

    端点必须声明在 `GET /{name}` 之前：否则 `upload` 会被当成技能名。
    """
    registry = _registry(state)

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise ServiceError(
            "zip_too_large",
            f"上传体超过上限 {MAX_UPLOAD_BYTES} 字节",
            status=413,
            details={"limit": "zip_bytes", "max": MAX_UPLOAD_BYTES, "actual": int(declared)},
        )

    data = await request.body()
    try:
        report = install_zip(
            registry.root,
            data,
            overwrite=overwrite,
            known_tools=state.registry.names(),
        )
    except PackError as exc:
        raise ServiceError(
            exc.code, exc.message, status=_PACK_STATUS.get(exc.code, 400), details=exc.details
        ) from exc

    registry.reload()
    return {
        "installed": [
            {"name": item.name, "files": item.files, "overwritten": item.overwritten}
            for item in report.installed
        ],
        "skipped": report.skipped,
        "warnings": report.warnings,
        "skills": [
            _meta_dict(meta, enabled=registry.is_enabled(meta.name))
            for meta in registry.list_meta(include_disabled=True)
        ],
    }


@router.post("", status_code=201)
async def create_skill(body: SkillCreateRequest, state: StateDep) -> dict:
    registry = _registry(state)
    try:
        write_skill(
            registry.root,
            name=body.name,
            description=body.description,
            body=body.body,
            allowed_tools=body.allowed_tools,
            version=body.version,
        )
    except SkillNotFound as exc:  # pragma: no cover - 新建不会走到
        raise not_found("skill_not_found", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 校验/文件错误统一转 400
        raise ServiceError("skill_invalid", str(exc), status=400) from exc
    registry.reload()
    meta = registry.get(body.name)
    return {
        "skill": _meta_dict(
            meta, body=body.body, enabled=registry.is_enabled(body.name)
        ),
        "issues": _this_skill_issues(state, body.name),
    }


@router.get("/{name}")
async def get_skill(name: str, state: StateDep) -> dict:
    registry = _registry(state)
    try:
        meta, body = read_skill(registry.root, name)
    except SkillNotFound as exc:
        raise not_found("skill_not_found", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("skill_invalid", str(exc), status=400) from exc
    return _meta_dict(meta, body=body, enabled=registry.is_enabled(name))


@router.put("/{name}")
async def update_skill(name: str, body: SkillUpdateRequest, state: StateDep) -> dict:
    registry = _registry(state)
    try:
        write_skill(
            registry.root,
            name=name,
            description=body.description,
            body=body.body,
            allowed_tools=body.allowed_tools,
            version=body.version,
            overwrite=True,
        )
    except SkillNotFound as exc:
        raise not_found("skill_not_found", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("skill_invalid", str(exc), status=400) from exc
    registry.reload()
    meta = registry.get(name)
    return {
        "skill": _meta_dict(meta, body=body.body, enabled=registry.is_enabled(name)),
        "issues": _this_skill_issues(state, name),
    }


@router.patch("/{name}")
async def patch_skill(name: str, body: SkillToggleRequest, state: StateDep) -> dict:
    """启用 / 停用一个技能：只翻 `skills.disabled` 一个字段并热切换过滤面。

    **不重建任何部件**，所以不要求「无活跃 run」——运行中也能一键开关。
    """
    registry = _registry(state)
    require_config(state)
    if name not in registry.all_names():
        raise not_found("skill_not_found", f"skill 不存在: {name}")
    await state.set_skill_enabled(name, body.enabled)
    return {
        "skill": _meta_dict(registry.get(name), enabled=registry.is_enabled(name)),
        "disabled": registry.disabled_names(),
    }


@router.delete("/{name}", status_code=204)
async def delete_skill_route(name: str, state: StateDep) -> Response:
    registry = _registry(state)
    try:
        delete_skill(registry.root, name)
    except SkillNotFound as exc:
        raise not_found("skill_not_found", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ServiceError("skill_invalid", str(exc), status=400) from exc
    registry.reload()
    return Response(status_code=204)
