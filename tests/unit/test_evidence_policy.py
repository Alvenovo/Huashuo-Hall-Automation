"""锁证据采集策略（`hall_auto/evidence.py` + `tests/conftest.py` 的 `--evidence`）。

## 为什么单开一个文件

证据是**交付物**：`report.html` 是自包含单文件，手册里写着"可直发组长"；
多机汇总（`farm_control.aggregate`）只读证据目录，不读 `done/` 回执。
但 2026-09-22 查的时候，`EvidenceSession` **一条用例都没有** —— 两个模式都没测。
（`farm_agent` 侧的 `evidence_snapshot` / `newest_run_dir` 有测，但那是"怎么找到本轮目录"，
不是"录不录、录成什么"。）

## 默认值为什么要锁

**当前默认是 `all`**（`tests/conftest.py` 的 `--evidence`）。

历史别记错：2026-09-22 曾把默认改成 `failure-only`（跑批产物太多：`reports/` 下已堆了 3.8 万张 PNG），
**2026-09-23 又改回 `all`** —— 因为查下来「成功也留证」有真实依赖（见下）。
**所以手册里写的「默认 all」是对的**，别再按"默认只留失败"理解。

改默认之前必须先确认**没人在依赖"成功也留证"**，查下来只有一处关键依赖：

- `farm_control.aggregate` 读的是 `summary.json`，**不是** per-case 的 `result.json`；
  而 `finish()` 无条件写 `summary.json` -> 汇总不受影响。
- `farm_agent.newest_run_dir` 靠"证据根目录快照前后差"认本轮目录，
  所以**必须**保证 `new_run_dir()` 无论什么模式都会建目录 —— 否则套件全绿时找不到本轮目录，
  农场会**张冠李戴**回传上一轮的旧目录（比没有证据危险得多，见 `newest_run_dir` 的注释）。

这两条都是"改了默认也不会坏"的**前提**，所以都在这里锁住。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import types
from pathlib import Path

import pytest
from PIL import Image

from hall_auto import evidence as ev

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

NODEID_OK = "tests/launch/test_p1_apps.py::test_list_renders"
NODEID_BAD = "tests/launch/test_p1_apps.py::test_install_flow"


@pytest.fixture
def fake_capture(monkeypatch):
    """替掉真抓屏 —— 单测不许碰屏幕，也要让"抓到了"这件事可断言。

    ⚠️ 这里**必须写出一张合法 PNG**，不能图省事写 `b"png"`：
    `conftest.pytest_runtest_makereport` 是在 **call 阶段结束、fixture 还没 teardown 时**
    截图的（它自己的注释就是这么写的），所以本 fixture 生效期间，**真 session 也会走这个假抓屏**。
    写个假文件的话，最后 `_render_html` 里 `Image.open()` 打不开，
    会把整个 session 的 `pytest_sessionfinish` 崩掉 —— 报错还指向真实证据目录，极难对上号。
    """
    calls: list[Path] = []

    def _fake(path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1, 1), (0, 0, 0)).save(path)
        calls.append(path)
        return True

    monkeypatch.setattr(ev, "capture_screen", _fake)
    return calls


def _session(tmp_path, mode):
    run = tmp_path / "run"
    run.mkdir()
    return ev.EvidenceSession(run, mode), run


def _case_dir(run: Path, nodeid: str) -> Path:
    return (run / ev._safe_path_name(ev.module_of(nodeid))
            / ev._safe_path_name(ev.case_label(nodeid)))


# ---------------- 两个模式各录什么 ----------------


def test_failure_only_records_evidence_only_for_failed_cases(tmp_path, fake_capture):
    """`failure-only` 下：失败留原图 + result.json，通过/跳过**一个文件都不写**。"""
    s, run = _session(tmp_path, ev.MODE_FAILURE)

    s.record(NODEID_OK, ev.OUTCOME_PASSED, 1.0, "assert True")
    assert not _case_dir(run, NODEID_OK).exists(), "通过用例不该留证据目录"
    assert fake_capture == [], "通过用例连抓屏都不该做（省时间也是改默认的动机之一）"

    s.record(NODEID_BAD, ev.OUTCOME_FAILED, 2.0, "assert False")
    d = _case_dir(run, NODEID_BAD)
    assert (d / "final.png").is_file(), "失败用例必须留原图 —— 那是排查的唯一线索"
    rec = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert rec["outcome"] == ev.OUTCOME_FAILED
    assert rec["assertion"] == "assert False"
    assert rec["screenshot"], "result.json 里要指向那张图，否则报告嵌不出来"


def test_all_mode_records_evidence_for_every_outcome(tmp_path, fake_capture):
    """`all` 下：三种结果都留证（显式指定时要能拿回老行为）。"""
    s, run = _session(tmp_path, ev.MODE_ALL)

    for nodeid, outcome in (
        (NODEID_OK, ev.OUTCOME_PASSED),
        (NODEID_BAD, ev.OUTCOME_FAILED),
        ("tests/launch/test_p1_apps.py::test_needs_creds", ev.OUTCOME_SKIPPED),
    ):
        s.record(nodeid, outcome, 0.5, "x")
        assert (_case_dir(run, nodeid) / "result.json").is_file(), f"{outcome} 该留证"


# ---------------- 汇总读的是 summary.json，不是 per-case ----------------


def test_finish_always_writes_summary_and_report_listing_every_case(tmp_path, fake_capture):
    """`finish()` 必须无条件写 `summary.json` + `report.html`，且**列全**所有用例。

    这条是改默认的**前提**：控制机汇总只读 `summary.json`。
    如果 failure-only 下 `summary.json` 只列失败用例（或干脆不写），
    那多机汇总报告会凭空少掉一堆通过的用例 —— 看着像"那台机器没跑"，比报错还难查。
    """
    s, run = _session(tmp_path, ev.MODE_FAILURE)
    s.record(NODEID_OK, ev.OUTCOME_PASSED, 1.0, "assert True")
    s.record(NODEID_BAD, ev.OUTCOME_FAILED, 2.0, "assert False")
    report = s.finish()

    assert report == run / "report.html" and report.is_file()
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == ev.MODE_FAILURE
    assert summary["total"] == 2, "通过的那条也要在 total 里 —— 它确实跑过了"
    assert summary["passed"] == 1 and summary["failed"] == 1
    nodeids = {c["nodeid"] for c in summary["cases"]}
    assert nodeids == {NODEID_OK, NODEID_BAD}, "不抓图的用例也要留在 cases 里"
    assert [c for c in summary["cases"] if c["nodeid"] == NODEID_OK][0]["screenshot"] is None


# ---------------- setup 阶段失败（pytest 的 ERROR）必须进记录 ----------------


def _load_conftest():
    """按**路径**加载 `tests/conftest.py`。

    不写 `import conftest`：跑 pytest 时它可能以 `conftest` / `tests.conftest`
    两种名字待在 `sys.modules` 里，按名字取不可靠。按路径加载才稳定。
    """
    spec = importlib.util.spec_from_file_location(
        "_hall_conftest_guard", REPO_ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeReport:
    """只带 hook 用到的那几个字段的假 report。"""

    def __init__(self, when, *, failed=False, skipped=False, longrepr="", duration=0.1):
        self.when = when
        self.failed = failed
        self.skipped = skipped
        self.longrepr = longrepr
        self.duration = duration


class _FakeItem:
    def __init__(self, nodeid, evidence):
        self.nodeid = nodeid
        self.config = types.SimpleNamespace(_evidence=evidence)
        self.function = types.SimpleNamespace(__doc__="用例说明")

    def iter_markers(self):
        return []

    def get_closest_marker(self, name):
        """conftest 的 `_wants_screenshot` 用它判「是不是 unit 用例」。

        假 item 没有 marker → 返回 None → 按「要抓屏」处理，
        和这些用例原来的预期一致。
        """
        return next((m for m in self.iter_markers() if getattr(m, "name", "") == name), None)


def _feed_report(conftest_mod, item, report):
    """驱动 hookwrapper 版的 `pytest_runtest_makereport`：走到 yield，再把假 report 交回去。"""
    gen = conftest_mod.pytest_runtest_makereport(item, None)
    next(gen)

    class _Outcome:
        def get_result(self):
            return report

    try:
        gen.send(_Outcome())
    except StopIteration:
        pass


def test_setup_error_is_recorded_so_it_cannot_be_silently_dropped(tmp_path, fake_capture):
    """**这条锁的是 2026-09-22 的假绿根因。**

    `pytest_runtest_makereport` 原先只认 `when == "call"`，外加 `when == "setup"` 的
    **skip** —— `setup` 阶段失败（= pytest 的 ERROR，fixture 崩了、用例一行都没执行）
    **没有分支，直接被丢弃**。

    真机实测后果：节点上 188 条用例因 `%TEMP%\\pytest-of-admin` 拒绝访问而在 setup 就崩，
    `summary.json` 却写 `total 273 / passed 273 / failed 0`；而
    `farm_control.aggregate` 按 nodeid 合并跨轮结果、**新那轮没记录就保留老记录**，
    于是矩阵里那一格挂着更早一轮的 ✓ —— 整行看着全绿。

    所以这里断言三件事：进 records、进 summary 的 `errors`、**不混进 failed**。
    """
    s, run = _session(tmp_path, ev.MODE_FAILURE)
    item = _FakeItem(NODEID_BAD, s)
    _feed_report(
        _load_conftest(), item,
        _FakeReport("setup", failed=True, longrepr="PermissionError: [WinError 5] 拒绝访问。"),
    )

    assert [r.outcome for r in s.records] == [ev.OUTCOME_ERROR], (
        "setup 阶段失败没进记录 —— 汇总报告那一格会留着上一轮的旧结果，假绿又回来了"
    )

    report = s.finish()
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 1 and summary["errors"] == 1, "error 没进 summary —— 汇总层还是看不见它"
    assert summary["failed"] == 0, (
        "error 不该混进失败列：看报告的人要能区分「跑挂了」和「根本没跑起来」"
    )
    assert (_case_dir(run, NODEID_BAD) / "final.png").is_file(), (
        "error 也要留图 —— 临时目录被拒这类现场只能靠截图复现"
    )
    html = report.read_text(encoding="utf-8")
    assert "错误" in html, "报告里要能一眼看到「错误」这一档，不能只有通过/失败/跳过"


def test_setup_skip_is_still_recorded_as_skipped(tmp_path, fake_capture):
    """补 error 分支别把原有的 setup-skip 分支挤掉（缺凭据/缺夹具靠它才看得见）。"""
    s, _ = _session(tmp_path, ev.MODE_FAILURE)
    item = _FakeItem(NODEID_OK, s)
    _feed_report(_load_conftest(), item, _FakeReport("setup", skipped=True, longrepr="Skipped: 缺凭据"))

    assert [r.outcome for r in s.records] == [ev.OUTCOME_SKIPPED]
    assert "缺凭据" in s.records[0].assertion


def test_call_phase_result_is_unchanged(tmp_path, fake_capture):
    """call 阶段的行为不能被这次改动带偏（通过/失败仍照旧）。"""
    s, _ = _session(tmp_path, ev.MODE_FAILURE)
    item = _FakeItem(NODEID_OK, s)
    _feed_report(_load_conftest(), item, _FakeReport("call", failed=False))

    assert [r.outcome for r in s.records] == [ev.OUTCOME_PASSED]


# ---------------- 农场靠它认本轮目录：目录必须无条件建出来 ----------------


def test_new_run_dir_creates_the_directory_even_with_no_evidence(tmp_path):
    """**这条是 failure-only 能安全当默认的关键**。

    `farm_agent.newest_run_dir()` 用"跑之前的目录快照"排除上一轮 —— 本轮目录**必须存在**，
    它才认得出来。所以 `new_run_dir()` 无论什么模式都得建目录：
    套件全绿、一张图都没有，目录也要在。否则农场会回传**上一轮的旧目录**，
    报告里那台机器挂着一份别的运行的结果。
    """
    run = ev.new_run_dir(root=tmp_path, node="R01")

    assert run.is_dir(), "目录没建出来 —— 农场就认不出本轮"
    # 2026-10-15 起是两层：`<根>/<节点>/<时间戳>`。节点那层同样是防多机回传撞名，
    # 而且和农场侧（results/<节点>/<轮次>/）结构对齐。
    assert run.parent.name == "R01", f"节点那层不对：{run}"
    assert run.parent.parent == tmp_path, f"层次不对：{run}"
    assert run.name[:4].isdigit() and "_" in run.name, f"轮次目录名该是 <日期>_<时刻>：{run.name}"


def test_new_run_dir_does_not_collide_within_the_same_minute(tmp_path):
    """同一分钟起两次（重跑/多套件）不能撞名 —— 撞了就是两份结果互相覆盖。"""
    a = ev.new_run_dir(root=tmp_path, node="R01")
    b = ev.new_run_dir(root=tmp_path, node="R01")

    assert a != b and a.is_dir() and b.is_dir()


# ---------------- 默认值：代码与手册必须说同一句话 ----------------


def _conftest_evidence_default() -> str:
    """从 `tests/conftest.py` 的 `--evidence` 注册里读出 `default=` 的实际值。"""
    src = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "addoption"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--evidence"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "default":
                continue
            if isinstance(kw.value, ast.Constant):
                return kw.value.value
            # conftest 里写的是常量名（MODE_FAILURE），按 evidence 模块里的真值解析
            assert isinstance(kw.value, ast.Name), f"看不懂的 default 写法：{ast.dump(kw.value)}"
            return getattr(ev, kw.value.id)
    raise AssertionError("`tests/conftest.py` 里找不到 `--evidence` 的 default —— 守卫失效了")


def test_evidence_default_is_all():
    """2026-09-23 定调：默认 `all`（成功失败都留证）。

    **2026-09-22 曾定成 `failure-only`**（`reports/` 堆到 3.8 万张 PNG），
    2026-09-23 用户要求改回 `all` —— 理由是排查时**通过用例的现场图也有用**
    （"这一步界面长什么样" 只能从通过那刻的图看，失败图只有崩掉那一刻）。

    ⚠️ **代价要知道**：每台机器每轮全量档约 650 张图（`unit` 618 条也各一张），
    比 `failure-only` 多约 150-200MB/轮。盘紧张时用 `--evidence=failure-only` 压回去。

    锁住是为了**别被静默改回去**：这个值决定每台机器每轮跑批往盘上写多少东西，
    改它该是一次有意识的决定，不是顺手。
    """
    assert _conftest_evidence_default() == ev.MODE_ALL
    assert ev.MODE_ALL == "all", "命令行取值变了，手册和脚本都在用字面量"


def test_runbook_states_the_same_default_as_conftest():
    """手册写的默认值必须跟 conftest 一致 —— 不然一线按手册加开关会加反。"""
    default = _conftest_evidence_default()
    text = (REPO_ROOT / "项目知识库" / "运行手册.md").read_text(encoding="utf-8")

    assert f"`--evidence={default}`（默认）" in text, (
        f"运行手册里没写「`--evidence={default}`（默认）」—— "
        "默认值改了但手册没跟上，一线会以为要显式加 failure-only，或者反过来以为默认是全留"
    )


# ---------------- 失败文本必须留下**根因**（不能只截头）----------------
#
# 2026-09-23 真机踩出来的：原来是 `text[:800]`。pytest 的 `longrepr` 是完整调用链，
# 顺序是「用例源码 → `_ _ _` 逐层往下 → **真正的异常类型与消息在最后**」。
# 截前 800 字符 = 留下了我们本来就有的用例源码、丢掉了唯一有用的异常行。
#
# 后果很具体：`apps-lifecycle` 的 `test_fixture_install_then_uninstall` 失败，
# 报告里那段文字停在 `> matched = install_fixture(` 就没了 ——
# 异常是什么、为什么，一个字都看不到，只能去节点翻 `reports\_elev\elevated_run.log`。


class _LongRepr:
    """只带 `longrepr` 的假 report（`_assertion_text` 只用这一个字段）。"""

    def __init__(self, text: str):
        self.longrepr = text


def test_assertion_text_keeps_the_exception_at_the_tail():
    """**核心回归锁**：超长时尾巴必须留 —— 根因就在尾巴上。"""
    conftest_mod = _load_conftest()
    traceback_text = (
        "用例自己的源码（几十行，本来就有）\n" * 60
        + "E   RuntimeError: 钉住安装包不存在：C:/.../fixtures/xxx.exe"
    )
    assert len(traceback_text) > conftest_mod.ASSERTION_HEAD + conftest_mod.ASSERTION_TAIL, (
        "这条用例要造一段**真的超长**的文本，否则走不到截断分支，等于没测"
    )
    out = conftest_mod._assertion_text(_LongRepr(traceback_text))

    assert "RuntimeError: 钉住安装包不存在" in out, (
        "截断把根因丢了 —— 报告里只剩用例源码，等于没有报错"
    )
    assert "省略" in out, "要显式标出中间被省略了多少，别让人以为原文就这么短"
    assert len(out) < len(traceback_text), "超长时要真的截，不能原样返回（报告会爆）"


def test_assertion_text_keeps_short_text_intact():
    """短文本原样返回 —— 别为了统一格式给正常失败加省略号。"""
    conftest_mod = _load_conftest()
    short = "assert False"
    assert conftest_mod._assertion_text(_LongRepr(short)) == short


def test_assertion_text_handles_empty_longrepr():
    conftest_mod = _load_conftest()
    assert conftest_mod._assertion_text(_LongRepr("")) == ""


# ---------------- --evidence=all 下 unit 不抓屏（2026-09-23）----------------
#
# `--evidence=all` 一开，**每一条**用例都抓一次屏。而 `tests/unit` 有 600+ 条
# **纯逻辑**用例，它们不碰 UI —— 抓下来的是**桌面**，一条都没有排查价值，
# 代价却是实测 `pytest tests/unit` 从 **16s → 149s**（633 次抓屏）。
# 单测的结论在 `summary.json` / `report.html` 里一条都不少，所以省掉的不是信息。


def test_unit_cases_do_not_take_screenshots():
    """**核心**：`unit` 用例不抓屏。这条红了**别改断言**，去看 `_wants_screenshot`。"""
    conftest_mod = _load_conftest()

    class _Item:
        def __init__(self, unit: bool):
            self._unit = unit

        def get_closest_marker(self, name):
            return types.SimpleNamespace(name=name) if (self._unit and name == "unit") else None

    assert conftest_mod._wants_screenshot(_Item(unit=True)) is False, (
        "unit 用例抓的是桌面，没价值还慢 10 倍"
    )
    assert conftest_mod._wants_screenshot(_Item(unit=False)) is True, (
        "UI 套件要抓 —— `--evidence=all` 的意图就是「通过用例也留现场图」"
    )


def test_capture_false_writes_no_evidence_dir(tmp_path, fake_capture):
    """`capture=False` **连目录都不建**（不只是不抓屏），但**结论仍要记进 summary**。

    守卫这条是为了别被"顺手改成只跳过抓屏" —— 那会留下 600+ 个**空目录**，
    回传时白拷一堆目录项。
    """
    s, run = _session(tmp_path, ev.MODE_ALL)
    rec = s.record(NODEID_BAD, ev.OUTCOME_FAILED, 1.0, "boom", capture=False)

    assert not _case_dir(run, NODEID_BAD).exists(), "capture=False 不该建证据目录"
    assert fake_capture == [], "capture=False 连抓屏都不该尝试"
    assert rec.outcome == ev.OUTCOME_FAILED, "结论仍要记进 summary（省的是图，不是信息）"
    assert s.records == [rec]


# ---------------- 报告默认把「有截图」的排前面（2026-09-29）----------------
#
# `--evidence=all` 下一轮报告里，600+ 条 unit 用例是 `capture=False`（无图），
# 和十几条 UI 用例（有终态截图）按执行顺序混在一起。人工回看现场图要一路翻过
# 几百行「无截图」。改成默认就把有图的排在最前面（组内仍按执行顺序），
# 再配一个「只看有截图」筛选彻底隐藏无图的。


def test_report_puts_cases_with_screenshots_first(tmp_path, fake_capture):
    """有图的行排在无图的行前面，且各行带 data-img 标记 + 工具栏有「只看有截图」按钮。"""
    s, run = _session(tmp_path, ev.MODE_FAILURE)
    s.record(NODEID_OK, ev.OUTCOME_PASSED, 1.0, "assert True")   # failure-only：通过不留图
    s.record(NODEID_BAD, ev.OUTCOME_FAILED, 2.0, "assert False") # 失败留图
    s.finish()
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    # finish() 默认按 EVIDENCE_ROOT/<run名> 找图；测试 run 在 tmp_path 下，显式传 run_dir 才嵌得进去
    html_text = ev._render_html(summary, run_dir=run)

    assert 'data-f="img"' in html_text and "只看有截图" in html_text, (
        "工具栏少了「只看有截图」按钮 —— 人工又得翻过几百行无图的 unit 用例"
    )
    # summary["cases"] 里通过那条在前；渲染后必须把有图（失败）那条排到前面
    assert summary["cases"][0]["nodeid"] == NODEID_OK, "前提：录入顺序里无图的通过用例在前"
    pos_img = html_text.index('data-img="1"')
    pos_noimg = html_text.index('data-img="0"')
    assert pos_img < pos_noimg, "有截图的行要默认排在无截图的行前面，省得人工翻页"
    assert "r.dataset.img==='1'" in html_text, "筛选逻辑要按 data-img 过滤，别只看结果状态"


# ---------------------------------------------------------------------------
# 证据清理：两种目录名都要认（2026-10-13 修）
# ---------------------------------------------------------------------------


def test_run_time_parses_both_dir_name_formats():
    """轮次目录名有两种格式，**都要能取到时间**。

    早期是 `2026-09-17_1052`；多机之后是 `<节点>_2026-10-13_1405`（同秒撞了再加 `_2`）。
    原来的实现用 `name.split("_")[:2]` 拼日期 —— 对带节点前缀的格式抛 ValueError
    被 `continue` 吃掉，于是**那些目录永远不被清理**。
    2026-10-13 实测：本机 1140 个轮次目录里 1124 个（98.6%）属于这种，证据无限累积。
    """
    from datetime import datetime

    assert ev._run_time("2026-09-17_1052") == datetime(2026, 9, 17, 10, 52)
    assert ev._run_time("LAPTOP-VS5F7HF4_2026-10-13_1405") == datetime(2026, 10, 13, 14, 5)
    assert ev._run_time("LAPTOP-VS5F7HF4_2026-10-13_1405_2") == datetime(2026, 10, 13, 14, 5)


def test_run_time_returns_none_for_unparseable_names():
    """取不到时间就返回 None —— 调用方**不删**（不猜、不误删）。"""
    assert ev._run_time("汇总") is None
    assert ev._run_time("report") is None
    assert ev._run_time("") is None


def test_prune_removes_node_prefixed_dirs_too(tmp_path, monkeypatch):
    """**核心回归**：带节点前缀的旧轮次也必须被清掉（这是修的那个 bug）。"""
    from datetime import datetime, timedelta

    root = tmp_path / "evidence"
    root.mkdir()
    old = datetime.now() - timedelta(days=40)
    fresh = datetime.now() - timedelta(days=1)
    old_dir = root / f"LAPTOP-x_{old.strftime('%Y-%m-%d_%H%M')}"
    fresh_dir = root / f"LAPTOP-x_{fresh.strftime('%Y-%m-%d_%H%M')}"
    old_dir.mkdir()
    fresh_dir.mkdir()
    (old_dir / "final.png").write_bytes(b"x")
    (fresh_dir / "final.png").write_bytes(b"x")

    removed = ev.prune_old_runs(root, keep_days=30)

    assert old_dir in removed, "带节点前缀的过期轮次没被清 —— 就是这次修的 bug"
    assert not old_dir.exists()
    assert fresh_dir.exists(), "30 天内的不能删"


def test_prune_never_touches_unparseable_dirs(tmp_path):
    """名字里没有时间的目录**不许删** —— 宁可留着，也别误删汇总之类的东西。"""
    root = tmp_path / "evidence"
    root.mkdir()
    keep = root / "汇总"
    keep.mkdir()
    (keep / "x.html").write_text("x", encoding="utf-8")

    assert ev.prune_old_runs(root, keep_days=1) == []
    assert keep.exists()


# ---------------------------------------------------------------------------
# 用例目录名：编号_标题（2026-10-13）
#
# 原来直接用函数名（`test_p0_06_search_lists_keyword_titles`）—— 能看出大意但读起来费劲，
# 也跟用例清单对不上号。现在用「编号_标题」，能直接去 cases/cases.yaml / 清单 / 禅道对。
# ---------------------------------------------------------------------------


def test_case_label_uses_id_and_title():
    """登记的用例 -> `编号_标题`；**编号是稳定键**（标题改了编号不改），还能防重名。"""
    assert ev.case_label(
        "tests/launch/test_p0_search.py::test_p0_06_search_lists_keyword_titles"
    ) == "P0-06_搜索命中"


def test_case_label_handles_page_bound_cases():
    """按页面挂载的用例：**pytest 的参数 id 就是用例编号**，直接从 nodeid 里取。"""
    assert ev.case_label(
        "tests/launch/test_p1_register.py::test_cases_register[P1C-01]"
    ) == "P1C-01_注册页控件图"


def test_case_label_ignores_param_ids_that_are_not_case_ids():
    """参数化用例的参数 id 是 `short-3` 这种，**不是**用例编号 -> 退回按函数名查。"""
    assert ev.case_label(
        "tests/launch/test_p1_register.py::test_register_invalid_password_blocks_submit[short-3]"
    ) == "P1C-03_非法密码拦截（参数化）"


def test_case_label_falls_back_to_function_name():
    """**命名失败绝不能影响取证** —— 查不到就用函数名，不抛、不中断。"""
    assert ev.case_label("tests/unit/test_xxx.py::test_some_guard") == "test_some_guard"
    assert ev.case_label(
        "tests/launch/test_x.py::test_brand_new_not_registered_yet"
    ) == "test_brand_new_not_registered_yet"


def test_safe_path_name_keeps_chinese_and_full_width_punctuation():
    """**目录名清洗只剔真正非法的字符** —— 中文、全角括号、「」、冒号都要原样保留。

    原来的 `_safe_name` 只留 CJK 汉字，会把 `（默认页签）` 洗成 `_默认页签_`（尾巴多个下划线），
    「一眼看出是哪条用例」就没了。所以新加了路径专用的清洗。
    """
    assert ev._safe_path_name("P1B-01a_登录弹窗控件图（默认页签）") == "P1B-01a_登录弹窗控件图（默认页签）"
    assert ev._safe_path_name("P0-06_搜索命中") == "P0-06_搜索命中"
    assert ev._safe_path_name("只读探针：我的三个列表 + 公网可达性") == "只读探针：我的三个列表 + 公网可达性"
    assert ev._safe_path_name("注册页不该出现「邀请码」输入框") == "注册页不该出现「邀请码」输入框"
    assert "/" not in ev._safe_path_name("a/b"), "路径分隔符必须被剔掉"
    assert ":" not in ev._safe_path_name("a:b"), "冒号（半角）必须被剔掉，否则 Windows 建不了目录"


def test_safe_name_still_normalizes_for_apps_comparison():
    """⚠️ `_safe_name` **不能**跟着改 —— `apps.py` 拿它比对界面文字，
    「把标点洗成 `_`」是那个比对口径的一部分。这条钉住它别被顺手"优化"掉。"""
    assert ev._safe_name("P1B-01a_登录弹窗控件图（默认页签）") == "P1B-01a_登录弹窗控件图_默认页签_"


def test_record_puts_the_label_in_the_path(tmp_path):
    """**端到端**：`record()` 真的把 `编号_标题` 用在目录名上（不是只算了个变量没用）。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = ev.EvidenceSession(run_dir, ev.MODE_FAILURE)

    def _no_capture(_path):
        return False

    import hall_auto.evidence as mod

    original = mod.capture_screen
    mod.capture_screen = _no_capture
    try:
        rec = session.record(
            "tests/launch/test_p0_search.py::test_p0_06_search_lists_keyword_titles",
            ev.OUTCOME_FAILED, 0.1, "assert x",
        )
    finally:
        mod.capture_screen = original

    assert (run_dir / "launch" / "P0-06_搜索命中").is_dir(), (
        "用例目录没叫「编号_标题」—— 命名改了但 record 没用上"
    )
    assert rec.screenshot is None or "P0-06_搜索命中" in rec.screenshot


