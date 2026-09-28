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
    assert len(messages) == 2, (
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
    assert len(notes) == len(shots) == 2, (
        f"依据/截图两两配对的前提变了：_occlusion_note={notes} _save_screenshot={shots}"
    )
    for note_line, shot_line in zip(notes, shots):
        assert note_line < shot_line, (
            f"第 {note_line} 行才量遮挡、第 {shot_line} 行就截图了 —— "
            "截图会 set_focus，依据必须更早取"
        )
