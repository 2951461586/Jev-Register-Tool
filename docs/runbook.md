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

必填三项：`TEMPMAIL_ADMIN_KEY`（建邮箱 / `/admin/*`）、`TEMPMAIL_BASE`（Worker 根地址）、
`TEMPMAIL_DOMAIN`（建邮箱用哪个域名）。
选填：`CF_API_TOKEN` / `CF_ACCOUNT_ID` / `CF_D1_ID`（只有 `tools/probes/*` 诊断脚本用）、
`TEMPMAIL_DOMAINS`（`probe_email_routing.py` 要查的域名，留空则问 Worker 的 `/health`）。

> 🔴 **代码里没有任何真实默认值** —— 以前 `config.py` 的 `TEMPMAIL_BASE` / `TEMPMAIL_DOMAIN`
> 带着真实值当默认，探针里还写着活的 `cfat_` 令牌。`os.getenv(k, "真值")` 就是泄漏点：
> 它让"忘了配"和"配好了"在代码里长得一样，而且会被 git 一路带上去。
> 现在缺哪项由 `validate()` / `validate_cf()` **显式报出来**，`--doctor` 直接列缺项。

`--doctor` 会验：配置齐全 → 邮箱服务健康 → 能建邮箱 → 台账可读。

## 1. 五种模式

| 模式 | 干什么 | 会不会发注册请求 |
|---|---|---|
| `--mode apply` | 建邮箱 + 投递申请 + 等确认邮件 | 会（申请段） |
| `--mode scan` | 按规则表给窗口内邮件分桶，**列出漏网主题** | 不会（只读） |
| `--mode watch` | 轮询等获批，**命中即刻自动续跑 4→7** | 命中才会 |
| `--mode resume` | 对指定已获批邮箱跑 4→7 | 会（注册段） |
| `--mode claim` | ⚠️ **实验性；两进程用法已知失效**（见 §1.5） | 会（注册段） |

### 并发（`--concurrency`）

`apply` / `full` / `resume` 支持 `--concurrency N`（默认 1 = 串行，行为与加并发前一致）：

```bash
$PY tools/run_e2e.py --mode resume --email a@example-mail.test --email b@example-mail.test \
  --email c@example-mail.test --concurrency 3
```

- 并发安全的**前提**是每个账号只读**自己的**收件箱索引端点（`/api/inbox?email=`）。
  申请段与注册段都满足。
- 每个 worker 用**独立的 `Pipeline` 实例**（各自的 `requests.Session` 与 `stats`），
  唯一共享的是带锁的 `Ledger`。会话 client **不再是实例字段** ——
  挂实例字段在串行时看不出问题，并发会**串号**（A 的 key 建在 B 的会话上）。
- 🔴 **`--mode watch` 不支持并发**，传了会被直接拒绝。
  它读的是**全表共享窗口**（`/admin/all`，retention 只有 100 行），
  并发读不会更快，只会互相抢同一批行 + 放大 D1 读配额。
  要提速就缩短 `--watch-interval`。

### 1.1 投递申请

```bash
$PY tools/run_e2e.py --mode apply --count 5
```

单账号约 3~6s（含等确认邮件）。**这一段的终点是"确认邮件到达"，不是"获批"。**

🔴 **看到 `未在 Ns 内收到 waitlist 确认邮件` 不要直接当成失败。**
2026-09-20 实测：连跑 10 批次时确认邮件的到达延迟**单调爬升**
（41.7 / 52.9 / 52.8 / 43.9 / 68.6 / 86.7 / 93.6 / 192 / 198 s），
阈值当时是 180s，最后两条被判 `failed` —— 但收件箱复核显示它们
**在超时后 12s / 18s 就落了库**，是**假阴性**。

同一天还发现**两例延迟约 20 分钟**的（09:01 / 09:04 提交 → 09:21:28 才入库）。
那段时间正好横跨共享 Worker 的故障恢复窗口（09:19:29 才恢复落库），
所以更像是**故障期积压/重投**而非常态延迟；两例后来都正常获批，
也说明**确认邮件没收到并不影响审批**。

> ⇒ 阈值只是"本次不等了"，**不是**"这封邮件不会来了"。
> 任何固定阈值都会漏掉长尾 —— 所以处置必须是"先回查，再决定"。

