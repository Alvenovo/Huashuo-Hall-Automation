# 用例清单（`cases/`）怎么用

> 面向：需要**增删改用例**的人。一页看完。
> 设计背景与取舍见根目录《用例数据化方案.md》。

---

## 一句话

**`cases/cases.yaml` 是唯一的真源。** 改用例 = 改这个文件。别的都是它的派生物。

```
cases/cases.yaml   ← 唯一真源，进 git，手改这个
cases/清单.md       ← 导出物（别手改），人看的表格版
```

---

## 三种操作

### 加一条用例

**分两种情况**，先想清楚是哪种：

| 情况 | 怎么做 | 需要改代码吗 |
| --- | --- | --- |
| 这个用例**已经有对应的自动化测试**（比如新加一组参数） | 在 YAML 里加一条，`ref` 指向已有的测试函数 | 不用 |
| 这是个**全新的**要自动化的用例 | 先写测试函数（`tests/launch/…`），再在 YAML 里登记一条 | 要 |
| 这个用例**暂时不自动化**，只想先记着 | 在 YAML 里加一条，`status: todo`，**`ref` 与 `suite` 都可以留空** | 不用 |

`status: todo` 的条目**不参与「清单 → 代码」对账**（本来就没有代码可指），
但**参与「代码 → 清单」**（它不会让别的用例漏登记）。
⚠️ 想在跑批里真跑起来，还是得先在 `hall_auto/suites.py` 里给它一个套件 —— 现在 `P2X-01/02` 就是这种状态。

### 删一条用例

1. 从 `cases/cases.yaml` 里删掉那条。
2. 如果对应的测试函数也不要了，一起删掉（`tests/launch/…`）。
3. 跑一遍守卫：`pytest tests/unit/test_cases_inventory.py -m unit -q`
   - 报「清单里有条目指向了不存在的用例」→ 你删了代码没删清单，或反过来。

### 改一条用例

| 改什么 | 改哪 |
| --- | --- |
| 编号 / 用例名 / 验证点描述 / 优先级 / 备注 | **只改 YAML** |
| 断言逻辑、控件定位、等待 | 改对应的测试函数（YAML 不用动） |

---

## 改完必做（三步，缺一步守卫就红）

```powershell
# 1) 重出清单（否则「清单过期」那条守卫会红）
.venv\Scripts\python.exe -X utf8 tools\cases_export.py --format md

# 2) 跑守卫，确认两个方向都对得上
.venv\Scripts\python.exe -X utf8 -m pytest tests/unit/test_cases_inventory.py -m unit -q

# 3) 跑全量单测，确认没连带破坏
.venv\Scripts\python.exe -X utf8 -m pytest tests/unit -m unit -q
```

---

## 导出给人看 / 导禅道

```powershell
# 宽表 CSV —— Excel 双击就能看（utf-8-sig，不会乱码）
.venv\Scripts\python.exe -X utf8 tools\cases_export.py --format csv --out "$env:USERPROFILE\Desktop\测试清单.csv"

# 禅道导入用的中间 JSON（12 列 CSV 再交给「禅道用例CSV」skill 生成）
.venv\Scripts\python.exe -X utf8 tools\cases_export.py --format zentao-json
```

⚠️ **禅道那条产出的 `steps` 是骨架** —— 自动化用例没有「人类步骤」，
现在是用 `verify` 兜底生成的。导禅道前**必须人工补真实步骤**。

---

## 字段速查

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 编号，全局唯一。P0 沿用原编号；其余按组自编 `P1A/P1B/P1C/P1D/P2S/P2W` |
| `title` | ✅ | 用例名 |
| `suite` | ✅ | 套件名，必须是 `hall_auto/suites.py` 里的键（`todo` 可空） |
| `marker` | ✅ | pytest 标记（选套件用）。**不能写 `xfail`/`skipif` 这类 pytest 内置标记**（`todo` 可空） |
| `tier` | ✅ | `code` = 逻辑在代码里；`data` = **参数表在本文件里**，由 conftest 注入 |
| `params` | | `tier: data` 时必填。每组一个 dict，`name` 会变成 pytest 的用例 id |
| `kind` | ✅ | `case` = 业务用例；`probe` = 只读探针（导出时默认不列） |
| `status` | ✅ | `active` / `manual`（人在环）/ `xfail`（疑似缺陷）/ `destructive`（真装真卸）/ `todo`（还没自动化） |
| `verify` | ✅ | 「具体验证什么」—— 导出物直接用它 |
| `ref` | ✅ | 代码指针 `tests/launch/xxx.py::test_yyy`（`todo` 可空） |
| `needs` | | 运行前提，写给人看 |
| `notes` | | 备注（已知问题、待办） |

---

## 参数表外置（`tier: data`）—— 加一组参数不用碰 Python

`cases/cases.yaml` 里 `tier: data` 的条目，参数表就写在它下面：

```yaml
- id: P1C-03
  tier: data
  ref: tests/launch/test_p1_register.py::test_register_invalid_password_blocks_submit
  params:
  - {name: short-3,     password: "ab1",      confirm: "ab1",      expect_tips: [true, true]}
  - {name: digits-only, password: "12345678", confirm: "12345678", expect_tips: [true, true]}
```

测试函数**照常写**，只多一个 `case` 形参：

```python
@pytest.mark.login
def test_register_invalid_password_blocks_submit(register_pid, case):
    password, confirm = case["password"], case["confirm"]
    ...
```

`tests/launch/conftest.py::pytest_generate_tests` 见到 `case` 形参就去 YAML 取参数表注入。

**加一组参数 = 在 YAML 里加一行，不碰 Python。**
已实测：往 `P1C-03` 插第 8 组 → 收集数 43→44；删掉 → 回到 43。

⚠️ 声明了 `case` 但 YAML 里没有对应条目 → **直接报错**，不会静默少跑。

## 守卫会拦你什么

`tests/unit/test_cases_inventory.py` 每次跑单测都会检查：

1. YAML schema 合法（必填字段、枚举值、id 唯一）
2. **清单 → 代码**：每条 `ref` 指的函数真的存在
3. **代码 → 清单**：每个 `def test_` 都在 YAML 里登记过
4. `suite` 是 `suites.py` 的键，且 `ref` 文件确实属于那个套件
5. `marker` 在 `pytest.ini` 里声明过
6. `cases/清单.md` 与真源一致（没重出就红）

**这些守卫做过负路径注入验证**（故意破坏 → 确认变红 → 还原 → 复绿），不是「看着是绿的」。
