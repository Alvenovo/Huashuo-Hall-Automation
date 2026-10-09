from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from PIL import Image

from hall_auto.config import Config, REPO_ROOT
from hall_auto.launch import (
    LaunchError,
    _press_button,
    _save_screenshot,
    concrete_main,
    start_fresh,
    wait_main_window,
    wait_until_ready,
)
from hall_auto.product import stop_main_process
from hall_auto.waiting import wait_until_or_raise
from hall_auto.winapi import _hwnd_visible

ABOUT_DIR = REPO_ROOT / "reports" / "about"
OCR_SCRIPT = REPO_ROOT / "tools" / "ocr_text.ps1"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")

# 关于页的版本号 UIA name 只是占位符 ver（产品无障碍缺陷），只能 OCR 截图。
# 中文 OCR 引擎对这行浅灰小字的常见误认，按字形归一：
_OCR_FIXES = {
    "．": ".",
    "，": ".",
    "、": ".",
    "l": "1",
    "I": "1",
    "O": "0",
    "o": "0",
    "Z": "2",
    "S": "5",
    "B": "8",
}


def normalize_ocr(text: str) -> str:
    out = []
    for ch in text:
        if ch.isspace():
            continue
        out.append(_OCR_FIXES.get(ch, ch))
    return "".join(out)


def parse_version(text: str) -> str | None:
    match = VERSION_RE.search(normalize_ocr(text))
    return match.group(0) if match else None


def _find_aid(main, control_type: str, aid: str):
    for node in main.descendants(control_type=control_type):
        try:
            if (node.element_info.automation_id or "") == aid:
                return node
        except Exception:
            continue
    return None


def _click_aid(main, aid: str) -> bool:
    node = _find_aid(main, "Button", aid)
    return node is not None and _press_button(node)


def _button_aids(main) -> list[str]:
    try:
        nodes = main.descendants(control_type="Button")
    except Exception:
        return []
    aids: list[str] = []
    for node in nodes:
        try:
            aids.append(node.element_info.automation_id or "")
        except Exception:
            continue
    return aids


def _setbtn_note(main) -> str:
    try:
        handle = main.handle
    except Exception:
        handle = None
    visible = _hwnd_visible(handle) if handle else False
    return f"hwnd={handle} 可见={visible} 按钮aid={_button_aids(main)[:24]}"


def _rebind_if_stale(cfg: Config, main):
    """就绪后主窗口句柄仍可能冻在已销毁的过渡窗上：句柄不可见或按钮树空就按 pid 重绑重试；窗活着且有按钮说明 SetBtn 真缺，不重绑。"""
    try:
        handle = main.handle
        pid = main.element_info.process_id
    except Exception:
        return None
    if handle and _hwnd_visible(handle) and _button_aids(main):
        return None
    try:
        return concrete_main(pid, cfg.display_name_contains)
    except LaunchError:
        return None


def _version_node(main):
    for node in main.descendants(control_type="Text"):
        try:
            if (node.element_info.automation_id or "") == "VersionLabel":
                return node
        except Exception:
            continue
    return None


def _ocr_image(path: Path) -> str:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(OCR_SCRIPT),
            "-ImagePath",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise LaunchError(f"OCR 失败: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _prepare_for_ocr(raw: Path, ready: Path) -> None:
    img = Image.open(raw).convert("L")
    big = img.resize((img.width * 8, img.height * 8), Image.LANCZOS)
    padded = Image.new("L", (big.width + 80, big.height + 80), 255)
    padded.paste(big, (40, 40))
    padded.point(lambda p: 0 if p < 200 else 255).save(ready)


def read_about_version(cfg: Config) -> str:
    """进 设置 → 关于，OCR 关于页版本号。返回四段版本号字符串。"""
    ABOUT_DIR.mkdir(parents=True, exist_ok=True)
    app = start_fresh(cfg)
    try:
        main = wait_main_window(app, cfg)
        wait_until_ready(cfg, main)
        btn = _find_aid(main, "Button", "SetBtn")
        if btn is None:
            rebound = _rebind_if_stale(cfg, main)
            if rebound is not None:
                print("[env] 关于页：主窗口句柄已失效，重绑后重试 SetBtn")
                main = rebound
                btn = _find_aid(main, "Button", "SetBtn")
        if btn is None or not _press_button(btn):
            evidence = _save_screenshot(main, "about_setbtn")
            raise LaunchError(
                f"关于页：点不到设置按钮 SetBtn；{_setbtn_note(main)}；截图: {evidence}",
                evidence=evidence,
            )
        wait_until_or_raise(
            lambda: _find_aid(main, "Button", "AboutBtn") is not None,
            "关于页：点完设置后 AboutBtn 没出现（设置页没打开？）",
        )
        if not _click_aid(main, "AboutBtn"):
            raise LaunchError("关于页：点不到关于按钮 AboutBtn")
        wait_until_or_raise(
            lambda: _version_node(main) is not None,
            "关于页：找不到 VersionLabel 节点",
            interval=0.5,
        )
        node = _version_node(main)
        try:
            main.set_focus()
            time.sleep(0.5)
        except Exception:
            pass
        stamp = time.strftime("%Y%m%d_%H%M%S")
        raw = ABOUT_DIR / f"{stamp}_version_raw.png"
        node.capture_as_image().save(raw)
        ready = ABOUT_DIR / f"{stamp}_version_ocr.png"
        _prepare_for_ocr(raw, ready)
        text = _ocr_image(ready)
        version = parse_version(text)
        if version is None:
            raise LaunchError(f"关于页：OCR 没认出四段版本号，原文 {text!r}，截图 {ready}")
        return version
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)
