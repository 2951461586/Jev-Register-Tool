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
│   ├── typesafe.py            Server Action / Stytch / onboarding / 建 Key
│   └── pipeline.py            阶段化编排（唯一的上层聚合者）
│
├── tools/                     入口脚本（含命令行逻辑）
│   ├── _bootstrap.py          按标记文件定位仓库根，统一 sys.path
│   ├── run_e2e.py             ★ 主入口：apply / watch / resume / claim / scan
│   ├── selftest.py            自测 96 项，含负对照，**全程离线**
│   ├── verify_keys.py         ★ 验收：真打一次推理接口 + 导出可用凭据
│   └── probes/                一次性诊断探针（不参与主流程）
│       ├── probe_confirm.py          单看"确认邮件"那一步的每跳原始响应
│       └── probe_confirm_flow.py     干净实验：先确认再回调，用状态码判假设
│
├── docs/                      说明文档
│   ├── architecture.md        本文件
│   ├── mail-filters.md        收件过滤规则（任务交付）
│   ├── runbook.md             怎么跑 + 故障处置
│   └── audit-2026-09-20.md    架构/耦合/目录/文档审计（含 P0 与全部证据）
│
├── evidence/                  录制的证据（已 gitignore）
│   ├── *.har / *.eml          抓包与邮件原文
│   └── scraped/               抓来的第三方 bundle（framer/ js/）——
│                              性质是**录制证据**，不是导出产物
└── exports/                   运行产物（已 gitignore）
    ├── ledger.jsonl           台账
    ├── keys.txt / keys_verified.json   验收结果与可用凭据
    ├── run_*.json / *.log     各次运行的记录
    └── _diag/                 一次性诊断残留（探针输出、抓下的页面快照）
```

**分层原则**：`src/` 只放可复用的库代码，`tools/` 只放入口与一次性探针。
判断标准是"这段逻辑会不会被第二个入口复用"——会就下沉到 `src/`。

**`exports/` 与 `evidence/` 的边界**：`exports/` 只放"跑出来的结果"，
`evidence/` 只放"抓下来 / 录下来的原始材料"。抓来的第三方 JS bundle 属于后者 ——
2026-09-20 之前它们混在 `exports/js`、`exports/framer` 里。

## 2. 模块规模与职责

| 模块 | 行数 | 职责 | 内部依赖 |
|---|---:|---|---|
| `config.py` | 86 | 常量集中地 + `.env` 加载 + `validate()` 启动校验 | 无 |
| `mailrules.py` | 269 | 收件规则表 + `extract_otp()`（锚定/降级） | **无**（纯 stdlib） |
| `ledger.py` | 183 | 台账读写、并集合并、等级语义 | 无 |
| `tempemail.py` | 186 | Worker 收信（索引端点、5xx 重试、计数） | `config` |
| `framer_waitlist.py` | 98 | 申请表单 + PoW | `config` |
| `typesafe.py` | 388 | 登录链路（Server Action → Stytch → 回调 → onboarding → 建 Key） | `config` |
| `pipeline.py` | 521 | 阶段编排（含并发扇出） | `config` + 上面 5 个叶子 |
| `run_e2e.py` | 260 | CLI（每模式一个函数，主流程只分派） | `config` `ledger` `pipeline` `tempemail` `mailrules` |
| `selftest.py` | 622 | 自测 96 项（含编排层离线测试） | `ledger` `framer_waitlist` `typesafe` `mailrules` `pipeline` `tempemail` |
| `verify_keys.py` | 117 | 验收 + 导出 | `config` `ledger` |
| `_bootstrap.py` | 37 | sys.path 定位 | 无 |

## 3. 依赖分层（AST 实测）

```
第 3 层   入口        tools/run_e2e.py   tools/selftest.py   tools/verify_keys.py
                              │                  │                  │
第 2 层   编排        ┌───────┴──────────────────┴──────────────────┴────┐
                      │              src/pipeline.py                    │
                      └──┬────────┬────────┬────────┬────────┬─────────┘
                         │        │        │        │        │
第 1 层   叶子        mailrules  ledger  tempemail  framer  typesafe
                         │        │        │     waitlist    │
                         └────────┴────────┴────────┴────────┘
                                          │
