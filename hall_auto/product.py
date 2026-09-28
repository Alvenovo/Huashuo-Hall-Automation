from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import winreg

UNINSTALL_KEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\AsusMemberCenter"
UNINSTALL_KEY_32 = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AsusMemberCenter"
EXE_NAME = "AsusMemberCenter.exe"

# 「同类产品」：安装器（NSIS 的 .onInit）一检测到这些在装就**拒绝安装**。
# 2026-09-28 实测弹窗原文：
#   「您已安装了华硕大厅或华硕电脑管家，请先卸载该应用，再运行安装向导！」
# 点掉「确定」后安装器以**退出码 2** 结束（NSIS 脚本里 `Abort` 的返回码）。
#
# 为什么必须单独扫：`read_installed()` 只查大厅自己那两把卸载键，
# **看不见电脑管家** —— 于是脚本把「有冲突」读成了「干净机器」，
# 先跑一次什么都没做的卸载、再启动安装器，最后只丢回一句「NSIS 退出码 2」。
CONFLICT_DISPLAY_KEYWORDS = ("电脑管家", "华硕大厅")

# 大厅自己的卸载键叶名 —— 它不算冲突（升级流程本来就要先卸它）。
_MANAGED_KEY_LEAF = "AsusMemberCenter"

# 「这就是我们自己」的身份串：卸载命令 / 图标 / 安装目录里出现它，说明这条卸载项
# 指的就是大厅本体。**光按叶名排除不够** —— 万一产品在别的键（比如 GUID 键）上
# 也注册了一份，只按叶名就会把它当成"冲突"，于是**本来能装的安装被自己挡住**。
_OWN_PRODUCT_MARKERS = ("AsusMemberCenter",)

_UNINSTALL_ROOTS = (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
)
DEFAULT_INSTALL_DIR = Path(r"C:\Program Files (x86)\ASUS\ASUS Member Center")

CRITICAL_PROCESS_NAMES = ("AsusMemberCenter",)
HELPER_PROCESS_NAMES = (
    "AppStoreServer",
    "NotifyApp",
    "UpAppNotify",
    "AppCheck",
)
PROCESS_NAMES = CRITICAL_PROCESS_NAMES + HELPER_PROCESS_NAMES


@dataclass(frozen=True)
class InstalledProduct:
    display_name: str
    display_version: str
    uninstall_string: str
    install_dir: Path
    exe_path: Path
    file_version: str | None
    key_path: str


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _query_values(hive: int, subkey: str) -> dict[str, str] | None:
    try:
        with winreg.OpenKey(hive, subkey) as key:
            data: dict[str, str] = {}
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                data[str(name)] = "" if value is None else str(value)
                i += 1
            return data
    except FileNotFoundError:
        return None


def _file_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    GetFileVersionInfoSizeW = ctypes.windll.version.GetFileVersionInfoSizeW
    GetFileVersionInfoW = ctypes.windll.version.GetFileVersionInfoW
    VerQueryValueW = ctypes.windll.version.VerQueryValueW

    size = GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not GetFileVersionInfoW(str(path), 0, size, buf):
        return None

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    lptr = ctypes.c_void_p()
    llen = wintypes.UINT()
    if not VerQueryValueW(buf, "\\", ctypes.byref(lptr), ctypes.byref(llen)):
        return None
    info = ctypes.cast(lptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    return "{}.{}.{}.{}".format(
        info.dwFileVersionMS >> 16,
        info.dwFileVersionMS & 0xFFFF,
        info.dwFileVersionLS >> 16,
        info.dwFileVersionLS & 0xFFFF,
    )


def _install_dir_from(values: dict[str, str]) -> Path:
    loc = (values.get("InstallLocation") or "").strip().strip('"')
    if loc:
        return Path(loc)
    icon = (values.get("DisplayIcon") or "").strip().strip('"')
    if icon:
        return Path(icon).parent
    return DEFAULT_INSTALL_DIR


def read_installed() -> InstalledProduct | None:
    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY),
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY_32),
    ):
        values = _query_values(hive, subkey)
        if not values:
            continue
        install_dir = _install_dir_from(values)
        exe_path = install_dir / EXE_NAME
        hive_name = "HKLM"
        return InstalledProduct(
            display_name=values.get("DisplayName") or "",
            display_version=(values.get("DisplayVersion") or "").strip(),
            uninstall_string=values.get("UninstallString") or values.get("QuietUninstallString") or "",
            install_dir=install_dir,
            exe_path=exe_path,
            file_version=_file_version(exe_path),
            key_path=f"{hive_name}\\{subkey}",
        )
    return None


def _list_subkeys(hive: int, subkey: str) -> list[str]:
    """列出某把注册表键下的子键名。键不存在或读不动一律返回空表（只读探测，不抛）。"""
    try:
        with winreg.OpenKey(hive, subkey) as key:
            names: list[str] = []
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
            return names
    except OSError:
        return []


@dataclass(frozen=True)
class ConflictingProduct:
    """会让安装器拒绝安装的「同类产品」（电脑管家等）。"""

    display_name: str
    key_path: str


