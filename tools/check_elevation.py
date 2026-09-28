"""提权段诊断：一条命令说清「提权侧到底跑没跑」。

## 为什么要这个脚本

提权通道是 **broker**：`farm_agent` 写 `reports/_elev/request.json` →
`schtasks /Run /TN HallAutoP1` → 提权侧读请求、跑 pytest、写 `result.json`。

问题在于 **`schtasks /Run` 返回 0 只代表"任务被启动了"，不代表"事情办了"**。
任务被启动了却什么都没发生是完全可能的（动作路径失效、python 起不来、
请求文件缺失、被幂等分支静默 return 0）—— 而这些情况的共同表现是：

    控制机：日志停在投任务那一行，之后零信号
    节点：  「…提权段 install 进行中（已 Ns）」一直打，**永不结束**

两者**与节点死机同形**，现场只能干等到 `ELEVATION_TIMEOUT_SECONDS`（4 小时）。
2026-09-28 实测：新机首投 `install`，65 分钟一直"进行中"。

本脚本把「提权侧从不写结果」的几条分支逐条判掉，直接给结论 + 下一步看哪。

## 用法

    .\\.venv\\Scripts\\python.exe -X utf8 tools\\check_elevation.py

**在节点上跑**（`reports/_elev/` 是节点本机目录，控制机上看不到）。
退出码：0 = 结论已给出（**不代表提权段成功**，看输出）；1 = 仓库路径都不对。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.elevation import (  # noqa: E402
    ELEVATION_TASK_NAME,
    log_path,
    read_json,
    request_path,
    result_path,
)

WRAPPER_NAME = "wrapper.log"


def _wrapper_path() -> Path:
    return log_path().parent / WRAPPER_NAME


def _describe_file(path: Path) -> str:
    if not path.is_file():
        return "不存在"
    stat = path.stat()
    age_min = (datetime.now() - datetime.fromtimestamp(stat.st_mtime)).total_seconds() / 60
    return f"存在（{stat.st_size} 字节，最后写入 {age_min:.0f} 分钟前）"


def _tail(path: Path, lines: int = 25) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读不动：{exc}）"
    body = text.splitlines()[-lines:]
    return "\n".join(body)


def _task_action() -> tuple[str, str]:
    """查计划任务的动作。返回 (状态, 说明)，状态 ∈ `present` / `absent` / `unknown`。

    **三态必须分开**：把「查不了」说成「不存在」会把人送去重跑 bootstrap，
    而真因可能是权限不够、或 `schtasks` 被安全策略挡了（本项目实测过：
    代理会话里 `schtasks.exe` 被命令安全拦掉，报的是 `WinError 5 拒绝访问`）。
    误诊的代价是白跑一趟、还顺手把已存在的任务触发时间重置了。

    **必须查 XML**：`schtasks /Query /TN x` 的表格输出在中文系统上列宽会错位，
    而 XML 里 `Command` / `Arguments` 是干净的 —— 我们要看的正是那两行。
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", ELEVATION_TASK_NAME, "/XML"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        return "unknown", f"调不起 schtasks（{exc}）"
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        # 真「没有这个任务」与「查不动」在中文/英文系统上的措辞不同，但都含关键词。
        low = text.lower()
        if "找不到" in text or "cannot find" in low or "does not exist" in low:
            return "absent", text[:300]
        return "unknown", f"schtasks 退出码 {proc.returncode}：{text[:300]}"
    xml = proc.stdout.replace("&amp;", "&")
    keep = [
        line.strip()
        for line in xml.splitlines()
        if "<Command>" in line or "<Arguments>" in line or "<UserId>" in line
    ]
    return "present", "\n".join(keep) if keep else "（XML 里没找到 Command/Arguments）"


