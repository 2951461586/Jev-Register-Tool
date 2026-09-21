# 第二轮审计：耦合 / 目录 / 架构（2026-09-20 晚）

> 审计时间：2026-09-20 21:00
> 方式：读全部源码（19 个 .py，4744 行）+ AST 复算依赖/死符号/重复块 + 跑自测 + 对**行为**做复现。
> **每条结论都带可复现证据**，复算命令见附录。
> 前置：`docs/audit-2026-09-20.md`（当日上午那轮）的 18 项已全部落地。
> 本文**不重复**那 18 项，只报**当前仍然存在**的问题。

---

## 0. 先给结论

**架构底子是好的，不需要推翻重来。** 剩余空间集中在三类：

| 类别 | 条目 | 性质 |
|---|---|---|
| 🔴 **会静默丢数据 / 文档承诺了不工作的流程** | 2 条 | 与上午的 P0 同一类：不报错，只是数字变错 |
| 🟡 **第二份真源 + 死符号 + 重复代码** | 11 条 | 可机械消除，风险低，收益是"消除下次改错的地方" |
| 🟢 **结构升级（真正的重构）** | 3 条 | 有实际收益，但改动面大，现状够用 |

**实测确认的正面结论**（不是客套，是复算结果）：

| 项 | 实测 |
|---|---|
| 依赖分层 | 单向无环：`config 14 / ledger 7 / typesafe 7 / tempemail 6 / pipeline 6 / mailrules 5 / framer_waitlist 3` |
| 自测 | **151 项通过 / 0 失败**，全程离线，带负对照 |
| 仓库状态 | git 干净（5 个提交），`git status --short` 空 |
| `.gitignore` | `result/` 已**显式**列目录（`*.txt` 无通配覆盖那条坑已堵死） |
| 复杂度分布 | 无 1000 行以上的 `src/` 模块；最热函数 `stage_login` 97 行 |

> ⚠️ 上表是**审计当时（21:00）**的实测值。**当前值见 §0.6 末的指标表** ——
> 批次 A/B 之后自测是 161、依赖图多了 `parsing`、`typesafe` 是 359 行。
> 本节刻意不改成新数字：这一节是"当时底子如何"的快照。

---

## 0.5 修复状态（2026-09-20 22:30，本轮落地 1-6 项）

| # | 项 | 状态 | 落地位置与凭据 |
|---|---|---|---|
| 1 | `_fan_out` 并发崩溃静默丢账号 | ✅ **已修** | `pipeline._fan_out` + `_worker_crash_record()`；`resume_pending.pending()` 排除 `worker-crash#N` 占位键；回归测试 `test_fan_out_worker_crash_is_recorded`（7 项，含"串行路径 fail-fast"负对照）。复现脚本已从"证明有洞"改写为"验证不变量"，实测 **4 提交 / 4 返回 / 4 落账** |
| 2 | `claim` 两进程失效但 runbook 推荐 | ✅ **已标废弃**（**功能未修**） | `runbook.md` §1.5 重写 + §1 模式表 + §4.2 加"例外"段；`README.md` 工具树；`run_e2e.py` 的 `--mode` help 与 `--send` 执行前的显式告警；`pipeline.claim()` docstring。**附了根因候选与验证方法**（见 §1.5），等一次真人邮箱场景实测 |
| 3 | `ledger.add_source` 悬空护栏 | ✅ 已删 | `ledger.py`：删方法 + `_extra_sources` 字段；`load()` / `raw_rows()` 的重复读循环抽成 `_iter_raw()`（顺带消掉第三处重复） |
| 4 | 规则分组第二份真源 | ✅ 已收敛 | `mailrules.any_of(*rules)` 改收 `MailRule` 对象；`pipeline` 改用 `any_of(*CODE_RULES)` / `any_of(*LINK_RULES)`；自测加 3 项护栏（含"CODE/LINK 不重叠"负对照）；`docs/mail-filters.md` 同步 |
| 5 | `stage_login` 魔法链接 18 行重复 | ✅ 已抽 | 新增 `typesafe.extract_magic_link()` + `MAGIC_LINK_RE`（由 `config.STYTCH_LOGIN_HOST` **派生**，不再硬编码域名）；`pipeline._exchange_link()` 两个调用点共用。`stage_login` **97 → 74 行** |
| 6 | 零引用符号与只写字段 | ✅ 已删 | `typesafe.ACTION_GOOGLE`、`mailrules.SENDER_TRANSACTIONAL`、`tempemail.Mail.text` / `Mail.raw`、`Stats.http_4xx` / `.created` / **`.last_error`**（上一轮漏报的）、`ledger.Ledger.keys()`、`framer_waitlist` 未使用的 `import json`。⚠️ `config.STYTCH_LOGIN_HOST` **不删** —— 改为真正启用（消掉 3 处硬编码 URL） |

### 实测指标变化（前 → 后）

| 指标 | 修复前 | 修复后 | 复算命令 |
|---|---:|---:|---|
| 自测 | 151 通过 | **161 通过 / 0 失败** | `tools/selftest.py` |
| 顶层零引用符号 | 5 | **0** | `audit/scan.py` |
| 方法级零引用 | 1（`Ledger.keys`，被 docstring 假引用漏掉） | **0** | `audit/scan2.py` |
| ≥6 行重复代码块 | 12 组 | **0**（仅剩两处探针的单行 `import json`） | `audit/scan.py` |
| `stage_login` 行数 | 97 | **74** | `audit/scan.py` §4 |
| `pipeline.py` 行数 | 639 | 685 | 同上 |

> ⚠️ **`pipeline.py` 变大了 46 行**，如实记录：新增 `_exchange_link()` 与
> `_worker_crash_record()` 两个函数，以及说明"为什么必须这么写"的注释。
> **最热的函数变小了（97→74）、重复消失了、不变量成立了** —— 这才是这次的目标；
> 模块总行数不是。

### 本轮顺带修掉的**工具**缺陷（比代码缺陷更值得记）

审计脚本本身有两个盲点，都是"工具错了 ⇒ 给出假安全感"这一类：

1. **正则数引用不准**：初版只数 `name(` / `.name`，于是 `job`（作为回调**裸名**传递）
   被误报为死代码。改用 AST 的 `Name` / `Attribute` 节点计数。
2. **字符串里的同名 token 被当成引用**：`framer_waitlist.py` 的模块 docstring 里贴了
   一段 JS（`t.keys() - n`），给 `Ledger.keys()` 记了一次假引用 ⇒
   **真正的死方法被漏掉**。现在先 `ast` 剥 docstring 再统计。

> 这与上午审计 §10 的教训完全同构（AST 脚本漏计 `from src import X`，
> 而文档偏偏声明"依赖图是算出来的"）。**审计工具必须自己先被审计。**

### 仍未做（见 §6、§8、§9，属批次 C）

- ⑩ `selftest.py` 拆 `tools/tests/`（现 **1155 行**，已超自定线 355 行）
- ⑪ `pipeline` 拆 `stages.py` + `runner.py`

---

## 0.6 修复状态（2026-09-20 23:10，本轮落地 7-9 项）

