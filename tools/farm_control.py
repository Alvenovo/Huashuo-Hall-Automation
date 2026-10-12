"""多机跑批：控制机侧（投任务 / 收结果 / 汇总矩阵）。

配套 `tools/farm_agent.py`（节点侧）。控制机这边只做三件事，保持薄：

    dispatch  按套件并发预算生成任务文件，投到 farm_root/tasks/<node>.json
    status    看哪些节点还没交活
    aggregate 把 results/ 下各节点的 summary.json 汇总成一张「节点 × 用例」矩阵报告
              顺带写 `results/<节点>/index.html`（目录名 ↔ 套件 ↔ 结果）和 `汇总/` 归档副本
    index     只写各节点的轮次索引，不出矩阵报告（想快速对目录时用这个，快得多）

## 用法

    # 投任务：把 launch / apps-detect / security 摊到 8 台机器
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_control.py dispatch ^
        --nodes R01,R02,R03,R04,R05,R06,R07,R08 --suites launch,apps-detect,security

    # 看进度
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_control.py status

    # 汇总
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_control.py aggregate

需要 `HALL_FARM_ROOT` 指向共享盘农场目录。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.env_pack import NODE_ENV_EVIDENCE_NAME  # noqa: E402
from hall_auto.suites import FULL_RUN_KEYWORDS, SUITES, get_suite, resolve_suite_names  # noqa: E402


def farm_root() -> Path:
    """农场目录：环境变量 > config.local.yaml > 硬退出。

    控制机通常就是办公机或包源机，也可能跑过 bootstrap 因此配置里有 farm_root。
    环境变量优先是为了临时切到别的农场做验证。
    """
    raw = os.environ.get("HALL_FARM_ROOT", "").strip()
    if not raw:
        try:
            from hall_auto.config import load_config

            raw = str(load_config().farm_root or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raise SystemExit(
            "未设 HALL_FARM_ROOT，config.local.yaml 里也没有 farm_root。\n"
            "例：$env:HALL_FARM_ROOT='\\\\SHARE\\qa\\hall-farm'"
        )
    return Path(raw)


def cmd_dispatch(args) -> int:
    root = farm_root()
    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    suite_names = resolve_suite_names(args.suites)
    if not nodes or not suite_names:
        raise SystemExit("--nodes 与 --suites 都不能为空")

    # 全量档展开必须打出来：否则「我投了 all」和「实际投了哪几个」之间没有凭据 ——
    # 以后往全量档里加套件，事后没人能还原这次到底投了什么。
    if args.suites.strip().lower() in FULL_RUN_KEYWORDS:
        print(f"--suites {args.suites.strip()} → 全量档 {len(suite_names)} 个：{', '.join(suite_names)}")

    # 人在环套件（manual）必须挡在农场外：它在节点上会卡在 input() 等验证码/新密码，
    # agent 一直不返回、整台机器再也取不到下一个任务，而控制机这边只看到"执行中"。
    # 手册明确承诺 login-manual「永不进农场」，这里就得真的拒投，不能只打印一句警告。
    if not getattr(args, "allow_manual", False):
        manual = [n for n in suite_names if not get_suite(n).farm_safe]
        if manual:
            raise SystemExit(
                f"套件 {', '.join(manual)} 是人在环（要真人守着输短信验证码 / 改密码），"
                "不能投进无人值守农场：\n"
                "  它在节点上会卡在等输入，agent 一直不返回，那台机器再也取不到新任务。\n"
                "  请在**那台机器的交互式终端**里手工跑：\n"
                f"    .\\.venv\\Scripts\\python.exe -X utf8 tools\\farm_agent.py --local {manual[0]}\n"
                "  确实要投（例如你正远程盯着那台机器），加 --allow-manual 显式放行。"
            )

    for name in suite_names:
        get_suite(name)  # 早报错，别投出去才发现名字写错

    task_id = args.task_id or f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "done").mkdir(parents=True, exist_ok=True)

    # 按并发预算把套件摊到节点上（plan_shards 的口径见 hall_auto/suites.py）
    per_node: dict[str, list[dict]] = {n: [] for n in nodes}
    for name in suite_names:
        suite = get_suite(name)
        if suite.parallel == "serial":
            target, sid, scount = nodes[0], 1, 1
        elif suite.parallel == "full":
            # 不限并发：每台跑完整套件
            for node in nodes:
                per_node[node].append({"name": name, "shard_id": 1, "shard_count": 1})
            continue
        else:
            limit = min(suite.concurrency(), len(nodes))
            for idx, node in enumerate(nodes):
                per_node[node].append({"name": name, "shard_id": idx % limit + 1, "shard_count": limit})
            continue
        per_node[target].append({"name": name, "shard_id": sid, "shard_count": scount})

    written = 0
    for node, suites in per_node.items():
        if not suites:
            continue
        payload = {
            "task_id": task_id,
            "node": node,
            "suites": suites,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 破坏性套件统一由节点侧环境变量控制，这里显式带上便于审计
        if any(s["name"] in ("install", "apps-lifecycle") for s in suites):
            payload["env"] = {"HALL_ALLOW_INSTALL": "1"}
        out = root / "tasks" / f"{node}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
        print(f"{node}: {len(suites)} 个套件 -> {out.name}")

    print(f"\n任务 {task_id} 已投给 {written} 台节点（共 {len(nodes)} 台）。")
    _print_concurrency_notes(suite_names)
    _warn_missing_credentials(suite_names)
    return 0


# 这些套件靠凭据才能真跑；节点上没配 → 用例静默 skip（报告一片黄）。
# 控制机这边看不出来，所以投任务时就提醒一次，别等汇总时才发现。
_CRED_REQUIRED = {
    "login": "HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_MS_USER",
    "login-manual": "HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_TEST_NEW_PASSWORD",
}


def _warn_missing_credentials(suite_names: list[str]) -> None:
    """投了需要凭据的套件就提醒：凭据在**节点本地** farm_node.env，不是在这里设。"""
    needed = sorted({name for name in suite_names if name in _CRED_REQUIRED})
    if not needed:
        return
    print("\n⚠ 凭据提醒：")
    for name in needed:
        print(f"  {name} 需要节点本地 farm_node.env 里有：{_CRED_REQUIRED[name]}")
    print("  （密码不落共享盘、不进任务文件；每台节点机各自仓库根下的 farm_node.env 已 gitignore）")
    print("  没配的节点上，这些用例会静默 skip —— 汇总表「节点环境」里的凭据列能区分：")
    print("    ✗ = 这台机器没配凭据（去填 farm_node.env）；✓ = 凭据配了，是用例本身在跳过。")


def _print_concurrency_notes(suite_names: list[str]) -> None:
    """把并发预算摊开给人看：这是防服务端被打爆的关键参数，别悄悄生效。"""
    print("并发预算：")
    for name in suite_names:
        suite = get_suite(name)
        if suite.parallel == "full":
            note = "全并行（不碰服务端）"
        elif suite.parallel == "serial":
            note = "全局串行（同时只允许 1 台）"
        else:
            note = f"限流 ≤{suite.concurrency()} 台（打服务端 / 厂商 CDN）"
        flag = "" if suite.farm_safe else "  ⚠ 人在环，不应进农场"
        print(f"  {name:16} {note}{flag}")


def _elapsed_minutes(created_at: object) -> int | None:
    """投出到现在的分钟数。读不出返回 None（**不许瞎猜成 0** —— 0 看起来像"刚投出去"）。"""
    try:
        started = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return None
    return int((datetime.now() - started).total_seconds() // 60)


def _describe_running(path: Path) -> list[str]:
    """把「执行中」的那个任务摊开：**是什么、投出去多久了、要不要等**。

    为什么不能只打文件名：控制机上唯一能看到的信号就是这里。早先只打
    `LAPTOP-68TGB8VB.json.running`，于是**「已经跑了 59 分钟」与「刚投出去 3 秒」
    长得一模一样** —— 而这两者该做的处置完全相反（一个得去节点看，一个就该等）。
    2026-09-28 实测就是这么干等的：投 `install` 后 59 分钟，`status` 只说"执行中 1 个"。
    """
    data = _read_json(path)
    if not data:
        return [f"  执行中: {path.name}（内容读不动 —— 可能正被节点改名，稍后再看）"]

    task_id = str(data.get("task_id") or "")
    names = [
        str(s.get("name") or "")
        for s in (data.get("suites") or [])
        if isinstance(s, dict) and s.get("name")
    ]

    head = f"  执行中: {path.name}"
    if names:
        head += f" | 套件 {'/'.join(names)}"
    if task_id:
        head += f" | 任务 {task_id}"
    out = [head]

    created = str(data.get("created_at") or "")
    elapsed = _elapsed_minutes(created)
    if elapsed is None:
        out.append(f"     投出时间：{created or '读不出'}（算不出已跑多久）")
    else:
        out.append(f"     投出 {created.replace('T', ' ')}，已跑 {elapsed} 分钟")

    elevated = []
    for name in names:
        try:
            if get_suite(name).needs_elevation:
                elevated.append(name)
        except KeyError:
            pass
    if elevated:
        out.append(
            f"     ⚠ 含提权套件（{'/'.join(elevated)}）：由节点侧自己跑，最长静默等 4 小时；"
            r"真因在【节点本机】reports\_elev\elevated_run.log，控制机看不到"
        )
    return out


def cmd_status(args) -> int:
    root = farm_root()
    tasks = sorted((root / "tasks").glob("*.json"))
    dones = sorted((root / "done").glob("*.json"))
    running = sorted((root / "tasks").glob("*.running"))
    print(f"待执行 {len(tasks)} 个，执行中 {len(running)} 个，已完成 {len(dones)} 个")
    for f in tasks:
        print(f"  待执行: {f.name}")
    for f in running:
        for line in _describe_running(f):
            print(line)
    for f in dones[-10:]:
        print(f"  已完成: {f.name}")
    return 0


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _node_creds_from_receipts(root: Path) -> dict[str, dict]:
    """兜底：从 `done/` 回执里取各节点最近一次的凭据状态（按文件修改时间取最新）。

    只在证据目录里没有 `node_env.json` 时才用得上 —— 比如早先版本节点跑出来的结果。
    正常情况下 `node_env.json` 与本次运行严格对应，优先用它。
    """
    out: dict[str, dict] = {}
    done_dir = root / "done"
    if not done_dir.is_dir():
        return out
    for path in sorted(done_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        data = _read_json(path)
        node = str(data.get("node") or "")
        creds = data.get("credentials")
        if node and isinstance(creds, dict) and creds:
            out[node] = creds
    return out


def _suite_of(run_dir: Path, summary: dict) -> str:
    """这一轮跑的是什么套件 —— **目录名里没有，只能从证据里读**。

    首选 `node_env.json` 的 `suite`（`farm_agent.py` 写的，权威）；老版本节点没这个文件时，
    退化成 summary 里出现过的 module 名集合（如 `launch` 或 `launch+login`）。

    存在的理由（2026-10-12 用户实报）：run 目录名只有「节点_日期_时刻」，
    一个节点跑过 133 轮之后，`..._1349` 和 `..._1353` 谁是谁根本分不清，
    更没法「拿报告去对应校验」。
    """
    env = _read_json(run_dir / NODE_ENV_EVIDENCE_NAME)
    suite = str(env.get("suite") or "").strip()
    if suite:
        return suite
    mods = sorted({str(c.get("module") or "") for c in (summary.get("cases") or []) if c.get("module")})
    return "+".join(mods) if mods else "?"


def _collect_nodes_data(root: Path) -> dict[str, dict]:
    """扫 `results/<节点>/<轮次>/summary.json` 并按节点聚合。

    `aggregate` 与 `index` 共用这一份扫描 —— 两处各写一份，口径迟早会漂。
    """
    results_root = root / "results"
    if not results_root.is_dir():
        raise SystemExit(f"没有结果目录：{results_root}")

    fallback_creds = _node_creds_from_receipts(root)

    nodes_data: dict[str, dict] = {}
    for node_dir in sorted(d for d in results_root.iterdir() if d.is_dir()):
        for run_dir in sorted(d for d in node_dir.iterdir() if d.is_dir()):
            summary_file = run_dir / "summary.json"
            if not summary_file.is_file():
                continue
            summary = _read_json(summary_file)
            if not summary:
                continue
            # 同一节点多轮 result 合并：后面的覆盖前面的同名用例。
            #
            # ⚠️ **合并是刻意的**：一个节点可能这轮跑 launch、下轮跑 settings，覆盖要跨轮累积。
            # 但"覆盖"只在**新那轮有记录**时发生 —— 新那轮没跑到（没投这个套件、或那条用例
            # setup 就崩了没进记录）时，老记录会**留在矩阵里冒充最新结果**。
            # 所以每格都记下它来自哪一轮（case_run），报告里标出来，别让人误读。
            bucket = nodes_data.setdefault(node_dir.name, {"profile": summary.get("profile") or {},
                                                            "cases": {}, "runs": [],
                                                            "case_run": {}, "run_meta": {},
                                                            "credentials": {}})
            run_name = summary.get("run") or run_dir.name
            bucket["runs"].append(run_name)
            bucket["run_meta"][run_name] = {
                "finished_at": summary.get("finished_at") or "",
                "passed": summary.get("passed", 0),
                "failed": summary.get("failed", 0),
                "errors": summary.get("errors", 0),
                "skipped": summary.get("skipped", 0),
                "total": summary.get("total", 0),
                "duration_s": summary.get("duration_s", 0),
                "suite": _suite_of(run_dir, summary),
            }
            # 目录名按 `节点_日期_时刻[_n]` 排，字典序≈时间序，所以最后一条就是最新一轮
            bucket["latest_run"] = run_name
            # 凭据状态来自节点随证据回传的 node_env.json（farm_agent 写）。
            # **必须真的读它**：报告的「凭据」列是测试人员区分「没配凭据」和
            # 「用例本身在跳过」的唯一依据（见《测试机操作手册》第四部分）。
            node_env = _read_json(run_dir / NODE_ENV_EVIDENCE_NAME)
            creds = node_env.get("credentials")
            if isinstance(creds, dict) and creds:
                bucket["credentials"] = creds
            for case in summary.get("cases") or []:
                bucket["cases"][case["nodeid"]] = case
                bucket["case_run"][case["nodeid"]] = run_name

    for node, bucket in nodes_data.items():
        if not bucket.get("credentials") and node in fallback_creds:
            bucket["credentials"] = fallback_creds[node]
    return nodes_data


def _write_node_indexes(root: Path, nodes_data: dict) -> list[Path]:
    """给每个节点写一份 `results/<节点>/index.html`：目录名 ↔ 套件 ↔ 结果。

    目录名不能改（`hall_auto/evidence.py` 的命名 + 节点侧 agent 已铺开，
    改名要所有节点 pull 一次，收益不抵成本），所以在控制机侧补一张对照表。
    """
    written = []
    for node, bucket in sorted(nodes_data.items()):
        path = root / "results" / node / "index.html"
        try:
            path.write_text(_render_node_index(node, bucket), encoding="utf-8")
        except OSError as exc:
            print(f"⚠ 节点索引写失败 {path}：{exc}")
            continue
        written.append(path)
    return written


def cmd_index(args) -> int:
    root = farm_root()
    nodes_data = _collect_nodes_data(root)
    if not nodes_data:
        raise SystemExit("没有可汇总的结果（results/ 下没有 summary.json）")
    written = _write_node_indexes(root, nodes_data)
    for p in written:
        print(f"节点索引：{p}")
    return 0


def cmd_aggregate(args) -> int:
    root = farm_root()
    nodes_data = _collect_nodes_data(root)
    if not nodes_data:
        raise SystemExit("没有可汇总的结果（results/ 下没有 summary.json）")

    report_html = _render_matrix(nodes_data)

    # 归档一份带时间戳的：根目录那个固定名会被下一轮直接覆盖，10-09 那份就是这么没的，
    # 跨批次对比从此无据可查。根目录那份**保留** —— 手册/清单里承诺的就是那个路径，
    # 挪走要同步《运行手册》《测试机操作手册》《新机操作清单》三份文档，不值当。
    archive_dir = root / "汇总"
    archive_dir.mkdir(parents=True, exist_ok=True)
    # 时间戳只到秒 —— 同一秒连跑两次（改代码后立刻重跑，很常见）会互相覆盖，
    # 那就把「归档」的意义丢了。撞了就加序号，跟 `evidence.new_run_dir` 一个口径。
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    archived = archive_dir / f"aggregate_{stamp}.html"
    n = 1
    while archived.exists():
        n += 1
        archived = archive_dir / f"aggregate_{stamp}_{n}.html"
    archived.write_text(report_html, encoding="utf-8")

    out = root / "aggregate_report.html"
    out.write_text(report_html, encoding="utf-8")

    indexes = _write_node_indexes(root, nodes_data)

    print(f"汇总报告：{out}")
    print(f"归档副本：{archived}")
    if indexes:
        print(f"节点索引：{len(indexes)} 份（results\\<节点>\\index.html，目录名 ↔ 套件 ↔ 结果）")
    print(f"覆盖节点 {len(nodes_data)} 台，用例 {len({c for v in nodes_data.values() for c in v['cases']})} 条")
    for node in sorted(nodes_data):
        runs = nodes_data[node].get("runs", [])
        if len(runs) > 1:
            print(f"  {node}：跨 {len(runs)} 轮合并（最新 {nodes_data[node].get('latest_run')}）"
                  "，报告里带 ° 的格子来自更早轮次")
    err_nodes = []
    for node in sorted(nodes_data):
        n_err = sum(1 for c in nodes_data[node]["cases"].values() if c.get("outcome") == "error")
        if n_err:
            err_nodes.append(f"{node} {n_err} 条")
    if err_nodes:
        print(f"⚠ 有「错误」（setup 崩了、用例根本没执行）：{'; '.join(err_nodes)}")
        print("  这类不是用例失败，去看该轮 run 目录里的 report.html 拿堆栈和截图。")
    no_creds = sorted(n for n, v in nodes_data.items() if not (v.get("credentials") or {}))
    if no_creds:
        print(f"⚠ 这些节点没拿到凭据状态（报告里凭据列会全 ✗，别据此判断用例是否真跑）：{', '.join(no_creds)}")
    return 0


_OUTCOMES = ("passed", "failed", "error", "skipped")
_SYMBOL = {"passed": "✓", "failed": "✗", "error": "!", "skipped": "–"}
_COLOR = {"passed": "#1a7f37", "failed": "#c62828", "error": "#8a5300", "skipped": "#9aa0a6"}


def _cred_cell(creds: dict, key: str) -> str:
    """凭据格：有=绿 ✓，没有=红 ✗。

    存在的意义是**区分 skip 的根因** —— 报告上一片黄时，看这列就知道
    是「这台机器没配凭据」还是「用例本身在跳过」。
    """
    ok = bool(creds.get(key))
    color = _COLOR["passed"] if ok else _COLOR["failed"]
    return f'<td style="color:{color}">{"✓" if ok else "✗"}</td>'


def _render_matrix(nodes_data: dict) -> str:
    """节点 × 用例矩阵：一眼看出「哪台机器挂在哪条用例」，那是兼容性结论的形态。

    ⚠️ 每格都标**来源轮次**（鼠标悬停可见），来自更早轮次的还会带一个 `°`。
    不标的话，合并跨轮结果会让人把老结果当最新结果读 —— 2026-09-22 就踩过：
    unit 有 188 条用例 setup 崩了没进记录，矩阵里那一格留着上一轮的 ✓，整行看着全绿。
    """
    all_cases = sorted({c for v in nodes_data.values() for c in v["cases"]})
    nodes = sorted(nodes_data)

    head = "".join(f"<th>{html.escape(n)}</th>" for n in nodes)
    rows = []
    for nodeid in all_cases:
        cells = []
        for node in nodes:
            bucket = nodes_data[node]
            case = bucket["cases"].get(nodeid)
            if case is None:
                cells.append('<td class="na">·</td>')
                continue
            outcome = case.get("outcome", "")
            symbol = _SYMBOL.get(outcome, "?")
            color = _COLOR.get(outcome, "#000")
            src = bucket.get("case_run", {}).get(nodeid, "")
            meta = bucket.get("run_meta", {}).get(src, {})
            carried = bool(src) and src != bucket.get("latest_run")
            tip = f"来源轮次: {src}（{meta.get('finished_at', '')}）"
            if carried:
                tip += " · 本节点最新一轮没跑到这条，用的是更早轮次的结果"
            assertion = (case.get("assertion") or "").strip()
            if assertion:
                tip += " · " + assertion[:200]
            mark = '<span class="carry">°</span>' if carried else ""
            cells.append(f'<td style="color:{color}" title="{html.escape(tip)}">{symbol}{mark}</td>')
        short = nodeid.split("::")[-1]
        module = nodeid.split("::")[0]
        rows.append(f'<tr><td class="case"><div>{html.escape(short)}</div>'
                    f'<div class="mod">{html.escape(module)}</div></td>{"".join(cells)}</tr>')

    env_rows = []
    for node in nodes:
        profile = nodes_data[node].get("profile") or {}
        creds = nodes_data[node].get("credentials") or {}
        env_rows.append(
            "<tr><td>" + html.escape(node) + "</td>"
            + "".join(f"<td>{html.escape(str(profile.get(k, '')))}</td>"
                      for k in ("os", "screen", "scale_percent", "webview2", "webview2_version", "ms_session"))
            + "".join(_cred_cell(creds, k) for k in
                      ("password_login", "microsoft_sso", "share_creds"))
            + "</tr>"
        )

    # 轮次表：把"这个节点的结论是哪几轮拼出来的"摊开，矩阵里的 ° 才有处可查
    run_rows = []
    for node in nodes:
        bucket = nodes_data[node]
        for run_name in bucket.get("runs", []):
            meta = bucket.get("run_meta", {}).get(run_name, {})
            latest = " ← 最新" if run_name == bucket.get("latest_run") else ""
            # 轮次目录链到该轮自己的 report.html：从汇总报告要能一路点到现场。
            # 「← 最新」必须留在 `<a>` 标签**内部** —— 单测按连续文本
            # `R01_..._1000 ← 最新` 断言，标签夹在中间会把它切断。
            link = (f'<a href="results/{html.escape(node)}/{html.escape(run_name)}/report.html">'
                    f'{html.escape(run_name)}{latest}</a>')
            run_rows.append(
                f"<tr><td>{html.escape(node)}</td>"
                f"<td>{html.escape(str(meta.get('suite', '?')))}</td>"
                f"<td>{link}</td>"
                f"<td>{html.escape(str(meta.get('finished_at', '')))}</td>"
                f"<td>{meta.get('total', 0)}</td>"
                f'<td style="color:{_COLOR["passed"]}">{meta.get("passed", 0)}</td>'
                f'<td style="color:{_COLOR["failed"]}">{meta.get("failed", 0)}</td>'
                f'<td style="color:{_COLOR["error"]}">{meta.get("errors", 0)}</td>'
                f"<td>{meta.get('skipped', 0)}</td></tr>"
            )
    if not run_rows:
        run_rows.append('<tr><td colspan="9" class="na">没有可汇总的 run</td></tr>')

    totals = {n: {o: sum(1 for c in nodes_data[n]["cases"].values() if c.get("outcome") == o)
                  for o in _OUTCOMES} for n in nodes}

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>多机汇总 · 节点 × 用例矩阵</title>
<style>
 body{{margin:0;background:#f6f8fa;color:#1f2328;font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;font-size:13px;}}
 .wrap{{max-width:1400px;margin:0 auto;padding:24px 16px 64px;}}
 h1{{font-size:20px;margin:0 0 4px;}} h2{{font-size:15px;margin:28px 0 8px;}}
 .meta{{color:#656d76;margin-bottom:16px;}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;}}
 th,td{{border-bottom:1px solid #e4e8ec;padding:6px 8px;text-align:center;}}
 th{{background:#eef1f4;font-size:12px;color:#656d76;position:sticky;top:0;}}
 td.case{{text-align:left;min-width:260px;}} .mod{{color:#9aa0a6;font-size:11px;}}
 .na{{color:#c9ced3;}}
 .carry{{color:#9aa0a6;font-size:11px;margin-left:1px;}}
 .lg-pass{{color:#1a7f37;}} .lg-fail{{color:#c62828;}}
 .lg-err{{color:#8a5300;}} .lg-skip{{color:#9aa0a6;}}
 .note{{background:#fff8e6;border:1px solid #f0d9a0;border-radius:8px;padding:10px 12px;color:#6b5300;margin-bottom:14px;}}
 .tbl-scroll{{overflow-x:auto;}}
</style></head><body><div class="wrap">
<h1>多机汇总 · 节点 × 用例矩阵</h1>
<div class="meta">生成于 {html.escape(datetime.now().isoformat(timespec='seconds'))} &nbsp;·&nbsp;
节点 {len(nodes)} 台 &nbsp;·&nbsp; 用例 {len(all_cases)} 条 &nbsp;·&nbsp;
<span class="lg-pass">✓ 通过</span> &nbsp;
<span class="lg-fail">✗ 失败</span> &nbsp;
<span class="lg-err">! 错误（setup 崩了，用例没执行）</span> &nbsp;
<span class="lg-skip">– 跳过</span> &nbsp; · 未跑到 &nbsp; ° 来自更早轮次</div>

<div class="note"><b>这张表是跨轮次合并的。</b>一个节点可能这轮跑 launch、下轮跑 settings，
所以同一个节点不同用例可能来自不同 run。鼠标悬停任意一格可看<b>它来自哪一轮</b>；
带 <b>°</b> 的格子表示<b>本节点最新一轮没跑到这条</b>，用的是更早轮次的结果 —— 别当最新结论读。
各轮的明细见下面「轮次」表（含<b>套件</b>列，轮次名可点进该轮报告）。<br>
<b>想去磁盘上核对某个目录是哪个套件？</b>看 <code>results\\&lt;节点&gt;\\index.html</code> —— 那份表把
「目录名 ↔ 套件 ↔ 结果」并排列出，顺序与资源管理器一致。</div>

<h2>各节点统计（跨轮合并后）</h2>
<div class="tbl-scroll"><table><thead><tr><th>节点</th><th>通过</th><th>失败</th><th>错误</th><th>跳过</th></tr></thead><tbody>
{"".join(f'<tr><td>{html.escape(n)}</td><td style="color:{_COLOR["passed"]}">{totals[n]["passed"]}</td><td style="color:{_COLOR["failed"]}">{totals[n]["failed"]}</td><td style="color:{_COLOR["error"]}">{totals[n]["error"]}</td><td>{totals[n]["skipped"]}</td></tr>' for n in nodes)}
</tbody></table></div>

<h2>轮次</h2>
<div class="tbl-scroll"><table><thead><tr><th>节点</th><th>套件</th><th>轮次</th><th>完成时间</th><th>用例</th><th>通过</th><th>失败</th><th>错误</th><th>跳过</th></tr></thead><tbody>
{"".join(run_rows)}
</tbody></table></div>

<h2>节点环境</h2>
<div class="tbl-scroll"><table><thead><tr><th>节点</th><th>系统</th><th>分辨率</th><th>缩放</th><th>WebView2</th><th>WV2 版本</th><th>微软会话</th><th>密码凭据</th><th>SSO 凭据</th><th>共享盘</th></tr></thead><tbody>
{"".join(env_rows)}
</tbody></table></div>

<h2>节点 × 用例</h2>
<div class="tbl-scroll"><table><thead><tr><th>用例</th>{head}</tr></thead><tbody>
{"".join(rows)}
</tbody></table></div>
</div></body></html>
"""


