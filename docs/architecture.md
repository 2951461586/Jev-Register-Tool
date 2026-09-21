# 架构：目录、模块、耦合

> 依赖关系是**用 AST 算出来的**，不是凭印象画的。
> 复算命令见文末「附录」。
>
> ⚠️ **附录那段脚本本身曾经有 bug**（漏掉 `from src import X` 这种写法），
> 导致本文的「被依赖次数」表系统性偏低。2026-09-20 已修正，并复算了全部数字。
> 教训：声明"用工具算出来的"的文档，**要复算一遍工具本身** ——
> 不复算的"可复算"承诺比不承诺更糟。

## 1. 目录结构

```
Jev-Register-Tool/
├── README.md                  总览与快速开始
├── .env / .env.example        凭据（.env 已 gitignore）
├── .gitignore                 通配排除 .env / 产物 / 证据
│
├── src/                       库代码（可被 import，不含命令行逻辑）
│   ├── __init__.py
│   ├── config.py              配置与常量、.env 加载、启动校验
│   ├── mailrules.py           收件规则表 + OTP 抽取（纯叶子，零内部依赖）
│   ├── ledger.py              JSONL 台账：并集合并 / 幂等 / 等级语义
│   ├── tempemail.py           CF Temp Email Worker 客户端
│   ├── framer_waitlist.py     Framer 表单申请（含 PoW 复刻）
│   ├── parsing.py             页面/邮件文本 → 结构（`$ACTION_*` / JS 字面量 / 魔法链接）
│   │                          **纯函数、零第三方依赖** ⇒ 可脱离 `requests` 单测
│   ├── typesafe.py            Server Action / Stytch / onboarding / 建 Key（只发 HTTP）
│   ├── stages.py              ★ 单账号阶段实现（`StageMixin`）+ `AccountRecord`
│   │                          出网动作**全在这里**：apply / login / onboarding / 建 Key
│   └── runner.py              ★ `Pipeline`：批量 / 并发 / 监听 / 人工接力 / 台账写入
│                              继承 `stages.StageMixin`；`stages` **不**反向依赖它
│
├── tools/                     入口脚本（含命令行逻辑）
│   ├── _bootstrap.py          按标记文件定位仓库根，统一 sys.path
│   ├── run_e2e.py             ★ 主入口：apply / watch / resume / claim / scan
│   ├── resume_pending.py      ★ 补跑台账里还没拿到 key 的账号（幂等）。进度计数
│   │                          **只信它** —— 走 `Ledger.load()` 合并视图，
│   │                          不是"末行胜出"（那会让重跑失败把计数压低）
│   ├── normalize_ledger.py    修被 CR / 尾部空白污染的 key、email（**不折叠行**）
│   ├── selftest.py            自测**入口**（112 行）：只做聚合与调度 + 登记完整性元检查
│   ├── tests/                 自测本体（6 个文件 1406 行，按被测对象分）
│   │   ├── __init__.py        仅为让 `tests` 可当包导入（**不是** pytest 测试包）
│   │   ├── support.py         共享夹具：`check()` 计数 + 离线替身 + 模块别名转手
│   │   ├── test_parsing.py    解析层：PoW / 紧凑 JSON / Server Action / JS 字面量
│   │   ├── test_ledger.py     台账：并集合并 / 状态词汇 / 身份字段归一化
│   │   ├── test_mailrules.py  收件规则 + OTP 抽取
│   │   └── test_orchestration.py  编排层：错误码分流 / 申请 / 登录 / 监听 / 并发
│   ├── verify_keys.py         ★ 验收：真打一次推理接口 + 导出可用凭据
│   └── probes/                一次性诊断探针（不参与主流程）
│       ├── audit_keys_against_site.py  站点侧对账：`GET /api/api-keys` vs 交付物
│       ├── probe_confirm.py          单看"确认邮件"那一步的每跳原始响应
│       ├── probe_confirm_flow.py     干净实验：先确认再回调，用状态码判假设
│       ├── probe_onboarding.py       探 /setup/* 的 Server Action 形态（--post 才发请求）
│       ├── probe_worker_health.py    Worker /health + D1 连通性（只读）
│       ├── probe_email_routing.py    查各域名在 Worker 上的收信路由（只读）
│       └── verify_bodycandidates.mjs Node 回归台：把 Worker bundle 驱动到落库那一步
│
├── docs/                      说明文档
│   ├── architecture.md        本文件
│   ├── mail-filters.md        收件过滤规则（任务交付）
│   ├── runbook.md             怎么跑 + 故障处置
│   ├── audit-2026-09-20.md    一轮审计（**时间点快照**：37/96 项，数字刻意不改）
│   └── audit-2026-09-20-round2.md  二轮审计 + 批次 A/B/C 修复状态（§0.5 §0.6 §0.7）
│
├── evidence/                  录制的证据（已 gitignore）
│   ├── *.har / *.eml          抓包与邮件原文
│   └── scraped/               抓来的第三方 bundle（framer/ js/）——
│                              性质是**录制证据**，不是导出产物
├── exports/                   运行台账与记录（已 gitignore）
│   ├── ledger.jsonl           台账（**全部**尝试，含失败的）
│   ├── run_*.json / *.log     各次运行的记录
│   └── _diag/                 一次性诊断残留（探针输出、抓下的页面快照）
│
└── result/                    ★ 交付物（已 gitignore，**只放成功的**）
    ├── success.jsonl          成功账号（append-only，按 key 并集去重）
    ├── keys.txt               email----api_key----api_key_id（明文，人可读）
    └── keys_verified.json     机器可读的验收结果
```