🔴 **超时不再记为 `failed`，而是 `applied`（申请已投递）**（2026-09-20 改）。
理由是有**硬证据**的：当天 5 个"确认邮件超时"的账号**后来全部获批**，
其中一个的确认邮件**至今从未到达** ⇒ 回执与申请注册是**两件独立的事**。
把回执超时记成 `failed`，会让这些账号从"待复查"清单里消失 ——
与 P0（台账等级语义）是同一类错误：**不报错，只是少几行**。
`applied` 在 `ledger.RANK` 里是 1 分，低于 `confirmed`(2) / `approved`(3) / `keyed`(5)，
所以后续复查仍会把它正确升级，不会锁死。

> 护栏：`tools/selftest.py` 的 `[编排：确认邮件等待阈值]` 段会把阈值 ≥ 240s
> 钉住、检查文案点明"申请已注册 / 勿重投"，并用**负对照**断言超时**不许**被记成
> `failed`。改回旧行为自测立刻失败。

处置顺序（**先复核，再决定要不要重投**）：

1. 回查该邮箱的收件箱索引端点，确认邮件到底有没有到（迟到 ≠ 未发）：

   ```bash
   $PY tools/run_e2e.py --mode scan     # 看窗口内到底有什么
   ```

2. **优先用 `--mode watch` 接住下游**。`watch` **完全不看台账状态** ——
   它直接扫 Worker 窗口里所有 `account is ready` 邮件，谁获批就续跑 4→7。
   所以哪怕确认回执永远没到，只要申请注册成功了，链路照样能走完。
3. 只有表单提交本身失败（`stages.apply != ok`）才算真的失败；
   表单 201 但回执未到 ⇒ **不要重投**（重投只会再产生一个重复申请）。
4. 阈值默认 `CONFIRM_TIMEOUT = 300s`。需要临时覆盖用 `--confirm-timeout`。
   在"回执普遍迟到"的时段，**不要为了等回执把阈值调大** ——
   那会让整批跑得极慢；正确做法是调小阈值（快速投递）+ 用 `watch` 接下游。

### 1.2 看窗口里到底有什么（最常用的诊断）

```bash
$PY tools/run_e2e.py --mode scan
```

**这是"我到底收到了什么"的唯一可信来源**，比凭印象说"我好像收到过"靠谱。
判据见 `docs/mail-filters.md` §5。

### 1.3 等获批并自动续跑（**批量出号的标准姿势**）

```bash
$PY tools/run_e2e.py --mode watch --watch-timeout 900 --watch-interval 15 \
  > exports/watch.log 2>&1 &
```

🔴 **必须常驻**。审批是批量定时的（实测延迟约 15~25 分钟），而 Worker 窗口只有
**~37 分钟**（100 行 ÷ 邻居刷屏速率）——"等邮件到了再去取"这种设计必然漏掉。
`watch` 是**边到边取**：命中当场消费。

它不依赖台账地址（扫全窗口），所以申请是在别处提交的也能接上。

### 1.4 对已获批邮箱补跑

```bash
$PY tools/run_e2e.py --mode resume \
  --email a@example-mail.test --email b@example-mail.test
```

零申请请求，可反复跑（幂等）。**失败重试就用这个。**

### 1.5 人工接力（获批邮箱是真人邮箱时）

> 🔴 **先读这段，不要直接照抄命令。**
>
> `--mode claim` 的**两进程用法已知失效**（2026-09-20 实测）：`--send` 与
> `--token` 是**两个独立进程**，各自新建 `TypeSafeClient()`，中间**没有任何会话传递**。
> 实测**码在 2 分钟内提交仍报 `401 Code expired`**。
>
> ⚠️ 而 §4.2 对这个错误码的处置写的是"重新发码，10 分钟内提交" ——
> **照着做会掉进死循环**。这就是必须在这里显式警告的原因：错误码与根因毫无字面关联。

**第一选择：`resume`**（同一进程内完成"发码 → 收码 → 提交"，会话是连续的）：

```bash
$PY tools/run_e2e.py --mode resume --email me@real.com
```

> 只要邮箱在 Worker 覆盖的域内（= 我们自己建的临时邮箱），**一律用 `resume`**。
> 它才是"失败重试"的正常入口（见 §1.4）。

**只有当邮箱是真人邮箱、Worker 读不到时**才轮到 `claim`，而它当前是坏的：

```bash
# ⚠️ 以下流程 2026-09-20 实测报 401 Code expired，仅作为"待修复路径"的记录，
#    不要当成可用流程。
$PY tools/run_e2e.py --mode claim --email me@real.com --send
$PY tools/run_e2e.py --mode claim --email me@real.com --token 123456
```