| # | 项 | 状态 | 落地位置与凭据 |
|---|---|---|---|
| 7 | `typesafe` 解析层拆 `parsing.py` | ✅ **已做** | 新建 `src/parsing.py`（150 行）：`actions_from_html` / `action_form_fields` / `compact_ref` / `parse_js_object` / `visible_text` / `extract_magic_link` / `ACTION_FIELD_RE` / `ACTION_KEY_RE` / `MAGIC_LINK_RE`。`typesafe.py` **431 → 359 行**，只剩 client + `Result` + 异常。引用面同步改公开名：`tools/selftest.py`（4 处）+ 3 个探针的 import。**没有留兼容别名**（见下） |
| 8 | 活文档刷数字 | ✅ **已做** | 自测 **125 → 161**（`README` ×2、`runbook` ×2、`mail-filters` ×1、`architecture` ×3）；`architecture.md` §2 行数表 12 行全部按 `wc -l` 重算 + 新增 `parsing.py` 行；§3 依赖分层图与「被依赖次数」表加 `parsing`；§5 耦合点表 2 行改指 `parsing.*`；§6 架构债表 4 行刷新；`runbook` §3 自测分节表从 15 行补到 **20 行**（实测项数）。**每条都补了复算命令** |
| 9 | `resume_pending` 路径走 `config.EXPORT_DIR` | ✅ **已做** | `resume_pending.py` 的 `Path("exports/resume_pending.log")` → `config.EXPORT_DIR / "resume_pending.log"`（原写法是 CWD 依赖，与 `--log` 的 help 文本会静默不一致） |

### 本轮实测指标（7-9 项）

| 指标 | 前 | 后 | 复算命令 |
|---|---:|---:|---|
| 自测 | 161 通过 | **161 通过 / 0 失败**（无回归） | `tools/selftest.py` |
| 顶层零引用符号 | 0 | **0** | `audit/scan.py` §1 |
| `typesafe.py` 行数 | 431 | **359** | `wc -l src/typesafe.py` |
| `parsing.py` 行数 | — | **150** | `wc -l src/parsing.py` |
| 依赖分层 | `config 15 / ledger 7 / tempemail 6 / pipeline 6 / mailrules 5 / typesafe 7 / framer 3` | **`config 15 / ledger 7 / parsing 6 / tempemail 6 / pipeline 6 / mailrules 5 / typesafe 4 / framer 3`** | 附录 A |

> `typesafe 7 → 4` 是**预期内的下降**：三个探针原本从 `src.typesafe` 导入
> `_parse_js_object` 等私有名，现在改从 `src.parsing` 导入 —— 也就是
> **探针不再需要"为了拿一个纯函数而 import 整个 HTTP 客户端"**。

### 两个刻意的取舍（都写进代码注释了，免得下次被"顺手改回去"）

1. **不留兼容别名。** 拆分后没写 `_actions_from_html = actions_from_html` 这类壳。
   理由：同一份实现挂两个名字正是本报告 §3 一直在清的那种"第二份真源"；
   而且引用面只有 4 个文件（自测 + 3 探针），机械改一遍比养一个永久别名便宜。
   ⇒ 代价：**旧的私有名现在会 `ImportError`**，这是有意的（改名要报错，不要静默）。
2. **`parsing.py` 的边界判据是"能不能脱离 `requests` 单测"，不是"有没有正则"。**
   所以 `TypeSafeClient.exchange_magic_link()` 里抠 `xhr.send(JSON.stringify(…))`
   那一句、以及 `token_from_redirect_url()` **刻意留在原地** —— 它们各自只有一个
   调用点，输入来自那一次请求，抽出来只多一层间接。判据一旦放宽成"见到正则就挪"，
   `parsing.py` 会退化成杂物间。

> ⚠️ 报告 §8.2 原本枚举的移动集合是 6 项（`_actions_from_html` / `_compact_ref` /
> `_action_form_fields` / `_parse_js_object` / `_visible_text` / 两个正则）。
> 实际多挪了 `MAGIC_LINK_RE` + `extract_magic_link`：它同样是"可独立测的纯变换"，
> 留在 `typesafe.py` 会让新模块的边界变成"一部分解析器"。**这是对报告的一处偏离，
> 在此如实记录。**

---

## 0.7 修复状态（2026-09-21 04:20，本轮落地 ⑩ ⑪ —— 批次 C）

| # | 项 | 状态 | 落地位置与凭据 |
|---|---|---|---|
| ⑩ | `selftest.py` 拆 `tools/tests/` | ✅ **已做** | 入口 `tools/selftest.py` **1191 → 69 行**（只做聚合与调度，`main()` 体 20 行逐字未改）；新建 `tools/tests/`：`support.py` 237（共享夹具）/ `test_parsing.py` 143 / `test_ledger.py` 205 / `test_mailrules.py` 117 / `test_orchestration.py` 485 / `__init__.py` 5。走**机械切分 + 逐字等价断言**（生成器 `.workbuddy-ai/tmp/split_selftest_10.py`，复制段 892 行 + `main` 体 20 行） |
| ⑪ | `pipeline` 拆 `stages.py` + `runner.py` | ✅ **已做** | `src/pipeline.py`（684 行）→ `src/stages.py` **405**（`StageMixin` + `AccountRecord`，出网动作全在这里）+ `src/runner.py` **324**（`Pipeline`：批量 / 并发 / 监听 / 台账写入）。**不留兼容壳**：4 个 Python 引用点直接改名（`run_e2e` / 2 个探针 / 自测）。生成器 `.workbuddy-ai/tmp/split_pipeline_11.py`，**复制段 655 行逐字等价** |

### 本轮实测指标

| 指标 | 前 | 后 | 复算命令 |
|---|---:|---:|---|
| 自测 | 161 通过 | **161 通过 / 0 失败**（无回归） | `$PY tools/selftest.py` |
| 顶层零引用符号 | 0 | **0** | `.workbuddy-ai/audit/scan.py` §1 |
| `src/pipeline.py` | 684 | **已拆**：`stages` 405 + `runner` 324 | `wc -l src/stages.py src/runner.py` |
| `tools/selftest.py` | 1191 | **69**（+ `tools/tests/` **1192**，6 文件） | `wc -l tools/selftest.py tools/tests/*.py` |
| 最长函数（`src/`） | `stage_login` 74 | **`stage_login` 74**（未动，`stages.py`） | `scan.py` §4 |
| 依赖分层（含 tools） | `config 15 / ledger 7 / parsing 6 / tempemail 6 / pipeline 6 / mailrules 5 / typesafe 4 / framer 3` | **`config 15 / ledger 7 / parsing 6 / tempemail 6 / typesafe 5 / mailrules 5 / runner 4 / support 4 / stages 3 / framer 3`** | 附录 A |
| src 依赖图 | 无环（8 模块） | **无环（9 模块）**，`stages` 不反向依赖 `runner` | 附录 A |

> `check()` 调用点 **162** 个、执行 **161** 项 —— 这个差 1 **拆分前后完全一致**
> （`grep -c "^\s*check("` 拆分前备份也是 162），不是本轮引入的。
> 文档里的 161 以**运行输出**为准。

### 🔴 本轮唯一真正的风险点：monkeypatch 接缝（拆分类模块最容易踩的那种）

`offline()` 原本只 patch `src.pipeline` **一个**命名空间。拆完之后，"**读**"这三个
出网符号的模块变成了两个，patch 必须跟着分家：

    stages.TypeSafeClient   ← stage_login / stage_login_with_token 建会话
    stages.framer_submit    ← stage_apply 提交申请表单
    runner.TypeSafeClient   ← claim() 的 send_first 分支
    runner.TempMailClient   ← Pipeline.__init__ / _clone() 建邮箱客户端

判据只有一条：**读**这个符号的那个模块的 globals。少 patch 一个模块，另一个会
静默拿到**真类** ⇒ 自测开始发真请求。这种失效**不报 `ImportError`**，只是突然全线
超时，极难定位。⇒ 现在 `offline()` 里有一条 `assert`：三个符号必须都被覆盖到，
少一个立刻炸（接缝守卫）。验证：自测仍 **161 / 0** —— 若接缝失效，依赖替身的
11 组编排测试会立刻超时报错，不可能全绿。

### 另外三处坑：都不是"搬家副作用"，是**护栏本身的洞**

