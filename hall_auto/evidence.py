from __future__ import annotations

import base64
import html
import io
import json
import platform
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from hall_auto.config import REPO_ROOT
from hall_auto.dpi import machine_profile, node_id
from hall_auto.screen import grab_virtual_screen

EVIDENCE_ROOT = REPO_ROOT / "reports" / "evidence"
_PREVIEW_MAX_WIDTH = 1280

MODE_ALL = "all"
MODE_FAILURE = "failure-only"

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
# setup 阶段就崩（fixture 报错）= pytest 的 ERROR，用例**根本没执行**。
# 必须与 failed 分开：混在一起看报告的人分不清「跑挂了」和「没跑起来」——
# 2026-09-22 就是因为它压根没进记录，汇总报告里那一格留着更早一轮的 ✓（假绿）。
OUTCOME_ERROR = "error"


def new_run_dir(root: Path = EVIDENCE_ROOT, node: str | None = None) -> Path:
    """多机并行时目录带节点前缀，避免 20 台回传撞名（同日同时分）。

    形如 `R12_2026-09-18_1030`；单机场景前缀仍会带（来自主机名），便于事后追溯是哪台跑的。
    """
    root.mkdir(parents=True, exist_ok=True)
    prefix = _safe_name(node or node_id(), limit=24)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base = root / f"{prefix}_{stamp}"
    cand, n = base, 1
    while cand.exists():
        n += 1
        cand = root / f"{base.name}_{n}"
    cand.mkdir(parents=True)
    return cand


# 用例目录名要「一眼看出是哪条用例」。原来直接用函数名（`test_p0_06_search_lists_keyword_titles`），
# 能看出大意但读起来费劲，也跟用例清单对不上号。
# 现在用「**编号_标题**」（`P0-06_搜索命中`）—— 编号是稳定键、能防重名、还能直接去
# `cases/cases.yaml` / `cases/清单.md` / 禅道里对上那一条。
#
# ⚠️ **绝不让命名影响取证**：查不到（新用例还没登记、用例文件缺失、标题含非法字符…）
# 就**退回函数名**，不抛、不中断。
_CASE_LABELS: dict[str, str] | None = None


def _case_labels() -> dict[str, str]:
    """`用例键 -> 编号_标题`。进程内缓存一次（跑批中途不会改用例文件）。

    键有两种，因为 `nodeid` 有两种形状：
      - 普通用例 / 参数化用例：**测试函数名**（`test_p0_06_search_lists_keyword_titles`）
      - 按页面挂载的通用函数：**pytest 的参数 id 就是用例编号**（`test_cases_register[P1C-01]`）
    """
    global _CASE_LABELS
    if _CASE_LABELS is not None:
        return _CASE_LABELS
    labels: dict[str, str] = {}
    try:
        from hall_auto import cases as cases_db

        for c in cases_db.all_cases():
            cid, title = str(c.get("id") or ""), str(c.get("title") or "")
            if not cid:
                continue
            label = f"{cid}_{title}" if title else cid
            labels[cid] = label                       # 参数 id 直接就是编号
            ref = c.get("ref") or ""
            if "::" in ref:
                labels[ref.split("::")[-1]] = label   # 也按函数名认
    except Exception:
        labels = {}
    _CASE_LABELS = labels
    return labels


def case_label(nodeid: str) -> str:
    """用例目录该叫什么。查不到就退回函数名（**命名失败不影响取证**）。"""
    raw = nodeid.split("::")[-1]
    labels = _case_labels()
    if "[" in raw and raw.endswith("]"):
        param = raw[raw.index("[") + 1:-1]
        if param in labels:                            # 参数 id = 用例编号
            return labels[param]
    func = raw.split("[")[0]
    return labels.get(func, raw)


def module_of(nodeid: str) -> str:
    rel = Path(nodeid.split("::")[0])
    return rel.parts[1] if len(rel.parts) >= 2 else "other"


def case_of(nodeid: str) -> str:
    return nodeid.split("::")[-1]


