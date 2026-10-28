from __future__ import annotations

import sys

import pytest

from hall_auto import drivers
from hall_auto.console import focus_console
from hall_auto.launch import start_fresh, wait_main_window, wait_until_ready
from hall_auto.login import (
    ask_code_until_accepted,
    handle_bind_phone_popup,
    login_dialog_open,
    login_with_password,
    login_with_sms_code,
    logged_in,
    logout,
    open_login_dialog,
    request_sms_code,
)
from hall_auto.product import stop_main_process


def _require_interactive_tty(what: str) -> None:
    """人在环用例的统一门禁：stdin 不是真终端就 skip，绝不发码/改密码。

    ⚠️ **默认捕获下 `sys.stdin` 被 pytest 换成 `DontReadFromInput`，`isatty()` 恒为 False**
    —— 所以这个 skip 不是"偶发"，是"**没加 `-s` 就必发**"：人在环用例会一句不落地全 skip，
    而 `farm_agent` 照打「退出码 0」，看着像「跑过了、只是没到条件」，实际是这套功能从没执行过。
    就算绕过这个守卫，`input()` 也会抛
    `OSError: pytest: reading from stdin while output is captured! Consider using -s.`

    `-s` 由套件定义自己带（`hall_auto/suites.py` 的 `build_command`，`login-manual` 标了
    `interactive=True`），所以走 `farm_agent --local login-manual` 自动就有；
    手敲 pytest 必须自己加。实测对照见 `build_command` 的注释。
    """
    if not sys.stdin.isatty():
        pytest.skip(
            f"非交互式终端：{what}。"
            r"请跑 tools\farm_agent.py --local login-manual（或自己终端 pytest -m manual -s）"
        )


def _ask_code(prompt: str) -> str:
    """**先把终端抢到前台**，再读一行。

    脚本操作大厅时会把大厅窗口顶到前台，终端被压在后面 —— 不切窗口的话，
    人收完码还得自己 Alt+Tab 找回来。点完「发送验证码」立刻把终端提到最前，
    人只管收码、输码。

    全文件只有这一处 `input()`，是刻意的：让"问人之前先切窗口"变成**结构性的**，
    而不是散在四处、少写一次就多一次「码发了、人却在别的窗口里干等」。
    `focus_console()` 拿不到窗口就返回 False，**不该因此失败** —— 人手动切一下即可。

    验证码输错时**会被 `ask_code_until_accepted` 再调一次**（同一个码不重发、
    只重问），所以这里每次都重新 `focus_console()`：重问时大厅窗口可能又被顶到前面了。
    """
    if not focus_console():
        print("   ⚠️ 上面那行说了原因；点一下终端窗口再输码即可，不影响用例结果。")
    return input(prompt).strip()


def _bind_phone_if_prompted(cfg, pid) -> str:
    """登录成功后处理「绑定手机号」弹窗（**没绑过的账号才弹**）。

    为什么塞在登录用例里、而不是单开一条：
    1. 它**就是登录流程的一部分** —— 在没绑过的账号上，登录成功紧接着就弹；
    2. 必须排在 `logout()` **之前** —— 模态弹窗不关，「点用户区 → 退出登录」根本点不动，
       而后面每条用例开头都要 `logout()`，会被它一路卡住。

    号码走 `HALL_BIND_PHONE`，没配就回退到 `HALL_TEST_USER`（见 `Config.bind_phone`）。
    没弹返回 `"absent"`，**那是正常情况不是失败**。
    """
    status = handle_bind_phone_popup(pid, cfg.bind_phone(), _ask_code)
    if status == "bound":
        print("✅ 绑定手机号弹窗：已自动填号 → 点获取验证码 → 提交成功。")
    elif status == "absent":
        print("（没有绑定手机号弹窗 —— 该账号已绑过，正常）")
    return status


@pytest.fixture(scope="module")
def ready_pid(cfg, installed):
    """登录弹窗/用户菜单会顶掉主窗口句柄，所以趁窗口健康时记下 pid，后面一律按进程找控件。"""
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    yield main.element_info.process_id
    stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.login
