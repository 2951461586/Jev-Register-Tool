# Jev-Register-Tool

TypeSafe（Jev / System One）**注册 → 确认邮件（魔法链接）→ onboarding → 建 API Key → 入库**
全链路工具，**纯 HTTP、无浏览器**。

收信支持**两个后端**（CF Temp Email Worker / Remail 聚合），可直接批量出号，
**没有需要等待的外部阻断点**。

> 🔴 **2026-09-21：邀请制已取消。** 链路从 7 段缩到 4 段 —— 站点不再有
> "投申请 / 等回执 / 等人工审批"，注册后直接收到
> `Welcome to TypeSafe — confirm your email`，**邮件里的魔法链接就是登录凭据**。
> 两条站点侧判据：`POST /login` 直接回 `x-action-redirect: /login?sent=true`
> （`?waitlist=` 参数已失效），且收到的邮件主题就是上面那句。
> 因此 `--mode apply / watch / claim` 三个模式已**整体删除**（详见
> `docs/architecture.md` §6）。

**实测战果**：台账合并视图 453 个地址，其中 **388 个拿到 API Key**。

**最近一轮（2026-09-21 晚，优化后）100 个账号 / 并发 4 / 超时 60s**：
`95 keyed / 5 failed`（95%）—— 墙钟 **527s**、速率 **11.4 个/分**、真实限流 **0 处**。
**全量 453 把 key 真打推理接口 453/453 通过**（`HTTP 200 / model=jev-1.13.0`）。
本批触发**批内重发 9 次、救回 7 个账号** —— 无重发则只有 88%。

> 对比优化前（并发 2 / 超时 300s）：50 个账号 `43 keyed / 7 failed`（86%）、
> 墙钟 1552s、速率 1.93 个/分 ⇒ 同规模**提速 8.6 倍、成功率 +12 个百分点**。
> ⚠️ 报速率必须同时报**当批丢包率**：本批丢包 9%（上批 0%），重发等待拖长了尾部。
> 依据与复算命令见 `docs/optimization-2026-09-21.md`。

> ✅ **2026-09-21 接入 Remail**（`--mail-backend remail`，第二个邮箱后端）：
> 实测 1 单 `outlook.com` 走完全链路拿到 key，验收 `HTTP 200 / model=jev-1.13.0`。
> 过程中踩到两个**只在这个后端上出现**的坑（都已修 + 已加护栏）：
>
> 1. 它的 `bodyPreview` 是**截断预览**（实测 248 字符，**完全不含链接**）⇒
>    必须再取全文（4012 字符）才能拿到魔法链接，否则报"魔法链接邮件里没找到链接"
>    —— 把"我们只读了预览"说成"站点没发链接"；
> 2. 全文是 **HTML**，链接里的 `&` 被转义成 `&amp;` ⇒ 不还原的话 token 参数名
>    变成 `amp;token`，**等于根本没传 token**，交换必失败且报错像"链接已过期"。
>
> 详见 `src/remail.py` 头注与 `docs/optimization-2026-09-21.md` §7.4。

> ⚠️ 两套口径**行数天然不等**，别拿它们相等当对账判据：
> `result/success.jsonl` 是**账号级**（按邮箱去重 ⇒ 388 行）；
> `result/keys.txt` 是**凭据级**（同账号重跑会拿到第二把 key ⇒ 453 条）。
> 交付凭据以 `keys.txt` 为准。

> **凭据与基础设施标识一律不进仓库。** 代码里没有任何真实默认值 ——
> 全部走 `.env`（`.env.example` 是唯一真源说明），缺哪项由 `--doctor` 显式报出来。
> 被 gitignore 的（用通配，不逐条列举）：
> `.env*` / `*.har` / `*.eml` / `*.json` / `*.jsonl` / `*.csv` / `*.log` /
> `*.bak*` / `exports/` / `result/` / `evidence/` / `.workbuddy-ai/`。
> `result/keys.txt` 里是**明文 API Key**，别提交。
> 🔴 `result/` 必须**显式**列目录：`*.txt` 没有任何通配规则覆盖，
> 光靠 `*.json` / `*.jsonl` 挡不住 `result/keys.txt`。

## 配置