def _safe_name(name: str, limit: int = 80) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]", "_", name)
    cleaned = cleaned.strip(" .")
    return (cleaned or "case")[:limit]


# Windows 文件名里**真正非法**的只有这些（外加控制字符）。
# 中文、全角括号（）「」、冒号：、逗号，都是**合法**的，不该被洗掉 ——
# 「一眼看出是哪条用例」靠的就是它们。
_PATH_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_path_name(name: str, limit: int = 80) -> str:
    """**给目录名用的**清洗：只把真正非法的字符换成 `_`，其余原样保留。

    ⚠️ **别拿它替掉 `_safe_name`** —— 那个还被 `apps.py` 用来比对界面文字
    （`_safe_name(node) != popup_text(app_name)`），它的"把标点洗成 `_`"是**比对口径的一部分**，
    改了会让两边对不上。两个函数各管各的，别合并。
    """
    cleaned = _PATH_ILLEGAL.sub("_", name).strip(" .")
    return (cleaned or "case")[:limit]


def capture_screen(path: Path) -> bool:
    try:
        img, _ = grab_virtual_screen()
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        return True
    except Exception:
        return False


@dataclass
class CaseRecord:
    module: str
    case: str
    nodeid: str
    outcome: str
    duration_s: float
    timestamp: str
    assertion: str
    screenshot: str | None
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "case": self.case,
            "title": self.title,
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "duration_s": round(self.duration_s, 2),
            "timestamp": self.timestamp,
            "assertion": self.assertion,
            "screenshot": self.screenshot,
        }


class EvidenceSession:
    def __init__(self, run_dir: Path, mode: str):
        self.run_dir = run_dir
        self.mode = mode
        self.records: list[CaseRecord] = []

    def _should_capture(self, outcome: str) -> bool:
        # error 也要留图：setup 崩掉时现场（临时目录被拒、夹具起不来）只能靠截图复现
        return self.mode == MODE_ALL or outcome in (OUTCOME_FAILED, OUTCOME_ERROR)

    def record(self, nodeid: str, outcome: str, duration: float, assertion: str,
               title: str = "", capture: bool = True) -> CaseRecord:
        """记一条用例。`capture=False` = **连证据目录都不建**（不只是不抓屏）。

        ## 为什么要有这个开关（2026-09-23）

        `--evidence=all` 一开，**每一条**用例都抓一次屏。而 `tests/unit` 有 600+ 条
        **纯逻辑**用例，它们不碰 UI —— 抓下来的是**桌面**，一条都没有排查价值，
        代价却是实测 `pytest tests/unit` 从 **16s → 149s**（633 次抓屏）。

        单测的结论在 `summary.json` / `report.html` 里**一条都不少**，
        所以这里省掉的只是"没用的桌面截图"，不是信息。
        调用方（`tests/conftest.py`）按 marker 判定：带 `unit` 的用例传 `capture=False`。
        """
        module, case = module_of(nodeid), case_of(nodeid)
        # 目录名用「编号_标题」（查不到退回函数名）—— 见 case_label 的说明
        label = _safe_path_name(case_label(nodeid))
        screenshot_rel = None
        if capture and self._should_capture(outcome):
            case_dir = self.run_dir / _safe_path_name(module) / label
            case_dir.mkdir(parents=True, exist_ok=True)
            png = case_dir / "final.png"
            if capture_screen(png):
                screenshot_rel = f"{_safe_path_name(module)}/{label}/final.png"
            rec = CaseRecord(
                module=module, case=case, nodeid=nodeid, outcome=outcome,
                duration_s=duration, timestamp=datetime.now().isoformat(timespec="seconds"),
                assertion=assertion, screenshot=screenshot_rel, title=title,
            )
            (case_dir / "result.json").write_text(
                json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            rec = CaseRecord(
                module=module, case=case, nodeid=nodeid, outcome=outcome,
                duration_s=duration, timestamp=datetime.now().isoformat(timespec="seconds"),
                assertion=assertion, screenshot=None, title=title,
            )
        self.records.append(rec)
        return rec

    def finish(self) -> Path:
        passed = sum(1 for r in self.records if r.outcome == OUTCOME_PASSED)
        failed = sum(1 for r in self.records if r.outcome == OUTCOME_FAILED)
        skipped = sum(1 for r in self.records if r.outcome == OUTCOME_SKIPPED)
        errors = sum(1 for r in self.records if r.outcome == OUTCOME_ERROR)
        total_s = round(sum(r.duration_s for r in self.records), 2)
        summary = {
            "run": self.run_dir.name,
            "node": node_id(),
            "machine": platform.node(),
            "profile": machine_profile(),
            "mode": self.mode,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(self.records),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "duration_s": total_s,
            "cases": [r.to_dict() for r in self.records],
        }
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = self.run_dir / "report.html"
        report.write_text(_render_html(summary), encoding="utf-8")
        return report


# 轮次目录名有**两种**格式：
#   早期    `2026-09-17_1052`
#   多机之后 `<节点>_2026-10-13_1405`（同秒撞了再加 `_2`）
# 所以**不能用 `name.split("_")[:2]`** —— 对带节点前缀的格式它拼出
# `LAPTOP-x_2026`，`strptime` 抛 ValueError → 被 `continue` 吃掉 → **永远不清**。
# 2026-10-13 实测：本机 1140 个轮次目录里 **1124 个解析不了（98.6%）**，
# 「保留 30 天」形同虚设，证据会无限累积（已积到 49520 张 final.png）。
# 改成在名字里**搜**日期时间，两种格式都认。
_RUN_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{4})")