**分层原则**：`src/` 只放可复用的库代码，`tools/` 只放入口与一次性探针。
判断标准是"这段逻辑会不会被第二个入口复用"——会就下沉到 `src/`。

**`exports/` 与 `evidence/` 的边界**：`exports/` 只放"跑出来的结果"，
`evidence/` 只放"抓下来 / 录下来的原始材料"。抓来的第三方 JS bundle 属于后者 ——
2026-09-20 之前它们混在 `exports/js`、`exports/framer` 里。

**`exports/` 与 `result/` 的边界**：台账要留**全部**尝试（含失败的，便于复盘），
交付物只该有成功的。写入口刻意选在 `stage_create_key()` —— 那是**唯一**产出 key
的地方，挂在那里就自动覆盖全部四条路径（`run_batch` / `resume` / `watch` / `claim`），
不需要在四个调用点各写一遍（那种写法迟早漏一处）。
🔴 `result/` 必须**显式**写进 `.gitignore`：`*.txt` 没有任何通配规则覆盖，
`*.json` / `*.jsonl` 挡不住 `result/keys.txt` 里的明文 Key。

## 2. 模块规模与职责

| 模块 | 行数 | 职责 | 内部依赖 |
|---|---:|---|---|
| `config.py` | 121 | 常量集中地 + `.env` 加载 + `validate()` / `validate_cf()` 启动校验 | 无 |
| `mailrules.py` | 289 | 收件规则表 + `extract_otp()`（锚定/降级） | **无**（纯 stdlib） |
| `ledger.py` | 232 | 台账读写、并集合并、等级语义 | 无 |
| `parsing.py` | 150 | 页面/邮件文本 → 结构（`$ACTION_*` / JS 字面量 / 可见文案 / 魔法链接） | `config`（**不依赖 `requests`**） |
| `tempemail.py` | 188 | Worker 收信（索引端点、5xx 重试、计数） | `config` |
| `framer_waitlist.py` | 97 | 申请表单 + PoW | `config` |
| `typesafe.py` | 442 | 登录链路（Server Action → Stytch → 回调 → onboarding → 建 Key） | `config` + `parsing` |
| `stages.py` | 417 | ★ 单账号阶段实现（`StageMixin`）+ `AccountRecord` | `mailrules` `parsing` `typesafe` `framer_waitlist` |
| `runner.py` | 324 | ★ `Pipeline`：批量 / 并发 / 监听 / 台账写入 | `config` `ledger` `stages` `tempemail` `typesafe` |
| `run_e2e.py` | 288 | CLI（每模式一个函数，主流程只分派） | `config` `ledger` `runner` `tempemail` `mailrules` |
| `selftest.py` | 112 | 自测**入口**：按顺序调用 `tests/` 下 23 个 `test_*` + 登记完整性元检查 | `tests.*` |
| `tests/support.py` | 237 | 共享夹具：`check()` 计数 + 离线替身 + 模块别名转手 | `src.*` 全部 |
| `tests/test_parsing.py` | 143 | 解析层 4 组（PoW / 紧凑 JSON / Server Action / JS 字面量） | `support` |
| `tests/test_ledger.py` | 205 | 台账 3 组（并集合并 / 状态词汇 / 身份归一化） | `support` |
| `tests/test_mailrules.py` | 118 | 收件规则 + OTP 抽取 | `support` |
| `tests/test_orchestration.py` | 698 | 编排层 14 组（错误码分流 / 申请 / 登录 / 监听 / setup 降级 / 并发…） | `support` |
| `verify_keys.py` | 165 | 验收 + 导出 | `config` `ledger` |
| `_bootstrap.py` | 37 | sys.path 定位 | 无 |