_INDEX_CSS = """
 body{margin:0;background:#f6f8fa;color:#1f2328;font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;font-size:13px;}
 .wrap{max-width:1200px;margin:0 auto;padding:24px 16px 64px;}
 h1{font-size:20px;margin:0 0 4px;} h2{font-size:15px;margin:28px 0 8px;}
 .meta{color:#656d76;margin-bottom:16px;line-height:1.7;}
 table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;}
 th,td{border-bottom:1px solid #e4e8ec;padding:7px 10px;text-align:center;}
 th{background:#eef1f4;font-size:12px;color:#656d76;}
 td.run{text-align:left;font-family:Consolas,monospace;font-size:12px;white-space:nowrap;}
 td.suite{text-align:left;font-weight:600;}
 tr.latest{background:#f0f7ff;}
 tr.latest td.suite{color:#185fa5;}
 .tag{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:10px;background:#dbeafe;color:#185fa5;font-size:11px;}
 a{color:#185fa5;text-decoration:none;} a:hover{text-decoration:underline;}
 .toolbar{display:flex;gap:8px;align-items:center;margin:0 0 10px;flex-wrap:wrap;}
 .toolbar input{flex:1;min-width:200px;padding:7px 11px;border:1px solid #d0d7de;border-radius:8px;font-size:13px;}
 .btn{padding:6px 13px;border:1px solid #d0d7de;background:#fff;border-radius:18px;cursor:pointer;font-size:12px;}
 .btn.active{background:#1f2328;color:#fff;border-color:#1f2328;}
 .note{background:#fff8e6;border:1px solid #f0d9a0;border-radius:8px;padding:10px 12px;color:#6b5300;margin-bottom:14px;}
 .empty{color:#9aa0a6;padding:14px;text-align:center;}
"""

