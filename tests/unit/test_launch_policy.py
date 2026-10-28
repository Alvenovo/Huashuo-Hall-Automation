from __future__ import annotations

import ast
from pathlib import Path

import pytest

import hall_auto.launch as launch
from hall_auto.launch import (
    Overlay,
    is_unknown,
    is_whitelisted,
    matches_button,
    prefer_dismiss,
)
from hall_auto.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_MODULE = REPO_ROOT / "hall_auto" / "launch.py"


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["启动卡片窗口", "更新对话框", "用户协议", "欢迎使用"],
)
def test_whitelist_known_popups(name):
    settings = load_config().launch
    assert is_whitelisted(name, settings)


@pytest.mark.unit
def test_unknown_popup_not_whitelisted():
    settings = load_config().launch
    assert not is_whitelisted("致命错误", settings)
    assert not is_whitelisted("", settings)


@pytest.mark.unit
def test_update_and_card_prefer_dismiss():
    settings = load_config().launch
    assert prefer_dismiss("启动卡片窗口", settings)
    assert prefer_dismiss("更新对话框", settings)
    assert not prefer_dismiss("用户协议", settings)
    assert not prefer_dismiss("欢迎使用 华硕大厅", settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("aid", "name", "labels", "expected"),
    [
        ("", "关闭", ("关闭", "取消"), True),
        ("", "我选好了", ("关闭", "取消"), False),
        ("", "我知道了", ("我知道了",), True),
        ("closebtn", "", ("关闭",), True),
        ("closebtn", "", ("同意",), False),
        ("ConfirmBtn", "", ("同意", "确定"), True),
        # aid 命中时不再按名称兜底，避免把确认按钮当成关闭按钮点掉
        ("ConfirmBtn", "关闭", ("关闭",), False),
        ("", "关闭", (), False),
        ("", "我已阅读并同意", ("同意",), True),
    ],
)
def test_matches_button(aid, name, labels, expected):
    assert matches_button(aid, name, labels) is expected


@pytest.mark.unit
def test_is_unknown_only_spares_whitelisted_and_first_run():
    """只有白名单弹窗和首跑协议窗可以「不是未知弹窗」。

    这条**同时是「这个函数真的跑得起来」的守卫** —— 2026-09-28 实付：
    重构时把 `is_loading` 删掉了，但 `is_unknown` 里还留着对它的调用，
    而**单测全绿**（当时没有任何用例调用 `is_unknown`，`NameError` 要等真跑才炸，
    端到端一跑就炸）。**没被调用过的函数 = 没被验过的函数。**
    """
    settings = load_config().launch
    assert not is_unknown(Overlay(name="更新对话框", node=None), settings)
    assert is_unknown(Overlay(name="致命错误", node=None), settings)
    assert not is_unknown(Overlay(name="随便什么", node=None, first_run=True), settings)


# ------------------------------ 未知弹窗必须报出「它说了什么」，不是「它叫什么」（2026-09-28）


class _FakeInfo:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeChild:
    def __init__(self, name: str) -> None:
        self.element_info = _FakeInfo(name)


class _FakeNode:
    def __init__(self, *names: str) -> None:
        self._names = names

    def descendants(self, **_kwargs):
        return [_FakeChild(n) for n in self._names]


class _BoomNode:
    def descendants(self, **_kwargs):
        raise RuntimeError("UIA 挂了")


@pytest.mark.unit
def test_overlay_text_reports_what_the_dialog_says():
    """报错必须带上弹窗**文字** —— 名字说明不了任何事。

    2026-09-28 实付：`wait_until_ready` 只报 `未知弹窗: ['Splash']`。
    名字 `Splash` 毫无信息量，而框里写的是一句决定性的话
    「检测到您的电脑缺少程序运行必需组件或组件版本低，请按提示安装」。
    真因就写在框里，报错却把它丢了 —— 于是有人去猜 `Splash` 是什么，
    还差点把它当加载态**放行**掉（= 把这个环境错误静默吞掉）。
    """
    node = _FakeNode("确定", "检测到您的电脑缺少程序运行必需组件或组件版本低，请按提示安装", "关闭")
    text = launch._overlay_text(node)
    assert "缺少程序运行必需组件" in text
    assert "确定" in text and "关闭" in text


