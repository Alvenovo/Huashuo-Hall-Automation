from __future__ import annotations

from types import SimpleNamespace

import pytest

from hall_auto.about import normalize_ocr, parse_version


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("vl ． 6 ． 1 1 ． 4", "1.6.11.4"),
        ("v1.6.11.4", "1.6.11.4"),
        ("V1．6．8．17", "1.6.8.17"),
        ("华硕大厅 v1.6.11.4 版权所有", "1.6.11.4"),
        ("没有版本号", None),
    ],
)
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


@pytest.mark.unit
def test_normalize_ocr_strips_spaces_and_fixes_glyphs():
    assert normalize_ocr("vl ． 6 ． 1 1 ． 4") == "v1.6.11.4"


class _Info:
    def __init__(self, aid=None, pid=7):
        self.automation_id = aid
        self.process_id = pid


class _Node:
    def __init__(self, aid):
        self.element_info = _Info(aid)


class _Main:
    def __init__(self, handle, aids, pid=7):
        self.handle = handle
        self._aids = aids
        self.element_info = _Info(pid=pid)

    def descendants(self, control_type=None):
        return [_Node(a) for a in self._aids]


_CFG = SimpleNamespace(display_name_contains="华硕大厅")


@pytest.mark.unit
def test_rebind_if_stale_keeps_live_main(monkeypatch):
    """窗活着且按钮树非空：SetBtn 是真缺，不许重绑掩盖。"""
    import hall_auto.about as about

    calls = []
    monkeypatch.setattr(about, "_hwnd_visible", lambda hwnd: True)
    monkeypatch.setattr(about, "concrete_main", lambda pid, title: calls.append((pid, title)))
    assert about._rebind_if_stale(_CFG, _Main(11, ["SetBtn", "AboutBtn"])) is None
    assert calls == []


@pytest.mark.unit
def test_rebind_if_stale_rebinds_dead_handle(monkeypatch):
    """句柄不可见且按钮树空 = 冻在已销毁的过渡窗上：按 pid 重绑。"""
    import hall_auto.about as about

    sentinel = object()
    monkeypatch.setattr(about, "_hwnd_visible", lambda hwnd: False)
    monkeypatch.setattr(about, "concrete_main", lambda pid, title: sentinel)
    assert about._rebind_if_stale(_CFG, _Main(11, [])) is sentinel


@pytest.mark.unit
def test_setbtn_note_carries_handle_and_aids(monkeypatch):
    import hall_auto.about as about

    monkeypatch.setattr(about, "_hwnd_visible", lambda hwnd: True)
    note = about._setbtn_note(_Main(11, ["CloseBtn", "OkBtn"]))
    assert "hwnd=11" in note and "可见=True" in note and "CloseBtn" in note
