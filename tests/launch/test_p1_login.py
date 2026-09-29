from __future__ import annotations

import os
import sys

import pytest

from hall_auto.console import focus_console
from hall_auto.launch import LaunchError, start_fresh, wait_main_window, wait_until_ready
from hall_auto.login import (
    BIND_DIALOG_TIMEOUT_SEC,
    _by_aid,
    _by_name,
    ask_code_until_accepted,
    close_microsoft_login,
    dismiss_bind_dialog,
    find_bind_dialog,
    handle_bind_phone_popup,
    login_dialog_open,
    login_field_aids,
    login_with_password,
    login_with_password_value,
    login_with_sms_code,
    logged_in,
    logout,
    microsoft_pick_account,
    microsoft_send_code,
    open_forgot_password_page,
    open_login_dialog,
    open_microsoft_login,
    request_forgot_sms,
    request_sms_code,
    submit_forgot_reset,
    submit_microsoft_code,
    submit_microsoft_email,
    switch_login_tab,
    wait_microsoft_logged_in,
    wait_microsoft_logged_in_or_bind,
    wait_microsoft_state,
)
from hall_auto.product import stop_main_process


def _require_interactive_tty(what: str) -> None:
    """人在环用例的统一门禁：stdin 不是真终端就 skip，绝不发码/改密码。

    ⚠️ **默认捕获下 `sys.stdin` 被 pytest 换成 `DontReadFromInput`，`isatty()` 恒为 False**
    —— 所以这个 skip 不是"偶发"，是"**没加 `-s` 就必发**"：三条人在环用例会一句不落地全 skip，
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
    focus_console()
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
def test_login_dialog_controls(ready_pid):
    """P1-B 登录弹窗控件图：手机号/密码/协议/登录/找回/注册齐全，短信页签切换后出验证码框"""
    logout(ready_pid)
    open_login_dialog(ready_pid)
    fields = login_field_aids(ready_pid)
    assert "MobileInputBox" in fields, f"缺手机号输入框: {fields}"
    assert "PasswordSecInput" in fields, f"缺密码输入框: {fields}"
    assert "agree" in fields, f"缺同意协议复选框: {fields}"
    assert "登录" in fields, f"缺登录按钮: {fields}"
    assert "忘记密码" in fields and "注册账号" in fields, f"缺找回/注册入口: {fields}"
    switch_login_tab(
        ready_pid,
        "短信验证码登录",
        ready=lambda: _by_aid(ready_pid, "Edit", "CodeInput") is not None,
    )
    sms = login_field_aids(ready_pid)
    assert "CodeInput" in sms, f"短信页签缺验证码输入框: {sms}"
    assert "CodeSendBtn" in sms, f"短信页签缺发送验证码按钮: {sms}"
    assert "PasswordSecInput" not in sms, f"切到短信页签后密码框应消失: {sms}"


def _assert_user_area_shows(user: str, name: str) -> None:
    """断言用户区显示的账号能对应上**登录用的那个手机号**。

    ## 先看清用户区显示的是什么（2026-09-28 更正）

    用户区显示的是**账号昵称**，不是手机号、也不是"绑定号"：

    ```
    用户信息区域，已登录用户：asus_1883771273
    ```

    `1883771273` 只有 10 位，**不是合法的国内手机号**（11 位、1 开头）——
    它是这个账号的昵称。所以「用户区显示的是绑定号」这个 2026-09-23 的定性**是错的**
    （那轮"换个号就过了"只说明另一个号的**昵称**恰好是手机号，证明不了显示的是绑定号）。

    ## 所以这条断言的真实前提

    它其实在断言：**这个账号的昵称里含登录手机号的尾号**。
    那是**账号数据**，不是产品不变量 —— 昵称是用户可以随便改的。

    失败时的正确处置（按顺序）：
      1. **换一个昵称就是手机号的测试号**（最省事，`HALL_TEST_USER` 换掉即可）；
      2. 或在 App 里把这个号的昵称改成登录手机号；
      3. 真要长期用「昵称任意」的号，就得给一个**显式的期望昵称**（配置项），
         别再从手机号尾号反推 —— 那是猜。

    断言本身保留：它仍是"登录进去的是不是我们要的那个账号"的唯一检查，
    只是**别再把失败读成"产品缺陷"或"绑错号"**。登录是成功的。
    """
    assert user[-5:] in name, (
        f"已登录态的用户区没带登录手机号的尾号：显示 {name!r}，期望含 {user[-5:]!r}。\n"
        "  · **登录是成功的**，失败的是这条「账号对不对」的检查 —— 别当产品缺陷报。\n"
        "  · 用户区显示的是**账号昵称**（如 asus_1883771273），不是手机号、也不是绑定号。\n"
        "  · 正解：换一个**昵称就是登录手机号**的测试号（或把该号昵称改成手机号）。\n"
        "  详见《运行手册.md》「坑 13」。"
    )


@pytest.mark.login
def test_password_login_success(cfg, ready_pid):
    """P1-B 账号密码登录：填手机号+密码勾选协议登录后进入已登录态，用户区带手机号尾号"""
    user, password = cfg.test_account()
    if not (user and password):
        pytest.skip("缺凭据：设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    logout(ready_pid)
    name = login_with_password(cfg, ready_pid)
    assert logged_in(ready_pid)
    _assert_user_area_shows(user, name)


@pytest.mark.login
def test_logout_returns_to_anonymous(ready_pid):
    """P1-B 退出登录：已登录态点退出后回到未登录态，登录弹窗自动关闭"""
    if not logged_in(ready_pid):
        pytest.skip("当前未登录，先跑密码登录用例")
    logout(ready_pid)
    assert not logged_in(ready_pid)
    assert not login_dialog_open(ready_pid)


@pytest.mark.login
def test_microsoft_login_sso(cfg, ready_pid):
    """微软账号登录：填邮箱→下一步→点缓存账号磁贴走 SSO 免密→断言已登录→finally 还原登出。

    设计边界（主流程自动化设计.md）：微软账号测到系统/WebView 登录窗为止；
    出现密码页(i0118) / MFA / 邮箱验证码页 / **绑定手机号弹窗** 则标记人工、
    不算脚本失败（skip）。**最后一种是 2026-09-23 真机补上的** —— 微软账号那个号
    没绑过手机号时，SSO 成功后会弹「绑定手机号」，绑定要人收短信码，无人值守跑不通。
    本机有缓存的 Windows/MS 会话时，点「下一步」直接出账号选择器，可免密 SSO，
    所以这条在专用机上能无人值守跑通；无缓存会话的机器会落到 password / email_otp 分支 skip。
    邮箱走 HALL_MS_USER（或 config.local.yaml 的 accounts.microsoft.email），不进 git、不进对话。
    2026-09-16 探针实跑通过：SSO 后用户区=已登录用户 61935，logout 还原成未登录。
    2026-09-22 真机改走「发送登录代码到邮箱」→ 落 email_otp 分支 skip（见下）。
    """
    email = cfg.microsoft_account()
    if not email:
        pytest.skip("缺微软邮箱：设置 HALL_MS_USER")
    logout(ready_pid)
    ms_win = open_microsoft_login(ready_pid)
    submit_microsoft_email(ms_win, email)
    state = wait_microsoft_state(ready_pid, ms_win, email)
    if state == "password":
        pytest.skip("微软要密码(i0118)：本机无缓存 MS 会话，需人工在自己终端输入，标记人工")
    if state == "mfa":
        pytest.skip("微软要二次验证(MFA)：按设计标记人工，不算脚本失败")
    if state == "email_otp":
        # 2026-09-22 真机新增分支：微软不再直接出账号选择器，而是
        # 「我们向 <邮箱> 发送登录代码。」页（带【发送验证码】按钮），码发到邮箱。
        # 要人开邮箱取码 → 人在环，无人值守**不算失败**（早先误判成 account_picker，
        # 去点一块纯文本然后死等已登录态，报的是「提交后未进入已登录态」，方向全错）。
        # 收码流程在 test_microsoft_login_code_manual（-m manual）。
        pytest.skip(
            "微软要邮箱验证码：需人工开邮箱取码，标记人工；"
            "收码流程见 test_microsoft_login_code_manual（-m manual）"
        )
    if state == "waiting":
        pytest.fail("微软登录点「下一步」后没到任何已知分支（picker/password/mfa/email_otp）")
    try:
        if state == "account_picker":
            microsoft_pick_account(ms_win, email)
        # 2026-09-23 真机新增分支：**微软账号那个号没绑过手机号时，SSO 之后会弹「绑定手机号」**。
        #
        # 绑定必须人收短信码 → 无人值守跑不通。所以和已有的 password / mfa / email_otp
        # 一样**标记人工、不算脚本失败（skip）** —— 这条用例的 docstring 本来就写了
        # 这条设计边界，只是漏了「绑定」这一种。收码 + 绑定的流程在
        # `test_microsoft_login_code_manual`（-m manual）。
        #
        # ⚠️ **必须在这里判，不能等 `wait_microsoft_logged_in`**：绑定弹窗是登录的
        # **必填一步**，不绑就回不到已登录态。等下去只会 40s 超时后报
        # 「提交后未进入已登录态」，把「这个号没绑手机号」这个真因**盖成"登录失败"**
        # —— 2026-09-23 真机就是这么被带偏的（现场看到的是脚本把弹窗关掉了，很费解）。
        #
        # ⚠️ **弹窗要先收掉再 skip**：它是模态的，留着会把后面每条用例都点不动。
        # 收掉 = 放弃这次登录，所以下面不会再走到"已登录态"。
        if find_bind_dialog(ready_pid, timeout_sec=BIND_DIALOG_TIMEOUT_SEC) is not None:
            dismiss_bind_dialog(ready_pid)
            pytest.skip(
                "微软 SSO 之后弹了「绑定手机号」：绑定要人收短信码，标记人工；"
                "收码 + 绑定流程见 test_microsoft_login_code_manual（-m manual）。"
                "**根治办法：手工把微软账号绑一次手机号**，之后这条就能无人值守跑通。"
            )
        name = wait_microsoft_logged_in(ready_pid)
        assert logged_in(ready_pid), "SSO 后仍未进入已登录态"
        assert "已登录" in name, f"已登录态用户区文案异常: {name!r}"
    finally:
        logout(ready_pid)
    assert not logged_in(ready_pid), "还原登出后应回到未登录态"


@pytest.mark.login
@pytest.mark.manual
def test_microsoft_login_code_manual(cfg, ready_pid):
    """微软登录·邮箱验证码人在环用例：点【发送验证码】→ 人工开邮箱取码回填 → 断言已登录 → 还原登出。

    2026-09-22 真机发现：这台机器点微软入口、填邮箱、点「下一步」后，
    **不再直接出账号选择器**，而是「我们向 <邮箱> 发送登录代码。」页
    （带【发送验证码】按钮），码发到 HALL_MS_USER 那个邮箱，需要真人去收。
    无人值守那条（test_microsoft_login_sso）遇到这一页只 skip，真收码走这条。

    会真发登录代码到邮箱。和短信登录同一个口径：只在交互式终端跑
    （非 tty 自动 skip），免得发了码却没人回填、把账号卡在半路。
    邮箱走 HALL_MS_USER，不落盘、不进 git。

    **登录成功后**如果弹出「绑定手机号」（没绑过的账号才弹），会接着自动
    填号 → 点获取验证码 → 问人要短信码 → 提交（见 `_bind_phone_if_prompted`）。

    ⚠️ 2026-09-28 修正：这个弹窗**不是"登录成功之后"才弹**，它就是**登录的最后一步** ——
    不绑就回不到已登录态。所以等结果时用的是 `wait_microsoft_logged_in_or_bind`，
    「已登录」和「弹了绑定窗」一起等。以前只等已登录，于是这台没绑过手机号的机器上
    这条**必然报「提交后未进入已登录态」**，而码其实是对的。

    验证码**输错了不用重跑整条**（2026-09-28）：脚本发现码没被接受会在原地
    再问一次，最多 `CODE_MAX_ATTEMPTS` 次，**不会重发码**；随时直接回车放弃。
    """
    email = cfg.microsoft_account()
    if not email:
        pytest.skip("缺微软邮箱：设置 HALL_MS_USER")
    _require_interactive_tty("微软验证码要人工开邮箱取码")
    logout(ready_pid)
    # 整条流程裹在 try/finally 里：挂在哪一步都不能把微软登录窗留在屏幕上 ——
    # 它是大厅进程里的独立顶层窗，留着会把**下一条**用例卡死。
    # 2026-09-22 实测：这条挂在提交按钮上，紧跟着的 test_sms_login_manual 就报
    # 「切到页签 '短信验证码登录' 后界面没就绪」—— 那不是它的毛病，是被这条连累的。
    try:
        ms_win = open_microsoft_login(ready_pid)
        submit_microsoft_email(ms_win, email)
        state = wait_microsoft_state(ready_pid, ms_win, email)
        if state == "account_picker":
            pytest.skip("这台机器走的是账号选择器免密分支（无验证码可收），请跑 test_microsoft_login_sso")
        if state != "email_otp":
            pytest.skip(f"微软没走邮箱验证码分支（当前 {state}）")
        microsoft_send_code(ms_win)

        def _submit_ms_code(code: str) -> str:
            """填码 + 提交 + 等回大厅，**三步绑成一次"尝试"** 给重试循环用。

            必须绑在一起：只提交不等结果的话，码错的失败要等下一轮才暴露，
            人会以为"这次成功了"。

            等的是「已登录 **或** 弹了绑定手机号」，不是只等已登录 ——
            2026-09-28 真机实锤：这个微软账号没绑过手机号，码是对的、登录也成功了，
            但「绑定手机号」是**必填一步**，不绑就回不到已登录态。只等已登录必然超时，
            报出来却是「提交后未进入已登录态」，把真因盖成"登录失败"。
            """
            submit_microsoft_code(ms_win, code)
            state, name = wait_microsoft_logged_in_or_bind(ready_pid, ms_win)
            if state != "bind":
                return name
            # 走到这里说明**码是对的**（码错了不会弹绑定窗），登录已经成功，
            # 只是卡在绑定。人在环，正好有人能收短信码。
            print("\n（登录代码已被接受 —— 弹出「绑定手机号」，这是登录的必填一步，接着绑）")
            status = _bind_phone_if_prompted(cfg, ready_pid)
            if status != "bound":
                pytest.skip(
                    "微软登录代码**已被接受**（不是码错、也不是登录失败），"
                    "但卡在「绑定手机号」这一步 —— 这个账号没绑过，不绑就回不到已登录态；"
                    f"本次绑定状态={status!r}。"
                    "根治办法：手工把微软账号绑一次手机号，之后这条就能跑通。"
                )
            return wait_microsoft_logged_in(ready_pid, ms_win)

        status, name = ask_code_until_accepted(
            _ask_code,
            _submit_ms_code,
            what="微软已向配置的邮箱发送登录代码。输入邮箱收到的验证码",
        )
        if status != "ok":
            pytest.skip("人工放弃回填（登录代码已发出）")
        assert logged_in(ready_pid), "提交验证码后仍未进入已登录态"
        assert "已登录" in str(name), f"已登录态用户区文案异常: {name!r}"
        # 兜底：正常路径上「绑定手机号」已经在 `_submit_ms_code` 里处理掉了
        # （它是登录的必填一步，不绑到不了这里）。这一句防的是"已登录之后才弹"的
        # 另一种时序 —— 弹窗是模态的，不收掉下面的 logout() 就点不动。
        _bind_phone_if_prompted(cfg, ready_pid)
    finally:
        logout(ready_pid)
        close_microsoft_login(ready_pid)  # 挂在中途也要把登录窗收掉，别连累下一条
    assert not logged_in(ready_pid), "还原登出后应回到未登录态"


@pytest.mark.login
@pytest.mark.manual
def test_sms_login_manual(cfg, ready_pid):
    """短信登录人在环用例：脚本发验证码后停下，等人工回填手机收到的码。

    会真发短信。只在交互式终端跑（无人值守时 stdin 不是 tty，自动 skip，
    不会发了短信却没人回填）。手机号走 HALL_TEST_USER，不落盘。
    2026-09-16 已按此流程人工实跑通过（尾号 1935，回填后进入已登录态）。

    **登录成功后**如果弹出「绑定手机号」（没绑过的账号才弹），会接着自动
    填号 → 点获取验证码 → 问人要短信码 → 提交（见 `_bind_phone_if_prompted`），
    所以这台机器上这条最多要人工回填 **两个** 短信码。

    验证码**输错了不用重跑整条**（2026-09-28）：脚本发现码没被接受会在原地
    再问一次，最多 `CODE_MAX_ATTEMPTS` 次，**不会重发码**（短信有服务端限流，
    重跑一次就多一次超限风险）；随时直接回车放弃。
    """
    user, _ = cfg.test_account()
    if not user:
        pytest.skip("缺手机号：设置 HALL_TEST_USER")
    _require_interactive_tty("短信登录要人工回填验证码")
    logout(ready_pid)
    request_sms_code(ready_pid, user)
    status, name = ask_code_until_accepted(
        _ask_code,
        lambda code: login_with_sms_code(ready_pid, code),
        what="输入手机收到的短信验证码",
    )
    if status != "ok":
        pytest.skip("人工放弃回填（验证码已发出）")
    assert logged_in(ready_pid)
    _assert_user_area_shows(user, str(name))
    # 同微软那条：没绑过的账号会弹绑定弹窗，必须在 logout 之前处理掉。
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

