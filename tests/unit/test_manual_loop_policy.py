"""锁「人在环」的交互便利设施。

三件事：
1. `hall_auto.console.focus_console()` —— 点完「发送验证码」把终端抢到前台，
   人不用手动 Alt+Tab。**它只是便利，绝不能成为用例的前置条件或断言的一部分。**
2. 人在环用例里**只许通过 `_ask_code()` 读 stdin** —— 那一处负责"先切窗口再问人"。
3. 重输验证码时**只许重问、不许重发码**（2026-09-28 加）。

为什么要专门锁第 2 条（2026-09-22 用户明确要求「点完发送验证码自动跳到终端」）：
散着写 4 处 `input()` 的话，下次加第 5 个码就会漏掉切窗口，
而漏了**不报错**，只是人得自己 Alt+Tab —— 这类"静默降级"最不容易被发现。

第 3 条同理：把发码塞进重试里，功能看着正常（重输能过），
只是每次都多发一条短信，静默地把测试号发到限流。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hall_auto import console

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_TESTS = REPO_ROOT / "tests" / "launch" / "test_p1_login.py"
LOGIN_MODULE = REPO_ROOT / "hall_auto" / "login.py"

# 「发码」这一类调用。重试循环里只要出现一个，就意味着**每次重输都会再发一次码**：
# 短信撞服务端限流（见 `request_forgot_sms` 的注释）、邮件要人再翻一次邮箱。
# 重试的全部价值就在"码不重发"，所以这两条守卫盯的就是它。
SEND_CODE_CALLS = (
    "request_sms_code",
    "request_forgot_sms",
    "microsoft_send_code",
    "fill_bind_phone",
)


# ---------- focus_console：拿不到窗口是常态，绝不能抛 ----------


@pytest.mark.unit
def test_focus_console_returns_a_bool_and_never_raises():
    assert isinstance(console.focus_console(), bool)


@pytest.mark.unit
def test_focus_console_swallows_finder_errors(monkeypatch):
    """**关键保护**：三条找窗口的路全炸时也要安静返回 False。

    终端类型没覆盖到是**正常情况**（用户可能跑在 VS Code 集成终端里），
    这时候抛异常会把「省一次 Alt+Tab」的便利变成用例失败 —— 完全不成比例。
    """

    def _boom():
        raise OSError("拿不到窗口")

    monkeypatch.setattr(console, "_console_hwnd", _boom)
    monkeypatch.setattr(console, "_console_title_hwnd", _boom)
    monkeypatch.setattr(console, "_ancestor_window", _boom)
    assert console.focus_console() is False


@pytest.mark.unit
def test_ancestor_chain_is_bounded():
    """爬父进程链必须封顶 —— 环或异常表都会让它转不出来。"""
    assert len(console._ancestor_pids()) <= 8


@pytest.mark.unit
def test_desktop_shell_windows_are_never_focused():
    """**关键保护**：桌面/任务栏被提到前台 = **所有窗口最小化**，比不聚焦还糟。

    `explorer.exe` 就在祖先进程链上，不排掉就会聚焦到「Program Manager」。
    """
    assert {"Progman", "WorkerW", "Shell_TrayWnd"} <= console._SHELL_WINDOW_CLASSES
    assert "explorer.exe" in console._SHELL_EXES


# ---------- 人在环用例：只许通过 _ask_code 读 stdin ----------


def _manual_tree() -> ast.Module:
    return ast.parse(MANUAL_TESTS.read_text(encoding="utf-8"))


def _calls(node: ast.AST, func_name: str) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == func_name:
            out.append(sub)
    return out


@pytest.mark.unit
def test_ask_code_is_the_only_place_that_reads_stdin():
    """**关键保护**：除了 `_ask_code` 自己，不许别处直接 `input()`。

    绕过去 = 少了"先把终端提到前台"，人得自己切窗口 —— 而且不报错，纯静默降级。
    """
    tree = _manual_tree()
    helper = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_ask_code"),
        None,
    )
    assert helper is not None, "`_ask_code` 不见了 —— 切窗口那步也跟着没了"
    allowed = {id(call) for call in _calls(helper, "input")}
    assert allowed, "`_ask_code` 里没有 input()，那它就不是那个唯一入口了"

    offenders = [
        call.lineno
        for call in _calls(tree, "input")
        if id(call) not in allowed
    ]
    assert not offenders, f"这些行绕过了 _ask_code 直接 input()：{offenders}"


@pytest.mark.unit
def test_ask_code_focuses_the_terminal_before_reading():
    """顺序不能反：**先切窗口，再问人**。反了等于没做（提示语出现时窗口还是后台）。"""
    helper = next(
        n
        for n in ast.walk(_manual_tree())
        if isinstance(n, ast.FunctionDef) and n.name == "_ask_code"
    )
    focus_lines = [c.lineno for c in _calls(helper, "focus_console")]
    input_lines = [c.lineno for c in _calls(helper, "input")]
    assert focus_lines and input_lines
    assert min(focus_lines) < min(input_lines), "_ask_code 里 focus_console 没有排在 input 前面"


# ---------- 人在环重输：只许重问，不许重发码 ----------
#
# 2026-09-28：验证码输错了原来只能重跑整条（会重新发码）。改成"原地重输"之后，
# 最容易被后人写坏的地方就是**把发码塞进重试里** —— 那样功能看着正常
# （重输能过），只是每次都多发一条短信，静默地把测试号发到超限。
# 所以这里用静态检查把它钉住，而不是靠人记得。


def _login_tree() -> ast.Module:
    return ast.parse(LOGIN_MODULE.read_text(encoding="utf-8"))


def _called_names(node: ast.AST) -> set[str]:
    """子树里出现过的被调用函数名（`f(...)` 的 `f`，含 `a.f(...)` 的 `f`）。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name:
                out.add(name)
    return out


