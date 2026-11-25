"""本地媒体路径工具（不依赖 lark-oapi）.

单独成模块：adapter 顶层要用这两个函数，而 ``feishu`` 模块顶层 ``import lark_oapi``
——实测占插件包导入耗时的 82%（Mac 热缓存 0.70s/0.85s，Windows 冷缓存放大到
数秒）。插件 register() 必须赶在网关平台扫描的 deadline 前落地，导入慢了就会
「No adapter available for feishu」（2026-11-19 13:29 梦珊机升级后首次启动）。
"""

from __future__ import annotations

import os
from urllib.parse import unquote

__all__ = ["local_file_name", "local_file_path"]


def local_file_path(path: str) -> str:
    """file:// URL → 本地路径（quote 解码）；裸路径原样返回.

    gateway run_notifications 以 f"file://{quote(p)}" 传本地媒体——Windows
    盘符路径必含 %3A%5C，含空格/中文路径必被 quote，不解码读取必失败
    （2026-11-17 梦珊机话题群文件投递事故链之一）。
    """
    if path.startswith("file://"):
        return unquote(path[len("file://"):])
    return path


def local_file_name(path: str, override: str | None = None) -> str:
    """展示用文件名：显式指定优先，否则取（解码后）路径 basename."""
    if override:
        return override
    return os.path.basename(local_file_path(path))
