# skills — 技能子模块

> 状态：**已实现 M1–M7**（`skill lint`、`reload()`、热重载端点 `POST /skills/reload`、
> **可写层 `writer.py` + 服务端 CRUD 端点**、**zip 包安装 `pack.py` + `POST /skills/upload`**、
> **技能启用/停用开关 `PATCH /skills/{name}`** 均已补齐）。
> 全局设计见 [docs/skills-system-design.md](../../docs/skills-system-design.md)。

## 职责

为 Agent 提供**按需加载的能力说明书（Skill）**。

工具解决「能做什么」，Skill 解决「怎么做」——把领域流程、规范、输出格式与资源打包，
平时只占用一行（name + description），模型判断相关时才把正文读进上下文。

核心机制是**渐进式披露**：启动只收目录 → 模型按需 `load` 正文 → 正文引用的资源再按需读取。

## 与工具系统的关系

Skill 系统**不自建执行器**，复用 `harness/tool`。已定方案 **C（目录入提示 + 工具取正文）**：

- `SkillRegistry.catalog()` 生成的 `name: description` 目录由宿主注入 **system prompt**；
- 仅暴露两个**只读工具** `skill.load`（取正文）与 `skill.read`（取 skill 内资源），随工具系统一起导出给模型；
- 执行完全复用已有工具（fs / shell / MCP…），Skill 不引入新执行逻辑。

- 工具系统文档：[harness/tool/README.md](../tool/README.md)
- 适配器子模块：[harness/tool/adapter/README.md](../tool/adapter/README.md)

## 目录结构

代码模块 `harness/skill`（实现）：

```
harness/skill/
├─ README.md        # 本文档
├─ __init__.py      # 公共导出
├─ schema.py        # SkillMeta / Skill / SkillError / SkillNotFound / SkillParseError / SkillDisabled / PackError
├─ parser.py        # SKILL.md 解析（frontmatter + 正文）与元信息校验
├─ registry.py      # SkillRegistry：scan / reload / get / names / all_names / catalog / load / read_resource（停用开关）
├─ lint.py          # skill lint：静态检查（只读）+ 命令行入口
├─ writer.py        # 写入器：render / write / read / delete / lint_draft / safe_skill_dir（控制台 CRUD 用）
├─ pack.py          # zip 包安装：install_zip（结构识别 + 安全校验 + 两阶段原子落盘）
├─ tools.py         # register_skill_tools()：注册 skill.load / skill.read
└─ test/            # test_skills.py、test_pack.py
```

技能文件目录 `<项目根>/skills`（内容，**单一固定搜索根**）：

```
skills/<skill-name>/SKILL.md
```

> 代码与技能文件分处两地；不做多根合并。

## 快速上手

```python
from pathlib import Path
from harness.skill import SkillRegistry, register_skill_tools
from harness.tool import ToolRegistry, Executor, Context

skills = SkillRegistry(Path("skills"))          # 构造即扫描
register_skill_tools(ToolRegistry(), skills)    # 注册 skill.load / skill.read

skills.names()                                   # ['bid-extraction', ...]
skills.catalog()                                 # 注入 system prompt 的目录文本
skills.warnings                                  # 损坏 skill 的提示（不中断加载）

executor = Executor(tool_registry)
await executor.call("skill.load", {"name": "bid-extraction"}, Context())
```

## API 摘要

| 成员 | 说明 |
|---|---|
| `SkillRegistry(root, *, disabled=(), max_desc_chars, max_body_chars, max_resource_chars)` | 构造即 `scan()`；`disabled` 为初始停用名单 |
| `scan()` / `reload()` | 重新扫描目录（**全扫，含停用项**）；单个 skill 损坏只记 `warnings`，不抛错 |
| `get(name)` / `names()` / `list_meta(*, include_disabled=False)` | 查询（未知 name 抛 `SkillNotFound`）；`get` **不过滤停用**，`names` 只给已启用项 |
| `all_names()` / `is_enabled(name)` / `disabled_names()` | 全部技能名 / 是否启用 / 实际存在的停用名单（管理端点用） |
| `set_disabled(names)` | 热切换停用集合（只改集合，无缓存可清） |
| `catalog(names=None)` | 生成 name+description 目录，description 超 `max_desc_chars` 截断加 `…`；**显式传 name 也过滤停用** |
| `load(name)` | 返回 `Skill`（正文已剥离 frontmatter，超长截断）；停用 → `SkillDisabled` |
| `read_resource(name, path)` | 读取 skill 内资源，越界抛 `SkillError`，缺失抛 `FileNotFoundError`；停用 → `SkillDisabled` |
| `validate_allowed_tools(known)` | 校验声明的工具是否已注册，**仅提示不改权限** |
| `register_skill_tools(tool_registry, skills, *, prefix="skill")` | 注册两个只读工具，返回工具名 |

