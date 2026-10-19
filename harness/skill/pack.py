"""技能 zip 包的安装：结构识别 → 安全校验 → 两阶段原子落盘。

`writer.py` 只管单个 `SKILL.md` 的文本读写；zip 引入的是**新的攻击面**
（路径穿越 / zip bomb / 符号链接 / 大小写覆盖），所以单独成模块便于评审。

支持两种包结构（**自动识别**）：

- **单技能**：zip 根层就有 `SKILL.md`（可附带 `reference/`、`scripts/` 等资源目录）；
- **多技能**：一级子目录各含一个 `SKILL.md`。

两者同时存在 → `zip_ambiguous`（无法判断意图，不猜）。

**原子性**：先在 root 的**同级**临时目录里解包 + 校验 + 算冲突，全部通过后才逐个
`os.replace` 进 root。因此「有同名冲突 / 包内有非法技能 / 命中任何限额」时，
root 里一个字节都没写。极端磁盘故障下仍可能只替换了前 k 个技能——这里不假装是事务。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .lint import ERROR, LintIssue, lint_skills
from .parser import NAME_PATTERN, parse_skill_md
from .registry import ENTRY_NAME
from .schema import PackError, SkillError, SkillParseError
from .writer import safe_skill_dir

CHUNK = 64 * 1024

# 噪声条目：macOS / Windows / 版本控制产物，进 skipped 而不是报错
NOISE_DIRS = {"__MACOSX", ".git", ".svn", "__pycache__", ".idea", ".vscode"}
NOISE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
NOISE_SUFFIXES = (".bak", ".tmp", ".orig", ".rej")

STAGING_PREFIX = ".skill-install-"
SINGLE_DIR = "__single__"  # 单技能包在 staging 内部的临时目录名

# zip bomb 的「声明压缩比」闸门：只对大文件生效，避免误杀高度重复的正常文本
BOMB_RATIO = 1000
BOMB_MIN_BYTES = 1_000_000

_WIN_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_BAD_CHARS = frozenset('<>"|?*')

_Entry = tuple[zipfile.ZipInfo, list[str]]


@dataclass(frozen=True)
class InstallLimits:
    """安装限额（默认值足够宽松，同时把 zip bomb 的破坏面压到 20MB 以内）。"""

    max_zip_bytes: int = 20_000_000  # 上传原始体上限
    max_entries: int = 500  # zip 条目数
    max_total_bytes: int = 20_000_000  # 解包总量（声明值 + 实读值都查）
    max_file_bytes: int = 5_000_000  # 单文件
    max_depth: int = 8  # 相对包根的路径深度
    max_skills: int = 50  # 一包最多装几个技能


@dataclass(frozen=True)
class InstalledSkill:
    name: str
    files: list[str]  # 相对技能目录的路径（含 SKILL.md）
    overwritten: bool


@dataclass
class InstallReport:
    installed: list[InstalledSkill] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 被忽略的噪声条目（原样名称）
    warnings: list[str] = field(default_factory=list)  # lint warning、覆盖后残留的旧文件等


def install_zip(
    root: Path | str,
    data: bytes,
    *,
    overwrite: bool = False,
    limits: InstallLimits | None = None,
    known_tools: Iterable[str] | None = None,
) -> InstallReport:
    """把一个 zip 包里的技能安装到 `root` 下（详见模块 docstring）。

    失败一律抛 `PackError`（带稳定 `code` 与 `details`），HTTP 状态由 server 层决定。
    """
    limits = limits or InstallLimits()
    if len(data) > limits.max_zip_bytes:
        raise _too_large("zip_bytes", limits.max_zip_bytes, len(data))

    try:
        parsed = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise PackError("zip_invalid", f"不是有效的 zip 包: {exc}") from exc

    installed: list[InstalledSkill] = []
    with parsed:
        entries, skipped = _collect(parsed, limits)
        mode, dirs = _detect(entries, limits)
        staging = _make_staging(root)
        try:
            _extract(parsed, entries, staging, mode, limits)
            names = _finalize(staging, mode, dirs)
            errors, warnings = _validate(staging, names, known_tools)
            if errors:
                raise PackError(
                    "skill_invalid",
                    "包内有不可用的技能（详见 details.issues）",
                    details={"skills": _names_of(errors), "issues": errors},
                )

            resolved_root = Path(root).expanduser().resolve()
            targets: list[Path] = []
            for name in names:
                try:
                    targets.append(safe_skill_dir(resolved_root, name))
                except SkillError as exc:
                    raise PackError(
                        "skill_invalid",
                        str(exc),
                        details={"skills": [{"name": name, "reason": str(exc)}], "issues": []},
                    ) from exc

            conflicts = sorted(
                name for name, target in zip(names, targets) if target.exists()
            )
            if conflicts and not overwrite:
                raise PackError(
                    "skill_conflict",
                    f"已存在同名技能 {len(conflicts)} 个；确认覆盖请勾选「覆盖」后重传",
                    details={"conflicts": conflicts, "overwrite_hint": True},
                )

            resolved_root.mkdir(parents=True, exist_ok=True)
            installed, merge_warnings = _commit(staging, targets, names)
            warnings.extend(merge_warnings)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    return InstallReport(installed=installed, skipped=skipped, warnings=warnings)


# ---- 内部：收集与识别 ----
def _safe_entry(rel: str) -> list[str]:
    """校验并切分包内条目路径；任何可疑形态都抛 `zip_unsafe_entry`。"""
    text = rel.replace("\\", "/")

    def reject(reason: str) -> PackError:
        return PackError(
            "zip_unsafe_entry",
            f"包内条目路径不安全: {rel}",
            details={"entry": rel, "reason": reason},
        )

    if not text or text.startswith("/"):
        raise reject("absolute")
    if _WIN_DRIVE.match(text):
        raise reject("drive")
    if ":" in text:
        raise reject("ads")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise reject("bad_suffix")
    for part in parts:
        if part == "..":
            raise reject("traversal")
        # Windows 会静默剥掉结尾的 `.` / 空格，导致覆盖同名文件
        if part != part.rstrip(". "):
            raise reject("bad_suffix")
        if _RESERVED.match(part):
            raise reject("reserved_name")
        if any(char in _BAD_CHARS for char in part):
            raise reject("bad_char")
    return parts


def _is_noise(parts: list[str]) -> bool:
    if any(part in NOISE_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    if name in NOISE_NAMES or name.startswith("._"):
        return True
    return name.endswith(NOISE_SUFFIXES)


def _collect(zf: zipfile.ZipFile, limits: InstallLimits) -> tuple[list[_Entry], list[str]]:
    """过滤目录项 / 噪声项，做路径、类型、限额与大小写冲突校验。"""
    entries: list[_Entry] = []
    skipped: list[str] = []
    seen: dict[str, str] = {}
    declared = 0

    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = _safe_entry(info.filename)
        if _is_noise(parts):
            skipped.append(info.filename)
            continue

        filetype = stat.S_IFMT(info.external_attr >> 16)
        if filetype == stat.S_IFLNK:
            raise PackError(
                "zip_unsafe_entry",
                f"包内含符号链接，拒绝安装: {info.filename}",
                details={"entry": info.filename, "reason": "symlink"},
            )
        if filetype not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise PackError(
                "zip_unsafe_entry",
                f"包内含特殊类型条目，拒绝安装: {info.filename}",
                details={"entry": info.filename, "reason": "special_file"},
            )
        if len(parts) > limits.max_depth:
            raise PackError(
                "zip_unsafe_entry",
                f"包内条目层级过深: {info.filename}",
                details={"entry": info.filename, "reason": "too_deep"},
            )
        if info.file_size > limits.max_file_bytes:
            raise _too_large("file_bytes", limits.max_file_bytes, info.file_size)
        if (
            info.file_size >= BOMB_MIN_BYTES
            and info.compress_size > 0
            and info.file_size / info.compress_size > BOMB_RATIO
        ):
            raise _too_large("ratio", BOMB_RATIO, round(info.file_size / info.compress_size))

        key = "/".join(part.lower() for part in parts)
        text = "/".join(parts)
        if key in seen:
            raise PackError(
                "zip_unsafe_entry",
                f"包内条目大小写冲突（Windows 上会互相覆盖）: {text} 与 {seen[key]}",
                details={"entry": text, "reason": "case_conflict"},
            )
        seen[key] = text

        declared += info.file_size
        entries.append((info, parts))

    if len(entries) > limits.max_entries:
        raise _too_large("entries", limits.max_entries, len(entries))
    if declared > limits.max_total_bytes:
        raise _too_large("total_bytes", limits.max_total_bytes, declared)
    return entries, skipped


def _detect(entries: list[_Entry], limits: InstallLimits) -> tuple[str, list[str]]:
    """识别包结构，返回 ("single" | "multi", 技能目录名列表)。"""
    root_skill = any(parts == [ENTRY_NAME] for _, parts in entries)
    subdirs = sorted(
        {parts[0] for _, parts in entries if len(parts) == 2 and parts[1] == ENTRY_NAME}
    )

    if root_skill and subdirs:
        raise PackError(
            "zip_ambiguous",
            "包内同时存在根层 SKILL.md 与子目录技能，无法判断结构；请二选一",
            details={"entries": [ENTRY_NAME, *[f"{name}/{ENTRY_NAME}" for name in subdirs]]},
        )
    if root_skill:
        return "single", []
    if not subdirs:
        raise PackError(
            "zip_no_skill",
            f"包内没有找到 {ENTRY_NAME}（应在根层，或在一级子目录里）",
        )
    if len(subdirs) > limits.max_skills:
        raise _too_large("skills", limits.max_skills, len(subdirs))
    return "multi", subdirs


def _make_staging(root: Path | str) -> Path:
    """在 root 的**同级**建临时目录（同卷才能 `os.replace` 目录，且不被 `scan()` 看见）。"""
    resolved = Path(root).expanduser().resolve()
    try:
        return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=resolved.parent))
    except OSError:  # 父目录不可写 → 退回 root 内（scan 只会给一条 warning）
        resolved.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=resolved))


# ---- 内部：解包与校验 ----
def _extract(
    zf: zipfile.ZipFile,
    entries: list[_Entry],
    staging: Path,
    mode: str,
    limits: InstallLimits,
) -> None:
    """把条目解到 staging（单技能包统一落到 `SINGLE_DIR` 下）。**有界实读**，不信头部声明。"""
    base = staging.resolve()
    total = 0

    for info, parts in entries:
        target_parts = [SINGLE_DIR, *parts] if mode == "single" else parts
        target = staging.joinpath(*target_parts)
        if not target.resolve().is_relative_to(base):
            raise PackError(
                "zip_unsafe_entry",
                f"包内条目路径不安全: {info.filename}",
                details={"entry": info.filename, "reason": "traversal"},
            )
        target.parent.mkdir(parents=True, exist_ok=True)

        written = 0
        try:
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > limits.max_file_bytes:
                        raise _too_large("file_bytes", limits.max_file_bytes, written)
                    if total > limits.max_total_bytes:
                        raise _too_large("total_bytes", limits.max_total_bytes, total)
                    dst.write(chunk)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise PackError(
                "zip_invalid", f"解压失败（包可能损坏）: {info.filename}: {exc}"
            ) from exc


def _finalize(staging: Path, mode: str, dirs: list[str]) -> list[str]:
    """单技能包按 frontmatter 的 `name` 重命名目录；返回技能名列表。"""
    if mode == "multi":
        return list(dirs)

    staged = staging / SINGLE_DIR
    try:
        raw, _ = parse_skill_md(staged / ENTRY_NAME)
    except SkillParseError as exc:
        raise PackError(
            "skill_invalid",
            f"{ENTRY_NAME} 无法解析: {exc}",
            details={"skills": [{"name": "", "reason": str(exc)}], "issues": []},
        ) from exc

    raw_name = raw.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not NAME_PATTERN.match(name):
        raise PackError(
            "skill_invalid",
            f"frontmatter 的 name 非法: {raw_name!r}（只允许小写字母/数字/连字符）",
            details={
                "skills": [{"name": str(raw_name), "reason": "name 必须是 小写字母/数字/连字符"}],
                "issues": [],
            },
        )
    os.replace(staged, staging / name)
    return [name]


def _validate(
    staging: Path, names: list[str], known_tools: Iterable[str] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """对 staging 里的技能跑 lint：**error 级才拒**，warning 转成提示语。"""
    wanted = set(names)
    issues = [issue for issue in lint_skills(staging, known_tools=known_tools) if issue.skill in wanted]
    errors = [_issue_dict(issue) for issue in issues if issue.level == ERROR]
    warnings = [
        f"{issue.skill}: {issue.message}（{issue.code}）" for issue in issues if issue.level != ERROR
    ]
    return errors, warnings


def _commit(
    staging: Path, targets: list[Path], names: list[str]
) -> tuple[list[InstalledSkill], list[str]]:
    """逐个技能落盘：新建走目录级 `os.replace`，覆盖走逐文件替换 + `SKILL.md.bak`。"""
    installed: list[InstalledSkill] = []
    warnings: list[str] = []

    for name, dst in zip(names, targets):
        src = staging / name
        files = _relative_files(src)
        overwritten = dst.exists()

        if not overwritten:
            os.replace(src, dst)
        else:
            entry = dst / ENTRY_NAME
            if entry.is_file():
                shutil.copy2(entry, entry.with_name(ENTRY_NAME + ".bak"))
            for rel in files:
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src / rel, target)
            stale = [
                rel
                for rel in _relative_files(dst)
                if rel not in files and rel != ENTRY_NAME + ".bak"
            ]
            if stale:
                warnings.append(
                    f"{name}: 覆盖后保留了 {len(stale)} 个旧文件 {stale}（不自动删除，请人工确认）"
                )

        installed.append(InstalledSkill(name=name, files=files, overwritten=overwritten))
    return installed, warnings


# ---- 内部：小工具 ----
def _relative_files(directory: Path) -> list[str]:
    return sorted(
        str(path.relative_to(directory)).replace(os.sep, "/")
        for path in directory.rglob("*")
        if path.is_file()
    )


def _names_of(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"name": str(item.get("skill", "")), "reason": str(item.get("message", ""))} for item in errors]


def _issue_dict(issue: LintIssue) -> dict[str, Any]:
    return {
        "skill": issue.skill,
        "level": issue.level,
        "code": issue.code,
        "message": issue.message,
    }


def _too_large(limit: str, maximum: int, actual: int) -> PackError:
    return PackError(
        "zip_too_large",
        f"超过限额（{limit}）：实际 {actual} > 上限 {maximum}",
        details={"limit": limit, "max": maximum, "actual": actual},
    )


__all__ = [
    "InstallLimits",
    "InstallReport",
    "InstalledSkill",
    "install_zip",
]