1. **`test_status_vocabulary` 的扫描面悄悄缩小。** 它原本扫
   `(ROOT/"tools").glob("*.py")`，自测拆进 `tools/tests/` 之后，这个 glob
   **匹配不到子目录** ⇒ 测试文件里写的 status 字面量会逃出覆盖检查。
   这类"覆盖面缩小"不会让任何一条断言变红，只是让护栏对**将来**新增的状态
   失去反应 —— 典型假绿。已改成 `rglob`：扫描文件 **16 → 28**，字面量集合
   **不变**（仍是 8 个），即只增不减。
2. **审计工具自己也踩了同一个坑。** `.workbuddy-ai/audit/scan.py` 用三个**显式**
   glob 收集 `.py`（`src/` + `tools/` + `tools/probes/`），新增 `tools/tests/`
   后**完全不在扫描范围内** —— 拆分后 1192 行代码从死符号扫描里消失，而
   `selftest.py` 看起来"瘦了 20 倍"。`scan2.py` 同理：还在扫入口文件里的
   `check()` 调用 ⇒ 报 **0 处**。两处都已修。
   **这正是本项目反复强调的"审计工具必须自己先被审计"** —— 不修的话，下次跑
   审计会得到一份"看起来很干净"的假报告。
3. **`scan2.py` 的修复当时只做了一半**（收尾更新文档时才抓到）。上一轮只同步了
   `check()` 计数**那一处**（症状），**没同步它自己的 `ALL`**（根因）⇒ 三处 AST
   扫描 —— 方法级零调用点 / docstring 剥离 / 字段只写不读 —— **同时**失明。
   实测：`mailrules.subject_ok` 被 `tools/tests/test_mailrules.py:39` 真实调用，
   却被报成"零调用点"。补 `ALL` 后 **1 → 0**，且**未引入任何新发现**（证明修的是
   根因，不是把这个符号塞进白名单）。两个脚本现在都带"扫描面非空"断言
   （`assert SRC and TOOLS and TESTS`）。
   ⇒ **一个盲区往往以多个症状出现。修了被报出来的那个，要主动去找它旁边的同类。**

### 三处对报告的刻意偏离（都写进代码注释了）

1. **`StageMixin` 而不是自由函数**（§8.1 原计划 `apply(rec, mail, ...)` 这类）。
   阶段方法实际读 **4 个实例字段**：`self.mail` / `self.login_mode` /
   `self.success_ledger` / `self.log`。转自由函数必须先引入一个 ctx 参数对象 ——
   那是**另一档改动**（生产调用点 9 处 + 自测 14 处），换来的收益却只是
   "不造 `Pipeline` 实例就能调"，而现行自测早已通过 `Pipeline` 实例覆盖了全部阶段。
   `StageMixin` 用**零缩进改动**达成模块级切分 ⇒ 可以做字节级等价验证。
2. **`support.py` 而不是 `conftest.py`**（§6.1 原计划）。`conftest.py` 是 pytest 的
   保留名；本项目零第三方依赖、跑法仍是 `$PY tools/selftest.py`，用它只会让人以为
   装了 pytest，且将来真要迁 pytest 时会撞名。
3. **顺手修了三个审计工具缺陷**（都不在原计划里）。前两个见上；第三个是收尾
   更新文档时才发现的：`scan2.py` 当时只同步了 `check()` 计数**那一处**，漏了它
   自己的 `ALL`，于是三处 AST 扫描（方法级零调用点 / docstring 剥离 / 字段只写
   不读）**同时**失明，把 `mailrules.subject_ok`（被
   `tools/tests/test_mailrules.py:39` 真实调用）报成"零调用点"。补 `ALL` 后
   方法级零调用点 **1 → 0**，且**未引入任何新发现** —— 说明修的是根因，而不是
   把这个符号塞进白名单。两个脚本现在都带"扫描面非空"断言。
   理由见上 —— 属于"发现工具给出假安全感就当场修"，不是顺手重构。

### 仍未做（有意不做）

- §6.3 `exports/` 残留清理（报告原文即"不建议现在动手"）
- §6.4 删 `.pytest_cache/`（同上）
- §5.4 `probes/` 里 6 行 dfp 重复（报告原文建议**接受**它）
- §8.3 `resume_pending.py` 用 subprocess 调 `run_e2e.py`（收益不足以覆盖回归面）
- `--mode claim` 两进程失效 —— **仍是唯一已知未修的功能洞**，见 §0.5 / runbook §1.5

---

## 1. 🔴 P1：`_fan_out` 在并发 worker 抛异常时**静默丢掉账号**

> ✅ **已修**（2026-09-20 22:30，见 §0.5）。下文保留**审计当时**的形态与证据，
> 以维持"结论可核查"；当前实现见 `src/runner.py` 的 `_fan_out` / `_worker_crash_record`
> （2026-09-21 起 `pipeline.py` 已拆成 `runner.py` + `stages.py`）。

### 位置

`src/pipeline.py:452-474`（`Pipeline._fan_out`）

```python
out: list[AccountRecord | None] = [None] * len(jobs)
with ThreadPoolExecutor(max_workers=concurrency) as pool:
    ...
    for fut in as_completed(futs):
        try:
            out[futs[fut]] = fut.result()
        except Exception as exc:
            self.log(f"✗ 并发任务异常: {type(exc).__name__}: {exc}")   # ← 只有这一行痕迹
return [r for r in out if r is not None]                              # ← 把 None 过滤掉
```

### 复现（已跑，`.workbuddy-ai/audit/repro_fanout.py`）

```
提交任务数        : 4
_fan_out 返回条数 : 3
台账实际写入条数  : 3
丢失的账号        : ['a2@x.com']
```

### 为什么这是 P1 而不是"日志噪音"

1. 崩溃的 worker **既不在返回值里、也不在台账里** —— 因为 `job()` 是先跑完再 `ledger.append()`，
   异常发生在 append 之前。
2. `report()` 的"合计"用的是 `len(recs)` ⇒ **数字自洽，看不出缺口**。
   提交 100 个、崩了 3 个，报告会写"合计 97"，没有任何一处会显示 100。
3. 唯一痕迹是 stdout 一行 `✗ 并发任务异常`，**串行路径（`concurrency=1`）完全没有这条路径** ——
   也就是说这个洞只在开并发时存在，而开并发正是为了跑大批量。
4. 这与上午修的 P0（`RANK` 词汇错配导致账号从验收清单消失）、
   第 16 项（确认邮件超时被记成 failed）是**同一类**：
   *不报错，只是少几行*。项目自己已经把它定义为最高优先级。

### 建议修法

**最小改法**（推荐）：异常也要落一条台账记录，让"提交数 == 结果数"成为不变量。

```python
except Exception as exc:
    key = jobs[futs[fut]][0]
    rec = AccountRecord(key=str(key), email=str(key))
    rec.status = "failed"
    rec.error = f"worker 崩溃: {type(exc).__name__}: {exc}"
    self.ledger.append(rec.to_dict())
    out[futs[fut]] = rec
    self.log(f"✗ 并发任务异常（已记账，不再静默丢弃）: {type(exc).__name__}: {exc}")
return [r for r in out if r is not None]
```

**配套护栏**：自测加一条负对照 —— 注入一个必崩的 job，断言
`len(_fan_out(...)) == len(jobs)` **且** 台账里有一条 `failed`。
现在的 `test_concurrency_no_crosstalk` 只测了"6 个全成功"，没有崩溃路径。

---

## 2. 🔴 P1：`--mode claim` 的推荐用法**实测不工作**，但 runbook 仍把它写成标准流程

> ✅ **已处置**（2026-09-20 22:30，见 §0.5）：**文档侧已全面标注废弃**（runbook §1.5
> 重写、§4.2 加例外段、README、CLI help、`--send` 前告警），
> **功能侧未修** —— 附了根因候选与验证方法，等一次真人邮箱场景实测。

### 现状