def _function_defs(tree: ast.Module) -> dict[str, list[ast.FunctionDef]]:
    """模块内所有函数（含嵌套）按名字索引 —— 为了把 `_submit_ms_code` 这种
    "先定义、再把名字当回调传进去"的写法也解析到函数体。"""
    out: dict[str, list[ast.FunctionDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            out.setdefault(node.name, []).append(node)
    return out


@pytest.mark.unit
def test_retry_helper_itself_never_sends_a_code():
    """**关键保护**：`ask_code_until_accepted` 里不许有任何"发码"调用。

    它的契约就是「只重问、不重发」。在它里面补一个发送，等于每次重输都再发一次码。
    """
    helper = next(
        (
            n
            for n in ast.walk(_login_tree())
            if isinstance(n, ast.FunctionDef) and n.name == "ask_code_until_accepted"
        ),
        None,
    )
    assert helper is not None, "`ask_code_until_accepted` 不见了 —— 重输机制整个没了"
    bad = _called_names(helper) & set(SEND_CODE_CALLS)
    assert not bad, f"重试循环里出现了发码调用：{sorted(bad)}"


@pytest.mark.unit
def test_manual_retry_callbacks_never_send_a_code():
    """**关键保护**：人在环用例传给重试循环的 submit 回调里，不许有"发码"调用。

    这是上一条守不住的地方：发码被塞进 `lambda code: ...` 或
    `def _submit_ms_code(...)` 里时，`ask_code_until_accepted` 自己看不见，
    但每重输一次就会再发一次码。
    """
    tree = _manual_tree()
    defs = _function_defs(tree)
    retries = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ask_code_until_accepted"
    ]
    assert retries, "测试里一处 ask_code_until_accepted 都没用上 —— 人在环重输没接上"

    offenders: list[str] = []
    for call in retries:
        if len(call.args) < 2:
            offenders.append(f"第 {call.lineno} 行：调用没给 submit 回调")
            continue
        submit = call.args[1]
        targets: list[ast.AST] = [submit]
        if isinstance(submit, ast.Name):
            targets += defs.get(submit.id, [])
        names: set[str] = set()
        for target in targets:
            names |= _called_names(target)
        bad = names & set(SEND_CODE_CALLS)
        if bad:
            offenders.append(f"第 {call.lineno} 行：{sorted(bad)}")
    assert not offenders, f"重试回调里在发码，每次重输都会重发一次：{offenders}"


# ---------------------------------------------------------------------------
# 收码后等结果：**「已登录」和「绑定手机号」要一起等**
#
# 2026-09-28 真机实锤：微软账号没绑过手机号时，代码提交成功后弹「绑定手机号」，
# 而它是登录的**必填一步** —— 不绑就回不到已登录态。原来只等已登录，
# 于是必然超时，报出来是「微软登录：提交后未进入已登录态」，
# 把「码是对的、登录已成功、卡在绑定」盖成"登录失败"。
#
# 这个坑**在无人值守那条上 2026-09-23 就修过一次**（`test_microsoft_login_sso` 加了
# `find_bind_dialog` 分支），只是人在环那条漏了 —— 所以这里用结构守卫钉住，
# 而不是指望下一个人记得。
# ---------------------------------------------------------------------------


def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef:
    node = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name),
        None,
    )
    assert node is not None, f"{name} 不见了"
    return node


