"""锁住 `launch` 套件的「本机未安装大厅」守卫。

## 为什么要有这条守卫（2026-09-28）

新机 `LAPTOP-68TGB8VB` 首跑，因为 4 个模块漏了安装守卫，**一次吐 22 条 ERROR**
（本该全是 skip）。而 `hall_auto/suites.py` 里 `Suite.needs` 的约定原文就是
「运行前提（写给人看，**缺则整套 skip** / 报错）」—— 那 22 条 ERROR 是
**没实现自己声明的意图**，不是"没装"的错。

所以断言**不看运行结果，只看源码结构** —— 坑就在"忘了写"，
而"忘了写"跑本机是**看不出来的**（本机真装了，守卫写没写都是绿的）。

## 三条规则

1. **凡是要启动大厅的 fixture / 测试函数，参数里必须有 `installed`。**
   共享守卫在 `tests/launch/conftest.py`。
2. **各模块不许自己再定义一份 `installed`。** 原来 `test_p0_launch.py` 自己抄了一份、
   另外 4 个模块没抄 —— 就是因为可以各写各的，才会漏。守卫只能有一份。
3. **凡从 `cfg` 取机器目录的入口，必须自己判存在（`is_dir()` / `is_file()`）再往下用。**
   规则 1 只管「本机装没装大厅」，管不到别的机器前提。
   2026-09-28 实测：`test_personal_storage_all_signed` 拿
   `cfg.security.personal_storage_dir`（`C:/Program Files (x86)/ASUS/PersonalStorage`，
   **随产品安装才出现**）直接去镜像，机器上没有就抛 `SecurityToolError` ——
   而**同套件的另两条**（`test_control_panel_info` / `test_manifest_matches_package`）
   都规规矩矩 skip。缺目录是环境问题，不是「签名有问题」，红在这里是假红。

## 为什么只查 fixture / test 函数

`Session.restart()`（`test_p1_theme.py`）这类**类方法**也会调 `start_fresh`，
但它只可能从已守卫的 `session` fixture 造出来的实例上调用 —— 上游已经拦住了。
把类方法也纳入规则，就得追"谁实例化了这个类"，静态分析会脆得没法维护。
**这是有意的取舍，不是漏网**：真正的入口（fixture / test）全都在规则内。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LAUNCH_DIR = Path(__file__).resolve().parent.parent / "launch"
SHARED_CONFTEST = LAUNCH_DIR / "conftest.py"

# 这两个一被调用就要求「本机已装大厅」（见 hall_auto/launch.py 与 suites.py 的 needs）。
LAUNCH_ENTRYPOINTS = ("start_fresh", "launch_until_ready")

# 共享守卫的名字。
GUARD_NAME = "installed"

# 「机器目录」的识别口径：从 cfg 取来、名字以 `_dir` 结尾的字段。
# 它们存不存在取决于**这台机器装了什么**，不取决于代码 —— 缺了要 skip，不能红。
MACHINE_DIR_SUFFIX = "_dir"

# 判存在的方式：Path 的这几个方法，或直接 skip。
EXISTENCE_CHECKS = ("is_dir", "is_file", "exists")


def _module_files() -> list[Path]:
    return sorted(LAUNCH_DIR.glob("test_*.py"))


def _called_name(call: ast.Call) -> str:
    """被调用者的名字：`f()` 取 `f`，`mod.f()` 取 `f`。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _launches_the_app(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call) and _called_name(sub) in LAUNCH_ENTRYPOINTS
        for sub in ast.walk(node)
    )


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = node.args
    return {p.arg for p in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Attribute):
            names.add(dec.attr)
        elif isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Call):
            inner = dec.func
            if isinstance(inner, ast.Attribute):
                names.add(inner.attr)
            elif isinstance(inner, ast.Name):
                names.add(inner.id)
    return names


def _is_fixture_or_test(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """入口函数：pytest fixture 或测试函数。类方法不算（理由见模块 docstring）。"""
    return "fixture" in _decorator_names(node) or node.name.startswith("test_")


def _entry_functions(path: Path) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_fixture_or_test(node)
    ]


