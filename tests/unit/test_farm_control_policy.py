"""锁控制机侧策略（tools/farm_control.py）：人在环门禁 + 汇总报告 + 节点索引。

三件事都直接决定测试人员能不能按手册得出结论：

1. **人在环套件不能进农场**。手册明确承诺 `login-manual`「永不进农场」，但早先
   `dispatch` 只打印一句 `⚠ 人在环，不应进农场`，照样把任务写出去 ——
   节点上它会卡在 `input()` 等短信验证码，agent 一直不返回，那台机器再也取不到新任务，
   而控制机这边只看到"执行中"。

2. **汇总报告的「凭据」列必须真有数据**。手册第四部分让测试人员靠这一列区分
   「这台机器没配凭据」和「凭据配了、是用例本身在跳过」。早先 `cmd_aggregate`
   只读 `results/*/summary.json`，而凭据状态只写在 `done/` 回执里，
   `nodes_data` 从未写入 `credentials` 键 → 那一列**恒为 ✗**，
   一线会拿着全红的凭据列去反复折腾 `farm_node.env`。

3. **「目录名 ↔ 套件」必须对得上**。run 目录名只有「节点_日期_时刻」，
   一个节点跑过 133 轮之后，`..._1349` 和 `..._1353` 谁是谁在资源管理器里根本分不清，
   也就没法「拿报告去对应校验」（2026-10-12 用户实报）。套件名一直躺在每轮的
   `node_env.json` 里，只是没人把它摊到目录这一层 —— `results/<节点>/index.html` 就是补这个。
   同时根目录那份 `aggregate_report.html` 固定名会被下一轮覆盖，所以另存带时间戳的归档。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import farm_control  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def farm(tmp_path, monkeypatch):
    for sub in ("tasks", "done", "results", "logs"):
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("HALL_FARM_ROOT", str(tmp_path))
    return tmp_path


def _dispatch(nodes: str, suites: str, **kw):
    return farm_control.cmd_dispatch(
        argparse.Namespace(nodes=nodes, suites=suites, task_id=kw.pop("task_id", "T1"), **kw)
    )


# ---------------- 人在环套件门禁 ----------------


def test_dispatch_rejects_manual_suite(farm):
    """**关键门禁**：`login-manual` 默认不许投进农场，且一个任务文件都不该产生。"""
    with pytest.raises(SystemExit) as exc:
        _dispatch("R01", "login-manual")
    assert "人在环" in str(exc.value)
    assert not list((farm / "tasks").iterdir()), "拒投就不该留下任务文件"


def test_dispatch_manual_rejection_mentions_how_to_run_it(farm):
    """拒绝信息要给出正确跑法（在交互式终端 --local），否则一线只会换个参数硬试。"""
    with pytest.raises(SystemExit) as exc:
        _dispatch("R01", "login-manual")
    assert "--local" in str(exc.value)


def test_dispatch_allows_manual_with_explicit_flag(farm):
    """显式 `--allow-manual`（例如你正远程盯着那台机器）才放行。"""
    assert _dispatch("R01", "login-manual", allow_manual=True) == 0
    payload = json.loads((farm / "tasks" / "R01.json").read_text(encoding="utf-8"))
    assert payload["suites"] == [{"name": "login-manual", "shard_id": 1, "shard_count": 1}]


def test_dispatch_rejects_manual_mixed_with_normal_suites(farm):
    """混着投也要整体拒掉，不能"只投能跑的那部分" —— 那样一线以为 login-manual 也跑了。"""
    with pytest.raises(SystemExit):
        _dispatch("R01", "launch,login-manual")
    assert not list((farm / "tasks").iterdir())


def test_dispatch_normal_suites_still_work(farm):
    assert _dispatch("R01,R02", "launch,apps-detect") == 0
    assert (farm / "tasks" / "R01.json").is_file()
    assert (farm / "tasks" / "R02.json").is_file()


def test_dispatch_marks_destructive_suites(farm):
    """真装卸套件要在任务里显式带上 `HALL_ALLOW_INSTALL=1`，便于审计"谁开的闸"。"""
    _dispatch("R01", "install")
    payload = json.loads((farm / "tasks" / "R01.json").read_text(encoding="utf-8"))
    assert payload["env"]["HALL_ALLOW_INSTALL"] == "1"


def test_dispatch_unknown_suite_fails_fast(farm):
    """套件名写错要当场报错，别投出去才发现。"""
    with pytest.raises(KeyError):
        _dispatch("R01", "no-such-suite")


# ---------------- 「执行中」要说清楚是什么、多久了 ----------------

# 控制机侧唯一能看到的信号就是 `status`。早先只打一行文件名，于是
# 「刚投出去 3 秒」与「已经跑了 59 分钟」长得一模一样 —— 而处置完全相反
# （一个就该等，一个得去节点看）。2026-09-28 实测就是这么干等的。


def _put_running(
    farm: Path, node: str, *, task_id: str, suites: list[str], created_at: str
) -> Path:
    path = farm / "tasks" / f"{node}.json.running"
    path.write_text(
        json.dumps({
            "task_id": task_id, "node": node,
            "suites": [{"name": n, "shard_id": 1, "shard_count": 1} for n in suites],
            "created_at": created_at,
        }, ensure_ascii=False), encoding="utf-8",
    )
    return path


def _status_out(farm, capsys) -> str:
    assert farm_control.cmd_status(argparse.Namespace()) == 0
    return capsys.readouterr().out


def test_status_shows_how_long_a_running_task_has_been_going(farm, capsys):
    """「执行中」必须带上**已跑多久**，以及是哪个任务、哪些套件。"""
    started = (datetime.now() - timedelta(minutes=59)).isoformat(timespec="seconds")
    _put_running(farm, "R01", task_id="task_20260928_123053", suites=["install"],
                 created_at=started)

    out = _status_out(farm, capsys)
    assert "task_20260928_123053" in out, "要报出任务号，否则没法对着日志查"
    assert "install" in out, "要报出套件名"
    assert "已跑 59 分钟" in out, "时长是判断「该不该等」的唯一信号"


def test_status_flags_elevated_suites_and_where_their_output_lives(farm, capsys):
    """提权套件要单独提示：节点自己跑、最长静默等 4 小时、真因在**节点本机**。

    不提示的话，运维会以为提权段的输出也该回传到控制机，然后在共享盘上找一个
    根本不存在的东西（2026-09-28 实测：控制机侧翻遍也没有）。
    """
    started = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    _put_running(farm, "R01", task_id="task_2", suites=["install"], created_at=started)

    out = _status_out(farm, capsys)
    assert "提权" in out
    assert "4 小时" in out, "要写出等待上界，否则看到日志不动就会以为已经卡死"
    assert "elevated_run.log" in out, "要指路到真因所在"
    assert "节点本机" in out, "要说明这份日志不在控制机、也不在共享盘上"


def test_status_does_not_flag_a_readonly_suite_as_elevated(farm, capsys):
    """反向：只读套件不该被标成提权 —— 标错了会让人白等，或去节点翻一个不存在的日志。"""
    started = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    _put_running(farm, "R01", task_id="task_3", suites=["launch"], created_at=started)

    out = _status_out(farm, capsys)
    assert "已跑 1 分钟" in out
    assert "elevated_run.log" not in out


def test_status_does_not_invent_a_duration(farm, capsys):
    """`created_at` 读不出来时**不许猜成 0** —— 「已跑 0 分钟」看起来像刚投出去，会把排查带偏。"""
    _put_running(farm, "R01", task_id="task_4", suites=["launch"], created_at="")

    out = _status_out(farm, capsys)
    assert "分钟" not in out, "读不出时间就不该给出一个时长（0 分钟会被读成「刚投出去」）"
    assert "算不出已跑多久" in out


# ---------------- 汇总报告的凭据列 ----------------


def _put_run(farm: Path, node: str, run: str, *, node_env: dict | None = None) -> Path:
    run_dir = farm / "results" / node / run
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({
            "run": run, "node": node,
            "profile": {"os": "Windows 11", "screen": "2560x1600", "scale_percent": 150},
            "total": 1, "passed": 1, "failed": 0, "skipped": 0, "duration_s": 1.0,
            "cases": [{
                "module": "launch", "case": "test_a", "title": "启动", "outcome": "passed",
                "nodeid": "tests/launch/test_p0_launch.py::test_a", "duration_s": 0.5,
                "timestamp": "2026-09-21T10:00:00", "assertion": "", "screenshot": None,
            }],
        }, ensure_ascii=False), encoding="utf-8",
    )
    if node_env is not None:
        (run_dir / farm_control.NODE_ENV_EVIDENCE_NAME).write_text(
            json.dumps(node_env, ensure_ascii=False), encoding="utf-8"
        )
    return run_dir


def _render(farm: Path) -> str:
    farm_control.cmd_aggregate(argparse.Namespace())
    return (farm / "aggregate_report.html").read_text(encoding="utf-8")


def test_aggregate_reads_credentials_from_evidence(farm):
    """**核心回归锁**：证据目录里的 `node_env.json` 必须被读出来，凭据列渲染成 ✓。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000", node_env={
        "node": "R01", "credentials": {"password_login": True, "microsoft_sso": True, "share_creds": True},
    })
    html = _render(farm)
    assert html.count("#1a7f37\">✓") == 3, "三个凭据格都该是 ✓"
    assert "#c62828\">✗" not in html, "配了凭据就不该出现 ✗"


