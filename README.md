# 华硕大厅客户端 UI 自动化

WPF 外壳 + WebView2 业务页 + NSIS 安装器的 Windows 桌面客户端自动化测试框架。
基于 pytest + pywinauto(UIA) + pywin32(Win32)，支持 P0/P1/P2 分层、无人值守跑批、多机农场与证据留痕。

## 为什么不是普通 UI 自动化

- 三层定位降级：WPF 走 UIA；WebView2 走 UIA 树；NSIS 安装向导 UIA 失明 → Win32 `#32770` + `WM_GETTEXT`，只按文字匹配，不猜坐标。
- 六道可见性检查：IsWindowVisible / IsIconic / 顶层窗 / WindowFromPoint / DWMWA_CLOAKED / alpha；六道全过仍不可见，才判产品问题。
- 绕开 UIPI：非提权进程点不动提权卸载器 → 预建计划任务通道，真装真卸 0 人工。
- 静默失败治理：marker 守卫 + `--collect-only` 数量核对 + 失败信息带原始值（hwnd / rect / visible / ...）。
- 多机农场：控制机 + 节点 agent + 共享盘投递，支持用例分片；就绪轮询 3.1s → 1.1s。

## 目录

- `hall_auto/` 业务能力层（登录 / 应用 / 安装器 / 证据 / Win32 / 等待 / 提权）
- `tests/` 用例层（unit / launch / install + conftest 环境门禁）
- `tools/` 跑批与自举（bootstrap / farm / run_farm / reset）
- `项目知识库/` 运行手册、探壳结论、问题与解决
- `学习库/` 分层学习笔记

## 快速开始

Python 3.12。

```bash
pip install -r requirements.txt
copy config.yaml config.local.yaml
```

测试账号 / 密码只走环境变量：`HALL_TEST_USER` / `HALL_TEST_PASSWORD` / `HALL_TEST_NEW_PASSWORD` / `HALL_MS_USER`。

```powershell
.\run_p0_launch.ps1
.\run_p0_install.ps1
.\run_p1_apps.ps1
```

## 用例分层

- P0：安装 → 启动 → 搜索 → 详情主链
- P1：账号 / 装卸 / 设置，各自独立、失败互不影响
- P2：会员 / 活动入口
- `-m manual`：人在环，不进无人值守农场

## AI 协作

- `AGENTS.md` 定红线、知识索引与交接分工
- `会话交接.md` 锚定跨会话状态
- 17 条坑回写规则
- 规则层 / 状态层分离，换会话读库即可复原

## 规模

296 commits（2026-09-03 ~ 10-28，主体期 152）｜182 files｜34,830 行 Python｜49 个测试文件｜859 个 unit 测试函数 + 34 个端到端函数

## 安全

仓库不含密码 / 测试号 / 邮箱；账号只走环境变量；`config.local.yaml` 已 gitignore；破坏性操作有 marker 门禁 + 代码红线。