def _run_time(name: str) -> datetime | None:
    """从轮次目录名里取时间；取不到返回 None（**不删**，不猜）。"""
    m = _RUN_STAMP.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H%M")
    except ValueError:
        return None


def _read_json(path: Path) -> dict:
    """读一个 json；读不了就返回 {}（**索引页不能因为一个坏文件就整页不生成**）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _suites_of_run(run_dir: Path, summary: dict) -> str:
    """这一轮跑的是什么套件 —— **目录名里没有，只能从证据里读**。

    优先 `node_env.json` 的 `suite`（`farm_agent` 写的，权威）；本地跑没有那个文件，
    退化成 summary 里出现过的 module 名（`unit` / `launch` / `launch+login` …）。
    """
    env = _read_json(run_dir / "node_env.json") if (run_dir / "node_env.json").is_file() else {}
    suite = str((env or {}).get("suite") or "").strip()
    if suite:
        return suite
    mods = sorted({str(c.get("module") or "") for c in (summary.get("cases") or []) if c.get("module")})
    return "+".join(mods) if mods else "?"


def write_local_index(root: Path = EVIDENCE_ROOT) -> Path | None:
    """给**本地**证据根生成 `index.html`：一轮一行，能直接看出跑了什么、模块在哪。

    ## 为什么需要（2026-10-13 用户实报）

    农场侧 2026-10-12 就有 `results/<节点>/index.html`（目录名 ↔ 套件 ↔ 结果），
    **本地这份一直没有** —— 于是 `reports/evidence/` 底下堆成一片
    `LAPTOP-x_2026-10-13_1740_8` 这种只有时间戳的目录：**看不出哪轮跑了什么、
    某个模块的证据在哪个子目录**。

    本地跑一次就重生成一次（挂在 `pytest_sessionfinish`，跟 `prune_old_runs` 同处）。
    扫不到任何轮次就返回 None（不写空索引）。
    """
    if not root.is_dir():
        return None
    rows: list[tuple[str, str, int, int, int, int, str]] = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        summary = _read_json(d / "summary.json")
        if not summary:
            continue
        mods = sorted(p.name for p in d.iterdir() if p.is_dir())
        rows.append((
            d.name,
            _suites_of_run(d, summary),
            int(summary.get("passed") or 0),
            int(summary.get("failed") or 0),
            int(summary.get("error") or 0),
            int(summary.get("skipped") or 0),
            "、".join(mods) if mods else "（没建用例目录 —— 单测不抓屏）",
        ))
    if not rows:
        return None

    body = "\n".join(
        f'<tr><td><a href="{name}/report.html">{name}</a></td><td>{suite}</td>'
        f'<td class="ok">{p}</td><td class="bad">{f}</td><td class="err">{e}</td>'
        f'<td class="skip">{sk}</td><td class="mods">{mods}</td></tr>'
        for name, suite, p, f, e, sk, mods in rows
    )
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>本地证据索引</title><style>
body{{margin:0;background:#f6f8fa;color:#1f2328;font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;font-size:14px;line-height:1.5}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:20px;margin:0 0 6px}}
.meta{{color:#656d76;font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:10px;overflow:hidden}}
th,td{{padding:8px 12px;border-bottom:1px solid #eaeef2;text-align:left;font-size:13px}}
th{{background:#f6f8fa;font-weight:600}}
tr:last-child td{{border-bottom:none}}
td.ok{{color:#1a7f37}}td.bad{{color:#c62828}}td.err{{color:#8a5300}}td.skip{{color:#656d76}}
td.mods{{color:#656d76;font-size:12px}}
a{{color:#0969da;text-decoration:none}}a:hover{{text-decoration:underline}}
</style></head><body><div class="wrap">
<h1>本地证据索引</h1>
<div class="meta">{len(rows)} 轮 · 每轮一个目录，用例证据在 <b>&lt;轮次&gt;/&lt;模块&gt;/</b> 下
（模块名 = 测试文件去掉 <code>test_</code> 前缀）· 点轮次名看该轮报告</div>
<table><thead><tr><th>轮次</th><th>套件</th><th>过</th><th>红</th><th>错</th><th>跳</th><th>用例目录（模块）</th></tr></thead>
<tbody>
{body}
</tbody></table></div></body></html>"""
    out = root / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def prune_old_runs(root: Path = EVIDENCE_ROOT, keep_days: int = 30) -> list[Path]:
    if keep_days <= 0 or not root.exists():
        return []
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        day = _run_time(d.name)
        if day is None or day >= cutoff:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed


_CSS = """
  :root{--pass:#1a7f37;--pass-bg:#e6f4ea;--fail:#c62828;--fail-bg:#fdecea;
        --err:#8a5300;--err-bg:#fff4e0;--skip:#656d76;--skip-bg:#eef1f4;--ink:#1f2328;--muted:#656d76;--line:#d0d7de;--card:#fff;--bg:#f6f8fa;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;font-size:14px;line-height:1.5;}
  .wrap{max-width:1100px;margin:0 auto;padding:24px 16px 64px;}
  header.top{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 24px;margin-bottom:16px;}
  header.top h1{margin:0 0 6px;font-size:20px;}
  header.top .meta{color:var(--muted);font-size:13px;}
  .stats{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap;}
  .stat{flex:1;min-width:110px;border:1px solid var(--line);border-radius:8px;padding:10px 14px;}
  .stat .n{font-size:24px;font-weight:700;} .stat .l{color:var(--muted);font-size:12px;}
  .stat.pass .n{color:var(--pass);} .stat.fail .n{color:var(--fail);} .stat.skip .n{color:var(--skip);}
  .stat.err .n{color:var(--err);}
  .toolbar{display:flex;gap:8px;align-items:center;margin:0 0 12px;flex-wrap:wrap;}
  .toolbar input{flex:1;min-width:180px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px;}
  .btn{padding:7px 14px;border:1px solid var(--line);background:var(--card);border-radius:20px;cursor:pointer;font-size:13px;}
  .btn.active{background:var(--ink);color:#fff;border-color:var(--ink);}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:middle;}
  th{background:#eef1f4;font-size:12px;color:var(--muted);font-weight:600;}
  tr:last-child td{border-bottom:none;}
  tr.row-failed{background:var(--fail-bg);}
  tr.row-failed:hover{background:#f8d7d3;}
  tr.row-error{background:var(--err-bg);}
  tr.row-error:hover{background:#ffe9c4;}
  tr.row-skipped{background:var(--skip-bg);}
  tr.row-skipped:hover{background:#e3e7eb;}
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;}
  .badge.pass{color:var(--pass);background:var(--pass-bg);} .badge.fail{color:var(--fail);background:var(--fail-bg);}
  .badge.error{color:var(--err);background:var(--err-bg);}
  .badge.skipped{color:var(--skip);background:var(--skip-bg);}
  .mod{display:inline-block;padding:1px 8px;border-radius:6px;background:#eef1f4;color:var(--muted);font-size:12px;}
  .case{font-weight:600;} .title{font-size:13px;color:#3b4149;margin-top:2px;} .assert{color:var(--muted);font-size:12px;margin-top:2px;}
  .err{color:var(--fail);font-size:12px;margin-top:4px;font-family:Consolas,monospace;white-space:pre-wrap;}
  .dur{color:var(--muted);font-size:12px;white-space:nowrap;}
  .thumb{width:120px;height:72px;object-fit:cover;border:1px solid var(--line);border-radius:6px;cursor:zoom-in;background:#fff;display:block;}
  .noimg{color:var(--muted);font-size:12px;}
  .lb{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;z-index:99;cursor:zoom-out;}
  .lb.open{display:flex;} .lb img{max-width:92vw;max-height:92vh;border-radius:6px;box-shadow:0 8px 40px rgba(0,0,0,.5);}
  footer{color:var(--muted);font-size:12px;text-align:center;margin-top:24px;}
"""

