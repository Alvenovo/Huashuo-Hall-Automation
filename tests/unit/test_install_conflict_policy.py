"""锁「同类产品挡住安装器」这条路径：退出码 2 必须能自解释。

2026-09-28 实测：新机首投 `install`，两个用例双双 rc=1、`NSIS 退出码 2`，
证据截图里才看到弹窗 ——「您已安装了华硕大厅或华硕电脑管家，请先卸载该应用，
再运行安装向导！」。

而脚本侧完全不知情：`read_installed()` 只查大厅自己那两把卸载键，
**看不到电脑管家**，于是把「有冲突」读成了「干净机器」——
先跑一次什么都没做的卸载，再启动安装器，最后只丢回一句「NSIS 退出码 2」。

下面的用例把三件事钉死：
1. 扫得到冲突（且**不把大厅自己**当成冲突，否则装了大厅的机器全都装不上）；
2. 明知会被挡，就别先把大厅卸了（不可逆动作要排在只读判断之后）；
3. 报错要能自解释（说清被谁挡的、怎么解、应急开关是什么）。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from hall_auto import installer, product
from hall_auto.config import load_config

pytestmark = pytest.mark.unit


@pytest.fixture
def cfg(tmp_path):
    with mock.patch.dict("os.environ", {"HALL_INSTALLER_DIR": str(tmp_path)}, clear=False):
        yield load_config()


def _registry(entries: dict[str, dict[str, str]]):
    """把「卸载表长什么样」喂给扫描函数：{键叶名: 值字典}。"""
    def _subkeys(hive, root):
        return list(entries)

    def _values(hive, subkey):
        return entries.get(subkey.rsplit("\\", 1)[-1])

    return (
        mock.patch.object(product, "_list_subkeys", _subkeys),
        mock.patch.object(product, "_query_values", _values),
    )


# ---------------- 扫卸载表：找得到 / 不误伤 ----------------


def test_conflict_scan_skips_the_hall_itself():
    """**关键**：大厅自己的显示名就是「华硕大厅」，关键字必然命中。

    不按键叶名 `AsusMemberCenter` 排除的话，凡是装了大厅的机器
    都会被判成「有冲突」—— 升级流程直接全军覆没。
    """
    subs, vals = _registry({
        "AsusMemberCenter": {"DisplayName": "华硕大厅"},
    })
    with subs, vals:
        assert product.find_conflicting_products() == []


def test_conflict_scan_finds_pc_manager():
    """电脑管家在**别的键**上 —— 这正是 `read_installed()` 看不见它的原因。"""
    subs, vals = _registry({
        "ASUSPCManager": {"DisplayName": "华硕电脑管家"},
    })
    with subs, vals:
        found = product.find_conflicting_products()
    assert [c.display_name for c in found] == ["华硕电脑管家"]
    assert "ASUSPCManager" in found[0].key_path


def test_conflict_scan_dedupes_across_registry_views():
    """同一产品在 32/64 两个视图里都有一份，只该报一次。"""
    subs, vals = _registry({
        "PCManager64": {"DisplayName": "华硕电脑管家"},
        "PCManager32": {"DisplayName": "华硕电脑管家"},
    })
    with subs, vals:
        found = product.find_conflicting_products()
    assert len(found) == 1


def test_conflict_scan_ignores_entries_without_a_display_name():
    """空显示名的卸载项很常见（系统补丁等），别拿它去匹配关键字。"""
    subs, vals = _registry({
        "KB5000000": {"DisplayName": ""},
        "NoName": {},
    })
    with subs, vals:
        assert product.find_conflicting_products() == []


def test_conflict_scan_never_raises_when_registry_is_unreadable():
    """探测失败不许抛 —— 扫描失败不该把整轮跑批带下水。"""
    with mock.patch.object(
        product, "_list_subkeys", side_effect=OSError("拒绝访问")
    ):
        with pytest.raises(OSError):
            # 底层函数照抛，说明它本身不吞异常……
            product._list_subkeys(0, "x")
    # ……但入口必须吞掉：注册表读不动时返回空表，让流程照常往下走。
    with mock.patch.object(product, "_list_subkeys", return_value=[]), \
         mock.patch.object(product, "_query_values", return_value=None):
        assert product.find_conflicting_products() == []


# ---------------- 拦截：类型、文案、应急开关 ----------------


def test_install_blocked_is_its_own_error_type():
    """必须是 `InstallError` 的子类（调用方按 InstallError 兜底），

    但又**得能区分** —— 升级流程对 InstallError 的重试会先卸大厅再装一遍，
    冲突没解决时第二遍照样失败，白卸一次。
    """
    assert issubclass(installer.InstallBlocked, installer.InstallError)


def test_block_message_says_who_blocked_and_how_to_get_past_it():
    fake = [product.ConflictingProduct("华硕电脑管家", r"HKLM\SOFTWARE\...\ASUSPCManager")]
    with mock.patch.object(installer, "find_conflicting_products", lambda: fake):
        with pytest.raises(installer.InstallBlocked) as exc:
            installer.check_install_blockers()
    msg = str(exc.value)
    assert "华硕电脑管家" in msg, "得说清是谁挡的，否则又要去翻截图"
    assert "HALL_IGNORE_INSTALL_CONFLICT=1" in msg, "应急开关必须写在报错里"
    assert "华硕电脑管家" in msg and "先卸载" in msg, "得给出可执行的下一步"


def test_escape_hatch_skips_the_check():
    """确认不冲突时（比如守卫只对某个版本生效）得能跳过这道拦截。"""
    fake = [product.ConflictingProduct("华硕电脑管家", r"HKLM\SOFTWARE\...\ASUSPCManager")]
    with mock.patch.object(installer, "find_conflicting_products", lambda: fake), \
         mock.patch.dict("os.environ", {"HALL_IGNORE_INSTALL_CONFLICT": "1"}):
        installer.check_install_blockers()  # 不抛


def test_check_passes_when_no_conflict():
    with mock.patch.object(installer, "find_conflicting_products", list):
        installer.check_install_blockers()  # 不抛


# ---------------- 顺序：只读判断排在不可逆动作之前 ----------------


def test_baseline_clean_refuses_before_uninstalling_the_hall(cfg):
    """**核心**：明知会被挡，就别先把大厅卸了。

    卸载不可逆。原顺序是「先卸 → 再装」，冲突时机器会停在
    「大厅被卸掉且装不回来」—— 对跑批是最坏结果。
    """
    fake = [product.ConflictingProduct("华硕电脑管家", r"HKLM\SOFTWARE\...\ASUSPCManager")]
    with mock.patch.object(installer, "find_conflicting_products", lambda: fake), \
         mock.patch.object(installer, "uninstall_hall") as uninstall, \
         mock.patch.object(installer, "resolve_setup") as resolve, \
         mock.patch.object(installer, "_run_nsis") as run:
        with pytest.raises(installer.InstallBlocked):
            installer.install_baseline_clean(cfg)
    uninstall.assert_not_called()
    resolve.assert_not_called()
    run.assert_not_called()


def test_upgrade_does_not_retry_a_blocked_install(cfg, tmp_path):
    """被挡住时**不许**走「卸掉重装」那条重试分支。

    那条分支对瞬时故障有用；对冲突只会白卸一次大厅，然后同样失败。
    """
    expected = installer.expected_version_from_setup_name(cfg.baseline_setup)
    installed = mock.Mock(display_name="华硕大厅", display_version=expected)
    setup = tmp_path / cfg.latest_setup
    setup.write_bytes(b"")
    fake = [product.ConflictingProduct("华硕电脑管家", r"HKLM\SOFTWARE\...\ASUSPCManager")]
    with mock.patch.object(installer, "read_installed", return_value=installed), \
         mock.patch.object(installer, "find_conflicting_products", lambda: fake), \
         mock.patch.object(installer, "resolve_setup", return_value=setup), \
         mock.patch.object(installer, "uninstall_hall") as uninstall:
        with pytest.raises(installer.InstallBlocked):
            installer.upgrade_to_latest(cfg)
    uninstall.assert_not_called()


def test_upgrade_still_retries_ordinary_install_failures(cfg):
    """反向：普通安装失败该重试还得重试，别把这条兜底也掐了。"""
    expected = installer.expected_version_from_setup_name(cfg.baseline_setup)
    installed = mock.Mock(display_name="华硕大厅", display_version=expected)
    ok = mock.Mock(display_name="华硕大厅", display_version="9.9.9")
    calls = []

    def _install_setup(cfg_, path):
        calls.append(path)
        if len(calls) == 1:
            raise installer.InstallError("NSIS 退出码 2")
        return ok

    with mock.patch.object(installer, "read_installed", return_value=installed), \
         mock.patch.object(installer, "find_conflicting_products", list), \
         mock.patch.object(installer, "resolve_setup", return_value=Path("C:/x/setup.exe")), \
         mock.patch.object(installer, "install_setup", _install_setup), \
         mock.patch.object(installer, "uninstall_hall") as uninstall:
        result = installer.upgrade_to_latest(cfg)
    assert result is ok
    uninstall.assert_called_once()
    assert len(calls) == 2


# ---------------- 报错兜底：安装路径才带冲突提示 ----------------


def _fake_popen(returncode: int):
    proc = mock.Mock()
    proc.wait.return_value = returncode
    return mock.patch.object(installer.subprocess, "Popen", return_value=proc)


def test_nsis_failure_on_install_path_carries_the_conflict_hint(tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"")
    fake = [product.ConflictingProduct("华硕电脑管家", r"HKLM\SOFTWARE\...\ASUSPCManager")]
    with _fake_popen(2), \
         mock.patch.object(installer, "find_conflicting_products", lambda: fake):
        with pytest.raises(installer.InstallError) as exc:
            installer._run_nsis(exe, 10, hint_conflict=True)
    assert "NSIS 退出码 2" in str(exc.value)
    assert "华硕电脑管家" in str(exc.value), "安装路径必须带上真因"


def test_nsis_failure_on_uninstall_path_stays_plain(tmp_path):
    """卸载路径别乱扣帽子：那儿的非 0 退出是另一回事。"""
    exe = tmp_path / "uninst.exe"
    exe.write_bytes(b"")
    fake = [product.ConflictingProduct("华硕电脑管家", r"HKLM\SOFTWARE\...\ASUSPCManager")]
    with _fake_popen(2), \
         mock.patch.object(installer, "find_conflicting_products", lambda: fake):
        with pytest.raises(installer.InstallError) as exc:
            installer._run_nsis(exe, 10)
    assert "华硕电脑管家" not in str(exc.value)


def test_nsis_timeout_on_install_path_mentions_the_modal_dialog(tmp_path):
    """超时那条也得给方向：静默参数压不住模态弹窗，人没点就一直挂着。

    否则现场看到的是「装得慢」，实际是压根没开始装。
    """
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"")
    proc = mock.Mock()
    proc.wait.side_effect = installer.subprocess.TimeoutExpired(cmd="x", timeout=10)
    with mock.patch.object(installer.subprocess, "Popen", return_value=proc), \
         mock.patch.object(installer, "find_conflicting_products", list):
        with pytest.raises(installer.InstallError) as exc:
            installer._run_nsis(exe, 10, hint_conflict=True)
    assert "模态弹窗" in str(exc.value)
    proc.kill.assert_called_once()