`docs/runbook.md` §1.5 把两进程接力写成推荐做法：

```bash
# a) 先让站点把码发到那个邮箱（不提交）
$PY tools/run_e2e.py --mode claim --email me@real.com --send
# b) 从邮箱取到 6 位码，10 分钟内提交
$PY tools/run_e2e.py --mode claim --email me@real.com --token 123456
```

`README.md` 的模式表也列着 `claim`，`selftest.py::test_claim` 有 7 项覆盖。

### 实测结论（记录在 `.workbuddy-ai/memory/MEMORY.md`，但**没有进 docs/**）

> ⚠️ `--mode claim` 的**两进程设计会 `401 Code expired`**：
> `--send` 与 `--token` 各自新建会话，实测码到 2 分钟内提交仍报过期。
> `resume`（同一会话内发码+提交）正常 ⇒ 需要人工接力时**别用 claim，改用 resume**。

代码侧确认成因：`claim(send_first=True)` 内部 `TypeSafeClient()` 是**局部变量**，
进程退出即丢弃；第二次调用 `stage_login_with_token()` 又 `TypeSafeClient()` 新建。
两个进程之间没有任何会话传递。

### 为什么必须处置

这是"**文档承诺了一个不工作的流程**"。操作者按 runbook 走，会拿到 `401 Code expired`，
而 runbook §4.2 对该错误码的处置写的是"**重新发码，10 分钟内提交**" ——
**把人推回同一个死循环**。与上午第 17 项（`post_setup` 静默退化只报 HTTP 404）
是同一类：错误信息与根因毫无字面关联。

### 两条路（取舍明确）

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 标废弃**（成本 10 分钟） | runbook §1.5 改成"人工接力用 `resume`"，`claim` 标注为**实验性/已知失效**；README 模式表同步；`test_claim` 保留（它测的是单进程内 `code_sent` 语义，仍有效） | 不修复功能，但**消除误导**。`claim` 的"外部凭据"能力（真人邮箱场景）暂时放弃 |
| **B. 修**（成本半天） | 把 `--send` 阶段的会话 cookie 落盘（`requests.Session` 的 cookies + 发码时间戳），`--token` 阶段读回；或干脆合并成单进程：`--send --wait-token` 交互式读 stdin | 真正修复，但要新增"会话持久化"这一层，且要处理 cookie 过期/清理，属于新功能 |

**建议先做 A**：当前的交付路径（`resume` 批量补跑）已经完全可用，
`claim` 的独特价值只在"获批邮箱是真人邮箱、Worker 读不到"这一种场景，
而该场景本轮没有实际需求。**把误导消掉比把冷路径修好更重要。**

---

## 3. 🟡 P2：**第二份真源**（与上午 §2 同类，但这次藏在"唯一真源"文件内部）

> ✅ **已收敛**（2026-09-20 22:30，见 §0.5）：3.1 改用 `any_of(*CODE_RULES)`；
> 3.2 `config.STYTCH_LOGIN_HOST` **改为真正启用**（不删）；3.3 / 3.4 已删常量并写明删除理由。

上午删掉了 `config.py` 里那份重复的收件规则。**同一个病换了地方复发**：

### 3.1 `mailrules.CODE_RULES` / `LINK_RULES` —— 零引用

```python
# src/mailrules.py:158,161
CODE_RULES: tuple[MailRule, ...] = (_BY_NAME["signin_code"], _BY_NAME["verify_code"])
LINK_RULES: tuple[MailRule, ...] = (_BY_NAME["welcome_confirm"], _BY_NAME["signin_link"])
```

而 `pipeline.py:67-68` 表达的是**同一件事**，用的是另一套写法：

```python
MATCH_CODE = any_of("signin_code", "verify_code")
MATCH_LINK = any_of("welcome_confirm", "signin_link")
```

零引用实测：`grep -rn "CODE_RULES\|LINK_RULES"` → 只有定义处 2 行。

**危害**：它就在"唯一真源"文件里，看起来比 `any_of(...)` 更权威。
下一个人要加一条验证码规则（例如站点新增 `login_code` 文案）时，
**极可能去改 `CODE_RULES` 而漏改 `pipeline.MATCH_CODE`** ⇒ 新规则不生效，
且 `--mode scan` 会显示"已被规则覆盖"（因为 `RULES` 表里有），
排查会引向"D1 窗口被挤爆"。

**建议**：让 `any_of` 接收规则对象而不是名字，`pipeline` 直接用这两个常量：

```python
# mailrules.py
def any_of(*rules: MailRule):
    return lambda m: any(r.matches(m) for r in rules)
# pipeline.py
MATCH_CODE = any_of(*CODE_RULES)
MATCH_LINK = any_of(*LINK_RULES)
```

这样"哪些规则算码/链接"只有一处定义。**或者**直接删掉这两个常量 ——
但考虑到它们承载了语义分组，收敛到它们更合适。

### 3.2 `config.STYTCH_LOGIN_HOST` —— 零引用，而 URL 被硬编码 3 次

```
src/config.py:51:   STYTCH_LOGIN_HOST = "https://login.typesafe.ai"     ← 零引用
src/typesafe.py:237:  self.s.post("https://login.typesafe.ai/v1/magic_links/redirect/dfp", ...)
src/typesafe.py:241:  "Origin": "https://login.typesafe.ai",
tools/probes/probe_confirm.py:108,111
tools/probes/probe_confirm_flow.py:82,85
```

常量存在、代码绕开它 —— 改配置**不生效**。对比 `config.SITE_ORIGIN` 是正常使用的，
说明这是遗漏而非设计。

### 3.3 `mailrules.SENDER_TRANSACTIONAL` —— 零引用，且它描述的**分层根本没落地**

```python
# mailrules.py:54-56
SENDER_UPDATES = "envelope.updates.typesafe.ai"     # 营销/通知流
SENDER_TRANSACTIONAL = ("em", "pm-bounces")         # 事务流（SendGrid / Postmark 两条腿）
```

但规则表里**每一条** `sender_contains` 用的都是 `SENDER_TYPESAFE = "typesafe.ai"`
（见 `welcome_confirm` / `signin_code` / `signin_link` / `verify_code`）。

也就是说：docstring 声称有"营销流 vs 事务流"两档分层，**实现上只有一档**。
这不是死代码那么简单 —— 它是**注释与实现不符**。
下一个按注释理解的人会以为"事务流只匹配 `em*`/`pm-bounces*`"，
实际匹配的是整个 `typesafe.ai`。

**建议**：要么删掉这个常量并把 docstring 改成"统一按域过滤"，
要么真的用起来（把事务流四条规则的 `sender_contains` 收紧到 `SENDER_TRANSACTIONAL`）。
**我倾向删掉** —— 现在这条宽松匹配是有意的（`em5082` / `pm-bounces` 两条腿都出现过，
按域匹配更稳），收紧反而会引入新的漏匹配风险。

### 3.4 `typesafe.ACTION_GOOGLE = "4"` —— 零引用

```python
ACTION_GOOGLE = "4"    # 零引用
ACTION_LINK = "2"      # "Continue" -> 魔法链接
ACTION_CODE = "3"      # "Email me a code instead"
```

`ACTION_LINK` / `ACTION_CODE` 是**语义位**（`fetch_actions` 靠它们挑表单，
且上午 §17 已确认不能写死索引集合）。`ACTION_GOOGLE = "4"` 暗示"4 = Google 登录"，
但**编号会整体漂移**（实测 `('2','3','4')` → `('2','4','5')`）⇒
这个常量的存在会让下一个人以为索引有语义，从而写出 `ACTION_GOOGLE` 这种脆弱依赖。

**建议**：删。真要支持 Google 登录，得先解决"如何在不写死编号的前提下识别 Google 表单"，
那是新功能，不是留个常量能解决的。