@pytest.mark.unit
def test_manual_microsoft_case_waits_for_the_bind_dialog_too():
    """**关键保护**：人在环收码那条等结果时必须用 `wait_microsoft_logged_in_or_bind`。

    只等已登录的话，在**没绑过手机号**的账号上必然超时（2026-09-28 真机实锤）。
    而且回调里还得把绑定流程接上 —— 光"看见弹窗"不处理，一样回不到已登录态。
    """
    submit = _find_func(_manual_tree(), "_submit_ms_code")
    called = _called_names(submit)
    assert "wait_microsoft_logged_in_or_bind" in called, (
        "收码后只等已登录 —— 没绑过手机号的账号上这条必然误报「提交后未进入已登录态」"
    )
    assert "_bind_phone_if_prompted" in called, "看到绑定弹窗却不接绑定流程，照样回不到已登录态"


@pytest.mark.unit
def test_forgot_password_restore_failure_is_wrapped_and_reports_the_new_password():
    """**关键保护**：还原程里**任何一步**失败，报错都要带「账户可能停在【新密码】」。

    2026-09-28 真机实付：还原那程发码撞短信限流、裸抛了一句限流提示，
    「密码可能已经改了」一个字都没说 —— 而第一程是**成功**的，账户真停在了 NEW 上。

    所以盯的是**结构**：finally 里的还原调用必须被 try/except 包住，
    且 handler 里要调 `_report_restore_failure`（它负责把警告抛出去或补进日志）。
    """
    fn = _find_func(_manual_tree(), "test_forgot_password_reset_manual")
    finals = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert finals, "这个用例没有 finally 还原程了？"

    guarded = [
        handler
        for node in finals
        for inner in ast.walk(node)
        if isinstance(inner, ast.Try)
        for handler in inner.handlers
        if "_report_restore_failure" in _called_names(handler)
    ]
    assert guarded, (
        "还原程没有被 try/except 包住并交给 `_report_restore_failure` —— "
        "发码/提交任何一步裸抛，都会把「账户可能停在【新密码】」这句警告绕过去"
    )


@pytest.mark.unit
def test_restore_failure_warning_names_the_new_password():
    """`_report_restore_failure` 的两条出口：不盖原报错 / 没原报错时自己抛。

    光有结构（上一条）不够 —— 还得确认那句警告里真的写了「新密码」，
    否则现场读了也不知道账户被改过。
    """
    from hall_auto.launch import LaunchError

    from tests.launch.test_p1_login import RESTORE_FAILED_WARNING, _report_restore_failure

    assert "新密码" in RESTORE_FAILED_WARNING

    # ① 本条用例本来就已失败 → 只补警告，不抛（原报错才是要查的东西）
    _report_restore_failure(LaunchError("原报错"), LaunchError("还原挂了"))

    # ② 没有原报错 → 把警告作为失败抛出去，并带上还原失败的原因
    with pytest.raises(AssertionError) as exc:
        _report_restore_failure(None, LaunchError("还原挂了"))
    assert "新密码" in str(exc.value)
    assert "还原挂了" in str(exc.value)


def test_forgot_password_skips_before_touching_anything_when_opted_out(monkeypatch):
    """本轮选择不测改密码（`HALL_MANUAL_SKIP_PASSWORD=1`）→ **立即 skip**。

    传 `cfg=None, ready_pid=None`：跳过守卫若真在最前面，这两个参数一个都碰不到，
    就不会 AttributeError —— 这就证明了 skip 发生在任何发码/改密码/读凭据之前。
    """
    from tests.launch.test_p1_login import test_forgot_password_reset_manual

    monkeypatch.setenv("HALL_MANUAL_SKIP_PASSWORD", "1")
    with pytest.raises(pytest.skip.Exception) as exc:
        test_forgot_password_reset_manual(cfg=None, ready_pid=None)
    assert "改密码" in str(exc.value)


def test_forgot_password_does_not_skip_when_flag_absent(monkeypatch):
    """开关没置（或非 "1"）时**不许**被这条守卫跳过 —— 照常往下走。

    仍传 `cfg=None`：既然没在守卫处 skip，就会执行到 `cfg.test_account()`，
    对 None 取属性抛 AttributeError。用它反证"守卫没触发、流程继续了"。
    """
    from tests.launch.test_p1_login import test_forgot_password_reset_manual

    monkeypatch.delenv("HALL_MANUAL_SKIP_PASSWORD", raising=False)
    with pytest.raises(AttributeError):
        test_forgot_password_reset_manual(cfg=None, ready_pid=None)


