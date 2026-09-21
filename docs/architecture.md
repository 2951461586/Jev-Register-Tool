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
│   ├── remail.py              Remail 聚合客户端（**第二个邮箱后端**）。
│   │                          接口刻意对齐 `tempemail`（`create_mailbox` /
│   │                          `list_mails` / `wait_for_mail`）⇒ `stages` 层无感切换；
│   │                          复用它的 `Mail` / `Stats`，但**异常类型独立**
│   │                          （`RemailError` 不继承 `TempMailError`）
│   ├── parsing.py             页面/邮件文本 → 结构（`$ACTION_*` / JS 字面量 / 魔法链接）
│   │                          **纯函数、零第三方依赖** ⇒ 可脱离 `requests` 单测
│   ├── typesafe.py            Server Action / Stytch / onboarding / 建 Key（只发 HTTP）
│   ├── stages.py              ★ 单账号阶段实现（`StageMixin`）+ `AccountRecord`
│   │                          出网动作**全在这里**：login / onboarding / 建 Key
│   └── runner.py              ★ `Pipeline`：批量 / 并发 / 补跑 / 台账写入
│                              继承 `stages.StageMixin`；`stages` **不**反向依赖它
│                              另有邮箱后端工厂 `make_mail_client(backend)`
│
├── tools/                     入口脚本（含命令行逻辑）
│   ├── _bootstrap.py          按标记文件定位仓库根，统一 sys.path
│   ├── run_e2e.py             ★ 主入口：full / resume / scan（+ --doctor）
│   ├── relogin_pending.py     对没拿到 key 的地址**重新发一封**确认邮件，
│   │                          并把历史链接一起用（魔法链接 7 天有效）
│   ├── resume_pending.py      ★ 补跑台账里还没拿到 key 的账号（幂等）。进度计数
│   │                          **只信它** —— 走 `Ledger.load()` 合并视图，
│   │                          不是"末行胜出"（那会让重跑失败把计数压低）
│   ├── normalize_ledger.py    修被 CR / 尾部空白污染的 key、email（**不折叠行**）
│   ├── selftest.py            自测**入口**（151 行）：只做聚合与调度 + 登记完整性元检查
│   ├── tests/                 自测本体（6 个文件 2463 行，303 项，按被测对象分）
│   │   ├── __init__.py        仅为让 `tests` 可当包导入（**不是** pytest 测试包）
│   │   ├── support.py         共享夹具：`check()` 计数 + 离线替身 + 模块别名转手
│   │   ├── test_parsing.py    解析层：紧凑 JSON / Server Action / JS 字面量 / HTML 实体
│   │   ├── test_ledger.py     台账：并集合并 / 状态词汇 / 身份字段归一化
│   │   ├── test_mailrules.py  收件规则 + OTP 抽取
│   │   └── test_orchestration.py  编排层：错误码分流 / 登录 / 门禁 / 并发 / 邮箱后端选择
│   ├── verify_keys.py         ★ 验收：真打一次推理接口 + 导出 4 份交付物
│   ├── check_deliverables.py  ★ **独立复核**那 4 份（只读，退出码 0/1 可当门禁）
│   └── probes/                一次性诊断探针（不参与主流程）
│       ├── audit_keys_against_site.py  站点侧对账：`GET /api/api-keys` vs 交付物
│       ├── probe_gate_chain.py       ★ 逐跳走 onboarding 门禁，打完整链路 + 试建 key
│       ├── probe_confirm.py          单看"魔法链接"那一步的每跳原始响应
│       ├── probe_login_shape.py      /login 页渲染形态（只 GET，不发信）
│       ├── probe_new_login_flow.py   发信 → 收信：凭据到底是链接还是码
│       ├── probe_full_new_flow.py    一次跑完（自建邮箱，不依赖台账）
│       ├── probe_onboarding.py       探 /setup/* 的 Server Action 形态（--post 才发请求）
│       ├── probe_onboarding_state.py /probe_onboarding_state2.py  门禁页形态快照
│       ├── probe_worker_health.py    Worker /health + D1 连通性（只读）
│       ├── probe_email_routing.py    查各域名在 Worker 上的收信路由（只读）
│       ├── probe_remail.py           ★ Remail 诊断：**库存 + 成本 + 凭证台账**（只读，不下单）
│       └── verify_bodycandidates.mjs Node 回归台：把 Worker bundle 驱动到落库那一步
│
├── docs/                      说明文档
│   ├── architecture.md        本文件
│   ├── mail-filters.md        收件过滤规则（任务交付）
│   ├── runbook.md             怎么跑 + 故障处置
│   ├── optimization-2026-09-21.md  性能优化实测（并发/超时/重发 + §7.4 Remail 接入）
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
    ├── success.jsonl          成功**账号**（append-only，按邮箱去重 ⇒ 每邮箱一行）
    │                          ⚠️ 账号级：同账号的第二把 key 会被合并；凭据级看 keys.txt
    ├── keys.txt               email----api_key----api_key_id（明文，人可读）
    ├── keys_verified.json     机器可读的验收结果
    ├── apikeys.txt            纯 api_key，一行一个（keys.txt 的**无元数据版**）
    │                          ⚠️ 与 keys.txt 由**同一次** verify 写出 ⇒ 两者集合恒等
    └── remail_orders.jsonl    ⚠️ **不是交付物**：Remail 的 per-order 取件凭证
                               （`serviceToken`）。跨进程补跑取件靠它，别当垃圾清掉
```

**分层原则**：`src/` 只放可复用的库代码，`tools/` 只放入口与一次性探针。
判断标准是"这段逻辑会不会被第二个入口复用"——会就下沉到 `src/`。

**`exports/` 与 `evidence/` 的边界**：`exports/` 只放"跑出来的结果"，
`evidence/` 只放"抓下来 / 录下来的原始材料"。抓来的第三方 JS bundle 属于后者 ——
2026-09-20 之前它们混在 `exports/js`、`exports/framer` 里。

**`exports/` 与 `result/` 的边界**：台账要留**全部**尝试（含失败的，便于复盘），
交付物只该有成功的。写入口刻意选在 `stage_create_key()` —— 那是**唯一**产出 key
的地方，挂在那里就自动覆盖全部两条路径（`run_batch` / `resume`），
不需要在两个调用点各写一遍（那种写法迟早漏一处）。
🔴 `result/` 必须**显式**写进 `.gitignore`：`*.txt` 没有任何通配规则覆盖，
`*.json` / `*.jsonl` 挡不住 `result/keys.txt` 里的明文 Key。

## 2. 模块规模与职责

| 模块 | 行数 | 职责 | 内部依赖 |
|---|---:|---|---|
| `config.py` | 176 | 常量集中地 + `.env` 加载 + `validate()` / `validate_cf()` / `validate_remail()` 启动校验 | 无 |
| `mailrules.py` | 288 | 收件规则表 + `extract_otp()`（锚定/降级） | **无**（纯 stdlib） |
| `ledger.py` | 244 | 台账读写、并集合并、等级语义 | 无 |
| `parsing.py` | 205 | 页面/邮件文本 → 结构（`$ACTION_*` / JS 字面量 / 可见文案 / 魔法链接） | `config`（**不依赖 `requests`**） |
| `tempemail.py` | 186 | CF Worker 收信（索引端点、5xx 重试、计数） | `config` |
| `remail.py` | 389 | Remail 聚合收信（**第二个后端**：下单 / 取件 / 取全文 / 凭证落盘） | `config` `tempemail`（复用 `Mail` `Stats`） |
| `typesafe.py` | 558 | 登录链路（Server Action → Stytch → 回调 → onboarding → 建 Key） | `config` + `parsing` |
| `stages.py` | 575 | ★ 单账号阶段实现（`StageMixin`）+ `AccountRecord` | `mailrules` `parsing` `typesafe` |
| `runner.py` | 359 | ★ `Pipeline`：批量 / 并发 / 补跑 / 台账写入 + 邮箱后端工厂 | `config` `ledger` `remail` `stages` `tempemail` |
| `run_e2e.py` | 271 | CLI（每模式一个函数，主流程只分派） | `config` `ledger` `runner` `stages` `tempemail` |
| `selftest.py` | 151 | 自测**入口**：按顺序调用 `tests/` 下 32 个 `test_*` + 登记完整性元检查 | `tests.*` |
| `tests/support.py` | 332 | 共享夹具：`check()` 计数 + 离线替身 + 模块别名转手 | `src.*` 全部 |
| `tests/test_parsing.py` | 225 | 解析层 5 组（紧凑 JSON / Server Action / JS 字面量 / HTML 实体 / 残缺候选） | `support` |
| `tests/test_ledger.py` | 353 | 台账 4 组（并集合并 / 状态词汇 / 身份归一化 / key 验收重试网络层 + 交付物自证） | `support` |
| `tests/test_mailrules.py` | 105 | 收件规则 + OTP 抽取 | `support` |
| `tests/test_orchestration.py` | 1443 | 编排层 21 组（错误码分流 / 登录 / 链接候选 / 门禁 / setup 降级 / 并发 / 邮箱后端 / 工具只读护栏…） | `support` |
| `verify_keys.py` | 236 | 验收 + 导出（`keys.txt` / `apikeys.txt` / `keys_verified.json`） | `config` `ledger` |
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
>
> 🔴 `src/__init__.py`（40 行）与 `tests/__init__.py`（5 行）刻意**不进表**：
> 它们是包声明与模块清单，没有"职责"可言，放进来只会稀释这张表的信号。
> 复算总行数时要把它们算上（`wc -l` 会算），所以**总行数与表内之和本来就不等**。

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
                      └─┬──────┬──────┬──────┬─────────────────────────┘
                        │      │      │      │
第 1 层   叶子      mailrules ledger tempemail typesafe
                        │      │      │         │
                        │      │      │         ▼
第 0.5 层              │      │      │      parsing       ← 纯函数、
                        │      │      │         │            零第三方依赖
第 0 层   配置        └──────┴──────┴─────────┴── src/config.py

并列叶子   remail ──(只复用 `Mail` / `Stats`)──▶ tempemail
           ⚠️ 两个邮箱客户端**互不引用**，只共享数据结构；异常类型也各自独立
```

**被依赖次数**（越大越底层，改动越要谨慎）：

| 模块 | 被依赖 | 说明 |
|---|---:|---|
| `config` | 19 | 常量集中地。改它要跑全量自测 |
| `parsing` | 12 | 解析层。**纯函数、不依赖 `requests`** ⇒ 可以脱离网络单测 |
| `tempemail` | 11 | CF 收信唯一入口（`remail` 也复用它的 `Mail` / `Stats`） |
| `typesafe` | 11 | 登录链路（阶段层与 4 个探针都直引） |
| `ledger` | 7 | 台账唯一写入口 |
| `runner` | 7 | CLI + 自测 + 探针（原 `pipeline` 的调度半边） |
| `stages` | 5 | 阶段层；被 `runner` + CLI + 探针引用 |
| `mailrules` | 5 | 规则表 + OTP 抽取；**纯叶子**，可离线测 |
| `tests/support.py` | 5 | 自测共享夹具（5 个 `test_*` 模块都从这里取） |
| `remail` | 3 | **第二个邮箱后端**；被 `runner` + 自测引用 |

> 复算命令见文末附录（`from src import X` 那种写法必须单独计 —— 旧版脚本就漏在这里）。
> ⚠️ 该表是**脚本输出**，加/删任何 `.py` 文件后**必须重跑**，否则立刻过期。
>
> 🔴 **2026-09-21 修的一处口径不一致**：附录脚本只收
> `src/ + tools/ + tools/probes/`，**不含 `tools/tests/`**，而这张表里一直有
> `support`（它只被 `tools/tests/` 里的模块 import）。也就是说
> **复算命令复算不出表里的数字** —— 表里的 `support` 项在脚本输出里根本不会出现。
> 现在两边都含 `tools/tests/`，命令与表**逐项对齐**（实测
> `config 19 / parsing 12 / tempemail 11 / typesafe 11 / ledger 7 / runner 7 /
> stages 5 / mailrules 5 / support 5 / remail 3`）。
>
> 历史沿革（数字都是当时实测的）：旧版表格（`config=4 / typesafe=3 / framer=2 /
> mailrules=2`）是错的 —— 附录脚本用 `startswith('src.')` 判断，而
> `from src import config` 的 `module` 恰好是 `"src"`（**不带点**），
> 所以 `tools/*` 的顶层导入**全被漏计**。2026-09-20 晚修正后复算到
> `config 11 / typesafe 7 / pipeline 4`；解析层拆分后是
> `config 15 / ledger 7 / parsing 6 / tempemail 6 / pipeline 6 / mailrules 5 /
> typesafe 4 / framer 3`；批次 C 把 `pipeline` 拆成 `runner` + `stages`。
> `framer_waitlist` 这一项随邀请制取消**整模块删除**（2026-09-21）。

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

> 🔴 **2026-09-21：邀请制取消，链路从 7 段缩到 4 段。**
> 删掉的三段（~~建邮箱 + 投 Framer 申请~~ / ~~等 waitlist 确认回执~~ /
> ~~等获批邮件~~）都是"为等待而存在"的，站点侧现在**一步到位**：
> 发一封 `Welcome to TypeSafe — confirm your email`，里面的**魔法链接就是登录凭据**。
> 判据是两条站点侧实测：`POST /login` 直接回 `x-action-redirect: /login?sent=true`
> （不再有 `?waitlist=` 参数），且收到的邮件主题就是上面那句。

```
                ┌─ 阶段 1 注册 + 登录（同一步，无外部阻断点）─────────────┐
                │                                                        │
 建临时邮箱 ──▶ POST /login ──▶ 收 "confirm your email" ──▶ 魔法链接交换  │
   tempemail     typesafe           tempemail + mailrules      typesafe  │
   (stage_login 在 email 为空时自建)      .extract_magic_link              │
                │                       ──▶ POST /api/auth/callback      │
                │                            200 ⇒ 会话建立（cookie 落在 client 上）
                └────────────────────────────────────────────────────────┘
                                        │
                ┌─ 阶段 2-4 onboarding → 建 Key → 入库 ─────────────────┐
                │                                                        │
                │  GET /hook（不跟随重定向）──▶ 门禁链（**引导**，非门槛）│
                │    tos ─▶ set-name ─▶ console-survey（我们提交不了）    │
                │     ▲ 站点自己都不稳定：相邻两次 GET 可给出不同答案     │
                │                                                        │
                │  POST /api/api-keys ──▶ apikey_xxx   ← ★ 真判据在这里  │
                │                                        │                │
                │        ledger.append()  ──▶ exports/ledger.jsonl        │
                │                           （全部尝试，含失败的）        │
                │        success_ledger.append() ──▶ result/success.jsonl │
                └────────────────────────────────────────────────────────┘
                                        │
                ┌─ 验收（独立于链路）────────────────────────────────────┐
                │  verify_keys.py ──▶ POST api.typesafe.ai/v1/systemone  │
                │                    真打一次推理，导出 result/keys.txt    │
                └────────────────────────────────────────────────────────┘
```

🔴 **"这个账号成不成"的判据只有一条：`POST /api/api-keys` 有没有回 key。**
`/hook` 的重定向链是**引导**（2026-09-21 实测：门禁停在 `console-survey` 时，
建 key 照样 200 + 明文 key），而且它**本身非确定性**（同一次运行里相邻两次
`GET /hook` 会给出不同答案，实测见 `tools/probes/probe_gate_chain.py`）。
判据历史上换过三次（`/api/me` 字段 → `/hook` 归零 → 建 key 结果），
每次怎么错的都留在 `typesafe.complete_onboarding()` 的文档里。

**取码走 `mailrules.extract_otp()`**（锚定优先、宽松降级），
而不是在 `stages` 里就地写正则 —— 与服务端 D1 规则 id=20 同构，见 §5。
⚠️ 取码路径是**兼容分支**：主路径拿到的是魔法链接（`link` 模式，也是 CLI 默认）。
站点对同一次发信请求**可能回链接而不是码**（实测 4 次里 1 次），所以码模式下
等不到码时必须回捞一次链接 —— 见 `test_code_mode_falls_back_to_magic_link`。

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
| **`/setup/*` action id** | `typesafe.FALLBACK_SETUP_ACTIONS` | 🔴 仅降级用，**会随部署失效**。2026-09-20 实测该表里的 id 已全部作废（POST 回 `404 Server action not found.`）。首选永远是运行时抓隐藏域。**2026-09-21 起**：走降级必打告警，失败时 `error` 指向真因（不再只回 `HTTP 404`）—— 见 §6 与 `test_post_setup_degrade_is_observable` |
| **onboarding 门禁链** | `typesafe.onboarding_gate` / `KNOWN_ONBOARDING_GATES` | 🔴 站点**随时增删步骤**，而且**自己都不稳定**：实测相邻两次 `GET /hook` 给出不同答案（`set-name` 与 200 交替）。⇒ ① 遇到不认识的步骤**只记录不判失败**（`console-survey` 就是这种：它的页面没有 `$ACTION_*` 隐藏域，我们提交不了）；② 同一跳重复出现就停，别空转；③ 判据一律不放在这里。改站点后跑 `tools/probes/probe_gate_chain.py` 看完整链路 |
| **确认邮件超时阈值 + 批内重发** | `stages.MAIL_TIMEOUT` / `RETRY_SEND_ON_TIMEOUT` / `MAX_SEND_RETRIES` | 🔴 阈值是**针对某个延迟分布**的，站点改了发信节奏它就该变。2026-09-21 实测延迟 `P50 3.26s / max 4.57s` ⇒ 从 300s 下调到 **60s**。**阈值与重发是一个改动**：只降阈值不重发 ⇒ 偶发慢邮件被直接判死，成功率**反降**。两条都由 `test_mail_timeout_headroom` 钉住（含"真的重发了"的行为验证：数发信次数）。⚠️ 旧依据"实测延迟爬升到 198s"**已被证伪**（那是批量发的 waitlist 回执），别再引回去 |
| **台账状态等级** | `ledger.RANK` | 决定升级/降级语义。**必须覆盖 `stages` 写的每个 status**，由 `test_status_vocabulary` 用 AST 钉住（扫描面 `src/*.py` + `tools/**/*.py`）。⚠️ 还要覆盖**历史词汇**（`applied` / `confirmed` / `approved` / `code_sent`）—— 它们不再被写入，但历史台账里有，删掉会让那些行 `rank()` 落 0 分，任何一次重跑都能把凭据覆盖成空 |
| **台账身份字段** | `ledger.EARNED_FIELDS` / `DICT_FIELDS` | 决定"哪些字段不许被空值覆盖"（`api_key` 等）与"哪些是累积型字典"。`DICT_FIELDS` 里的 `waitlist` 是**历史键名**（现名 `signup`），必须留着兜住老台账 |
| **推理端点** | `verify_keys.API_URL` | 站点换端点 ⇒ 验收误判为"key 不可用" |
| **邮件正文形态** | `remail._hydrate` / `parsing.extract_magic_link` | 🔴 **两个后端给的正文形态不同**：CF 是纯文本全文；Remail 是 **HTML + 截断预览**（2026-09-21 实测：preview **248 字符、完全无链接**，全文 **4012 字符**才有）。⇒ ① 取件匹配成功后必须**再取全文**（`_hydrate`，只对匹配到的那一封取，否则 N+1 会打爆取件配额）；② 提取链接必须做 **HTML 实体反转义**（`&amp;` → `&`），否则 token 的参数名变成 `amp;token`，**等于没传 token**，而报错读起来像"链接已过期"。两条分别由 `test_magic_link_html_entity` 与 `test_mail_backend_selection` 钉住 |
| **同一 URL 在正文里有多份、且可能残缺** | `parsing.extract_magic_links` / `_looks_complete` | 🔴 2026-09-21 补跑实测：正文里同一个魔法链接出现 **12 次，其中 8 次被截断了 4 个字符**（`&token=D8SZNL…` → `&tokenSZNL…`）。残缺形态**等于没传 `token`**，Stytch 回 `400 invalid_public_token_id`（报的是 `public_token` 的格式问题，与"少了个参数"毫无字面关联），外层读成"链接可能已被使用/过期" ⇒ **4/100 账号被误判死**，而完整那条就在同一封邮件里。⇒ ① 提取必须返回**全部去重候选、完整优先**；② 交换必须**按序试**到拿到会话为止（判据是终态）。⚠️ `=D8` 是 token 的**字面字符**，不是 quoted-printable 转义 —— 受控实验：`&token=D8…` 回 200，补 `=` 或解成 `0xD8` 都回 400 ⇒ **不能靠"还原转义"修，只能换候选**。由 `test_magic_link_truncated_variant` + `test_magic_link_tries_all_candidates` 钉住 |

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
| ~~无并发~~ | **已加** `--concurrency`（注册段） | `watch` 已删除（见下），不再有"刻意串行"的通路 |
| ~~`pipeline.py` 零测试~~ | **已补** 192 项自测（审计当时 96 项） | 继续加边界用例。测试本体现已在 `tools/tests/` |
| ~~`typesafe.py` 混了 HTTP 客户端 + HTML/JS 解析~~ | **2026-09-20 已拆**：解析层独立成 `src/parsing.py`，`typesafe.py` 413 → **359** 行，只留 HTTP | — |
| ~~`pipeline.py` 684 行，阶段方法 + 并发脚手架挤在一个类~~ | **2026-09-20 已拆**：`src/stages.py` + `src/runner.py`。⚠️ 用的是 `StageMixin` 而不是报告原建议的自由函数 —— 阶段方法要读 `self.mail` / `self.login_mode` / `self.success_ledger` / `self.log`，转自由函数得先引入 ctx 对象，属另一档改动 | `typesafe.py` 现在 558 行（**变大了**，见下），若再膨胀优先拆它的 onboarding 段 |
| ~~`selftest.py` 1155 行，单文件承载全部测试~~ | **2026-09-20 已拆**：入口 `selftest.py` + `tools/tests/` 6 个文件。⚠️ **刻意不叫 `conftest.py`、不引 pytest** | 加新测试就落到对应 `test_*.py`，**并在 `selftest.py` 的 `main()` 里登记** —— 漏登记会被 `_registry_gap()` 当场拦下（2026-09-21 加） |
| ~~`FALLBACK_SETUP_ACTIONS` 是**假护栏**~~ | **2026-09-21 已修**：`post_setup()` 的降级通路以前 `except TypeSafeError: acts = {}` **静默**退化到一张已知全部作废的 action id 表，于是"站点改版"最终只表现为 `onboarding 失败: HTTP 404`。现在降级必打可辨识告警、失败时 `error` 点出真因 + 给下一步 | 兜底表本身仍会随部署失效（这是它的性质）。重录 HAR 拿到新 id 后，要同步 `typesafe.py` 的注释与 runbook §4.6 |
| ~~`auth_callback` 请求体多传 `waitlistEmail`（站点已收紧 schema）~~ | **2026-09-21 已修**：站点把该接口的 schema 收紧成 **strict**，多一个未知键直接 400。当时**所有**账号登录全灭（含 122 个早已拿到 key 的），而错误文案只有一句 `HTTP 400: Bad request`。已删该键，并让 `_fail_auth` 特判 Zod 的 `Unrecognized key`、把真因与处置直接写进 `error` | 这类"站点收紧请求体"**没有通用护栏** —— 只能靠我们自己维护的键集测试（`test_auth_callback_payload_shape`）。下次见到 `400 Bad request`，先跑 `tools/probes/audit_keys_against_site.py` 分清"回调坏了"还是"未获批"（runbook §4.8） |
| ~~`complete_onboarding` 用 `/api/me` 字段判缺口~~ | **2026-09-21 换过两次判据**，每次的错法都记在 `typesafe.complete_onboarding()` 的表里：① `/api/me` 的 `console_survey_completed_at` 有滞后且语义会变 ⇒ 账号已完全 onboard 却被记 `partial`（实跑 25 个误报 **19 个**）；② 改成 `/hook` 归零 ⇒ **站点自己非确定性**（相邻两次 GET 答案不同）⇒ 空转 + 报空错误 | 现在判据是**建 key 的结果**，门禁只作诊断。护栏 `test_onboarding_gate_is_guidance_not_a_gate`（含"门禁卡住但 key 建得出 ⇒ 必须记 keyed"的正向断言） |
| ~~`--mode watch`~~ / ~~`--mode claim`~~ / ~~`--mode apply`~~ | **2026-09-21 已删除**（不是"修好"，是**删除**）：三个模式都是邀请制的产物。`watch` 轮询全表共享窗口找"获批邮件"（该事件已不存在）；`claim` 的两进程设计**实测已失效**（`--send` 与 `--token` 各建会话、cookie 不传递 ⇒ 必报 `401 Code expired`）；`apply` 投的 Framer 表单本身不存在了。连带删除 `stages.stage_login_with_token()`（`claim` 的唯一落点）与 `tempemail.first_mail_matching()`（`stage_wait_approval` 的唯一调用者） | 需要"人工粘凭据"改用 `resume`：魔法链接 7 天有效。原文件在 `.workbuddy-ai/backup/refactor-20260921-180438/` |
| `typesafe.py` 从 442 → **558 行**（变大） | 如实记录：新增了 `onboarding_gate()` / `_onboarding_result()` / 门禁序列诊断，并把"判据换过三次"的复盘写进 docstring | 模块行数**不是**指标。本轮 `complete_onboarding` 的重复块归零、错误文案从"空字符串"变成可定位，这些才是收益。若真要瘦身，把 onboarding 段整体挪成 `src/onboarding.py` |
| `--mail-backend` 第二个邮箱后端（2026-09-21 新增） | **已接入并实测**：Remail 后端 1 单 `outlook.com` 走完全链路拿到 key（验收 `HTTP 200 / model=jev-1.13.0`）。过程中修掉两个**只在该后端出现**的坑：① `bodyPreview` 是**截断预览**（248 字符、无链接）⇒ 必须再取全文；② 全文是 **HTML**，`&` 转义成 `&amp;` ⇒ token 参数名被污染成 `amp;token`。⚠️ 另修一个静默洞：`runner._clone()` 原本**不传 `mail`** ⇒ 并发 worker 会悄悄换回 CF 后端（串行 `concurrency=1` 永远复现不出来） | 成本量级：Remail 是**付费**后端（`outlook.com` 实测 **8 积分/单**）。曾经最便宜的 `domain`（0.01/单）2026-09-21 已 **0 库存** ⇒ 别按旧印象估预算。库存**会变**，报"库存不足"时先跑 `tools/probes/probe_remail.py`（只读，一次报全库存/成本/余额/凭证台账），**不要改代码猜**。Remail 的 `code` 模式邮箱是 **10 分钟窗口**，下单后要尽快跑完 |
| `parsing.extract_magic_links()` 残缺候选（2026-09-21 新增） | **已修**：正文里同一个魔法链接**出现 12 次、其中 8 次被截断了 4 个字符**（`&token=D8SZNL…` → `&tokenSZNL…`，`=D8` 整个没了）。残缺形态**等于没传 `token`**，Stytch 回 `400 invalid_public_token_id`（报的是 `public_token` 的**格式**问题，与"少了个参数"毫无字面关联），外层把它读成"链接已用/过期" ⇒ **4/100 账号被误判死**，而完整那条**就在同一封邮件里**。旧实现 `extract_magic_link()` 只取第一个匹配 ⇒ 恰好取到残缺那条就必失败。现返回**全部去重候选、完整优先**，阶段层 `_exchange_link()` **按序试到拿到会话**（残缺那条 GET 只回 400、**不消耗**一次性 token ⇒ 逐条试安全） | ⚠️ `=D8` 是 token 的**字面字符**，**不是** quoted-printable 转义 —— 受控实验四形态只有 `&token=D8…` 回 200 ⇒ **不能靠"还原转义"修，只能换候选**。判"这条完不完整"必须用 `[?&]token=` **锚定参数名起点**，否则会被 `stytch_token_type=magic_links` 里的 `token=` 子串骗。护栏 `test_magic_link_truncated_variant` + `test_magic_link_tries_all_candidates`；处置流程见 runbook §4.13 |

## 附录：复算依赖图

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
"$PY" - <<'EOF'
import ast
from pathlib import Path
from collections import Counter

# 🔴 四类文件都要收：src / tools / tools/tests / tools/probes。
#    漏掉 tools/tests 会让 §3 表里的 `support` 项**根本不出现在输出里**
#    （2026-09-21 实测发现：表里有 support、命令却算不出它）。
cnt = Counter()
for p in sorted(list(Path('src').glob('*.py')) + list(Path('tools').glob('*.py'))
                + list(Path('tools/tests').glob('*.py'))
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
# ⚠️ 模式必须覆盖**两种语序**：README 写「自测 N 项」，本文 §6 写「已补 192 项自测」。
#    ⚠️ 这里的 N **刻意不写死** —— 它随自测增减，写死了这条注释自己就会变成假的
#       （实测踩过：改完项数后忘了同步注释里的数字）。
#    ⚠️ 两者数字**本来就不同**：README 是现状，§6 那条是**历史快照**（审计当时 96 项）。
#       不要为了"对齐"去改 §6 —— 那会把历史记录改成假的。
#    ⚠️ **四种前缀写法只覆盖 3/6 处**（2026-09-22 复算时发现）：`自测 N 项` / `全套 N 项` /
#       `合计 **N 项**` 能匹配，但 `N 行，N 项` 与裸 `# N 项，含负对照` 匹配不到 ——
#       典型"扫描面缩水"。⇒ 改用下面的宽模式，再**人工**区分「现状」与「历史快照」。
grep -rnE "[0-9]+ 项" README.md docs/
#    ↑ 会一并列出**不许改**的历史快照（`audit-2026-09-20.md` 的 37 / 96 项、
#      `optimization-2026-09-21.md` 的 235→269、本文 §6 的 192）—— 那些是刻意保留的时间点。
grep -rn "自测 [0-9]* 项\|[0-9]* 项自测\|全套 [0-9]* 项\|合计 \*\*[0-9]* 项" README.md docs/
```

> ⚠️ **`docs/audit-2026-09-20.md` 里的 37 项 / 96 项不要改** ——
> 那是**刻意保留的时间点快照**，改了会破坏它"可核查"的属性。
