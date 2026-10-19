# 记忆系统设计文档

> 定位：**跨会话长期记忆（文件即记忆，注入 system prompt）** · 技术栈：**Python（零依赖，纯文件）** · 状态：**已实现 M1–M3**
> 相关：[会话管理设计文档](session-management-design.md) · [Agent Loop 设计文档](agent-loop-design.md) · [Skills 系统设计文档](skills-system-design.md) · [工具系统设计文档](agent-tool-system-design.md) · [配置系统设计文档](configuration-system-design.md) · [项目 TODO](../todo.md)

---

## 1. 定位与非目标

### 定位

补齐 [todo.md](../todo.md) P2 的「Memory」：**跨会话**的长期记忆。

仓库里已有两层记忆，本模块补第三层、也是最"长寿"的一层：

| 层 | 载体 | 生命周期 | 存什么 |
|---|---|---|---|
| 短期 | [harness/session](../harness/session/README.md) 四段分区 | 一个会话 | 本会话多轮消息 |
| 工作记忆 | [todo.write](../harness/tool/builtin/todo.py) | 一个会话（进程内） | 本轮待办 |
| **长期（本模块）** | **`MEMORY.md` 一个 Markdown 文件** | **跨会话、跨进程** | **偏好 / 稳定事实 / 结论与坑** |

核心主张：**记忆就该是人能读能改的一份文件**，而不是一个数据库。

```
MEMORY.md ──读取──▶ PromptBuilder.system_prompt() ──▶ system 消息 ──▶ 模型
    ▲
    └── memory.save / memory.forget（模型）  ／  直接编辑（人）
```

理由（与 [SKILL.md](../harness/skill/README.md)（渐进式披露）、`AGENTS.md` 同源）：

1. **模型一定"记得"**——记忆在 system 里，不依赖模型会不会想起来去检索。
2. **可审计、可手改、可 git diff**——记忆是人类该能审查的东西。
3. **零依赖零服务**——不需要 embedding、不需要数据库、不需要索引重建。
4. **和仓库既有模式一致**——`SKILL.md`、`AGENTS.md` 都是"文件即配置"。

### 为什么不上向量库 / 全文检索

| | `MEMORY.md` | SQLite + FTS5 |
|---|---|---|
| 依赖 | 零，纯文件 | 零（标准库），但要处理分词 / 排序 |
| 模型是否一定记得 | **一定**（进 system） | 取决于它会不会去搜 |
| 可手改 | 天生 | 要写查询 |
| 记忆量大时 | 全量注入，token 线性涨 | 按需检索，可扩到几千条 |
| 同一事实的更新 | 靠人去合并，容易重复 | upsert 天然去重 |

结论：**md 装"必须始终记得的那几十条"，不做检索。** 真涨到几百条时再加检索层
（那时也只把「核心」一节注入 system，其余留给工具按需取）——那时再加，不是现在。

> 顺带记一条实测结论（将来若上 FTS5 会用到）：SQLite 默认的 `unicode61` 分词器对中文无效
> （整句被当成一个 token，`MATCH '中文回答'` 命中 0 条），必须换 `trigram`。

### 非目标（本期不做）

- 不做向量 / embedding / 语义检索。
- 不做全量历史归档（那是 `harness/session` 与 `read_spans` 的职责）。
- 不做黑箱自动学习：**写入必须可见、可审计、可删**。
- 不做 `memory.search`（内容已在 system 里，多余）。
- 不做跨用户 / 多租户隔离（当前单进程单用户的部署形态）。
- 不做记忆的自动过期（容易变成静默丢数据，宁可让上限报错逼人整理）。

---

## 2. 三个必须解决的真问题

### 2.1 前缀缓存：会击穿，但只击穿一次

服务层每轮都重算 system（[PromptBuilder.system_prompt()](../harness/agent/prompt.py) + session 固定区）。
所以关键不是"重算会不会破坏缓存"，而是**输入是否确定性**：

