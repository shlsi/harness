# Skills 系统设计文档

> 定位：**可扩展技能库（渐进式披露）** · 技术栈：**Python** · 状态：**已实现 M1–M7**
> 相关：[工具系统设计文档](agent-tool-system-design.md) · [harness/skill/README.md](../harness/skill/README.md)

---

## 1. 要解决什么问题

工具（Tool）解决的是「**能做什么**」——原子能力，模型通过 function calling 调用。
但很多任务需要的是「**怎么做**」——一套领域流程、规范、输出格式，而不是一个新的函数。

举例：
- 「解标（从招标文件提取要点出报告）」
- 「md 转 docx 并还原版式」
- 「生成编标大纲」

这些能力本质是**知识 + 流程 + 资源**，塞进系统提示会撑爆上下文，做成工具又无法表达。

**Skill = 按需加载的能力说明书。** 平时只占一行（name + description），
模型判断相关时才把正文读进上下文，正文里引用的脚本/参考文档再按需取用。

### Skill vs Tool

| 维度 | Tool | Skill |
|---|---|---|
| 本质 | 可执行函数 | 知识/流程/资源包 |
| 暴露方式 | function calling | 目录（catalog）→ 按需加载正文 |
| 上下文占用 | 定义常驻（可裁剪） | 仅 name+description 常驻，正文按需 |
| 谁执行 | 框架 | 模型（用已有工具执行） |
| 数量级 | 几十 | 可上百（靠描述筛选） |

> 二者互补：**Skill 通常不引入新函数，而是教会模型如何组合已有工具。**

---

## 2. 核心机制：渐进式披露（Progressive Disclosure）

```
① 启动扫描
   skills/*/SKILL.md → 只取 frontmatter 的 name + description
   ↓
② 注入目录（catalog）
   系统提示里只放「name: description」列表（每个约 1~2 行）
   ↓
③ 模型判断「这个任务需要某个 skill」
   ↓
④ 按需加载正文
   调用 skill.load(name) → 返回 SKILL.md 正文进上下文
   ↓
⑤ 按需取资源
   正文里提到 reference.md / scripts/xxx.py
   → 模型用 skill.read(name, rel_path) 或已有 fs.read 读取
   ↓
⑥ 用已有工具执行
```

三层加载，token 从粗到细，避免一次性灌入。

---

## 3. Skill 目录约定

```
skills/
└─ <skill-name>/
   ├─ SKILL.md          # 必须：frontmatter + 正文
   ├─ reference.md      # 可选：参考文档（术语表、字段说明等）
   ├─ scripts/          # 可选：脚本（由 shell.run / code.run 调用）
   └─ assets/           # 可选：模板、样例文件
```

### zip 包布局（上传安装）

`skills/` 目录也可通过上传一个 zip 包**批量安装**技能（见 §5 `pack.py`）。包结构**自动识别**两种：

| 布局 | 判定 | 装法 |
|---|---|---|
| **单技能** | zip 根层直接有 `SKILL.md` | 所有条目装进一个技能目录，目录名取 frontmatter 的 `name` |
| **多技能** | 一级子目录各含一个 `SKILL.md` | 每个子目录算一个技能，目录名 = 子目录名 |

两者同时存在 → `zip_ambiguous`（无法判断意图，**不猜**）；都没有 → `zip_no_skill`。

包内自带的 `__MACOSX/`、`._*`、`.DS_Store`、`Thumbs.db`、`desktop.ini`、`.git/`、`.svn/`、
`__pycache__/`、`.idea/`、`.vscode/`、`*.bak` / `*.tmp` / `*.orig` / `*.rej` 等**噪声条目**
（macOS / Windows / 版本控制产物）进 `report.skipped` 被忽略，**不算错误**。

### SKILL.md 格式