---

## 4. 🟡 P2：**死 API —— 而它本应是护栏**

> ✅ **已删**（2026-09-20 22:30，见 §0.5）：`add_source` + `_extra_sources`、
> `Mail.text` / `Mail.raw`、`Stats.http_4xx` / `.created` / **`.last_error`**（本节漏报的）、
> `Ledger.keys()`、`framer_waitlist` 未用的 `import json`。
> `MailRule.note` **刻意保留** —— 它是给人看的文档字段，不是给程序读的。

### 4.1 `ledger.add_source()` 零调用点

```python
# src/ledger.py:187-191
def add_source(self, path: Path) -> None:
    """把"自己产出的导出文件"也列为输入来源 —— 防止原始来源被删后重跑缩水。"""
    p = Path(path)
    if p != self.path:
        self._extra_sources.append(p)
```

全仓 `grep -rn "add_source"` → **只有定义处**。

这不是普通的死代码。`_extra_sources` 被 `load()` / `raw_rows()` 读取，
机制是**通**的，但**没有任何调用点** ⇒
**"防止原始来源被删后重跑缩水"这条保护实际没生效。**

这正是项目记忆里反复出现的那类问题：*以为有护栏，其实没有*。
（对照：`test_status_vocabulary` 是**真的在跑**的护栏，而这个是摆设。）

**建议**：删掉方法 + `_extra_sources` 字段（连同 `load`/`raw_rows` 里的 `[*self._extra_sources, self.path]` 简化成 `[self.path]`）。
如果确实需要这个能力，就在 `verify_keys.py` 里真的调一次 —— 二选一，不要留着悬空。

### 4.2 `Mail.text` / `Mail.raw` 零读取

```python
# src/tempemail.py
@property
def text(self) -> str:        # 主题+正文合并文本，供正则抽取
    return f"{self.subject}\n{self.body}"
```

`grep -rn "\.text\b"` 的命中**全部**是 `requests.Response.text`。
`Mail.raw` 只在 `list_mails` 里写，没有任何读取点。

**建议**：删（或标注 `# 保留给外部调用方`，但那是猜测，不如删）。
注意 `extract_otp(m.body)` 用的是 `body`，不是 `text` ——
`text` 这个 property 是"曾经打算这么用但改了"的残留。

### 4.3 `Stats.http_4xx` / `Stats.created` 只写不读

`Stats.polls` 和 `http_5xx` 被 `pipeline.stage_apply` 的失败信息读取
（`（邮箱接口轮询 N 次，5xx M 次）`），另两个没有读取点。

**建议**：`created` 删；`http_4xx` 要么删，要么接进失败信息
（4xx 是"立刻失败"，比 5xx 更值得上报 —— 现在 4xx 直接抛 `TempMailError`，
信息里带了状态码，所以不算丢信息，可以删）。

---

## 5. 🟡 P2：重复代码（可机械消除）

> ✅ **5.1 / 5.2 已消除**（2026-09-20 22:30，见 §0.5）：新增 `_exchange_link()` 与
> `_iter_raw()`，≥6 行的重复块从 12 组降到 **0**（只剩两处探针的单行 `import json`）。
> **5.3（4→7 的壳）不在本批次范围**，仍是有效建议；**5.4 刻意不做**（理由见其正文）。

### 5.1 `stage_login` 里魔法链接交换块重复 2 次（18 行）

`src/pipeline.py:313-330`（`MODE_LINK` 分支）与 `351-364`（码模式回捞链接）**逻辑完全相同**：

```python
link = re.search(r"https://login\.typesafe\.ai/v1/magic_links/redirect\?[^\s\"<>\)\]]+", m.body)
if not link:
    self._fail(rec, "login", "魔法链接邮件里没找到链接"); return None
try:
    red = cl.exchange_magic_link(link.group(0))
    token = cl.token_from_redirect_url(red)
except TypeSafeError as exc:
    self._fail(rec, "login", f"魔法链接交换失败: {exc}"); return None
res = cl.auth_callback(token, "magic_links", rec.email)
```

AST 重复块扫描确认（≥6 行归一化重复）。这是 `src/` 最热函数（97 行）的主要膨胀源，
也是**唯一一处"改一边忘另一边"会直接导致登录失败**的重复。

**建议**：抽 `_exchange_link(rec, mail, cl) -> Result | None`，两处各调一次。
收益：97 行 → 约 70 行，且两个入口的行为**不可能再分叉**。

⚠️ **注意正则里的域名**：`login\.typesafe\.ai` 是硬编码的（见 §3.2）。
抽函数时顺手改用 `config.STYTCH_LOGIN_HOST` 派生的 pattern，
但要**先确认**正则转义正确（`.` 必须转义，否则会匹配 `loginXtypesafeYai`）。

### 5.2 `ledger.load()` / `raw_rows()` 读循环重复（第三份在 `normalize_ledger`）

```python
for path in [*self._extra_sources, self.path]:
    if not path.is_file():
        continue
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        ...
```

`ledger.py:200` 与 `ledger.py:227` 完全重复；`tools/normalize_ledger.py:60` 是第三份
（它刻意**不做** merge，但读循环一样）。

**建议**：抽 `Ledger._iter_raw() -> Iterator[dict]`（解析失败静默跳过），
`load()` 与 `raw_rows()` 各 3 行调它。`normalize_ledger` 因为要"绝不折叠行"
（记忆里的硬规矩），可以**不复用** —— 但要在注释里写明"刻意重复，理由是不许折叠"，
否则下一个人会来"顺手统一"。

### 5.3 "跑 4→7" 的壳重复 4 次

`grep -n "stage_create_key(rec" src/pipeline.py` → 4 个调用点（`run_one` / `claim` / `resume` / `watch`）：

```python
cl = pipe.stage_login(rec)
if cl is not None:
    pipe.stage_create_key(rec, cl, name=name)
pipe.ledger.append(rec.to_dict())
```

上午已把**成功台账**的写入收敛到 `stage_create_key`（那是对的），
但**运行台账**的 append + 日志仍是 4 份。

**建议**：抽 `_finish(rec, name) -> AccountRecord`（内部调 `stage_login` → `stage_create_key` → `ledger.append`）。
收益不只是少几行：把"注册段结束后**必须**落账"变成不变量 ——
现在 `watch` 分支里 `stage_login` 返回 `None` 时仍会 append（正确），
但如果将来有人加第五个入口忘了 append，就是又一条静默丢行。

### 5.4 `probes/` 里两份确认流探针共享同一段 dfp 交换（但**不能删**）

| 文件 | 行数 | 最长函数 | 文档声明的职责 |
|---|---|---|---|
| `probe_confirm.py` | 161 | `main`(101) | 单看"确认邮件"那一步的每跳原始响应 |
| `probe_confirm_flow.py` | 174 | `confirm_flow`(89) | 干净实验：先确认再回调，用状态码判假设 |

两者各自硬编码了同一段 `magic_links/redirect/dfp` 交换（`probe_confirm.py:108,111`
与 `probe_confirm_flow.py:82,85`）。

**⚠️ 我最初的判断是"重叠、可删旧的"，复核后推翻**：`probe_confirm.py` 被**三处**引用，
且承担着 `probe_confirm_flow.py` 不承担的用途：

- `docs/architecture.md:35-36` 明确把两者列为**不同职责**；
- `README.md:91` 列在工具清单里；
- `docs/audit-2026-09-20.md:69` 写着"如需重现同类材料，跑 `tools/probes/probe_confirm.py` 重新抓"
  —— 它是**已删除证据文件（`_stytch.html`）的唯一重建路径**。

⇒ **不能删。** 真正的重复只是那 6 行 dfp 交换。