**停用只过滤「曝光面」**：`scan()` 照旧全扫（管理端点 / 前端面板仍看得到停用项，否则「重新启用」无从下手），
只有 `names()` / `catalog()` / `load()` / `read_resource()` 排除停用项，`get()` 不过滤。
`load()` / `read_resource()` 命中停用抛 **`SkillDisabled`**（消息 `skill 已停用: <name>`；**刻意不复用
`SkillNotFound`**——`skill.load` 的错误文本会回喂模型，必须能区分「没这个技能」与「这个技能被关了」）。

```python
from harness.skill import SkillRegistry, SkillDisabled

skills = SkillRegistry("skills", disabled=["bid-extraction"])   # 初始停用
skills.names()                    # 不含 bid-extraction
skills.all_names()                # 含 bid-extraction
skills.catalog()                  # 目录不含停用项（显式传 name 也会被过滤）
try:
    skills.load("bid-extraction")
except SkillDisabled as exc:      # "skill 已停用: bid-extraction"
    ...

skills.set_disabled([])           # 热切换：下一轮 run 的 catalog 立刻带上
```

> 之所以能「热切换而不重建部件」：`PromptBuilder` 持有 registry 引用且每轮 `build()` 现调 `skills.catalog()`，
> `skill.load` / `skill.read` 工具也闭包同一 registry。持久真源是 `SkillsConfig.disabled`
> （`build_skills` 传给 `SkillRegistry(disabled=…)`）。

`lint` 是开发期工具，**刻意不从 `harness.skill` 顶层导出**（否则 `python -m harness.skill.lint`
会抛 runpy 警告，服务启动也会白白加载它）。用 `harness.skill.lint`：

| 成员 | 说明 |
|---|---|
| `lint_skills(root, *, known_tools=None, max_body_chars, max_desc_chars)` | 静态检查，返回 `LintIssue` 列表（空 = 没问题） |
| `LintIssue(skill, level, code, message)` | `level` 是 `error` / `warning`；`code` 稳定可脚本过滤 |
| `python -m harness.skill.lint [root]` | 命令行入口，`root` 默认 `skills`；有问题退出码 1 |

## 写入器（writer.py）

控制台需要**新建 / 编辑 / 删除**技能，写路径单独成层——同样**刻意不从 `harness.skill` 顶层导出**：

```python
from harness.skill.writer import write_skill, read_skill, delete_skill, lint_draft

write_skill("skills", name="release-check", description="发版前检查清单…",
            body="## 何时使用\n…", allowed_tools=["fs.read"], version="1.0.0")
read_skill("skills", "release-check")     # -> (SkillMeta, body)
delete_skill("skills", "release-check")   # 缺失 → SkillNotFound
lint_draft(name="draft", description="…", body="…")   # 不落盘
```

| 成员 | 说明 |
|---|---|
| `render_skill_md(...)` | 由字段拼出 `SKILL.md` 文本（不落盘） |
| `safe_skill_dir(root, name)` | **公开**：把 `name` 解析成 root 内的技能目录（唯一路径入口，见下） |
| `write_skill(root, *, name, description, body, allowed_tools, version, overwrite=False)` | 原子写入；name 已存在且 `overwrite=False` → `SkillError` |
| `read_skill(root, name)` | 读回 `(SkillMeta, body)`；缺失 → `SkillNotFound` |
| `delete_skill(root, name)` | 删除技能目录；缺失 → `SkillNotFound` |
| `lint_draft(...)` | 草稿 lint（临时目录，**不触碰真实技能目录**），返回 `LintIssue` 列表 |

