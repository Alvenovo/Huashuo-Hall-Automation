"""锁提权段诊断（tools/check_elevation.py）的结论逻辑。

这个脚本的全部价值在于**别把人指错方向**：提权段不出结果有 5 条互不相同的分支，
判错一条就白跑一趟（最贵的是把「查不动 schtasks」说成「任务不存在」——
人会去重跑 bootstrap，还会顺手重置已存在任务的触发时间）。

2026-09-28 实测的触发场景：新机首投 `install`，节点侧「…提权段 install 进行中」
一直打、永不结束，控制机侧 65 分钟零信号 —— 两边都与节点死机同形。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_elevation as ce  # noqa: E402

pytestmark = pytest.mark.unit

REQ_ID = "T1_install_s1_20260928_123053_123456"


def _verdict(tmp_path: Path, *, req: str = REQ_ID, res: str = "",
             wrapper: bool = False, log: bool = False, task: str = "present") -> str:
    wrapper_path = tmp_path / "wrapper.log"
    log_file = tmp_path / "elevated_run.log"
    if wrapper:
        wrapper_path.write_text("...", encoding="utf-8")
    if log:
        log_file.write_text("===== 提权段 install =====\n", encoding="utf-8")
    return ce._verdict(req, res, wrapper_path, log_file, task)


def test_no_request_means_the_agent_never_reached_the_elevated_step(tmp_path):
    assert "还没走到提权段" in _verdict(tmp_path, req="")


def test_matching_result_is_the_only_green(tmp_path):
    """`request_id` 一致才算本轮跑完 —— 这是**唯一**能说"成功"的分支。"""
    assert "✅" in _verdict(tmp_path, res=REQ_ID)


def test_stale_result_from_a_previous_round_is_not_green(tmp_path):
    """上一轮的 result.json 还躺在原地时，**不许**当成本轮的结果（本项目反复踩的假绿）。"""
    out = _verdict(tmp_path, res="OLD_install_s1_xxx")
    assert "上一轮" in out
    assert "✅" not in out


def test_running_log_that_stopped_growing_is_reported_as_stuck(tmp_path):
    """`elevated_run.log` 存在但不再增长 = 提权侧起来了却卡住（NSIS / 共享盘拷贝无超时）。"""
    log_file = tmp_path / "elevated_run.log"
    log_file.write_text("x", encoding="utf-8")
    import os
    import time

    old = time.time() - 10 * 60
    os.utime(log_file, (old, old))
    out = ce._verdict(REQ_ID, "", tmp_path / "wrapper.log", log_file, "present")
    assert "卡住" in out


def test_wrapper_ran_but_produced_no_result(tmp_path):
    """有 wrapper.log、没有 elevated_run.log → python 压根没起来（或走了静默 return 0 的分支）。"""
    out = _verdict(tmp_path, wrapper=True)
    assert "wrapper 跑了但没写结果" in out


def test_never_started_is_distinguished_from_missing_task(tmp_path):
    """两个都不是"在跑"，但处置不同：一个查任务动作，一个重建任务。"""
    never = _verdict(tmp_path, task="present")
    assert "从未跑起来" in never

    absent = _verdict(tmp_path, task="absent")
    assert "计划任务不在" in absent
    assert "从未跑起来" not in absent, "任务不在时不该再让人去查动作路径"


def test_unknown_task_status_is_never_reported_as_absent(tmp_path):
    """**关键防误诊**：`schtasks` 调不起来（权限 / 被安全策略挡）**不等于**任务不存在。

    报成「不存在」会把人送去重跑 bootstrap —— 白跑一趟，还可能重置任务触发时间。
    本项目实测过：代理会话里 `schtasks.exe` 被命令安全拦掉，报的是 `WinError 5`。
    """
    out = _verdict(tmp_path, task="unknown")
    assert "判不出来" in out
    assert "计划任务不在" not in out
    assert "不存在" not in out


def test_task_action_reports_unknown_when_schtasks_cannot_start(monkeypatch):
    """`_task_action` 的三态：调不起来必须是 `unknown`，不能退化成 `absent`。"""

    def _boom(*_a, **_kw):
        raise OSError(5, "拒绝访问。")

    monkeypatch.setattr(ce.subprocess, "run", _boom)
    status, detail = ce._task_action()
    assert status == "unknown", "调不起来不是「不存在」"
    assert "调不起" in detail