```markdown
---
name: bid-extraction
description: 解标 —— 从招标文件中提取评审要点、资质要求并输出带行号溯源的 PDF 报告。当用户给出招标文件并要求解标时使用。
allowed-tools: [fs.read, fs.write, shell.run]      # 可选：声明该 skill 依赖的工具
version: 1.0.0                                     # 可选
---

# 解标流程

## 步骤
1. 读取招标文件 …
2. 按章节提取…

## 输出格式
- 每条结论必须带「文件名 + 行号」溯源
- 报告结构见 reference.md

## 注意事项
- 不确定的条款标注「待确认」，不要编造
```

**frontmatter 字段**

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 唯一标识，`^[a-z0-9]+(-[a-z0-9]+)*$`，与目录名一致 |
| `description` | 是 | 决定模型是否选用，建议 ≤ 200 字，写清「做什么 + 何时用」 |
| `allowed-tools` | 否 | 声明依赖的工具（用于校验/权限收敛） |
| `version` | 否 | 版本号 |
| 其他 | 否 | 透传到 `SkillMeta.metadata`（**不会生效**，`skill lint` 会以 `unknown-field` 警告） |

> **文件按 `utf-8-sig` 读取**：Windows 编辑器（记事本、PowerShell `Set-Content`）会写 BOM，
> 而 BOM 让首行变成 `\ufeff---`、frontmatter 判定失败，skill 就**静默加载不了**（只表现为 `skills: 0`）。
> 已做容错——这一条正是 `skill lint` 真机跑出来的（它把这个失败报成 `parse-error`）。

---

## 4. 数据结构

```python
@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path                                   # skill 目录
    entry: Path                                  # SKILL.md 路径
    allowed_tools: tuple[str, ...] = ()
    version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Skill:
    meta: SkillMeta
    body: str                                    # SKILL.md 正文（不含 frontmatter）
```

两类新增异常（M7）：

```python
class SkillDisabled(SkillError):                 # 目录仍在，但被停用 → load / read_resource 拒绝
    def __init__(self, name: str) -> None: ...   # 消息：skill 已停用: <name>

class PackError(SkillError):                     # zip 安装失败；HTTP 状态由 server 层决定
    code: str
    message: str
    details: dict[str, Any]
```

---

## 5. 模块 API

```python
class SkillRegistry:
    def __init__(self, root: Path, *, disabled: Iterable[str] = (),
                 max_desc_chars: int = 300, max_body_chars: int = 60_000,
                 max_resource_chars: int = 100_000) -> None: ...

    def scan(self) -> list[SkillMeta]: ...        # 扫描并解析（**全扫，含停用项**）
    def reload(self) -> None: ...                 # 热重载（开发期）
    def get(self, name: str) -> SkillMeta: ...    # **不过滤停用**（管理端点还要用）；不存在 → SkillNotFound
    def names(self) -> list[str]: ...             # **已启用**的技能名（曝光面）
    def all_names(self) -> list[str]: ...         # 全部技能名（含停用；管理端点 / healthz 用）
    def is_enabled(self, name: str) -> bool: ...
    def disabled_names(self) -> list[str]: ...    # 停用名单（**只报实际存在的**）
    def set_disabled(self, names: Iterable[str]) -> None: ...   # 热切换（只改集合，无缓存可清）
    def list_meta(self, *, include_disabled: bool = False) -> list[SkillMeta]: ...
    def catalog(self, *, names: list[str] | None = None) -> str:
        """生成注入 system prompt 的目录，仅含 name + description（**显式传入 name 也过滤停用**）。"""
    def load(self, name: str) -> Skill: ...       # 读取并按需缓存正文；停用 → SkillDisabled
    def read_resource(self, name: str, rel_path: str) -> str:
        """读取 skill 目录内文件，做路径穿越校验；停用 → SkillDisabled。"""
```

> `root` 为**单一固定目录**（`<项目根>/skills`），不做多根合并；
> 注意区分「代码模块 `harness/skill`」与「技能文件目录 `<项目根>/skills`」——二者不是同一处。

