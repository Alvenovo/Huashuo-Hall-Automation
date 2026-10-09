"""只读环境探针：把「大厅三个列表里有什么 / 公网通不通」打进跑批日志，供跨机差异定性。

不点任何按钮、不装不卸不登录；断言只守「三个列表刷得出来」，**列表内容只打印不判对错**。
存在理由：2026-10-09 节点更新列表缺 7-Zip（64位）而控制机在列，光看失败截图只能猜；
这类「同一份代码两台机器表现不同」的定性需要节点侧的一手事实。

投法（不进全量档，诊断时才投）：
    tools/run_farm.py --nodes <节点> --suites probe
"""

from __future__ import annotations

import json

import pytest

from hall_auto.apps import read_mine_lists
from hall_auto.launch import start_fresh, wait_main_window, wait_until_ready
from hall_auto.product import public_net_reachable, stop_main_process


@pytest.fixture()
def ready_main(cfg, installed):
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    yield main
    stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.launch
def test_probe_mine_lists_and_net(ready_main):
    """打印三个列表条目 + 公网可达性，然后以 skip 收尾把事实带进证据与日志。

    为什么收尾是 skip 不是 pass：农场绿轮**不落 captured stdout**（老坑），print 的事实
    到不了共享日志；而 skip 的 reason 会进 `summary.json` 的 assertion 字段和 pytest
    短摘要行，控制机翻日志/报告就能看见。探针本身没有「通过」语义，skip 正合适。
    断言只守「列表刷得出来」，刷不出来就 fail（那是真坏了）。
    """
    states = read_mine_lists(ready_main)
    for tab, state in states.items():
        assert state.loaded, f"{tab} 列表没刷出来：容器和空态提示都没找到"
    facts = {tab: list(state.items) for tab, state in states.items()}
    facts["public_net"] = public_net_reachable()
    line = json.dumps(facts, ensure_ascii=False)
    print(f"[probe] {line}", flush=True)
    pytest.skip(f"[probe] {line}")