_INDEX_JS = """
 const rows=[...document.querySelectorAll('#tbl tbody tr')];
 let f='all';
 function apply(){
   const q=document.getElementById('q').value.trim().toLowerCase();
   rows.forEach(r=>{
     const okF = f==='all' || r.dataset.suite===f;
     const okQ = !q || r.dataset.text.toLowerCase().includes(q);
     r.style.display=(okF&&okQ)?'':'none';
   });
 }
 document.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{
   document.querySelectorAll('.btn').forEach(x=>x.classList.remove('active'));
   b.classList.add('active'); f=b.dataset.f; apply();
 });
 document.getElementById('q').oninput=apply;
"""


def _verdict(meta: dict) -> str:
    """一轮的结论标签。失败 > 错误 > 通过，顺序不能反。"""
    if meta.get("failed"):
        return f'<span style="color:{_COLOR["failed"]}">✗ {meta["failed"]} 条失败</span>'
    if meta.get("errors"):
        return f'<span style="color:{_COLOR["error"]}">! {meta["errors"]} 条错误</span>'
    if meta.get("total"):
        return f'<span style="color:{_COLOR["passed"]}">✓ 全通过</span>'
    return '<span style="color:#9aa0a6">无记录</span>'


def _render_node_index(node: str, bucket: dict) -> str:
    """单节点轮次索引：**目录名 ↔ 套件 ↔ 结果**。

    2026-10-12 用户实报：`LAPTOP-x_2026-09-29_1349` 这类目录名里只有节点和时间，
    一个节点跑过 133 轮之后，资源管理器里完全分不清谁是谁，也没法「拿报告去对应校验」。
    套件名本来就写在每轮的 `node_env.json` 里，只是没人把它摊到目录这一层 —— 这里补上。

    表格按**目录名字典序**排（≈ 时间序），与资源管理器的顺序一致，方便左右对照。
    """
    runs = bucket.get("runs", [])
    run_meta = bucket.get("run_meta", {})
    latest = bucket.get("latest_run", "")

    rows = []
    for run_name in runs:
        meta = run_meta.get(run_name, {})
        suite = str(meta.get("suite") or "?")
        is_latest = run_name == latest
        tag = '<span class="tag">最新</span>' if is_latest else ""
        link = f'<a href="./{html.escape(run_name)}/report.html">{html.escape(run_name)}</a>'
        rows.append(
            f'<tr class="{"latest" if is_latest else ""}" data-suite="{html.escape(suite)}" '
            f'data-text="{html.escape(run_name + " " + suite)}">'
            f'<td class="run">{link}{tag}</td>'
            f'<td class="suite">{html.escape(suite)}</td>'
            f'<td>{html.escape(str(meta.get("finished_at") or ""))}</td>'
            f'<td>{meta.get("total", 0)}</td>'
            f'<td style="color:{_COLOR["passed"]}">{meta.get("passed", 0)}</td>'
            f'<td style="color:{_COLOR["failed"]}">{meta.get("failed", 0)}</td>'
            f'<td style="color:{_COLOR["error"]}">{meta.get("errors", 0)}</td>'
            f'<td style="color:{_COLOR["skipped"]}">{meta.get("skipped", 0)}</td>'
            f'<td>{meta.get("duration_s", 0)}s</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="9" class="empty">这个节点还没有任何一轮结果</td></tr>')

    # 按套件归并：想「校验 login」的人不该从 133 行里翻
    by_suite: dict[str, list[str]] = {}
    for run_name in runs:
        by_suite.setdefault(str(run_meta.get(run_name, {}).get("suite") or "?"), []).append(run_name)
    suite_rows = []
    for suite in sorted(by_suite):
        names = by_suite[suite]
        meta = run_meta.get(names[-1], {})
        suite_rows.append(
            f'<tr><td class="suite">{html.escape(suite)}</td>'
            f'<td>{len(names)}</td>'
            f'<td class="run"><a href="./{html.escape(names[-1])}/report.html">{html.escape(names[-1])}</a></td>'
            f'<td>{html.escape(str(meta.get("finished_at") or ""))}</td>'
            f'<td>{_verdict(meta)}</td></tr>'
        )
    if not suite_rows:
        suite_rows.append('<tr><td colspan="5" class="empty">无</td></tr>')

    buttons = ['<button class="btn active" data-f="all">全部套件</button>']
    for suite in sorted(by_suite):
        buttons.append(f'<button class="btn" data-f="{html.escape(suite)}">{html.escape(suite)}</button>')

    profile = bucket.get("profile") or {}
    env_bits = " &nbsp;·&nbsp; ".join(
        p for p in (
            f"系统: {html.escape(str(profile.get('os', '')))}" if profile.get("os") else "",
            f"缩放: {profile.get('scale_percent', '?')}%",
            f"分辨率: {html.escape(str(profile.get('screen', '')))}" if profile.get("screen") else "",
            f"大厅: {html.escape(str(profile.get('hall_version', '')))}" if profile.get("hall_version") else "",
        ) if p
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(node)} · 轮次索引</title>
<style>{_INDEX_CSS}</style></head><body><div class="wrap">
<h1>{html.escape(node)} · 轮次索引</h1>
<div class="meta">共 {len(runs)} 轮 &nbsp;·&nbsp; 最新一轮 <b>{html.escape(latest or "—")}</b>
&nbsp;·&nbsp; 生成于 {html.escape(datetime.now().isoformat(timespec="seconds"))}<br>{env_bits}</div>

