"""全测试共享 fixture.

常用模型清单（v0.19.0）的 engine.__init__ 会读 <HERMES_HOME>/feishu_streaming_model_cycle.json
——不隔离的话，开发者本机一旦真实使用过该功能，全量单测就会被本机状态污染
（_model_cycle 被真实 favorites 顶替）。默认路径统一指到 tmp；显式传 home 的
Config(home) 语义保持不变（profile 隔离测试依赖它）。
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolated_model_cycle_file(tmp_path: Any, monkeypatch: Any) -> None:
    import plugin._vendor.config as config_mod

    real = config_mod._config_path
    monkeypatch.setattr(
        config_mod, "_config_path",
        lambda home=None: real(home) if home is not None else tmp_path / "config.yaml")