@pytest.mark.unit
def test_overlay_text_dedupes_and_truncates():
    node = _FakeNode("关闭", "关闭", "", "  ", "很长" * 200)
    text = launch._overlay_text(node, limit=20)
    assert text.count("关闭") == 1, "重复文字应去重"
    assert len(text) <= 21 and text.endswith("…"), f"超长应截断：{text!r}"


@pytest.mark.unit
def test_overlay_text_never_raises_when_uia_is_broken():
    """抠文字失败**不许把就绪检查搞崩** —— 那会让真因更看不见。"""
    assert launch._overlay_text(_BoomNode()) == ""


@pytest.mark.unit
def test_with_hint_appends_the_hint():
    assert launch._with_hint("未知弹窗: ['Splash']", "大厅被别的窗口挡住了：「X」") == (
        "未知弹窗: ['Splash']｜遮挡诊断：大厅被别的窗口挡住了：「X」"
    )


@pytest.mark.unit
def test_with_hint_leaves_message_alone_when_hint_is_empty():
    assert launch._with_hint("超时未就绪 missing=[]", "") == "超时未就绪 missing=[]"


@pytest.mark.unit
@pytest.mark.parametrize("pid", [None, 0])
def test_occlusion_note_is_empty_without_a_pid(monkeypatch, pid):
    calls: list[int] = []

    def _never(pid_arg):
        calls.append(pid_arg)
        return "不该被调用"

    monkeypatch.setattr(launch, "occlusion_hint", _never)
    assert launch._occlusion_note(pid) == ""
    assert calls == [], "没有 pid 时不该去扫遮挡"


# ------------------------------------------------------------- 结构守卫（AST）


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"hall_auto/launch.py 里找不到函数 {name}")


def _ready_func() -> ast.FunctionDef:
    return _func(ast.parse(LAUNCH_MODULE.read_text(encoding="utf-8")), "wait_until_ready")


def _launch_error_messages() -> list[str]:
    """`wait_until_ready` 里每条 `raise LaunchError(...)` 的消息表达式（源码形式）。"""
    messages = []
    for node in ast.walk(_ready_func()):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            exc = node.exc
            if isinstance(exc.func, ast.Name) and exc.func.id == "LaunchError":
                messages.append(ast.unparse(exc.args[0]))
    return messages


def _call_lines(func: ast.FunctionDef, name: str) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == name
    )


def _assign_source(func: ast.FunctionDef, varname: str) -> str | None:
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == varname:
                    return ast.unparse(node.value)
    return None


@pytest.mark.unit
def test_ready_failures_all_carry_the_occlusion_diagnosis():
    """就绪检查的**每条**失败出口都必须带遮挡诊断 —— 只给结论、不给依据是不行的。

    这条是结构守卫：`wait_until_ready` 里 `raise LaunchError(...)` 的条数被钉死，
    新增出口忘了包 `_with_hint` 会立刻变红。
    """
    messages = _launch_error_messages()
    assert len(messages) == 3, (
        f"wait_until_ready 的 LaunchError 出口数变了（{len(messages)}）——"
        "新增/删改出口时请同步这条守卫，并确认每条都带了遮挡诊断"
    )
    for text in messages:
        assert "_with_hint(" in text, f"这条就绪失败没带遮挡诊断：{text}"