def _is_our_own_product(leaf: str, values: dict[str, str]) -> bool:
    """这条卸载项指的就是大厅本体吗（叶名命中，或身份串命中）。

    比对前**去掉非字母数字**：注册表里那条路径写作 `ASUS Member Center`（带空格），
    而产品标识串是 `AsusMemberCenter`（不带）—— 直接比会漏。
    """
    if leaf == _MANAGED_KEY_LEAF:
        return True
    blob = "".join(
        ch
        for ch in " ".join(
            values.get(name) or ""
            for name in (
                "UninstallString",
                "QuietUninstallString",
                "DisplayIcon",
                "InstallLocation",
            )
        ).lower()
        if ch.isalnum()
    )
    return any(marker.lower() in blob for marker in _OWN_PRODUCT_MARKERS)


def find_conflicting_products() -> list[ConflictingProduct]:
    """扫卸载表，找出安装器会因此拒绝安装的同类产品。

    **只读**，不改任何东西；读不动就返回空表（探测失败不该把跑批带下水）。

    为什么不能靠 `read_installed()`：它只查 `AsusMemberCenter` 那两把键，
    电脑管家在**别的键**上，于是「有冲突」被读成「干净机器」——
    2026-09-28 新机首投 `install` 就是这么白跑一趟的（先卸了个不存在的大厅，
    再启动安装器，弹窗、退出码 2，脚本只会说「NSIS 退出码 2」）。

    ⚠️ **必须排除大厅自己**：它的显示名就是「华硕大厅」，关键字必然命中。
    不排除的话，凡是装了大厅的机器全被判成「有冲突」，
    而调用方 `check_install_blockers()` 会**直接拦住安装** —— 等于自己把自己锁死。
    """
    found: list[ConflictingProduct] = []
    seen: set[str] = set()
    for root in _UNINSTALL_ROOTS:
        for leaf in _list_subkeys(winreg.HKEY_LOCAL_MACHINE, root):
            values = _query_values(winreg.HKEY_LOCAL_MACHINE, f"{root}\\{leaf}")
            if not values:
                continue
            if _is_our_own_product(leaf, values):
                continue  # 大厅自己：升级流程本来就要先卸它，不算冲突
            name = (values.get("DisplayName") or "").strip()
            if not name or name in seen:
                continue
            lowered = name.lower()
            if any(kw.lower() in lowered for kw in CONFLICT_DISPLAY_KEYWORDS):
                seen.add(name)
                found.append(
                    ConflictingProduct(
                        display_name=name,
                        key_path=f"HKLM\\{root}\\{leaf}",
                    )
                )
    return found


def agreement_config_path() -> Path | None:
    """首跑隐私协议同意标记所在的用户级 user.config（Agreen 设置）。

    路径含 exe 哈希目录与版本号目录，用 glob 找；找不到返回 None（用例 skip）。
    只读/写用户 AppData，无需提权。
    """
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "AppStore"
    if not base.is_dir():
        return None
    candidates = sorted(base.glob("AsusMemberCenter.exe_Url_*/*/user.config"))
    return candidates[-1] if candidates else None


def read_agreen() -> str | None:
    path = agreement_config_path()
    if path is None:
        return None
    match = re.search(
        r'<setting name="Agreen"[^>]*>\s*<value>([^<]*)</value>',
        path.read_text(encoding="utf-8"),
    )
    return match.group(1) if match else None


def set_agreen(value: str) -> None:
    path = agreement_config_path()
    if path is None:
        raise LookupError("未找到大厅 user.config，无法写 Agreen")
    text = path.read_text(encoding="utf-8")
    new = re.sub(
        r'(<setting name="Agreen"[^>]*>\s*<value>)[^<]*(</value>)',
        rf"\g<1>{value}\g<2>",
        text,
        count=1,
    )
    if new == text:
        raise LookupError("user.config 里没有 Agreen 设置")
    path.write_text(new, encoding="utf-8")


def image_running(stem: str) -> bool:
    target = stem.lower().removesuffix(".exe")
    return any(name.lower() == target for name in _running_image_names())


def _running_image_names() -> set[str]:
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith('"'):
            continue
        image = line.split(",")[0].strip('"')
        if image.lower().endswith(".exe"):
            names.add(image[:-4])
        else:
            names.add(image)
    return names


def _kill_image(name: str) -> None:
    for args in (
        ["taskkill", "/IM", f"{name}.exe", "/F", "/T"],
        ["taskkill", "/IM", f"{name}.exe", "/F"],
    ):
        subprocess.run(args, capture_output=True, check=False)


def stop_main_process(timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if "AsusMemberCenter" not in _running_image_names():
            return
        _kill_image("AsusMemberCenter")
        time.sleep(1)
    if "AsusMemberCenter" in _running_image_names():
        raise TimeoutError("未能结束主进程 AsusMemberCenter")


def stop_product(timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        running = [name for name in PROCESS_NAMES if name in _running_image_names()]
        if not running:
            return
        for name in running:
            _kill_image(name)
        time.sleep(1)
    leftover_critical = [
        name for name in CRITICAL_PROCESS_NAMES if name in _running_image_names()
    ]
    if leftover_critical:
        raise TimeoutError(f"未能结束主进程: {leftover_critical}")