> **行数怎么算的**：`wc -l`（即文件里 `\n` 的个数）。复算：
>
> ```bash
> wc -l src/*.py tools/*.py tools/tests/*.py | sort -rn
> ```
>
> ⚠️ 上一版这张表没有写明度量方式，于是同一个文件在不同文档里出现
> **差 1 的两组数字**（`wc -l` vs `行数 = src.count("\n") + 1`）——
> 又一个"没写清楚就没法复算"的例子。**以本命令的输出为准。**

## 3. 依赖分层（AST 实测）

```
第 3 层   入口        tools/run_e2e.py   tools/selftest.py   tools/verify_keys.py
                              │                  │                  │
                              │                  ▼
                              │      tools/tests/test_*.py ── tests/support.py
                              │                  │
第 2 层   调度        ┌───────┴──────────────────┴──────────────────┴────┐
                      │           src/runner.py（`Pipeline`）           │
                      │                  │ 继承 `StageMixin`            │
第 1.5 层 阶段        │           src/stages.py（**出网动作全在这里**） │
                      └─┬──────┬──────┬──────┬──────┬──────────────────┘
                        │      │      │      │      │
第 1 层   叶子      mailrules ledger tempemail framer  typesafe
                        │      │      │   waitlist     │
                        │      │      │      │         ▼
第 0.5 层              │      │      │      │      parsing       ← 纯函数、
                        │      │      │      │         │            零第三方依赖
第 0 层   配置        └──────┴──────┴──────┴─────────┴── src/config.py
```

**被依赖次数**（越大越底层，改动越要谨慎）：

| 模块 | 被依赖 | 说明 |
|---|---:|---|
| `config` | 15 | 常量集中地。改它要跑全量自测 |
| `ledger` | 7 | 台账唯一写入口 |
| `parsing` | 6 | 解析层。**纯函数、不依赖 `requests`** ⇒ 可以脱离网络单测 |
| `tempemail` | 6 | 收信唯一入口 |
| `typesafe` | 5 | 登录链路 |
| `mailrules` | 5 | 规则表 + OTP 抽取；**纯叶子**，可离线测 |
| `runner` | 4 | CLI + 自测 + 探针（原 `pipeline` 的调度半边） |
| `tests/support.py` | 4 | 自测共享夹具（4 个 `test_*` 模块都从这里取） |
| `stages` | 3 | 阶段层；被 `runner` + 探针引用 |
| `framer_waitlist` | 3 | 申请 |