安全不变量（`safe_skill_dir()` 内置）：`name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`，且解析后目录必须
`is_relative_to(root)`（`../evil` / 绝对路径 / 符号链接逃逸全部拒绝）；落盘前先用 `parse_skill_md_text()` +
`build_meta()` 自校验；覆盖前备份 `SKILL.md.bak`，再 `.tmp` + `os.replace` 原子替换。

> `safe_skill_dir()` 由私有 `_safe_dir` 提升为**公开**函数：`write_skill` / `delete_skill` /
> `pack.install_zip` 都走它，维持「路径穿越只有一个入口」这个性质。

服务端端点（`POST/GET/PUT/DELETE /skills`、`/skills/lint`、`/skills/validate`、`/skills/reload`、
`/skills/upload`、`PATCH /skills/{name}`）
见 [server/README.md](../../server/README.md) 与 [docs/api-service-design.md §4.10](../../docs/api-service-design.md)。

## zip 包安装（pack.py）

上传一个 zip 包即可**批量安装**技能。`pack.py` 是纯库能力（与 HTTP 无关），和 `writer.py` / `lint.py`
一样**刻意不从 `harness.skill` 顶层导出**（都依赖 `lint`），按需导入：

```python
from harness.skill.pack import InstallLimits, install_zip

report = install_zip("skills", zip_bytes, overwrite=False,
                     limits=InstallLimits(), known_tools=registry.names())
report.installed   # [InstalledSkill(name, files, overwritten), ...]
report.skipped     # 被忽略的噪声条目（__MACOSX/、._*、.DS_Store、Thumbs.db…）
report.warnings    # lint warning、覆盖后残留的旧文件等
```

**两种包结构自动识别**：zip 根层直接有 `SKILL.md` → 单技能（目录名取 frontmatter 的 `name`）；
否则一级子目录各含 `SKILL.md` → 多技能。两者同时存在 → `zip_ambiguous`；都没有 → `zip_no_skill`。

| 成员 | 说明 |
|---|---|
| `install_zip(root, data, *, overwrite=False, limits=None, known_tools=None)` | 安装 zip 包，返回 `InstallReport`；失败一律抛 `PackError(code, message, details)` |
| `InstallLimits(...)` | 限额（frozen）：`max_zip_bytes=20M` / `max_entries=500` / `max_total_bytes=20M` / `max_file_bytes=5M` / `max_depth=8` / `max_skills=50` |
| `InstalledSkill(name, files, overwritten)` / `InstallReport(installed, skipped, warnings)` | 安装结果 |

稳定错误码（HTTP 状态由 server 层定）：`zip_invalid` / `zip_too_large`（`details.limit` ∈
`zip_bytes`/`entries`/`total_bytes`/`file_bytes`/`skills`/`ratio`）/ `zip_unsafe_entry`（`details.reason` ∈
`absolute`/`drive`/`ads`/`traversal`/`bad_suffix`/`reserved_name`/`bad_char`/`too_deep`/`symlink`/
`special_file`/`case_conflict`）/ `zip_ambiguous` / `zip_no_skill` / `skill_invalid` / `skill_conflict`
（`details.conflicts`、`overwrite_hint`）。

**两阶段原子**：先在 root 的**同级**目录 `tempfile.mkdtemp(prefix=".skill-install-")`（同卷才能
`os.replace` 目录，且天然不被 `scan()` 看见）解包 → 逐技能 `build_meta` + `lint_skills`（**error 级才拒**，
warning 进 `warnings`）→ 算冲突（有冲突且未开 `overwrite` 就抛错，此时 root 里一个字节都没写）→ 落盘：
目标不存在走目录级 `os.replace`；覆盖时先把旧 `SKILL.md` 备份为 `.bak` 再逐文件 `os.replace`，**不删旧资源**
（陈旧文件写进 `warnings`）。极端磁盘故障下仍可能只替换了前 k 个技能——**不假装是事务**，可用 `.bak` 回滚。

## Skill 定义格式

每个 skill 是一个目录，入口为 `SKILL.md`：

```markdown
---
name: bid-extraction
description: 解标 —— 从招标文件提取评审要点并输出带行号溯源的 PDF 报告。当用户给出招标文件并要求解标时使用。
allowed-tools: [fs.read, fs.write, shell.run]
version: 1.0.0
---

# 解标流程
（步骤、输出格式、注意事项……）
```