| 变量 | 必填 | 用途 |
|---|---|---|
| `TEMPMAIL_ADMIN_KEY` | ✅ 后端=cf | 建邮箱 / `/admin/*` |
| `TEMPMAIL_BASE` | ✅ 后端=cf | Worker 根地址（`https://<子域>.workers.dev`） |
| `TEMPMAIL_DOMAIN` | ✅ 后端=cf | 建邮箱用哪个域名（**完整域名**） |
| `REMAIL_API_KEY` | ✅ 后端=remail | remail.aishop6.com 的 `rk-` 开头 key |
| `REMAIL_BASE` | ✅ 后端=remail | 默认 `https://remail.aishop6.com` |
| `REMAIL_PROJECT_ID` | ⬜ | TypeSafe 在 Remail 上的项目 id（默认 `155`） |
| `REMAIL_EMAIL_SUFFIX` | ⬜ | 商品后缀（默认 `outlook.com`）。⚠️ **不是完整邮箱地址** |
| `REMAIL_SERVICE_MODE` | ⬜ | `code`=短效接码 10 分钟窗口（默认）/ `purchase`=长效购买 |
| `CF_API_TOKEN` | ⬜ | 只有 `tools/probes/*` 诊断脚本用；主流程不需要 |
| `CF_ACCOUNT_ID` / `CF_D1_ID` | ⬜ | 同上 |
| `TEMPMAIL_DOMAINS` | ⬜ | `probe_email_routing.py` 要查的域名；留空则问 Worker 的 `/health` |

> **两个邮箱后端二选一**：`--mail-backend cf`（默认，免费）或 `remail`（**付费**）。
> 启动校验**按后端走** —— 用 `cf` 跑批不会被 Remail 的缺失项拦住，反之亦然。
> 两边接口是同一套（`create_mailbox` / `list_mails` / `wait_for_mail`），
> 所以 `stages` 层不知道用的是哪个。差异与坑见 `src/remail.py` 头注。

## 快速开始

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

