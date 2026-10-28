from __future__ import annotations

import os
import sys

import pytest

from hall_auto import drivers
from hall_auto.console import focus_console
from hall_auto.launch import LaunchError, start_fresh, wait_main_window, wait_until_ready
from hall_auto.login import (
    _by_aid,
    _by_name,
    ask_code_until_accepted,
    handle_bind_phone_popup,
    login_dialog_open,
    login_with_password,
    login_with_password_value,
    login_with_sms_code,
    logged_in,
    logout,
    open_forgot_password_page,
    open_login_dialog,
    request_forgot_sms,
    request_sms_code,
    submit_forgot_reset,
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



RESTORE_FAILED_WARNING = (
    "还原程失败：账户可能停在【新密码】！"
    "请先用 HALL_TEST_NEW_PASSWORD 登录一次，手工改回原密码；"
    "或等短信冷却后重跑本条（它会自己再走一遍还原）。"
)


def _restore_old_password(ready_pid: int, user: str, old_password: str) -> None:
    """第二程 NEW -> OLD 还原：改回原密码 + 用原密码登一次验证。

    幂等开页（`open_forgot_password_page` 自己会在已在页面上时直接返回），
    所以不管上一程把登录窗留在什么状态都能重新导航到位。

    ⚠️ 这一段里**任何一步失败都意味着账户可能停在【新密码】** ——
    调用方必须用 `_report_restore_failure` 收场，别让它裸抛。
    """
    logout(ready_pid)
    open_forgot_password_page(ready_pid)
    request_forgot_sms(ready_pid, user)
    status, _ = ask_code_until_accepted(
        _ask_code,
        lambda code: submit_forgot_reset(ready_pid, code, old_password),
        what="【第2次·还原原密码】输入手机收到的重置验证码",
    )
    if status != "ok":
        raise LaunchError("人工放弃还原（账户可能仍停在【新密码】）")
    logout(ready_pid)
    login_with_password_value(ready_pid, user, old_password)
    assert logged_in(ready_pid), "原密码登录后仍未进入已登录态，还原可能没生效"


def _report_restore_failure(primary: BaseException | None, restore_exc: BaseException) -> None:
    """还原程挂了怎么收场：**无论如何都要让人知道账户可能停在【新密码】**。

    为什么单拎出来（2026-09-28 真机实付）：原来只有「人放弃还原」那条分支写了这句话，
    而 `request_forgot_sms` 自己抛错（短信限流，见《运行手册.md》「坑 12」）会**绕过**它 ——
    现场看到的是「连点 N 次发送按钮都没进倒计时态」，**一个字都没提密码可能已经改了**。
    而那一轮第一程其实是成功的（只有一条异常链、总耗时 29.7s），
    也就是说那个测试号真的停在了 NEW 上，却没人被告知。

    `primary` 不为 None = 本条用例本来就已失败/跳过 → 只在日志里补一句警告，
    **不盖掉原报错**（原报错才是这次要查的东西）；否则把警告本身作为失败抛出去。
    """
    message = f"{RESTORE_FAILED_WARNING}还原失败原因：{restore_exc}"
    if primary is not None:
        print(f"\n⚠️ {message}\n（本条用例本来就已经失败/跳过，上面那条才是原始报错）")
        return
    raise AssertionError(message) from restore_exc


@pytest.mark.login
@pytest.mark.manual
def test_forgot_password_reset_manual(cfg, ready_pid):
    """忘记密码往返用例：真改密码 + 重新登录验证 + 还原回原密码。

    会真把测试号密码改两次（OLD→NEW 验证，再 NEW→OLD 还原），账户最终回到原状，
    HALL_TEST_PASSWORD 不用改、用例可重复跑。需要人工转交 **两个** 短信验证码。
    三个密码值全走环境变量（HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_TEST_NEW_PASSWORD），
    不落盘、不进对话、不进 git。和短信登录一样只在交互式终端跑（非 tty 自动 skip）。
    还原程放在 finally：只要第一次提交发出去了，哪怕新密码验证那步报错，也一定把
    密码压回 OLD（若本就是 OLD，再设一次无害），保证账户不会卡在 NEW 上。
    2026-09-16 先以「点到发送为止」探针验过页面结构和发送链路，本版把真正的重置闭上。

    两个码**输错了都能在原地重输**（2026-09-28，同一个码不重发）。人放弃不提交时
    `submitted` 立不起来，还原程**跳过** —— 密码本来就没动，不该再要人输一个码。

    ⚠️ 还原程里**任何一步**失败（含发码被限流），报错都必须带
    `RESTORE_FAILED_WARNING` —— 那时账户可能真的停在【新密码】上。
    2026-09-28 那轮就是漏了这条：还原程发码撞限流、裸抛了一句限流提示，
    "密码可能已经改了"一个字都没说，而第一程其实是成功的。
    """
    # 本轮人工选择不测改密码：立即 skip，绝不发码、绝不动密码。
    # 开关由 farm_agent 在问完「要不要参与人在环」后再问一句「要不要测改密码」置上，
    # 省掉这条真发的 2 个短信码。skip 带原因，报告里是「本轮选择不测」而非空白/假绿。
    if os.environ.get("HALL_MANUAL_SKIP_PASSWORD") == "1":
        pytest.skip("本轮人工选择不测改密码（HALL_MANUAL_SKIP_PASSWORD=1）——省 2 个短信码")
    user, old_password = cfg.test_account()
    new_password = cfg.new_password()
    if not (user and old_password and new_password):
        pytest.skip("缺凭据：设置 HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_TEST_NEW_PASSWORD")
    if new_password == old_password:
        pytest.skip("新密码与原密码相同，往返验证证明不了密码真被改过；换一个不同的 HALL_TEST_NEW_PASSWORD")
    _require_interactive_tty("改密码要人工回填两次验证码")

    # 第一程 OLD -> NEW
    logout(ready_pid)
    open_forgot_password_page(ready_pid)
    for aid in ("MobileInput", "CodeInput", "CodeSendBtn", "PasswordSecInput", "RePasswordSecInput"):
        assert _by_aid(ready_pid, "Edit", aid) is not None or _by_aid(ready_pid, "Button", aid) is not None, (
            f"忘记密码页缺控件 {aid}"
        )
    assert _by_name(ready_pid, "Text", "忘记密码", exact=True) is not None, "忘记密码页缺标题「忘记密码」"
    request_forgot_sms(ready_pid, user)
    submitted = False

    def _submit_new(code: str) -> dict:
        """第一程提交。**只要被调用过就把 submitted 立起来** —— 密码可能已经变了，
        还原程必须跑，哪怕这次提交本身报错。"""
        nonlocal submitted
        submitted = True
        return submit_forgot_reset(ready_pid, code, new_password)

    # 提交一旦发出，密码就可能已变 NEW；放进 try，让 finally 的还原程兜住提交本身报错的情况
    primary: BaseException | None = None
    try:
        status, _ = ask_code_until_accepted(
            _ask_code,
            _submit_new,
            what="【第1次·改成新密码】输入手机收到的重置验证码",
        )
        if status != "ok":
            pytest.skip("人工放弃（验证码已发出，密码尚未修改）")
        logout(ready_pid)
        login_with_password_value(ready_pid, user, new_password)
        assert logged_in(ready_pid), "新密码登录后仍未进入已登录态，重置可能没生效"
    except BaseException as exc:  # noqa: BLE001 —— 连 skip 一起兜，见下
        # 记下"这条用例本来就挂了/跳了"，还原程再挂时**不能盖掉它** ——
        # 原报错才是这次要查的东西（2026-09-28：还原程自己抛错，把上面
        # 「账户可能停在【新密码】」那句提示整个绕过去了，现场只看到限流报错）。
        primary = exc
        raise
    finally:
        # 人放弃（一次都没提交）时密码没动，**不必**白折腾一趟还原程 ——
        # 那还要人再输一个码，而这时账户本来就是原样。
        if submitted:
            try:
                _restore_old_password(ready_pid, user, old_password)
            except BaseException as restore_exc:  # noqa: BLE001
                _report_restore_failure(primary, restore_exc)
