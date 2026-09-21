# 收件过滤规则

> 格式对齐同机 OpenXLab 项目的做法：**信封发件人子串做第一道过滤，主题子串做第二道。**
> 实现：`src/mailrules.py`　自测：`tools/selftest.py`（`[收件规则 mailrules]` 段 21 项，
> `[OTP 抽取（锚定 / 降级）]` 段 10 项；全套 184 项 —— 以 `$PY tools/selftest.py`
> 末行的 `通过 N / 失败 0` 为准，本文不重复维护这个数字的副本）

## 1. 为什么要两道过滤

实测（2026-09-20，Worker 窗口内 100 封）拿到的**信封发件人**分布：

| 信封发件人 | 封数 | 主题 |
|---|---:|---|
| `envelope.updates.typesafe.ai` | 15 | `TypeSafe AI: Your account is ready` ← **获批** |
| `envelope.updates.typesafe.ai` | 9 | `TypeSafe AI: You're on the waitlist for Jev!` ← **申请确认** |
| `em5082.typesafe.ai` | 2 | `Welcome to TypeSafe — confirm your email` |
| `em5082.typesafe.ai` / `pm-bounces.typesafe.ai` | 20 | `Your TypeSafe sign-in code` / `…verification code` |
| `dm.openxlab.org.cn` | 54 | `【OpenXLab】注册激活` ← 邻居项目 |

### 🔴 核心事实：要区分的这两封，发件人**完全相同**

`Your account is ready`（获批）和 `You're on the waitlist`（申请确认）
信封发件人**都是** `envelope.updates.typesafe.ai`。

**只按发件人过滤 ⇒ 会把"你在等待名单上"当成"你已获批"**，
进而对未获批的账号去跑注册段，拿到 `403 Access restricted` 还以为是白名单问题。
所以 **subject 是必需的判别位，不是锦上添花**。

> 这也是 OpenXLab 那条规则（`sender_contains="openxlab"`）**不能直接照搬**的原因：
> 它一类邮件一个域（`dm.openxlab.org.cn`），我们两类邮件共用一个域。

### ⚠️ 注意：`sender` 是**信封**发件人，不是人类可读的 From

| | 值 |
|---|---|
| 人类可读 From（`.eml` 里） | `TypeSafe AI <team@updates.typesafe.ai>` |
| 信封发件人（Worker 的 `sender` 字段） | `010001a0bbb0ae2e-c50c1c38@envelope.updates.typesafe.ai` |

用 `team@updates.typesafe.ai` 去做子串匹配**一条都中不了**。
规则表里的常量全部取自实测的 `sender` 字段。

顺带一个免费的校验位：事务流的信封地址是 **VERP** 格式，
里面**内嵌了收件人**：

```
bounces+<acct>-<shard>-oai-ecc230d8aa7f4bf2=example-mail.test@em5082.typesafe.ai
                            └────────── 收件人 ──────────┘
```

可以拿来做一致性交叉验证，但**不要**用它替代 `to` 字段（`to` 才是权威）。

## 2. 规则表

| 规则名 | 阶段 | `sender_contains` | `subject_contains` | 说明 |
|---|---|---|---|---|
| `waitlist_confirm` | 2-confirm | `envelope.updates.typesafe.ai` | `on the waitlist` | 申请确认。正文 "We'll be in touch when it's your turn." —— **不是**获批 |
| `account_ready` | 3-approved | `envelope.updates.typesafe.ai` | `account is ready` | 🔴 **唯一**能解锁注册段的信号 |
| `welcome_confirm` | 4-login | `typesafe.ai` | `confirm your email` | Stytch 注册确认信，魔法链接 7 天有效 |
| `signin_link` | 4-login | `typesafe.ai` | `sign in to typesafe` | 登录魔法链接（`/login` 的 ACTION_2 触发） |
| `signin_code` | 4-login | `typesafe.ai` | `sign-in code` | 6 位验证码，10 分钟、一次性 |
| `verify_code` | 4-login | `typesafe.ai` | `verification code` | 验证码的另一种文案，取码时两者都要收 |

