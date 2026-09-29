"""锁 tools/md_to_docx.py 里**纯函数**的那半截：Markdown → HTML。

Word COM 那半截（HTML → .docx）依赖本机装了 Word，没法在无人值守里跑，所以不测；
但「转换质量」全在 `markdown_to_html` 里，它出错的代价很具体 —— 一线拿到的 .docx
要么漏内容、要么把 `**` / 反引号原样露出来。这里把发放文档里真出现的构造钉住。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = REPO_ROOT / "tools" / "md_to_docx.py"
    spec = importlib.util.spec_from_file_location("md_to_docx_under_test", path)
    assert spec and spec.loader, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def md():
    return _load()


def test_headings_map_to_h_tags(md):
    out = md.markdown_to_html("# 一级\n\n### 三级\n")
    assert "<h1>一级</h1>" in out
    assert "<h3>三级</h3>" in out


def test_hr_and_paragraph(md):
    out = md.markdown_to_html("正文一段\n\n---\n\n另一段\n")
    assert "<hr>" in out
    assert "<p>正文一段</p>" in out


def test_fenced_code_keeps_content_literal(md):
    """代码块里的 `**` / 反引号是**终端原样输出**，不许被当格式吃掉。"""
    src = "```powershell\n[git-setup] **新开窗口**\n```\n"
    out = md.markdown_to_html(src)
    assert "<pre><code>" in out
    assert "[git-setup] **新开窗口**" in out
    assert "<strong>" not in out


def test_bold_spanning_inline_code_is_converted(md):
    """**核心**：`**看 <code>x</code> 就停**` 这种粗体跨过行内码，两端都得变成 <strong>。

    早先按代码段切开分别处理，开引号在一段、闭引号在另一段，两边都匹配不上，
    于是 `**` 原样漏进 .docx —— 本清单里这种写法到处都是。
    """
    out = md.markdown_to_html("**看不到 `install_git.cmd` 就停**\n")
    assert "<strong>" in out
    assert "<code>install_git.cmd</code>" in out
    assert "**" not in out


def test_gfm_table_becomes_html_table(md):
    src = "| 东西 | 用途 |\n| --- | --- |\n| 账号 | 步骤七 |\n"
    out = md.markdown_to_html(src)
    assert "<table>" in out and "<th>东西</th>" in out
    assert "<td>账号</td>" in out and "<td>步骤七</td>" in out


def test_task_list_items_get_checkboxes(md):
    out = md.markdown_to_html("- [ ] 没做\n- [x] 做了\n")
    assert "☐" in out and "☒" in out
    assert "[ ]" not in out and "[x]" not in out


def test_nested_list_and_code_inside_item(md):
    src = "1. 第一步：\n\n   ```powershell\n   python --version\n   ```\n\n   - 子项甲\n   - 子项乙\n"
    out = md.markdown_to_html(src)
    assert "<ol>" in out and "<ul>" in out
    assert "python --version" in out
    assert "子项甲" in out


def test_blockquote_recurses_into_list(md):
    src = "> - **机器**：测试机\n> - **终端**：新开\n"
    out = md.markdown_to_html(src)
    assert "<blockquote>" in out
    assert "<strong>机器</strong>" in out


def test_html_escaped_in_body_but_not_doubled(md):
    out = md.markdown_to_html("填 `<节点名>` 的地方\n")
    assert "&lt;节点名&gt;" in out


def test_link_becomes_anchor(md):
    out = md.markdown_to_html("[下载](https://example.com/x)\n")
    assert '<a href="https://example.com/x">下载</a>' in out


def test_full_doc_has_no_leaked_markers_outside_code(md):
    """整份《新机操作清单》转完，`<pre>` 之外不许残留 `**` —— 漏一个就是没转干净。"""
    doc = REPO_ROOT / "项目知识库" / "新机操作清单.md"
    if not doc.is_file():
        pytest.skip("仓库里没有这份文档")
    out = md.markdown_to_html(doc.read_text(encoding="utf-8"))
    stripped = md.re.sub(r"<pre>.*?</pre>", "", out, flags=md.re.S)
    assert "**" not in stripped
    assert "`" not in stripped