@pytest.mark.unit
def test_unknown_popup_message_carries_the_dialog_text():
    """结构守卫：「未知弹窗」那条出口的消息，必须是由 `_overlay_text` 拼出来的。

    没有这条，把 `_overlay_text` 摘掉也照样绿 —— 而那正是 09-28 的原始缺陷
    （报「Splash」而不报框里那句「缺少程序运行必需组件」）。

    ⚠️ 只查「消息里有 `_overlay_text(`」是**假守卫**：真实写法是
    `detail = ..._overlay_text(...)...` 然后 `f"未知弹窗: {detail}"`。
    所以这里**追一层**：把消息里用到的变量找出来，看它各自的赋值里有没有它。
    """
    func = _ready_func()
    messages = [m for m in _launch_error_messages() if "未知弹窗" in m]
    assert len(messages) == 1, f"「未知弹窗」出口应当只有一条，实际 {len(messages)} 条"
    used = {
        node.id
        for node in ast.walk(ast.parse(messages[0], mode="eval"))
        if isinstance(node, ast.Name)
    }
    sources = [s for s in (_assign_source(func, name) for name in used) if s]
    assert any("_overlay_text(" in s for s in sources), (
        "「未知弹窗」那条消息没带上弹窗文字。"
        f"消息用到的变量={sorted(used)}，各自的来源={sources}"
    )


@pytest.mark.unit
def test_occlusion_is_measured_before_the_screenshot():
    """遮挡依据必须在**截图之前**取。

    ⚠️ `_save_screenshot()` 会 `set_focus()` 把大厅提到前台 —— 之后再量遮挡，
    量到的是**我们自己刚造出来的**「没被挡住」，真因当场被抹掉。
    这条不是推理，是**实验证过**的：自己铺一块非置顶窗盖住大厅，
    截图前 `occlusion_hint` 报出遮挡者、截图后同一个函数返回空。
    """
    func = _ready_func()
    notes = _call_lines(func, "_occlusion_note")
    shots = _call_lines(func, "_save_screenshot")
    assert len(notes) == len(shots) == 3, (
        f"依据/截图两两配对的前提变了：_occlusion_note={notes} _save_screenshot={shots}"
    )
    for note_line, shot_line in zip(notes, shots):
        assert note_line < shot_line, (
            f"第 {note_line} 行才量遮挡、第 {shot_line} 行就截图了 —— "
            "截图会 set_focus，依据必须更早取"
        )


class _NavRect:
    def __init__(self, top: int):
        self.top = top
        self.left = 0
        self.right = 10
        self.bottom = top + 10


class _NavInfo:
    def __init__(self, name: str, control_type: str, aid: str = ""):
        self.name = name
        self.control_type = control_type
        self.automation_id = aid


class _NavNode:
    def __init__(self, name, control_type="ListItem", top=0, parent=None, aid=""):
        self.element_info = _NavInfo(name, control_type, aid)
        self._top = top
        self._parent = parent
        self._children: list["_NavNode"] = []
        self.pressed: list[str] = []
        self.scrolled: list[tuple] = []
        self._on_scroll = None
        self._no_scroll = False

    def window_text(self):
        return self.element_info.name

    def rectangle(self):
        return _NavRect(self._top)

    def parent(self):
        return self._parent

    def descendants(self, control_type=None):
        out = []
        for child in self._children:
            if control_type is None or child.element_info.control_type == control_type:
                out.append(child)
            out.extend(child.descendants(control_type))
        return out

    def invoke(self):
        self.pressed.append("invoke")

    @property
    def iface_scroll(self):
        if self._no_scroll:
            raise RuntimeError("不支持 ScrollPattern")
        outer = self

        class _Scroll:
            def SetScrollPercent(self, h, v):
                outer.scrolled.append((h, v))
                if outer._on_scroll:
                    outer._on_scroll()

        return _Scroll()


def _nav_scene(names_tops, with_home_name=False, hidden_home=None):
    """造一个左侧导航：List 容器 + 若干页签 ListItem（name, top）。

    hidden_home：虚拟化滚出可视区的首页页签 —— 滚回顶（SetScrollPercent）时才进树。
    """
    nav = _NavNode("应用分类", control_type="List")
    for name, top in names_tops:
        nav._children.append(_NavNode(name, top=top, parent=nav))
    if with_home_name:
        nav._children.insert(0, _NavNode(launch.HOME_TAB_NAME, top=0, parent=nav))
    if hidden_home:
        home = _NavNode(hidden_home, top=0, parent=nav)

        def realize():
            if home not in nav._children:
                nav._children.insert(0, home)

        nav._on_scroll = realize
    return nav