**停用（disabled）只过滤「曝光面」**：`scan()` 照旧全扫（所以管理端点与前端面板仍能看到停用项，
否则「重新启用」无从下手）；只有 `names()` / `catalog()` / `load()` / `read_resource()` 排除停用项，
`get()` **不过滤**。之所以能「热切换」，是因为 `PromptBuilder` 持有 registry 引用且每轮 `build()`
现调 `skills.catalog()`，`skill.load` / `skill.read` 工具也闭包同一 registry——`set_disabled()` 之后
**下一轮 run 立即生效**，无需重建任何部件（详见 §9 M7）。持久真源是 `SkillsConfig.disabled`。

### 解析器（parser.py）

```python
def parse_skill_md(path: Path) -> tuple[dict[str, Any], str]:
    """拆出 frontmatter（YAML）与正文；YAML 用 PyYAML（已装）。"""
```

校验失败（缺 name/description、name 非法、目录名不匹配）→ 记 warning 并跳过该 skill，
**不因单个 skill 损坏导致整体加载失败**。

### 写入器（writer.py）

控制台要能**新建 / 编辑 / 删除**技能，所以有了这一层。它只负责「把一份草稿安全地变成磁盘上的 `SKILL.md`」，
不做扫描（那是 `registry` 的事）：

```python
def render_skill_md(*, name, description, body="", allowed_tools=(), version=None) -> str: ...
def write_skill(root, *, name, description, body="", allowed_tools=(), version=None,
                overwrite=False) -> Path: ...        # 已存在且 overwrite=False → SkillError
def read_skill(root, name) -> tuple[SkillMeta, str]: ... # 缺失 → SkillNotFound
def delete_skill(root, name) -> None: ...                # 缺失 → SkillNotFound
def lint_draft(*, name, description, body="", allowed_tools=(), version=None,
               known_tools=None) -> list[LintIssue]: ...   # 草稿 lint，**不落盘**
def safe_skill_dir(root, name) -> Path: ...              # 公开：写路径的**唯一**入口（见下）
```

关键不变量：

- `safe_skill_dir()`：`name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`；解析后目录必须 `is_relative_to(root)`（防 `..` / 绝对路径 / 符号链接逃逸）。
- 写入前**先用 `parse_skill_md_text()` + `build_meta()` 自校验**，保证落盘的 `SKILL.md` 一定能被 `scan()` 读回。
- 覆盖已有文件时先备份 `SKILL.md.bak`，再写 `.tmp` + `os.replace`（原子替换）。
- `lint_draft()` 用 `tempfile.mkdtemp()` 造一份临时草稿后复用 `lint_skills()`，`finally` 清理——**绝不触碰真实技能目录**。

> `safe_skill_dir()` 由私有 `_safe_dir` 提升为**公开**函数：`write_skill` / `delete_skill` /
> `pack.install_zip` 都走它，维持「路径穿越只有一个入口」这个性质。

> `writer.py` 同样**不从 `harness/skill/__init__.py` 导出**（与 `lint` 同理，避免无谓加载）。

### 安装器（pack.py）

zip 包安装是**新的攻击面**（路径穿越 / zip bomb / 符号链接 / 大小写覆盖），所以单独成模块便于评审。
它与 HTTP 无关，是纯库能力：

```python
@dataclass(frozen=True)
class InstallLimits:
    max_zip_bytes: int = 20_000_000       # 上传原始体上限
    max_entries: int = 500                # zip 条目数
    max_total_bytes: int = 20_000_000     # 解包总量（声明值 + 实读值都查）
    max_file_bytes: int = 5_000_000       # 单文件
    max_depth: int = 8                    # 相对包根的路径深度
    max_skills: int = 50                  # 一包最多装几个技能

def install_zip(root, data: bytes, *, overwrite=False, limits=None,
                known_tools=None) -> InstallReport: ...
# InstallReport(installed: list[InstalledSkill], skipped: list[str], warnings: list[str])
# InstalledSkill(name: str, files: list[str], overwritten: bool)
# 失败一律抛 PackError(code, message, details)；HTTP 状态由 server 层定
```