**根因候选（⚠️ 未验证 —— 等一次真人邮箱场景实测）**：

`stage_login()` 在提交回调**之前**会先 `GET /login?waitlist=<email>` 建立会话
（经由 `fetch_actions()`），而 `stage_login_with_token()` **完全没有这一步** ——
它上来就 `POST /api/auth/callback`。两条路唯一的差别就在这里，所以最可能的原因是
回调缺少 `/login` 页面种下的会话 cookie。

**验证方法**（不要跳过这一步就宣布修好了）：在 `stage_login_with_token()` 开头补一次
`cl.s.get(config.SITE_LOGIN, params={"waitlist": rec.email})`，然后用一个真实获批邮箱
重跑两进程流程。若 401 消失即证实。

> 本项目已经有过"判据跑在错误的层上 ⇒ 假阴性"的教训（见 `tools/resume_pending.py`
> 的 docstring：按"末行胜出"计数会把重跑失败记录压掉，导致计数**下降**、
> 守卫误判"没有新增"而提前收工）。
> **在拿到实测凭据之前，这个修复只能记为"候选"，不能记为"已完成"。**

`claim` **不需要** `TEMPMAIL_ADMIN_KEY`，一次只处理一个地址
（凭据一次性 + 10 分钟有效期）。

## 2. 验收（**不算完成，除非跑过这一步**）

```bash
$PY tools/verify_keys.py
```

拿台账里每个 key **真打一次** `POST https://api.typesafe.ai/v1/systemone`，
输出可用/不可用统计，并把**成功数据**导出到 `result/`（交付物目录）：