# ---------- focus_console：提上去之后还要「待住了」（2026-10-13） ----------
#
# 用户实报：人在环忘记密码往返、改回密码那一步，发完验证码后终端没到前台，
# 得手点一下才能输码。真因是原来只验「调用那一刻成了没有」——
# 大厅是 webview，脚本刚操作过它，它紧接着可能又把前台抢回去。
# 所以现在改成「提上去 → 等一下 → 再看还在不在前台」，被抢就重试。


def test_focus_console_retries_when_focus_is_stolen_back(monkeypatch):
    """**核心回归**：提上去又被抢回去时必须重试，不能提一次就认成功。"""
    forced: list[int] = []
    monkeypatch.setattr(console, "_find_console_hwnd", lambda: 12345)
    monkeypatch.setattr(console, "_force_foreground",
                        lambda h: (forced.append(h), True)[1])
    rounds = {"n": 0}

    def _fg(hwnd):
        rounds["n"] += 1
        return rounds["n"] >= 3          # 前两轮被抢回去，第三轮待住

    monkeypatch.setattr(console, "_is_foreground", _fg)

    assert console.focus_console(settle_sec=0) is True
    assert len(forced) >= 3, f"被抢回去之后没有重试（只提了 {len(forced)} 次）"


def test_focus_console_gives_up_and_says_it_was_stolen(monkeypatch, capsys):
    """一直被抢回去 -> 返回 False，并**说清是"被抢回来了"**（不是找不到窗口）。"""
    monkeypatch.setattr(console, "_find_console_hwnd", lambda: 12345)
    monkeypatch.setattr(console, "_force_foreground", lambda h: True)
    monkeypatch.setattr(console, "_is_foreground", lambda hwnd: False)

    assert console.focus_console(rounds=2, settle_sec=0) is False
    out = capsys.readouterr().out
    assert "抢回来" in out, "失败时要说清原因 —— 静默失败的话下次还得靠猜"
    assert "不影响用例" in out, "要让一线知道这不是失败、不用去查"


def test_focus_console_says_why_when_no_window_found(monkeypatch, capsys):
    """找不到终端窗口 -> 返回 False，原因与「被抢回去」**可分辨**（两者修法不同）。"""
    monkeypatch.setattr(console, "_find_console_hwnd", lambda: 0)

    assert console.focus_console(settle_sec=0) is False
    out = capsys.readouterr().out
    assert "没找到终端窗口" in out


def test_focus_console_succeeds_on_the_first_round_without_extra_rounds(monkeypatch):
    """一次就待住了 -> 只提一次（别白白多提三下，那是闪屏）。"""
    forced: list[int] = []
    monkeypatch.setattr(console, "_find_console_hwnd", lambda: 7)
    monkeypatch.setattr(console, "_force_foreground",
                        lambda h: (forced.append(h), True)[1])
    monkeypatch.setattr(console, "_is_foreground", lambda hwnd: True)

    assert console.focus_console(settle_sec=0) is True
    assert forced == [7]


# ---------- focus_console：伪控制台窗不是终端（2026-10-27） ----------
#
# 节点实报：人在环改密码那步终端还是没到前台，得手点。本机 WT 里复现：
# Win11 26200 的 Windows Terminal 下 `GetConsoleWindow()` 给 `PseudoConsoleWindow`，
# 且 `IsWindowVisible` 为**真**（老版本它是隐藏的，判可见性就够）。
# 第 1 条路先拿到它 → 抢前台四轮全败 → 能拿到真窗口的第 2/3 条路根本不执行。


class _FakeKernel32:
    def GetConsoleWindow(self):
        return 4321


class _FakeUser32:
    def IsWindowVisible(self, hwnd):
        return 1


def test_console_hwnd_rejects_visible_pseudo_console(monkeypatch):
    """伪控制台窗报可见也抢不到前台 —— 不能当终端。"""
    monkeypatch.setattr(console, "_kernel32", _FakeKernel32)
    monkeypatch.setattr(console, "_user32", _FakeUser32)
    monkeypatch.setattr(console, "_window_class", lambda hwnd: "PseudoConsoleWindow")
    assert console._console_hwnd() == 0


def test_console_hwnd_still_accepts_real_conhost(monkeypatch):
    """排伪控制台窗别把真 conhost 也排掉 —— 那条路还是最可靠的。"""
    monkeypatch.setattr(console, "_kernel32", _FakeKernel32)
    monkeypatch.setattr(console, "_user32", _FakeUser32)
    monkeypatch.setattr(console, "_window_class", lambda hwnd: "ConsoleWindowClass")
    assert console._console_hwnd() == 4321