> 复算命令见文末附录（`from src import X` 那种写法必须单独计 —— 旧版脚本就漏在这里）。
> ⚠️ 该表是**脚本输出**，加/删任何 `.py` 文件后**必须重跑**，否则立刻过期。
> 本次（`pipeline` 拆 `runner` + `stages`、自测拆 `tools/tests/` 后）实测：
> `config 15 / ledger 7 / parsing 6 / tempemail 6 / typesafe 5 / mailrules 5 /
> runner 4 / support 4 / stages 3 / framer_waitlist 3`。
> 其中 `typesafe 7 → 4` 是解析层拆分带来的**预期内下降**（三个探针原本从
> `src.typesafe` 导入 `_parse_js_object` 等私有名，现在改从 `src.parsing` 导入）；
> 这次 `typesafe 4 → 5` 是因为 `runner` 与 `stages` **各自**导入它，属正常。
> `pipeline 6` 拆成了 `runner 4 + stages 3`（有重叠，因为 `stages` 也被探针直引）。

> 上表是**修正后**的数字。历史：旧版表格（config=4 / typesafe=3 / framer=2 /
> mailrules=2）是错的 —— 附录脚本用 `startswith('src.')` 判断，而
> `from src import config` 的 `module` 恰好是 `"src"`（**不带点**），
> 所以 `tools/*` 的顶层导入**全被漏计**。2026-09-20 晚新增 3 个探针后复算到
> `config 11 / typesafe 7 / pipeline 4`；解析层拆分后是
> `config 15 / ledger 7 / parsing 6 / tempemail 6 / pipeline 6 / mailrules 5 /
> typesafe 4 / framer 3`（当时 `pipeline` **尚未**拆分）；批次 C 把 `pipeline`
> 拆成 `runner` + `stages` 之后，才是上表这一组。
> ⇒ **加/删文件后必须重跑附录脚本**，否则这张表立刻过期。

**依赖方向是单向的**：`tools → runner → stages → 叶子 → config`。
没有任何叶子反向依赖编排层，也没有循环 —— `stages` **不** import `runner`
（`AccountRecord` 刻意放在 `stages` 就是为了让这个方向成立）。

**两个"能脱离网络测"的模块**（都只用 stdlib）：

- `mailrules.py` —— **零内部依赖 + 零第三方依赖**（只用 `unicodedata` / `re`），
  可以被任意层安全引用，不会带进 `requests` 或配置副作用；
- `parsing.py` —— 零第三方依赖，只依赖 `config`。里面的函数都是纯变换，
  所以 `python -c "from src import parsing"` **不需要 `requests` 装好就能跑**
  （这是拆分解析层的实际收益之一，不是审美）。

**第三方依赖只有 `requests` 一个。**
刻意不用 `bs4` / `lxml`（本机 venv 里也没有），HTML 解析一律 stdlib `re` +
`unicodedata`——少一个依赖就少一处会在别的机器上炸的地方。

## 4. 数据流

```
                ┌─ 阶段 1-2 申请 + 确认 ────────────────────────────────┐
                │                                                        │
 建临时邮箱 ──▶ Framer 表单(PoW) ──201──▶ 等 waitlist_confirm 邮件        │
   tempemail      framer_waitlist              tempemail + mailrules     │
                └────────────────────────────────────────────────────────┘
                                        │
                ┌─ 阶段 3 获批（外部，批量定时 ~25 分钟）────────────────┐
                │   account_ready 邮件落地 ──▶ 解锁下游                  │
                │   ★ 只能等；窗口仅 ~37 分钟 ⇒ 必须 --mode watch 边到边取 │
                └────────────────────────────────────────────────────────┘
                                        │
                ┌─ 阶段 4-7 注册 → onboarding → 建 Key → 入库 ──────────┐
                │                                                        │
  /login 抓 $ACTION_* ──▶ 发码 ──▶ 收码 ──▶ /api/auth/callback          │
     typesafe          typesafe  mailrules     typesafe                  │
                                 .extract_otp                            │
                                        │                                │
                    /api/me ──▶ /setup/tos → set-name → console-survey   │
                                        │                                │
                          POST /api/api-keys ──▶ apikey_xxx              │
                                        │                                │
                                  ledger.append()  ──▶ exports/ledger.jsonl
                                        │            （全部尝试，含失败的）
                                        └── success_ledger.append() ──▶ result/success.jsonl
                └────────────────────────────────────────────────────────┘
                                        │
                ┌─ 验收（独立于链路）────────────────────────────────────┐
                │  verify_keys.py ──▶ POST api.typesafe.ai/v1/systemone  │
                │                    真打一次推理，导出 result/keys.txt    │
                └────────────────────────────────────────────────────────┘
```