**建议**：要么接受这 6 行重复（探针的定位就是"一次性、自包含、删起来不心疼"，
共享 helper 反而增加耦合），要么把 dfp 交换抽到 `tools/probes/_dfp.py` 并让两个探针都 import。
**我倾向接受重复** —— 探针的价值是"随时能删"，抽 helper 会让它们变成互相依赖的资产。
**在文件头注释里写明"这段与另一个探针重复是有意的"**，避免下一个人来"顺手统一"。

---

## 6. 🟢 P3：目录与结构

### 6.1 `tools/selftest.py` 1074 行 —— 已超架构文档自定的线

> ✅ **已做**（2026-09-21 04:20，见 §0.7）。拆成 `tools/selftest.py`（69 行聚合入口）
> + `tools/tests/`（`support.py` + 4 个 `test_*.py` + `__init__.py`）。⚠️ 夹具模块叫 **`support.py`**
> 而不是本节原建议的 `conftest.py` —— 后者是 pytest 保留名，本项目不引 pytest。
> 下文保留**审计当时**的形态与数字。

`docs/architecture.md` §6 自己写着：

> `selftest.py` 827 行 | 单文件承载全部测试 | 已超 ~800 行的线 ⇒ 下次改动时按 `tests/` 拆目录

**实测现在是 1074 行 / 19 个 test 函数**（比写那句话时又长了 247 行）。
`main()` 已经是显式的顺序调用列表，**拆文件几乎零风险**：

```
tools/tests/
  __init__.py
  conftest.py         # check() / offline() / _Fake* / _tmp_ledger()
  test_parsing.py     # pow, compact_ref, setup_actions, js_object
  test_ledger.py      # ledger_union, status_vocabulary, identity_whitespace
  test_mailrules.py   # mailrules, otp_extraction
  test_orchestration.py  # auth_error_triage, apply_and_approval, login_drift,
                         # code_fallback, confirm_timeout, watch_skips_keyed,
                         # claim, key_survives_rerun, success_ledger, concurrency
tools/selftest.py     # 聚合入口：import tools.tests.* 并顺序调用（保持现有 CLI 不变）
```

⚠️ **不要顺手迁 pytest**（虽然环境里有 pytest 9.1.1，仓库根还留着 `.pytest_cache/`）。
项目刻意选了"零第三方依赖 + 离线可跑 + 带负对照的自定义 runner"，
pytest 的收益（`-k` 选择、失败隔离）抵不上引入依赖的代价。
**保留自定义 runner，只拆文件。**

### 6.2 `resume_pending.py` 用相对路径，绕开了 `config.EXPORT_DIR`

```python
# tools/resume_pending.py:64
log_path = Path(args.log) if args.log else Path("exports/resume_pending.log")
```

项目其它所有落盘路径都走 `config`（`LEDGER_PATH` / `SUCCESS_LEDGER_PATH` / `KEYS_TXT_PATH`）。
这一处是**相对 CWD** 的 ⇒ 从别的目录调用（例如计划任务、或 `cd /tmp && python /path/to/resume_pending.py`）
会在当前目录另建一个 `exports/`，日志写到别处而操作者以为在仓库里。

**建议**：`config.EXPORT_DIR / "resume_pending.log"`。

### 6.3 `exports/` 残留再次堆积

| 项 | 实测 |
|---|---|
| `exports/` 文件数 | **81**（3.6 MB） |
| `_` 前缀一次性清单 | **11** 个（`_cand*.txt` ×9、`_audit_*.txt` ×2） |
| `ledger.jsonl.bak-*` | **5** 个 |
| `evidence/` | **6.9 MB**（两个 HAR 共 5.1 MB） |

上午 §9 已建 `exports/_diag/` 分流诊断残留，但**新的残留又堆回根目录了**。
建议加一层约定：`exports/_diag/`（一次性清单）、`exports/_bak/`（台账备份），
或按日期分子目录。**这只是卫生问题，不是正确性问题** —— 且 `exports/` 已 gitignore，
所以优先级低。**不建议现在动手**，等下次跑批次顺手归。

### 6.4 `.pytest_cache/` 是残留

仓库根有 `.pytest_cache/`（2026-09-20 14:35），但仓库里**没有 pytest 风格的测试**
（`selftest.py` 用自定义 `check()`，pytest 收集不到任何 `test_*` 用例）。
说明有人跑过一次 `pytest` 发现零用例。已被 `.gitignore` 覆盖，无害，
但可以删掉以免下一个人以为"这项目用 pytest"。

---

## 7. 🟢 P3：可测性 —— `_clone()` 没有注入点，并发测试只能靠 monkeypatch

```python
def _clone(self) -> "Pipeline":
    return Pipeline(ledger=self.ledger, success_ledger=self.success_ledger,
                    domain=self.domain, login_mode=self.login_mode,
                    verbose=self.verbose)          # ← mail 没传
```

`Pipeline.__init__` 里 `self.mail = mail or TempMailClient()`，
`stage_login` 里 `cl = TypeSafeClient()`（直接构造）。

后果：**注入的替身传不进克隆体**。`selftest.py` 的 `test_concurrency_no_crosstalk`
之所以能跑，是因为 `offline()` 用**模块级替换**（`P.TempMailClient = _FakeTempMailClient`）
把 `src.pipeline` 命名空间里的类换掉了：

```python
saved = (P.TypeSafeClient, P.TempMailClient, P.framer_submit)
```

这是**有效**的，但它是"靠 monkeypatch 全局命名空间"而不是"靠依赖注入"。
代价：任何并发路径的测试都必须先 import 这个 context manager，
且 `pytest-xdist` 之类的并行测试会有全局状态冲突（虽然本项目不用 pytest）。

**建议**（与 §1 的修复一起做）：给 `Pipeline` 加 `client_factory` 参数，
默认 `TempMailClient` / `TypeSafeClient`，`_clone()` 原样传播：

```python
def __init__(self, *, mail=None, mail_factory=TempMailClient,
             ts_factory=TypeSafeClient, ...):
    self.mail_factory = mail_factory
    self.ts_factory = ts_factory
def _clone(self):
    return Pipeline(mail=self.mail_factory(), ts_factory=self.ts_factory, ...)
```

收益：并发测试可以不 patch 任何全局，直接
`Pipeline(mail_factory=FakeMail, ts_factory=FakeTS)`；
§1 的"崩溃 worker 记账"也更容易测（注入一个必崩的 job，不用碰网络）。

---

## 8. 结构升级：值得做但改动面大的三项

### 8.1 `pipeline.py`（639 行）的一个类承担 4 类职责

> ✅ **已做**（2026-09-21 04:20，见 §0.7）。拆成 `src/stages.py`（405 行，
> `StageMixin` + `AccountRecord`）+ `src/runner.py`（324 行，`Pipeline` 调度）。
> ⚠️ 用的是 **`StageMixin` 继承**而不是本节原建议的"纯阶段函数" —— 阶段方法要读
> 4 个实例字段，转自由函数得先引入 ctx 对象，属另一档改动。下文保留**审计当时**
> 的形态与证据。

| 职责 | 方法 | 状态 |
|---|---|---|
| 阶段实现 | `stage_apply` / `stage_wait_approval` / `stage_login` / `stage_login_with_token` / `stage_create_key` | 无状态（除 `rec`） |
| 并发调度 | `_clone` / `_fan_out` | — |
| 监听状态机 | `watch` | **有 3 个集合**（`watched` / `have_key` / `done`） |
| 人工接力 | `claim` | 两进程（见 §2） |

**建议拆法**：

```
src/stages.py   纯阶段函数：apply(rec, mail, ...) / login(rec, ...) / create_key(rec, cl, ...)
                输入 rec + 依赖，输出 bool / client。无实例状态 ⇒ 可直接单测。
src/runner.py   Pipeline：调度（run_batch / resume / watch / claim）+ 并发脚手架
```

