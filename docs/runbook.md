# Runbook：怎么跑 + 故障处置

所有命令**从项目根执行**。

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
```

> ⚠️ **必须用上面这个解释器**。系统 python 缺 `requests`。
> 本机 venv 里**没有** `bs4` / `lxml`——HTML 解析一律 stdlib `re`，别去装。

## 0. 首次准备

```bash
cp .env.example .env      # 按注释填：3 个必填项见下
$PY tools/run_e2e.py --doctor
```

**必填项按后端二选一**（`--mail-backend cf|remail`，默认 `cf`）：

| 后端 | 必填 | 性质 |
|---|---|---|
| `cf`（默认） | `TEMPMAIL_ADMIN_KEY` / `TEMPMAIL_BASE` / `TEMPMAIL_DOMAIN` | 免费；建邮箱只是本地 Worker 的一次调用 |
| `remail` | `REMAIL_BASE` / `REMAIL_API_KEY` | ⚠️ **付费**：`create_mailbox()` = **真实下单扣积分** |

选填：`REMAIL_PROJECT_ID`（默认 `155`）/ `REMAIL_EMAIL_SUFFIX`（默认 `outlook.com`）/
`REMAIL_SERVICE_MODE`（默认 `code` = 10 分钟窗口）、
`CF_API_TOKEN` / `CF_ACCOUNT_ID` / `CF_D1_ID`（只有 `tools/probes/*` 诊断脚本用）、
`TEMPMAIL_DOMAINS`（`probe_email_routing.py` 要查的域名，留空则问 Worker 的 `/health`）。

> ⚠️ **启动校验按后端走** —— 用 `cf` 跑批不会被 Remail 的缺失项拦住，反之亦然
> （`config.validate()` vs `config.validate_remail()`）。

> 🔴 **代码里没有任何真实默认值** —— 以前 `config.py` 的 `TEMPMAIL_BASE` / `TEMPMAIL_DOMAIN`
> 带着真实值当默认，探针里还写着活的 `cfat_` 令牌。`os.getenv(k, "真值")` 就是泄漏点：
> 它让"忘了配"和"配好了"在代码里长得一样，而且会被 git 一路带上去。
> 现在缺哪项由 `validate()` / `validate_cf()` **显式报出来**，`--doctor` 直接列缺项。

`--doctor` 会验：配置齐全 → 邮箱服务健康 → 能建邮箱 → 台账可读。

⚠️ 对 `remail` 后端**只做只读检查**（API Key 是否有效 + 余额 + 当前项目/商品配置），
**不会下单** —— 体检不该花钱。要验证下单请直接 `--count 1`，那笔钱花得看得见。

## 1. 三种模式

| 模式 | 干什么 | 会不会发注册请求 |
|---|---|---|
| （默认）`full` | ★ 全链路：建邮箱 → 发信 → 登录 → onboarding → 建 key | 会 |
| `--mode scan` | 按规则表给窗口内邮件分桶，**列出漏网主题** | 不会（只读） |
| `--mode resume` | 对**指定邮箱**补跑（幂等，可反复跑） | 会 |

**邮箱后端**（`--mail-backend`，默认 `cf`）：`full` / `resume` / `scan` 与 `--doctor` 都认。
`cf` 与 `remail` 的公开接口是同一套，所以模式不变；但 **`scan` 只支持 `cf`**
（Remail 没有"扫全窗口"端点，取件必须带 `email` + `serviceToken`），传 `remail` 会被显式拒绝。

> ⚠️ **补跑时后端必须与建这些邮箱时一致**。Remail 的取件凭证 `serviceToken`
> 是 per-order 的（落 `result/remail_orders.jsonl`）⇒ CF 建的地址它取不了，反之亦然。
> `resume_pending.py` 会在开跑前把不匹配的地址数报出来，不会让你从一堆同样的报错里自己悟。

> 🔴 **2026-09-21 邀请制取消后删掉了三个模式**（`apply` / `watch` / `claim`）：
>
> | 删掉的 | 为什么 |
> |---|---|
> | `apply` | 它投的 Framer 表单**本身不存在了**（申请环节没了） |
> | `watch` | 它轮询全表共享窗口找"获批邮件"，而"获批"这个**事件不存在了** |
> | `claim` | 人工接力。两进程用法**实测已失效**（必报 `401 Code expired`），见 §1.5 |
>
> 连带删除 `stages.stage_login_with_token()` 与 `tempemail.first_mail_matching()`
> —— 它们是这两个模式各自的唯一落点。原文件在
> `.workbuddy-ai/backup/refactor-20260921-180438/`。

### 并发（`--concurrency`）

`full` / `resume` 支持 `--concurrency N`（默认 1 = 串行，行为与加并发前一致）：

```bash
$PY tools/run_e2e.py --mode resume --email a@example-mail.test --email b@example-mail.test \
  --email c@example-mail.test --concurrency 3
```

- 并发安全的**前提**是每个账号只读**自己的**收件箱索引端点（`/api/inbox?email=`）。
- 每个 worker 用**独立的 `Pipeline` 实例**（各自的 `requests.Session` 与 `stats`），
  唯一共享的是带锁的 `Ledger`。会话 client **不再是实例字段** ——
  挂实例字段在串行时看不出问题，并发会**串号**（A 的 key 建在 B 的会话上）。
- 实测（2026-09-21）：`--count 2 --concurrency 2` 两把 key 互不串号，都通过验收。

### 1.1 全链路（默认模式）

```bash
$PY tools/run_e2e.py --count 5
```

单账号关键路径实测 `login 6.7s + create_key 5.0s`。

**这一段的终点是"建出 key"，中间没有任何需要等待的外部环节。**
（旧的 `apply → 等回执 → 等获批 → resume` 四步流程已随邀请制取消作废。）

🔴 **看到 `未在 Ns 内收到确认邮件` 不要直接当成失败，也不要先去改请求。**

**阈值与依据（2026-09-21 已重定）**：43 个成功账号实测到达延迟
（`signup.mail_at ÷ 1000 − created_at`，50 批次跑批）：
`min 2.66s · P50 3.26s · P90 3.80s · max 4.57s`
⇒ 阈值取 `MAIL_TIMEOUT = 60s`（对实测最大延迟仍有 **13 倍**余量）。

⚠️ **旧值 300s 的依据已被证伪，别再引回去**：旧文档引的是"2026-09-20 实测延迟
单调爬升到 198s" —— 那批其实是**批量发出**的 waitlist 回执（一次性涌入 47 封、
延迟约 25 分钟），被误读成"单调爬升"；而且那条链路（等审批回执）**已随邀请制取消
整体删除**。⇒ **阈值是"针对某个延迟分布"的；分布变了，它就该变。**

**超时后会在批内自动重发一次**（`RETRY_SEND_ON_TIMEOUT`，上限
`MAX_SEND_RETRIES = 1`）：正常邮件 3s 内必到 ⇒ "等到 60s 还没有"几乎必然是**丢包**，
重发比让账号落到下一轮更划算。⚠️ 它与阈值是**一个改动**：只降阈值不重发，
偶发的慢邮件会被直接判死，成功率反而下降（`tools/selftest.py` 的
`[编排：确认邮件等待阈值]` 段把这两条都钉住）。

处置顺序：

1. **先看日志有没有"批内重发第 1 次"**。有 ⇒ 已经自动重发过，
   说明是真的连续丢包，直接进第 2 步。
2. **再重跑一次**。魔法链接 7 天有效，重跑会连历史链接一起用
   （`relogin_pending.py` 就是干这个的），比改请求更有效。
3. 回查收件箱，确认邮件到底有没有到（迟到 ≠ 未发）：
   ```bash
   $PY tools/run_e2e.py --mode scan     # 看窗口内到底有什么
   ```
4. 只有**发信请求本身**失败（`stages.send != ok`）才算真的失败。

> 超时文案里带了**邮箱接口的轮询计数**（`轮询 N 次，5xx M 次`）：
> 计数正常 = 站点没发（重跑即可）；计数很高 / 5xx 多 = **我们读不出来**
> （去查 Worker）。这两件事的处置**恰好相反**，所以文案必须把它们分开。

### 1.2 看窗口里到底有什么（最常用的诊断）

```bash
$PY tools/run_e2e.py --mode scan
```

**这是"我到底收到了什么"的唯一可信来源**，比凭印象说"我好像收到过"靠谱。
判据见 `docs/mail-filters.md` §5。

### 1.3 补跑指定邮箱

```bash
$PY tools/run_e2e.py --mode resume \
  --email a@example-mail.test --email b@example-mail.test
```

可反复跑（幂等）。**失败重试就用这个。**

🔴 **默认跳过台账里已有 `api_key` 的地址**（`skip_keyed=True`）。
重跑会给同一账号**造出第二把 key**：两把在服务端都有效，但
`Ledger.load()` 按邮箱去重、**末行胜出** ⇒ 交付物里少一把。
这与 P0 / 阈值误判是同一类：**不报错，只是行数不对**。
确需重跑请从库内调用并传 `skip_keyed=False`（CLI 上没有这个开关，是刻意的）。

护栏：`test_resume_skips_keyed`（含"没有 key 的地址照常被处理"的正对照）。

### 1.4 整批补跑（从台账里挑）

```bash
$PY tools/resume_pending.py
```

它挑的是"有邮箱、但还没有 `api_key`"的地址。**进度/计数只信它** ——
它走 `Ledger.load()` 的**合并视图**，不是"末行胜出"（那会让重跑失败把计数
**压低**，守卫据此误判"没有新增"而提前收工）。详见 §4.3。

### 1.5 人工接力（`--mode claim`）—— **已移除，不是"修好"**

> 🔴 这一节记录的是**已删除的功能**，留在这里是为了不丢掉根因。
> 不要照抄任何命令 —— 那些参数在 CLI 上已经不存在了。

`claim` 的用途曾是"邮箱是真人邮箱、Worker 读不到时，由人把验证码/魔法链接
token 粘进来"。它被删除有三条独立理由：

1. **它本身实测已失效**：`--send` 与 `--token` 是**两个独立进程**，各自新建
   `TypeSafeClient()`，中间**没有任何会话传递**。实测**码在 2 分钟内提交仍报
   `401 Code expired`**。⚠️ 而 §4.2 对这个错误码的处置写的是"重新发码，
   10 分钟内提交" —— **照着做会掉进死循环**。错误码与根因毫无字面关联。
2. **它的唯一调用者 `runner.claim()` 已随 `watch()` 一起删除** ⇒ 无入口。
3. **新链路里没有这个场景**：魔法链接 7 天有效，`resume` 一条命令就能把
   没拿到 key 的地址重跑（含历史链接复用）。

**根因候选（⚠️ 始终未实测，随功能删除而失去验证机会）**：

`stage_login()` 在提交回调**之前**会先 `GET /login` 建立会话（经由
`fetch_actions()`），而 `stage_login_with_token()` **完全没有这一步** ——
它上来就 `POST /api/auth/callback`。两条路唯一的差别就在这里，所以最可能的
原因是回调缺少 `/login` 页面种下的会话 cookie。

> 记录这一条是因为它是"**判据跑在错误的层上 ⇒ 假阴性**"的又一个实例
> （参见 `tools/resume_pending.py` 的 docstring）。若将来真要做"人工粘凭据"，
> 先验证这个候选：在新建 client 之后补一次 `GET /login`，再跑两进程流程。
> **没实测前不许记为"已修"** —— 现在是"已删除"，这两个词不能混。

## 2. 验收（**不算完成，除非跑过这一步**）

```bash
$PY tools/verify_keys.py
```

拿台账里每个 key **真打一次** `POST https://api.typesafe.ai/v1/systemone`，
输出可用/不可用统计，并把**成功数据**导出到 `result/`（交付物目录）：

- `result/success.jsonl` —— 成功**账号**（账号级：每邮箱一行；从台账补录，幂等；每次跑批也会自动追加）
- ⚠️ **两套口径别混**：`success.jsonl` 是**账号级**（`Ledger` 以邮箱为主键，同账号重跑的
  第二把 key 会被合并掉），而 `keys.txt` / `keys_verified.json` 是**凭据级**（每把 key 一行）
  ⇒ **前两者**行数**天然不等**。2026-09-21 21:13 快照：**388 / 453 / 453**
  （`keys.txt` 与 `keys_verified.json` 由**同一次** `verify_keys.py` 写出，故相等；
  两者**只在同一次运行内**才保证一致，跨次比较会不等）。要交付凭据以 `keys.txt` 为准；
  `verify_keys.py` 会把「输入条数」与「落盘行数」两个口径都印出来自证。

  ```bash
  wc -l result/success.jsonl result/keys.txt          # 账号级 / 凭据级
  "$PY" -c "import json;print(len(json.load(open('result/keys_verified.json'))))"
  ```
- `result/keys.txt` —— `email----api_key----api_key_id`（可复制）
- `result/keys_verified.json` —— 机器可读的验收结果

> 为什么分开：`exports/ledger.jsonl` 要留**全部**尝试（含失败的，便于复盘），
> 而交付物只该有成功的。以前两者混在 `exports/` 里，取交付物时得自己筛。
> `result/` 已在 `.gitignore` 里**显式**列出（`*.txt` 没有通配规则覆盖，
> 只靠 `*.json`/`*.jsonl` 挡不住里面的明文 Key）。

> **`apikey_...` 字符串不是终点。** 拿到它只证明创建接口返回了它，
> 不证明它在推理网关上有效。而且 **Jev 不是 OpenAI 兼容的 chat completions**，
> 是 `choice` / `score` / `noul` 结构化判断原语——按"它应该兼容 OpenAI"
> 去写验证脚本会全部误判为失败。

## 3. 自测

```bash
$PY tools/selftest.py      # 262 项，含负对照，**全程离线**（不碰网络）
```

覆盖（**顺序与 `selftest.py` 的打印顺序一致**，项数直接来自实测）：

| 模块 | 段 | 项数 | 钉住什么 |
|---|---|---:|---|
| `test_parsing` | Server Action bound 参数 | 2 | `$ACTION:0` 必须紧凑 JSON（带空格 ⇒ 500） |
| `test_parsing` | Server Action 渲染形态 | 12 | **索引集合不许写死**；`:2` 缺失不许 skip；旧形态不能被弄坏 |
| `test_parsing` | Stytch 落地页 JS 对象字面量 | 4 | 裸键名不是 JSON |
| `test_parsing` | 魔法链接：HTML 实体反转义 | 6 | 🔴 两个后端正文形态不同：Remail 给 **HTML**，`&` 被转义成 `&amp;` ⇒ 不还原则 token 参数名变成 `amp;token`，**等于没传 token**（见 §4.12） |
| `test_parsing` | 魔法链接：残缺候选排在完整之后 | 9 | 🔴 正文里同一 URL 有 **12 份、8 份被截断 4 字符**（`&token=D8…` → `&token…`）⇒ 只取第一个必失败（见 §4.13） |
| `test_ledger` | 台账并集合并（真实词汇） | 15 | **`keyed` 之后重跑失败不许清空 `api_key`** |
| `test_ledger` | 状态词汇覆盖（AST） | 4 | 新增 status 必须登记进 `ledger.RANK`（含历史词汇） |
| `test_ledger` | 身份字段空白归一化 / 只写 LF | 23 | CRLF 污染台账键（实测污染过 39 条）；`verify_keys.py` 补录后必须回读落盘行数并印出两个口径 |
| `test_mailrules` | 收件规则 | 17 | 发件人同域必须叠加 subject；弯引号；CODE/LINK 分组不重叠；**营销流规则与 `SENDER_UPDATES` 不许回归** |
| `test_mailrules` | OTP 抽取 | 10 | 锚定优先；**诱饵在前仍取真码**；非锚定必须显式告警 |
| `test_orchestration` | 编排：认证错误码分流 | 13 | 401/401/403 三条路不能混；400 的 `Unrecognized key` 要点名具体键 |
| `test_orchestration` | auth_callback 请求体形态 | 7 | 站点是 **strict** schema ⇒ 多一个键 = 全员登录失败，只回一句 `400 Bad request`（见 §4.8） |
| `test_orchestration` | 编排：链路无外部阻断点 | 7 | 结构护栏：`stage_apply` / `stage_wait_approval` / `claim` / `watch` **都不许回归** |
| `test_orchestration` | 登录：Server Action 编号漂移 → 重试 | 7 | 缺索引先原样重试，别把编号漂移当"页面结构变了" |
| `test_orchestration` | onboarding：站点门禁驱动 | 11 | 门禁链按站点重定向走；未知步骤报名字；非 `/setup/*` 跳转不许当"已通过" |
| `test_orchestration` | 编排：门禁是引导，不是门槛 | 9 | ★ **门禁卡住但 key 建得出 ⇒ 必须记 `keyed`**（不误报 partial）；同一跳只提交一次；有界停下 |
| `test_orchestration` | 登录：码模式 → 魔法链接回捞 | 5 | 同一次发码可能回**链接**而非码（实测 4 次里 1 次） |
| `test_orchestration` | 编排：确认邮件等待阈值 | 11 | **迟到 ≠ 未发**；阈值不许退回 180s；超时文案必须带轮询计数 |
| `test_orchestration` | 编排：resume 不重复建 key | 4 | `resume` 不许给同一账号建第二把 key（含"没 key 的照常处理"正对照） |
| `test_orchestration` | 编排：setup 降级通路可观测 | 10 | 降级必须打**可辨识告警**；失败时 `error` 指向真因 + 给下一步，不许只回 `HTTP 404` |
| `test_orchestration` | 编排：重跑失败不丢凭据 | 6 | P0 回归（端到端） |
| `test_orchestration` | 编排：成功台账只收成功 | 8 | `result/` 是交付物 ⇒ 失败那次一条都不许写进去 |
| `test_orchestration` | 编排：并发不串号 | 9 | 每个 key 建在**自己**的会话上 |
| `test_orchestration` | 编排：并发 worker 崩溃不静默丢弃 | 7 | 提交数 == 返回数 == 落账数；含"串行路径不吞异常"负对照 |
| `test_orchestration` | 编排：邮箱后端选择（cf / remail） | 27 | 🔴 `_clone()` 不许丢后端（串行复现不出来）；`domain` 语义按后端取；取件凭证跨进程恢复；取全文只在**匹配成功后** |
| `test_orchestration` | 编排：魔法链接候选按序试 | 10 | 完整候选排第一 ⇒ **只发一次请求**；第一条失效 ⇒ **继续试**下一条（判据是**拿到会话**，不是"URL 长得对"） |
| `test_orchestration` | 工具：Remail 探针必须只读（源码级） | 8 | ★ 不许出现 `create_mailbox` / `order` / `purchase` 的**调用点**（含"正则能识别付费调用"负对照）；所有 `_request` 都是 GET；退出码契约 0/1/2/3 |
| `selftest` | 用例登记完整性（AST 元检查） | 1 | 新增 `test_*` 忘记登记 ⇒ **永不执行**，而"通过 N / 失败 0"看起来正常 |

> 合计 **262 项**（26 段 + 1 项入口元检查）。⚠️ 元检查那行**打印在最后一段后面**，
> 所以按输出分段统计时它会被算进**最后一个**用例段（当前是「Remail 探针必须只读」，
> 显示 9 而非 8）——想复算就按"最后一段减 1"处理。
>
> ⚠️ **这个数字是副本，真源是 `tools/selftest.py` 的输出。** 核对方法：
>
> ```bash
> $PY tools/selftest.py | tail -3                       # 看 "通过 N / 失败 0"
> grep -rn "自测 [0-9]* 项\|全套 [0-9]* 项\|合计 \*\*[0-9]* 项" README.md docs/
> ```
>
> 自测本体在 `tools/tests/`（`selftest.py` 只是聚合入口）。**加新测试就落到
> 对应的 `test_*.py`**，别往入口里塞 —— 见 `docs/architecture.md` §1 的目录树。
>
> 两处不一致就说明又漂了。**改了任一段的项数，必须重跑上面第二条命令并逐个同步。**
> （把数字写死在散文里就是"注定漂移的第二份真源" —— 见 `audit-2026-09-20-round2.md` §9.4。）

> 改了 `ledger.RANK` 却没同步管线状态 ⇒ `状态词汇覆盖` 立刻失败。
> 这是 2026-09-20 那个 P0（重跑失败清空 `api_key`）的结构性修复。

## 4. 故障处置

### 4.1 `403 Access restricted`（该邮箱未被邀请）

**不是 bug，是白名单。** 别改请求。

> ⚠️ 2026-09-21 起邀请制已取消，实测回调**直接 200** ⇒ 这个码**不再是常态**。
> 但处置逻辑保留：站点随时可能恢复白名单，而删掉它会让"没被邀请"伪装成
> "凭据错误"，把排查引向重新发码的死循环。

判断顺序（见 `docs/mail-filters.md`）：

1. `--mode scan` 里 `welcome_confirm` 是 0 封吗？→ 是就先跑一次 `full`/`resume`
2. 有确认邮件、`resume` 仍 403 → 抄错邮箱了，核对地址
3. 都排除 → 站点可能重新开了白名单，等通知（没有"查获批邮件"这条路了，
   因为那封邮件不再产生）

### 4.2 `401 Code expired` / `401 Authentication failed`

四个错误码别混：

| 响应 | 含义 | 处置 |
|---|---|---|
| `401 Code expired` | OTP 错/过期（凭据校验在**前**，不看邮箱） | 重新发码，10 分钟内提交 |
| `401 Authentication failed` | 魔法链接 token **已被用过**（一次性） | 换一封邮件里的链接（或重跑，历史链接会被一起用） |
| `403 Access restricted` | 凭据有效，但**不在白名单** | 见 §4.1 |
| `400 Bad request` + `Unrecognized key` | 请求体多/少了键（站点改了 schema） | 见 §4.8 |

**校验顺序**：token 先、白名单后。无效 token 时**根本不看邮箱**——
所以只有"凭据正确时拿到的 403"才算白名单证据。

> 🔴 历史上还有一个**例外**：`--mode claim` 的两进程用法会让 `401 Code expired`
> 表示"会话没接上"而不是"码过期"，照着上表处置会掉进死循环。
> **该模式已于 2026-09-21 整体删除**（连同 `stage_login_with_token()`），
> 所以这个例外不存在了。根因记录留在 §1.5，不要把它读成"还有个模式可用"。

### 4.3 "没收到确认邮件"

先分清三种情况（`--mode scan` 的计数 + 超时文案里的轮询计数会告诉你）：

| 症状 | 根因 | 处置 |
|---|---|---|
| `welcome_confirm` 计数涨了，但流程说没收到 | 收信时序 / 匹配问题 | 看 `mailrules` 是否有漏网主题 |
| 计数没涨，且**轮询次数正常、5xx 为 0** | 站点没发（发信丢包） | **重跑一次** —— 魔法链接 7 天有效 |
| 计数没涨，且**轮询次数很高 / 5xx 多** | 我们**读不出来**（Worker 侧） | 查 Worker，见 §4.4 / §4.5 |
| `--mode scan` 报"漏网主题" | **站点改了文案** | 更新 `src/mailrules.py` 的规则表 |
| 日志里出现 `⚠ 取码走了**降级**路径` | **站点改了邮件模板**，锚定失配 | 去查模板/规则，**不要**去重新发码 |
| 明明收到了码却报 `401 Code expired` | 抽到了**报文头里的数字**而非真码 | 查有没有那行降级警告（见 §4.3.1） |

实测 `oai-d3b08883633d4bc2` 出现过一次超时，**重试即成功**（瞬时问题）。
所以**单次失败先重试，不要直接判死**。

🔴 站点发信**确实会丢包**：实测同一账号 4 次发码里 1 次回的是魔法链接、
另有整批"请求 200 但邮件完全不到"的时段（见 §4.10）。
判据是超时文案里的 `轮询 N 次，5xx M 次` —— 它把"没发"和"读不出来"分开。

### 4.3.1 取码走了降级路径（新增）

`stages.stage_login` 用 `mailrules.extract_otp()` 取码：**模板固定句锚定优先，
宽松正则降级**。走降级时会打：

```
  [login] ⚠ 取码走了**降级**路径（模板锚定失配）主题='Your TypeSafe sign-in code'
          —— 站点可能改了邮件模板，建议跑 --mode scan 看漏网主题
```

**这条警告的含义是"我们可能抽到了错的数字"**（服务端曾抽到 `MTA74-AB1` 这个
MTA 标识）。正确动作是查模板变更 / 跑 `--mode scan`，
**不是**重新发码 —— 重新发码会拿到一模一样的邮件，再走一次降级。

详见 `docs/mail-filters.md` §6。

### 4.4 邮箱服务 5xx

`tempemail` 已内建重试（5xx 重试、4xx 立刻失败）。
语义上：**5xx = "服务端现在读不出来"，不是"邮件不存在"**。
持续 5xx 时 `Stats.http_5xx` 会计数，报错信息里会带出来。

### 4.5 窗口被冲刷（邮件"消失"）

Worker retention 是**全表 100 行的硬窗口**，被同机 OpenXLab 项目
（~54 封/窗口）灌满。超过窗口的邮件**已被物理删除，事后无法找回**。

诊断（直查 D1）：

```bash
TOK="<cfat_ token>"; ACC="<account_id>"; DB="<database_uuid>"
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'content-type: application/json' \
  -d '{"sql":"SELECT COUNT(*), MIN(received_at), MAX(received_at) FROM emails"}' \
  "https://api.cloudflare.com/client/v4/accounts/$ACC/d1/database/$DB/query"
```

**判据**：`COUNT(*)` **恰好等于**接口返回上限（都是 100）⇒ 服务端在删旧行。
再把"窗口下界"与"你关心的那件事的时间"比一下——前者更早才排除"被挤掉"。

> 表名是 **`emails`**，不是 `messages`（`messages` 只是 `/admin/all` 的 JSON 字段名）。
> 先 `SELECT name FROM sqlite_master WHERE type='table'` 拿真实表名。

### 4.6 `/setup/*` 的 Server Action 隐藏域换了形态（**站点改版**，2026-09-20 实测）

**症状**：`login` 成功，但 onboarding 报错。⚠️ **报错文案在 2026-09-21 变过**：

| 时期 | 文案 |
|---|---|
| 2026-09-20 | `onboarding 失败: HTTP 404`（只有这一句，无指向性） |
| 2026-09-21 起 | `提交 tos 后门禁**原地不动**（tos）：HTTP 200 但内容不是有效提交（多半走了降级通路、action id 已失效）｜本次 [tos:via=next-action(6046a5522e4a…),HTTP 200]，门禁序列 [...]` |

新文案把 **`via=`（走的哪条通路）、HTTP 码、门禁序列** 全打出来了 ——
判读时先看 `via=`：`next-action(...)` 就是降级通路，`nojs(action_1)` 才是首选通路。

**根因**：`/setup/*` 页的 Server Action 隐藏域换了形态。

| | 旧（`/login` 至今如此） | 新（`/setup/*`，2026-09-20 起） |
|---|---|---|
| 索引 `<n>` | `2` / `3` / `4` | **`1`** |
| 字段 | `:0` `:1` `:2` | **只有 `:0` `:1`** |
| 额外 | — | **`$ACTION_KEY`**（必须回填） |

旧代码枚举 `("2","3","4")` 且要求 `:0` 与 `:2` **同时存在** ⇒ 新形态一条都抓不到
⇒ `acts` 为空 ⇒ 退化到 `FALLBACK_SETUP_ACTIONS` 里**已作废**的 action id
⇒ POST 回 `404 Server action not found.`。

**为什么难定位**：`/login` 的 `fetch_actions()` 有显式校验（找不到就抛错），
而 `post_setup()` 是 `try/except TypeSafeError: acts = {}` —— **静默退化**。
报错只写 `HTTP 404`，与"索引集合写死了"毫无字面关联。

**处置**：跑探针，把两跳分开看：

```bash
$PY tools/probes/probe_gate_chain.py        # ★ 首选：逐跳打门禁 + 最后试建 key
$PY tools/probes/probe_onboarding.py <email>          # 只读：GET + 看有没有 $ACTION 隐藏域
$PY tools/probes/probe_onboarding.py <email> --post   # 真提交，两条通路分别裸打
```

判读：`A. 渐进增强` 通路 200 而 `B. 降级` 通路 404 ⇒ **解析器的问题，不是站点挂了**。

> ⚠️ 别把"门禁原地不动"一律当改版处置：先确认 **key 建不出来**。
> 若 key 建得出来，那只是 §4.9 的正常留痕。

**已修**：`_actions_from_html` 改成扫**任意** `<n>`、只要求 `:0` 存在，
`:1`/`:2` 有就收没有就不发，并带出 `$ACTION_KEY`
（该函数现已挪到 `src/parsing.py`，公开名 `actions_from_html`）。
自测 `[Server Action 渲染形态]` 12 项钉住（含"旧实现在新形态上必须返回空"的负对照）。

**2026-09-21 补修 —— 上面"为什么难定位"的结构性成因**：`post_setup()` 里的
`except TypeSafeError: acts = {}` 是**静默**的。它把"没抓到隐藏域"吞掉，
再退化到一张**已知全部作废**的 action id 表，最终只留下一句 `HTTP 404`。

| | 修复前 | 修复后 |
|---|---|---|
| 降级时 | 静默，日志里看不出走过降级 | `⚠ post_setup(...) 未抓到 $ACTION_* 隐藏域 —— 退化到降级通路…` |
| 失败时 `error` | `HTTP 404` | `HTTP 404 —— 且本次走的是**降级通路**（未抓到 $ACTION_* 隐藏域：…）；该通路的 action id 为录制值，站点改版后必失效。下一步：跑 tools/probes/probe_onboarding.py 看页面实际渲染形态` |

⇒ **再看到 `onboarding 失败` 时，先看 `error` 里有没有"降级通路"四个字**：
有 ⇒ 就是本节这类问题（解析器 / 站点改版）；没有 ⇒ 才是别的失败。
自测 `[编排：setup 降级通路可观测]` 10 项钉住（含"正常路径不许打降级告警"的负对照）。

### 4.7 长等待被前台超时杀掉

`wait_for_mail(timeout=180)` 这类长等待**不要放前台**——会在 ~120s 被 SIGTERM，
日志只剩半截。更糟的是它会**伪造出一个业务结论**
（曾据此误判"新邮箱收不到确认邮件"，其实只是进程被杀得早）。

**看到日志突然截断，先怀疑进程被杀，再怀疑业务。**

### 4.8 `400 Bad request` —— 站点收紧了回调 schema（2026-09-21 实测）

**症状**：`认证回调 HTTP 400: Bad request`，**所有**账号都这样 —— 包括早已拿到
key 的已获批账号；重试不恢复。台账里最后一次成功建 key 停在 **2026-09-20 15:17**。

**根因**：站点把 `/api/auth/callback` 的请求体 schema 收紧成 **strict** ——
多一个未知键直接拒。响应体是 Zod 的 `flatten()` 格式，其实把话说得很明白：

```json
{"error":"Bad request",
 "details":{"formErrors":["Unrecognized key: \"waitlistEmail\""],"fieldErrors":{}}}
```

我们当时还在传 `waitlistEmail`。邮箱现在由 token/session 在**服务端**推导
（200 响应体里自带 `"email"`），客户端既不需要、也不允许再传。
去掉该键后实测 **HTTP 200**，会话 cookie 正常下发、`redirectTo: "/hook"`。

**判据 —— 先分清是"回调坏了"还是"未获批"**（这一步不能跳）：

```bash
$PY tools/probes/audit_keys_against_site.py --limit 2
```

它只做"登录 + 列 key"，**不建新 key**（不会给已交付账号造出第二把）：

- 已获批账号**也**报 400 ⇒ 回调通路坏了（本节）
- 只有未获批账号报 400/403 ⇒ 才是白名单问题（§4.1）

**处置**：改 `src/typesafe.py::auth_callback` 的请求体键集。
护栏 `自测 [auth_callback 请求体形态]` 逐键钉住，多键/少键都会**离线**先报红。

> ⚠️ **`400 Bad request` 这句文案本身毫无指向性** —— 只看到它就往"验证码错/白名单"
> 上想，会白跑一整轮。`stages._fail_auth` 现在特判 Zod 的 `Unrecognized key`，
> 直接报出是哪个键、该改哪个文件。

### 4.9 `stages.onboarding = partial`（**正常现象，不是故障**，2026-09-21 重写）

**症状**：日志里出现

```
  [onboarding] ⚠ 门禁未归零：遇到不认识的引导步骤 'console-survey'（…）
  [onboarding]   （门禁序列 ['tos', 'set-name', 'console-survey']；仍继续建 key …）
```

然后 `[api_key] apikey_…` 照常出现，最终 `status=keyed`。

🔴 **这是正常的，不要去"修"它。** 站点把 `/hook` 的重定向当**引导**用，
而我们能自动提交的只有前两跳：

| 门禁 | 我们能不能提交 | 说明 |
|---|---|---|
| `tos` | ✅ | `$ACTION_1` 隐藏域在，`post_setup` 走 no-JS 通路 |
| `set-name` | ✅ | 同上（实测降级通路也能成） |
| `console-survey` | ❌ | 该页**没有 `$ACTION_*` 隐藏域**，渲染的是 "Get started / Let's create your org" 向导 |

**决定性证据**：门禁停在 `console-survey` 时，`POST /api/api-keys` 返回
**200 + 明文 key**（2026-09-21 实测，`probe_gate_chain.py` 的最后一节）。
⇒ **门禁不是硬门槛，判据是能不能建出 key。**

复现命令（一次跑完，会逐跳打出 `/hook` 的原始 `location`）：

```bash
$PY tools/probes/probe_gate_chain.py
```

**处置**：什么都不用做。`stages.onboarding = partial` 只是留痕，
`status` 仍然是 `keyed`，key 也通过了验收。

> ⚠️ 历史（2026-09-21 早先版本）：那时判据是"门禁必须归零"，于是这些账号被记成
> `partial`，还试过"靠下一轮换会话补上"（`resume_pending.py --rounds 3`）。
> **现在不需要了** —— 但 `resume_pending.py` 的 `--rounds` 默认值没有改回 1，
> 因为多轮对"发信丢包"仍然有用（见 §4.10）。
>
> ⚠️ 另一个坑：`/hook` **自己非确定性** —— 同一次运行里相邻两次 `GET /hook`
> 会给出不同答案（实测 `set-name` 与 200 交替）。所以**任何**拿它当归零判据的
> 写法都会空转。`complete_onboarding` 现在有 `attempted` 去重，同一跳只提交一次。

### 4.10 发信请求"成功"但邮件**完全不到**（站点发信故障，2026-09-21 实测）

**症状**：报 `未在 Ns 内收到确认邮件`，或发信阶段报
`HTTP 500 Internal Server Error` / `HTTP 503 Service Unavailable`；重试仍不恢复。

⚠️ **`POST /login` 回「已发出」也不能当成「邮件会到」。**
站点会**接受**请求却不发信：2026-09-21 实测 **12 次发码只送到 1 封（~8%）**，
其中**已获批账号也大量收不到**（16:57 那次收到了，17:01 那次就收不到）。
⇒ 判"是否获批"时，**只有"已获批账号收得到、待审批账号收不到"才成立**；
已获批账号自己都在丢信时，任何"未获批"的结论都是**假判据**。

**判据 —— 看超时文案里的轮询计数**（这一步不能跳）：

| 超时文案 | 含义 | 处置 |
|---|---|---|
| `轮询 N 次，5xx 0 次`（N 与等待时长匹配） | 站点**没发** | 重跑；或等站点恢复 |
| `轮询 N 次，5xx M 次`（M 明显 > 0） | 我们**读不出来** | 查 Worker（§4.4 / §4.5） |

> 这两件事的处置**恰好相反**，所以文案必须把它们分开 ——
> 只写一句"没收到"会让运维按老经验去改请求，白跑一轮。

**辅助证据（一起看）**：

1. **D1 直查**（`tools/probes/probe_worker_health.py` 第 2 节）：看 `newest` 距现在多久。
   若 `newest` 很近 ⇒ 收信链路正常，问题在**发信**侧。
2. 🔴 **别拿 `Welcome to TypeSafe — confirm your email` 当"未获批"信号 —— 它恰恰是凭据。**
   它是 Stytch 的**魔法链接**（`stytch_token_type=magic_links`、
   **7 天有效、一次性**）；`mailrules.LINK_RULES` 里的 `welcome_confirm` 就是它。
   2026-09-21 实测它**发给了两个最终拿到 key 的账号**
   ⇒ **与获批状态无关**。曾据正文 `finish creating your account` 把它误判成
   "站点认为该邮箱还没有账号"，方向**是反的**。

⚠️ **别用 `/login` 页形态判任何东西**：站点有 A/B 实验，`/login` 会返回两种页面
（`len=41953 acts=['2','3']` vs `len=43507 acts=['2','3','4']`），
**在已获批和待审批账号里都出现** ⇒ 与账号状态无关。

**处置**：等站点发信恢复后重跑 `tools/resume_pending.py`。故障期间重试只是浪费请求。
（`resume_pending.py` 的 `--rounds 3` 在这里仍然有意义：多轮给丢包留了机会。）

### 4.11 判"这个邮箱到底能不能拿 key"的**唯一可信**方法：直接跑一次

**背景**：旧链路要等 `TypeSafe AI: Your account is ready` 获批邮件，
但共享 Worker 的 D1 窗口只有 100 行、被同机邻居项目刷屏 ⇒ 那封信会被挤掉。
当时 50 个账号**全部停在 `confirmed`，一封获批邮件都没见着**。

🔴 **「窗口里没看到某封邮件」≠「那个状态没发生」**。可信判据只有**站点侧实测**：

| `POST /api/auth/callback` 响应 | 含义 |
|---|---|
| `200` + `{"success":true,"userId":…}` | 凭据有效、会话已建立 |
| `403 {"error":"Access restricted"}` | 未获批（业务门槛，不是技术故障，见 §4.1） |
| `401 Authentication failed` | 凭据一次性、**已被用过** ⇒ 换一封，**别当判据** |

⇒ 对无 key 的账号，**不要靠读邮件推断状态，直接跑一次**：

```bash
$PY tools/run_e2e.py --mode resume --email <邮箱>
```

🔴 **`relogin_pending.py` 会用上"已经躺在收件箱里"的魔法链接。**
`stage_login` 的 `wait_for_mail(since_ms=now-5s)` 只认**请求之后到达**的信
⇒ 上一轮收到、当时没消费掉的链接永远用不上，而它 **7 天有效**。
2026-09-21 实测：一个账号的 `welcome_confirm` 链接在收件箱里躺了 35 分钟，
用它走 `exchange_magic_link → auth_callback` **一次就拿到 200**
（此前该账号一直报超时）。

```bash
$PY tools/relogin_pending.py --email <邮箱> --rounds 3
$PY tools/relogin_pending.py --all-pending      # 台账里所有无 key 账号
```

**刻意串行，别加并发**（它逐账号读收件箱 + 建会话，并发没有收益）。

⚠️ **判据落在"信到了没"，不是"站点回没回 200"** —— 站点会接受请求却不发信（§4.10）。

### 4.12 Remail 后端专属故障（四个，处置各不相同）

> 只在 `--mail-backend remail` 时出现。**共同点**：错误文案都容易把人引向错误方向 ——
> 分别读起来像"站点没发链接"、"Remail 没库存"、"Remail 坏了"、"站点丢包"。

**第 0 步（先跑这个，只读、不下单、不花积分）**：

```bash
$PY tools/probes/probe_remail.py          # 退出码 0 全绿 / 1 有告警 / 2 凭据缺失 / 3 接口不可用
```

一次报全四件事：**凭据**（base/项目/模式/后缀是否齐）· **API Key 体检**（余额，实测 `balance`
就在 profile 响应里）· **项目与库存**（每个商品的 `codePrice`/`purchasePrice` + `totalAvailable`，
并直接给出**全局最便宜的有货档**与"按余额最多还能买 N 单"）· **凭证台账**
（`result/remail_orders.jsonl` 的坏行 / 缺字段 / 同地址重复下单 / **花了钱但没拿到 key** / 后缀漂移）。
**① ~ ④ 里大半问题在这一步就能定位，不用逐个试。**

**① `魔法链接邮件里没找到链接`** —— 两个独立根因，2026-09-21 实测**都踩过**：

| 根因 | 判据 | 状态 |
|---|---|---|
| **只读了 `bodyPreview`**（截断预览：实测 **248 字符、完全无链接**，全文 **4012 字符**才有） | `send: ok`、`mail_wait` 正常，就是 extract 返回空 | 已修：`wait_for_mail` 匹配成功后调 `_hydrate()` 取全文 |
| **全文是 HTML，`&` 被转义成 `&amp;`** | 提取出的 URL 含 `&amp;`，参数名变成 `amp;token` ⇒ **等于没传 token** | 已修：`extract_magic_links()` 加 `html.unescape()`（⚠️ 与 §4.13 的**残缺候选**是两个不同的坑，别互相顶替） |

```bash
# 复算：直接看这个邮箱到底能取到什么（只读，不花钱）
$PY -c "
import sys; sys.path.insert(0,'.')
from src.remail import RemailClient
from src.parsing import extract_magic_link
c = RemailClient(); print('恢复凭证', c.restored, '条')
for m in c.list_mails('<邮箱>'):
    print('preview', len(m.body), '| id', m.id)
    full = c.fetch_body('<邮箱>', m.id)
    print('全文', len(full), '| 提取', extract_magic_link(full)[:120])
"
```

**② `HTTP 422 {"message":"Insufficient inventory."}`** —— 商品**没库存**。

⚠️ 这不是故障，是**商品供给状态**。2026-09-21 实测：曾经最便宜的 `domain`
（0.01 积分/单）**已 0 库存**，而有货的最便宜档是 `outlook.com` **8 积分/单**（**800 倍**）。

```bash
# 判据：看当天哪个商品/后缀有货（只读，不花钱）
$PY -c "
import sys; sys.path.insert(0,'.')
import requests
from src import config
H={'Authorization': f'Bearer {config.REMAIL_API_KEY}','User-Agent': config.UA}
d=requests.get(f'{config.REMAIL_BASE}/v1/open/projects/{config.REMAIL_PROJECT_ID}',
               headers=H,timeout=20).json()
for p in d['products']:
    print(f\"{p['type']:<15} code={str(p['codeEnabled']):<6}{p['codePrice']:<11}库存={p['totalAvailable']}\")
"
```

⇒ 把 `REMAIL_EMAIL_SUFFIX`（`.env` 或 `--domain`）换到一个有货的后缀。
**不要改代码猜** —— 库存是动态的。

**③ `没有 <邮箱> 的 serviceToken`** —— 三种可能，处置完全不同（错误文案里已列全）：

| 可能 | 判据 | 处置 |
|---|---|---|
| 该地址是 **CF 后端**建的 | 域名是你的 CF 收信域 | 换 `--mail-backend cf` |
| 凭证台账 `result/remail_orders.jsonl` 丢了 / 被清 | 文件不存在或明显偏小 | **无法补**：token 已随文件丢失，那些订单只能重新下单 |
| 该地址在本 Remail 账号下确实没下过单 | — | 同上 |

⚠️ `result/remail_orders.jsonl` **不是交付物，别当垃圾清掉** —— 它是跨进程补跑
（`resume_pending.py` / `relogin_pending.py`）取件的**唯一**依据。

**④ 补跑收不到新信（但取件本身正常）** —— `code` 模式邮箱是 **10 分钟窗口**
（`codeWindowMinutes: 10`）。过了窗口收不到新信，但**窗口内收到的旧邮件仍在**。
⇒ 用 `relogin_pending.py`（它**不设 `since_ms`**，会把历史链接一起用）：

```bash
$PY tools/relogin_pending.py --email <邮箱> --mail-backend remail --rounds 2
```

### 4.13 `魔法链接交换失败: 页面长度 256`（**链接被截断**，2026-09-21 实测）

**症状**：一批里少数账号（实测 100 批里 4 个）终态 `failed`，`error` 是
`魔法链接交换失败: 魔法链接页面未找到 dfp 交换参数（链接可能已被使用/过期，页面长度 256）`。
**这行文案会把排查引向"重新发信"——方向是错的，白烧账号。**

**根因**：同一封邮件正文里**同一个 URL 出现多次（实测 12 次），其中一部分被截断了 4 个字符**：

```
完整：…&stytch_token_type=magic_links&token=D8SZNLCEWqnwIucRySURNiIZob5Y5B8rVl7nEenAmiWJ
残缺：…&stytch_token_type=magic_links&tokenSZNLCEWqnwIucRySURNiIZob5Y5B8rVl7nEenAmiWJ
                                            ↑ `=D8` 整个没了
```

残缺形态**等于根本没传 `token`** ⇒ Stytch 回 `400 invalid_public_token_id`（报的是
`public_token` 的**格式**问题，与"少了个参数"毫无字面关联）⇒ 外层把它读成"链接已用/过期"。
旧实现 `extract_magic_link()` **只取第一个匹配**，恰好取到残缺那条 ⇒ 该账号**必失败**，
而**完整的那条就在同一封邮件里**。

⚠️ **`=D8` 是 token 的字面字符，不是 quoted-printable 转义。** 受控实验四形态：

| 形态 | 结果 |
|---|---|
| `&token=D8SZNL…`（原样） | **HTTP 200 + dfp payload** ✅ |
| `&token=SZNL…`（把 `=D8` 补成 `=`） | 400 |
| 把 `=D8` 解成字节 `0xD8` | 400 |

⇒ **不能靠"还原转义"修，只能换一条候选。**

**判据（一眼定案）**：把邮件正文里的 URL 原文打出来，看有没有 `&token` 后面直接跟字符
（缺 `=`）。只看错误文案永远看不出真因。

```bash
# 只读：把该邮箱最近一封确认邮件的所有 URL 候选列出来（不消耗 token）
$PY -c "
import sys; sys.path.insert(0,'.')
from src import config
from src.tempemail import TempMailClient
from src.parsing import extract_magic_links, _looks_complete
cl=TempMailClient(config.TEMPMAIL_BASE, config.TEMPMAIL_ADMIN_KEY)
for m in cl.list_mails('<邮箱>'):
    if 'confirm' in (m.subject or '').lower():
        for u in extract_magic_links(m.body):
            print(('完整' if _looks_complete(u) else '残缺'), u[-60:])
"
```

**处置**：不需要重新发信。直接补跑即可 —— 修复后的 `_exchange_link()` 会**按序试所有候选**，
完整的那条排第一：

```bash
$PY tools/relogin_pending.py --email <邮箱> --rounds 2
```

实测这 4 个账号**第 1 轮就用历史链接**拿到 key（**零成本、未重新发信**）。

**为什么"逐条试"是安全的**：残缺那条 GET 只会拿回 400、**不消耗**一次性 token；成功那条
**一旦拿到 redirect 就立刻返回**，不会继续往下试。

**护栏**：`test_magic_link_truncated_variant`（解析层，9 项）+
`test_magic_link_tries_all_candidates`（编排层，10 项）。⚠️ 编排层那条的替身用**独立的第二实现**
判断"带不带 `&token=`"，避免"用被测代码验证被测代码"。

## 5. 别做这些事

- ❌ 别用 `/admin/all` 拉列表再自己筛——烧 D1 读配额，且会被别人的邮件挤出窗口
- ❌ 别打 `*.workers.dev` 不带浏览器 UA（CF 边缘 403，body 含 `error code: 1010`）
- ❌ 别对 `api.typesafe.ai` 假设 OpenAI 兼容
- ❌ 别为了省事重装/重建环境；优先改配置与代码
- ❌ 别在凭据有效性没被验证之前，把 `403` 当结论
- ❌ 别把"此刻没查到"讲成"不存在"——站点行为有延迟，重跑一次常常就好
- ❌ 别在 `stages` 里就地写取码正则（走 `mailrules.extract_otp`）
- ❌ 别看到降级警告就去重新发码（模板没变的话，重发只会再降级一次）
- ❌ 别把会话 client 挂成 `Pipeline` 的实例字段（并发会串号）
- ❌ 别把 `$ACTION_<n>` 的索引集合写死（`/login` 用 2/3/4，`/setup/*` 用 1）
- ❌ 别信 `FALLBACK_SETUP_ACTIONS` 里的 action id 长期有效（部署一变就作废）。
  它是**最后手段**不是可用通路 —— 走它必定打告警（2026-09-21 起），
  看到告警就去查页面实际渲染形态，别指望它自己能成功
- ❌ 别往 `/api/auth/callback` 的请求体里**加键**：站点是 strict schema，
  多一个键 = 全体账号登录失败，且只回一句 `400 Bad request`（见 §4.8）
- ❌ 别在站点发信故障期间用"没收到邮件"推断"未获批"：先拿**已获批账号**发码做对照，
  它也收不到就说明判据本身被阻断了（见 §4.10）
- ❌ **别拿 `/hook` 的门禁当归零判据**：它是引导、而且**自己非确定性**
  （相邻两次 GET 答案不同）。`stages.onboarding = partial` 是正常留痕（见 §4.9）
- ❌ **别对 `--mode resume` 传台账里已有 `api_key` 的地址**：会造第二把 key，
  而 `Ledger.load()` 按邮箱去重 ⇒ 交付物少一把（见 §1.3）
- ❌ 别试图把已删除的 `apply` / `watch` / `claim` 三个模式"加回来"：
  它们依赖的站点侧环节（申请表单、获批事件、跨进程会话）都不存在或已证伪。
  `test_chain_has_no_external_gate` 会立刻报红