第 0 层   配置                        src/config.py
```

**被依赖次数**（越大越底层，改动越要谨慎）：

| 模块 | 被依赖 | 说明 |
|---|---:|---|
| `config` | 8 | 常量集中地。改它要跑全量自测 |
| `mailrules` | 5 | 规则表 + OTP 抽取；**纯叶子**，可离线测 |
| `tempemail` | 5 | 收信唯一入口 |
| `typesafe` | 5 | 登录链路 |
| `ledger` | 4 | 台账唯一写入口 |
| `framer_waitlist` | 3 | 申请 |
| `pipeline` | 2 | 只有 CLI 与自测依赖它 |

> 上表是**修正后**的数字。旧版表格（config=4 / typesafe=3 / framer=2 / mailrules=2）
> 是错的：附录脚本用 `startswith('src.')` 判断，而 `from src import config` 的
> `module` 恰好是 `"src"`（**不带点**），所以 `tools/*` 的顶层导入**全被漏计**。

**依赖方向是单向的**：`tools → pipeline → 叶子 → config`。
没有任何叶子反向依赖 `pipeline`，也没有循环。`mailrules.py` 是唯一
**零内部依赖 + 零第三方依赖**的模块（只用 `unicodedata` 和 `re`），
所以它可以被任意层安全引用，不会带进 `requests` 或配置副作用。

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
                └────────────────────────────────────────────────────────┘
                                        │
                ┌─ 验收（独立于链路）────────────────────────────────────┐
                │  verify_keys.py ──▶ POST api.typesafe.ai/v1/systemone  │
                │                    真打一次推理，导出 exports/keys.txt  │
                └────────────────────────────────────────────────────────┘
```

**取码走 `mailrules.extract_otp()`**（锚定优先、宽松降级），
而不是在 pipeline 里就地写正则 —— 与服务端 D1 规则 id=20 同构，见 §5。

## 5. 关键耦合点（改一处要连带看哪里）

| 耦合点 | 位置 | 连带影响 |
|---|---|---|
| **邮件文案** | `mailrules.RULES` | 站点改文案 ⇒ 规则不中。`--mode scan` 的漏网主题是唯一信号 |
| **发件人域** | `mailrules.SENDER_*` | 站点换 ESP ⇒ 全部规则失效（症状同上） |
| **OTP 模板句** | `mailrules.OTP_ANCHORED_RE` | 站点改模板 ⇒ 锚定失配 ⇒ 走降级路径（**日志会明确写出来**） |
| **Server Action 渲染形态** | `typesafe._actions_from_html` | 页面结构变 ⇒ `fetch_actions` 抛错（有明确报错，不是静默失败） |
| **`$ACTION_<n>:0` 必须紧凑 JSON** | `typesafe._compact_ref` | 加空格 ⇒ Next.js 直接 500。自测有负对照钉住 |
| **Framer PoW 常量** | `config.POW_*` | 站点调难度 ⇒ 申请被拒。常量取自 JS，不是试出来的 |
| **`/setup/*` action id** | `typesafe.FALLBACK_SETUP_ACTIONS` | 仅降级用；首选是运行时抓隐藏域 |
| **台账状态等级** | `ledger.RANK` | 决定升级/降级语义。**必须覆盖 pipeline 写的每个 status**，由 `test_status_vocabulary` 用 AST 钉住 |
| **台账身份字段** | `ledger.EARNED_FIELDS` | 决定"哪些字段不许被空值覆盖"（`api_key` 等） |
| **推理端点** | `verify_keys.API_URL` | 站点换端点 ⇒ 验收误判为"key 不可用" |

**已刻意解耦的地方**：

- `mailrules` 不 import `tempemail` —— 它用鸭子类型读 `.sender` / `.subject`，
  所以自测里能用 3 行的假对象测规则，不需要起 HTTP。
- `pipeline` 不自己拼 subject 子串，也**不自己写取码正则** —— 全部走 `mailrules`。
- 探针放 `tools/probes/` 且不参与主流程 —— 诊断代码不会成为主链路的负担。
- 并发时每个 worker 一个独立 `Pipeline` 实例，**不共享会话对象**；
  唯一共享的是带锁的 `Ledger`。

## 6. 已知的架构债

| 项 | 现状 | 建议 |
|---|---|---|
| ~~`ledger.RANK` 词汇与 pipeline 不一致~~ | **2026-09-20 已修**（曾导致重跑失败清空 `api_key`） | 已加 AST 词汇覆盖测试，不会复发 |
| ~~`config.py` 有收件规则的第二份真源~~ | **已修**：删掉 5 个零引用常量 | — |
| ~~`QuotaLedger` 整类无调用点~~ | **已修**：删除 67 行 | — |
| ~~无并发~~ | **已加** `--concurrency`（申请段/注册段） | `watch` 刻意保持串行 |
| ~~`pipeline.py` 零测试~~ | **已补** 96 项自测，其中编排层 41 项 | 继续加边界用例 |
| `pipeline.py` 521 行 | 阶段方法 + 并发脚手架挤在一个类里 | 若再加阶段，按"申请段 / 注册段"拆两个模块 |
| `typesafe.py` 388 行 | 混了 HTTP 客户端 + HTML/JS 解析 | 解析函数已独立成模块级 `_parse_js_object` 等，可整体挪到 `parsing.py` |
| `selftest.py` 622 行 | 单文件承载全部测试 | 超过 ~800 行时按 `tests/` 拆目录（保留一个聚合入口） |
| `--mode watch` 与 `resume` 有重复 | 都做"跑 4→7" | 已抽 `stage_login` + `stage_create_key`，重复的只是循环壳 |

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

**改这段脚本后要顺手复算本文 §2 / §3 的表格** —— 表格是脚本的输出，
不是独立的事实。同理，改了 `tools/selftest.py` 的检查项数量，
要同步 `README.md` / `docs/runbook.md` / `docs/mail-filters.md` 里写的项数。