<div class="note">左边「轮次目录」就是资源管理器里 <code>results\\{html.escape(node)}\\</code> 下的文件夹名，
点进去是该轮的报告。<b>按套件筛一下</b>就能只留你关心的那几轮。</div>

<h2>按套件看（每个套件最近一轮）</h2>
<table><thead><tr><th style="text-align:left">套件</th><th>轮次数</th><th style="text-align:left">最近一轮</th><th>完成时间</th><th>结论</th></tr></thead><tbody>
{"".join(suite_rows)}
</tbody></table>

<h2>全部轮次</h2>
<div class="toolbar"><input id="q" type="text" placeholder="搜目录名 / 套件…">{"".join(buttons)}</div>
<table id="tbl"><thead><tr><th style="text-align:left">轮次目录</th><th style="text-align:left">套件</th><th>完成时间</th><th>用例</th><th>通过</th><th>失败</th><th>错误</th><th>跳过</th><th>耗时</th></tr></thead><tbody>
{"".join(rows)}
</tbody></table>
</div><script>{_INDEX_JS}</script></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="多机跑批控制机侧")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dispatch = sub.add_parser("dispatch", help="生成并投放任务")
    p_dispatch.add_argument("--nodes", required=True, help="逗号分隔的节点标识")
    p_dispatch.add_argument(
        "--suites",
        required=True,
        help=(
            "逗号分隔的套件名；写 all（或 full）= 全量档，一次投完所有无人值守能跑的套件。"
            f"单个套件可用：{', '.join(sorted(SUITES))}"
        ),
    )
    p_dispatch.add_argument("--task-id", default="")
    p_dispatch.add_argument(
        "--allow-manual",
        action="store_true",
        help="允许把人在环套件（login-manual）投进农场。默认拒绝——它会让节点卡在等输入。",
    )
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_status = sub.add_parser("status", help="看任务进度")
    p_status.set_defaults(func=cmd_status)

    p_agg = sub.add_parser("aggregate", help="汇总成节点×用例矩阵")
    p_agg.set_defaults(func=cmd_aggregate)

    p_idx = sub.add_parser("index", help="只出各节点的轮次索引（目录名 ↔ 套件 ↔ 结果）")
    p_idx.set_defaults(func=cmd_index)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