def main() -> int:
    elev_dir = log_path().parent
    print("=" * 72)
    print("提权段诊断")
    print("=" * 72)
    print(f"仓库根        {REPO_ROOT}")
    venv_py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    print(f"venv python   {venv_py}  [{'存在' if venv_py.is_file() else '缺失 —— wrapper 会退化成裸 python'}]")
    print(f"提权目录      {elev_dir}  [{'存在' if elev_dir.is_dir() else '不存在 —— 提权段从未在这台机器上跑过'}]")
    print()

    req_path = request_path()
    res_path = result_path()
    log = log_path()
    wrapper = _wrapper_path()

    request = read_json(req_path)
    result = read_json(res_path)

    print("--- 协议文件 ---")
    print(f"request.json      {_describe_file(req_path)}")
    print(f"result.json       {_describe_file(res_path)}")
    print(f"{WRAPPER_NAME:<17} {_describe_file(wrapper)}")
    print(f"elevated_run.log  {_describe_file(log)}")
    print()

    req_id = str(request.get("request_id") or "")
    res_id = str(result.get("request_id") or "")
    if req_id:
        print(f"请求 request_id   {req_id}")
        print(f"          suite   {request.get('suite')}  "
              f"created_at={request.get('created_at')}")
    if res_id:
        print(f"结果 request_id   {res_id}"
              f"{'  ← 与请求一致' if res_id == req_id else '  ← **与请求不一致（这是上一轮的结果）**'}")
        print(f"          exit_code {result.get('exit_code')}  note={result.get('note') or '（无）'}")
    print()

    print(f"--- 计划任务 {ELEVATION_TASK_NAME} ---")
    task_status, detail = _task_action()
    if task_status == "present":
        print("[存在] 动作：")
        for line in detail.splitlines():
            print(f"    {line}")
        if "run_elevated_suite.ps1" not in detail:
            print("    ⚠ 动作**不是** run_elevated_suite.ps1 —— 大概率指向旧脚本，")
            print("      触发它只会跑老流程，`install` 永远跑不到。用管理员重跑 bootstrap 重建。")
        elif str(REPO_ROOT) not in detail:
            print("    ⚠ 动作里的仓库路径**不是当前这个仓库** —— 任务烧的是 bootstrap 当时的")
            print("      绝对路径；仓库挪过位置就触发不到本目录。用管理员重跑 bootstrap 重建。")
    elif task_status == "absent":
        print("[不存在] 任务真的没建。")
        print("    ⚠ 提权通道没建 = 触发它什么都不会发生，且**节点侧一句报错都没有**。")
        print("      用管理员重跑 tools\\bootstrap_machine.ps1（幂等）。")
    else:
        print(f"[查不到] {detail}")
        print("    ⚠ **这不等于任务不存在**：可能是权限不够、或被安全策略挡住了 schtasks。")
        print("      换个**管理员窗口**重跑本脚本再看一次，别据此就去重建任务。")
    print()

    print("--- 结论 ---")
    print(_verdict(req_id, res_id, wrapper, log, task_status))
    print()

    for label, path in (("wrapper.log", wrapper), ("elevated_run.log", log)):
        body = _tail(path)
        if body:
            print(f"--- {label} 末 25 行 ---")
            print(body)
            print()
    return 0


def _verdict(
    req_id: str, res_id: str, wrapper: Path, log: Path, task_status: str
) -> str:
    if not req_id:
        return (
            "· request.json 不在（或字段不全）—— agent 还没走到提权段，"
            "或者这一轮的 `_elev` 已被清理。若此刻正「进行中」，说明请求文件没写成功。"
        )
    if res_id and res_id == req_id:
        return "✅ 本轮结果已落盘（看上面 exit_code）。提权段是跑完了的。"
    if res_id and res_id != req_id:
        return (
            "✗ result.json 是**上一轮**的、本轮没回。提权侧这一轮没写结果 —— "
            "按下面的分支查（先看 wrapper.log 在不在）。"
        )
    if log.is_file():
        age_min = (datetime.now() - datetime.fromtimestamp(log.stat().st_mtime)).total_seconds() / 60
        if age_min >= 5:
            return (
                f"⚠ 提权侧**跑起来了但卡住**：elevated_run.log 存在，"
                f"但 {age_min:.0f} 分钟没有新内容。看它末尾停在哪一步 ——"
                "若停在装包/取包，多半是 NSIS 或共享盘拷贝挂住了。"
            )
        return "· 提权侧正在跑（elevated_run.log 在增长）。再等，别重复触发。"
    # 到这里：请求写了、没结果、连 elevated_run.log 都没有 —— 提权侧的 pytest 压根没起来。
    # **产物优先于计划任务查询**：wrapper.log 由 .ps1 自己建，它能把
    # 「python 起不来」和「连 .ps1 都没跑」分开，比查任务动作更直接。
    # （早先先判 task_status，结果 `schtasks` 一被安全策略挡住就短路成"判不出来"，
    #   明明产物已经把答案写在那儿了。）
    if wrapper.is_file():
        return (
            "✗ wrapper 跑了但没写结果：看上面 wrapper.log 末几行 —— 多半是 python 起不来\n"
            "  （venv 缺失 + PATH 里没有裸 python），或 elevated_runner 走了「没有待执行的\n"
            "  提权请求」/「已经跑过」那两条静默 return 0 的分支。"
        )
    if task_status == "absent":
        return "✗ 计划任务不在 → 触发无效（且节点侧不报错）。用管理员重跑 tools\\bootstrap_machine.ps1。"
    if task_status == "unknown":
        return (
            "? 判不出来：`_elev` 下连 wrapper.log 都没有，且计划任务查不动（权限 / 被策略挡）。\n"
            "  先用**管理员窗口**重跑本脚本拿到任务动作；仍拿不到就手工\n"
            "  `schtasks /Run /TN " + ELEVATION_TASK_NAME + "` 再回来看有没有 wrapper.log。"
        )
    return (
        "✗ 提权侧**从未跑起来**：请求写了，但 wrapper.log / elevated_run.log / result.json\n"
        "  一个都没有 —— 计划任务被触发后在**启动 python 之前**就失败了（或压根没跑）。\n"
        "  查：① 上面计划任务的动作路径对不对（绝对路径是 bootstrap 当时烧进去的）；\n"
        "      ② 手工 `schtasks /Run /TN " + ELEVATION_TASK_NAME + "` 再回来看有没有 wrapper.log；\n"
        "      ③ 任务历史：事件查看器 → 任务计划程序 → 任务计划程序服务。"
    )


if __name__ == "__main__":
    raise SystemExit(main())
