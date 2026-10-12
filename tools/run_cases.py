# -*- coding: utf-8 -*-
"""按**用例文件**执行 —— 2026-10-12 组长要的「脚本去找这个路径下的文件A，
然后执行文件A中的所有用例或者某一部分用例」。

    # 跑文件A里所有可跑的用例（默认排除人在环 manual 与还没自动化的 todo）
    .venv\\Scripts\\python.exe -X utf8 tools\\run_cases.py

    # 只列出会跑什么，不真跑（先看一眼最稳）
    .venv\\Scripts\\python.exe -X utf8 tools\\run_cases.py --list

    # 只跑指定的几条编号
    .venv\\Scripts\\python.exe -X utf8 tools\\run_cases.py --ids P0-03,P1C-03

    # 按套件 / 按状态挑
    .venv\\Scripts\\python.exe -X utf8 tools\\run_cases.py --suite login
    .venv\\Scripts\\python.exe -X utf8 tools\\run_cases.py --status xfail

    # 用例文件换个地方放（组长要的「放在文件夹某路径下」）
    .venv\\Scripts\\python.exe -X utf8 tools\\run_cases.py --cases "D:\\跑批\\用例\\文件A.yaml"
    # 或者长期生效：set HALL_CASES_FILE=D:\\跑批\\用例\\文件A.yaml

## 安全闸（**这些不是可选项**）

- `status: manual`（人在环：会**真发短信**、`input()` 会**永久阻塞**）——
  默认**不选**；要跑必须 `--allow-manual`，且要求当前是**交互式终端**，脚本自动加 `-s`。
  （不加 `-s` 时 pytest 把 `sys.stdin` 换成 `DontReadFromInput`，`isatty()` 恒假 →
  三条全 skip 还报「退出码 0」—— 2026-09-22 真机踩过，见 `hall_auto/suites.py:316`。）
- `status: destructive` 或套件属于 `install` / `apps-lifecycle`（**真装真卸**）——
  默认**不选**；要跑必须 `--allow-destructive` 或环境变量 `HALL_ALLOW_INSTALL=1`。
- `status: todo`（还没自动化）—— 永远跳过（没有代码可跑），只提示。

## 它和 `run_farm.py` / `suites.py` 的分工

- 本脚本 = **本机按用例挑着跑**（开发者/排障用）。
- `tools/run_farm.py` = **多机农场投任务**（走共享盘、提权通道、并发预算）。
  真装真卸与 20 台并发那套约束归农场管，本脚本**不做并发**。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto import cases as cases_db  # noqa: E402
from hall_auto.suites import pytest_basetemp  # noqa: E402

#: 默认不选的状态。`manual` 会真发短信/阻塞，`todo` 没代码。
DEFAULT_EXCLUDED = ("manual", "todo")

#: 这些套件跑起来是真装真卸，必须显式开闸。
DESTRUCTIVE_SUITES = {"install", "apps-lifecycle"}


def select_cases(
    all_cases: list[dict],
    *,
    ids: list[str] | None = None,
    suite: str | None = None,
    statuses: list[str] | None = None,
    exclude_statuses: tuple[str, ...] = DEFAULT_EXCLUDED,
    allow_manual: bool = False,
    allow_destructive: bool = False,
) -> tuple[list[dict], list[str]]:
    """挑出要跑的用例。返回 `(选中, 跳过说明)`。

    **纯函数**（不碰 pytest、不碰磁盘），所以策略可以被单测直接钉住 ——
    `tests/unit/test_run_cases_policy.py`。
    """
    skipped: list[str] = []
    idset = {i.strip() for i in (ids or []) if i.strip()}

    picked: list[dict] = []
    for c in all_cases:
        tag = c.get("id") or "?"
        if idset and tag not in idset:
            continue
        if suite and c.get("suite") != suite:
            continue
        st = c.get("status", "active")
        if statuses and st not in statuses:
            continue

        if not c.get("ref"):
            skipped.append(f"{tag}：status={st}，没有代码可跑（还没自动化）")
            continue
        if st in exclude_statuses:
            if st == "manual" and allow_manual:
                pass
            else:
                skipped.append(f"{tag}：status={st}，被排除（要跑请 --allow-manual）" if st == "manual"
                               else f"{tag}：status={st}，被排除")
                continue
        if st == "manual" and not allow_manual:
            skipped.append(f"{tag}：人在环，需要 --allow-manual")
            continue
        if st == "destructive" or c.get("suite") in DESTRUCTIVE_SUITES:
            if not allow_destructive:
                skipped.append(f"{tag}：真装真卸，需要 --allow-destructive（或 HALL_ALLOW_INSTALL=1）")
                continue
        picked.append(c)

    if idset:
        found = {c.get("id") for c in picked} | {s.split("：")[0] for s in skipped}
        for miss in sorted(idset - found):
            skipped.append(f"{miss}：用例文件里没有这个编号")
    return picked, skipped


def node_ids(picked: list[dict]) -> list[str]:
    """选中用例 → pytest 节点 id（去重、保序）。

    `tier: data` 的用例**只给函数级节点 id** —— 它的参数组由
    `tests/launch/conftest.py::pytest_generate_tests` 从用例文件注入，
    跑一次函数 = 跑它全部参数组。想只跑某一组，用 `-k` 自己加。
    """
    out: list[str] = []
    for c in picked:
        ref = c["ref"]
        if ref not in out:
            out.append(ref)
    return out


def build_argv(picked: list[dict], cases_path: Path, extra: list[str] | None = None) -> list[str]:
    argv = [*node_ids(picked), "-v", "-p", "no:cacheprovider"]
    if any(c.get("status") == "manual" for c in picked):
        argv.append("-s")           # 人在环必须带，理由见模块 docstring
    mode = os.environ.get("HALL_EVIDENCE", "").strip()
    if mode:
        argv.append(f"--evidence={mode}")
    argv.append(f"--basetemp={pytest_basetemp('cases', cases_path.stem)}")
    argv += extra or []
    return argv


def main() -> int:
    ap = argparse.ArgumentParser(description="按用例文件执行（组长 2026-10-12 要的那条）")
    ap.add_argument("--cases", default="", help=f"用例文件路径（默认取 {cases_db.CASES_ENV} 或 cases/cases.yaml）")
    ap.add_argument("--ids", default="", help="只跑这些编号，逗号分隔，如 P0-03,P1C-03")
    ap.add_argument("--suite", default="", help="只跑这个套件，如 login")
    ap.add_argument("--status", default="", help="只跑这些状态，逗号分隔，如 active,xfail")
    ap.add_argument("--exclude-status", default="", help="额外排除的状态，逗号分隔")
    ap.add_argument("--list", action="store_true", help="只列出会跑什么，不执行")
    ap.add_argument("--allow-manual", action="store_true", help="允许跑人在环（会真发短信，需交互式终端）")
    ap.add_argument("--allow-destructive", action="store_true", help="允许跑真装真卸（install / apps-lifecycle）")
    ap.add_argument("--extra", default="", help="透传给 pytest 的额外参数，如 \"-k mismatch\"")
    args = ap.parse_args()

    cases_path = cases_db.resolve_path(args.cases or None)
    print(f"[用例文件] {cases_path}")
    if not cases_path.is_file():
        print(f"[FAIL] 这个路径下没有用例文件。用 --cases 指定，或设 {cases_db.CASES_ENV}。")
        return 2
    src = f"--cases" if args.cases else (cases_db.CASES_ENV if os.environ.get(cases_db.CASES_ENV) else "默认")
    print(f"[来源] {src}")

    try:
        all_cases = cases_db.all_cases(cases_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 2

    allow_destructive = args.allow_destructive or os.environ.get("HALL_ALLOW_INSTALL", "").strip() == "1"
    picked, skipped = select_cases(
        all_cases,
        ids=[s for s in args.ids.split(",") if s.strip()],
        suite=args.suite or None,
        statuses=[s.strip() for s in args.status.split(",") if s.strip()] or None,
        exclude_statuses=DEFAULT_EXCLUDED + tuple(
            s.strip() for s in args.exclude_status.split(",") if s.strip()),
        allow_manual=args.allow_manual,
        allow_destructive=allow_destructive,
    )

    print(f"[选中] {len(picked)} 条 / 文件里共 {len(all_cases)} 条")
    for c in picked:
        print(f"       {c['id']:8} {c.get('suite', ''):14} {c.get('status', ''):12} {c.get('title', '')}")
    if skipped:
        print(f"[跳过] {len(skipped)} 条：")
        for s in skipped:
            print(f"       {s}")

    if not picked:
        print("[END] 没有可跑的用例（都没选中，或全被安全闸挡了）")
        return 0

    if args.allow_manual and not sys.stdin.isatty():
        print("[FAIL] --allow-manual 需要**交互式终端**：人在环用例要人工收短信码。")
        print("       在测试机自己的终端窗口里跑，别在 CI / 重定向管道里跑。")
        return 2

    argv = build_argv(picked, cases_path, extra=[args.extra] if args.extra else None)
    print(f"[pytest] {' '.join(argv)}")
    if args.list:
        print("[END] --list：只列出，没有执行。")
        return 0

    print("-" * 72)
    proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", *argv], cwd=REPO_ROOT)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