def _main_with(nav):
    main = _NavNode("华硕大厅", control_type="Window")
    main._children.append(nav)
    return main


@pytest.mark.unit
def test_back_if_subpage_clicks_home_tab_by_name():
    nav = _nav_scene([("折扣", 100), ("壁纸", 200), ("分类", 300), ("我的", 400)], with_home_name=True)
    main = _main_with(nav)
    assert launch._back_if_subpage(main) is True
    home = nav._children[0]
    assert home.pressed == ["invoke"]
    assert nav.scrolled == [], "按名字就能找到「推荐」时不该动滚动"


@pytest.mark.unit
def test_back_if_subpage_scrolls_nav_when_home_tab_not_realized(monkeypatch):
    """节点真机场景：落在折扣页时导航滚掉了两行，树里只剩 折扣/壁纸/分类/我的，
    首页页签（登录态是昵称 wmw001）不在 UIA 树里 —— 必须滚回顶再点它。"""
    wheels = []
    monkeypatch.setattr(launch, "wheel_at", lambda *a, **k: wheels.append(a))
    nav = _nav_scene(
        [("折扣", 200), ("壁纸", 300), ("分类", 400), ("我的", 500)], hidden_home="wmw001"
    )
    main = _main_with(nav)
    assert launch._back_if_subpage(main) is True
    assert nav.scrolled == [(-1.0, 0.0)], "没把导航滚回顶就点，点到的还是折扣"
    assert nav._children[0].pressed == ["invoke"], "滚回顶后应点首页页签（昵称）"
    assert wheels == [], "ScrollPattern 已经把页签滚出来了，不该再动真鼠标"


@pytest.mark.unit
def test_back_if_subpage_falls_back_to_wheel_without_scroll_pattern(monkeypatch):
    """自绘列表不暴露 ScrollPattern 时退滚轮；滚完仍没「推荐」就点最上面一项。"""
    wheels = []
    monkeypatch.setattr(launch, "wheel_at", lambda *a, **k: wheels.append(a))
    nav = _nav_scene([("wmw001", 100), ("折扣", 200), ("壁纸", 300), ("分类", 400), ("我的", 500)])
    nav._no_scroll = True
    main = _main_with(nav)
    assert launch._back_if_subpage(main) is True
    assert len(wheels) == 1, "ScrollPattern 不可用必须退滚轮兜底"
    assert nav._children[0].pressed == ["invoke"]


@pytest.mark.unit
def test_back_if_subpage_gives_up_without_nav_anchors():
    """只有应用列表（没有 壁纸/分类/我的 锚点）时别乱点 —— 维持原样等超时报真因。"""
    app_list = _NavNode("列表", control_type="List")
    app_list._children.append(_NavNode("微信27的10已选择", top=10, parent=app_list))
    main = _main_with(app_list)
    assert launch._back_if_subpage(main) is False
    assert app_list.scrolled == []
    assert app_list._children[0].pressed == []


@pytest.mark.unit
def test_back_if_subpage_prefers_back_button():
    nav = _nav_scene([("折扣", 100), ("壁纸", 200), ("分类", 300), ("我的", 400)])
    back = _NavNode("", control_type="Button", aid="BackBtn", parent=nav.parent() or nav)
    main = _NavNode("华硕大厅", control_type="Window")
    main._children.extend([back, nav])
    assert launch._back_if_subpage(main) is True
    assert back.pressed == ["invoke"]
    assert nav.scrolled == []


# ------------------------- 主窗口按「面积最大」选，破多窗歧义（2026-10-09）


class _FakeApp:
    def __init__(self, pid):
        self.process = pid