**取码走 `mailrules.extract_otp()`**（锚定优先、宽松降级），
而不是在 `stages` 里就地写正则 —— 与服务端 D1 规则 id=20 同构，见 §5。

## 5. 关键耦合点（改一处要连带看哪里）

| 耦合点 | 位置 | 连带影响 |
|---|---|---|
| **邮件文案** | `mailrules.RULES` | 站点改文案 ⇒ 规则不中。`--mode scan` 的漏网主题是唯一信号 |
| **发件人域** | `mailrules.SENDER_*` | 站点换 ESP ⇒ 全部规则失效（症状同上） |
| **OTP 模板句** | `mailrules.OTP_ANCHORED_RE` | 站点改模板 ⇒ 锚定失配 ⇒ 走降级路径（**日志会明确写出来**） |
| **Server Action 渲染形态** | `parsing.actions_from_html` | 🔴 页面结构变 ⇒ **`/setup/*` 会静默退化到陈旧 fallback ⇒ 404**（2026-09-20 实翻车，见下）。`/login` 有 `fetch_actions` 显式校验索引 2/3，所以它抛错 |
| **`$ACTION_<n>` 的索引集合** | 同上 | 🔴 **不许写死**。`/login` 用 2/3/4，`/setup/*` 用 1。写死枚举 + 要求 `:2` 同时存在 ⇒ 新形态一条都抓不到 |
| **`$ACTION_<n>:0` 必须紧凑 JSON** | `parsing.compact_ref` | 加空格 ⇒ Next.js 直接 500。自测有负对照钉住 |
| **`/api/auth/callback` 请求体键集** | `typesafe.auth_callback` | 🔴 站点是 **strict** schema：多一个未知键 ⇒ **全员**登录 `400 Bad request`（含已获批账号），而文案毫无指向性。2026-09-21 实测 —— 删掉站点已废弃的 `waitlistEmail` 才恢复（邮箱改由 token/session 在服务端推导）。键集由 `test_auth_callback_payload_shape` 逐键钉住 |
| **Framer PoW 常量** | `config.POW_*` | 站点调难度 ⇒ 申请被拒。常量取自 JS，不是试出来的 |
| **`/setup/*` action id** | `typesafe.FALLBACK_SETUP_ACTIONS` | 🔴 仅降级用，**会随部署失效**。2026-09-20 实测该表里的 id 已全部作废（POST 回 `404 Server action not found.`）。首选永远是运行时抓隐藏域。**2026-09-21 起**：走降级必打告警，失败时 `error` 指向真因（不再只回 `HTTP 404`）—— 见 §6 与 `test_post_setup_degrade_is_observable` |
| **`/setup/*` 的渲染依赖会话状态** | `typesafe.fetch_setup_actions` | 🔴 同一会话里 POST 之后，`/setup/console-survey` **不再渲染** `$ACTION_*`（页面 39490 → 40507 字节），且**轮询 62s 不恢复**；**换一个新会话立刻恢复**。⇒ onboarding 的最后一跳只能由**下一次登录**补上（`resume_pending.py` 默认 `--rounds 3`；`--rounds 1` 实测漏 76%）。另一半（POST 报错后回读 `/api/me` 判成功）由 `test_onboarding_merged_submit_is_not_a_failure` 钉住 |
| **台账状态等级** | `ledger.RANK` | 决定升级/降级语义。**必须覆盖 `stages` 写的每个 status**，由 `test_status_vocabulary` 用 AST 钉住（扫描面 `src/*.py` + `tools/**/*.py`） |
| **台账身份字段** | `ledger.EARNED_FIELDS` | 决定"哪些字段不许被空值覆盖"（`api_key` 等） |
| **推理端点** | `verify_keys.API_URL` | 站点换端点 ⇒ 验收误判为"key 不可用" |