**两阶段原子**：先在 root 的**同级**目录 `tempfile.mkdtemp(prefix=".skill-install-")`（同卷才能
`os.replace` 目录，且天然不被 `scan()` 看见；parent 不可写则回落 root 内并在 `finally` 清理）解包 →
逐技能 `build_meta` + `lint_skills`（**error 级才拒**，warning 进 `report.warnings`）→ 算冲突
（有冲突且未开 `overwrite` 就抛错，此时 root 里一个字节都没写）→ 落盘：目标不存在走目录级
`os.replace`；覆盖时先把旧 `SKILL.md` 备份为 `SKILL.md.bak` 再逐文件 `os.replace`，**不删旧资源**
（陈旧文件写进 `warnings`）。

> 已知限制：极端磁盘故障下仍可能只替换了前 k 个技能，**不假装是事务**，可用 `.bak` 回滚。

> `pack.py` 与 `writer.py` / `lint.py` 一样**刻意不在包里导入**（它们都依赖 `lint`，导进来会让
> `python -m harness.skill.lint` 触发 runpy RuntimeWarning，也让服务启动白白加载 lint）——
> 用 `from harness.skill.pack import install_zip` 按需导入。

---

## 6. 与工具系统的集成

Skill 系统**不自建执行器**，而是复用 `harness/tool`。

**已定方案：C —— 目录入提示 + 工具取正文。**

| 环节 | 做法 |
|---|---|
| 目录发现 | `SkillRegistry.catalog()` 生成的 name+description 列表，由宿主注入 **system prompt**（不占用一次 function calling） |
| 加载正文 | 暴露只读工具 **`skill.load`**，模型按需调用取 `SKILL.md` 正文 |
| 取资源 | 暴露只读工具 **`skill.read`**，读取 skill 目录内的脚本/参考文档 |
| 执行 | 完全复用已有工具（fs / shell / MCP…），Skill 不引入新执行逻辑 |

被否决的方案：A（全工具化，含 `skill.list`）——多一次调用且目录信息在提示里天然更省；
B（纯 fs.read 读正文）——依赖 fs 权限、不可观测。

### 工具定义

只注册**两个**只读工具（`tags=["skill"]`）：

| 工具 | 参数 | 返回 | side_effect | 校验 |
|---|---|---|---|---|
| `skill.load` | `name: str` | `SKILL.md` 正文（超长截断） | read | skill 必须存在**且已启用**（停用 → `SkillDisabled`） |
| `skill.read` | `name: str, path: str` | skill 目录内文件内容（有大小上限） | read | 路径必须在该 skill 目录内；停用 → `SkillDisabled` |

> 目录已在 system prompt 中，故**不提供** `skill.list`。
> 两个工具均为只读，无需审批；随工具系统一起导出给模型。

---

## 7. 安全