def test_shared_guard_exists_in_conftest():
    """共享守卫必须在 —— 否则各模块又会退回"各写各的"。"""
    assert SHARED_CONFTEST.is_file(), (
        f"缺少 {SHARED_CONFTEST.name}：launch 套件的安装守卫必须是**一份共享的**，"
        "不能靠各模块自己记得写。"
    )
    tree = ast.parse(SHARED_CONFTEST.read_text(encoding="utf-8"))
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == GUARD_NAME
        and "fixture" in _decorator_names(node)
    ]
    assert guarded, (
        f"{SHARED_CONFTEST.name} 里没有名为 {GUARD_NAME!r} 的 fixture —— "
        "守卫被删了或改名了，各模块的 `installed` 参数会立刻变成 unknown fixture。"
    )


def test_no_module_defines_its_own_installed_fixture():
    """各模块不许再自己抄一份 `installed`。"""
    offenders = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == GUARD_NAME
                and "fixture" in _decorator_names(node)
            ):
                offenders.append(path.name)
    assert not offenders, (
        f"这些模块自己定义了 {GUARD_NAME!r} fixture：{offenders}\n"
        "守卫只能有一份（tests/launch/conftest.py）—— 允许各写各的正是 2026-09-28 "
        "4 个模块漏写、22 条 ERROR 的成因。删掉本地的，直接用共享那份。"
    )


def test_every_entry_that_launches_the_app_has_the_install_guard():
    """凡是要启动大厅的 fixture / 测试函数，都必须带 `installed`。"""
    offenders = []
    for path in _module_files():
        for node in _entry_functions(path):
            if not _launches_the_app(node):
                continue
            if GUARD_NAME in _params(node):
                continue
            offenders.append(f"{path.name}::{node.name}")
    assert not offenders, (
        "这些入口会启动大厅，但没有安装守卫 —— 全新机器上会吐 ERROR 而不是 skip：\n  "
        + "\n  ".join(offenders)
        + f"\n修法：给它们加一个 `{GUARD_NAME}` 参数（共享守卫在 tests/launch/conftest.py）。"
    )


def _machine_dir_attrs(node: ast.AST) -> list[str]:
    """函数里引用的「机器目录」字段名（`cfg.security.personal_storage_dir` -> 该名）。"""
    return sorted(
        {
            sub.attr
            for sub in ast.walk(node)
            if isinstance(sub, ast.Attribute) and sub.attr.endswith(MACHINE_DIR_SUFFIX)
        }
    )


def _guards_existence(node: ast.AST) -> bool:
    """函数里有没有**真的判过存在性**。

    只认 `is_dir()` / `is_file()` / `exists()` 这类调用 —— **不能拿任意 `pytest.skip`
    充数**：机器目录用例往往本来就有一个 skip（比如「工具没配」），
    拿它充数的话，把目录守卫删了这条规则还是绿的。
    （本规则第一版就是这么写的，注入验证时当场露馅。）
    """
    return any(
        isinstance(sub, ast.Call) and _called_name(sub) in EXISTENCE_CHECKS
        for sub in ast.walk(node)
    )


def test_every_machine_dir_use_is_guarded_by_an_existence_check():
    """凡用 `cfg.*_dir` 的入口，必须先判存在（或 skip）—— 缺目录是环境问题，不是缺陷。

    这条抓的是「**声明了前提，却没实现"缺则 skip"**」这一类 —— 与那 22 条 ERROR 同源。
    """
    offenders = []
    for path in _module_files():
        for node in _entry_functions(path):
            dirs = _machine_dir_attrs(node)
            if not dirs:
                continue
            if _guards_existence(node):
                continue
            offenders.append(f"{path.name}::{node.name}（用了 {'/'.join(dirs)}）")
    assert not offenders, (
        "这些入口直接用了机器上的目录，却没有判存在 —— 机器上没这个目录时会**硬失败**，"
        "而它本该是 skip：\n  "
        + "\n  ".join(offenders)
        + "\n修法：用之前先 `if not path.is_dir(): pytest.skip(...)`，"
        "并在 skip 文案里写清**要验这条得先让本机具备什么**。"
    )
