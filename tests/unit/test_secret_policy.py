"""凭据打码：pytest 的失败 traceback 会渲染**函数参数值**，凭据必须打码。

为什么值得一条专门的守卫（2026-10-10 实付）：节点跑 `login` 失败，回传的
`report.html` / 农场日志里**直接印着密码明文** —— 因为 pytest 9 起
`_pytest/nodes.py` 里 `funcargs=True` 是硬编码的，traceback 会把每个 frame 的
**参数值**渲染出来（`--showlocals` 只管函数体内的局部变量，管不到它），
而农场会把报告回传共享盘 → 撞"密码不落盘"红线。
"""

from __future__ import annotations

import pytest
from _pytest._io.saferepr import saferepr

from hall_auto.config import Config, Secret

pytestmark = pytest.mark.unit


def test_secret_repr_is_masked():
    """repr 只出 ***。"""
    assert repr(Secret("SECRETPWD")) == "'***'"


def test_pytest_saferepr_masks_secret():
    """锁住**真正的渲染路径**：pytest 打 frame 参数用的是 saferepr，它必须走我们的 repr。

    单测 `repr()` 打码不够 —— 真正决定明文落不落盘的是 saferepr 的分派：
    它对非内置类型才走 `repr()`。这条把那条分派也一起锁住。
    """
    assert saferepr(Secret("SECRETPWD")) == "'***'"


def test_secret_is_still_a_working_str():
    """打码不能伤功能：比较、切片、编码、拼接、f-string 全按普通 str 走。"""
    s = Secret("Abc123!")
    assert s == "Abc123!"
    assert s[:3] == "Abc"
    assert s.encode() == b"Abc123!"
    assert f"{s}" == "Abc123!"
    assert s + "x" == "Abc123!x"
    assert isinstance(s, str)


def test_test_account_and_new_password_return_masked_secret(monkeypatch):
    """端到端：从 Config 取出的凭据，repr / saferepr 里都不能出现明文。"""
    monkeypatch.setenv("HALL_TEST_USER", "19500000000")
    monkeypatch.setenv("HALL_TEST_PASSWORD", "SECRETPWD")
    monkeypatch.setenv("HALL_TEST_NEW_PASSWORD", "NEWSECRET")
    cfg = Config.__new__(Config)  # 本用例只走 raw 与环境变量，其余字段无关
    object.__setattr__(cfg, "raw", {})  # Config 是 frozen dataclass，绕过只读
    user, password = cfg.test_account()
    assert isinstance(user, Secret)
    assert isinstance(password, Secret)
    for value in (user, password, cfg.new_password()):
        assert "SECRET" not in repr(value)
        assert "SECRET" not in saferepr(value)