| 风险 | 对策 |
|---|---|
| 路径穿越（`../../etc/passwd`） | `read_resource` 解析后校验 `resolved.is_relative_to(skill_dir)` |
| 超大文件撑爆上下文 | `read_resource` 与 `skill.load` 设 `max_chars`，超限截断 |
| 提示注入 | skill 正文会进入上下文，**只加载可信来源目录**；`root` 需显式配置 |
| 能力越权 | `allowed-tools` **仅作声明与校验**（启动时检查声明的工具已注册），不改变会话权限；真正收敛仍靠 `Context.scopes` |
| 递归膨胀 | 限制 `skill.read` 单次大小与加载次数，正文内不自动展开引用 |
| **写操作越界**（控制台新建/删除） | `writer.safe_skill_dir()` 双重拦截：`name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`，且解析后目录必须 `is_relative_to(root)`——`../evil`、`a/b`、绝对路径、指向 root 自身的名字全部拒绝；服务层再把 `SkillError` 统一转 **400 `skill_invalid`** |
| **写坏技能**（落盘后 scan 读不回） | 写盘前先用 `parse_skill_md_text()` + `build_meta()` 自校验；覆盖前备份 `.bak`；`os.replace` 原子替换 |
| **草稿校验污染目录** | `lint_draft()` 只在 `tempfile.mkdtemp()` 里造草稿，`finally` 清理 |
| **zip slip**（包内条目 `../../x`） | 条目路径先 `\` → `/` 归一；拒绝绝对路径 / 盘符 / `..` 段 / `:`（ADS）/ 以 `.` 或空格结尾（Windows 会剥离）/ 保留设备名（`CON`/`NUL`/`COM1`…）/ Windows 非法字符 `<>"\|?*` / 层级超限；解包时再校验一次 `is_relative_to(staging)`；命中 → `zip_unsafe_entry`（`details.reason`） |
| **zip bomb** | 条目数 + 声明总量 + 单文件 + 压缩比（**仅当 `file_size >= 1MB` 且压缩比 > 1000**，避免误杀高度重复的正常文本）+ **有界实读**（边写边计数，不信头部声明）；超限 → `zip_too_large`（`details.limit`） |
| **符号链接 / 特殊文件** | 由类型位 `external_attr >> 16` 检出，直接拒绝（`zip_unsafe_entry`，`reason=symlink` / `special_file`） |
| **大小写覆盖** | 条目名做**大小写不敏感**重名检测（Windows 上 `A.md` 与 `a.md` 会互相覆盖）→ `zip_unsafe_entry`（`reason=case_conflict`） |
| **覆盖残留** | 覆盖时先把旧 `SKILL.md` 备份为 `.bak`，逐文件 `os.replace`，**不删旧资源**（陈旧文件写进 `warnings`，请人工确认） |

---

## 8. 目录结构

**代码模块**（`harness/skill`）：

```
harness/skill/
├─ README.md           # 模块文档
├─ __init__.py         # 公共导出
├─ schema.py           # SkillMeta / Skill / SkillError / SkillNotFound / SkillDisabled / PackError
├─ parser.py           # SKILL.md 解析（frontmatter + 正文）
├─ registry.py         # SkillRegistry：scan / catalog / load / read_resource（含停用开关）
├─ tools.py            # skill.load / skill.read（注册进 tool 注册表）
├─ lint.py             # 静态检查（函数 + `python -m` 入口）
├─ writer.py           # 写入器：render / write / read / delete / lint_draft / safe_skill_dir
├─ pack.py             # zip 包安装：install_zip（结构识别 + 安全校验 + 两阶段原子落盘）
└─ test/
```

**技能文件目录**（单一固定，`<项目根>/skills`）：

```
agent/
└─ skills/                        # SkillRegistry.root，唯一搜索根
   ├─ bid-extraction/
   │  ├─ SKILL.md
   │  └─ reference.md
   └─ md2docx/
      ├─ SKILL.md
      └─ scripts/convert.py
```

> 代码与技能文件**分处两地**：`harness/skill` 放实现，`<项目根>/skills` 放内容。
> 单一固定目录，不做多根合并（同名冲突问题因此不存在）。

---

## 9. 落地路线

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 解析+注册 | `parser` + `SkillRegistry.scan/get/names` | ✅ 已完成 |
| M2 目录+加载 | `catalog()` + `load()` + `read_resource()` | ✅ 已完成 |
| M3 工具暴露 | `tools.py` 注册 `skill.load` / `skill.read`；`catalog()` 接入 system prompt | ✅ 已完成（工具部分） |
| M4 安全 | 路径穿越校验、大小限制、`allowed-tools` 声明校验 | ✅ 已完成 |
| M5 体验 | `skill lint` 校验、`reload()` 测试、热重载端点 | ✅ 已完成（[lint.py](../harness/skill/lint.py)、`POST /skills/reload`） |
| M6 可写 | `writer.py`（render/write/read/delete/lint_draft）+ 服务端 CRUD 端点 + 控制台技能面板 | ✅ 已完成（[writer.py](../harness/skill/writer.py)、见下） |
| M7 分发与开关 | `pack.py`（zip 上传安装）+ 技能启用/停用开关 | ✅ 已完成（[pack.py](../harness/skill/pack.py)、`POST /skills/upload`、`PATCH /skills/{name}`，见下） |