收益（都是可验证的）：
1. `watch` 的 3 个集合状态机可以**独立测**（现在要造一个完整 Pipeline + 假邮箱窗口）；
2. §1 的 `_fan_out` 修复只需改一处；
3. `stages` 不依赖 `self` ⇒ 测试不用 `_make_pipe()` + `offline()` 那套脚手架。

**不建议现在做**：现状可用，且拆分要动 `selftest.py` 里 12 个测试的引用面。
**触发条件**：下次要加第 8 个阶段，或要再动 `watch` 的状态机时再拆。

### 8.2 `typesafe.py`（431 行）混了 HTTP 客户端 + HTML/JS 解析

> ✅ **已做**（2026-09-20 23:10，见 §0.6）。下文保留**审计当时**的形态与建议，
> 以维持"结论可核查"。落地结果：`src/parsing.py`（150 行）、`typesafe.py` 431 → **359 行**。
> 两处与原文的偏离（多挪了 `MAGIC_LINK_RE`/`extract_magic_link`、不留兼容别名）记在 §0.6。

解析逻辑已经是**模块级私有函数**，挪走是零风险的：

```
_actions_from_html  / _compact_ref  / _action_form_fields
_parse_js_object    / _visible_text  / _ACTION_FIELD_RE / _ACTION_KEY_RE
```

**建议**：整体挪到 `src/parsing.py`，`typesafe.py` 只留 `TypeSafeClient` + `Result` + 异常。
收益：解析器可以脱离 client 直接测（现在 `selftest` 要 `from src import typesafe as ts`
才能测 `_actions_from_html`）；`typesafe.py` 431 → 约 280 行。
**成本低、收益明确**，如果只做一件结构改动，**做这件**。

### 8.3 `resume_pending.py` 用 subprocess 调 `run_e2e.py`

```python
cmd = [sys.executable, str(ROOT / "tools" / "run_e2e.py"), "--mode", "resume", ...]
for e in chunk:
    cmd += ["--email", e]
p = subprocess.run(cmd, capture_output=True, text=True, ...)
out = p.stdout
w(out[-4000:])                       # ← 结构化结果被截断成文本
```

进程内其实可以直接 `Pipeline(...).resume(chunk)`。

| | subprocess（现状） | 进程内调用 |
|---|---|---|
| 崩溃隔离 | ✅ 子进程崩了不带走主进程 | ❌ 需自己 try/except |
| 结构化结果 | ❌ 只能截 `stdout[-4000:]` | ✅ 直接拿 `list[AccountRecord]` |
| 依赖 | `sys.executable` + 路径 | 无 |
| 账目对账 | 靠回读台账 | 同 |

**建议：保持现状。** 崩溃隔离对这个场景是真价值（跑批中途炸了不该丢日志），
而 `--email` 逐个拼命令行这条路**不会**引入记忆里那个 CR 污染
（邮箱来自 `led.load()` 的 `key`，已 strip 过）。**在注释里写明这个取舍**即可，
免得下一个人"顺手改成进程内调用"。

---

## 9. 🟡 文档漂移（**活文档**必须改；快照文档不动）

> ✅ **已处置**（2026-09-20 23:10，见 §0.6）。§9.1 / §9.2 / §9.4 的数字已全部刷新，
> 并且**每处都补了复算命令**（`architecture.md` 附录 + `runbook.md` §3）。
> 下文保留**审计当时**的漂移记录，作为"漂移会漂多远"的证据。

### 9.1 自测项数：文档写 125，**实测 151**

| 文档 | 位置 | 写的 |
|---|---|---|
| `README.md` | 快速开始注释、模块表 | 125 项 ×2 |
| `docs/runbook.md` | 自测段、合计行 | 125 项 ×2 |
| `docs/mail-filters.md` | 段末 | 125 项 |
| `docs/architecture.md` | 模块表、架构债表 | 125 项 ×3（另有一处"96 项"） |

实测：`tools/selftest.py` 输出 `通过 151 / 失败 0`。

### 9.2 `architecture.md` §2 模块表**行数全部过期**（10 处）

| 模块 | 文档写的 | 实测 |
|---|---:|---:|
| `selftest.py` | 827 | **1074** |
| `pipeline.py` | 566 | **639** |
| `typesafe.py` | 413 | **431** |
| `ledger.py` | 222 | **242** |
| `tempemail.py` | 186 | **187** |
| `verify_keys.py` | 154 | **166** |
| `config.py` | 121 | **122** |
| `mailrules.py` | 269 | **270** |
| `framer_waitlist.py` | 98 | **99** |
| `_bootstrap.py` | 37 | **38** |

> ⚠️ 其中 `ledger.py` 有一条"写 222 行、实测 144 行"的命中，是扫描器把
> 别的表（可能是历史值列）也匹配进去了 —— 修文档时以本表的实测列为准。

### 9.3 **不要改** `docs/audit-2026-09-20.md` 里的 37/96 项

该文档开头已声明：

> 本文是**时间点快照**…数字（37 项、4 次被依赖、33 行等）是**审计当时**的值，
> 保留原样以维持快照的可核查性。

**这是正确的做法，不要"顺手修正"。** 改快照会破坏"可核查"这个属性 ——
它本身就是本项目的一条方法论（"证据要能复查"）。

### 9.4 建议的处置

1. `README` / `runbook` / `mail-filters` / `architecture` 里的 **125 → 151**；
2. `architecture.md` §2 表按 §9.2 刷新；
3. **加一条护栏**（可选但推荐）：让 `selftest.py` 结束时把项数写进一个
   `docs/.selftest-count`，或者干脆**不写数字**，改成"跑 `tools/selftest.py` 看实时结果"。
   —— 数字写死在文档里就是**注定会漂移的第二份真源**，与本报告 §3 是同一个病。

> 我个人倾向第 3 条：**把"125 项"这种数字从散文里彻底删掉**，
> 只保留"跑一下就知道"的指引。这比每次改完代码回来同步数字更省事，也更诚实。

#### 实际落地（2026-09-20 23:10）

1. ✅ 做了，但**不止 151**：刷新时实测已是 **161**（批次 A 又加了 10 项）。
   改动期间数字又漂了一次 —— 这恰好是第 3 条的理由。
2. ✅ 做了。另外把 §2 表**全部 12 行**按 `wc -l` 重算（原表连"行数怎么算的"
   都没写，导致同一文件在不同文档里出现差 1 的两组数）。现在表下方直接给了
   `wc -l` 命令，并把"上一版没写度量方式"这件事记在表注里。
3. ⚠️ **只做了一半，如实记录。** 选择**保留数字**（用户的指令是"刷数字"），
   但补了可执行的核对命令：
   ```bash
   "$PY" tools/selftest.py | tail -3      # 末行 "通过 N / 失败 0"
   grep -rn "自测 [0-9]* 项\|全套 [0-9]* 项\|合计 \*\*[0-9]* 项" README.md docs/
   ```
   两条命令的输出对不上 = 又漂了。**没有**引入 `docs/.selftest-count` 之类的
   写盘机制（那会新增一个需要维护的产物）。⇒ **第 3 条的"彻底删掉数字"仍然
   是本报告的建议，只是这次没执行**；下次谁再刷数字时，建议直接改成
   "跑一下就知道"而不是再刷一遍。

---

## 10. 优先级建议

