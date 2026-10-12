# -*- coding: utf-8 -*-
"""用例清单真源（默认 `cases/cases.yaml`）的读取器 —— **只读，从不写回**。

设计见根目录《用例数据化方案.md》。四条约定：

1. **YAML 是唯一真源**。这里只提供读取与索引；改用例只改 YAML。
2. **路径可配**（2026-10-12 组长要的「脚本去找这个路径下的文件A」）：
   优先级 `HALL_CASES_FILE` 环境变量 > 默认 `cases/cases.yaml`。
   命令行工具还可以用 `--cases <路径>` 覆盖（见 `tools/run_cases.py`）。
3. **绑定键是「测试函数名」**，不是编号 —— 函数名稳定，编号给人看。
4. `tier: data` 的用例，**参数表在 YAML 里**。测试函数声明一个 `case` 形参，
   由 `tests/launch/conftest.py::pytest_generate_tests` 注入。
   → 加一组参数 = 改 YAML，不碰 Python。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 没配 `HALL_CASES_FILE` 时用的默认真源（相对仓库根）。
DEFAULT_CASES = REPO_ROOT / "cases" / "cases.yaml"

#: 真源路径的环境变量名。组长要的「放在文件夹某路径下」就靠它。
CASES_ENV = "HALL_CASES_FILE"


def resolve_path(raw: str | os.PathLike[str] | None = None) -> Path:
    """解析用例文件路径：显式参数 > `HALL_CASES_FILE` > 默认。

    相对路径按**仓库根**解析（不是 cwd）—— 跑批时 cwd 可能是别处，
    按 cwd 解析会让「同一份配置在不同目录跑出不同结果」。
    """
    cand = raw if raw is not None else os.environ.get(CASES_ENV, "")
    cand = str(cand).strip()
    if not cand:
        return DEFAULT_CASES
    p = Path(cand)
    return p if p.is_absolute() else (REPO_ROOT / p)


def load(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """读真源。**不缓存** —— 缓存会让「改了 YAML 但同进程内读到旧值」变成隐形 bug。"""
    target = resolve_path(path)
    if not target.is_file():
        raise FileNotFoundError(f"找不到用例文件：{target}（用 --cases 或 {CASES_ENV} 指定）")
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise ValueError(f"{target} 结构不对：顶层要有 meta / cases")
    return doc


def all_cases(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    return list(load(path)["cases"])


def by_func(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    """`测试函数名` -> 那条用例。同一函数名出现两次会报错（绑定键必须唯一）。"""
    out: dict[str, dict[str, Any]] = {}
    for c in all_cases(path):
        ref = c.get("ref") or ""
        func = ref.split("::")[-1] if "::" in ref else ""
        if not func:
            continue
        if func in out:
            raise ValueError(f"用例文件里 {func} 绑定了两条用例（编号 {out[func]['id']} / {c['id']}）")
        out[func] = c
    return out


def params_for(func_name: str, path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]] | None:
    """某个测试函数的参数表。`None` = 这条不是 `tier: data`（不注入）。"""
    case = by_func(path).get(func_name)
    if case is None or case.get("tier") != "data":
        return None
    params = case.get("params")
    if not isinstance(params, list) or not params:
        raise ValueError(f"{case['id']} 是 tier=data 但没有非空 params")
    return params