- `result/success.jsonl` —— 成功账号（从台账补录，幂等；每次跑批也会自动追加）
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
$PY tools/selftest.py      # 184 项，含负对照，**全程离线**（不碰网络）
```

覆盖（**顺序与 `selftest.py` 的打印顺序一致**，项数直接来自实测）：

| 模块 | 段 | 项数 | 钉住什么 |
|---|---|---:|---|
| `test_parsing` | PoW | 5 | `sha256(salt+secret)` 前缀 + Form-Fields 逐字一致 |
| `test_parsing` | Server Action bound 参数 | 2 | `$ACTION:0` 必须紧凑 JSON（带空格 ⇒ 500） |
| `test_parsing` | Server Action 渲染形态 | 12 | **索引集合不许写死**；`:2` 缺失不许 skip；旧形态不能被弄坏 |
| `test_parsing` | Stytch JS 字面量 | 4 | 裸键名不是 JSON |
| `test_ledger` | 台账并集合并（真实词汇） | 15 | **`keyed` 之后重跑失败不许清空 `api_key`** |
| `test_ledger` | 状态词汇覆盖（AST） | 4 | 新增 status 必须登记进 `ledger.RANK` |
| `test_ledger` | 身份字段空白归一化 / 只写 LF | 9 | CRLF 污染台账键（实测污染过 39 条） |
| `test_mailrules` | 收件规则 | 21 | 发件人同域必须叠加 subject；弯引号；CODE/LINK 分组不重叠 |
| `test_mailrules` | OTP 抽取 | 10 | 锚定优先；**诱饵在前仍取真码**；非锚定必须显式告警 |
| `test_orchestration` | 编排：认证错误码分流 | 12 | 401/401/403 三条路不能混 |
| `test_orchestration` | 编排：申请段 / 邀请制 | 9 | 未获批**不算执行失败**；计时器不重叠 |
| `test_orchestration` | 登录：Server Action 编号漂移 → 重试 | 7 | 缺索引先原样重试，别把编号漂移当"页面结构变了" |
| `test_orchestration` | 登录：码模式 → 魔法链接回捞 | 5 | 同一次发码可能回**链接**而非码（实测 4 次里 1 次） |
| `test_orchestration` | 编排：确认邮件等待阈值 | 6 | **迟到 ≠ 未发**；阈值不许退回 180s |
| `test_orchestration` | 监听：不重复建 key | 3 | `watch` 续跑不许给同一账号建第二把 key |
| `test_orchestration` | 编排：claim 两段式 | 7 | `code_sent` 不是 `failed`（**单进程内**的语义；两进程失效见 §1.5） |
| `test_orchestration` | 编排：重跑失败不丢凭据 | 6 | P0 回归（端到端） |
| `test_orchestration` | 编排：成功台账只收成功 | 8 | `result/` 是交付物 ⇒ 失败那次一条都不许写进去 |
| `test_orchestration` | 编排：并发不串号 | 9 | 每个 key 建在**自己**的会话上 |
| `test_orchestration` | 编排：并发 worker 崩溃不静默丢弃 | 7 | 提交数 == 返回数 == 落账数；含"串行路径不吞异常"负对照 |
| `test_orchestration` | 编排：setup 降级通路可观测 | 10 | 降级必须打**可辨识告警**；失败时 `error` 指向真因 + 给下一步，不许只回 `HTTP 404` |
| `test_orchestration` | 登录：`/api/auth/callback` 请求体键集 | 7 | 站点是 **strict** schema ⇒ 多一个键 = 全员登录失败，只回一句 `400 Bad request`（2026-09-21 实测，见 §4.8） |
| `test_orchestration` | onboarding：合并提交 + `/api/me` 读滞后 | 5 | POST 报错后**必须回读** `/api/me`：缺口关了就是成功，不许把"已经好了"记成失败（实测误报 76%，见 §4.9） |
| `selftest` | 用例登记完整性（AST 元检查） | 1 | 新增 `test_*` 忘记登记 ⇒ **永不执行**，而"通过 N / 失败 0"看起来正常 |

> 合计 **184 项**（23 段 + 1 项入口元检查）。
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

**不是 bug，是白名单。** 别改请求，去等获批。

判断顺序（见 `docs/mail-filters.md`）：

1. `--mode scan` 里 `account_ready` 是 0 封吗？→ 是就还没获批，继续 `watch`
2. `account_ready` 有，但 `resume` 仍 403 → 抄错邮箱了，核对地址

### 4.2 `401 Code expired` / `401 Authentication failed`

三个错误码别混：

| 响应 | 含义 | 处置 |
|---|---|---|
| `401 Code expired` | OTP 错/过期（凭据校验在**前**，不看邮箱） | 重新发码，10 分钟内提交 |
| `401 Authentication failed` | 魔法链接 token **已被用过**（一次性） | 换一封邮件里的链接 |
| `403 Access restricted` | 凭据有效，但**不在白名单** | 等获批 |
| `400 Bad request` + `Unrecognized key` | 请求体多/少了键（站点改了 schema） | 见 §4.8 |

**校验顺序**：token 先、白名单后。无效 token 时**根本不看邮箱**——
所以只有"凭据正确时拿到的 403"才算白名单证据。

> 🔴 **一个已知的例外，别照上表处置**：如果你用的是 `--mode claim` 的两进程用法
> （`--send` 然后另起一个进程 `--token`），那么 `401 Code expired` **不是**
> "码过期/抽错码"，而是**会话没接上**——两个进程各建一套会话，凭据再新也没用。
> 此时"重新发码"是**无效处置**，会把你推进死循环。
> 判据：同一个码，用 `--mode resume` 走单进程能过、用 claim 两进程必失败。
> 详见 §1.5。

### 4.3 "没收到验证码邮件"

先分清三种情况（`--mode scan` 的计数会告诉你）：

| 症状 | 根因 | 处置 |
|---|---|---|
| `signin_code` / `verify_code` 计数涨了，但流程说没收到 | 收信时序 / 匹配问题 | 看 `mailrules` 是否有漏网主题 |
| 计数没涨 | 发信没成功或窗口被冲刷 | 重试；确认窗口下界时间戳 |
| `--mode scan` 报"漏网主题" | **站点改了文案** | 更新 `src/mailrules.py` 的规则表 |
| 日志里出现 `⚠ 取码走了**降级**路径` | **站点改了邮件模板**，锚定失配 | 去查模板/规则，**不要**去重新发码 |
| 明明收到了码却报 `401 Code expired` | 抽到了**报文头里的数字**而非真码 | 查有没有那行降级警告（见 §4.3.1） |

实测 `oai-d3b08883633d4bc2` 出现过一次 `未收到验证码邮件`，**重试即成功**
（瞬时问题）。所以**单次失败先重试，不要直接判死**。

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

### 4.6 `onboarding 失败: HTTP 404`（**站点改版**，2026-09-20 实测）

**症状**：`login` 成功（说明邀请门槛已过），但统一报 `onboarding 失败: HTTP 404`。

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
$PY tools/probes/probe_onboarding.py <email>          # 只读：GET + 看有没有 $ACTION 隐藏域
$PY tools/probes/probe_onboarding.py <email> --post   # 真提交，两条通路分别裸打
```