**已刻意解耦的地方**：

- `mailrules` 不 import `tempemail` —— 它用鸭子类型读 `.sender` / `.subject`，
  所以自测里能用 3 行的假对象测规则，不需要起 HTTP。
- `parsing` 不 import `typesafe`，`typesafe` 单向 import `parsing` ——
  解析器因此**不经过 client 的命名空间**，也不需要 `requests` 就能 import。
- `stages` 不自己拼 subject 子串，也**不自己写取码正则** —— 全部走 `mailrules`。
- 探针放 `tools/probes/` 且不参与主流程 —— 诊断代码不会成为主链路的负担。
- 并发时每个 worker 一个独立 `Pipeline` 实例，**不共享会话对象**；
  唯一共享的是带锁的 `Ledger`。

## 6. 已知的架构债

| 项 | 现状 | 建议 |
|---|---|---|
| ~~`ledger.RANK` 词汇与编排层不一致~~ | **2026-09-20 已修**（曾导致重跑失败清空 `api_key`） | 已加 AST 词汇覆盖测试，不会复发。⚠️ 该测试的扫描面用 `rglob`：自测拆进 `tools/tests/` 后，`tools/*.py` 这个 glob 匹配不到子目录（见 §2 注） |
| ~~`config.py` 有收件规则的第二份真源~~ | **已修**：删掉 5 个零引用常量 | — |
| ~~`QuotaLedger` 整类无调用点~~ | **已修**：删除 67 行 | — |
| ~~无并发~~ | **已加** `--concurrency`（申请段/注册段） | `watch` 刻意保持串行 |
| ~~`pipeline.py` 零测试~~ | **已补** 184 项自测（审计当时 96 项） | 继续加边界用例。测试本体现已在 `tools/tests/` |
| ~~`typesafe.py` 混了 HTTP 客户端 + HTML/JS 解析~~ | **2026-09-20 已拆**：解析层独立成 `src/parsing.py`，`typesafe.py` 413 → **359** 行，只留 HTTP | — |
| ~~`pipeline.py` 684 行，阶段方法 + 并发脚手架挤在一个类~~ | **2026-09-20 已拆**：`src/stages.py`（417 行，阶段 + `AccountRecord`）+ `src/runner.py`（324 行，调度）。⚠️ 用的是 `StageMixin` 而不是报告原建议的自由函数 —— 阶段方法要读 `self.mail` / `self.login_mode` / `self.success_ledger` / `self.log`，转自由函数得先引入 ctx 对象，属另一档改动 | 若再膨胀，先拆 `runner.watch`（66 行） |
| ~~`selftest.py` 1155 行，单文件承载全部测试~~ | **2026-09-20 已拆**：入口 `selftest.py` 112 行 + `tools/tests/` 6 个文件 1406 行（四个 `test_*.py` + `support.py` 夹具 + `__init__.py`）。⚠️ **刻意不叫 `conftest.py`、不引 pytest** | 加新测试就落到对应 `test_*.py`，**并在 `selftest.py` 的 `main()` 里登记** —— 漏登记会被 `_registry_gap()` 当场拦下（2026-09-21 加） |
| ~~`FALLBACK_SETUP_ACTIONS` 是**假护栏**~~ | **2026-09-21 已修**：`post_setup()` 的降级通路以前 `except TypeSafeError: acts = {}` **静默**退化到一张已知全部作废的 action id 表（文档三处写明），于是"站点改版"最终只表现为 `onboarding 失败: HTTP 404`。现在降级必打可辨识告警、失败时 `error` 点出真因 + 给下一步 | 兜底表本身仍会随部署失效（这是它的性质）。重录 HAR 拿到新 id 后，要同步 `typesafe.py` 的注释与 runbook §4.6 |
| ~~`auth_callback` 请求体多传 `waitlistEmail`（站点已收紧 schema）~~ | **2026-09-21 已修**：站点把 `/api/auth/callback` 的 schema 收紧成 **strict**，多一个未知键直接 400。当时**所有**账号登录全灭（含 122 个已获批、早已拿到 key 的），而错误文案只有一句 `HTTP 400: Bad request` —— 排查会被引向"验证码错/白名单"，方向完全相反。已删该键（邮箱改由 token/session 在服务端推导），并让 `_fail_auth` 特判 Zod 的 `Unrecognized key`、把真因与处置直接写进 `error` | 这类"站点收紧请求体"**没有通用护栏** —— 只能靠我们自己维护的键集测试（`test_auth_callback_payload_shape`）。下次见到 `400 Bad request`，先跑 `tools/probes/audit_keys_against_site.py` 分清"回调坏了"还是"未获批"（runbook §4.8） |
| ~~`complete_onboarding` 把"POST 报错"直接当成失败~~ | **2026-09-21 已修**：站点把三步合成同一张表单、而 `/api/me` **读有滞后** ⇒ 多发的那次 survey POST 必然 404，于是**账号明明已完全 onboard 却被记成 `partial`**（实跑 25 个里误报 **19 个**）。现在 POST 报错后**回读 `/api/me`**：缺口关了就是成功，并往 `log` 留痕 | 另一半是站点侧行为（**最后一跳在同一会话里做不到**，换会话才行），只能靠下一轮补 —— 见 runbook §4.9。这类问题没有通用护栏，判据只能写进文档 |
| `--mode watch` 与 `resume` 有重复 | 都做"跑 4→7" | 已抽 `stage_login` + `stage_create_key`，重复的只是循环壳 |
| `--mode claim` 两进程设计**已知失效** | 标了废弃但**功能没修** | 根因候选与验证法见 `runbook.md` §1.5；未实测前不许记为"已修" |