def test_aggregate_shows_cross_when_credentials_absent(farm):
    """没配凭据的节点该显示 ✗ —— 这一列的意义就是区分根因，不能两边都一个样。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000", node_env={
        "node": "R01", "credentials": {"password_login": False, "microsoft_sso": False, "share_creds": False},
    })
    html = _render(farm)
    assert html.count("#c62828\">✗") == 3
    assert "#1a7f37\">✓" not in html


def test_aggregate_falls_back_to_done_receipts(farm):
    """证据里没有 `node_env.json`（早先版本节点跑的结果）-> 用 `done/` 回执兜底。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000")
    (farm / "done" / "R01_T1.json").write_text(json.dumps({
        "task_id": "T1", "node": "R01",
        "credentials": {"password_login": True, "microsoft_sso": False, "share_creds": True},
    }, ensure_ascii=False), encoding="utf-8")
    html = _render(farm)
    assert html.count("#1a7f37\">✓") == 2, "回执里的凭据要能补上"
    assert html.count("#c62828\">✗") == 1


def test_aggregate_warns_when_credentials_unknown(farm, capsys):
    """两边都拿不到凭据状态时要**明确警告**，别让全 ✗ 被当成"没配凭据"的结论。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000")
    _render(farm)
    out = capsys.readouterr().out
    assert "没拿到凭据状态" in out
    assert "R01" in out


def test_aggregate_requires_results(farm):
    with pytest.raises(SystemExit):
        farm_control.cmd_aggregate(argparse.Namespace())


# ---------------- 汇总层不能有假绿：error 单列 + 每格标来源轮次 ----------------


def _case(nodeid: str, outcome: str, assertion: str = "") -> dict:
    module = nodeid.split("::")[0].split("/")[-2]
    return {
        "module": module, "case": nodeid.split("::")[-1], "title": "",
        "nodeid": nodeid, "outcome": outcome, "duration_s": 0.1,
        "timestamp": "2026-09-22T10:00:00", "assertion": assertion, "screenshot": None,
    }


def _put_cases(farm: Path, node: str, run: str, cases: list[dict], finished_at: str = "") -> Path:
    """写一个只含指定用例的 run —— 用来构造"跨轮合并"的场景。"""
    run_dir = farm / "results" / node / run
    run_dir.mkdir(parents=True)
    counts = {o: sum(1 for c in cases if c["outcome"] == o) for o in ("passed", "failed", "error", "skipped")}
    (run_dir / "summary.json").write_text(
        json.dumps({
            "run": run, "node": node,
            "profile": {"os": "Windows 11", "screen": "2560x1600", "scale_percent": 150},
            "finished_at": finished_at or "2026-09-22T10:00:00",
            "total": len(cases), **counts, "duration_s": 1.0, "cases": cases,
        }, ensure_ascii=False), encoding="utf-8",
    )
    return run_dir


def test_aggregate_marks_cells_that_came_from_an_earlier_run(farm):
    """**跨轮合并必须标来源**：老轮次的结果会留在矩阵里冒充最新结论。

    2026-09-22 的假绿就是这个机制：unit 有 188 条用例在 setup 就崩、没进记录，
    `aggregate` 按 nodeid 合并时"新那轮没记录就保留老记录" —— 矩阵那一格挂着上一轮的 ✓，
    整行看着全绿。所以每格都要能查出来自哪一轮，来自更早轮次的还要带 `°`。
    """
    stale = "tests/launch/test_p0_launch.py::test_a"
    fresh = "tests/launch/test_p0_search.py::test_b"
    _put_cases(farm, "R01", "R01_2026-09-22_0900", [_case(stale, "passed"), _case(fresh, "failed")])
    _put_cases(farm, "R01", "R01_2026-09-22_1000", [_case(fresh, "passed")])

    html = _render(farm)
    assert "来源轮次: R01_2026-09-22_0900" in html, "每格都要能查出来自哪一轮，否则没法判断是不是最新结论"
    assert html.count('<span class="carry">°</span>') == 1, "只有来自更早轮次的那一格该带 °"
    assert "R01_2026-09-22_1000 ← 最新" in html, "轮次表要标出哪一轮是最新"
    assert html.count("R01_2026-09-22_0900") >= 2, "轮次表要把老轮次也列出来"


def test_aggregate_shows_errors_as_their_own_column(farm, capsys):
    """error（setup 崩了、用例根本没执行）必须**单列**，不能混进「失败」。

    混进去，看报告的人就分不清「跑挂了」和「没跑起来」—— 而那正是假绿藏身的地方。
    报告里 `!` = error，控制台也要明确点出来。
    """
    _put_cases(farm, "R01", "R01_2026-09-22_1000", [
        _case("tests/unit/test_x.py::test_a", "error", "PermissionError: [WinError 5] 拒绝访问。"),
        _case("tests/unit/test_x.py::test_b", "failed", "AssertionError: 断言挂了"),
    ])

    html = _render(farm)
    assert ">!</td>" in html, "error 的符号该是 !（不是 ✗，也不是没见过的 ?）"
    assert "错误（setup 崩了，用例没执行）" in html, "图例要解释 ! 是什么"
    assert "<th>错误</th>" in html, "统计表要有独立的「错误」列"
    assert html.count("#8a5300") >= 2, "错误列和 error 格都用 error 色，别跟失败的红混在一起"

    out = capsys.readouterr().out
    assert "错误" in out and "setup" in out, "控制台要明确提示有 error，别让人只看个总数"


# ---------------- 节点索引：目录名 ↔ 套件 ----------------

# 2026-10-12 用户实报：一个节点跑了 133 轮，`results/<节点>/` 下全是
# `LAPTOP-x_2026-09-29_1349` / `..._1353` 这种**只带节点和时间**的目录名，
# 资源管理器里分不清哪个是哪个，也就没法「拿报告去对应校验」。
# 套件名本来就在每轮的 `node_env.json` 里（`farm_agent.py` 写的），
# 索引页的职责就是把它摊到目录这一层 —— 目录名不能改（节点侧已铺开，改名要全网 pull）。


def _put_suite_run(farm: Path, node: str, run: str, suite: str | None) -> Path:
    """`suite=None` 模拟老版本节点（压根不写 `node_env.json`）。"""
    env = None if suite is None else {
        "node": node, "task_id": "T1", "suite": suite, "credentials": {},
    }
    return _put_run(farm, node, run, node_env=env)


def _line_of(html: str, needle: str, *, require: str = "") -> str:
    """取含 `needle` 的那一行 —— 索引页每条 `<tr>` 都在自己一行上。

    `require` 用来消歧：同一个轮次名在页面里出现不止一次（索引页的「按套件看」表、
    汇总报告矩阵格的 `title="来源轮次: …"`），不加限定会取到错的那一行。
    """
    return next((ln for ln in html.splitlines() if needle in ln and require in ln), "")


def test_node_index_maps_each_run_dir_to_its_suite(farm):
    """**核心回归锁**：索引页要把「目录名 ↔ 套件」对到同一行上。

    只把套件名列出来还不够 —— 用户要的是「这个文件夹是哪套件」，
    所以目录名和套件必须落在同一行里，按套件筛一下就能只留他关心的那几轮。
    """
    _put_suite_run(farm, "R01", "R01_2026-09-29_1349", "launch")
    _put_suite_run(farm, "R01", "R01_2026-09-29_1352", "unit")

    assert farm_control.cmd_index(argparse.Namespace()) == 0
    html = (farm / "results" / "R01" / "index.html").read_text(encoding="utf-8")

    for run, suite in (("R01_2026-09-29_1349", "launch"), ("R01_2026-09-29_1352", "unit")):
        line = _line_of(html, run, require="data-suite=")
        assert line, f"{run} 该在索引表里有一行"
        assert f'data-suite="{suite}"' in line, f"{run} 那一行要标出套件 {suite}，否则还是对不上"
        assert f"./{run}/report.html" in line, f"{run} 要能点进它自己的报告"

    assert 'data-suite="?"' not in html, "node_env.json 里明明有 suite，不该退化成 ?"


def test_node_index_falls_back_to_module_names_without_node_env(farm):
    """老版本节点没写 `node_env.json` → 退化成 summary 里的 module 名，不能整列 `?`。

    回退不是可选项：`results/` 里存着 09 月的老结果，那些轮次永远不会有 node_env.json，
    没有回退的话一半的目录名照样对不上套件。
    """
    _put_suite_run(farm, "R01", "R01_2026-09-29_1349", None)

    assert farm_control.cmd_index(argparse.Namespace()) == 0
    html = (farm / "results" / "R01" / "index.html").read_text(encoding="utf-8")

    assert 'data-suite="launch"' in html, "没有 node_env.json 时该从 summary 的 module 名推断"
    assert 'data-suite="?"' not in html


def test_node_index_links_every_run_to_its_own_report(farm):
    """反向：索引页不许漏轮次 —— 漏掉的那一轮就又回到「分不清」了。"""
    runs = [f"R01_2026-09-29_13{h:02d}" for h in (49, 52, 53, 56, 58)]
    for run in runs:
        _put_suite_run(farm, "R01", run, "launch")

    assert farm_control.cmd_index(argparse.Namespace()) == 0
    html = (farm / "results" / "R01" / "index.html").read_text(encoding="utf-8")

    for run in runs:
        assert f"./{run}/report.html" in html, f"{run} 漏了"


# ---------------- 汇总报告的归档副本 ----------------

# 根目录那份 `aggregate_report.html` 是**固定文件名，每跑一次覆盖一次**。
# 10-09 那轮的汇总报告就是这么没的 —— 想跟 10-10 的结果做跨批次对比时，无据可查。
# 所以每跑一次要另存一份带时间戳的；根目录那份保留，因为手册/清单承诺的是那个路径。


def test_aggregate_archives_a_timestamped_copy(farm):
    """归档副本要与根目录那份**内容一致**，别生成个空壳。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000")

    _render(farm)

    archived = list((farm / "汇总").glob("aggregate_*.html"))
    assert len(archived) == 1, f"该有一份归档，实际 {len(archived)} 份"
    assert archived[0].read_text(encoding="utf-8") == \
        (farm / "aggregate_report.html").read_text(encoding="utf-8"), "归档副本内容该和根目录那份一样"


