from __future__ import annotations

import pytest

from hall_auto.launch import LaunchError, launch_until_ready
from hall_auto.product import stop_main_process


pytestmark = [pytest.mark.launch, pytest.mark.smoke]


# `installed` 守卫**不在这里定义** —— 用 `tests/launch/conftest.py` 那份共享的。
# 2026-09-28 之前本模块自己抄了一份、另外 4 个模块没抄，于是新机上吐了 22 条 ERROR。


def test_p0_03_launch_window(cfg, installed):
    """P0-03 启动主窗口：标题正确且必备控件结构齐全"""
    result = launch_until_ready(cfg)
    assert cfg.display_name_contains in result.title
    for aid in cfg.launch.required_auto_ids:
        assert aid in result.structure_ids


def test_p0_04_ready_cold_starts(cfg, installed):
    """P0-04 冷启动稳定性：连续冷启动多次，首页结构每次完全一致"""
    seen: list[tuple[str, ...]] = []
    errors: list[str] = []
    try:
        for index in range(cfg.launch.cold_starts):
            try:
                result = launch_until_ready(cfg)
            except LaunchError as exc:
                errors.append(f"cold start {index + 1}: {exc}")
                continue
            assert cfg.display_name_contains in result.title
            seen.append(result.structure_ids)
        if errors:
            pytest.fail(" ; ".join(errors))
        assert len(seen) == cfg.launch.cold_starts
        assert len(set(seen)) == 1, f"三次启动结构不一致: {seen}"
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)
