"""P2 安全验证：组长用例表（1.6.11.2S sheet）「安全验证」板块的固定项。

用例对照：数字签名 → test_package_all_signed；数字签名-个人空间盘 →
test_personal_storage_all_signed；控制面板信息检查 → test_control_panel_info；
签章检查报告 → test_signcheck_report_no_ecc。

不改机器状态：只解包/镜像到 reports/_secwork 工作目录，不点大厅、不装卸任何东西。
逆向拦截 / 升级验证几条依赖服务端升级包，见覆盖对照表，不在这里写。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hall_auto import security
from hall_auto.config import load_config

pytestmark = pytest.mark.security


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def extracted_package(cfg):
    if not cfg.security.configured:
        pytest.skip("security.tools_dir 未配置（内部工具不进仓库）")
    return security.extracted_tree(cfg)


def test_package_all_signed(cfg, extracted_package):
    result = security.run_checkappv(extracted_package, cfg)
    assert result.unsigned_count == 0, f"安装包存在未签名文件: {result.unsigned_files}"


def test_personal_storage_all_signed(cfg):
    """个人空间盘目录里的可签名文件必须全部有有效签名。

    需要**目录存在**守卫：`security.personal_storage_dir` 指的是
    `C:/Program Files (x86)/ASUS/PersonalStorage` —— 这是**随产品安装才出现**的路径，
    产品没装（或装在别处）时 `mirror_signable_files` 直接抛 `SecurityToolError`。

    2026-09-28 实测：新机全量跑，`security` 套件里**就这一条红**，而同套件的
    `test_control_panel_info` 却规规矩矩 skip ——
    缺目录是**环境问题，不是"签名有问题"**，红在这里是假红。
    （与 `launch` 那 22 条 ERROR 同源：声明了前提，却没实现"缺则 skip"。）
    """
    if not cfg.security.configured:
        pytest.skip("security.tools_dir 未配置（内部工具不进仓库）")
    src = Path(cfg.security.personal_storage_dir)
    if not src.is_dir():
        pytest.skip(
            f"个人空间盘目录不存在: {src}"
            "（产品未装或装在别处；要验这条先让本机装上带个人空间盘的版本）"
        )
    mirror = security.work_root(cfg) / "ps_mirror"
    count = security.mirror_signable_files(src, mirror)
    assert count > 0, f"个人空间盘目录没有可检文件: {src}"
    result = security.run_checkappv(mirror, cfg)
    assert result.unsigned_count == 0, f"个人空间盘存在未签名文件: {result.unsigned_files}"


def test_control_panel_info(cfg, installed):
    """控制面板里的卸载条目必须与已装大厅一致。

    需要 `installed` 守卫：本用例验的是**已装大厅在控制面板里的呈现**，
    没装时 `hall_entries` 必然返回空（2026-09-28 实测：74 条卸载表条目里
    按不存在的名字筛得 0 条），`assert hall` 会红 —— 那是**假红**，不是缺陷。
    套件的其余用例（签名/签章）只解包，不需要已装，所以守卫只加在这一条上。
    """
    entries = security.control_panel_entries()
    hall = security.hall_entries(entries, cfg.display_name_contains)
    assert hall, "控制面板里没有大厅条目"
    publisher = security.expected_publisher()
    wrong = [f"{e['key']}={e['Publisher']!r}" for e in hall if (e.get("Publisher") or "") != publisher]
    assert not wrong, f"发布者不是{publisher}: {wrong}"
    versions = {e.get("DisplayVersion") for e in hall}
    assert installed.display_version in versions, (
        f"控制面板版本 {versions} 与已装 {installed.display_version} 不一致"
    )
    space_disk = [e["key"] for e in entries if "个人空间盘" in (e.get("DisplayName") or "")]
    assert not space_disk, f"控制面板不应出现个人空间盘信息: {space_disk}"


def test_signcheck_report_no_ecc(cfg, extracted_package):
    report = security.run_signcheck(extracted_package, cfg)
    bad = security.signcheck_ecc_rows(report)
    assert not bad, f"签章检查报告存在 ECC/ECDSA: {bad}"