| 情形 | 后果 |
|---|---|
| 文件内容没变 | 每轮拼出**同样的字节** → 缓存照样命中 ✅ |
| 会话中途 `memory.save` 改了文件 | 下一轮 system 变 → **击穿一次**，之后重新稳定 ✅（低频事件，可接受） |
| 文件里写了「每轮生成的 `now()` / 随机 id」 | 每轮都不同 → **永久击穿** ❌ |

> **日期可以写进文件**（写一次就固化成字符串，之后不再变），**每轮现算的时间戳不行**。
> 这与 [会话管理设计文档](session-management-design.md) 里「禁止把时间戳、随机数写进 system」是同一条规矩。

想要"会话内绝不生效新记忆"的更严做法，是把快照固化进 session 固定区（建会话时读一次）。
本期**不做**：收益是省一次击穿，代价是多一份状态。

### 2.2 上限：拒绝写入，而不是静默截断

文件必须封顶（默认 4000 字符），否则每轮都付 token 且越滚越贵。

超限时 `memory.save` **报错**，要求先整理（合并/删除）——**静默丢最旧的一条，比写不进去危险得多**：
记忆被丢掉是无声的，模型和人都不会察觉。

### 2.3 并发写：整体文件 + 原子替换

md 是**整体文件**，不能像 session 的 JSONL 那样纯 append。多会话同时 save 时
「读-改-写」必须串行：进程内 `threading.Lock` + 写临时文件再 `os.replace` 原子替换。

> **诚实的限制**：`threading.Lock` 只在单进程内有效。多副本部署要换文件锁——
> 但当前服务是单进程单副本（见 [api-service-design.md](api-service-design.md) §1），先如实标注。

---

## 3. 分层与目录

```
harness/memory/
├─ README.md      # 模块文档
├─ __init__.py    # 公共导出
├─ schema.py      # MemoryEntry / 解析与渲染（纯函数，零 IO）
├─ store.py       # MemoryStore：读 / 写 / 去重 / 上限 / 原子替换 / render()
├─ tools.py       # register_memory_tools(registry, store) → memory.save / memory.forget
└─ test/          # 全离线（临时文件）

MEMORY.md         # 记忆文件本体（项目根，像 AGENTS.md 一样显眼、可提交、可手改）
```

依赖方向：`harness/memory → harness/tool`（单向，只用到 `ToolRegistry` / `ToolSpec`）；
`harness/agent` 不依赖 memory（`PromptBuilder` 只接收一个 `str | None` 或一个 loader 回调）。

---

## 4. 记忆文件契约

### 4.1 格式

```markdown
# 长期记忆

## 用户偏好
- 回复用中文，不用 emoji
- 不要引入向量库（嫌麻烦）

## 项目事实
- 项目根 c:\Users\Lenovo\Desktop\agent，Python + FastAPI

## 结论与坑
- SQLite 默认 unicode61 分词器对中文无效，FTS5 要用 trigram
```

约定：

- `# 标题` 是文件标题，**不注入**（渲染时去掉）。
- `## 分节`：分节标题 + 若干 `- ` 条目；只认这两级，其余原样保留但按条目处理。
- **一条记忆 = 一行 bullet**。这是去重与删除的粒度。
- 分节名自由，默认落到「结论与坑」。

### 4.2 去重

按**规范化后的行文本**去重（折叠空白、去首尾）：

- 已存在 → 不追加，返回「已存在」并更新它的位置（移到该节末尾，体现"最近又提起"）。
- 不存在 → 追加到指定分节末尾。

### 4.3 上限

`max_chars`（默认 4000）**在写入前判定**：`渲染后的总长 > max_chars` 就**拒绝**并回报
「记忆已满（N/4000 字符），请先合并或删除（可用 memory.forget）」。

### 4.4 原子写

```
读文件 → 内存改 → 写 MEMORY.md.tmp → os.replace(tmp, MEMORY.md)
```

`os.replace` 在同一文件系统上是原子的，因此不会出现"写一半被读到"的半文件。

---

## 5. M1：MemoryStore + 工具

### 5.1 MemoryStore

