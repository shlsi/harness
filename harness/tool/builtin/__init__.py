"""内置工具集。导入本包即完成内置工具注册（写入 default_registry）。"""

from . import clock, code, fs, interaction, net, shell, todo

__all__ = ["clock", "code", "fs", "interaction", "net", "shell", "todo"]