校验规则：`name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$` **且与目录名一致**；
`description` 必填（决定模型是否选用）；frontmatter 必须由独占一行的 `---` 包裹。
未识别字段透传到 `SkillMeta.metadata`（**不会生效**——`skill lint` 会警告）。

> 文件按 **`utf-8-sig`** 读取：Windows 编辑器（记事本 / PowerShell `Set-Content`）可能写入 BOM，
> 而 BOM 会让首行变成 `\ufeff---`、frontmatter 判定失败、skill 静默加载不了。已容错。

## skill lint（静态检查）

```bash
python -m harness.skill.lint            # 检查 ./skills
python -m harness.skill.lint path/to/skills
```

```python
from harness.skill.lint import lint_skills
for issue in lint_skills("skills", known_tools=registry.names()):
    print(issue)      # [warning] alpha-skill: … (unknown-field)
```

**为什么需要它**：`scan()` 遇到坏 skill 只把提示塞进 `registry.warnings`，而这个列表**没有出口**——
服务 `/healthz` 只报 `skills: 0`，分不清"没有 skill"与"有 skill 但全写坏了"。lint 把它变成能定位的清单。

| code | 级别 | 说明 |
|---|---|---|
| `missing-root` / `missing-entry` | error | 根目录或 `SKILL.md` 不存在 |
| `parse-error` | error | frontmatter 非法（name 非法/与目录名不符/缺 description/YAML 坏） |
| `empty-body` | error | 只有 frontmatter，正文是空的 |
| `unknown-field` | warning | frontmatter 里有拼错的字段，会被静默忽略 |
| `body-too-long` | warning | 正文超 `max_body_chars`，模型只看到前一段 |
| `description-truncated` | warning | description 超 `max_desc_chars`，catalog 里显示不全 |
| `description-too-short` | warning | description < 20 字符，模型难以判断该不该选用 |
| `unknown-tool` | warning | `allowed-tools` 声明了未注册的工具（**给了 `known_tools` 才查**） |

`unknown-tool` 默认不查：服务里工具集合随配置变化（MCP / 工具开关），命令行硬查会误报。
需要时显式传 `known_tools`。

> 不做「两个目录声明同一个 name」的检查——`build_meta` 强制 `name` 等于目录名，同级目录不可能重名。

## 安全

| 风险 | 实现 |
|---|---|
| 路径穿越 | `read_resource` 用 `Path.resolve()` + `is_relative_to(skill_dir)` 拦截；写路径统一走 `safe_skill_dir()` |
| 上下文膨胀 | 正文/资源分别受 `max_body_chars` / `max_resource_chars` 截断 |
| 提示注入 | 只加载显式配置的 `root` 目录，不做多根合并 |
| 能力越权 | `allowed-tools` 仅声明与校验，权限仍由 `Context.scopes` 控制 |
| zip slip | 条目路径 `\`→`/` 归一后拒绝绝对路径 / 盘符 / `..` / `:`(ADS) / 以 `.`或空格结尾 / 保留设备名（`CON`/`NUL`/`COM1`…）/ 非法字符 `<>"\|?*` / 层级超限；解包时再校验一次 `is_relative_to(staging)` |
| zip bomb | 条目数 + 声明总量 + 单文件 + 压缩比（仅 `file_size >= 1MB` 且压缩比 > 1000）+ **有界实读**（边写边计数，不信头部声明） |
| 符号链接 / 特殊文件 | 类型位 `external_attr >> 16` 检出，直接拒绝 |
| 大小写覆盖 | 条目名做大小写不敏感重名检测（Windows 上 `A.md`/`a.md` 会互相覆盖），拒绝 |
| 覆盖残留 | 覆盖时先备份旧 `SKILL.md` 为 `.bak`，逐文件 `os.replace`，**不删旧资源**（陈旧文件进 `warnings`） |

## 测试

```bash
python -m harness.skill.test.test_skills   # 或 pytest harness/skill/test
python -m harness.skill.test.test_pack
```

**61 条，全离线**（临时目录造 skill）。`test_skills.py`（37 条）：解析与 BOM 容错 / 扫描（含损坏跳过）/
name 与目录名一致 / catalog 截断 / 正文加载与资源读取（含路径穿越）/ `allowed-tools` 声明校验 /
**`reload()`（新增与删除生效、正文缓存清空、warnings 重置、根目录缺失不炸）** /
**`skill lint`（9 个 code、级别、排序、`known_tools` 开关、CLI 退出码）** /
**`writer`（新建→读回→覆盖→删除全流程；非法名 `Bad_Name` / `../evil` / 空串 / `a/b` 全被拒且不落盘；`lint_draft` 不落盘）** /
**停用语义（`names`/`catalog` 隐藏停用项、`load`/`read_resource` 抛 `SkillDisabled`、`list_meta(include_disabled=True)` 与 `get()` 仍可见、`set_disabled()` 热切换且 `reload()` 后仍生效）** /
两个工具的接线与执行。
`test_pack.py`（24 条）：单技能 / 多技能包识别、`zip_ambiguous` / `zip_no_skill`、冲突阻断整个包、
`overwrite` 的 `.bak` 备份与陈旧文件告警、zip slip / 深层路径 / 反斜杠归一 / 符号链接 / 大小写冲突、
条目数与总量与单文件与 zip 体积限额、非法 zip、包内 lint error 拒装（root 零写入）、
噪声条目忽略、BOM 容错、装完能被 `SkillRegistry` 正常加载。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 解析 + 扫描注册 | ✅ 已完成 |
| M2 | 目录生成 + 正文加载 + 资源读取 | ✅ 已完成 |
| M3 | 工具暴露（`skill.load` / `skill.read`） | ✅ 已完成 |
| M4 | 安全：路径穿越、大小限制、声明校验 | ✅ 已完成 |
| M5 | `skill lint` + `reload()` 测试 + 热重载端点 | ✅ 已完成（[lint.py](lint.py)、`POST /skills/reload`，[docs/skills-system-design.md](../../docs/skills-system-design.md)） |
| M6 | 可写层 [writer.py](writer.py) + 服务端 CRUD 端点 + 控制台技能面板 | ✅ 已完成（[docs/api-service-design.md §4.10](../../docs/api-service-design.md)） |
| M7 | 分发与开关：zip 安装 [pack.py](pack.py) + 上传端点 + 启用/停用开关 | ✅ 已完成（[docs/skills-system-design.md](../../docs/skills-system-design.md)、`POST /skills/upload`、`PATCH /skills/{name}`） |

## 已定决策

| 议题 | 结论 |
|---|---|
| 暴露方式 | C：catalog 入 system prompt + `skill.load` / `skill.read`（不提供 `skill.list`） |
| `allowed-tools` | 仅声明与校验，不改变会话权限 |
| 搜索根目录 | 单一固定目录 `<项目根>/skills` |
| `skill lint` 形态 | **函数 + 命令行入口**（不暴露成工具；不与 CLI 那一步耦合） |
| `skill lint` 的 `unknown-tool` | 默认不查（工具集合随配置变化，硬查会误报），需显式传 `known_tools` |
| BOM | 按 `utf-8-sig` 读，容忍 Windows 编辑器写入的 BOM |
| 写入层 | 单独 `writer.py`（读路径 / 写路径分家），同样不进 `__init__` |
| 覆盖策略 | `overwrite` 显式开关；先备份 `.bak`，再 `.tmp` + `os.replace` |
| zip 包布局 | 自动识别根层 / 一级子目录；两者并存 → `zip_ambiguous` |
| zip 安装原子性 | 两阶段（同级 staging 解包+校验+算冲突 → 全通过才落盘）；有冲突时 root 零写入 |
| 上传体形态 | raw zip 字节（非 multipart，不引入 `python-multipart`）；`Content-Length` 先早拒 |
| 停用语义 | 只过滤曝光面（`scan()` 全扫、`get()` 不过滤）；`load`/`read_resource` 抛 `SkillDisabled`；`set_disabled()` 热切换 |
| `pack` 导出 | 与 `writer` / `lint` 同理，刻意不进 `__init__`，按需 `from harness.skill.pack import install_zip` |
| `safe_skill_dir` | 由 `_safe_dir` 提升为公开，作 `write_skill` / `delete_skill` / `install_zip` 的唯一路径入口 |

其余待确认项见设计文档 §10。
