from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pywinauto import Application
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from hall_auto.config import Config, LaunchSettings, REPO_ROOT
from hall_auto.product import EXE_NAME, read_installed, stop_main_process
from hall_auto.winapi import (
    _hwnd_pid,
    _hwnd_rect,
    _hwnd_text,
    _hwnd_visible,
    _top_hwnds,
    ensure_window_shown,
    occlusion_hint,
    wheel_at,
)

REPORTS_DIR = REPO_ROOT / "reports" / "launch"

HOME_TAB_NAME = "推荐"

# 启动卡片窗的 UIA Name 标记：Win32 标题与主窗口同为「华硕大厅」，靠这个名字区分。
# 与 config 的 popup_whitelist_keywords「启动卡片」同源，但用途不同（认窗 vs 关弹窗）。
CARD_WINDOW_NAME_MARK = "启动卡片"


class LaunchError(RuntimeError):
    def __init__(self, message: str, evidence: Path | None = None):
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True)
class LaunchResult:
    title: str
    structure_ids: tuple[str, ...]
    evidence: Path | None = None


def popup_text(name: str) -> str:
    return (name or "").strip()


def is_whitelisted(name: str, settings: LaunchSettings) -> bool:
    text = popup_text(name)
    return any(key and key in text for key in settings.popup_whitelist_keywords)


def prefer_dismiss(name: str, settings: LaunchSettings) -> bool:
    text = popup_text(name)
    return any(key and key in text for key in settings.update_or_card_keywords)


def _overlay_text(node, limit: int = 160) -> str:
    """把覆盖层子树里的**文字**抠出来 —— 报错要报出弹窗**说了什么**，不是它**叫什么**。

    2026-09-28 实付：`wait_until_ready` 报 `未知弹窗: ['Splash']`。名字 `Splash`
    毫无信息量，而那个框里写的是一句决定性的话：

        检测到您的电脑缺少程序运行必需组件或组件版本低，请按提示安装

    **真因就写在框里，报错却把它丢了** —— 于是有人（就是我）去猜「Splash 是什么」，
    还差点把它当加载态**放行**掉，等于把一条产品侧线索静默吞掉。
    **名字是壳，文字才是内容。**
    （顺带：那个框是**偶发**的 —— 同一天 17:08 冷启动就不弹、`launch` 4/4 通过，
    别拿一次现场定性成「本机环境墙」。见《运行手册》坑 19「定性更正」。）
    """
    texts: list[str] = []
    seen: set[str] = set()
    try:
        nodes = node.descendants()
    except Exception:
        return ""
    for child in nodes:
        try:
            name = popup_text(child.element_info.name)
        except Exception:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        texts.append(name)
    joined = " / ".join(texts)
    return joined[:limit] + "…" if len(joined) > limit else joined


def _save_screenshot(wrapper, stem: str) -> Path | None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stem}.png"
    try:
        # capture_as_image 抓的是屏幕像素，不置顶会拍到挡在前面的窗口
        wrapper.set_focus()
        time.sleep(0.5)
    except Exception:
        pass
    try:
        wrapper.capture_as_image().save(path)
        return path
    except Exception:
        return None


@dataclass(frozen=True)
class Overlay:
    name: str
    node: object
    first_run: bool = False


def _wrap_hwnd(hwnd: int):
    try:
        return UIAWrapper(UIAElementInfo(hwnd))
    except Exception:
        return None


