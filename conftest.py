"""pytest 共享配置与 fixture。

本仓库的测试有两条并存的运行路径：

1. `python -m <module>`：各测试模块自带 `main()`，**不装 pytest 也能跑**（零依赖直跑）；
2. `pytest`：以本文件作为共享入口。

本文件只做两件事，不改动任何测试正文：

- **sys.path 兜底**：保证仓库根可导入（`import harness` / `import server`）。
- **进程级全局状态隔离**（autouse）：`harness/tool/sandbox.py`、`builtin/shell.py`、
  `builtin/net.py` 的策略是**进程级全局量**。手写 `main()` 遇到断言失败会立即停下，
  而 pytest 会继续跑后续用例——失败用例若在复位前中断，脏状态就会泄漏给后面的用例。
  这里在每个用例前后快照并复位（各用例自身的 `finally` 复位仍保留，本 fixture 是兜底）。
  沙箱另有**每调用覆盖**（`sandbox._override` ContextVar），用例若在 `use_sandbox()` 外
  手工 `set()` 过它就会泄漏给同线程后续用例，故一并复位。

测试全部**离线**，不触网。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---- sys.path 兜底 ----
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def isolate_process_globals():
    """用例前后复位进程级全局策略（sandbox / shell / net）与沙箱的每调用覆盖。"""
    from harness.tool import configure_sandbox, current_sandbox
    from harness.tool.builtin import net as net_mod
    from harness.tool.builtin import shell as shell_mod
    from harness.tool import sandbox as sandbox_mod

    # 先清掉可能泄漏的「每调用覆盖」，再快照进程级全局（否则会把泄漏值当成基准）
    sandbox_mod._override.set(None)
    sandbox = current_sandbox()
    shell = shell_mod.current_shell_policy()
    net = net_mod.current_net_policy()
    try:
        yield
    finally:
        sandbox_mod._override.set(None)
        configure_sandbox(
            enabled=sandbox.enabled,
            root=sandbox.root,
            write_paths=sandbox.write_paths,
            deny_paths=sandbox.deny_paths,
            readonly_mounts=sandbox.readonly_mounts,
            writable_mounts=sandbox.writable_mounts,
            extra_denied_domains=sandbox.extra_denied_domains,
        )
        shell_mod.configure_shell_policy(
            allowed_commands=list(shell.allowed_commands),
            deny_patterns=list(shell.deny_patterns),
        )
        net_mod.configure_net_policy(
            max_bytes=net.max_bytes,
            max_download_bytes=net.max_download_bytes,
        )


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """一个临时的「工作区」目录（可作沙箱 `root` 或 fs 工具的根）。

    需要在真实文件系统上造文件的用例（fs.* / sandbox / 配置）可用它，避免各自造目录。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace
