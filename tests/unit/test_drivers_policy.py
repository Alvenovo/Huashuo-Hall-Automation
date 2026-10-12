# -*- coding: utf-8 -*-
"""锁 `hall_auto/drivers.py` 的驱动模板行为（2026-10-12）。

## 为什么要有它

驱动模板是「改用例不动脚本」的那一层：用例文件里选模板 + 给期望值，模板负责断言。
模板写松了，**用例文件里写错期望值也不会红** —— 那就等于把假绿搬进了数据层。
所以这里用假页面把三种断言形态都钉住。

## 边界（顺便钉进测试）

驱动只覆盖「断言什么」；「打开哪个页面、点什么按钮」永远要代码。
所以 `PAGES` 里没注册的页面必须**报错**，不能悄悄放过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto import drivers  # noqa: E402


@pytest.fixture
def fake_page(monkeypatch):
    """注册一个假页面 `fakepage`，控件现状由测试自己给（不碰 pywinauto）。"""

    def _install(present: dict):
        monkeypatch.setitem(drivers.PAGES, "fakepage", ("假页面", "unused:unused"))
        monkeypatch.setitem(drivers._PAGE_CACHE, "fakepage", lambda handle: present)

    return _install


def test_controls_present_passes_when_all_there(fake_page):
    fake_page({"a": True, "b": True})
    drivers.controls_present("fakepage", 1, {"id": "X", "expect": ["a", "b"]})


def test_controls_present_reports_missing(fake_page):
    fake_page({"a": True, "b": False})
    with pytest.raises(AssertionError) as exc:
        drivers.controls_present("fakepage", 1, {"id": "X", "expect": ["a", "b"]})
    assert "b" in str(exc.value), "报错要点名缺哪个控件"


def test_controls_present_reports_unexpected(fake_page):
    """`expect_absent`：不该出现的控件出现了必须红 —— 这是「版本升级多塞了控件」的哨兵。"""
    fake_page({"a": True, "invite": True})
    with pytest.raises(AssertionError) as exc:
        drivers.controls_present("fakepage", 1,
                                 {"id": "X", "expect": ["a"], "expect_absent": ["invite"]})
    assert "invite" in str(exc.value)


def test_controls_present_exact_rejects_extra(fake_page):
    """`expect_exact`：集合必须相等 —— 防「期望值写少了、实际多了也不报」。"""
    fake_page({"a": True, "b": True})
    drivers.controls_present("fakepage", 1, {"id": "X", "expect": ["a", "b"], "expect_exact": True})
    with pytest.raises(AssertionError) as exc:
        drivers.controls_present("fakepage", 1,
                                 {"id": "X", "expect": ["a"], "expect_exact": True})
    assert "b" in str(exc.value)


def test_controls_present_without_any_expectation_is_an_error(fake_page):
    """既没 expect 也没 expect_absent = 什么都没断言 —— 必须报错，不能「空跑也算过」。"""
    fake_page({"a": True})
    with pytest.raises(AssertionError):
        drivers.controls_present("fakepage", 1, {"id": "X"})


def test_unknown_page_is_an_error():
    """没注册的页面必须报错 —— 别静默放过（那就是「改了用例却没真跑」）。"""
    with pytest.raises(AssertionError) as exc:
        drivers.controls_present("不存在的页面", 1, {"id": "X", "expect": ["a"]})
    assert "未知页面" in str(exc.value)


def test_unknown_driver_is_an_error():
    with pytest.raises(AssertionError) as exc:
        drivers.run("不存在的模板", "register", 1, {"id": "X"})
    assert "未知驱动模板" in str(exc.value)


def test_registered_drivers_and_pages_are_described():
    """`--list-drivers` 靠这两个函数 —— 空列表会让那个命令看起来「没有模板可用」。"""
    assert "controls_present" in [n for n, _ in drivers.describe()]
    assert "register" in [n for n, _ in drivers.describe_pages()]


def test_importing_drivers_does_not_pull_the_ui_stack():
    """**关键**：`drivers.py` 必须能在**没有 pywinauto** 的环境 import ——
    导出器 `tools/cases_export.py` 与单测守卫都 import 它。
    所以页面助手是 `"模块:函数名"` 字符串、用时才 import（延迟解析）。
    """
    src = (REPO_ROOT / "hall_auto" / "drivers.py").read_text(encoding="utf-8")
    assert "from hall_auto.register import" not in src, "别把页面助手改成直接 import"
    assert "importlib.import_module" in src, "页面助手要延迟解析"
