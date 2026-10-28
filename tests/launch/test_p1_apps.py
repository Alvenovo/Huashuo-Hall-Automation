from __future__ import annotations

import pytest

from hall_auto.apps import (
    SYNC_ITEM_PLACEHOLDER,
    _by_aid,
    app_list_state,
    install_fixture,
    installed_match,
    is_app_installed,
    open_mine,
    read_mine_lists,
    uninstall_fixture,
)
from hall_auto.launch import (
    concrete_main,
    start_fresh,
    wait_main_window,
    wait_until_ready,
)
from hall_auto.login import logged_in, login_with_password, logout
from hall_auto.product import stop_main_process
from hall_auto.winapi import occlusion_hint, window_state_facts


@pytest.fixture(scope="module")
def ready_main(cfg, installed):
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    yield main
    stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.apps
def test_mine_lists_refresh(ready_main):
    """三个页签都要能刷出数据；空要能区分「本来没有」和「接口失败」。"""
    states = read_mine_lists(ready_main)
    for tab, state in states.items():
        assert state.loaded, f"{tab} 列表没刷出来：容器和空态提示都没找到"

    uninstall = states["uninstall"].items
    matched = [name for name in uninstall if installed_match(name)]
    assert matched or not uninstall, (
        f"卸载列表和系统注册表对不上，怀疑接口失败而不是本来没有: {uninstall}"
    )


@pytest.mark.apps
@pytest.mark.destructive
def test_fixture_install_then_uninstall(cfg, ready_main):
    """P1-A 夹具真装真卸：大厅装完注册表有条目、卸完条目消失"""
    app_name = cfg.fixture_apps.install or cfg.fixture_apps.uninstall
    if not app_name:
        pytest.skip("没配夹具应用：config.local.yaml 的 fixture_apps.install")
    if is_app_installed(app_name):
        pytest.skip(f"{app_name} 已在机器上，先手工卸掉再跑真装真卸")

    matched = install_fixture(cfg, ready_main, app_name)
    assert matched, f"装完注册表里找不到 {app_name}"
    assert is_app_installed(app_name)

    uninstall_fixture(cfg, ready_main, cfg.fixture_apps.uninstall or app_name)
    assert not is_app_installed(app_name), f"卸完 {app_name} 还在注册表里"


def _occlusion_suffix(pid: int) -> str:
    """把「大厅窗口当时是什么状态」拼进失败信息。

    2026-09-23 真机：这条断言超时 45s，而失败截图里**大厅一个像素都没露**。
    大厅主内容区是 WebView2，**被遮挡 / 最小化 / 不可见时 Chromium 不渲染** →
    页面永远刷不出来，而 Win32 控件层照样读得到。原来只能靠人逐张看 PNG 截图才能定性。

    ⚠️ **诊断说"没问题"时也要给依据**（`window_state_facts`）——
    只报结论的话，遇到"结论说没事、截图说大厅不在屏幕上"就只能猜，
    而两边都不足以定性（2026-09-23 就卡在这儿）。
    """
    hint = occlusion_hint(pid)
    if hint:
        return f"\n  ⚠️ {hint}"
    return f"\n  ℹ️ 窗口诊断未发现问题。{window_state_facts(pid)}"


@pytest.mark.apps
@pytest.mark.login
def test_sync_list_logged_in(cfg, installed):
    """登录后同步列表能刷出「其他电脑已装应用」。只读断言：
    页底「全部安装」（InstallAllBtn）和条目安装会真装 QQ/网盘这类日常软件，绝不点。
    自带起停（start_fresh 会顶掉 ready_main 的进程），所以放在本模块最后。

    ⚠️ 本用例**不走** `ready_main` 夹具，所以必须自己带 `installed` 守卫 ——
    2026-09-28 之前没有，未安装的机器上会抛 `LaunchError` 而不是 skip。
    """
    user, password = cfg.test_account()
    if not user or not password:
        pytest.skip("同步列表要登录：设 HALL_TEST_USER / HALL_TEST_PASSWORD")
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    pid = main.element_info.process_id
    try:
        if logged_in(pid):
            logout(pid)
        login_with_password(cfg, pid)
        # 登录后主窗口 UIA Name 变「华硕应用商店」，按标题懒解析的 spec 失效，按 hwnd 重包
        logged_main = concrete_main(pid, cfg.display_name_contains)
        open_mine(logged_main)
        state = app_list_state(logged_main, "sync")
        assert state.loaded and not state.needs_login, (
            "登录后同步页仍没刷出列表（NoAppListBox 没出现）"
            f"（loaded={state.loaded}, needs_login={state.needs_login}, items={len(state.items)}）"
            + _occlusion_suffix(pid)
        )
        assert state.items, "同步列表为空：该账号在其他电脑没有安装记录，或条目名没读出来"
        assert all(name != SYNC_ITEM_PLACEHOLDER for name in state.items), (
            f"条目名读到的还是占位内部类名: {list(state.items)}"
        )
        assert _by_aid(logged_main, "Button", "InstallAllBtn") is not None, (
            "同步页没找到「全部安装」按钮（只断言存在，不点）"
        )
    finally:
        try:
            logout(pid)
        except Exception:
            pass
        stop_main_process(cfg.timeouts.process_stop_sec)