def test_cases_login_dialog(ready_pid, case):
    """**登录弹窗（默认页签）的通用用例函数** —— 承接 `page: login_dialog` 的用例。

    用例只要写 `driver` + `page: login_dialog`（不用给 `ref`）就会挂到这里，
    所以**加一条同页面的用例 = 只改 `cases/cases.yaml`**。
    ⚠️ 必须留在本文件里 —— `ready_pid` 是模块级 fixture。
    """
    logout(ready_pid)
    open_login_dialog(ready_pid)
    drivers.run(case["driver"], case["page"], ready_pid, case)


@pytest.mark.login
def test_cases_login_sms_tab(ready_pid, case):
    """**登录弹窗（短信验证码页签）的通用用例函数** —— 承接 `page: login_sms_tab` 的用例。

    切页签这个**动作**在 `hall_auto/login.py::login_sms_tab_fields` —— 交互是代码、期望是数据。
    """
    logout(ready_pid)
    open_login_dialog(ready_pid)
    drivers.run(case["driver"], case["page"], ready_pid, case)


@pytest.mark.login
def test_password_login_success(cfg, ready_pid):
    """P1-B 账号密码登录：填手机号+密码勾选协议登录后进入已登录态。

    不核对「用户区显示的是不是登录的那个号」：用户区只显示**账号昵称**（账号数据，
    可随时被改），认账号就得维护一个期望昵称；2026-10-09 定案不维护（坑 13 撤断言）。
    起点每条用例先 logout，登录态真假由 `logged_in` 守，见《运行手册.md》坑 13。
    """
    user, password = cfg.test_account()
    if not (user and password):
        pytest.skip("缺凭据：设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    logout(ready_pid)
    login_with_password(cfg, ready_pid)
    assert logged_in(ready_pid)


@pytest.mark.login
def test_logout_returns_to_anonymous(ready_pid):
    """P1-B 退出登录：已登录态点退出后回到未登录态，登录弹窗自动关闭"""
    if not logged_in(ready_pid):
        pytest.skip("当前未登录，先跑密码登录用例")
    logout(ready_pid)
    assert not logged_in(ready_pid)
    assert not login_dialog_open(ready_pid)


@pytest.mark.login
@pytest.mark.manual
def test_sms_login_manual(cfg, ready_pid):
    """短信登录人在环用例：脚本发验证码后停下，等人工回填手机收到的码。

    会真发短信。只在交互式终端跑（无人值守时 stdin 不是 tty，自动 skip，
    不会发了短信却没人回填）。手机号走 HALL_TEST_USER，不落盘。
    2026-09-16 已按此流程人工实跑通过（回填后进入已登录态）。

    **登录成功后**如果弹出「绑定手机号」（没绑过的账号才弹），会接着自动
    填号 → 点获取验证码 → 问人要短信码 → 提交（见 `_bind_phone_if_prompted`），
    所以这台机器上这条最多要人工回填 **两个** 短信码。

    验证码**输错了不用重跑整条**（2026-09-28）：脚本发现码没被接受会在原地
    再问一次，最多 `CODE_MAX_ATTEMPTS` 次，**不会重发码**（短信有服务端限流，
    重跑一次就多一次超限风险）；随时连续两次回车放弃。

    ⚠️ **单次空行不算放弃**（2026-10-27 节点实锤）：控制台残留的回车会被 `input()`
    读成空行，若一次空行就判放弃，则「发了验证码 → 1 秒 skip」白发一条短信。
    现在第一次空行只重问、连两次才放弃；agent 侧起 pytest 前还会清一次输入缓冲。
    """
    user, _ = cfg.test_account()
    if not user:
        pytest.skip("缺手机号：设置 HALL_TEST_USER")
    _require_interactive_tty("短信登录要人工回填验证码")
    logout(ready_pid)
    request_sms_code(ready_pid, user)
    status = ask_code_until_accepted(
        _ask_code,
        lambda code: login_with_sms_code(ready_pid, code),
        what="输入手机收到的短信验证码",
    )[0]
    if status != "ok":
        pytest.skip("人工放弃回填（验证码已发出）")
    assert logged_in(ready_pid)
    # 没绑过手机号的账号会弹「绑定手机号」，它是登录必填一步，必须在 logout 之前处理掉。
    _bind_phone_if_prompted(cfg, ready_pid)
    logout(ready_pid)
    assert not logged_in(ready_pid)
