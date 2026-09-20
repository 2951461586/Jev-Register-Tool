# 架构：目录、模块、耦合

> 依赖关系是**用 AST 算出来的**，不是凭印象画的。
> 复算命令见文末「附录」。

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
│   ├── mailrules.py           收件过滤规则表（纯叶子，零内部依赖）
│   ├── ledger.py              JSONL 台账：并集合并 / 幂等 / 补录
│   ├── tempemail.py           CF Temp Email Worker 客户端
│   ├── framer_waitlist.py     Framer 表单申请（含 PoW 复刻）
│   ├── typesafe.py            Server Action / Stytch / onboarding / 建 Key
│   └── pipeline.py            阶段化编排（唯一的上层聚合者）
│
├── tools/                     入口脚本（含命令行逻辑）
│   ├── _bootstrap.py          按标记文件定位仓库根，统一 sys.path
│   ├── run_e2e.py             ★ 主入口：apply / watch / resume / claim / scan
│   ├── selftest.py            自测 37 项，含负对照
│   ├── verify_keys.py         ★ 验收：真打一次推理接口 + 导出可用凭据
│   └── probes/                一次性诊断探针（不参与主流程）
│       ├── probe_confirm.py          单看"确认邮件"那一步的每跳原始响应
│       └── probe_confirm_flow.py     干净实验：先确认再回调，用状态码判假设
│
├── docs/                      说明文档
│   ├── architecture.md        本文件
│   ├── mail-filters.md        收件过滤规则（任务交付）
│   └── runbook.md             怎么跑 + 故障处置
│
├── evidence/                  录制的证据（.har / .eml，已 gitignore）
└── exports/                   运行产物（台账 / 报告 / 凭据，已 gitignore）
```

**分层原则**：`src/` 只放可复用的库代码，`tools/` 只放入口与一次性探针。
判断标准是"这段逻辑会不会被第二个入口复用"——会就下沉到 `src/`。

## 2. 模块规模与职责

| 模块 | 行数 | 职责 | 内部依赖 |
|---|---:|---|---|
| `config.py` | 84 | 常量集中地 + `.env` 加载 + `validate()` 启动校验 | 无 |
| `mailrules.py` | 208 | 收件规则表 | **无**（纯 stdlib） |
| `ledger.py` | 194 | 台账读写、并集合并、升级语义 | 无 |
| `tempemail.py` | 186 | Worker 收信（索引端点、5xx 重试、计数） | `config` |
| `framer_waitlist.py` | 98 | 申请表单 + PoW | `config` |
| `typesafe.py` | 388 | 登录链路（Server Action → Stytch → 回调 → onboarding → 建 Key） | `config` |
| `pipeline.py` | 430 | 阶段编排 | `config` + 上面 5 个叶子 |
| `run_e2e.py` | 226 | CLI | `config` `ledger` `pipeline` `tempemail` `mailrules` |
| `selftest.py` | 197 | 自测 | `ledger` `framer_waitlist` `typesafe` `mailrules` |
| `verify_keys.py` | 117 | 验收 + 导出 | `config` `ledger` |
| `_bootstrap.py` | 33 | sys.path 定位 | 无 |

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
| `config` | 4 | 常量集中地。改它要跑全量自测 |
| `ledger` | 4 | 台账唯一写入口 |
| `tempemail` | 4 | 收信唯一入口 |
| `typesafe` | 3 | 登录链路 |
| `mailrules` | 2 | 规则表 |
| `framer_waitlist` | 2 | 申请 |
| `pipeline` | 1 | 只有 CLI 依赖它 |

**依赖方向是单向的**：`tools → pipeline → 叶子 → config`。
没有任何叶子反向依赖 `pipeline`，也没有循环。`mailrules.py` 是唯一
**零内部依赖 + 零第三方依赖**的模块（只用 `unicodedata`），
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
     typesafe          typesafe  tempemail      typesafe                 │
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

## 5. 关键耦合点（改一处要连带看哪里）

| 耦合点 | 位置 | 连带影响 |
|---|---|---|
| **邮件文案** | `mailrules.RULES` | 站点改文案 ⇒ 规则不中。`--mode scan` 的漏网主题是唯一信号 |
| **发件人域** | `mailrules.SENDER_*` | 站点换 ESP ⇒ 全部规则失效（症状同上） |
| **Server Action 渲染形态** | `typesafe._actions_from_html` | 页面结构变 ⇒ `fetch_actions` 抛错（有明确报错，不是静默失败） |
| **`$ACTION_<n>:0` 必须紧凑 JSON** | `typesafe._compact_ref` | 加空格 ⇒ Next.js 直接 500。自测有负对照钉住 |
| **Framer PoW 常量** | `config.POW_*` | 站点调难度 ⇒ 申请被拒。常量取自 JS，不是试出来的 |
| **`/setup/*` action id** | `typesafe.FALLBACK_SETUP_ACTIONS` | 仅降级用；首选是运行时抓隐藏域 |
| **台账状态等级** | `ledger.RANK` | 决定"升级替换"语义；加新状态必须同时加等级 |
| **推理端点** | `verify_keys.API_URL` | 站点换端点 ⇒ 验收误判为"key 不可用" |

**已刻意解耦的地方**：

- `mailrules` 不 import `tempemail` —— 它用鸭子类型读 `.sender` / `.subject`，
  所以自测里能用 3 行的假对象测规则，不需要起 HTTP。
- `pipeline` 不自己拼 subject 子串 —— 全部走规则表，文案变更只改一个文件。
- 探针放 `tools/probes/` 且不参与主流程 —— 诊断代码不会成为主链路的负担。

## 6. 已知的架构债

| 项 | 现状 | 建议 |
|---|---|---|
| `pipeline.py` 430 行 | 6 个阶段挤在一个类里 | 若再加阶段，按"申请段 / 注册段"拆两个模块 |
| `typesafe.py` 388 行 | 混了 HTTP 客户端 + HTML/JS 解析 | 解析函数已独立成模块级 `_parse_js_object` 等，可整体挪到 `parsing.py` |
| 无并发 | 15 个账号串行 ~2.5 分钟 | 瓶颈在发码→收码的邮件往返（3~6s），不在站点接口；要提速得把收信做成并行轮询 |
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
            elif (n.module or '').startswith('src.'):
                cnt[n.module.split('.')[1]] += 1
print(cnt.most_common())
EOF
```
