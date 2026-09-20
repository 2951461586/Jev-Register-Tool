# Runbook：怎么跑 + 故障处置

所有命令**从项目根执行**。

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
```

> ⚠️ **必须用上面这个解释器**。系统 python 缺 `requests`。
> 本机 venv 里**没有** `bs4` / `lxml`——HTML 解析一律 stdlib `re`，别去装。

## 0. 首次准备

```bash
cp .env.example .env      # 填 TEMPMAIL_ADMIN_KEY
$PY tools/run_e2e.py --doctor
```

`--doctor` 会验：配置齐全 → 邮箱服务健康 → 能建邮箱 → 台账可读。

## 1. 五种模式

| 模式 | 干什么 | 会不会发注册请求 |
|---|---|---|
| `--mode apply` | 建邮箱 + 投递申请 + 等确认邮件 | 会（申请段） |
| `--mode scan` | 按规则表给窗口内邮件分桶，**列出漏网主题** | 不会（只读） |
| `--mode watch` | 轮询等获批，**命中即刻自动续跑 4→7** | 命中才会 |
| `--mode resume` | 对指定已获批邮箱跑 4→7 | 会（注册段） |
| `--mode claim` | 人工接力：发码 / 提交外部凭据 | 会（注册段） |

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

🔴 **必须常驻**。审批是批量定时的（实测延迟约 25 分钟），而 Worker 窗口只有
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

```bash
# a) 先让站点把码发到那个邮箱（不提交）
$PY tools/run_e2e.py --mode claim --email me@real.com --send
# b) 从邮箱取到 6 位码，10 分钟内提交
$PY tools/run_e2e.py --mode claim --email me@real.com --token 123456
```

`claim` **不需要** `TEMPMAIL_ADMIN_KEY`，一次只处理一个地址
（凭据一次性 + 10 分钟有效期）。

## 2. 验收（**不算完成，除非跑过这一步**）

```bash
$PY tools/verify_keys.py
```

拿台账里每个 key **真打一次** `POST https://api.typesafe.ai/v1/systemone`，
输出可用/不可用统计，并导出：

- `exports/keys.txt` —— `email----api_key----api_key_id`（可复制）
- `exports/keys_verified.json` —— 机器可读

> **`apikey_...` 字符串不是终点。** 拿到它只证明创建接口返回了它，
> 不证明它在推理网关上有效。而且 **Jev 不是 OpenAI 兼容的 chat completions**，
> 是 `choice` / `score` / `noul` 结构化判断原语——按"它应该兼容 OpenAI"
> 去写验证脚本会全部误判为失败。

## 3. 自测

```bash
$PY tools/selftest.py      # 96 项，含负对照，**全程离线**（不碰网络）
```

覆盖：

| 段 | 项数 | 钉住什么 |
|---|---:|---|
| PoW | 5 | `sha256(salt+secret)` 前缀 + Form-Fields 逐字一致 |
| Server Action bound 参数 | 2 | `$ACTION:0` 必须紧凑 JSON（带空格 ⇒ 500） |
| Stytch JS 字面量 | 4 | 裸键名不是 JSON |
| 台账并集合并（真实词汇） | 12 | **`keyed` 之后重跑失败不许清空 `api_key`** |
| 状态词汇覆盖（AST） | 4 | 新增 status 必须登记进 `ledger.RANK` |
| 收件规则 | 18 | 发件人同域必须叠加 subject；弯引号 |
| OTP 抽取 | 10 | 锚定优先；**诱饵在前仍取真码** |
| 编排：认证错误码分流 | 12 | 401/401/403 三条路不能混 |
| 编排：申请段 / 邀请制 | 7 | 未获批**不算执行失败** |
| 编排：claim 两段式 | 7 | `code_sent` 不是 `failed` |
| 编排：重跑失败不丢凭据 | 6 | P0 回归（端到端） |
| 编排：并发不串号 | 9 | 每个 key 建在**自己**的会话上 |

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

**校验顺序**：token 先、白名单后。无效 token 时**根本不看邮箱**——
所以只有"凭据正确时拿到的 403"才算白名单证据。

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

`pipeline.stage_login` 用 `mailrules.extract_otp()` 取码：**模板固定句锚定优先，
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

### 4.6 长等待被前台超时杀掉

`wait_for_mail(timeout=180)` 这类长等待**不要放前台**——会在 ~120s 被 SIGTERM，
日志只剩半截。更糟的是它会**伪造出一个业务结论**
（曾据此误判"新邮箱收不到确认邮件"，其实只是进程被杀得早）。

**看到日志突然截断，先怀疑进程被杀，再怀疑业务。**

## 5. 别做这些事

- ❌ 别用 `/admin/all` 拉列表再自己筛——烧 D1 读配额，且会被别人的邮件挤出窗口
- ❌ 别打 `*.workers.dev` 不带浏览器 UA（CF 边缘 403，body 含 `error code: 1010`）
- ❌ 别对 `api.typesafe.ai` 假设 OpenAI 兼容
- ❌ 别为了省事重装/重建环境；优先改配置与代码
- ❌ 别在凭据有效性没被验证之前，把 `403` 当结论
- ❌ 别把"此刻没查到"讲成"不存在"——批量审批有时间差
- ❌ 别在 `pipeline` 里就地写取码正则（走 `mailrules.extract_otp`）
- ❌ 别看到降级警告就去重新发码（模板没变的话，重发只会再降级一次）
- ❌ 别把会话 client 挂成 `Pipeline` 的实例字段（并发会串号）
- ❌ 别给 `--mode watch` 加并发（它读的是全表共享窗口）
