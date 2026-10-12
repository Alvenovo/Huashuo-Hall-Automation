# -*- coding: utf-8 -*-
"""用例清单真源（`cases/cases.yaml`）的读取器 —— **只读，从不写回**。

设计见根目录《用例数据化方案.md》。三条约定：

1. **YAML 是唯一真源**。这里只提供读取与索引；改用例只改 YAML。
2. **绑定键是「测试函数名」**，不是编号 —— 函数名稳定，编号给人看。
3. `tier: data` 的用例，**参数表在 YAML 里**。测试函数声明一个 `case` 形参，
   由 `tests/launch/conftest.py::pytest_generate_tests` 注入。
   → 加一组参数 = 改 YAML，不碰 Python。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_YAML = REPO_ROOT / "cases" / "cases.yaml"


def load() -> dict[str, Any]:
    """读真源。**不缓存** —— 缓存会让「改了 YAML 但同进程内读到旧值」变成隐形 bug。"""
    if not CASES_YAML.is_file():
        raise FileNotFoundError(f"找不到用例真源 {CASES_YAML}")
    doc = yaml.safe_load(CASES_YAML.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise ValueError("cases/cases.yaml 结构不对：顶层要有 meta / cases")
    return doc


def by_func() -> dict[str, dict[str, Any]]:
    """`测试函数名` -> 那条用例。同一函数名出现两次会报错（绑定键必须唯一）。"""
    out: dict[str, dict[str, Any]] = {}
    for c in load()["cases"]:
        ref = c.get("ref") or ""
        func = ref.split("::")[-1] if "::" in ref else ""
        if not func:
            continue
        if func in out:
            raise ValueError(f"cases.yaml 里 {func} 绑定了两条用例（编号 {out[func]['id']} / {c['id']}）")
        out[func] = c
    return out


def params_for(func_name: str) -> list[dict[str, Any]] | None:
    """某个测试函数的参数表。`None` = 这条不是 `tier: data`（不注入）。"""
    case = by_func().get(func_name)
    if case is None or case.get("tier") != "data":
        return None
    params = case.get("params")
    if not isinstance(params, list) or not params:
        raise ValueError(f"{case['id']} 是 tier=data 但没有非空 params")
    return params