判读：`A. 渐进增强` 通路 200 而 `B. 降级` 通路 404 ⇒ **解析器的问题，不是站点挂了**。

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

### 4.9 `onboarding` 卡在最后一跳（`/setup/console-survey` 抓不到 `$ACTION_*`）

**症状**：`--mode resume` / `--mode full` 报

```
onboarding 失败: HTTP 404 —— 且本次走的是**降级通路**（未抓到 $ACTION_* 隐藏域：
/setup/console-survey?returnTo=%2Fhook 未渲染出 Server Action 隐藏域…）
```

`status=partial`，而 `/api/me` 显示 `needs_tos=False / needs_name=False /
needs_survey=True` —— **前两跳都成功了，只有第三跳没做完**。

**判据 —— 先分清"会话问题"还是"站点改版"**（别直接按改版处置）：

用**新会话**重跑同一个账号（`--mode resume` 会重新登录 ⇒ 天然是新会话）。

- 新会话**立刻成功** ⇒ 本节这种情况，**不是**页面结构变了
- 新会话**也失败** ⇒ 才是真改版，跑 `probes/probe_onboarding.py --post`

**根因（实测，未完全归因）**：站点对 `/setup/*` 的渲染**取决于会话状态**。
同一个会话里（2026-09-21 逐跳记录）：

| 动作 | GET 到的页面 | `$ACTION_*` |
|---|---|---|
| 登录后 GET `/setup/tos` | len 39490，"Tell us about yourself" 表单 | `['1']` ✓ |
| POST `/setup/tos` → GET `/setup/set-name` | 同上 | `['1']` ✓ |
| POST `/setup/set-name` → GET `/setup/console-survey` | **len 40507** | **（无）** ✗ |

而**同一会话内轮询 62 秒都不恢复**；**换一个新会话立刻就有 `$ACTION_1`**。
⇒ **onboarding 的最后一跳在同一会话里做不到。**

**处置**：多跑一轮 —— 靠换会话补上。

```bash
$PY tools/resume_pending.py            # 默认 --rounds 3，第 2 轮就会补上
```

⚠️ **`--rounds 1` 不够**（实测：25 个里 19 个停在 `partial`，**76%**）。
`resume_pending.py` 的默认值本来就是 3，**不要为了"省一轮"把它调小**。

> 若单跑一个账号，第二次 `--mode resume` 即可（已验证 3 个账号：
> `login ok → onboarding ok → api_key ok`，直接拿到 key）。

## 5. 别做这些事

- ❌ 别用 `/admin/all` 拉列表再自己筛——烧 D1 读配额，且会被别人的邮件挤出窗口
- ❌ 别打 `*.workers.dev` 不带浏览器 UA（CF 边缘 403，body 含 `error code: 1010`）
- ❌ 别对 `api.typesafe.ai` 假设 OpenAI 兼容
- ❌ 别为了省事重装/重建环境；优先改配置与代码
- ❌ 别在凭据有效性没被验证之前，把 `403` 当结论
- ❌ 别把"此刻没查到"讲成"不存在"——批量审批有时间差
- ❌ 别在 `stages` 里就地写取码正则（走 `mailrules.extract_otp`）
- ❌ 别看到降级警告就去重新发码（模板没变的话，重发只会再降级一次）
- ❌ 别把会话 client 挂成 `Pipeline` 的实例字段（并发会串号）
- ❌ 别给 `--mode watch` 加并发（它读的是全表共享窗口）
- ❌ 别把 `$ACTION_<n>` 的索引集合写死（`/login` 用 2/3/4，`/setup/*` 用 1）
- ❌ 别信 `FALLBACK_SETUP_ACTIONS` 里的 action id 长期有效（部署一变就作废）。
  它是**最后手段**不是可用通路 —— 走它必定打告警（2026-09-21 起），
  看到告警就去查页面实际渲染形态，别指望它自己能成功
- ❌ 别看到 `confirm` 超时就直接重投申请（先回查收件箱，迟到 ≠ 未发）
- ❌ 别往 `/api/auth/callback` 的请求体里**加键**：站点是 strict schema，
  多一个键 = 全体账号登录失败，且只回一句 `400 Bad request`（见 §4.8）
- ❌ 别把 `resume_pending.py` 的 `--rounds` 调到 1：onboarding 的**最后一跳
  在同一会话里做不到**，第 2 轮换会话才补得上（实测 76% 会停在 partial，见 §4.9）