class _FrozenClock:
    """把 `farm_control.datetime` 换成一个 `now()` 固定的替身（`fromisoformat` 透传）。

    ## 为什么非要有它（2026-10-12）

    `test_second_aggregate_does_not_overwrite_the_first_archive` 要测的是
    「**同一秒**连跑两次会不会互相覆盖」。靠真实时钟的话，两次调用有一半概率落在**不同秒** ——
    那时压根没走到撞名分支，**用例测的是另一条路**，红了绿了都不代表真实行为。
    实测过：单跑 30 次全过、文件级连跑 5 轮全过，但全量套件里偶发红一次 —— 就是这个道理。

    把时钟钉死，这条用例才变成确定性的。
    """

    def __init__(self, frozen: datetime):
        self._frozen = frozen

    def now(self, tz=None) -> datetime:            # noqa: ARG002 —— 签名对齐真类
        return self._frozen

    @staticmethod
    def fromisoformat(s: str) -> datetime:
        return datetime.fromisoformat(s)


def test_second_aggregate_does_not_overwrite_the_first_archive(farm, monkeypatch):
    """连跑两次要留**两份**归档。

    同一秒连跑（改完代码立刻重跑，很常见）时间戳会撞 —— 撞了就加序号，
    否则「归档」退化成又一次覆盖，等于没做。

    **时钟钉死在同一秒**，保证每次都真走撞名分支（理由见 `_FrozenClock`）。
    """
    _put_run(farm, "R01", "R01_2026-09-21_1000")
    monkeypatch.setattr(farm_control, "datetime", _FrozenClock(datetime(2026, 10, 12, 14, 48, 30)))

    _render(farm)
    first = sorted((farm / "汇总").glob("aggregate_*.html"))
    assert len(first) == 1, f"第一次汇总该留一份归档，实际 {[p.name for p in first]}"

    _render(farm)
    archived = sorted((farm / "汇总").glob("aggregate_*.html"))
    assert len(archived) == 2, (
        f"同一秒两次汇总该留两份归档，实际 {len(archived)} 份：{[p.name for p in archived]}"
    )
    assert set(first) <= set(archived), (
        f"第一次那份必须还在，不能被覆盖：第一份 {[p.name for p in first]} / 现在 {[p.name for p in archived]}"
    )
    assert archived[0].name != archived[1].name, "两份归档不能同名"