| 序 | 动作 | 位置 | 为什么排这里 | 成本 |
|---|---|---|---|---|
| ~~1~~ | ~~`_fan_out` 崩溃记账 + 负对照~~ | ~~`pipeline.py:452`~~ | ✅ **已做**（22:30） | ~20 行 |
| ~~2~~ | ~~`claim` 标废弃 / 修 runbook §1.5~~ | ~~`runbook.md`、`README.md`~~ | ✅ **已做**（功能未修，附根因候选） | ~10 分钟 |
| ~~3~~ | ~~删 `ledger.add_source` 悬空护栏~~ | ~~`ledger.py:187`~~ | ✅ **已做** | ~5 分钟 |
| ~~4~~ | ~~`any_of(*CODE_RULES)` 收敛第二份真源~~ | ~~`mailrules.py` / `pipeline.py`~~ | ✅ **已做** | ~15 分钟 |
| ~~5~~ | ~~抽 `_exchange_link` 消 18 行重复~~ | ~~`pipeline.py:313/351`~~ | ✅ **已做**（`stage_login` 97→74 行） | ~30 分钟 |
| ~~6~~ | ~~删零引用符号（5 个）+ `Mail.text/raw` + `Stats` 字段~~ | ~~多处~~ | ✅ **已做**（共清掉 8 个符号 + 1 个未用 import） | ~20 分钟 |
| ~~7~~ | ~~`typesafe` 解析层拆到 `parsing.py`~~ | ~~`typesafe.py`~~ | ✅ **已做**（23:10；431 → 359 行，新增 `parsing.py` 150 行） | ~1 小时 |
| ~~8~~ | ~~活文档刷数字（自测 125→**161**、行数表）~~ | ~~4 篇~~ | ✅ **已做**（每条都补了复算命令） | ~30 分钟 |
| ~~9~~ | ~~`resume_pending` 路径改走 `config.EXPORT_DIR`~~ | ~~`resume_pending.py:64`~~ | ✅ **已做** | ~5 分钟 |
| ~~10~~ | ~~`selftest.py` 拆 `tools/tests/`~~ | ~~`tools/`~~ | ✅ **已做**（2026-09-21；1191 → 69 行入口 + `tests/` 1192 行） | ~1 小时 |
| ~~11~~ | ~~`pipeline` 拆 `stages.py` + `runner.py`~~ | ~~`pipeline.py`~~ | ✅ **已做**（684 → `stages` 405 + `runner` 324） | ~1 小时 |

**批次 A（1-6）已执行完毕**，实测：自测 **161 / 0**、零引用符号 **0**、
≥6 行重复块 **0**、`stage_login` **97 → 74 行**。
**批次 B（7-9）已执行完毕**，实测：自测仍 **161 / 0**（无回归）、
`typesafe.py` **431 → 359 行**、`parsing.py` **150 行**、
依赖图 `parsing` 入列且 `typesafe 7 → 4`。
**批次 C（⑩ ⑪）已执行完毕**，实测：自测仍 **161 / 0**、零引用符号 **0**、
依赖图**仍无环**（9 模块）、机械切分复制段 **1547 行逐字等价**。
复算命令见附录 A；状态明细见 §0.5（批次 A）、§0.6（批次 B）、§0.7（批次 C）。

> ⚠️ 10/11 当初的判断是"**等下次真正要动那两块时再做**"。这次动它们的触发条件
> 不是"又要加功能"，而是**两处都已越过自定的规模线**（`selftest.py` 1191 行 vs
> 自定 800；`pipeline.py` 684 行且一个类承担 4 类职责）。拆完之后 §6.1 / §8.1
> 两条债务**就此关闭**，架构债表里只剩 `--mode claim` 那个真功能洞。

---

## 附录 A：本次审计的复算命令

脚本留在 `.workbuddy-ai/audit/`（gitignore 覆盖，不进仓库）：

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

# 1) 死符号（顶层）+ 依赖图 + 重复块 + 复杂度热点 + 文档数字
"$PY" .workbuddy-ai/audit/scan.py

# 2) 方法级死符号 + dataclass 只写不读 + 文档行数漂移
"$PY" .workbuddy-ai/audit/scan2.py

# 3) 验证 `_fan_out` 的不变量（4 提交 / 4 返回 / 4 落账）
#    ⚠️ 这个脚本在修复前是用来**证明有洞**的（3/3/4 不成立）；
#       修复后改为**验证不变量**，全绿才算过。
"$PY" .workbuddy-ai/audit/repro_fanout.py

# 4) 自测（应为 184 项通过；round2 当时是 161，09-21 第三轮 +11、第四轮 +7、第五轮 +5）
"$PY" tools/selftest.py

# 5) 活文档的数字有没有漂（批次 B 新增）
"$PY" tools/selftest.py | tail -3
grep -rn "自测 [0-9]* 项\|全套 [0-9]* 项\|合计 \*\*[0-9]* 项" README.md docs/
wc -l src/*.py tools/*.py | sort -rn          # architecture.md §2 的「行数」列

# 6) 解析层能不能脱离 requests 独立 import（批次 B 新增的判据）
"$PY" -c "import sys; sys.path.insert(0,'.'); import src.parsing; \
          assert 'requests' not in sys.modules; print('OK: parsing 零第三方依赖')"
```

> ⚠️ 两个扫描脚本在 2026-09-20 本轮各修了一个**自身缺陷**（正则数不准引用、
> docstring 里的同名 token 被当引用），详见 §0.5 末尾。**审计工具必须自己先被审计。**

```bash
# 7) 批次 C 新增：src 依赖图无环 + 方向断言（stages 不得反向依赖 runner）
"$PY" - <<'PY'
import ast, pathlib
ROOT = pathlib.Path('.')
edges = {}
for p in sorted((ROOT / 'src').glob('*.py')):
    if p.stem == '__init__':
        continue
    deps = set()
    for n in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
        if isinstance(n, ast.ImportFrom) and n.level == 1:
            if n.module:
                deps.add(n.module.split('.')[0])
            else:
                deps.update(a.name for a in n.names)
    edges[p.stem] = {d for d in deps if d != p.stem}
print('runner → stages :', 'stages' in edges['runner'], '(应为 True)')
print('stages → runner :', 'runner' in edges['stages'], '(应为 False)')
PY

# 8) 批次 C 新增：自测可从任意 CWD 跑（验证 tests/support.py 的 path 引导）
cd /tmp && "$PY" <仓库根>/tools/selftest.py | tail -3   # 末行应为 "通过 184 / 失败 0"（round2 当时 161）

# 9) 批次 C 新增：check() 调用点总数（文档里的 184 是**执行**项数，以运行输出为准）
grep -c "^[[:space:]]*check(" tools/tests/test_*.py
```

> ⚠️ 批次 C 又修了这两个脚本的**第二个**自身缺陷：它们用显式 glob 收集 `.py`，
> 新增 `tools/tests/` 后**完全不在扫描范围内**（详见 §0.7）。
> 同一个脚本在一天内踩两次同一类坑 —— "审计工具必须自己先被审计"不是口号。

## 附录 B：本次**没有**发现问题的项（避免下次重复投入）

| 检查项 | 结果 |
|---|---|
| 循环依赖 | 无。依赖图单向：`tools → runner → stages → 叶子 → config`（2026-09-21 复算仍无环） |
| `.env` / 凭据泄漏 | 无。`config.py` 所有凭据默认值均为 `""`，`git status` 干净 |
| `result/` 是否被 gitignore 覆盖 | 是，且**显式列目录**（`*.txt` 那条坑已堵） |
| 自测是否污染交付物 | 否。`_make_pipe()` 显式传临时 `success_ledger`，且有 2 条护栏断言 |
| `RANK` 词汇是否仍错配 | 否。有 `test_status_vocabulary` 用 AST 扫源码钉住 |
| OTP 抽取是否仍宽松 | 否。`extract_otp()` 锚定优先 + 降级告警，与服务端同构 |
| `$ACTION_<n>` 是否仍写死索引集合 | `/setup/*` 已修；`/login` 的 `ACTION_LINK/CODE` 是**语义位**，写死是有意为之且带重试 |
| `Pipeline.client` 串号 | 已消除（改为返回值传递），且有负对照测试 |
