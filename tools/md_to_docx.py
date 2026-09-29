"""把 Markdown 手册转成 Word（.docx）—— 新测试机上没有 Typora，`.md` 打不开。

## 为什么单独一个脚本

一线拿到的新机通常只装了 Office，没有能读 `.md` 的编辑器。发放版 `.md`（`sync_handbook.py`
生成、密码已内联）落到桌面后，再跑本脚本转一份同名 `.docx`，双击就能看。

它**不接管** `sync_handbook.py` 的同步职责：那份工具负责「密码内联 + 幂等 + 留档」，
本脚本只负责「已经内联好的 `.md` → `.docx`」。所以正常顺序是先 `sync_handbook.py`，
再对桌面那份发放版 `.md` 跑本脚本（见《交付前验收.md》）。

## 怎么转的

本机没有 pandoc / python-docx / markdown 库，pip 也连不上外网。所以：

1. **纯 Python 手写** Markdown → HTML（够用即可：标题、围栏代码、表格、任务/嵌套列表、
   引用、粗体、行内码、链接、分隔线 —— 就是发放文档里出现的那些构造）；
2. **Word COM**（`pywin32` + 已装的 Word）把 HTML 打开另存成 `.docx`（`FileFormat=16`
   = `wdFormatXMLDocument`）。

没装 Word / 没装 pywin32 时**明确报错退出**，不静默产出半成品 —— 和本项目一贯的
「假绿比红灯更坏」一致。

## 用法

    .venv\\Scripts\\python.exe -X utf8 tools\\md_to_docx.py 新机操作清单.md
    .venv\\Scripts\\python.exe -X utf8 tools\\md_to_docx.py 手册.md -o D:\\out\\手册.docx

退出码：`0` 成功 ／ `2` 缺 Word 或 pywin32 ／ `1` 其他失败（源文件不存在等）。
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# Markdown -> HTML
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"^(\s*)```")
_HEADING_RE = re.compile(r"^(\s*)(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_QUOTE_RE = re.compile(r"^\s*>")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s+)(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_TASK_RE = re.compile(r"^\[( |x|X)\]\s+")


def _indent_of(line: str) -> int:
    return len(line[: len(line) - len(line.lstrip())].expandtabs(4))


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _inline_non_code(segment: str) -> str:
    s = _escape(segment)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(.+?)__", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def _inline(text: str) -> str:
    """行内格式。

    先把 `代码` 抽成占位符再做粗体/链接 —— 否则像 `**看 <code>x</code> 就停**` 这种
    **粗体跨过行内码**的写法会被拆开：开引号在一段、闭引号在另一段，两边都匹配不上，
    于是 `**` 原样漏出去（本清单里到处都是这种写法）。
    """
    codes: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    tmp = re.sub(r"`([^`]*)`", _stash, text)
    tmp = _inline_non_code(tmp)  # 转义 + 粗体 + 链接（占位符只含 \x00 和数字，不受影响）

    def _unstash(m: re.Match[str]) -> str:
        return "<code>" + _escape(codes[int(m.group(1))]) + "</code>"

    return re.sub(r"\x00(\d+)\x00", _unstash, tmp)


def _dedent(line: str, width: int) -> str:
    if not line.strip():
        return ""
    stripped = line.expandtabs(4)
    lead = len(stripped) - len(stripped.lstrip(" "))
    return stripped[min(lead, width):]


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_sep(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line)) and "-" in line


def _parse_table(lines: list[str], i: int) -> tuple[str, int]:
    header = _split_row(lines[i])
    j = i + 2  # 跳过表头与分隔行
    rows: list[list[str]] = []
    while j < len(lines) and lines[j].strip() and "|" in lines[j]:
        rows.append(_split_row(lines[j]))
        j += 1
    parts = ["<table>", "<thead><tr>"]
    parts += [f"<th>{_inline(c)}</th>" for c in header]
    parts.append("</tr></thead><tbody>")
    for r in rows:
        parts.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts), j


def _parse_list(lines: list[str], i: int) -> tuple[str, int]:
    first = _LIST_RE.match(lines[i])
    assert first is not None
    base_indent = len(first.group(1).expandtabs(4))
    ordered = first.group(2)[0].isdigit()
    content_indent = base_indent + len(first.group(2)) + len(first.group(3).expandtabs(4))

    items: list[list[str]] = []
    cur: list[str] | None = None
    j = i
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            # 空行：看后面还有没有属于本列表的内容（缩进更深或同级列表项）
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines):
                mk = _LIST_RE.match(lines[k])
                if (mk and len(mk.group(1).expandtabs(4)) >= base_indent) or _indent_of(lines[k]) > base_indent:
                    if cur is not None:
                        cur.append("")
                    j = k
                    continue
            break
        mk = _LIST_RE.match(line)
        ind = _indent_of(line)
        if mk:
            mk_indent = len(mk.group(1).expandtabs(4))
            mk_ordered = mk.group(2)[0].isdigit()
            if mk_indent == base_indent and mk_ordered == ordered:
                if cur is not None:
                    items.append(cur)
                cur = [mk.group(4)]
                j += 1
                continue
            if mk_indent > base_indent:
                if cur is not None:
                    cur.append(line)
                j += 1
                continue
        if ind > base_indent:
            # 续行 / 列表项里的围栏代码块
            if cur is not None:
                cur.append(line)
            j += 1
            continue
        break
    if cur is not None:
        items.append(cur)

    tag = "ol" if ordered else "ul"
    out = [f"<{tag}>"]
    for item in items:
        head = item[0]
        body_lines = [_dedent(ln, content_indent) for ln in item[1:]]
        task = _TASK_RE.match(head)
        prefix = ""
        if task:
            prefix = "☒ " if task.group(1).lower() == "x" else "☐ "
            head = _TASK_RE.sub("", head, count=1)
        inner = blocks_to_html([head] + body_lines)
        out.append(f"<li>{prefix}{inner}</li>")
    out.append(f"</{tag}>")
    return "".join(out), j


def _parse_quote(lines: list[str], i: int) -> tuple[str, int]:
    j = i
    inner: list[str] = []
    while j < len(lines) and (_QUOTE_RE.match(lines[j]) or (lines[j].strip() and inner and _indent_of(lines[j]) > 0)):
        stripped = _QUOTE_RE.sub("", lines[j], count=1)
        if stripped.startswith(" "):
            stripped = stripped[1:]
        inner.append(stripped)
        j += 1
    return "<blockquote>" + blocks_to_html(inner) + "</blockquote>", j


def _parse_paragraph(lines: list[str], i: int) -> tuple[str, int]:
    j = i
    buf: list[str] = []
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            break
        if (
            _FENCE_RE.match(line)
            or _HEADING_RE.match(line)
            or _HR_RE.match(line)
            or _QUOTE_RE.match(line)
            or _LIST_RE.match(line)
        ):
            break
        if j + 1 < len(lines) and "|" in line and _is_table_sep(lines[j + 1]):
            break
        buf.append(line.strip())
        j += 1
    return "<p>" + _inline(" ".join(buf)) + "</p>", j


def _parse_code(lines: list[str], i: int) -> tuple[str, int]:
    fence_indent = len(_FENCE_RE.match(lines[i]).group(1).expandtabs(4))
    j = i + 1
    buf: list[str] = []
    while j < len(lines) and not _FENCE_RE.match(lines[j]):
        buf.append(_dedent(lines[j], fence_indent))
        j += 1
    j += 1  # 跳过收尾的 ```
    code = _escape("\n".join(buf))
    return f"<pre><code>{code}</code></pre>", j


def blocks_to_html(lines: list[str]) -> str:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _FENCE_RE.match(line):
            frag, i = _parse_code(lines, i)
        elif _HR_RE.match(line):
            frag, i = "<hr>", i + 1
        elif _HEADING_RE.match(line):
            m = _HEADING_RE.match(line)
            level = len(m.group(2))
            frag, i = f"<h{level}>{_inline(m.group(3))}</h{level}>", i + 1
        elif _QUOTE_RE.match(line):
            frag, i = _parse_quote(lines, i)
        elif "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
            frag, i = _parse_table(lines, i)
        elif _LIST_RE.match(line):
            frag, i = _parse_list(lines, i)
        else:
            frag, i = _parse_paragraph(lines, i)
        if frag:
            out.append(frag)
    return "".join(out)


_CSS = (
    "body{font-family:'Microsoft YaHei','Segoe UI',sans-serif;font-size:11pt;line-height:1.5;}"
    "h1{font-size:20pt;}h2{font-size:16pt;}h3{font-size:13pt;}"
    "code{font-family:Consolas,'Courier New',monospace;background:#f2f2f2;}"
    "pre{font-family:Consolas,'Courier New',monospace;background:#f5f5f5;"
    "border:1px solid #ddd;padding:6px;white-space:pre-wrap;}"
    "pre code{background:none;}"
    "table{border-collapse:collapse;}th,td{border:1px solid #999;padding:4px 8px;}"
    "th{background:#eee;}"
    "blockquote{border-left:3px solid #ccc;margin-left:0;padding-left:12px;color:#444;}"
)


def markdown_to_html(text: str) -> str:
    """把整份 Markdown 文本转成一个自带样式的 HTML 文档（Word 能直接打开另存）。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = blocks_to_html(lines)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


