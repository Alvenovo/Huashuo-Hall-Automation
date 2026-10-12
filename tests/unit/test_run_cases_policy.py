# -*- coding: utf-8 -*-
"""锁 `tools/run_cases.py` 的**挑选与安全闸**策略（2026-10-12）。

## 为什么要有它

`run_cases.py` 是「按用例文件挑着跑」的入口，它挑错了会出**真事故**：

- 把 `status: manual`（人在环）挑进去 → 用例里的 `input()` 在非交互环境下
  **永久阻塞**，或真发一条短信出去。
- 把 `status: destructive` / `install` / `apps-lifecycle` 挑进去 →
  **真装真卸华硕大厅**，一台机器的状态当场被改。

这两条都是「默认必须挡住、要显式开闸」的东西。所以策略抽成纯函数
`select_cases()` 在这里钉住 —— 不依赖 pytest、不碰磁盘。

## 负路径

`test_guard_itself_*` 用合成数据证明「坏输入真的会被挡住」，
不是「看着是绿的」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import run_cases  # noqa: E402


def _case(cid: str, *, suite="launch", status="active", ref=None) -> dict:
    return {
        "id": cid,
        "title": cid,
        "suite": suite,
        "marker": ["launch"],
        "tier": "code",
        "kind": "case",
        "status": status,
        "verify": "v",
        "ref": ref if ref is not None else f"tests/launch/test_x.py::test_{cid.lower()}",
    }


SAMPLE = [
    _case("A-01"),
    _case("A-02", status="manual"),
    _case("A-03", status="destructive"),
    _case("A-04", suite="install"),
    _case("A-05", suite="apps-lifecycle"),
    _case("A-06", status="todo", ref=""),
    _case("A-07", suite="login"),
    _case("A-08", status="xfail"),
]


def _ids(picked: list[dict]) -> list[str]:
    return [c["id"] for c in picked]


def test_default_run_excludes_manual_and_todo():
    picked, skipped = run_cases.select_cases(SAMPLE)
    assert "A-02" not in _ids(picked), "人在环默认不能进 —— input() 会永久阻塞 / 真发短信"
    assert "A-06" not in _ids(picked), "todo 没代码可跑"
    assert any("A-02" in s for s in skipped) and any("A-06" in s for s in skipped), \
        "被挡掉的要给出理由，不能静默消失"


def test_default_run_excludes_destructive_and_its_suites():
    """真装真卸默认挡住：`status: destructive` 与 `install` / `apps-lifecycle` 两个套件。"""
    picked, skipped = run_cases.select_cases(SAMPLE)
    for cid in ("A-03", "A-04", "A-05"):
        assert cid not in _ids(picked), f"{cid} 是真装真卸，默认必须挡住"
        assert any(cid in s for s in skipped), f"{cid} 要给出「需要 --allow-destructive」的理由"


def test_allow_manual_opens_only_the_manual_gate():
    picked, _ = run_cases.select_cases(SAMPLE, allow_manual=True)
    ids = _ids(picked)
    assert "A-02" in ids, "--allow-manual 要能放开人在环"
    assert "A-03" not in ids, "--allow-manual 不该顺手放开真装真卸"


def test_allow_destructive_opens_only_the_destructive_gate():
    picked, _ = run_cases.select_cases(SAMPLE, allow_destructive=True)
    ids = _ids(picked)
    for cid in ("A-03", "A-04", "A-05"):
        assert cid in ids, f"--allow-destructive 要能放开 {cid}"
    assert "A-02" not in ids, "--allow-destructive 不该顺手放开人在环"


def test_ids_selects_exactly_those_cases():
    picked, _ = run_cases.select_cases(SAMPLE, ids=["A-07", "A-08"])
    assert _ids(picked) == ["A-07", "A-08"]


def test_ids_reports_unknown_id():
    picked, skipped = run_cases.select_cases(SAMPLE, ids=["A-01", "NOPE-99"])
    assert _ids(picked) == ["A-01"]
    assert any("NOPE-99" in s for s in skipped), "不存在的编号要报出来，不能静默少跑"


def test_ids_still_respects_the_gates():
    """点名要跑人在环也不行 —— 闸门比 --ids 优先。"""
    picked, skipped = run_cases.select_cases(SAMPLE, ids=["A-02"])
    assert picked == []
    assert any("A-02" in s for s in skipped)


def test_suite_and_status_filters():
    picked, _ = run_cases.select_cases(SAMPLE, suite="login")
    assert _ids(picked) == ["A-07"]
    picked, _ = run_cases.select_cases(SAMPLE, statuses=["xfail"])
    assert _ids(picked) == ["A-08"]


def test_extra_exclude_status_wins():
    """显式 `--exclude-status` 比默认更严时以更严的为准。"""
    picked, skipped = run_cases.select_cases(SAMPLE, exclude_statuses=("manual", "todo", "xfail"))
    assert "A-08" not in _ids(picked)
    assert any("A-08" in s for s in skipped)


def test_node_ids_dedupes_and_keeps_order():
    picked = [_case("B-02"), _case("B-01")]
    picked[1]["ref"] = picked[0]["ref"]        # 两条指同一个函数
    refs = run_cases.node_ids(picked)
    assert refs == [picked[0]["ref"]], "同一个函数只能进 pytest 一次"


def test_build_argv_adds_dash_s_only_for_manual():
    """人在环必须带 `-s`，否则 pytest 把 stdin 换成 DontReadFromInput、三条全 skip 还报退出码 0。"""
    normal = run_cases.build_argv([_case("A-01")], Path("cases/cases.yaml"))
    assert "-s" not in normal
    manual = run_cases.build_argv([_case("A-02", status="manual")], Path("cases/cases.yaml"))
    assert "-s" in manual, "人在环必须自动带 -s（2026-09-22 真机踩过）"


def test_build_argv_always_disables_pytest_cache():
    """`-p no:cacheprovider` 不能丢 —— 提权跑过之后 `.pytest_cache/` 归 Administrators，会刷一堆 WinError 5。"""
    argv = run_cases.build_argv([_case("A-01")], Path("cases/cases.yaml"))
    assert "-p" in argv and "no:cacheprovider" in argv


def test_build_argv_passes_basetemp():
    argv = run_cases.build_argv([_case("A-01")], Path("cases/cases.yaml"))
    assert any(a.startswith("--basetemp=") for a in argv), "要给专属 tmp 根，别碰系统临时目录"


def test_guard_itself_detects_a_leaked_manual_case():
    """守卫自检：**放开闸门后**人在环必须能被选进来 —— 否则上面几条可能因为"永远选不中"而恒绿。"""
    picked, _ = run_cases.select_cases(SAMPLE, allow_manual=True)
    assert "A-02" in _ids(picked)