## 附录：复算依赖图

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
"$PY" - <<'EOF'
import ast
from pathlib import Path
from collections import Counter

cnt = Counter()
for p in sorted(list(Path('src').glob('*.py')) + list(Path('tools').glob('*.py'))
                + list(Path('tools/probes').glob('*.py'))):
    for n in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
        if isinstance(n, ast.ImportFrom):
            if n.level and n.module:          # from .ledger import X
                cnt[n.module] += 1
            elif n.level:                     # from . import config
                for a in n.names:
                    cnt[a.name] += 1
            elif n.module == 'src':           # ★ from src import config
                for a in n.names:             #   module 恰为 "src"（不带点），
                    cnt[a.name] += 1          #   旧版脚本就是在这里漏计的
            elif (n.module or '').startswith('src.'):   # from src.ledger import X
                cnt[n.module.split('.')[1]] += 1
print(cnt.most_common())
EOF
```

**复算行数（§2 表的「行数」列）**：

```bash
wc -l src/*.py tools/*.py tools/tests/*.py | sort -rn
```

**复算自测项数（§2 / §1 里写的项数）**：

```bash
"$PY" tools/selftest.py | tail -3      # 末行 "通过 N / 失败 0"
```

**改这段脚本后要顺手复算本文 §2 / §3 的表格** —— 表格是脚本的输出，
不是独立的事实。同理，改了 `tools/tests/` 里的检查项数量，
要同步 `README.md` / `docs/runbook.md` / `docs/mail-filters.md` 里写的项数：

```bash
# ⚠️ 模式必须覆盖**两种语序**：README 写「自测 184 项」，本文 §6 写「已补 184 项自测」。
#    旧版只匹配前一种 ⇒ 本文自己的数字从来没被这条命令核对过（2026-09-21 发现并补上）。
grep -rn "自测 [0-9]* 项\|[0-9]* 项自测\|全套 [0-9]* 项\|合计 \*\*[0-9]* 项" README.md docs/
```

> ⚠️ **`docs/audit-2026-09-20.md` 里的 37 项 / 96 项不要改** ——
> 那是**刻意保留的时间点快照**，改了会破坏它"可核查"的属性。