def _overlay_windows(main, settings: LaunchSettings, main_handle=None, pid=None) -> list[Overlay]:
    """大厅自己弹的覆盖层：主窗口下的子 Window + 同进程的其他顶层窗。

    顶层窗用 Win32 EnumWindows 按 pid 过滤（0.00s 级），只把命中的 hwnd 包成
    UIA 节点；Desktop().windows() 全系统 UIA 枚举实测 1s 左右，就绪循环每轮
    要跑多次，用不起。first_run 只对非白名单顶层窗算 —— 首跑协议窗是独立
    顶层窗，子 Window 用不着走那次昂贵的 Button 子树遍历。

    main_handle / pid 是窗口生命周期内的不变量，但 UIA 每次取要 ~1s（实测），
    就绪循环里别反复取 —— 调用方算一次传进来；不传才退化成本地取。
    """
    found: list[Overlay] = []
    seen = set()
    if main_handle is None:
        try:
            main_handle = main.handle
        except Exception:
            main_handle = None

    def _add(name: str, node, top_level: bool) -> None:
        try:
            handle = node.handle
        except Exception:
            handle = id(node)
        if handle in seen or (main_handle is not None and handle == main_handle):
            return
        if not name:
            return
        seen.add(handle)
        first_run = (
            top_level
            and not is_whitelisted(name, settings)
            and _looks_like_first_run(node)
        )
        found.append(Overlay(name=name, node=node, first_run=first_run))

    try:
        nodes = main.descendants(control_type="Window")
    except Exception:
        nodes = []
    for node in nodes:
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        _add(name, node, top_level=False)

    if pid is None:
        try:
            pid = main.element_info.process_id
        except Exception:
            pid = None
    if pid:
        for hwnd in _top_hwnds():
            # EnumWindows 连隐藏的消息窗（Default IME 之类）一起给，UIA 的
            # Desktop 枚举不会，必须自己过滤，否则全被当成未知弹窗
            if not _hwnd_visible(hwnd) or _hwnd_pid(hwnd) != pid:
                continue
            if main_handle is not None and hwnd == main_handle:
                continue
            node = _wrap_hwnd(hwnd)
            if node is None:
                continue
            # UIA Name 优先、Win32 标题兜底 —— 和上面子 Window 分支同序。
            # 启动卡片窗的 Win32 标题是「华硕大厅」（和主窗口同名、毫无区分度），
            # 能对上白名单的「启动卡片」只在 UIA Name「启动卡片窗口」里；
            # 按 Win32 标题取名会把它报成「未知弹窗」直接判死（2026-10-09 本机实付）。
            try:
                name = popup_text(node.element_info.name)
            except Exception:
                name = ""
            if not name:
                name = popup_text(_hwnd_text(hwnd))
            _add(name, node, top_level=True)
    return found


def click_first_run_agree(main, settings: LaunchSettings, present: set[str]) -> bool:
    """首次协议画在主窗口上时，没有子 Window，直接点「同意」。更新框有 VersionLabel 则不点同意。"""
    if settings.required_auto_ids[0] in present:
        return False
    if "VersionLabel" in present or "ChangeLog" in present:
        return False
    return _click_named_button(main, settings.accept_buttons)


def _press_button(btn) -> bool:
    """优先 UIA Invoke：不依赖鼠标坐标和窗口层级。click_input 在大厅被遮挡时会点到遮挡窗口上。"""
    try:
        btn.invoke()
        return True
    except Exception:
        pass
    try:
        btn.set_focus()
        btn.click_input()
        return True
    except Exception:
        return False


def matches_button(aid: str, name: str, labels: tuple[str, ...]) -> bool:
    if aid in ("CancelBtn", "closebtn"):
        return any(x in ("取消", "关闭") for x in labels)
    if aid == "ConfirmBtn":
        return any(x in ("同意", "确定") for x in labels)
    return any(label and label in name for label in labels)


def _click_named_button(overlay, labels: tuple[str, ...]) -> bool:
    try:
        buttons = overlay.descendants(control_type="Button")
    except Exception:
        return False
    for btn in buttons:
        try:
            aid = btn.element_info.automation_id or ""
            name = popup_text(btn.element_info.name or btn.window_text())
        except Exception:
            continue
        if matches_button(aid, name, labels) and _press_button(btn):
            return True
    return False


def _looks_like_first_run(overlay) -> bool:
    labels = []
    try:
        buttons = overlay.descendants(control_type="Button")
    except Exception:
        return False
    for btn in buttons:
        try:
            labels.append(popup_text(btn.element_info.name or btn.window_text()))
        except Exception:
            continue
    return any("同意" == x or x.endswith("同意") for x in labels) and any(
        "取消" in x for x in labels
    )