**最小可用 = M1 + M2 + M3**（已达成；其中 `catalog()` 注入 system prompt 属宿主/Agent Loop 职责，
已由 `PromptBuilder` 接入）。实现见 [harness/skill/README.md](../harness/skill/README.md)；
测试：`python -m harness.skill.test.test_skills`（37/37）与 `python -m harness.skill.test.test_pack`（24/24），
共 **61** 条（全仓测试总数 **375**）。

### M5 交付明细

`skill lint` 做成**函数 + 命令行入口**（不暴露成工具，也不与尚未存在的 CLI 耦合）：

```bash
python -m harness.skill.lint [root]        # root 默认 skills；有问题退出码 1
```

```python
from harness.skill.lint import lint_skills
lint_skills("skills", known_tools=registry.names())   # -> list[LintIssue]
```

它存在的理由：`scan()` 把坏 skill 的提示塞进 `registry.warnings`，而**这个列表没有任何出口**——
服务 `/healthz` 只报 `skills: 0`，分不清"没有 skill"与"有 skill 但全写坏了"。

| code | 级别 | 说明 |
|---|---|---|
| `missing-root` / `missing-entry` | error | 根目录或 `SKILL.md` 不存在 |
| `parse-error` | error | frontmatter 非法 |
| `empty-body` | error | 只有 frontmatter，没有正文 |
| `unknown-field` | warning | 拼错的 frontmatter 字段会被静默忽略 |
| `body-too-long` | warning | 正文超 `max_body_chars`，模型只看到前一段 |
| `description-truncated` | warning | description 超 `max_desc_chars`，目录里显示不全 |
| `description-too-short` | warning | description < 20 字符，模型难以判断该不该选用 |
| `unknown-tool` | warning | `allowed-tools` 声明了未注册工具（**给 `known_tools` 才查**） |

`reload()` 测试（M5 遗留的另一半）覆盖：新增/删除 skill 生效、正文缓存清空、`warnings` 重置、
根目录缺失不抛错。

### 热重载（`POST /skills/reload`）

**形态选择：命令触发的端点，而非文件监听**。文件监听要引入 `watchdog` 依赖，且「何时算改完」
难以判定（编辑器写盘常常是多次事件）；显式端点行为确定、零依赖，也让"改完再刷新"的意图清晰。

```bash
curl -X POST http://127.0.0.1:8000/skills/reload
# {"skills": [...], "warnings": [...]}
```

**不需要换会话**：catalog 是**每轮 run 现读**的——[`PromptBuilder.system_prompt()`](../harness/agent/prompt.py)
在每次 `build()` 里调 `skills.catalog()`，所以重载后**下一轮对话**就用上新目录，已存在的会话同样生效。
（system prompt 的字节稳定只在**同一轮内**要求；跨轮刷新 catalog 是预期行为。）

技能未启用（`skills.root` 为空）时返回 400 `skills_disabled`，不会静默成功。

### M6 交付明细：可写技能（控制台 CRUD）

