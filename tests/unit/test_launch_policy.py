from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

import hall_auto.launch as launch
from hall_auto.launch import (
    Overlay,
    dismiss_whitelist_popups,
    is_loading,
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


# --------------------------------------------------- 加载态过渡窗（2026-09-28）


class _SpyNode:
    """假节点：只记「有没有人去它的子树里翻按钮」。"""

    def __init__(self) -> None:
        self.subtree_scans = 0

    def descendants(self, **_kwargs):
        self.subtree_scans += 1
        return []


@pytest.mark.unit
def test_loading_splash_is_neither_dismissed_nor_fatal():
    """`Splash` 是**加载态**，不是未知弹窗 —— 点不得，也判不得死。

    2026-09-28 实付：大厅加载时会弹一个 20×20、屏幕正中的顶层窗叫 `Splash`。
    把它当未知弹窗判死，报出的 `未知弹窗: ['Splash']` 会把排查方向带到
    「去找这个弹窗」，而真因是页面压根没加载出来（窗口被遮挡，坑 15）。
    """
    settings = load_config().launch
    assert is_loading("Splash", settings)
    assert not is_unknown(Overlay(name="Splash", node=None), settings)
    # 反例：加载态不等于「什么都放行」
    assert not is_loading("致命错误", settings)
    assert is_unknown(Overlay(name="致命错误", node=None), settings)
    assert not is_loading("", settings)


@pytest.mark.unit
def test_loading_popup_is_never_clicked_even_if_someone_whitelists_it():
    """加载态**优先于白名单** —— 就算有人把 `Splash` 加进白名单，也不许去点它。

    ⚠️ 只拿**当前**配置测这条是**假守卫**：`Splash` 本来就不在白名单里，
    把 `continue` 删掉它也照样绿（2026-09-28 注入实付：删了只红 2 条，这条没红）。
    真正会被踩的是「有人图省事把 `Splash` 塞进 `popup_whitelist_keywords`」——
    那时没有这条 `continue`，大厅就会开始往一个 20×20 的加载窗上点「同意 / 确定」。
    """
    base = load_config().launch
    settings = replace(
        base, popup_whitelist_keywords=(*base.popup_whitelist_keywords, "Splash")
    )
    assert is_whitelisted("Splash", settings), "前提不成立：这个假配置里 Splash 得是白名单"
    spy = _SpyNode()
    dismiss_whitelist_popups([Overlay(name="Splash", node=spy)], settings)
    assert spy.subtree_scans == 0, "Splash 被当成可点弹窗去点了"


@pytest.mark.unit
def test_whitelisted_popup_is_still_clicked():
    """负路径的反面：白名单弹窗**照旧**要去点。

    没有这条，把 `continue` 写在白名单判断之前（或直接 `return`）也照样"绿"。
    """
    settings = load_config().launch
    spy = _SpyNode()
    dismiss_whitelist_popups([Overlay(name="更新对话框", node=spy)], settings)
    assert spy.subtree_scans >= 1, "白名单弹窗反而没去点"


@pytest.mark.unit
def test_with_hint_appends_the_hint():
    assert launch._with_hint("未知弹窗: ['Splash']", "大厅被别的窗口挡住了：「X」") == (
        "未知弹窗: ['Splash']｜遮挡诊断：大厅被别的窗口挡住了：「X」"
    )


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


@pytest.mark.unit
def test_with_hint_leaves_message_alone_when_hint_is_empty():
    assert launch._with_hint("超时未就绪 missing=[]", "") == "超时未就绪 missing=[]"


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"hall_auto/launch.py 里找不到函数 {name}")


def _call_lines(func: ast.FunctionDef, name: str) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == name
    )


@pytest.mark.unit
def test_ready_failures_all_carry_the_occlusion_diagnosis():
    """就绪检查的**每条**失败出口都必须带遮挡诊断 —— 只给结论、不给依据是不行的。

    这条是结构守卫：`wait_until_ready` 里 `raise LaunchError(...)` 的条数被钉死，
    新增出口忘了包 `_with_hint` 会立刻变红。
    """
    func = _func(ast.parse(LAUNCH_MODULE.read_text(encoding="utf-8")), "wait_until_ready")
    messages = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        if not (
            isinstance(exc, ast.Call)
            and isinstance(exc.func, ast.Name)
            and exc.func.id == "LaunchError"
        ):
            continue
        messages.append(ast.unparse(exc.args[0]))
    assert len(messages) == 2, (
        f"wait_until_ready 的 LaunchError 出口数变了（{len(messages)}）——"
        "新增/删改出口时请同步这条守卫，并确认每条都带了遮挡诊断"
    )
    for text in messages:
        assert "_with_hint(" in text, f"这条就绪失败没带遮挡诊断：{text}"


@pytest.mark.unit
def test_occlusion_is_measured_before_the_screenshot():
    """遮挡依据必须在**截图之前**取。

    ⚠️ `_save_screenshot()` 会 `set_focus()` 把大厅提到前台 —— 之后再量遮挡，
    量到的是**我们自己刚造出来的**「没被挡住」，真因当场被抹掉。
    2026-09-28 端到端实付：同一条失败，先截图那轮报「窗口可见、未被遮挡」，
    先量再截图那轮才报出「被 WorkBuddy AI 挡住」。
    """
    func = _func(ast.parse(LAUNCH_MODULE.read_text(encoding="utf-8")), "wait_until_ready")
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
