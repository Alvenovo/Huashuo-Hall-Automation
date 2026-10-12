# -*- coding: utf-8 -*-
"""驱动模板库 —— 把「**同一种断言**在不同数据上重复」抽成模板，用例只给参数。

## 它解决组长那句「不要是死的脚本，要能根据用例改动，脚本能自己识别」

`cases/cases.yaml` 里 `tier: data` 的用例可以指定一个 `driver`（模板名）+ `params`。
**同一类用例增删改，只改用例文件，不碰 Python。** 加一条同类型的用例也一样。

## ⚠️ 边界（**这一层必须说清，别假装能绕开**）

驱动能覆盖的只有「**断言什么**」：

| 能数据化 | 不能数据化（永远要代码） |
| --- | --- |
| 期望哪些控件存在 / 不存在 | **打开哪个页面、点什么按钮** |
| 期望的提示状态、命中条数 | 控件怎么定位（aid 变了要改代码或配置） |
| 参数组合（密码、关键词…） | 提权、装卸、重启客户端这些动作 |

所以**加一条「新页面 / 新交互」的用例仍然要写代码**；
但**同类型的新用例、以及期望值的调整，只改 YAML**。
这条边界写进了《用例数据化方案.md》第三节，不打算绕。
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 页面注册表：`page` 名 -> 取「该页面控件现状」的函数
#
# ⚠️ 这里存的是 **`"模块:函数名"` 字符串**，用时才 import ——
# 因为那些模块会拉 pywinauto，而本文件还要被 `tools/cases_export.py`
# 和单测守卫（都不该依赖 UI 栈）import。**延迟解析是刻意的，别改成直接 import。**
#
# 每个函数返回 `dict[str, Any]`，值 truthy = 控件在。
# **新增一个页面**才需要在这里加一行 —— 这是交互层，属于代码，不属于数据。
# ---------------------------------------------------------------------------
PAGES: dict[str, tuple[str, str]] = {
    "register": ("注册页", "hall_auto.register:register_controls"),
    "settings_theme": ("设置页·主题三档", "hall_auto.settings:theme_state"),
}

_PAGE_CACHE: dict[str, Callable[[int], dict[str, Any]]] = {}


def page_getter(page: str) -> Callable[[int], dict[str, Any]]:
    """按名字取页面助手（首次调用才 import，见上面 PAGES 的说明）。"""
    if page not in PAGES:
        raise AssertionError(f"未知页面 {page!r}，可用：{sorted(PAGES)}")
    if page not in _PAGE_CACHE:
        mod_name, attr = PAGES[page][1].split(":", 1)
        _PAGE_CACHE[page] = getattr(importlib.import_module(mod_name), attr)
    return _PAGE_CACHE[page]


# ---------------------------------------------------------------------------
# 页面助手返回值的**统一形状**（各模板按需取键）
#
#   {"controls": {名字: 在不在}, "selected": 名字 or None}
#
# 老助手（如 `register_controls`）直接返回 `{名字: bool}`，`_split()` 会兼容，
# **不用为了适配驱动去改它们**。
# ---------------------------------------------------------------------------

def _split(raw: dict[str, Any]) -> tuple[dict[str, bool], Any]:
    if "controls" in raw or "selected" in raw:
        controls = {k: bool(v) for k, v in (raw.get("controls") or {}).items()}
        return controls, raw.get("selected")
    return {k: bool(v) for k, v in raw.items()}, None


# ---------------------------------------------------------------------------
# 驱动模板
# ---------------------------------------------------------------------------

def controls_present(page: str, handle: int, case: dict) -> None:
    """断言「某页面上，用例要求的控件都在」。

    `case["expect"]`  —— 必须存在的控件名（见 `PAGES` 里那个函数的键）
    `case["expect_absent"]` —— 可选，必须**不**存在的控件名
    `case["expect_exact"]` —— 可选，`true` 时要求「存在的控件集合 == expect」（多一个都算失败）
    """
    label, _ = PAGES.get(page, (page, ""))
    present, _selected = _split(page_getter(page)(handle))

    expect = list(case.get("expect") or [])
    absent = list(case.get("expect_absent") or [])
    if not expect and not absent:
        raise AssertionError(f"{case.get('id')}: controls_present 至少要给 expect 或 expect_absent")

    missing = [k for k in expect if not present.get(k)]
    assert not missing, (
        f"{label}缺控件：{missing}（要求的：{expect}；实际存在的："
        f"{sorted(k for k, ok in present.items() if ok)}）"
    )

    unexpected = [k for k in absent if present.get(k)]
    assert not unexpected, (
        f"{label}不该有的控件出现了：{unexpected}（实际存在的："
        f"{sorted(k for k, ok in present.items() if ok)}）"
    )

    if case.get("expect_exact"):
        extra = sorted(k for k, ok in present.items() if ok and k not in expect)
        assert not extra, f"{label}存在预期外的控件：{extra}（expect_exact=true 要求集合相等）"


def radio_group_exactly_one(page: str, handle: int, case: dict) -> None:
    """断言「一组单选控件（单选钮/档位）都在，且**恰有一个**处于选中态」。

    主题三档就是这一类。用例给：

    `case["group"]`       —— 这组控件的名字（`PAGES` 里那个助手认识的键）
    `case["expect_exact"]` —— 可选，`true` 时要求「存在的控件集合 == group」

    页面助手要返回统一形状（见 `_split`）：`{"controls": {...}, "selected": 名字 or None}`。
    """
    label, _ = PAGES.get(page, (page, ""))
    present, selected = _split(page_getter(page)(handle))

    group = list(case.get("group") or [])
    if not group:
        raise AssertionError(f"{case.get('id')}: radio_group_exactly_one 必须给 group")

    missing = [k for k in group if not present.get(k)]
    assert not missing, (
        f"{label}缺档位/选项：{missing}（要求的：{group}；实际存在的："
        f"{sorted(k for k, ok in present.items() if ok)}）"
    )

    if case.get("expect_exact"):
        extra = sorted(k for k, ok in present.items() if ok and k not in group)
        assert not extra, f"{label}存在预期外的档位：{extra}（expect_exact=true 要求集合相等）"

    assert selected in group, (
        f"{label}当前选中的是 {selected!r}，不在要求的那组 {group} 里"
        "（既不能一个没选，也不能选到组外）"
    )


DRIVERS: dict[str, tuple[str, Callable[[str, int, dict], None]]] = {
    "controls_present": ("断言页面上列出的控件都在（expect / expect_absent / expect_exact）",
                         controls_present),
    "radio_group_exactly_one": ("断言一组档位都在、且恰有一个选中（group / expect_exact）",
                                radio_group_exactly_one),
}


def run(driver: str, page: str, handle: int, case: dict) -> None:
    """按用例指定的模板执行。未知模板直接报错，不静默跳过。"""
    if driver not in DRIVERS:
        raise AssertionError(f"未知驱动模板 {driver!r}，可用：{sorted(DRIVERS)}")
    _, fn = DRIVERS[driver]
    fn(page, handle, case)


def describe() -> list[tuple[str, str]]:
    """给 `run_cases.py --list-drivers` 用：模板名 + 一句话说明。"""
    return [(name, desc) for name, (desc, _) in sorted(DRIVERS.items())]


def describe_pages() -> list[tuple[str, str]]:
    return sorted((name, label) for name, (label, _) in PAGES.items())
