# memory — 跨会话长期记忆

> 状态：**已实现（M1–M3）**。设计见 [docs/memory-system-design.md](../../docs/memory-system-design.md)。

## 职责

补齐三层记忆里最长寿的一层：**跨会话、跨进程**的偏好 / 事实 / 结论。

| 层 | 载体 | 生命周期 |
|---|---|---|
| 短期 | [harness/session](../session/README.md) 四段分区 | 一个会话 |
| 工作记忆 | [todo.write](../tool/builtin/todo.py) | 一个会话（进程内） |
| **长期（本模块）** | **`MEMORY.md` 一个 Markdown 文件** | **跨会话、跨进程** |

核心主张：**记忆就该是人能读能改的一份文件**——不上数据库、不上向量库。
内容是**全量注入 system prompt** 的（它就是"必须始终记得的那几十条"），
所以模型一定"记得"，不依赖它会不会想起来去检索。

```
MEMORY.md ──读取──▶ PromptBuilder.system_prompt() ──▶ system 消息 ──▶ 模型
    ▲
    └── memory.save / memory.forget（模型） ／ 直接编辑（人）
```

## 目录结构

```
harness/memory/
├─ README.md      # 本文档
├─ __init__.py    # 公共导出
├─ schema.py      # MemoryDoc / Section + 解析与渲染（纯函数，零 IO）
├─ store.py       # MemoryStore：读 / 写 / 去重 / 上限 / 原子替换 / render() / replace()
├─ tools.py       # register_memory_tools(registry, store)
└─ test/          # 17 条全离线测试
```

## 快速上手

```python
from harness.memory import MemoryStore, register_memory_tools
from harness.tool import ToolRegistry
from harness.agent import PromptBuilder

store = MemoryStore("MEMORY.md", max_chars=4000)

store.save("回复用中文，不用 emoji", section="用户偏好")   # 去重后追加
store.forget("不要 emoji")                               # 精确匹配 → 子串匹配
store.replace("# 长期记忆\n\n## 用户偏好\n- 回复用中文")   # 整文替换（控制台编辑用）
store.entries()                                          # ['回复用中文，不用 emoji']
store.render()                                           # 注入 system 的文本（空记忆 → ""）

registry = ToolRegistry()
register_memory_tools(registry, store)                   # memory.save / memory.forget

prompt = PromptBuilder(skills=skills, memory=store)      # system 里多一节「## 长期记忆」
```

> `MemoryStore.replace(text)` 供控制台 `PUT /memory` 用：**整文替换**——解析 → 校验上限 → 原子替换，
> 失败则文件不变（超限抛 `MemoryFull`）。

## 文件格式

```markdown
# 长期记忆

## 用户偏好
- 回复用中文，不用 emoji

## 项目事实
- 项目根 c:\Users\Lenovo\Desktop\agent，Python + FastAPI
```

- 一级标题是文档名，**不注入**（渲染进 system 时去掉，其余标题降一级）；
- `## 分节` 下每行 `- ` 是**一条记忆**（去重与删除的粒度就是这一行）；
- **手写内容保留**：自定义一级标题、非条目的散行都原样留着；只有空行会在每次写入时被规范化掉
  （等于每次 save 都会把文件重排成规范形态）。

## 三个关键性质

| 性质 | 做法 | 为什么 |
|---|---|---|
| **缓存友好** | 渲染确定性（无时间戳/随机数）+ 按 `mtime_ns` 缓存解析结果 | 内容没变 → 每轮拼出**同样的字节**，不击穿 KV 前缀缓存 |
| **不静默丢** | 超 `max_chars` 时 `MemoryFull` **拒绝写入**，错误照常回喂模型 | 静默截断丢记忆是**无声故障**；报错能让模型/人来整理 |
| **写得安全** | `threading.Lock` + 临时文件 `os.replace` 原子替换 | md 是整体文件，不能像 JSONL 那样纯 append；不留半文件 |

> `threading.Lock` 只在**单进程**内有效。多副本部署要换文件锁——当前服务是单进程单副本。

## 接入方式（由装配期完成）

```python
# harness/config/build.py
memory   = build_memory(config)                        # memory.enabled / path / max_chars
registry = build_registry(config, skills, memory)      # 注册 memory.*，同样受工具开关约束
prompt   = build_prompt(skills, memory)                # 注入 system
manager  = build_session(config, memory, tracer, provider)   # 压缩时抽取（见下）
```

```yaml
memory:
  enabled: true
  path: MEMORY.md         # 文件不存在视为空记忆，首次 save 时创建
  max_chars: 4000         # 超限拒绝写入（不静默截断）
  auto_extract: false     # 压缩时抽到的候选记忆是否直接写入（默认只记 span）
```

## 权限

两个工具都声明 `scopes=("memory:write",)` 且 `side_effect="write"`（不是 `destructive`，
不触发强制审批）。所以调 `memory.save` 需要 `Context(scopes={"memory:write"})` ——
服务层就是请求体里的 `scopes: ["memory:write"]`（调试前端有对应勾选框）。
想关掉记忆工具：`tools.disabled: ["memory.*"]`。

## 压缩时抽取（M3）

`SessionManager` 在压缩那一刻顺带抽候选记忆——**复用同一次模型调用，不新增成本**。
落点由 `memory.auto_extract` 决定：默认只记一条 `memory.candidate` span
（在 `/runs/{id}/trace` 里可见），开了才真正写盘。

需要能调模型的压缩器（`LLMCompressor`，**现在唯一的压缩实现**）。压缩本身就会调模型；
`auto_extract: false` 时同一次调用只产摘要，`true` 时才顺带抽取候选记忆，不会额外增加调用次数。

## 测试

```bash
python -m harness.memory.test.test_memory   # 或 pytest harness/memory/test
```

17 条测试**全离线**（只碰临时文件，无网络无模型），覆盖：
解析与渲染往返、标题降级、手写内容保留、换行折叠成一条、去重与移位、
超限报错且文件不变、渲染字节稳定与不留临时文件、8 线程并发写不丢条目、
forget 精确/子串/未命中/删空分节、缺失文件、**`replace()` 整文替换**、工具接线与 scopes、
`MemoryFull` 被 Executor 转成结构化错误回喂模型。