def test_aggregate_lists_the_suite_for_every_run(farm):
    """汇总报告的「轮次」表也要有套件列 —— 从总报告要能直接对到磁盘上的目录。"""
    _put_suite_run(farm, "R01", "R01_2026-09-29_1349", "launch")
    _put_suite_run(farm, "R01", "R01_2026-09-29_1352", "unit")

    html = _render(farm)

    assert "<th>套件</th>" in html, "轮次表要有套件列"
    line = _line_of(html, "R01_2026-09-29_1352", require="report.html")
    assert "<td>unit</td>" in line, "轮次那一行要写出套件"
    assert "results/R01/R01_2026-09-29_1352/report.html" in html, "轮次名要能点进该轮报告"


# ---------------- done/ 清理（2026-10-13） ----------------

# `done/` 不只是「已完成」的展示 —— 它还是**凭据状态的兜底来源**
# （`_node_creds_from_receipts`：证据目录里没有 `node_env.json` 的老节点靠读它）。
# 2026-10-13 用户看到 `status` 报「已完成 48 个」问怎么清；实查那 48 份全是同一个节点、
# 跨 09-29~10-10，而兜底函数**按 mtime 只取最新一份** → 老的永远读不到。
# 所以：**每个节点留最新 N 份**，别删光。


def _put_done(farm: Path, node: str, name: str, *, creds: dict | None = None,
              mtime: float | None = None) -> Path:
    path = farm / "done" / name
    path.write_text(json.dumps({
        "node": node, "task_id": name, "ok": True,
        "credentials": creds if creds is not None else {"password_login": True},
    }, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_prune_keeps_newest_n_per_node(farm):
    """每个节点只留最新 N 份，老的删掉。"""
    for i in range(6):
        _put_done(farm, "R01", f"R01_t{i}.json", mtime=1_700_000_000 + i)
    removed = farm_control.prune_done(farm, keep_per_node=3)
    left = sorted(p.name for p in (farm / "done").glob("*.json"))
    assert len(removed) == 3
    assert left == ["R01_t3.json", "R01_t4.json", "R01_t5.json"], f"留的不是最新 3 份：{left}"


def test_prune_is_per_node_not_global(farm):
    """**按节点各自保留** —— 不能全局取前 N，否则某个节点会被清空、凭据列变 ✗。"""
    for i in range(4):
        _put_done(farm, "R01", f"R01_t{i}.json", mtime=1_700_000_000 + i)
    for i in range(2):
        _put_done(farm, "R02", f"R02_t{i}.json", mtime=1_700_000_000 + i)
    farm_control.prune_done(farm, keep_per_node=3)
    left = sorted(p.name for p in (farm / "done").glob("*.json"))
    assert "R02_t0.json" in left and "R02_t1.json" in left, "R02 只有 2 份，一份都不该删"
    assert len(left) == 5, f"应为 3(R01) + 2(R02) = 5，实际 {left}"


def test_prune_keeps_the_credentials_source_alive(farm):
    """**核心不变量**：清完之后 `_node_creds_from_receipts` 仍能取到每个节点的凭据。

    这就是「不能全删」的原因 —— `done/` 是凭据状态的兜底来源，
    删光了老节点的「凭据」列会全变 ✗（2026-10-12 修过的坑：
    一线会拿着全红的凭据列去反复折腾 `farm_node.env`）。
    """
    for i in range(8):
        _put_done(farm, "R01", f"R01_t{i}.json", creds={"password_login": True},
                  mtime=1_700_000_000 + i)
    _put_done(farm, "R02", "R02_only.json", creds={"microsoft_sso": True},
              mtime=1_700_000_000)

    farm_control.prune_done(farm, keep_per_node=3)

    creds = farm_control._node_creds_from_receipts(farm)
    assert creds.get("R01"), "R01 的凭据取不到了 —— 清理把凭据来源删没了"
    assert creds.get("R02"), "R02 只有一份，绝不能被删"


def test_prune_refuses_to_wipe_everything(farm):
    """`keep_per_node<=0` 直接不删 —— 防手滑传 0 把凭据来源清空。"""
    for i in range(4):
        _put_done(farm, "R01", f"R01_t{i}.json", mtime=1_700_000_000 + i)
    assert farm_control.prune_done(farm, keep_per_node=0) == []
    assert len(list((farm / "done").glob("*.json"))) == 4


def test_prune_tolerates_broken_receipts(farm):
    """读不了的 json 归到「无名」组，**也要按最新保留**，不能因为解析失败就删掉。"""
    (farm / "done" / "broken.json").write_text("{ 这不是 json", encoding="utf-8")
    _put_done(farm, "R01", "R01_t0.json", mtime=1_700_000_000)
    removed = farm_control.prune_done(farm, keep_per_node=3)
    assert removed == []
    assert (farm / "done" / "broken.json").exists(), "解析失败的回执不该被删"


def test_prune_on_missing_dir_is_noop(tmp_path):
    assert farm_control.prune_done(tmp_path, keep_per_node=3) == []