```python
# harness/memory/store.py
class MemoryStore:
    def __init__(self, path: Path | str, *, max_chars: int = 4000) -> None: ...

    def render(self) -> str:
        """渲染成注入 system 的文本（去掉一级标题；空文件返回 ""）。"""

    def load(self) -> list[MemoryEntry]:
        """读并解析（文件不存在 → 空列表，不报错）。"""

    def save(self, text: str, *, section: str = DEFAULT_SECTION) -> str:
        """去重后追加到分节；超上限抛 MemoryFull。返回给模型看的结果文本。"""

    def forget(self, text: str) -> str:
        """按规范化文本匹配删除（子串匹配也接受）；没命中如实回报。"""
```

错误类型（`harness/memory/schema.py`）：`MemoryError`（基类）/ `MemoryFull` / `MemoryEntryInvalid`。
`MemoryFull` 的实现方式与 `ConfigError` 一致：带 `path` + `message`，便于原样回喂模型。

### 5.2 工具

`register_memory_tools(registry, store)` 注册两个工具（写法照 [register_skill_tools](../harness/skill/tools.py)）：

```python
ToolSpec(name="memory.save", description="把值得长期记住的事实/偏好/结论写入长期记忆…",
         side_effect="write", scopes=("memory:write",), tags=("memory",))
ToolSpec(name="memory.forget", description="删除一条不再正确的长期记忆…",
         side_effect="write", scopes=("memory:write",), tags=("memory",))
```

要点：

- **两个都是 `write`**，不是 `destructive`（不触发强制审批），但要 scopes，受既有的
  `tools.enabled/disabled` 开关约束（想做只读模式就把 `memory.*` 关掉）。
- 工具异常会由 Executor 转成结构化错误回喂模型（`MemoryFull` 的 message 会直接进 tool 结果），
  **模型能看到"满了"并自行整理**，不需要人介入。
- 名字用 `memory.` 前缀，与 `skill.` / `fs.` / `shell.` 一致。

---

## 6. M2：注入 system prompt

### 6.1 注入点

[PromptBuilder](../harness/agent/prompt.py) 加一节，顺序固定为 **base → skill 目录 → 长期记忆**：

```python
class PromptBuilder:
    def __init__(self, *, base_instructions="", skills=None,
                 memory: MemoryStore | None = None,      # ← 新增
                 include_skill_catalog=True, ...): ...

    def system_prompt(self) -> str:
        parts = [...]                       # 现有：base + skill 目录
        if self.memory is not None:
            memory = self.memory.render()
            if memory:
                parts.append(f"{MEMORY_SECTION_TITLE}\n{memory}")   # "## 长期记忆"
        return "\n\n".join(parts)
```

**顺序不能变**：顺序变了同样是缓存击穿。空记忆**不产生空节**（不留 `## 长期记忆\n` 这种残渣）。

### 6.2 每轮读文件还是读快照

`render()` 每次调用都读文件（`store` 内部按 `mtime` 缓存解析结果，避免每轮都做 IO 与解析）。

- 内容没变 → 命中 mtime 缓存，返回**同一个字符串** ✅
- 内容变了 → 重新解析（一次击穿，见 §2.1）

### 6.3 配置与装配

```yaml
memory:
  enabled: true
  path: MEMORY.md        # 相对项目根；文件不存在则视为空记忆，首次 save 时创建
  max_chars: 4000        # 超限拒绝写入（不静默截断）
```

```python
# harness/config/build.py
def build_memory(config: HarnessConfig) -> MemoryStore | None:
    """`memory.enabled: false` 或 path 为空白 → None（关闭）。"""
```

装配链：

```
build_state(config):
    memory = build_memory(config)
    registry = build_registry(config, skills, memory)      # ← 注册 memory.* 工具（受工具开关约束）
    prompt   = build_prompt(skills, memory)                # ← 注入 system
```

`build_registry` 多一个可选参数 `memory`，照 `skills` 的既有做法：
先注册到临时表，再用 `is_tool_allowed` 搬进正式表。

### 6.4 多实例一致性

`MemoryStore` 是**进程内单例**（由 `build_state` 创建一次，注入 registry 与 prompt），
因此工具写完之后，下一轮 `render()` 必定看到新内容——不需要额外的刷新机制。

---

## 7. M3：压缩时顺带抽取（"一定情况下抽取"）

### 7.1 为什么挑"压缩"这个时刻

