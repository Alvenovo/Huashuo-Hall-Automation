from __future__ import annotations

import pytest

from hall_auto import drivers
from hall_auto.launch import start_fresh, wait_main_window, wait_until_ready
from hall_auto.product import stop_main_process
from hall_auto.settings import (
    DARK_LUMINANCE_MAX,
    ensure_settings,
    main_luminance,
    open_settings,
    set_theme,
    theme_selected,
)


class Session:
    """重启后 main/pid 会换，用例改这里的引用，收尾还原才拿得到活的句柄。"""

    def __init__(self, main):
        self.main = main
        self.pid = main.element_info.process_id
        self.original: str | None = None

    def restart(self, cfg) -> None:
        stop_main_process(cfg.timeouts.process_stop_sec)
        app = start_fresh(cfg)
        self.main = wait_main_window(app, cfg)
        wait_until_ready(cfg, self.main)
        self.pid = open_settings(self.main)


@pytest.fixture(scope="module")
def session(cfg, installed):
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    holder = Session(main)
    holder.pid = open_settings(main)
    holder.original = theme_selected(holder.pid)
    yield holder
    try:
        if holder.original:
            set_theme(ensure_settings(holder.main), holder.original)
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.settings
def test_cases_settings_theme(session, case):
    """**设置页·主题三档的通用用例函数** —— 承接用例文件里所有 `page: settings_theme` 的用例。

    用例只要写 `driver` + `page: settings_theme`（不用给 `ref`）就会挂到这里。
    ⚠️ 必须留在本文件里 —— `session` 是模块级 fixture。
    """
    drivers.run(case["driver"], case["page"], session.pid, case)


@pytest.mark.settings
def test_theme_switch_applies_and_survives_restart(cfg, session):
    """P1-D 主题切换：切深色主窗真变暗且重启后保持，再切浅色真变亮、深浅亮度差达标"""
    set_theme(session.pid, "深色")
    assert theme_selected(session.pid) == "深色"
    dark = main_luminance(session.main)
    assert dark < DARK_LUMINANCE_MAX, f"选了深色但主窗口还是亮的：亮度 {dark:.1f}"

    session.restart(cfg)
    assert theme_selected(session.pid) == "深色", "重启后主题没保持住"
    assert main_luminance(session.main) < DARK_LUMINANCE_MAX, "重启后外观没跟着回到深色"

    set_theme(session.pid, "浅色")
    light = main_luminance(session.main)
    assert theme_selected(session.pid) == "浅色"
    assert light > DARK_LUMINANCE_MAX, f"选了浅色但主窗口还是暗的：亮度 {light:.1f}"
    assert light - dark > 50, f"深浅色外观差别太小，怀疑没真换肤：浅 {light:.1f} / 深 {dark:.1f}"