# --------------------------------------------------------------------------- #
# HTML -> DOCX（Word COM）
# --------------------------------------------------------------------------- #

_WD_FORMAT_XML_DOCUMENT = 16


class WordUnavailable(RuntimeError):
    """没装 Word / 没装 pywin32 —— 转不出 .docx，明确报错而不是静默失败。"""


def html_to_docx(html_text: str, docx_path: Path) -> None:
    try:
        import win32com.client as wc  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于机器
        raise WordUnavailable(f"没装 pywin32，无法调用 Word：{exc}") from exc

    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    try:
        tmp.write(html_text)
        tmp.close()
        try:
            word = wc.Dispatch("Word.Application")
        except Exception as exc:  # pragma: no cover - 取决于机器
            raise WordUnavailable(f"没找到可用的 Microsoft Word：{exc}") from exc
        try:
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(
                os.path.abspath(tmp.name), ConfirmConversions=False, ReadOnly=True
            )
            doc.SaveAs2(os.path.abspath(str(docx_path)), FileFormat=_WD_FORMAT_XML_DOCUMENT)
            doc.Close(False)
        finally:
            word.Quit()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def convert_file(src: Path, dst: Path) -> None:
    text = src.read_text(encoding="utf-8")
    html_to_docx(markdown_to_html(text), dst)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 Markdown 转成 Word .docx（走本机 Word）")
    parser.add_argument("src", help="要转换的 .md 文件")
    parser.add_argument("-o", "--out", default="", help="输出 .docx 路径（默认同名同目录）")
    args = parser.parse_args(argv)

    src = Path(args.src)
    if not src.is_file():
        print(f"[FAIL] 源文件不存在：{src}")
        return 1
    dst = Path(args.out) if args.out else src.with_suffix(".docx")
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        convert_file(src, dst)
    except WordUnavailable as exc:
        print(f"[FAIL] {exc}")
        print("       这台机器要能转出 .docx，需要装 Microsoft Word + pywin32。")
        return 2
    except Exception as exc:  # noqa: BLE001 - 转换失败要给出可读原因
        print(f"[FAIL] 转换失败：{exc}")
        return 1

    print(f"[OK] 已生成 {dst}（{dst.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