会话压缩（[compact.py](../harness/session/compact.py)）本来就是**"上下文要丢一批消息"**的时刻，
语义上正好是「从即将被丢掉的东西里，把值得留下的捞出来」。而且压缩已经在调一次模型
（`LLMCompressor`），**把抽取并进同一个 prompt，不新增任何模型调用**。

### 7.2 做法

`LLMCompressor` 的 prompt 里追加一段要求，让它输出两段：摘要 + 候选记忆。

```
…（原有摘要指令）

另外：如果这段历史里出现了**值得跨会话长期记住**的内容（用户偏好、稳定事实、结论与踩过的坑），
在摘要之后另起一段逐行列出候选，每行以「记忆：」开头；没有就只写一行「记忆：无」。
不要写一次性的对话内容，也不要重复摘要里的一般信息。
```

新增 `LLMCompressor.compress_and_extract(messages) -> CompressionResult(summary, memories)`，
输出用纯函数 `split_summary_and_memories()` 切分（`记忆：` 开头的行归记忆，其余归摘要）。

**取舍**：改 `compress()` 的返回类型会波及 `Compressor` Protocol。所以走**加法**：Protocol 不变，只在 `LLMCompressor` 上加可选能力；
`SessionManager` 用 `getattr(compressor, "compress_and_extract", None)` **探测**——
探测不到就走原来的 `compress()`，行为与今天完全一致。

### 7.3 抽取结果怎么落

候选**不直接写文件**，而是交给 `SessionManager(on_memory=...)` 回调；落点由装配决定：

| `memory.auto_extract` | 落点 |
|---|---|
| `false`（默认） | 只记一条 `memory.candidate` span（于是能在 `/runs/{id}/trace` 里看到候选，但不污染记忆） |
| `true` | 先记 span，再 `MemoryStore.save()` 写入（仍走去重与上限） |

**自动写长期记忆是"无人看守的污染源"**，所以默认关闭，让它在真实使用中被观察过再开。

> **成本说明**：自动抽取需要一个**能调模型的压缩器**。压缩器现已固定为 `LLMCompressor`
> （`PlaceholderCompressor` 已删除），所以压缩本身就会调模型；`auto_extract: false` 时
> **同一次调用只产摘要**，`true` 时才**顺带抽取**候选记忆（详见 §9 决策记录）。

---

## 8. 与 server / config 的关系

```
harness/config/build.py   build_memory / build_prompt / build_registry / build_session
        ↓
server/state.py build_state(config)  →  AppState(memory=..., prompt=..., registry=..., manager=...)
        ↓
POST /sessions/{id}/turns
        ↓  PromptBuilder.system_prompt() 含「## 长期记忆」
模型 ──memory.save──▶ MemoryStore ──原子写──▶ MEMORY.md
        ▲                                      │
        └────────── 下一轮 render() 读到 ◀──────┘
压缩 ──compress_and_extract──▶ 候选记忆 ──on_memory──▶ 只记 span（默认）/ 写入（auto_extract）
```

配置新增一节 `memory`，`server/` 侧**不需要新增任何端点**——记忆的读写全部发生在
模型与文件之间（外加人可以手改文件），候选抽取走既有的 trace 出口。

---

## 9. 里程碑

| 里程碑 | 内容 | 交付 |
|---|---|---|
| M1 | `schema.py` + `store.py`（解析 / 去重 / 上限 / 原子写 / `render`）+ `tools.py`（`memory.save` / `memory.forget`） | ✅ **16 条**单测：解析与渲染、手写内容保留、去重与移位、超限报错且文件不变、原子写不留临时文件、并发写不丢条目、forget 精确/子串/未命中、缺失文件、工具接线与 scopes、`MemoryFull` 结构化回喂 |
| M2 | `PromptBuilder` 加「长期记忆」节 + `MemoryConfig` + `build_memory` / `build_prompt` / `build_registry` + server 接线 | ✅ agent 3 条 + config 4 条 + server 2 条：顺序与空节、字节稳定、工具开关、记忆进 system、`memory.save` 经服务写盘、`/tools` 自省 |
| M3 | `LLMCompressor.compress_and_extract` + `split_summary_and_memories` + `SessionManager(on_memory=...)` 探测调用；默认只记 span 不写入 | ✅ session 4 条 + config 1 条：切分纯函数、一次调用、无能力压缩器不抽取、落点异常不中断压缩、`auto_extract` 决定压缩器 |