规则里同时写了 `subject_excludes` 做**负对照**，防止一条规则吃掉另一条：

```python
MailRule(name="account_ready",  subject_contains="account is ready",
         subject_excludes=("waitlist",))
MailRule(name="waitlist_confirm", subject_contains="on the waitlist",
         subject_excludes=("account is ready",))
```

## 3. 两个已踩的坑

### 3.1 主题里是**弯引号** U+2019

```
TypeSafe AI: You’re on the waitlist for Jev!
             └─ U+2019，不是 U+0027
```

按 `you're`（直引号）匹配**永远不中**。所以规则只用无标点片段
（`on the waitlist`），并在匹配前做一次 `NFKC` 归一化兜底。
自测里有负对照钉住这一点：

```
✓ [负对照] 直引号 you're 匹配不到弯引号 You’re
✓ 无标点片段 on the waitlist 能中
```

### 3.2 别用正则，用子串

主题里有 em dash（`—`）、弯引号、变体选择符。正则只会更脆。
`MailRule` 的两个字段都是**大小写不敏感的子串**。

## 4. 用法

```python
from src.mailrules import RULES, CODE_RULES, LINK_RULES, get, any_of, classify, diagnose, sender_ok

rule = get("account_ready")
if rule.matches(mail): ...          # 规则对象本身可直接当谓词

# 并集：取 6 位码时两条规则都要收。
# 🔴 `any_of` 收的是 **MailRule 对象**，不是规则名（2026-09-20 二轮审计改的）——
#    收名字会让"哪些规则算码"出现两份定义，加规则时改一边漏一边。
MATCH_CODE = any_of(*CODE_RULES)

classify(mail)      # -> "account_ready" / "unknown"
sender_ok(mail)     # 只按发件人：用来把"不是我们的"与"是我们的但主题不认识"分开
```

`stages.py` 里已统一改为读规则表，不再散落 subject 子串：

```python
MATCH_WAITLIST_CONFIRM = get_rule("waitlist_confirm")
MATCH_ACCOUNT_READY    = get_rule("account_ready")
MATCH_CODE             = any_of(*CODE_RULES)     # ← 分组只有这一处定义
MATCH_LINK             = any_of(*LINK_RULES)
```

## 5. 漏网主题必须显式报出来

站点改文案时，只报"没收到邮件"是查不出原因的。`diagnose()` 会把
**"是我们的但没规则认领"**的主题单独列出来：

```
$ python tools/run_e2e.py --mode scan
  · waitlist_confirm      9 封   [2-confirm]
  ★ account_ready        15 封   [3-approved]
  · welcome_confirm       2 封   [4-login]
  · signin_code           9 封   [4-login]
  · signin_link           0 封   [4-login]
  · verify_code          11 封   [4-login]

  邻居项目/无关邮件（发件人不含 typesafe.ai）：54 封
  ✓ 没有漏网主题：窗口内所有 TypeSafe 邮件都被规则覆盖
```

**判据**：`--mode scan` 报出漏网主题 ⇒ 站点改了文案，去更新规则表；
报 `✓ 没有漏网主题` 却仍然拿不到码 ⇒ 问题在别处（配额、窗口、发信失败）。

## 6. 取验证码：锚定优先，宽松降级

主题过滤只保证"这封是验证码邮件"，**不保证从正文里抽出来的数字是对的**。
所以 `src/mailrules.py` 提供 `extract_otp()`：

```python
from src.mailrules import extract_otp
code, how = extract_otp(mail.body)      # how ∈ {"anchored", "loose", "none"}
```

| `how` | 判据 | 可信度 |
|---|---|---|
| `anchored` | 命中模板固定句 `(\d{6})(?=\s+is\s+your\s+one-time\s+code)` | 高，与服务端 D1 规则 id=20 **同构** |
| `loose` | 锚定失配，退回"第一个 6 位数字" | **低** —— 可能抽到报文头里的标识 |
| `none` | 一个都没找到 | — |