cp .env.example .env                   # 按注释填（必填项见上表）
$PY tools/run_e2e.py --doctor          # 环境体检（缺哪项会直接报出来）
$PY tools/run_e2e.py --doctor --mail-backend remail   # Remail 体检：只读，**不下单**
$PY tools/selftest.py                  # 自测 269 项，离线可跑
$PY tools/run_e2e.py --count 5         # ★ 全链路：建邮箱 → 发信 → 登录 → 建 key
$PY tools/run_e2e.py --count 10 --concurrency 4   # 并发
$PY tools/run_e2e.py --count 1 --mail-backend remail  # ★ 换 Remail 后端（**每单扣积分**）
$PY tools/run_e2e.py --mode resume --email a@b.com --email c@d.com   # 对已知邮箱补跑
$PY tools/run_e2e.py --mode scan       # 诊断：列出窗口内全部邮件并按规则分桶（**仅 cf**）
$PY tools/verify_keys.py               # ★ 验收：真打推理接口 + 导出 result/ 下 4 份交付物
$PY tools/check_deliverables.py        # ★ 独立复核那 4 份（只读；退出码 0/1，可当门禁）
```

> `--concurrency` 对整条链路有效（每个 worker 一个独立会话 + 独立收件箱索引端点，
> 只共享带锁的台账）。实测 `--count 2 --concurrency 2` 两把 key 互不串号。
>
> 🔴 **`--mode resume` 默认跳过台账里已有 `api_key` 的地址**（`skip_keyed=True`）——
> 重跑会给同一账号**造出第二把 key**，而 `Ledger.load()` 按邮箱去重、末行胜出，
> 交付物里会少一把。确需重跑请显式传 `skip_keyed=False`（只有库内调用有这个参数）。
>
> ⚠️ **`--mail-backend remail` 是付费后端**：`create_mailbox()` = **真实下单扣积分**
> （TypeSafe 项目下 `outlook.com` 实测 8 积分/单）。因此：
>
> - `--doctor` 对 remail **只做只读检查**（key 是否有效 + 余额 + 当前商品配置），
>   **不会**偷偷下单 —— 体检不该花钱，要验证下单请直接 `--count 1`；
> - **补跑时后端必须与建这些邮箱时一致**：Remail 的取件凭证 `serviceToken`
>   是 per-order 的（存在 `result/remail_orders.jsonl`），CF 建的地址它取不了，
>   反之亦然。`resume_pending.py` 会在开跑前把不匹配的地址数报出来；
> - 站点侧还有一条硬限制：`code` 模式的邮箱是 **10 分钟窗口**
>   （`codeWindowMinutes: 10`）⇒ 下单后要尽快跑完，**过窗口就收不到新信**，
>   只能靠 `relogin_pending.py` 复用窗口内已收到的历史链接。

## 链路与可自动化程度

> 邀请制取消后**没有阻断点**了 —— 下表 4 段全程可自动，且**不需要任何"等"**。

| 阶段 | 方式 | 可自动化 | 实测耗时 |
|---|---|---|---|
| 1. 注册 + 登录 | 纯 HTTP（Server Action 复刻 + Stytch 魔法链接） | ✅ | 4.5~6.7s |
| 2. onboarding | 纯 HTTP（`/setup/*` 门禁链） | ⚠️ 前两步可自动，**最后一跳提交不了**（见下） | 含在 3 内 |
| 3. 建 API Key | 纯 HTTP（`POST /api/api-keys`） | ✅ | 1.8~5.0s |
| 4. 入库 | 本地 JSONL 台账 | ✅ | — |
| 5. **验收** | 纯 HTTP（真打推理接口） | ✅ | 每个 ~0.6s |

🔴 **"这个账号成不成"的判据只有一条：建 key 有没有回 key。**

`/hook` 的重定向链是**引导**，不是硬门槛，而且**站点自己都不稳定**：

- 门禁链实测是**三步** `tos → set-name → console-survey`，其中
  `console-survey` 那一页**没有 `$ACTION_*` 隐藏域**（渲染的是
  "Get started / Let's create your org" 向导）⇒ 我们提交不了；
- 就在这个"门禁未归零"的状态下，`POST /api/api-keys` 返回 **200 + 明文 key**；
- 相邻两次 `GET /hook` 会给出**不同答案**（`set-name` 与 200 交替）。

⇒ 所以流程是"尽力推进我们认识的步骤 → **直接建 key**"，
onboarding 的实况只记进台账（`stages.onboarding = partial`）供诊断。
复现命令：`$PY tools/probes/probe_gate_chain.py`（逐跳打门禁链 + 最后试建 key）。

> 判据历史上换过三次（`/api/me` 字段 → `/hook` 归零 → 建 key 结果），
> 每次怎么错的都留在 `src/typesafe.py::complete_onboarding()` 的文档里。

## 目录结构

```
src/                       库代码
  config.py                常量 + .env 加载 + 启动校验
  mailrules.py             收件规则表 + OTP 抽取（零内部依赖的纯叶子）
  ledger.py                JSONL 台账（并集合并 / 幂等 / 等级语义）
  tempemail.py             CF Worker 收信
  remail.py                Remail 聚合收信（**第二个邮箱后端**，接口刻意对齐
                           `tempemail`：`create_mailbox` / `list_mails` /
                           `wait_for_mail`）⇒ `stages` 层无感切换
  parsing.py               页面/邮件文本解析（`$ACTION_*`、JS 字面量、魔法链接）——
                           **纯函数、零第三方依赖**，可脱离 `requests` 单测
  typesafe.py              Server Action / Stytch / onboarding / 建 Key（只发 HTTP）
  stages.py                ★ 单账号阶段实现（`StageMixin`）+ `AccountRecord`
                           出网动作全在这里：login / onboarding / 建 Key
  runner.py                ★ `Pipeline`：批量 / 并发 / 补跑 / 台账写入
                           继承 `stages.StageMixin`，`stages` 不反向依赖它
                           另有邮箱后端工厂 `make_mail_client(backend)`

tools/                     入口脚本
  _bootstrap.py            按标记文件定位仓库根，统一 sys.path
  run_e2e.py               ★ 主入口：full / resume / scan（+ --doctor）
  relogin_pending.py       对没拿到 key 的地址重发确认邮件，并把历史链接一起用
                           （魔法链接 7 天有效 ⇒ 重跑比改请求有效）
  resume_pending.py        ★ 补跑台账里还没拿到 key 的账号（幂等，可反复跑）。
                           进度/计数**只信它** —— 走 `Ledger.load()` 合并视图
  normalize_ledger.py      修被 CR / 尾部空白污染的 key、email（**不折叠行**）
  selftest.py              自测**入口**：只做聚合与调度（27 个用例段在 `tests/`）
  tests/                   自测本体（6 个文件 2101 行，269 项，含负对照，**全程离线**）
    __init__.py              只为让 `tests` 可当包导入；**不是** pytest 测试包
    support.py               共享夹具：`check()` 计数 + 离线替身 + 模块别名
    test_parsing.py          解析层（紧凑 JSON / Server Action / JS 字面量 / HTML 实体）
    test_ledger.py           台账（并集合并 / 状态词汇 / 身份字段归一化）
    test_mailrules.py        收件规则 + OTP 抽取
    test_orchestration.py    编排层（错误码分流 / 登录 / 门禁 / 并发 / 邮箱后端选择）
  verify_keys.py           ★ 验收 + 导出可用凭据
  check_deliverables.py    ★ 独立复核四份交付物（只读）：CR 污染 / apikeys≡keys 集合恒等 /
                           与 .bak 的差集。**别只信 verify_keys 自己的汇总** —— 它是同一进程的自述
  probes/                  一次性诊断探针（只读，除注明外都不写台账）
    probe_gate_chain.py          ★ 逐跳走 onboarding 门禁 + 最后试建 key
    audit_keys_against_site.py   站点侧对账：`GET /api/api-keys` vs 我们的交付物
    probe_confirm.py             看"魔法链接"那一步的每跳原始响应
    probe_login_shape.py         /login 页渲染形态（只 GET，不发信）
    probe_new_login_flow.py      发信 → 收信：凭据到底是链接还是码
    probe_full_new_flow.py       一次跑完（自建邮箱，不依赖台账）
    probe_onboarding.py          探 `/setup/*` 的 Server Action 形态（`--post` 才发请求）
    probe_onboarding_state*.py   门禁页形态快照（**静态**，不如 probe_gate_chain 准）
    probe_worker_health.py       Worker /health + D1 连通性（需 CF_API_TOKEN，只读）
    probe_email_routing.py       查各域名在 Worker 上的收信路由（需 CF_API_TOKEN，只读）
    probe_remail.py              ★ Remail 诊断：库存 + 成本 + 余额 + 凭证台账（**只读，不下单**）
    verify_bodycandidates.mjs    Node 回归台：把 Worker bundle 打到落库那一步（见下）

docs/
  architecture.md          目录 / 模块 / 耦合 / 数据流（依赖图由 AST 算出）
  mail-filters.md          收件过滤规则 + 取码锚定（格式对齐 OpenXLab 项目）
  runbook.md               怎么跑 + 故障处置
  audit-2026-09-20.md      一轮审计（时间点快照：37/96 项，**数字刻意不改**）
  audit-2026-09-20-round2.md  二轮审计 + 批次 A/B/C 修复状态（§0.5 / §0.6 / §0.7）

evidence/                  录制的证据（har / eml / 抓来的第三方 bundle）
exports/                   运行台账与记录：ledger.jsonl / run_*.json / *.log / _diag/
result/                    ★ 交付物（**只放成功的**）：见下方口径说明
                           · success.jsonl       成功**账号**（账号级：每邮箱一行）
                           · keys.txt            凭据清单（凭据级：`email----key----id`）
                           · keys_verified.json  机器可读验收结果（凭据级）
                           · apikeys.txt         纯 api_key 一行一个（keys.txt 的**无元数据版**）
                           ⚠️ 两套口径**行数天然不等**：同账号重跑会拿到第二把 key，
                              账号级会按邮箱合并掉、凭据级两把都留。交付凭据以
                              keys.txt 为准，别拿三者行数相等当对账判据。
                           ⚠️ `apikeys.txt` 与 `keys.txt` 由**同一次** verify 写出，
                              故两者**集合恒等**（只差元数据形态）—— 可当交叉对账用。
                           · remail_orders.jsonl  **不是交付物**：Remail 的 per-order
                              取件凭证（`serviceToken`），补跑跨进程取件靠它。
```

> **`exports/` 与 `result/` 的分工**（2026-09-20 起）：台账要留**全部**尝试
> （含失败的，便于复盘），交付物只该有成功的。以前两者混在 `exports/` 里，
> 取交付物时得自己筛一遍。成功数据由 `stage_create_key()` 直接落 `result/success.jsonl`
> —— 那是**唯一**产出 key 的地方，写在这里就自动覆盖全部两条路径
> （`run_batch` / `resume`），不需要在两个调用点各写一遍。

**依赖方向单向**：`tools → runner → stages → 叶子 → config`，无循环。
第三方依赖**只有 `requests`**（刻意不用 `bs4` / `lxml`）。
详见 `docs/architecture.md`。

## 收件规则（摘要）

格式对齐同机 OpenXLab 项目的 `sender_contains="openxlab"` 做法：
**信封发件人子串第一道，主题子串第二道。**

| 规则名 | `sender_contains` | `subject_contains` |
|---|---|---|
| `welcome_confirm` | `typesafe.ai` | `confirm your email` ← ★ 主路径唯一凭据来源 |
| `signin_link` | `typesafe.ai` | `sign in to typesafe` |
| `signin_code` / `verify_code` | `typesafe.ai` | `sign-in code` / `verification code` |

> 🔴 **2026-09-21 删掉两条营销流规则**（`waitlist_confirm` / `account_ready`）与
> 它们的发件人常量 `SENDER_UPDATES`。原因：邀请制取消后这两封邮件不再存在，
> 而留着它们会让链路**看起来有 6 段**（"还要等获批"），把排查引向"为什么没收到获批邮件"。
> 护栏见 `tools/tests/test_mailrules.py` 的两条负向断言（规则名与常量都不许回归）。

⚠️ 取码（`signin_code`）是**兼容分支**，不是主路径：站点对同一次发信请求**可能
回魔法链接而不是 6 位码**（实测 4 次里 1 次），所以码模式下等不到码必须回捞一次链接。

详见 `docs/mail-filters.md`。

## 三个必须分清的 HTTP 状态

| 响应 | 含义 |
|---|---|
| `401 {"error":"Code expired"}` | OTP 错/过期（凭据校验在**前**，不看邮箱） |
| `401 {"error":"Authentication failed"}` | 魔法链接 token **已被用过**（一次性） |
| `403 {"error":"Access restricted"}` | 凭据有效，但身份**不在白名单** |

> `403` 自 2026-09-21 起**不再是常态**（邀请制已取消，实测回调直接 200），
> 但这条分流**必须保留**：站点随时可能恢复白名单，而删掉它会让"没被邀请"
> 伪装成"凭据错误"，把排查引向重新发码的死循环。

## 别做这些事

- ❌ 别用 `/admin/all` 拉列表自己筛（烧 D1 读配额 + 会被挤出窗口）
- ❌ 别打 `*.workers.dev` 不带浏览器 UA（CF 边缘 403 / `error code: 1010`）
- ❌ 别对 `api.typesafe.ai` 假设 OpenAI 兼容（它是 `choice`/`score`/`noul` 原语）
- ❌ 别把 `apikey_...` 字符串当终点，**必须真打一次推理接口**
- ❌ 别把"此刻没查到"讲成"不存在"（站点行为有延迟，重跑一次常常就好）
- ❌ 别在凭据有效性未验证前把 `403` 当结论
- ❌ 别把长等待放前台（~120s 被 SIGTERM，日志截断会伪造业务结论）
- ❌ 别在 `stages` 里就地写取码正则（走 `mailrules.extract_otp`；
  降级路径会在日志里显式警告，**看到警告要去查模板变更，不是重新发码**）
- ❌ 别把 `Pipeline` 的会话 client 挂成实例字段（并发会串号）
- ❌ **别拿 `/hook` 的门禁当"账号成不成"的判据** —— 它是引导、还非确定性；
  判据只有 `POST /api/api-keys` 的返回（见上文"链路与可自动化程度"）
- ❌ 别对 `--mode resume` 传台账里已有 `api_key` 的地址（会造第二把 key，
  而 `Ledger.load()` 按邮箱去重 ⇒ 交付物少一把）

## 相关文档

- `docs/architecture.md` —— 模块耦合、依赖分层、数据流
- `docs/mail-filters.md` —— 收件规则的完整推导、实测数据、取码锚定
- `docs/runbook.md` —— 三种模式、验收、故障处置
- `docs/audit-2026-09-20.md` —— 架构审计（含一个已修的 P0 与全部证据）