服务的端点（完整字段与错误码见 [api-service-design.md §4.10](api-service-design.md)）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/skills` | 列表（未启用 → `[]`） |
| GET | `/skills/{name}` | 详情（**含 `body`**） |
| POST | `/skills` | 新建（201）；name 已存在 → 400 |
| PUT | `/skills/{name}` | 覆盖（name = 路径参数 = 目录名，不可改） |
| DELETE | `/skills/{name}` | 删除整个技能目录（204） |
| GET | `/skills/lint` | 目录级 lint（`{issues, enabled}`） |
| POST | `/skills/validate` | 草稿 lint（不落盘） |
| POST | `/skills/reload` | 重新扫描 |

**路由顺序陷阱**：`/lint`、`/validate`、`/reload` 必须声明在 `GET /{name}` **之前**，否则 `FastAPI` 会把
`lint` 当成 skill 名匹配到详情路由。

**写后必 `reload()`**：每次写操作成功后调用 `registry.reload()`，让 `list_meta()` / `catalog()`
立刻反映新目录（同一进程内），不必等下一轮对话。

**为什么未启用时是 400 而不是 409**：`skills_disabled` 在 M4 起就是 400（`/skills/reload` 的既有契约）；
本次 CRUD 沿用同一错误码，保持前端一处判断。对照 `memory_disabled` 是 409——两者不对称是历史约定，**刻意不改**。

**控制台侧**：技能面板 = 列表卡片（含该技能自身 lint issue）+ 编辑器覆盖层（`校验` 走 `/skills/validate`，
`保存` 走 `POST`/`PUT`）详见 [前端控制台设计 §8](frontend-console-design.md)。

### M7 交付明细：zip 上传安装 + 启停开关

**一、zip 上传安装（`pack.py`）**

控制台要能一次性导入「一整个技能包」，在既有 CRUD 之外补一个上传端点（纯库能力见 §5 安装器）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/skills/upload?overwrite=false` | 请求体是 **raw zip 字节**（**不是 multipart**，故意不引入 `python-multipart` 依赖）；201 返回 `{installed:[{name,files,overwritten}], skipped, warnings, skills}` |

`Content-Length` **先早拒**（超限直接 413，不必读完整个 body）。稳定错误码（HTTP 状态由 server 层定）：

| code | 语义 | HTTP |
|---|---|---|
| `zip_invalid` | 不是有效 zip / 解压损坏 | 400 |
| `zip_too_large` | 超限额（`details.limit` ∈ `zip_bytes`/`entries`/`total_bytes`/`file_bytes`/`skills`/`ratio`） | 413 |
| `zip_unsafe_entry` | 条目不安全（`details.reason` ∈ `absolute`/`drive`/`ads`/`traversal`/`bad_suffix`/`reserved_name`/`bad_char`/`too_deep`/`symlink`/`special_file`/`case_conflict`） | 422 |
| `zip_ambiguous` | 根层与一级子目录同时存在 `SKILL.md` | 422 |
| `zip_no_skill` | 包内没有找到 `SKILL.md` | 422 |
| `skill_invalid` | 包内技能 lint error / frontmatter `name` 非法 | 422 |
| `skill_conflict` | 已存在同名技能且未开覆盖（`details.conflicts`、`overwrite_hint`） | 409 |

**路由顺序陷阱**：`/upload` 也必须声明在 `GET /{name}` **之前**，否则 `upload` 会被当成技能名。
**写后必 `reload()`**：安装成功后同样 `registry.reload()`。

**二、启用 / 停用开关**

| 方法 | 路径 | 说明 |
|---|---|---|
| PATCH | `/skills/{name}` | body `{enabled: bool}`；只翻 `skills.disabled` 一个字段并热切换过滤面 |

**设计取舍：停用只过滤「曝光面」，不重建任何部件。**

- `scan()` 照旧**全扫**——管理端点与前端面板仍能看到停用项，否则「重新启用」无从下手；
- 只有 `names()` / `catalog()` / `load()` / `read_resource()` 排除停用项
  （`catalog()` **即使显式传入 name 也过滤**，防旁路）；`get()` **不过滤**（管理端点保存后取元信息还要用）；
- `load()` / `read_resource()` 命中停用抛 `SkillDisabled`（消息 `skill 已停用: <name>`，**刻意不复用
  `SkillNotFound`**：`skill.load` 的错误文本会直接回喂模型，必须能区分「没这个技能」与「这个技能被关了」）；
- 为什么不用重建：`PromptBuilder` 持有 registry 引用且每轮 `build()` 现调 `skills.catalog()`，
  `skill.load` / `skill.read` 工具也闭包同一 registry → `set_disabled()` 之后**下一轮 run 立即生效**，
  因此 `PATCH` **不重建部件、不要求「无活跃 run」**；
- 持久真源是 `SkillsConfig.disabled: list[str]`（进程级），`build_skills` 把它传给 `SkillRegistry(disabled=…)`。

