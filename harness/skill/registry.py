"""技能注册表：扫描目录、生成目录（catalog）、按需加载正文与资源。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .parser import load_meta, parse_skill_md
from .schema import Skill, SkillDisabled, SkillError, SkillMeta, SkillNotFound, SkillParseError

ENTRY_NAME = "SKILL.md"


def _clip(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


class SkillRegistry:
    """管理单一固定目录下的所有 skill。

    只加载 frontmatter（name/description）用于生成 catalog；
    正文（SKILL.md body）与资源文件按需读取并缓存。

    **停用（disabled）只过滤"曝光面"**：`scan()` 照旧全扫，所以管理端点与前端面板
    仍能看到停用项（否则"重新启用"无从下手）；`names()` / `catalog()` / `load()` /
    `read_resource()` 才把停用项排除在外。`get()` 不过滤——保存后取元信息还要用。
    """

    def __init__(
        self,
        root: Path | str,
        *,
        disabled: Iterable[str] = (),
        max_desc_chars: int = 300,
        max_body_chars: int = 60_000,
        max_resource_chars: int = 100_000,
    ) -> None:
        self.root = Path(root)
        self.max_desc_chars = max_desc_chars
        self.max_body_chars = max_body_chars
        self.max_resource_chars = max_resource_chars
        self._skills: dict[str, SkillMeta] = {}
        self._bodies: dict[str, str] = {}
        self._warnings: list[str] = []
        self._disabled: set[str] = {str(name) for name in disabled}
        self.scan()

    # ---- 扫描与查询 ----
    def scan(self) -> list[SkillMeta]:
        """扫描根目录，解析所有 skill。单个损坏只记 warning，不影响整体。"""
        self._skills.clear()
        self._bodies.clear()
        self._warnings.clear()

        if not self.root.is_dir():
            self._warnings.append(f"skill 根目录不存在: {self.root}")
            return []

        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            entry = child / ENTRY_NAME
            if not entry.is_file():
                self._warnings.append(f"{child.name}: 缺少 {ENTRY_NAME}，已跳过")
                continue
            try:
                meta = load_meta(child, entry)
            except SkillParseError as exc:
                self._warnings.append(f"{child.name}: {exc}")
                continue
            if meta.name in self._skills:
                self._warnings.append(f"{child.name}: skill 名重复 {meta.name}，已跳过")
                continue
            self._skills[meta.name] = meta
        return list(self._skills.values())

    def reload(self) -> list[SkillMeta]:
        """重新扫描（开发期热重载）。"""
        return self.scan()

    def get(self, name: str) -> SkillMeta:
        """取元信息。**不过滤停用**：管理端点在「保存 / 开关」后还要用它。"""
        meta = self._skills.get(name)
        if meta is None:
            raise SkillNotFound(name)
        return meta

    def names(self) -> list[str]:
        """**已启用**的技能名（catalog / 工具可见面）。"""
        return sorted(name for name in self._skills if name not in self._disabled)

    def all_names(self) -> list[str]:
        """全部技能名（含停用；管理端点与 healthz 用）。"""
        return sorted(self._skills)

    def is_enabled(self, name: str) -> bool:
        return name in self._skills and name not in self._disabled

    def disabled_names(self) -> list[str]:
        """停用名单（只报实际存在的，避免暴露悬空名字）。"""
        return sorted(name for name in self._disabled if name in self._skills)

    def set_disabled(self, names: Iterable[str]) -> None:
        """热切换停用名单（只改集合；无缓存可清）。"""
        self._disabled = {str(name) for name in names}

    def list_meta(self, *, include_disabled: bool = False) -> list[SkillMeta]:
        targets = self.all_names() if include_disabled else self.names()
        return [self._skills[name] for name in targets]

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    # ---- 目录与加载 ----
    def catalog(self, names: Iterable[str] | None = None) -> str:
        """生成注入 system prompt 的目录（仅 name + description）。

        显式传入的 name 同样过滤停用项，防止绕过开关。
        """
        targets = list(names) if names is not None else self.names()
        lines = []
        for name in targets:
            if name in self._disabled:
                continue
            meta = self._skills.get(name)
            if meta is None:
                continue
            lines.append(f"- {meta.name}: {_clip(meta.description, self.max_desc_chars)}")
        return "\n".join(lines)

    def _ensure_enabled(self, name: str) -> SkillMeta:
        meta = self.get(name)
        if name in self._disabled:
            raise SkillDisabled(name)
        return meta

    def load(self, name: str) -> Skill:
        """加载 skill 正文（缓存），超长截断。停用 → `SkillDisabled`。"""
        meta = self._ensure_enabled(name)
        body = self._bodies.get(name)
        if body is None:
            _, body = parse_skill_md(meta.entry)
            self._bodies[name] = body
        return Skill(meta=meta, body=_clip(body, self.max_body_chars))

    def read_resource(self, name: str, rel_path: str) -> str:
        """读取 skill 目录内的资源文件；做路径穿越与大小校验。停用 → `SkillDisabled`。"""
        meta = self._ensure_enabled(name)
        base = meta.path.resolve()
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            raise SkillError(f"路径越界，拒绝读取: {rel_path}")
        if not target.is_file():
            raise FileNotFoundError(f"资源不存在: {rel_path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return _clip(text, self.max_resource_chars)

    # ---- 声明校验 ----
    def validate_allowed_tools(self, known_tools: Iterable[str]) -> list[str]:
        """校验各 skill 声明的 allowed-tools 是否已注册；返回未注册项（仅提示，不改权限）。"""
        known = set(known_tools)
        issues: list[str] = []
        for meta in self.list_meta():
            for tool in meta.allowed_tools:
                if tool not in known:
                    issues.append(f"{meta.name}: 声明的工具未注册 -> {tool}")
        return issues