def dismiss_whitelist_popups(overlays, settings: LaunchSettings) -> None:
    for overlay in overlays:
        name = overlay.name
        first_run = overlay.first_run
        if not (is_whitelisted(name, settings) or first_run):
            continue
        if prefer_dismiss(name, settings) and not first_run:
            labels = settings.dismiss_buttons
        else:
            labels = settings.accept_buttons
        if not _click_named_button(overlay.node, labels) and labels != settings.dismiss_buttons:
            _click_named_button(overlay.node, settings.dismiss_buttons)
        time.sleep(1)


def is_unknown(overlay: Overlay, settings: LaunchSettings) -> bool:
    return not (is_whitelisted(overlay.name, settings) or overlay.first_run)


_NAV_ANCHOR_NAMES = ("壁纸", "分类", "我的")


def _nav_list_from(named_items: list) -> object | None:
    """左侧导航 List 容器：用「壁纸/分类/我的」这几个登录态也不改名的页签锚定。

    首页页签登录态显示账号昵称（实测 wmw001），不能拿名字认导航；而壁纸/分类/我的
    在未登录、登录两种态下都叫这个名。
    """
    for node, name in named_items:
        if name not in _NAV_ANCHOR_NAMES:
            continue
        try:
            parent = node.parent()
            info = parent.element_info
        except Exception:
            continue
        if info is not None and info.control_type == "List":
            return parent
    return None


def _nav_items(nav) -> list:
    """导航页签按屏幕纵向排序：[(top, name, node)]，读不到的跳过。"""
    out = []
    try:
        nodes = nav.descendants(control_type="ListItem")
    except Exception:
        return out
    for node in nodes:
        try:
            name = popup_text(node.element_info.name or node.window_text())
            top = node.rectangle().top
        except Exception:
            continue
        out.append((top, name, node))
    out.sort(key=lambda item: item[0])
    return out