**返回体补 `enabled`**：`GET /skills` 每项新增 `enabled` 且**包含停用项**（配合 `list_meta(include_disabled=True)`）；
`POST /skills`、`GET|PUT /skills/{name}`、`POST /skills/reload`、`POST /skills/upload` 的返回体也补了 `enabled`，
前端一处判断即可。

---

## 10. 决策记录

| 议题 | 结论 |
|---|---|
| 暴露方式 | **C**：catalog 注入 system prompt + `skill.load` / `skill.read` 两个只读工具（不提供 `skill.list`） |
| `allowed-tools` | **仅声明与校验**，不改变会话权限 |
| 搜索根目录 | **单一固定目录** `<项目根>/skills` |
| `skill lint` 形态 | **函数 + 命令行入口**（`harness/skill/lint.py`），不暴露成工具、不与 CLI 耦合 |
| `unknown-tool` 检查 | **默认不查**（工具集合随配置变化，硬查会误报），需显式传 `known_tools` |
| 不做 `duplicate-name` 检查 | `build_meta` 强制 `name` 等于目录名，同级目录不可能重名 → 该规则不可达 |
| 编码 | 按 **`utf-8-sig`** 读 SKILL.md，容忍 Windows 编辑器写入的 BOM |
| `lint` 不进 `__init__` | 否则 `python -m harness.skill.lint` 抛 runpy 警告，服务启动也白白加载 |
| 热重载形态 | **命令触发的 `POST /skills/reload` 端点**（不加 `watchdog` 依赖，不换会话） |
| 写入层位置 | **单独 `writer.py`**，不塞进 `registry.py`（读路径 vs 写路径职责不同）；同样不进 `__init__` |
| 写入前自校验 | 落盘前先 `parse_skill_md_text()` + `build_meta()` 解析一遍，杜绝写入 scan 读不回的坏文件 |
| 覆盖策略 | `write_skill(overwrite=…)` 显式开关；覆盖前备份 `SKILL.md.bak`，`.tmp` + `os.replace` 原子替换 |
| 草稿 lint | 在 `tempfile.mkdtemp()` 里复用 `lint_skills()`——不为了「检查」而在真实目录落盘 |
| `skills_disabled` 状态码 | **保持 400**（沿用 `/skills/reload` 既有契约），不为 CRUD 改成 409 |
| zip 包布局识别 | **自动识别**根层 / 一级子目录两种；两者并存 → `zip_ambiguous`，**不猜意图** |
| zip 安装原子性 | **两阶段**：同级 staging 解包 + 校验 + 算冲突，全通过才落盘；有冲突时 root 零写入 |
| zip 覆盖策略 | 旧 `SKILL.md` 备份为 `.bak`，逐文件 `os.replace`；**不删旧资源**，陈旧文件进 `warnings` |
| 上传体形态 | **raw zip 字节**，不用 multipart（避免引入 `python-multipart` 依赖）；`Content-Length` 先早拒 |
| 停用语义 | **只过滤曝光面**（`scan()` 全扫、`get()` 不过滤）；`load` / `read_resource` 抛 `SkillDisabled`，不复用 `SkillNotFound` |
| 停用实现 | **热切换集合**（`set_disabled()`），不重建部件；catalog 每轮现读，**下一轮 run 生效**，故 `PATCH` 不要求无活跃 run |
| `pack` 不进 `__init__` | 与 `lint` / `writer` 同理（依赖 `lint`），按需 `from harness.skill.pack import install_zip` |
| `safe_skill_dir` 公开 | 由 `_safe_dir` 提升为公开：`write_skill` / `delete_skill` / `pack.install_zip` 共用，维持唯一路径入口 |

### 仍待确认

- [ ] `skill.read` 是否支持列目录/通配，还是只允许精确读单文件？
- [ ] catalog 的 token 预算：`description` 截断长度与目录总量上限？
- [ ] skill 是否需要 `version` 与多版本共存？（当前倾向不需要）