def _patch_windows(monkeypatch, windows):
    """windows: [(hwnd, visible, pid, title, rect, uia_name)]"""
    by_hwnd = {w[0]: w for w in windows}

    class _Info:
        def __init__(self, name, hwnd):
            self.name = name
            self.hwnd = hwnd

    monkeypatch.setattr(launch, "_top_hwnds", lambda: list(by_hwnd))
    monkeypatch.setattr(launch, "_hwnd_visible", lambda h: by_hwnd[h][1])
    monkeypatch.setattr(launch, "_hwnd_pid", lambda h: by_hwnd[h][2])
    monkeypatch.setattr(launch, "_hwnd_text", lambda h: by_hwnd[h][3])
    monkeypatch.setattr(launch, "_hwnd_rect", lambda h: by_hwnd[h][4])
    monkeypatch.setattr(launch, "UIAElementInfo", lambda h: _Info(by_hwnd[h][5], h))
    monkeypatch.setattr(launch, "UIAWrapper", lambda info: ("wrap", info))


_CARD = (1, True, 42, "华硕大厅", (0, 0, 1500, 800), "启动卡片窗口")  # 欢迎使用卡片：标题同主窗，UIA Name 不同
_MAIN = (2, True, 42, "华硕大厅", (0, 0, 1600, 900), "华硕大厅")  # 真主窗口
_OTHER = (3, True, 42, "别的程序", (0, 0, 1600, 900), "别的程序")  # 标题不含关键词，必须被过滤
_HIDDEN = (4, False, 42, "华硕大厅", (0, 0, 2000, 1200), "华硕大厅")  # 隐藏窗面积再大也不能选


@pytest.mark.unit
def test_main_window_picks_largest_area_window(monkeypatch):
    """同 pid 多窗标题都含「华硕大厅」时选**面积最大**的可见窗，不选卡片窗/隐藏窗。"""
    _patch_windows(monkeypatch, [_CARD, _MAIN, _OTHER, _HIDDEN])
    got = launch.main_window(_FakeApp(42), "华硕大厅", 1)
    assert got[0] == "wrap" and got[1].hwnd == _MAIN[0]


@pytest.mark.unit
def test_main_window_waits_instead_of_grabbing_the_startup_card(monkeypatch):
    """首跑那一瞬只有卡片窗可见时**不许**把它当主窗口冻住 —— 必须继续等真主窗口。

    2026-10-09 本机实付：即时解析抓了卡片窗，ready 随即把真主窗口报成「未知弹窗」。
    """
    _patch_windows(monkeypatch, [_CARD])
    with pytest.raises(launch.LaunchError):
        launch.main_window(_FakeApp(42), "华硕大厅", 0)


@pytest.mark.unit
def test_concrete_main_picks_largest_area_window(monkeypatch):
    """wb 夹具走 concrete_main 重解析主窗口，同样不能按「第一个」抓到卡片窗。"""
    _patch_windows(monkeypatch, [_CARD, _MAIN])
    got = launch.concrete_main(42, "华硕大厅")
    assert got[0] == "wrap" and got[1].hwnd == _MAIN[0]


@pytest.mark.unit
def test_main_window_raises_when_no_window_matches(monkeypatch):
    _patch_windows(monkeypatch, [_OTHER])
    with pytest.raises(launch.LaunchError):
        launch.main_window(_FakeApp(42), "华硕大厅", 0)


@pytest.mark.unit
def test_concrete_main_raises_when_no_window_matches(monkeypatch):
    _patch_windows(monkeypatch, [])
    with pytest.raises(launch.LaunchError):
        launch.concrete_main(42, "华硕大厅")


# ------------------------- 首页内容健康检查：结构就绪 ≠ 内容渲染（2026-10-09）


@pytest.mark.unit
def test_home_content_present_true_when_tiles_or_banners_rendered():
    main = _FakeNode("专题页", "专题页 - Web 内容", "广告图片1", "WPS Office27的2已选择")
    assert launch.home_content_present(main) is True