_JS = """
  const rows=[...document.querySelectorAll('#tbl tbody tr')];
  let filter='all';
  function apply(){
    const q=document.getElementById('q').value.trim().toLowerCase();
    rows.forEach(r=>{
      const okF = filter==='all' ? true
                : filter==='img' ? r.dataset.img==='1'
                : r.dataset.s===filter;
      const okQ = !q || r.dataset.text.toLowerCase().includes(q);
      r.style.display=(okF&&okQ)?'':'none';
    });
  }
  document.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{
    document.querySelectorAll('.btn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); filter=b.dataset.f; apply();
  });
  document.getElementById('q').oninput=apply;
  const lb=document.getElementById('lb'), lbimg=document.getElementById('lbimg');
  document.querySelectorAll('.thumb').forEach(t=>t.onclick=()=>{lbimg.src=t.src;lb.classList.add('open');});
  lb.onclick=()=>lb.classList.remove('open');
"""


def _embed(png_rel: str, run_dir: Path) -> str | None:
    p = run_dir / png_rel
    if not p.exists():
        return None
    # 原图约 1MB/张，全量内嵌会让报告上百 MB；报告里只放 1280 宽 JPEG 预览，原图留在磁盘
    with Image.open(p) as img:
        if img.width > _PREVIEW_MAX_WIDTH:
            ratio = _PREVIEW_MAX_WIDTH / img.width
            img = img.resize((_PREVIEW_MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _render_html(summary: dict, run_dir: Path | None = None) -> str:
    run_dir = run_dir or (EVIDENCE_ROOT / summary["run"])
    total = summary["total"] or 1
    rate = round(summary["passed"] / total * 100)
    decorated = []
    for c in summary["cases"]:
        outcome = c["outcome"]
        badge = {"passed": "通过", "failed": "失败", "error": "错误", "skipped": "跳过"}.get(outcome, outcome)
        data_uri = _embed(c["screenshot"], run_dir) if c["screenshot"] else None
        img = (f'<img class="thumb" alt="final" src="{data_uri}">'
               if data_uri else '<span class="noimg">无截图</span>')
        # failed / error 都把原文摊开：error 是 setup 崩了，堆栈就是唯一线索
        broken = outcome in (OUTCOME_FAILED, OUTCOME_ERROR)
        err = f'<div class="err">{html.escape(c["assertion"])}</div>' if (broken and c["assertion"]) else ""
        title_line = f'<div class="title">{html.escape(c["title"])}</div>' if c.get("title") else ""
        assert_line = f'<div class="assert">{html.escape(c["assertion"]) or "—"}</div>' if not broken else ""
        row = (
            f'<tr class="row-{outcome}" data-s="{outcome}" data-img="{1 if data_uri else 0}" '
            f'data-text="{html.escape(c["module"] + " " + c["case"] + " " + c.get("title", ""))}">'
            f'<td><span class="badge {outcome}">{badge}</span></td>'
            f'<td><span class="mod">{html.escape(c["module"])}</span></td>'
            f'<td><div class="case">{html.escape(c["case"])}</div>{title_line}{assert_line}{err}</td>'
            f'<td class="dur">{c["duration_s"]}s</td>'
            f'<td>{img}</td></tr>'
        )
        decorated.append((bool(data_uri), row))
    # 有截图的排前面，方便人工直接看现场图；sorted 稳定，组内仍是执行顺序。
    rows = [row for _has_img, row in sorted(decorated, key=lambda t: not t[0])]
    body_rows = "\n".join(rows) or '<tr><td colspan="5" class="noimg">本轮无用例</td></tr>'
    profile = summary.get("profile") or {}
    node = summary.get("node") or summary.get("machine") or "unknown"
    env_line = " &nbsp;·&nbsp; ".join(
        part
        for part in (
            f"节点: <b>{html.escape(str(node))}</b>",
            f"系统: {html.escape(str(profile.get('os', '')))}" if profile.get("os") else "",
            f"缩放: {profile.get('scale_percent', '?')}%",
            f"分辨率: {html.escape(str(profile.get('screen', '')))}" if profile.get("screen") else "",
            f"Python: {html.escape(str(profile.get('python', '')))}" if profile.get("python") else "",
        )
        if part
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>华硕大厅自动化 · 证据汇总报告 {html.escape(summary["run"])}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>华硕大厅自动化 · 证据汇总报告</h1>
    <div class="meta">Run: <b>{html.escape(summary["run"])}</b> &nbsp;·&nbsp; 总耗时: {summary["duration_s"]}s &nbsp;·&nbsp; 采集策略: {html.escape(summary["mode"])}</div>
    <div class="meta" style="margin-top:4px">{env_line}</div>
    <div class="stats">
      <div class="stat"><div class="n">{summary["total"]}</div><div class="l">用例总数</div></div>
      <div class="stat pass"><div class="n">{summary["passed"]}</div><div class="l">通过</div></div>
      <div class="stat fail"><div class="n">{summary["failed"]}</div><div class="l">失败</div></div>
      <div class="stat err"><div class="n">{summary.get("errors", 0)}</div><div class="l">错误（没跑起来）</div></div>
      <div class="stat skip"><div class="n">{summary["skipped"]}</div><div class="l">跳过</div></div>
      <div class="stat"><div class="n">{rate}%</div><div class="l">通过率</div></div>
    </div>
  </header>
  <div class="toolbar">
    <input id="q" type="text" placeholder="搜索用例名 / 模块…">
    <button class="btn active" data-f="all">全部</button>
    <button class="btn" data-f="passed">只看通过</button>
    <button class="btn" data-f="failed">只看失败</button>
    <button class="btn" data-f="error">只看错误</button>
    <button class="btn" data-f="img">只看有截图</button>
  </div>
  <table id="tbl">
    <thead><tr><th style="width:90px">结果</th><th style="width:90px">模块</th><th>用例 / 关键断言</th><th style="width:70px">耗时</th><th style="width:130px">终态截图</th></tr></thead>
    <tbody>
{body_rows}
    </tbody>
  </table>
  <footer>自包含单文件（截图 base64 内嵌），可直接发送/存档</footer>
</div>
<div class="lb" id="lb"><img id="lbimg" alt=""></div>
<script>{_JS}</script>
</body>
</html>
"""