# ---------------------------------------------------------------------------
# 本地索引页 + --evidence=off（2026-10-13）
#
# 用户实报：`reports/evidence/` 底下堆成一片 `LAPTOP-x_2026-10-13_1740_8`，
# 「看不懂每个模块存在哪」。农场侧 10-12 就有 index.html，本地这份一直没有。
# ---------------------------------------------------------------------------


def _fake_run(root: Path, name: str, *, modules: list[str], passed: int = 1, failed: int = 0) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)   # 两层结构下 root 可能是 `<证据根>/<节点>`，得建父目录
    (d / "summary.json").write_text(json.dumps({
        "run": name, "node": "N", "total": passed + failed, "passed": passed,
        "failed": failed, "error": 0, "skipped": 0, "duration_s": 1.0,
        "cases": [{"module": m, "case": "c", "nodeid": f"tests/{m}/x.py::test_c",
                   "outcome": "passed", "duration_s": 0.1, "timestamp": "t",
                   "assertion": None, "screenshot": None} for m in modules],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "report.html").write_text("<html></html>", encoding="utf-8")
    for m in modules:
        (d / m / "P0-01_示例").mkdir(parents=True)


def test_local_index_lists_every_run_with_suite_and_counts(tmp_path):
    """索引页一轮一行，**带套件名和过红跳** —— 光看目录名看不出这些。"""
    root = tmp_path / "evidence"
    root.mkdir()
    _fake_run(root, "N_2026-10-13_1000", modules=["launch"], passed=4)
    _fake_run(root, "N_2026-10-13_1001", modules=["unit"], passed=8, failed=2)

    out = ev.write_local_index(root)

    assert out is not None and out.name == "index.html"
    html = out.read_text(encoding="utf-8")
    assert html.count("<tr><td><a href=") == 2, "每轮该有一行"
    assert "launch" in html and "unit" in html, "要能看出跑了哪个套件"
    assert "N_2026-10-13_1000/report.html" in html, "每轮要能点进它自己的报告"
    assert "8" in html and "2" in html, "过/红要列出来"


def test_local_index_says_where_module_evidence_lives(tmp_path):
    """要写清「用例证据在 `<轮次>/<模块>/` 下」—— 用户原话是「看不懂每个模块存在哪」。"""
    root = tmp_path / "evidence"
    root.mkdir()
    _fake_run(root, "N_2026-10-13_1000", modules=["launch"])

    html = (ev.write_local_index(root)).read_text(encoding="utf-8")

    assert "模块" in html
    assert "&lt;轮次&gt;/&lt;模块&gt;/" in html, "要把路径写出来"
    assert "launch" in html


def test_local_index_skips_runs_without_summary(tmp_path):
    """没 `summary.json` 的目录不进索引（半截的、手工建的都不该出现）。"""
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "乱七八糟").mkdir()
    _fake_run(root, "N_2026-10-13_1000", modules=["launch"])

    html = (ev.write_local_index(root)).read_text(encoding="utf-8")
    assert "乱七八糟" not in html
    assert html.count("<tr><td><a href=") == 1


def test_local_index_returns_none_when_nothing_to_index(tmp_path):
    """没有可索引的轮次就不写空索引（返回 None）。"""
    root = tmp_path / "evidence"
    root.mkdir()
    assert ev.write_local_index(root) is None
    assert not (root / "index.html").exists()


def test_evidence_off_leaves_no_run_dir():
    """**`--evidence=off` 必须一个轮次目录都不建**（本地反复跑开发时用）。

    2026-10-13 用户实报 `reports/evidence/` 堆了 34 个看不出差别的目录 ——
    全是他/我反复跑单测留下的，每跑一次建一个。
    这条**跑真 pytest**（选项和 sessionfinish 的联动只有真跑才验得到）。
    """
    import subprocess
    import sys

    root = ev.EVIDENCE_ROOT
    before = {p.name for p in root.iterdir() if p.is_dir()}
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest",
         "tests/unit/test_evidence_policy.py::test_run_time_returns_none_for_unparseable_names",
         "-m", "unit", "-q", "--evidence=off"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    after = {p.name for p in root.iterdir() if p.is_dir()}
    # ⚠️ **不看 returncode**：本机沙箱有个「批量删除守卫」，pytest 清临时目录时
    # 文件数一超阈值就会拦下来（日志里能看到 SAFE_DELETE_BULK_CONFIRM_REQUIRED），
    # 那时 pytest 会因为没走完收尾而返回 1 —— 但用例其实是过的（输出里没有 failed）。
    # 2026-10-13 实测：全量单测因此假红过一次（909 过 0 红，退出码却是 1）。
    assert "failed" not in (proc.stdout or ""), f"off 跑失败了：\n{proc.stdout[-800:]}"
    assert after == before, f"`--evidence=off` 还是建了轮次目录：{sorted(after - before)}"


# ---------------------------------------------------------------------------
# 证据两层结构（`<节点>/<时间戳>`，2026-10-15）
# ---------------------------------------------------------------------------


def test_new_run_dir_is_two_levels(tmp_path):
    """一轮 = `<根>/<节点>/<时间戳>`，同分钟撞了加 `_2`。"""
    a = ev.new_run_dir(root=tmp_path, node="R01")
    b = ev.new_run_dir(root=tmp_path, node="R01")
    assert a.parent == tmp_path / "R01" and b.parent == tmp_path / "R01"
    assert a.name != b.name and b.name.endswith("_2")
    assert ev.new_run_dir(root=tmp_path, node="R02").parent == tmp_path / "R02"


def test_run_dirs_accepts_both_layouts(tmp_path):
    """**两种结构都要认** —— 别的机器 pull 之后老目录还在，不清它们。"""
    (tmp_path / "NODE-A" / "2026-10-15_1003").mkdir(parents=True)
    (tmp_path / "NODE-A" / "2026-10-15_1003_2").mkdir()
    (tmp_path / "NODE-B_2026-10-14_0900").mkdir()          # 老：平铺
    (tmp_path / "汇总").mkdir()                            # 不是轮次目录
    got = [d.relative_to(tmp_path).as_posix() for d in ev.run_dirs(tmp_path)]
    assert got == ["NODE-A/2026-10-15_1003", "NODE-A/2026-10-15_1003_2",
                   "NODE-B_2026-10-14_0900"], got


def test_prune_removes_old_runs_in_both_layouts_and_the_empty_node_dir(tmp_path):
    """两层都要能清；清完的**空节点目录也要收掉**（不然留一堆空壳）。"""
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d_%H%M")
    fresh = datetime.now().strftime("%Y-%m-%d_%H%M")
    (tmp_path / "NODE-A" / old).mkdir(parents=True)
    (tmp_path / "NODE-A" / fresh).mkdir()
    (tmp_path / f"NODE-B_{old}").mkdir()

    removed = ev.prune_old_runs(tmp_path, keep_days=30)

    assert len(removed) == 2, f"两层各该清一个：{removed}"
    assert (tmp_path / "NODE-A" / fresh).is_dir(), "30 天内的不能删"
    assert (tmp_path / "NODE-A").is_dir(), "还有轮次，节点目录不能删"
    # 老结构那轮被清掉之后没有节点目录可言；新结构被清空的节点目录才该收掉
    (tmp_path / "NODE-C" / old).mkdir(parents=True)
    ev.prune_old_runs(tmp_path, keep_days=30)
    assert not (tmp_path / "NODE-C").exists(), "被清空的节点目录该收掉，别留空壳"


def test_local_index_shows_which_node_each_run_belongs_to(tmp_path):
    """索引页要标出轮次属于哪个节点（一层节点之后，"这台机器跑过哪些轮"才看得出来）。"""
    _fake_run(tmp_path / "NODE-A", "2026-10-15_1003", modules=["launch"])
    html = (ev.write_local_index(tmp_path)).read_text(encoding="utf-8")
    assert "NODE-A/2026-10-15_1003" in html, "轮次名要带节点前缀"