@pytest.mark.unit
def test_home_content_present_false_when_webview_blank():
    """空白态：容器「专题页」在，但没有任何磁贴/横幅 —— 不能算就绪。"""
    main = _FakeNode("华硕大厅", "搜索框", "应用分类", "专题页", "专题页 - Web 内容")
    assert launch.home_content_present(main) is False


@pytest.mark.unit
def test_home_content_present_never_raises_when_uia_is_broken():
    assert launch.home_content_present(_BoomNode()) is False


# --------- 主窗口句柄冻在过渡窗上时，真主窗口会被报成「未知弹窗」（2026-10-09 节点 install）


def _main_like_node(*aids: str):
    node = _NavNode("华硕大厅", control_type="Window")
    for aid in aids:
        node._children.append(_NavNode("", control_type="Edit", aid=aid, parent=node))
    return node


@pytest.mark.unit
def test_looks_like_main_window_true_when_full_structure_present():
    settings = load_config().launch
    node = _main_like_node(*settings.required_auto_ids)
    assert launch._looks_like_main_window(node, settings) is True


@pytest.mark.unit
def test_looks_like_main_window_false_when_structure_incomplete():
    settings = load_config().launch
    node = _main_like_node(settings.required_auto_ids[0])
    assert launch._looks_like_main_window(node, settings) is False


@pytest.mark.unit
def test_looks_like_main_window_false_when_uia_is_broken():
    settings = load_config().launch
    assert launch._looks_like_main_window(_BoomNode(), settings) is False


@pytest.mark.unit
def test_wait_until_ready_rebind_propagates_to_callers_wrapper(monkeypatch):
    """换绑必须写回**调用方手里那个 wrapper**，不能只改 wait_until_ready 的局部变量。

    2026-10-27 节点实付：夹具 `ready_main` 拿到的是过渡窗（启动卡片），
    wait_until_ready 内部虽然换绑到了真主窗，但只改了它自己的局部变量，
    夹具 yield 的还是旧句柄 → 搜索用例在卡片窗里找 SearchBarInput 必然找不到。
    这条守卫钉死「换绑后调用方的 wrapper 句柄真的变了」。
    """
    settings = load_config().launch
    cfg = load_config()

    real = _main_like_node(*settings.required_auto_ids)
    real.handle = 777
    real.element_info = _RebindInfo(7, 777)

    calls = {"n": 0}

    def _overlays(*_a, **_k):
        calls["n"] += 1
        return [] if calls["n"] > 1 else [Overlay(name="华硕大厅", node=real)]

    monkeypatch.setattr(launch, "_overlay_windows", _overlays)
    monkeypatch.setattr(launch, "dismiss_whitelist_popups", lambda *_a, **_k: None)
    monkeypatch.setattr(launch, "_structure", lambda *_a, **_k: (set(settings.required_auto_ids), True))
    monkeypatch.setattr(launch, "click_first_run_agree", lambda *_a, **_k: False)
    monkeypatch.setattr(launch, "home_content_present", lambda *_a, **_k: True)
    monkeypatch.setattr(launch, "_save_screenshot", lambda *_a, **_k: None)

    holder = _RebindHolder(111, 7)
    launch.wait_until_ready(cfg, holder)

    assert holder.handle == 777, "换绑没写回调用方的 wrapper，用例还会拿过渡窗找控件"
    assert holder._element_info is real.element_info


class _RebindInfo:
    def __init__(self, pid: int, handle: int) -> None:
        self.process_id = pid
        self.handle = handle


class _RebindHolder:
    """ mimics 一个 pywinauto wrapper：只暴露 wait_until_ready 会碰到的几个属性。"""

    def __init__(self, handle: int, pid: int) -> None:
        self._element_info = _RebindInfo(pid, handle)
        self.handle = handle
        self._as_parameter_ = handle
        self._cache = {}

    @property
    def element_info(self):
        return self._element_info

    def window_text(self):
        return "华硕大厅"
