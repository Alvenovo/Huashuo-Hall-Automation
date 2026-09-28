"""锁微软登录状态机的**判定顺序**（hall_auto.login.microsoft_state）。

为什么值得一条专门的守卫：`email_otp` 与 `account_picker` 的先后顺序**就是正确性本身**。
「我们向 <邮箱> 发送登录代码。」这一页的正文**含邮箱**，先判 `account_picker`
就会把验证码页认成账号选择器 —— 脚本去点一块纯文本（点不动），然后死等已登录态，
最后报「微软登录：提交后未进入已登录态」。

**这个错报与真正的登录失败同形**：2026-09-22 真机整轮都在这上面绕
（那轮证据里的 `test_microsoft_login_sso/final.png` 是实锤：屏幕上明明是
「发送登录代码 + 【发送验证码】」页）。顺序被谁「顺手整理」成一个 `any()` 就会复发。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from hall_auto import login

pytestmark = pytest.mark.unit

EMAIL = "someone@outlook.com"  # 假值：真邮箱只走 HALL_MS_USER，不进仓库


class _Node:
    """只提供 microsoft_state 真正读到的三个字段。"""

    def __init__(self, name: str = "", aid: str = ""):
        self.element_info = SimpleNamespace(name=name, automation_id=aid)


def _state(names: list[str], email: str = EMAIL, *, aid: str = "") -> str:
    nodes = [_Node(name=n) for n in names]
    if aid:
        nodes.append(_Node(aid=aid))
    with (
        mock.patch.object(login, "logged_in", return_value=False),
        mock.patch.object(login, "_ms_nodes", return_value=nodes),
    ):
        return login.microsoft_state(1, object(), email)


# ---------- 判定顺序：本文件存在的理由 ----------


@pytest.mark.unit
def test_email_otp_page_is_not_mistaken_for_account_picker():
    """**关键保护**：验证码页含邮箱，顺序写反就变成 account_picker。"""
    page = ["登录你的 Microsoft 帐户", EMAIL, f"我们向 {EMAIL} 发送登录代码。"]
    assert _state(page) == "email_otp"


@pytest.mark.unit
def test_account_picker_still_detected_without_otp_text():
    """改了顺序不能把真·账号选择器一起改坏：只有邮箱、没有验证码文案时仍要认出来。"""
    assert _state(["选择一个帐户", EMAIL]) == "account_picker"


@pytest.mark.unit
def test_bare_email_is_not_an_otp_page():
    """光有邮箱不算验证码页 —— 否则账号选择器永远判不到。"""
    assert login._is_microsoft_otp_page(EMAIL) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    ["发送登录代码", "发送验证码", "输入代码", "Send code", "ENTER CODE", "email code"],
)
def test_otp_markers_recognised(text):
    assert login._is_microsoft_otp_page(text) is True


# ---------- 别的分支不能被顺序改动带坏 ----------


@pytest.mark.unit
def test_password_page_wins_over_everything():
    """i0118（要密码）优先级最高：出现就一定是密码页。"""
    assert _state([EMAIL, "输入密码"], aid="i0118") == "password"


@pytest.mark.unit
def test_mfa_page_still_detected():
    assert _state(["验证你的身份", EMAIL]) == "mfa"


@pytest.mark.unit
def test_logged_in_wins_first():
    with (
        mock.patch.object(login, "logged_in", return_value=True),
        mock.patch.object(login, "_ms_nodes", return_value=[_Node(name="发送登录代码")]),
    ):
        assert login.microsoft_state(1, object(), EMAIL) == "logged_in"


@pytest.mark.unit
def test_unknown_page_is_waiting():
    assert _state(["正在加载…"]) == "waiting"


@pytest.mark.unit
def test_no_email_configured_never_returns_account_picker():
    """没配邮箱时不能因为空串 `in` 任何字符串都成立而误判（`"" in "x"` 为真）。"""
    assert _state(["选择一个帐户", EMAIL], email="") == "waiting"


# ---------- 提交验证码：不许"抓一次快照就下结论" ----------
#
# 2026-09-22 真机报「填了验证码但点不到提交按钮」。三个坑：
#   ① 按钮要等页面校验完才变可点 → 抓一次就放弃，太早；
#   ② `set_edit_text` 走 UIA ValuePattern，webview 的 React 收不到 input 事件，
#      它自己的 state 还是空 → 按钮一直 disabled；
#   ③ 候选词里的「验证」会撞上【发送验证码】→ 点下去是**重发**，人白等一个码。


def _clicks(nodes, *, press_returns: bool = True):
    """跑一轮 `_press_submit_once`，返回 (是否点到, 被点到的 (aid, name) 列表)。"""
    clicked: list[tuple[str, str]] = []

    def _press(node):
        info = node.element_info
        clicked.append((info.automation_id, info.name))
        return press_returns

    with (
        mock.patch.object(login, "_ms_nodes", return_value=nodes),
        mock.patch.object(login, "_press_button", side_effect=_press),
    ):
        return login._press_submit_once(object(), []), clicked


@pytest.mark.unit
def test_submit_never_clicks_the_resend_button():
    """**关键保护**：候选词「验证」会匹配到【发送验证码】。

    点错的表现是「看着成功、实际重发」—— 比报错更难查：码又发了一遍，
    人拿着旧码一直提交失败，而日志里一切正常。
    """
    ok, clicked = _clicks([_Node(name="发送验证码"), _Node(name="下一步")])
    assert ok is True
    assert clicked == [("", "下一步")], clicked


@pytest.mark.unit
def test_submit_prefers_the_standard_aid():
    """有标准 aid 就别靠中文文案猜 —— 文案会变，aid 相对稳。"""
    ok, clicked = _clicks([_Node(name="下一步"), _Node(aid="idSIButton9")])
    assert ok is True
    assert clicked == [("idSIButton9", "")], clicked


@pytest.mark.unit
def test_submit_retries_instead_of_giving_up_on_the_first_snapshot():
    """按钮要等页面校验完才可点 —— 一次快照抓不到就放弃，正是 09-22 那个报错。"""
    calls = {"n": 0}

    def _nodes(_win):
        calls["n"] += 1
        return [] if calls["n"] < 3 else [_Node(name="下一步")]

    clock = {"t": 0.0}

    def _now():
        clock["t"] += 1.0
        return clock["t"]

    with (
        mock.patch.object(login, "_ms_nodes", side_effect=_nodes),
        mock.patch.object(login, "_press_button", return_value=True),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login.time, "time", side_effect=_now),
    ):
        assert login._wait_submit(object(), []) is True
    assert calls["n"] >= 3, "只在第一轮找过一次就没再找"


@pytest.mark.unit
def test_submit_error_says_whether_anything_was_found():
    """报错要能一眼分清「压根没找到」和「找到了点不动」—— 下一步排查方向完全相反。"""
    with (
        mock.patch.object(login, "wait_microsoft_code_input", return_value=mock.Mock()),
        mock.patch.object(login, "_ms_nodes", return_value=[_Node(name="发送验证码")]),
        mock.patch.object(login, "_press_button", return_value=False),
        mock.patch.object(login, "_bring_to_front", return_value=False),
        mock.patch.object(login, "dump_microsoft_tree", return_value=""),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SUBMIT_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError) as exc:
            login.submit_microsoft_code(object(), "123456")
    assert "一个都没找到" in str(exc.value)


@pytest.mark.unit
def test_submit_does_not_type_keys_when_the_window_is_not_in_front():
    """**关键保护**：补真键盘前必须确认窗口在前台。

    确认漏了就会往**别人的窗口**里打字 —— 刚被 `focus_console()` 提到最前的
    终端就是最可能的受害者（用户正等着在那儿输码）。
    """
    typed: list[str] = []
    box = mock.Mock()
    box.type_keys.side_effect = lambda *a, **k: typed.append(str(a))

    with (
        mock.patch.object(login, "wait_microsoft_code_input", return_value=box),
        mock.patch.object(login, "_ms_nodes", return_value=[_Node(name="发送验证码")]),
        mock.patch.object(login, "_press_button", return_value=False),
        mock.patch.object(login, "_bring_to_front", return_value=False),
        mock.patch.object(login, "dump_microsoft_tree", return_value=""),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SUBMIT_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError):
            login.submit_microsoft_code(object(), "123456")
    assert typed == [], f"窗口不在前台还敲了键盘：{typed}"


# ---------------------------------------------------------------------------
# 【发送验证码】必须"点了 + 确认发出去了"
#
# 2026-09-22 新机实测：脚本没点发送验证码就回来问人要码，人只能自己去点一下。
# 根因是原来那个实现**只点一下、不验证** —— webview 上 `invoke()` / `click_input()`
# 都可能静默失败，于是"没点"和"点了"在代码里长得一模一样。
#
# 成功判据取「代码输入框出现」：那是页面上唯一与"码已发出"一一对应的可观测变化
# （按钮本身点完会消失/变倒计时，`request_sms_code` 早就不拿按钮状态当判据了）。
# ---------------------------------------------------------------------------


class _Ctl:
    """只提供 `_ms_otp_send_candidates` 读到的两个字段。"""

    def __init__(self, name: str, control_type: str = ""):
        self.element_info = SimpleNamespace(name=name, control_type=control_type)


@pytest.mark.unit
def test_send_code_prefers_button_over_text():
    """**关键保护**：同名节点里 `Button` 必须排在 `Text` 前面。

    webview 里按钮常常是 `Button(发送验证码)` 套一层 `Text(发送验证码)`。
    先命中里面那个 Text 时 `invoke()` 必失败、`click_input()` 又只认矩形 ——
    点了跟没点一样，**而且不报错**。
    """
    nodes = [
        _Ctl("发送验证码", "Text"),
        _Ctl("发送验证码", "Button"),
        _Ctl("发送验证码", "Button"),
        _Ctl("发送登录代码", "Text"),  # 页面上别处的文案，不该入选
    ]
    with mock.patch.object(login, "_ms_nodes", return_value=nodes):
        picked = login._ms_otp_send_candidates(object())
    assert [n.element_info.control_type for n in picked] == ["Button", "Button", "Text"]


@pytest.mark.unit
def test_send_code_fails_loudly_when_the_code_box_never_shows_up():
    """**关键保护**：点了但代码框没出现 → **必须抛错**，不能当成功返回。

    不抛就是这个 bug 本身：脚本静默返回、接着问人要一个**根本没发出去**的码。
    """
    with (
        mock.patch.object(login, "_ms_otp_send_candidates", return_value=[object()]),
        mock.patch.object(login, "_press_button", return_value=True),
        mock.patch.object(login, "_bring_to_front", return_value=True),
        mock.patch.object(login, "microsoft_code_input", return_value=None),
        mock.patch.object(login, "dump_microsoft_tree", return_value=""),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SEND_TIMEOUT_SEC", 0),
        mock.patch.object(login, "MICROSOFT_OTP_PAGE_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError) as exc:
            login.microsoft_send_code(object())
    assert "代码输入框没出现" in str(exc.value)


@pytest.mark.unit
def test_send_code_succeeds_once_the_code_box_shows_up():
    """正常路径：点完代码框出现就返回。"""
    boxes = iter([None, object()])  # 第一次是"点之前没有框"，第二次是"点完有了"
    with (
        mock.patch.object(login, "_ms_otp_send_candidates", return_value=[object()]),
        mock.patch.object(login, "_press_button", return_value=True),
        mock.patch.object(login, "_bring_to_front", return_value=True),
        mock.patch.object(login, "microsoft_code_input", lambda _w: next(boxes, object())),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SEND_TIMEOUT_SEC", 0),
    ):
        login.microsoft_send_code(object())  # 不抛即通过


@pytest.mark.unit
def test_send_code_raises_when_no_send_button_exists():
    """连按钮都找不到时，报错要带上"找到过几个同名节点"，别只报一句「点不到」。"""
    with (
        mock.patch.object(login, "_ms_otp_send_candidates", return_value=[]),
        mock.patch.object(login, "dump_microsoft_tree", return_value=""),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SEND_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError) as exc:
            login.microsoft_send_code(object())
    assert "整棵树里没有这个名字" in str(exc.value)


# ---------------------------------------------------------------------------
# 登录后「绑定手机号」弹窗
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bind_confirm_names_never_contain_the_dialog_title_word():
    """**关键保护**：确定按钮的名字候选里不许有「绑定」。

    弹窗标题就是「绑定手机号」—— 按名字搜会把标题那块 `Text` 当成按钮，
    点下去什么都没发生，而脚本以为提交过了（比报错更难查）。
    """
    assert not any("绑定" in n for n in login._BIND_CONFIRM_NAMES), login._BIND_CONFIRM_NAMES


@pytest.mark.unit
def test_bind_signature_needs_a_control_not_just_the_text():
    """**关键保护**：光有「绑定手机号」文案不算弹窗，必须有手机号框或发送按钮。

    弹窗可能是主窗口下的子窗 —— 只按文案认，会命中主窗口里别处的字样，
    于是把主窗口当成弹窗、在里面乱点。
    """
    with (
        mock.patch.object(login, "_names_of", return_value=["绑定手机号", "华硕应用商店"]),
        mock.patch.object(login, "bind_phone_field", return_value=None),
        mock.patch.object(login, "bind_send_button", return_value=None),
    ):
        assert login._bind_signature(object()) is False

    with (
        mock.patch.object(login, "_names_of", return_value=["绑定手机号"]),
        mock.patch.object(login, "bind_phone_field", return_value=object()),
        mock.patch.object(login, "bind_send_button", return_value=None),
    ):
        assert login._bind_signature(object()) is True


@pytest.mark.unit
def test_bind_popup_absent_is_not_a_failure():
    """没弹 = 账号已绑过，是**正常情况**，不能报错。"""
    with mock.patch.object(login, "find_bind_dialog", return_value=None):
        assert login.handle_bind_phone_popup(1, "13800000000", lambda _p: "123456") == "absent"


@pytest.mark.unit
def test_bind_popup_closes_itself_when_no_phone_is_configured():
    """没配号码时：关掉弹窗 + 返回 skipped。

    关掉是关键 —— 模态窗留着会把后面每条用例都卡住（点不动用户区、退不了登录）。
    """
    closed: list[bool] = []
    with (
        mock.patch.object(login, "find_bind_dialog", return_value=object()),
        mock.patch.object(login, "close_bind_dialog", lambda _s: closed.append(True) or True),
    ):
        assert login.handle_bind_phone_popup(1, "", lambda _p: "123456") == "skipped"
    assert closed, "没配号码也必须把弹窗关掉"


@pytest.mark.unit
def test_bind_popup_closes_itself_even_when_a_step_blows_up():
    """**关键保护**：任何一步挂掉都要把模态弹窗收掉，再让异常往上抛。

    不收的话，用例失败信息会指向绑定弹窗，但**后面每条用例**都跟着一起挂 ——
    真因被埋在一串同形的失败里。
    """
    closed: list[bool] = []

    def _boom(*_a, **_k):
        raise login.LaunchError("绑定手机号：找不到手机号输入框")

    with (
        mock.patch.object(login, "find_bind_dialog", return_value=object()),
        mock.patch.object(login, "close_bind_dialog", lambda _s: closed.append(True) or True),
        mock.patch.object(login, "fill_bind_phone", _boom),
    ):
        with pytest.raises(login.LaunchError):
            login.handle_bind_phone_popup(1, "13800000000", lambda _p: "123456")
    assert closed, "挂了也得把绑定弹窗关掉"


@pytest.mark.unit
def test_bind_popup_reports_bound_on_the_happy_path():
    """正常路径：填号 → 问人要码 → 提交 → 返回 bound。"""
    asked: list[str] = []
    with (
        mock.patch.object(login, "find_bind_dialog", return_value=object()),
        mock.patch.object(login, "fill_bind_phone", lambda _s, _p: None),
        mock.patch.object(login, "submit_bind_code", lambda _s, _c: None),
    ):
        status = login.handle_bind_phone_popup(
            1, "13800000000", lambda prompt: asked.append(prompt) or "654321"
        )
    assert status == "bound"
    assert asked, "没有问人要验证码"
    assert "13800000000" in asked[0], f"提示语里应带号码，好让人核对：{asked[0]}"


@pytest.mark.unit
def test_bind_popup_gives_up_and_closes_when_the_human_presses_enter():
    """人回车放弃 → 关弹窗 + skipped，绝不留下模态窗。"""
    closed: list[bool] = []
    with (
        mock.patch.object(login, "find_bind_dialog", return_value=object()),
        mock.patch.object(login, "fill_bind_phone", lambda _s, _p: None),
        mock.patch.object(login, "close_bind_dialog", lambda _s: closed.append(True) or True),
    ):
        assert login.handle_bind_phone_popup(1, "13800000000", lambda _p: "") == "skipped"
    assert closed


@pytest.mark.unit
def test_dismiss_bind_dialog_is_a_single_non_blocking_probe():
    """`dismiss_bind_dialog` 默认**不等待**（timeout=0），弹了才关、没弹返回 False。

    等待会把每条登出用例都拖慢一个 `BIND_DIALOG_TIMEOUT_SEC` —— 它的用途是
    「顺手清掉残留」，不是「等它出现」。
    """
    seen: list[float] = []
    with mock.patch.object(login, "find_bind_dialog", lambda _pid, timeout_sec=0.0: seen.append(timeout_sec) or None):
        assert login.dismiss_bind_dialog(1) is False
    assert seen == [0.0], f"默认必须是一次非阻塞探针，实际 timeout={seen}"

    closed: list[bool] = []
    with (
        mock.patch.object(login, "find_bind_dialog", lambda _pid, timeout_sec=0.0: object()),
        mock.patch.object(login, "close_bind_dialog", lambda _s: closed.append(True) or True),
    ):
        assert login.dismiss_bind_dialog(1) is True
    assert closed


@pytest.mark.unit
def test_logout_dismisses_leftover_bind_dialog_before_touching_the_user_area():
    """**关键保护**：`logout()` 必须**先**收掉残留的「绑定手机号」模态弹窗，再点用户区。

    为什么这条值得单锁：无人值守的 SSO 那条（`test_microsoft_login_sso`）登进去
    就可能弹这个窗，它挡着用户区 → 直接点会抛「退出登录：点不开用户菜单」，
    **与"登出功能坏了"同形**，排查方向全错。人在环那两条会自己处理弹窗，
    所以这个假红只在无人值守那条上出现 —— 最难归因的一类。
    """
    order: list[str] = []
    with (
        mock.patch.object(
            login, "dismiss_bind_dialog", lambda _pid, timeout_sec=0.0: order.append("dismiss") or True
        ),
        mock.patch.object(login, "logged_in", lambda _pid: True),
        # 点不到用户区 → 抛 LaunchError，用来证明 dismiss 已经先跑过了
        mock.patch.object(login, "_by_aid", lambda *a, **k: order.append("by_aid") or None),
    ):
        with pytest.raises(login.LaunchError):
            login.logout(1)
    assert order == ["dismiss", "by_aid"], f"必须先收弹窗再点用户区，实际顺序 {order}"


@pytest.mark.unit
def test_logout_still_returns_early_when_already_logged_out():
    """已登出时 `logout()` 仍然直接返回 —— 不能因为加了清弹窗就多跑一趟点击。"""
    pressed: list = []
    with (
        mock.patch.object(login, "dismiss_bind_dialog", lambda _pid, timeout_sec=0.0: False),
        mock.patch.object(login, "logged_in", lambda _pid: False),
        mock.patch.object(login, "_by_aid", lambda *a, **k: pressed.append(a) or None),
    ):
        login.logout(1)
    assert pressed == []


# ---------------------------------------------------------------------------
# 人在环收码：输错了在**原地重输**，不重发码、不重跑整条
#
# 2026-09-28 用户提出：验证码是人手输的，打错一位很常见。原来错一次整条用例
# 直接 fail，想再试只能重跑 —— 而重跑会**重新发一次码**：短信有服务端限流
# （见 `request_forgot_sms` 的注释），邮件要人再翻一次邮箱。
#
# 拆分口径：`CodeRejected` = 页面还停在输入态、最可能码错 → 值得重问；
# 其它 `LaunchError` = 控件找不到/窗口被遮挡/按钮点不动 → 重问多少次都一样。
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_code_rejected_is_still_a_launch_error():
    """**关键保护**：`CodeRejected` 必须继承 `LaunchError`。

    没人接住时（无人值守那几条、以后新加的用例）行为和改动前**完全一致** —— 用例 fail。
    所以这次改动不会让任何一条老路径"变绿"。
    """
    assert issubclass(login.CodeRejected, login.LaunchError)


@pytest.mark.unit
def test_ask_code_until_accepted_returns_immediately_when_the_first_code_works():
    """第一次就过：只问一次、只提交一次，提示语里不该出现「第 N 次」。"""
    asked: list[str] = []
    status, value = login.ask_code_until_accepted(
        lambda prompt: asked.append(prompt) or "123456",
        lambda code: f"ok-{code}",
        what="输入验证码",
    )
    assert (status, value) == ("ok", "ok-123456")
    assert len(asked) == 1, f"成功了还多问：{asked}"
    assert "第" not in asked[0], f"第一次的提示语不该写「第 N 次」：{asked[0]}"


@pytest.mark.unit
def test_ask_code_until_accepted_reasks_after_a_rejection():
    """**核心行为**：码被拒（`CodeRejected`）就再问一次，第二次过就算过。"""
    asked: list[str] = []
    submitted: list[str] = []

    def _ask(prompt: str) -> str:
        asked.append(prompt)
        return f"code{len(asked)}"

    def _submit(code: str) -> str:
        submitted.append(code)
        if code == "code1":
            raise login.CodeRejected("码不对")
        return "已登录用户"

    status, value = login.ask_code_until_accepted(_ask, _submit, what="输入验证码")
    assert (status, value) == ("ok", "已登录用户")
    assert submitted == ["code1", "code2"], f"提交序列不对：{submitted}"
    assert len(asked) == 2
    assert "第 2/" in asked[1], f"重问时要告诉人是第几次：{asked[1]}"


@pytest.mark.unit
def test_ask_code_until_accepted_gives_up_after_max_attempts():
    """**关键保护**：试满就抛最后一次的 `CodeRejected`，不能无限重问。

    上限存在的理由：连错多次可能触发服务端锁定，一直问下去只会把号玩死。
    """
    asked: list[str] = []

    def _submit(_code: str) -> str:
        raise login.CodeRejected("码不对")

    with pytest.raises(login.CodeRejected):
        login.ask_code_until_accepted(
            lambda prompt: asked.append(prompt) or "000000",
            _submit,
            what="输入验证码",
            max_attempts=3,
        )
    assert len(asked) == 3, f"问了 {len(asked)} 次，应该正好 3 次"


@pytest.mark.unit
def test_ask_code_until_accepted_does_not_submit_when_the_human_presses_enter():
    """人回车放弃 → **一次都不提交**（提交空码会真去校验一次，还可能记一次错）。"""
    submitted: list[str] = []
    status, value = login.ask_code_until_accepted(
        lambda _p: "", lambda c: submitted.append(c), what="输入验证码"
    )
    assert (status, value) == ("abandoned", None)
    assert submitted == [], f"人放弃了还提交：{submitted}"


@pytest.mark.unit
def test_ask_code_until_accepted_does_not_retry_on_a_real_failure():
    """**关键保护**：只有 `CodeRejected` 才重试。

    其它 `LaunchError`（控件找不到、窗口被遮挡、按钮点不动）重问多少次都一样，
    重试只会白耗人在场的时间，还让人以为是自己输错了。
    """
    asked: list[str] = []

    def _submit(_code: str) -> str:
        raise login.LaunchError("登录：点不到登录按钮")

    with pytest.raises(login.LaunchError) as exc:
        login.ask_code_until_accepted(
            lambda prompt: asked.append(prompt) or "123456", _submit, what="输入验证码"
        )
    assert type(exc.value) is login.LaunchError, "硬失败被包成了 CodeRejected"
    assert len(asked) == 1, f"硬失败还重问：{asked}"


# ---------- 短信登录：超时后按"页面还在不在"分流 ----------


def _sms_login_raises(second_by_aid, *, timeout: int = 0):
    """跑一次 `login_with_sms_code`，返回它抛的异常。

    `second_by_aid` 是**超时后**那次 `_by_aid` 的返回值（找验证码框）：
    给对象 = 框还在，给 None = 页面已经翻走了。
    """
    box = mock.Mock()
    with (
        mock.patch.object(login, "_by_aid", side_effect=[box, second_by_aid]),
        mock.patch.object(login, "_by_name", return_value=box),
        mock.patch.object(login, "_press_button", return_value=True),
        mock.patch.object(login, "logged_in", return_value=False),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "LOGIN_SUBMIT_TIMEOUT_SEC", timeout),
    ):
        with pytest.raises(login.LaunchError) as exc:
            login.login_with_sms_code(1, "123456")
    return exc.value


@pytest.mark.unit
def test_sms_login_calls_it_rejected_while_the_code_box_is_still_there():
    """超时后验证码框还在 = 码被拒（可重试），不是"登录功能坏了"。"""
    exc = _sms_login_raises(mock.Mock())
    assert type(exc) is login.CodeRejected, f"应该是可重试的 CodeRejected，实际 {type(exc)}"


@pytest.mark.unit
def test_sms_login_is_a_hard_failure_when_the_page_moved_on():
    """验证码框消失 = 页面状态已经变了，重输没意义 → 普通 `LaunchError`（不重试）。"""
    exc = _sms_login_raises(None)
    assert type(exc) is login.LaunchError, f"页面都翻页了还判成码错：{type(exc)}"


# ---------- 微软：只有"没遮挡 + OTP 框还在"才算码错 ----------


def _ms_wait_raises(*, hint: str, otp_present: bool, ms_win=object()):
    """跑一次 `wait_microsoft_logged_in`，返回它抛的异常。"""
    with (
        mock.patch.object(login, "logged_in", return_value=False),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "occlusion_hint", return_value=hint),
        mock.patch.object(login, "window_state_facts", return_value="窗口诊断"),
        mock.patch.object(
            login, "microsoft_code_input", return_value=mock.Mock() if otp_present else None
        ),
        mock.patch.object(login, "MICROSOFT_SSO_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError) as exc:
            if ms_win is None:
                login.wait_microsoft_logged_in(1)
            else:
                login.wait_microsoft_logged_in(1, ms_win)
    return exc.value


@pytest.mark.unit
def test_microsoft_login_rejects_the_code_when_nothing_is_in_the_way():
    """OTP 框还在 + **没有窗口遮挡** → 判码错（可重试）。"""
    exc = _ms_wait_raises(hint="", otp_present=True)
    assert type(exc) is login.CodeRejected, f"应该是可重试的 CodeRejected，实际 {type(exc)}"


@pytest.mark.unit
def test_microsoft_login_does_not_blame_the_code_when_the_window_is_covered():
    """**关键保护**：有窗口挡着时**绝不能**判成"码错"。

    WebView2 被遮挡就不渲染、登录页永远提交不出去（2026-09-23 真机那条超时 40s
    的真因），而这时候 OTP 框也还在。判成码错会让人反复重输 —— 问题根本不在码上。
    """
    exc = _ms_wait_raises(hint="大厅被 wechat.exe 遮挡", otp_present=True)
    assert type(exc) is login.LaunchError, f"遮挡被当成了码错：{type(exc)}"
    assert "wechat" in str(exc), "遮挡诊断要留在报错里"


@pytest.mark.unit
def test_microsoft_login_without_a_window_handle_keeps_the_old_behaviour():
    """不传 `ms_win`（无人值守那条）行为一字未变：只抛普通 `LaunchError`。"""
    exc = _ms_wait_raises(hint="", otp_present=True, ms_win=None)
    assert type(exc) is login.LaunchError


# ---------- 绑定弹窗：重试时不许把弹窗关掉 ----------


@pytest.mark.unit
def test_bind_popup_reasks_without_closing_the_dialog_when_the_code_is_rejected():
    """**关键保护**：码被拒时**不许关弹窗** —— 关了就没法原地重输，只能重跑（重发码）。

    这条守的是"重试"和"收尾"的边界：`handle_bind_phone_popup` 的 `except Exception`
    会关弹窗，`CodeRejected` 必须**在它之前**被重试逻辑消化掉。
    """
    asked: list[str] = []
    closed: list[bool] = []
    tries: list[str] = []

    def _ask(prompt: str) -> str:
        asked.append(prompt)
        return f"code{len(asked)}"

    def _submit(_scope, code: str) -> None:
        tries.append(code)
        if code == "code1":
            raise login.CodeRejected("码不对")

    with (
        mock.patch.object(login, "find_bind_dialog", return_value=object()),
        mock.patch.object(login, "fill_bind_phone", lambda _s, _p: None),
        mock.patch.object(login, "submit_bind_code", _submit),
        mock.patch.object(login, "close_bind_dialog", lambda _s: closed.append(True) or True),
    ):
        status = login.handle_bind_phone_popup(1, "13800000000", _ask)
    assert status == "bound"
    assert tries == ["code1", "code2"], f"没重试：{tries}"
    assert closed == [], "码被拒时不能关弹窗，否则没法原地重输"


@pytest.mark.unit
def test_bind_popup_closes_the_dialog_when_the_code_keeps_being_rejected():
    """试满仍被拒 → 异常照抛，但**弹窗必须收掉**（模态窗留着会卡住后面每条用例）。"""
    closed: list[bool] = []
    with (
        mock.patch.object(login, "find_bind_dialog", return_value=object()),
        mock.patch.object(login, "fill_bind_phone", lambda _s, _p: None),
        mock.patch.object(
            login, "submit_bind_code", lambda _s, _c: (_ for _ in ()).throw(login.CodeRejected("码不对"))
        ),
        mock.patch.object(login, "close_bind_dialog", lambda _s: closed.append(True) or True),
    ):
        with pytest.raises(login.CodeRejected):
            login.handle_bind_phone_popup(1, "13800000000", lambda _p: "123456")
    assert closed, "重试耗尽后必须把模态弹窗收掉"
