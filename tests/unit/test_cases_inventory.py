# -*- coding: utf-8 -*-
"""用例清单 ↔ 代码 **双向对账** 守卫（2026-10-12，见《用例数据化方案.md》第八节）。

## 为什么要有它

`cases/cases.yaml` 是清单的唯一真源。**真源最大的风险是变成一句过期的谎** ——
本项目反复踩这个坑（桌面 `测试清单.docx` 就是活例子：手写、不在 git、和代码对不上没人知道）。

所以这里钉两条**方向相反**的锁：

1. **清单 → 代码**：YAML 里每条 `ref` 指的测试函数**必须真的存在**。
   否则报「清单里指的代码没了」（改了函数名 / 删了用例没同步清单）。
2. **代码 → 清单**：`tests/launch` + `tests/install` 里**每个** `def test_` **必须在 YAML 里登记**。
   否则报「新加的用例没登记」（加了用例忘了写清单）。

另外还锁：schema 合法、id 唯一、`suite` 是 `suites.py` 的键、`ref` 文件属于该套件、
`marker` 在 `pytest.ini` 里声明过、`cases/清单.md` 不过期。

## ⚠️ 负路径必须注入验证（项目惯例）

**只跑一次看到绿不算验证。** 2026-09-28 的教训：守卫第一版写得太松，
把被守护的代码整块删掉它**依然是绿的**。所以本文件带 `test_guard_itself_*` 自检，
用**合成数据**证明「坏输入真的会被判红」。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

CASES_YAML = REPO_ROOT / "cases" / "cases.yaml"
MD_OUT = REPO_ROOT / "cases" / "清单.md"
SCAN_DIRS = (REPO_ROOT / "tests" / "launch", REPO_ROOT / "tests" / "install")


# ---------------------------------------------------------------------------
# 扫描：代码侧有哪些用例
# ---------------------------------------------------------------------------

def _test_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        out.extend(sorted(p for p in d.glob("test_*.py") if p.is_file()))
    return out


def _code_refs() -> set[str]:
    """`tests/launch` + `tests/install` 里所有模块级 `def test_*` 的 `相对路径::函数名`。"""
    refs: set[str] = set()
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                refs.add(f"{rel}::{node.name}")
    return refs


def _funcs_in_file(rel: str) -> set[str]:
    path = REPO_ROOT / rel
    if not path.is_file():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}


# ---------------------------------------------------------------------------
# 纯函数：给定两个集合算出差异（自检直接喂合成数据）
# ---------------------------------------------------------------------------

def _missing_in_code(registered: set[str], in_code: set[str]) -> list[str]:
    """清单里有、代码里没有 —— 清单指向了不存在的用例。"""
    return sorted(registered - in_code)


def _missing_in_inventory(in_code: set[str], registered: set[str]) -> list[str]:
    """代码里有、清单里没登记 —— 新加用例忘了登记。"""
    return sorted(in_code - registered)


# ---------------------------------------------------------------------------
# 读真源与声明
# ---------------------------------------------------------------------------

def _load_doc() -> dict:
    import yaml

    assert CASES_YAML.is_file(), f"找不到用例真源 {CASES_YAML}"
    doc = yaml.safe_load(CASES_YAML.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and isinstance(doc.get("cases"), list), \
        "cases.yaml 结构不对：顶层要有 meta / cases"
    return doc


def _declared_markers() -> set[str]:
    """`pytest.ini` 里 `markers =` 声明的标记名。"""
    ini = REPO_ROOT / "pytest.ini"
    text = ini.read_text(encoding="utf-8")
    block = re.search(r"^markers\s*=\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    assert block, "pytest.ini 里找不到 markers 块"
    out: set[str] = set()
    for line in block.group(1).splitlines():
        m = re.match(r"\s*([A-Za-z_][\w-]*)\s*:", line)
        if m:
            out.add(m.group(1))
    return out


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------

def test_cases_yaml_is_valid():
    """schema 合法（复用导出器的校验器，保证「能导出」与「守卫」同一套判据）。"""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import importlib

    mod = importlib.import_module("cases_export")
    problems = mod.validate(_load_doc())
    assert not problems, "cases.yaml 校验不过：\n  " + "\n  ".join(problems)


def test_every_ref_points_to_an_existing_test():
    """清单 → 代码：每条 ref 指的函数必须存在。"""
    doc = _load_doc()
    bad: list[str] = []
    for c in doc["cases"]:
        if not c.get("ref"):
            continue  # status=todo：还没自动化，先登记着
        rel, _, func = (c.get("ref") or "").partition("::")
        if not rel or not func:
            bad.append(f"{c.get('id')}: ref 格式应为 <路径>::<函数名>，实际 {c.get('ref')!r}")
            continue
        if func not in _funcs_in_file(rel):
            bad.append(f"{c.get('id')}: {c['ref']} —— 这个函数在 {rel} 里不存在")
    assert not bad, (
        "用例清单指向了不存在的用例（改了函数名 / 删了用例没同步清单？）：\n  "
        + "\n  ".join(bad)
    )


def test_every_code_case_is_registered_in_inventory():
    """代码 → 清单：每个 UI 用例都必须在 YAML 里登记。

    这条红了**别改断言** —— 去 `cases/cases.yaml` 补一条登记（或确认那个函数是不是该删）。
    """
    registered = {c["ref"] for c in _load_doc()["cases"] if c.get("ref")}
    unregistered = _missing_in_inventory(_code_refs(), registered)
    assert not unregistered, (
        "这些用例没有登记进 cases/cases.yaml —— 清单从此与代码脱节：\n  "
        + "\n  ".join(unregistered)
        + "\n\n（在 cases/cases.yaml 的 cases: 下补一条，跑 tools/cases_export.py --format md 重出清单）"
    )


def test_no_orphan_inventory_entries():
    """清单 → 代码的另一半：不允许清单里留着已经没人指的函数名。"""
    registered = {c["ref"] for c in _load_doc()["cases"] if c.get("ref")}
    orphan = _missing_in_code(registered, _code_refs())
    assert not orphan, (
        "清单里有条目指向了不存在的用例：\n  " + "\n  ".join(orphan)
    )


def test_ids_are_unique():
    """编号唯一 —— 导出到禅道/Excel 时编号是主键，重了就没法对账。"""
    ids = [c["id"] for c in _load_doc()["cases"]]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, f"用例编号重复：{dup}"


def test_suites_are_real():
    """`suite` 必须是 `hall_auto/suites.py` 里的键 —— 否则跑批按套件过滤时这条永远不会被选到。"""
    from hall_auto.suites import SUITES

    bad = sorted({c["suite"] for c in _load_doc()["cases"] if c.get("suite") and c["suite"] not in SUITES})
    assert not bad, f"这些 suite 在 hall_auto/suites.py 里不存在：{bad}"


def test_markers_are_declared_in_pytest_ini():
    """`marker` 必须是 `pytest.ini` 声明过的 —— 未声明的标记 pytest 只警告不报错，会静默漏掉。"""
    declared = _declared_markers()
    bad: list[str] = []
    for c in _load_doc()["cases"]:
        for m in c["marker"]:
            if m not in declared:
                bad.append(f"{c['id']}: 标记 {m!r} 没在 pytest.ini 的 markers 里声明")
    assert not bad, "未声明的标记（pytest 只警告，不会拦你）：\n  " + "\n  ".join(sorted(set(bad)))


def test_exported_checklist_is_up_to_date():
    """`cases/清单.md` 必须与真源一致 —— 派生物过期等于给人看旧清单。"""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import importlib

    mod = importlib.import_module("cases_export")
    doc = _load_doc()
    assert MD_OUT.is_file(), f"{MD_OUT.name} 不存在 —— 跑 tools/cases_export.py --format md"
    assert MD_OUT.read_text(encoding="utf-8") == mod.render_md(doc, include_probe=False), (
        f"{MD_OUT.name} 与 cases.yaml 不一致 —— 改过真源没重出清单。\n"
        "  跑：.venv\\Scripts\\python.exe -X utf8 tools\\cases_export.py --format md"
    )


# ---------------------------------------------------------------------------
# 守卫自检（负路径）—— 证明「坏输入真的会被判红」
# ---------------------------------------------------------------------------

def test_guard_itself_detects_an_unregistered_case():
    """给一组「代码有、清单没有」的合成数据，`_missing_in_inventory` 必须认出来。

    没有这条，上面那条可能因为路径没扫到 / 集合写反而**永远绿** —— 等于没有守卫。
    """
    code = {"tests/launch/test_x.py::test_a", "tests/launch/test_x.py::test_b"}
    registered = {"tests/launch/test_x.py::test_a"}
    assert _missing_in_inventory(code, registered) == ["tests/launch/test_x.py::test_b"]
    assert _missing_in_inventory(code, code) == []


def test_guard_itself_detects_an_orphan_ref():
    """给一组「清单有、代码没有」的合成数据，`_missing_in_code` 必须认出来。"""
    code = {"tests/launch/test_x.py::test_a"}
    registered = {"tests/launch/test_x.py::test_a", "tests/launch/test_x.py::test_gone"}
    assert _missing_in_code(registered, code) == ["tests/launch/test_x.py::test_gone"]


def test_guard_scans_the_real_test_tree():
    """扫描面自检：确认真的扫到了用例文件（防「路径写错 → 集合恒空 → 恒绿」）。"""
    refs = _code_refs()
    assert len(refs) >= 30, f"只扫到 {len(refs)} 个用例，扫描路径可能不对：{_test_files()}"
    assert any(r.endswith("::test_p0_03_launch_window") for r in refs)


def test_marker_parser_reads_pytest_ini():
    """`pytest.ini` 解析器自检 —— 解析不出来会让 marker 那条恒绿。"""
    declared = _declared_markers()
    for expect in ("unit", "install", "launch", "login", "settings", "security", "wb", "manual"):
        assert expect in declared, f"pytest.ini 解析漏了 {expect}"