def _click_nav_home(nav) -> bool:
    """导航列表是虚拟化的：选中项不在顶部时首页页签根本不在 UIA 树里。

    节点真机（2026-10-09）落在折扣页时导航滚掉了两行，树里只剩
    折扣/壁纸/分类/我的，按名字找「推荐」永远落空 → 就绪循环空转 150s。
    所以先滚回顶（ScrollPattern，不灵就滚轮），再点最上面一项
    （登录态它是昵称页签，未登录是「推荐」，位置不变）。
    """
    items = _nav_items(nav)
    pick = next((n for _, name, n in items if name == HOME_TAB_NAME), None)
    if pick is None and items:
        before = items[0][1]
        try:
            nav.iface_scroll.SetScrollPercent(-1.0, 0.0)
        except Exception:
            pass
        time.sleep(0.3)
        items = _nav_items(nav)
        pick = next((n for _, name, n in items if name == HOME_TAB_NAME), None)
        if pick is None and items and items[0][1] == before:
            rect = nav.rectangle()
            wheel_at((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2, turns=4)
            time.sleep(0.4)
            items = _nav_items(nav)
            pick = next((n for _, name, n in items if name == HOME_TAB_NAME), None)
        if pick is None and items:
            pick = items[0][2]
    return pick is not None and _press_button(pick)


def click_home_tab(main) -> bool:
    """点左侧导航的首页页签。

    先按名字「推荐」；找不到再按导航容器认（登录态首页页签显示账号昵称，
    实测 wmw001，按名字永远落空）。
    """
    try:
        items = main.descendants(control_type="ListItem")
    except Exception:
        items = []
    named = []
    for node in items:
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if name == HOME_TAB_NAME:
            return _press_button(node)
        named.append((node, name))
    nav = _nav_list_from(named)
    if nav is None or not _click_nav_home(nav):
        return False
    print("[env] 导航里按名字找不到「推荐」，已按导航容器点首页页签")
    return True


def _back_if_subpage(main) -> bool:
    """大厅会恢复上次的页面，点回首页。实测重装后首跑落在设置/关于页（有
    BackBtn），应用详情页连 BackBtn 都没有，所以多级兜底：aid=BackBtn →
    名叫「返回」的按钮 → 侧边栏首页页签。"""
    try:
        buttons = main.descendants(control_type="Button")
    except Exception:
        buttons = []
    for node in buttons:
        try:
            aid = node.element_info.automation_id or ""
            if aid == "BackBtn":
                return _press_button(node)
            name = popup_text(node.element_info.name or node.window_text())
            if name == "返回":
                return _press_button(node)
        except Exception:
            continue
    return click_home_tab(main)


def _structure(main, settings: LaunchSettings) -> tuple[set[str], bool]:
    """一次全树遍历同时收 AutomationId 集合和 webview 就绪标志。

    descendants() 全树遍历实测 1.4s 一次，就绪循环每轮原来要走两次
    （查 id 一次、查 webview 一次），合成一次省一半。
    """
    aids: set[str] = set()
    webview = False
    try:
        descendants = main.descendants()
    except Exception:
        return aids, webview
    for node in descendants:
        try:
            info = node.element_info
            aid = info.automation_id
            if aid:
                aids.add(aid)
            if not webview:
                name = popup_text(info.name)
                if name and any(key in name for key in settings.webview_ready_names if key):
                    webview = True
        except Exception:
            continue
    return aids, webview


def connect_app(timeout_sec: int) -> Application:
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        try:
            return Application(backend="uia").connect(path=EXE_NAME, timeout=3)
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise LaunchError(f"未能连接到 {EXE_NAME}: {last}")


def _largest_title_hwnd(pid: int, title_contains: str) -> int | None:
    """同 pid 下标题含 title_contains 的**可见**顶层窗里，面积最大的那个 hwnd。

    ## 为什么按面积（2026-10-09 节点实付）

    首跑时同 pid 下会有**多个**顶层窗标题都含「华硕大厅」（启动卡片窗 Win32 标题
    也叫华硕大厅，UIA Name 才叫「启动卡片窗口」）：4 个匹配时 pywinauto 的 title_re
    直接抛 ElementAmbiguousError（install 套件就死在这），按"第一个"取又可能抓到
    卡片窗。主窗口永远是面积最大的那个，按 Win32 rect 选，歧义和抓错窗一起解掉。

    ## 为什么还要按 UIA Name 排除卡片窗（同日本机实付）

    光按面积不够：**首跑那一瞬真主窗口还没可见**，屏幕上最大的「华硕大厅」就是
    那张「欢迎使用」卡片（它自己就是个大 Dialog）。本函数是**即时**解析（不像旧
    title_re 那样每次用才重解析），抓错就冻住了 —— 于是 ready 把真主窗口当成
    「未知弹窗」报出来。卡片窗 UIA Name 恒为「启动卡片窗口」，真主窗口登录前后是
    「华硕大厅」/「华硕应用商店」，按这个名字排除即可；排除后若只剩卡片，返回
    None 让调用方继续等真主窗口。
    """
    best, best_area = None, 0
    for hwnd in _top_hwnds():
        if not _hwnd_visible(hwnd) or _hwnd_pid(hwnd) != pid:
            continue
        if title_contains not in popup_text(_hwnd_text(hwnd)):
            continue
        try:
            uia_name = popup_text(UIAElementInfo(hwnd).name)
        except Exception:
            uia_name = ""
        if CARD_WINDOW_NAME_MARK in uia_name:
            continue
        left, top, right, bottom = _hwnd_rect(hwnd)
        area = max(0, (right - left) * (bottom - top))
        if area > best_area:
            best, best_area = hwnd, area
    return best


def main_window(app: Application, title_contains: str, timeout_sec: int):
    """等主窗口出现，返回**面积最大**的那个匹配窗的 concrete wrapper（见 _largest_title_hwnd）。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            pid = app.process
        except Exception:
            pid = None
        if pid:
            best = _largest_title_hwnd(pid, title_contains)
            if best is not None:
                return UIAWrapper(UIAElementInfo(best))
        time.sleep(1)
    raise LaunchError(f"未出现主窗口（标题含 {title_contains}）")


def concrete_main(pid: int, title_contains: str):
    """按 Win32 标题 + pid 重新包一个具体的主窗口 wrapper（面积最大者）。

    登录后主窗口的 UIA Name 从「华硕大厅」变成「华硕应用商店」（Win32 标题不变，
    2026-09-16 实测），main_window 那个按 title_re 懒解析的 WindowSpecification
    会一直 ElementNotFoundError。登录态下要操作主窗口（进「我的」等）必须换用本函数。
    """
    best = _largest_title_hwnd(pid, title_contains)
    if best is None:
        raise LaunchError(f"pid={pid} 名下找不到标题含 {title_contains!r} 的可见顶层窗")
    return UIAWrapper(UIAElementInfo(best))


def dismiss_pre_main_popups(app: Application, settings: LaunchSettings) -> bool:
    """首跑协议/权限页是独立顶层窗口（实测叫「权限确认窗口」），点完同意主窗口才出现，所以等主窗口之前就要关它。

    枚举走 Win32 按 pid 过滤（等主窗口时这个循环每秒都在跑，UIA 全系统枚举用不起）。
    """
    clicked = False
    try:
        pid = app.process
    except Exception:
        return False
    for hwnd in _top_hwnds():
        if not _hwnd_visible(hwnd) or _hwnd_pid(hwnd) != pid:
            continue
        win = _wrap_hwnd(hwnd)
        if win is None:
            continue
        name = popup_text(_hwnd_text(hwnd))
        if not name:
            try:
                name = popup_text(win.element_info.name)
            except Exception:
                continue
        if not name:
            continue
        first_run = not is_whitelisted(name, settings) and _looks_like_first_run(win)
        if not (is_whitelisted(name, settings) or first_run):
            continue
        if prefer_dismiss(name, settings) and not first_run:
            labels = settings.dismiss_buttons
        else:
            labels = settings.accept_buttons
        if _click_named_button(win, labels):
            clicked = True
            time.sleep(0.5)
    return clicked


def wait_main_window(app: Application, cfg: Config):
    """等主窗口出现；**出现之后还要确保它真的在屏幕上**。

    ## 为什么"找到主窗口"不够（2026-09-23 真机）

    **UIA 找得到隐藏 / 最小化的窗口** —— 所以这里返回成功，不等于窗口可见。
    而大厅主内容区是 **WebView2**，**窗口不可见时 Chromium 不渲染**：
    启动阶段一切正常（控件都读得到），后面依赖页面的断言却**永远等不到**，
    最后报成「列表没刷出来」这种**看着像产品缺陷**的错。

    真机表现：`test_sync_list_logged_in` 等 44s 后报「登录后同步页仍没刷出列表」，
    失败截图里**大厅整个不在屏幕上**（只有桌面 + 终端）。

    所以这里补一步 `ensure_window_shown`：窗口正常时它什么都不做（返回空串），
    只有最小化 / 隐藏时才还原，并把"修了什么"打到日志里（**不静默**）。
    """
    deadline = time.time() + cfg.timeouts.launch_sec
    last = None
    while time.time() < deadline:
        dismiss_pre_main_popups(app, cfg.launch)
        try:
            main = main_window(app, cfg.display_name_contains, 3)
        except LaunchError as exc:
            last = exc
            time.sleep(1)
            continue
        fixed = ensure_window_shown(int(getattr(main, "handle", 0) or 0))
        if fixed:
            print(f"[env] {fixed} —— WebView2 要窗口可见才渲染，否则页面断言会空等")
        return main
    raise LaunchError(f"未出现主窗口（标题含 {cfg.display_name_contains}）: {last}")


def start_fresh(cfg: Config) -> Application:
    info = read_installed()
    if info is None or not info.exe_path.is_file():
        raise LaunchError("未安装华硕大厅，无法启动")
    stop_main_process(cfg.timeouts.process_stop_sec)
    time.sleep(1)
    subprocess.Popen([str(info.exe_path)], cwd=str(info.exe_path.parent))
    return connect_app(cfg.timeouts.launch_sec)


def _occlusion_note(pid: int | None) -> str:
    """取「大厅是不是被挡住了」这一句依据；没被挡返回空串。

    ⚠️ **必须在 `_save_screenshot()` 之前调用** —— 截图会 `set_focus()` 把大厅提到前台，
    之后再量遮挡，量到的是**我们自己刚造出来的**"没被挡住"，真因当场被抹掉。
    2026-09-28 实付：同一条失败，先截图那轮报「窗口可见、未被遮挡」，
    先量再截图那轮才报出「被 WorkBuddy AI 挡住」。
    """
    if not pid:
        return ""
    return occlusion_hint(pid)


def _with_hint(message: str, hint: str) -> str:
    """把遮挡依据拼到结论后面 —— 这是「控件读得到、页面却空白」的头号真因。

    **结论必须跟依据一起给**，否则人只能在报错和截图之间反复猜（09-23 的老教训）。
    """
    return f"{message}｜遮挡诊断：{hint}" if hint else message


HOME_CONTENT_MARKERS = ("广告", "已选择")


def home_content_present(main) -> bool:
    """首页 WebView 是否**真有内容**，而不是只有一个空壳容器。

    ## 为什么结构就绪还不够（2026-10-09 节点实付）

    10:01 那轮 `wait_until_ready` 判定通过（SearchBarInput/TabList/MainWeb 齐全、
    「专题页」也在），截图里首页却**整块内容区纯白**：导航、搜索框都在，WebView2
    没渲染出任何磁贴/横幅。于是 wb 7 条用例在 0.4s 全红，报「左下角入口未找全: []」
    —— 看着像产品缺陷，其实是**就绪判据漏了"内容"这一维**。

    「专题页」是 WebView2 的**容器**名，空白时它照样在；只有真的渲染出内容才会有
    「广告」横幅 / 「…已选择」应用磁贴（见 reports/probe/ready_dump.json 健康态）。
    所以拿这两个当硬标记，容器不算。
    """
    try:
        nodes = main.descendants()
    except Exception:
        return False
    for node in nodes:
        try:
            name = popup_text(node.element_info.name)
        except Exception:
            continue
        if name and any(m in name for m in HOME_CONTENT_MARKERS):
            return True
    return False


def _looks_like_main_window(node, settings: LaunchSettings) -> bool:
    """覆盖层子树里齐了主结构 id（SearchBarInput/TabList/MainWeb）= 它才是真主窗口。

    主窗口句柄可能冻在**过渡窗**上：splash/启动卡片先出现、随即自毁，真主窗口后出现，
    就被顶层窗枚举当成「未知弹窗」。2026-10-09 节点 install 两轮实付（1323/1334）：
    报错里「弹窗文字」是整棵首页树（wmw001/折扣/壁纸…），`截图: None` = 原句柄已死。
    这种「未知弹窗」不能报，要把主窗口换绑过去继续等就绪。
    """
    aids: set[str] = set()
    try:
        nodes = node.descendants()
    except Exception:
        return False
    for child in nodes:
        try:
            aid = child.element_info.automation_id
        except Exception:
            continue
        if aid:
            aids.add(aid)
    return all(aid in aids for aid in settings.required_auto_ids)


def _rebind_in_place(dst, src) -> None:
    """把 `dst` 这个 wrapper **原地**改指向 `src` 那个窗口。

    pywinauto 的 `element_info` 是只读属性，真身是 `_element_info`；`handle`、
    `_as_parameter_`、`_cache` 都从它派生 —— 换窗时四个一起换，漏一个就会出现
    「名字是新窗、句柄是旧窗」的半新半旧 wrapper。
    """
    dst._element_info = src.element_info
    dst.handle = src.handle
    dst._as_parameter_ = src.handle
    dst._cache = {}


def wait_until_ready(cfg: Config, main) -> LaunchResult:
    settings = cfg.launch
    # handle / pid 是不变量，但 UIA 每次取要 ~1s（实测），就绪循环每轮都跑覆盖层
    # 扫描，循环外算一次传进去，别每轮重取。
    try:
        main_handle = main.handle
    except Exception:
        main_handle = None
    try:
        pid = main.element_info.process_id
    except Exception:
        pid = None
    deadline = time.time() + cfg.timeouts.ready_sec
    content_retried = False
    while time.time() < deadline:
        # 每轮只枚举一次覆盖层，白名单/未知判定复用同一份列表
        overlays = _overlay_windows(main, settings, main_handle=main_handle, pid=pid)
        dismiss_whitelist_popups(overlays, settings)
        unknown = [o for o in overlays if is_unknown(o, settings)]
        if unknown:
            real = next((o for o in unknown if _looks_like_main_window(o.node, settings)), None)
            if real is not None:
                # 换绑必须**写回调用方手里那个 wrapper**：夹具 yield 的就是它。
                # 只改本函数局部变量的话，调用方拿到的还是过渡窗 —— 2026-10-27 节点
                # 实付：搜索用例在「启动卡片窗口」里找 SearchBarInput，必然找不到。
                _rebind_in_place(main, real.node)
                try:
                    main_handle = main.handle
                except Exception:
                    main_handle = None
                print("[env] 主窗口句柄冻在过渡窗上，已换绑到真主窗口继续等就绪")
                time.sleep(0.5)
                continue
            # 依据先取：截图会 set_focus，把大厅提到前台后就量不出遮挡了
            hint = _occlusion_note(pid)
            # 弹窗**文字**必须带上 —— 名字（`Splash`）说明不了任何事，文字才是真因
            detail = "；".join(
                f"{o.name}（弹窗文字：{_overlay_text(o.node) or '读不到'}）" for o in unknown
            )
            evidence = _save_screenshot(main, "unknown_popup")
            raise LaunchError(
                _with_hint(f"未知弹窗: {detail}；截图: {evidence}", hint),
                evidence=evidence,
            )
        if overlays:
            time.sleep(0.5)
            continue
        present, webview = _structure(main, settings)
        click_first_run_agree(main, settings, present)
        missing = [aid for aid in settings.required_auto_ids if aid not in present]
        if missing and _back_if_subpage(main):
            time.sleep(0.5)
            continue
        if not missing and webview:
            if home_content_present(main):
                return LaunchResult(
                    title=popup_text(main.window_text()),
                    structure_ids=tuple(settings.required_auto_ids),
                )
            if not content_retried:
                content_retried = True
                print("[env] 首页结构就绪但内容空白，点一次首页页签强制重载")
                click_home_tab(main)
                time.sleep(1)
                continue
            hint = _occlusion_note(pid)
            evidence = _save_screenshot(main, "blank_home")
            raise LaunchError(
                _with_hint(f"首页内容空白 home_content=False；截图: {evidence}", hint),
                evidence=evidence,
            )
        time.sleep(0.5)
    hint = _occlusion_note(pid)
    evidence = _save_screenshot(main, "not_ready")
    present, web = _structure(main, settings)
    missing = [aid for aid in settings.required_auto_ids if aid not in present]
    leftover = [o.name for o in _overlay_windows(main, settings, main_handle=main_handle, pid=pid)]
    raise LaunchError(
        _with_hint(
            f"超时未就绪 missing={missing} webview={web} home_content={home_content_present(main)} "
            f"overlays={leftover}；截图: {evidence}",
            hint,
        ),
        evidence=evidence,
    )


def launch_until_ready(cfg: Config) -> LaunchResult:
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    result = wait_until_ready(cfg, main)
    if cfg.display_name_contains not in result.title:
        raise LaunchError(f"主窗口标题不是 {cfg.display_name_contains!r}: {result.title!r}")
    return result