### 为什么不能只写 `\b\d{6}\b`（实测事故）

服务端最初也是取"第一个 6 位数字"，结果 3 封 Postmark 投递的邮件抽出来是
**`MTA74-AB1`** —— 一个 MTA 标识，而不是真验证码。根因是抽取器把
**未入库的原始报文头**也当来源，而"短横线码"分支的优先级高于"纯 6 位数字"分支。

修法是**用模板固定句做前瞻锚定**（不是调分支优先级）。客户端此前仍是
`re.findall(r"\b\d{6}\b", body)` 取 `codes[0]` —— **同一个坑的孪生版本**，
而且失败表现是 `401 Code expired`，runbook 对它的处置是"重新发码"，
**会把排查引向完全错误的方向**。2026-09-20 审计后改为与服务端同构。

### 降级必须可察觉

`how == "loose"` 时 `stages.stage_login` 会打一行警告：

```
  [login] ⚠ 取码走了**降级**路径（模板锚定失配）主题='…' —— 站点可能改了邮件模板
```

**没有这行警告却拿到了 `loose` 结果 = 日志被忽略了**，不是没问题。
看到它就说明"站点改了模板"，正确动作是跑 `--mode scan` 看漏网主题，
而不是去重新发码。

### 自测钉住了什么

`[OTP 抽取]` 段 10 项，其中两条是关键：

- **诱饵在前**：`Message-ID: <MTA74-AB1>\nTicket 999888 created\n\n123456 is your one-time code`
  ⇒ 必须取到 `123456`，不是 `999888`
- **负对照**：同一段文本只用宽松正则会取到 `999888` —— 证明锚定不是摆设

另外覆盖全角数字（NFKC 归一后命中）、非 ASCII 连字符 U+2011、
7 位数字不被截成 6 位。

---

## 附：魔法链接现在可从管理页直接取（2026-09-20 起）

**背景**：以前取 `confirm your email` / `sign in to typesafe` 的魔法链接，
必须自己拉 `raw_text` 再写正则抠 URL。原因是 Worker 的 `extracted_json`
只有 `fallbackCode`（只认验证码），链接从来抽不出来。

**现在**：Worker 的 D1 `rules` 表新增了 id=19 规则，与 OpenXLab 激活链接同形：

| 字段 | 值 |
|---|---|
| `sender_filter` | `@(?:[^@\s]+\.)*typesafe\.ai$` |
| `subject_filter` | `""`（留空 = 不限主题） |
| `pattern` | `(https://login\.typesafe\.ai/[^\s"'<>)\]]*token=[^\s"'<>)\]]+)` |

效果：**新到的** confirm / 登录邮件会自动带上
`extracted_json = [{"value": "https://login.typesafe.ai/v1/magic_links/redirect?…", "type": "link"}]`，
管理页渲染成 `Activation Link` 卡片（Copy link + Open in new tab）。
存量邮件已于 09-20 回填完毕。

**对本项目的意义**：

- 取链可以直接读 `/api/inbox` 的 `extracted_json`，**不必再解析 `raw_text`**。
  比正则更稳——不受正文里 HTML 副本、跟踪链接、换行折叠的影响。
- `src/tempemail.py` 的 `Mail` 已带 `raw`，但 `extracted_json` 需自己从 `raw` 里取；
  若要长期走这条路，建议在 `Mail` 上加一个 `extracted` 属性（**本次未改代码**）。

**为什么主题留空**：TypeSafe 的魔法链接至少有两个主题文案
（`Welcome to TypeSafe — confirm your email`、`sign in to typesafe`），
猜一个必漏一个。而 `login.typesafe.ai` + 强制 `token=` 双重锚定已足够唯一，
宽发件人 + 空主题不会误抽。

**注意**：`Your TypeSafe sign-in code`（OTP 邮件）抽出的仍是 `type: "code"`
（验证码），不是链接——那封邮件本来就该给码。两者不冲突。
