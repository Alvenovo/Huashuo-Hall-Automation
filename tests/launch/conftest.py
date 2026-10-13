"""`launch` 套件的共享前置守卫 —— **要启动大厅的模块都从这里拿**。

## 为什么要有这个文件（2026-09-28）

`launch` 标记下有 7 个模块都要**启动华硕大厅**，而「本机没装大厅」这件事
原来是**每个模块各写各的**：3 个模块记得加守卫，4 个忘了。忘了的那几个在
全新机器上直接吐 `LaunchError: 未安装华硕大厅，无法启动`，而且是 **ERROR 不是 skip**
—— 2026-09-28 新机 `LAPTOP-68TGB8VB` 首跑实测：**一次 22 条 ERROR**。

这不是"没装"的错，是**守卫没写全**的错。两条硬证据：

1. `hall_auto/suites.py` 里 `Suite.needs` 的约定原文是
   「运行前提（写给人看，**缺则整套 skip** / 报错）」，而 `launch` / `apps-detect` /
   `settings` 的 `needs` 都写着「本机已装大厅」。**设计意图就是 skip** ——
   那 22 条 ERROR 是没实现自己声明的意图。
2. 「未安装」在新机上是**正常中间态**，不是故障：`bootstrap` 第 10 步
   （`tools/bootstrap_machine.py` 的 `Step("安装包准备")`）**只把安装包搬到本机**
   （本地 → 缓存 → 共享盘 → 下载，命中即止），**从不执行安装程序**；
   真正装大厅的是 `install` 套件（`tests/install/test_p0_install.py`）。
   而 `install` 在 `FULL_RUN_SUITES` 里**排在最后**（有守卫
   `test_elevated_suites_run_last` 锁着，因为它会重置登录态与主题档位）。

所以**新机首跑的正确顺序是先单独投 `install`**（见《新机操作清单》步骤十七）。
在那之前，「未安装 → 整套 skip」是正确结果，不该被渲染成一片红。

## 用法

    @pytest.fixture(scope="module")
    def ready_main(cfg, installed):     # ← 加一个 installed 就行
        app = start_fresh(cfg)

守卫放在这里，新套件**不用记得抄**。`tests/unit/test_launch_install_guard.py`
锁着这条约定：凡是要启动大厅的模块都必须用到它。
"""

from __future__ import annotations

import pytest

from hall_auto.product import read_installed

# 跳过文案。**保留「本机未安装华硕大厅」这个前缀** —— 报告里按它筛；
# 后半句给出可执行的下一步（新机首跑最常见的困惑就是"共享盘明明有包"）。
INSTALLED_SKIP_REASON = "本机未安装华硕大厅（新机首跑请先投 install 套件，bootstrap 只搬包不安装）"


@pytest.fixture(scope="module")
def installed():
    """本机已安装华硕大厅；没装就**整套 skip**（不是失败）。

    返回 `read_installed()` 的结果，调用方需要时可以直接用。

    `scope="module"`：一个模块判一次就够。原来 `test_p0_launch.py` 里那份是
    function 级的，每条用例都重读一遍注册表 —— 没必要，而且两处实现迟早漂移。
    """
    info = read_installed()
    if info is None or not info.exe_path.is_file():
        pytest.skip(INSTALLED_SKIP_REASON)
    return info


# ---------------------------------------------------------------------------
# 用例清单数据化：把 `tier: data` 用例的参数表从 `cases/cases.yaml` 注入进来
# （2026-10-12，见根目录《用例数据化方案.md》）
# ---------------------------------------------------------------------------

def pytest_generate_tests(metafunc):
    """**只在测试函数声明了 `case` 形参时才动手** —— 对其它用例零副作用。

    三种注入形态：

    - **按页面挂载**（通用用例函数）：函数名登记在 `hall_auto/drivers.py` 的
      `PAGE_TEST_FUNCS` 里，就把用例文件里所有 `page` 匹配、且**没写 `ref`** 的用例收进来。
      → **加一条同类型用例 = 只改 `cases/cases.yaml`，不用碰 Python。**
    - `params:` 列表 → `case` 是**其中一组参数**，pytest id = 那组的 `name`。
    - `driver:` 模板（单条，写了 `ref`）→ `case` 是**整条用例**，pytest id = 用例编号。

    用例文件里查不到对应条目时**直接报错**，不静默跳过（静默跳过等于悄悄少跑用例）。
    """
    if "case" not in metafunc.fixturenames:
        return

    from hall_auto import cases as cases_db

    injected = cases_db.injection_for(metafunc.function.__name__)
    if injected is None:
        # 通用用例函数？按 page 收（去掉参数后缀，nodeid 形如 `路径::函数名[参数]`）
        ref = metafunc.definition.nodeid.split("[")[0]
        page = cases_db.page_of_test_func(ref)
        if page:
            items = cases_db.cases_for_page(page)
            if not items:
                raise pytest.UsageError(
                    f"{metafunc.function.__name__} 是页面 {page!r} 的通用用例函数，"
                    f"但用例文件里没有 `page: {page}` 且没写 `ref` 的用例 —— 白跑一条"
                )
            metafunc.parametrize("case", items, ids=[str(c["id"]) for c in items])
            return
        raise pytest.UsageError(
            f"{metafunc.function.__name__} 声明了 `case` 形参，"
            f"但用例文件里没有它的 `tier: data` 条目，它也不是任何页面的通用用例函数"
            f"（`PAGE_TEST_FUNCS`）"
        )
    items, ids = injected
    metafunc.parametrize("case", items, ids=ids)
