# -*- coding: utf-8 -*-
"""用例清单导出器 —— **只读** `cases/cases.yaml`，从不写回。

    python -X utf8 tools/cases_export.py --format md            # 出 cases/清单.md（进 git，供 --check 对账）
    python -X utf8 tools/cases_export.py --format csv           # 出宽表 CSV（Excel 双击就能看）
    python -X utf8 tools/cases_export.py --format zentao-json   # 出禅道导入用的 cases.json
    python -X utf8 tools/cases_export.py --check                # 校验 + 对账，rc!=0 表示要重出

设计约定（见《用例数据化方案.md》）：

- **YAML 是唯一真源**。本工具与守卫都只读它；改用例只改 YAML。
- **导出物是派生物**。`cases/清单.md` 进 git 是为了「不开工具也能看、能 diff」，
  由 `--check` 保证它不过期 —— 跟发放版 `sync_handbook.py --check` 是同一个套路。
- **不引入新依赖**：本机没有 openpyxl，所以不做 xlsx；CSV 用 utf-8-sig，Excel 直接双击可开。
- **禅道那条不在这里做格式**：只产出 `cases.json`（中立 schema），
  12 列格式由 `禅道用例CSV` skill 负责 —— 格式只留一个权威来源。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CASES_YAML = REPO / "cases" / "cases.yaml"
MD_OUT = REPO / "cases" / "清单.md"

VALID_TIER = {"code", "data"}
VALID_KIND = {"case", "probe"}
# todo = 还没自动化、先登记着（此时 ref 允许为空）
VALID_STATUS = {"active", "manual", "xfail", "destructive", "todo"}

# 导出时的套件顺序（跟 suites.py 的 FULL_RUN_SUITES 语义一致：先只读、破坏性的在后）
SUITE_ORDER = [
    "unit", "launch", "apps-detect", "probe", "login", "settings", "security", "wb",
    "apps-lifecycle", "install", "login-manual",
]

SUITE_LABEL = {
    "launch": "P0 —— 安装 / 启动 / 搜索",
    "install": "P0 —— 安装 / 升级（破坏性）",
    "apps-detect": "P1-A —— 应用生命周期（检测）",
    "apps-lifecycle": "P1-A —— 应用生命周期（真装真卸）",
    "login": "P1-B / P1-C —— 登录 / 注册",
    "login-manual": "P1-B —— 登录（人在环）",
    "settings": "P1-D —— 设置",
    "security": "P2 —— 安全验证",
    "wb": "P2 —— WorkBuddy 入口",
    "probe": "探针（不是业务用例）",
    "未归属": "还没排套件（还没自动化）",
}


def load() -> dict:
    import yaml

    if not CASES_YAML.is_file():
        raise SystemExit(f"[FAIL] 找不到真源 {CASES_YAML}")
    doc = yaml.safe_load(CASES_YAML.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise SystemExit("[FAIL] cases.yaml 结构不对：顶层要有 meta / cases")
    return doc


def validate(doc: dict) -> list[str]:
    """schema 校验。返回问题列表，空 = 通过。"""
    from hall_auto.suites import SUITES

    problems: list[str] = []
    seen: dict[str, int] = {}
    for i, c in enumerate(doc["cases"]):
        tag = c.get("id") or f"第 {i + 1} 条"
        # status=todo 是「还没自动化、先登记着」：ref 与 suite 都允许为空
        required = ("id", "title", "verify")
        if c.get("status") != "todo":
            required += ("ref", "suite")
        for key in required:
            if not c.get(key):
                problems.append(f"{tag}: 缺字段 {key}")
        if c.get("id"):
            if c["id"] in seen:
                problems.append(f"{tag}: id 与第 {seen[c['id']]} 条重复")
            seen[c["id"]] = i + 1
        if c.get("tier") not in VALID_TIER:
            problems.append(f"{tag}: tier 必须是 {sorted(VALID_TIER)}")
        if c.get("kind") not in VALID_KIND:
            problems.append(f"{tag}: kind 必须是 {sorted(VALID_KIND)}")
        if c.get("status") not in VALID_STATUS:
            problems.append(f"{tag}: status 必须是 {sorted(VALID_STATUS)}")
        if not isinstance(c.get("marker"), list) or (not c["marker"] and c.get("status") != "todo"):
            problems.append(f"{tag}: marker 必须是非空列表（status=todo 可空）")
        suite = c.get("suite")
        if suite and suite not in SUITES:
            problems.append(f"{tag}: suite={suite!r} 不在 hall_auto/suites.py 里")
        elif suite:
            rel = (c.get("ref") or "").split("::")[0]
            allowed = [p for p in SUITES[suite].paths]
            if rel and not any(rel == a or rel.startswith(a.rstrip("/") + "/") for a in allowed):
                problems.append(
                    f"{tag}: ref 文件 {rel} 不在 suite={suite} 的 paths {allowed} 里"
                )
        if c.get("tier") == "data" and not c.get("params") and not c.get("driver"):
            problems.append(f"{tag}: tier=data 必须有非空 params 或 driver")
        if c.get("driver"):
            from hall_auto import drivers as _drv

            if c["driver"] not in _drv.DRIVERS:
                problems.append(
                    f"{tag}: driver={c['driver']!r} 没在 hall_auto/drivers.py 注册"
                    f"（可用：{sorted(_drv.DRIVERS)}）"
                )
            elif c["driver"] == "controls_present" and not c.get("page"):
                problems.append(f"{tag}: controls_present 需要 page 字段")
            elif c["driver"] == "radio_group_exactly_one" and not c.get("group"):
                problems.append(f"{tag}: radio_group_exactly_one 需要非空 group 字段")
            elif c["driver"] == "controls_present" and c.get("page") not in _drv.PAGES:
                problems.append(
                    f"{tag}: page={c.get('page')!r} 没在 hall_auto/drivers.py 的 PAGES 里"
                    f"（可用：{sorted(_drv.PAGES)}）"
                )
    return problems


def _visible(doc: dict, include_probe: bool) -> list[dict]:
    return [c for c in doc["cases"] if include_probe or c.get("kind") != "probe"]


def render_md(doc: dict, include_probe: bool = False) -> str:
    rows = _visible(doc, include_probe)
    lines = [
        "# 华硕大厅客户端 UI 自动化 —— 测试用例清单",
        "",
        f"> 最后更新：{doc['meta'].get('最后更新', '')}",
        "> ⚠️ **这是导出物，别直接改。** 真源是 `cases/cases.yaml`；",
        "> 改完重跑 `tools/cases_export.py --format md`。守卫 `test_cases_inventory.py` 会检查它是否过期。",
        "",
        f"共 **{len(rows)}** 条"
        + ("" if include_probe else "（不含探针）")
        + "。",
        "",
    ]
    by_suite: dict[str, list[dict]] = {}
    for c in rows:
        by_suite.setdefault(c["suite"] or "未归属", []).append(c)
    order = ([s for s in SUITE_ORDER if s in by_suite]
             + [s for s in by_suite if s not in SUITE_ORDER])
    for suite in order:
        label = SUITE_LABEL.get(suite, suite)
        lines.append(f"## {label}" if suite == "未归属" else f"## {label}（`{suite}`）")
        lines.append("")
        lines.append("| 编号 | 用例 | 具体验证什么 | 状态 |")
        lines.append("| --- | --- | --- | --- |")
        for c in by_suite[suite]:
            status = "" if c["status"] == "active" else f"`{c['status']}`"
            lines.append(f"| {c['id']} | {c['title']} | {c['verify']} | {status} |")
        lines.append("")
    return "\n".join(lines)


def render_csv(doc: dict, include_probe: bool = False) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["编号", "用例", "所属套件", "具体验证什么", "状态", "运行前提", "代码位置", "备注"])
    for c in _visible(doc, include_probe):
        w.writerow([c["id"], c["title"], c["suite"] or "", c["verify"], c["status"],
                    c.get("needs", ""), c.get("ref", ""), c.get("notes", "")])
    return buf.getvalue()


def render_zentao_json(doc: dict, include_probe: bool = False) -> str:
    """产出 `禅道用例CSV` skill 的输入 schema。

    ⚠️ 自动化用例**没有人类步骤** —— 这里用 `verify` 兜底生成一条步骤，
    所以是**骨架**，导入禅道前必须人工补真实步骤。别把它当成品发出去。
    """
    out = []
    for c in _visible(doc, include_probe):
        out.append({
            "module": SUITE_LABEL.get(c["suite"], c["suite"] or "未归属"),
            "keyword": c["title"],
            "priority": 1 if c["id"].startswith("P0") else (2 if c["id"].startswith("P1") else 3),
            "name": f"{c['id']} {c['title']}",
            "precondition": c.get("needs", "") or "本机已装华硕大厅",
            "steps": [f"执行自动化用例 {c.get('ref') or '（尚未自动化）'}（或按「{c['title']}」手工复现）"],
            "expects": [c["verify"]],
        })
    return json.dumps(out, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="用例清单导出器（只读 cases/cases.yaml）")
    ap.add_argument("--format", default="md", choices=["md", "csv", "zentao-json"])
    ap.add_argument("--out", default="", help="输出路径；默认：md -> cases/清单.md，其余 -> 当前目录")
    ap.add_argument("--check", action="store_true", help="只校验 + 对账，不写文件")
    ap.add_argument("--include-probe", action="store_true", help="把只读探针也列进去")
    args = ap.parse_args()

    doc = load()
    problems = validate(doc)
    if problems:
        print(f"[FAIL] cases.yaml 有 {len(problems)} 处问题：")
        for p in problems:
            print(f"       - {p}")
        return 1

    rows = _visible(doc, args.include_probe)
    print(f"[OK] cases.yaml 校验通过：{len(doc['cases'])} 条（导出 {len(rows)} 条）")

    if args.check:
        want = render_md(doc, args.include_probe)
        if not MD_OUT.is_file():
            print(f"[FAIL] {MD_OUT.name} 不存在 —— 跑 --format md 生成")
            return 1
        got = MD_OUT.read_text(encoding="utf-8")
        if got != want:
            print(f"[FAIL] {MD_OUT.name} 与真源不一致（改过 cases.yaml 没重出？）")
            return 1
        print(f"[OK] {MD_OUT.name} 与真源一致")
        return 0

    if args.format == "md":
        target = Path(args.out) if args.out else MD_OUT
        text = render_md(doc, args.include_probe)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"[OK] 已生成 {target}（{target.stat().st_size} 字节）")
    elif args.format == "csv":
        target = Path(args.out) if args.out else Path.cwd() / "测试清单.csv"
        # ⚠️ **必须带 newline=""** —— `csv.writer` 自己就输出 `\r\n`，而 `write_text` 默认会把
        # `\n` 再翻成 `\r\n`，于是文件里变成 **`\r\r\n`**：Excel 打开是一堆空行，
        # `csv.reader` 也会把每行读成两行（2026-10-12 实测：39 条读成 79 条）。
        # 同理不能省 `encoding="utf-8-sig"` —— 少了 BOM，Excel 打开中文是乱码。
        target.write_text(render_csv(doc, args.include_probe), encoding="utf-8-sig", newline="")
        print(f"[OK] 已生成 {target}（utf-8-sig + 无多余换行，Excel 可直接双击）")
    else:
        target = Path(args.out) if args.out else Path.cwd() / "cases.json"
        target.write_text(render_zentao_json(doc, args.include_probe), encoding="utf-8")
        print(f"[OK] 已生成 {target}")
        print("     ⚠️ 这是**骨架**：自动化用例没有人类步骤，steps 由 verify 兜底。")
        print("        导禅道前请人工补步骤；12 列 CSV 交给 `禅道用例CSV` skill 生成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