全量测试：tool 35、skill 15、llm 27、agent 38、session 25、**memory 16**、config 35、server 21 = **212 条，全部离线**。

---

## 10. 兼容性

- `PromptBuilder` 新增的是**可选关键字参数**（`memory=None`）——现有调用全部不变；
  不配 memory 时 system 与今天**字节完全一致**。
- `SessionManager` / `Compressor` 的既有契约不变：`on_memory` 是可选参数，
  抽取走 `getattr(compressor, "compress_and_extract", None)` 探测。
- `registry` 里多出 `memory.save` / `memory.forget` 两个名字：它们**同样受
  `tools.enabled/disabled` 约束**，因此老配置想保持"工具数不变"只需 `disabled: ["memory.*"]`。
- `MEMORY.md` **不存在**时行为与今天完全一致（空记忆 → 不注入）。
- 新增配置节 `memory`，缺省即"开启 + 根目录 `MEMORY.md` + 4000 字符 + 不自动抽取"。
- **不新增依赖**：只用标准库（`os` / `threading` / `pathlib`）。

---

## 11. 决策记录

| 议题 | 结论 |
|---|---|
| 记忆载体 | **一个 Markdown 文件**（人可读可改可 diff），不上数据库 / 向量库 |
| 为什么不上检索 | md 装"必须始终记得的几十条"，进 system 就一定生效；量大了再加"核心注入 + 其余检索" |
| 注入位置 | `PromptBuilder` 的 system，顺序固定 base → skill → memory |
| 缓存策略 | 不固化快照；内容不变则字节不变。**文件里禁止每轮现算的时间戳 / 随机数** |
| 超上限 | **拒绝写入并报错**，不静默截断（静默丢记忆是无声故障） |
| 并发写 | 进程内 `threading.Lock` + 临时文件 `os.replace` 原子替换；多副本需换文件锁（当前不适用） |
| 去重粒度 | 一条记忆 = 一行 bullet，按规范化文本去重（重复 save 只移动到目标节末尾） |
| 手写内容 | 自定义一级标题、非条目的散行都**原样保留**；空行会被规范化掉（每次写入重排成规范形态） |
| 删除 | 必须有 `memory.forget`——不可删的记忆会变成永久污染 |
| 权限 | 两个工具都是 `write` + `scopes=("memory:write",)`，受既有工具开关约束 |
| 自动抽取时机 | **压缩时顺带抽**（复用已有的一次模型调用，不新增成本） |
| 自动抽取的实现 | **加法**：`Compressor` Protocol 不动，`LLMCompressor` 加 `compress_and_extract`，`SessionManager` 探测 |
| 自动写入 | **默认关**（`auto_extract: false`），只记 `memory.candidate` span；先观察再开 |
| 压缩器 | 只有 `auto_extract: true` 才把压缩器换成 `LLMCompressor`——不做静默的额外模型调用 |
| 候选的可见性 | 复用观测层：记一条 span，于是在 `traces.jsonl` / `/runs/{id}/trace` 里可见，不引入 logging 约定 |
| 不做 `memory.search` | 内容已在 system 里，多余 |

### 仍待确认

- [ ] **压缩器可配置**：现在只有 `auto_extract` 这个开关间接决定用哪个压缩器；
      也许该给 `session` 加一节 `compressor: placeholder | llm`（开了就该有更好的摘要，而不只是为了记忆）。
- [ ] 是否需要**多记忆文件**（如 `MEMORY.md` + 项目级 `docs/` 约定文件分开）？
- [ ] 抽取出来的候选记忆，长期看是否需要一个 `memory.pending` 的待审列表（人来批准）？
- [ ] 记忆量真的到几百条时：是"核心节注入 + 其余 FTS5 检索"，还是切分成多个文件？
- [ ] `MEMORY.md` 要不要进 `.gitignore`（含个人偏好，未必该提交）？
