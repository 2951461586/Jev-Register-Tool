"""收件过滤规则表 —— TypeSafe 侧。

格式对齐同机 OpenXLab 项目的做法（`sender_contains="openxlab"`）：
**信封发件人子串做第一道过滤，主题子串做第二道。**

2026-09-21：邀请制取消，营销流整体消失
──────────────────────────────────────
本表曾经要处理**六类**邮件，其中两类来自营销流（Loops/SES 的
`envelope.updates.typesafe.ai`）：

    envelope.updates.typesafe.ai   x15   TypeSafe AI: Your account is ready      ← 获批
    envelope.updates.typesafe.ai   x9    TypeSafe AI: You're on the waitlist...   ← 申请确认

TypeSafe 取消邀请制后，**这两类邮件不再产生**（申请与审批两个环节都没了），
对应的 `waitlist_confirm` / `account_ready` 两条规则连同 `SENDER_UPDATES`
常量一起删除。现在窗口内只剩**事务流**（SendGrid `em5082` / Postmark
`pm-bounces`）：

    em5082.typesafe.ai             Welcome to TypeSafe — confirm your email  ← 主路径凭据
    em5082.typesafe.ai / pm-bounces.typesafe.ai  Your TypeSafe sign-in code
    em5082.typesafe.ai / pm-bounces.typesafe.ai  Your TypeSafe verification code
    dm.openxlab.org.cn             【OpenXLab】注册激活                       ← 邻居项目

⚠️ **删除的判据是可实测的**，不要凭印象把规则加回来：窗口内若再出现
   `envelope.updates.typesafe.ai` 的信，`--mode scan` 会把它列进
   "漏网主题"（这正是 `diagnose()` 存在的意义），届时再按实际文案补规则。

为什么仍然**必须叠加主题**（这条教训与邀请制无关，长期有效）
────────────────────────────────────────────────────────────
历史上"申请确认"与"获批"两封信的**信封发件人完全相同**，只按发件人过滤会把
"你在等待名单上"误判成"你已获批"，进而对未获批的账号跑注册段、拿到 403 还以为
是白名单问题。⇒ 只按域过滤永远不够稳，**主题是必需的判别位**。
（OpenXLab 那条规则不能直接照搬的原因也在这：它一类邮件一个域。）

另外两个坑
──────────
1. 主题里可能出现**弯引号** U+2019（`You’re …`）。
   按 `you're`（直引号 U+0027）匹配**永远不中**。所以规则只用无标点的片段，
   并在匹配前做一次 Unicode 归一化兜底。
2. 信封发件人里**内嵌了收件人地址**（VERP 回弹编码）：
   `bounces+<acct>-<shard>-oai-ecc230d8aa7f4bf2=example-mail.test@em5082.typesafe.ai`
   这可以当一条免费的收件人一致性校验用，但**不要**把它当收件人字段的替代
   （`to` 才是权威字段）。

引用方式
────────
    from .mailrules import RULES, get, sender_ok, subject_ok, extract_otp

    rule = get("welcome_confirm")
    if rule.matches(mail): ...

    code, how = extract_otp(mail.body)     # how ∈ {"anchored","loose","none"}
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

# ── 发件人域常量（改这里就够了，不要在规则里散落字符串） ───────────────
# 全部 TypeSafe 流量的信封域都以此结尾（em5082 / pm-bounces）
SENDER_TYPESAFE = "typesafe.ai"
#
# ⚠️ 这里曾有一个 `SENDER_UPDATES = "envelope.updates.typesafe.ai"`，
# 2026-09-21 随邀请制取消一并删除 —— 它只被 `waitlist_confirm` /
# `account_ready` 两条规则引用，而那两条已不存在。
# 删除是**功能性的**（不是清理）：留着它会让人以为营销流还要处理。
#
# ⚠️ 更早还删过一个 `SENDER_TRANSACTIONAL = ("em", "pm-bounces")`，零引用。
# 那条的教训仍然适用：**注释声称的分层必须在代码里真的落地**，
# 否则下一个按注释理解的人会以为"事务流只匹配 `em*`/`pm-bounces*`"，实际匹配整个域。
# 现在只剩一类流量，按域匹配（`SENDER_TYPESAFE`）就够，不需要再分层。


def _norm(s: str) -> str:
    """NFKC 归一化 + 小写。

    NFKC 会把弯引号/全角字符折叠成 ASCII 形态，所以 `You’re` 与 `You're`
    归一化后都能被 `on the waitlist` 之外的写法命中 —— 但规则里仍只用
    无标点片段，双保险。
    """
    return unicodedata.normalize("NFKC", s or "").lower()


@dataclass(frozen=True)
class MailRule:
    """一条收件规则。

    sender_contains / subject_contains 都是**大小写不敏感的子串**，
    不是正则 —— 主题里有 em dash、弯引号、变体选择符，正则只会更脆。
    """

    name: str
    stage: str
    sender_contains: str
    subject_contains: str
    subject_excludes: tuple[str, ...] = ()
    note: str = ""

    def matches(self, mail: Any) -> bool:
        sender = _norm(getattr(mail, "sender", "") or "")
        subject = _norm(getattr(mail, "subject", "") or "")
        if self.sender_contains and _norm(self.sender_contains) not in sender:
            return False
        if self.subject_contains and _norm(self.subject_contains) not in subject:
            return False
        if any(_norm(x) in subject for x in self.subject_excludes):
            return False
        return True

    def __call__(self, mail: Any) -> bool:
        """让规则本身可直接当 `wait_for_mail` 的谓词用。"""
        return self.matches(mail)


# ── 规则表 ────────────────────────────────────────────────────────────
# 顺序无关，靠 name 取；subject_excludes 是**负对照**，防止一条规则吃掉另一条。
#
# 阶段标签反映**当前**链路的阶段序号（`1-signup` → `2-login`）：
#   1-signup  POST /login 发信 → 收确认邮件（唯一入口，取代了旧链路的 1-3 段）
#   2-login   换 token / 取码 → 认证回调
RULES: tuple[MailRule, ...] = (
    MailRule(
        name="welcome_confirm",
        stage="1-signup",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="confirm your email",
        note="🔴 **主路径的唯一凭据来源**（2026-09-21 起）。"
             "`POST /login` 提交邮箱后站点直接回这封 —— 不再需要先申请、再等获批。"
             "正文里的 Stytch 魔法链接 **7 天有效**，是重试登录时最稳的凭据。"
             "⚠️ 历史注记：邀请制时期它「不是解锁条件」（没有它也能注册成功），"
             "那个前提**已随邀请制取消而失效**，现在它就是入口。",
    ),
    MailRule(
        name="signin_code",
        stage="2-login",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="sign-in code",
        note="6 位登录验证码，10 分钟有效、一次性。"
             "由 `/login` 的「Email me a code instead」分支（ACTION_3）触发。",
    ),
    MailRule(
        name="signin_link",
        stage="2-login",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="sign in to typesafe",
        note="Stytch 登录魔法链接（由 /login 的 ACTION_2 触发）。"
             "与 welcome_confirm 都是魔法链接，但**触发源不同**："
             "welcome_confirm 来自首次注册的 `/login` 提交，这条来自已注册账号的登录。",
    ),
    MailRule(
        name="verify_code",
        stage="2-login",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="verification code",
        note="6 位验证码的另一种文案。与 signin_code 分开是为了日志可读；"
             "取码时两者都要收。",
    ),
)

_BY_NAME = {r.name: r for r in RULES}

#: 取 6 位码时要同时接受的两条规则
CODE_RULES: tuple[MailRule, ...] = (_BY_NAME["signin_code"], _BY_NAME["verify_code"])

#: 走魔法链接时两条规则都要接受（触发源不同，形态一样）
LINK_RULES: tuple[MailRule, ...] = (_BY_NAME["welcome_confirm"], _BY_NAME["signin_link"])


# ── OTP 抽取：锚定优先，宽松降级 ──────────────────────────────────────
#
# 🔴 为什么不能只写 `\b\d{6}\b`（这是踩过的坑，客户端版本）：
#
#   服务端（Worker 的 D1 `rules` id=20）最初也是取"第一个 6 位数字"，
#   结果 3 封 Postmark 投递的邮件抽出来是 `MTA74-AB1` —— 一个 **MTA 标识**，
#   而不是真验证码。根因是抽取器把**未入库的原始报文头**也当来源，
#   而"短横线码"分支的优先级高于"纯 6 位数字"分支。
#
#   修法不是调优先级，而是**用模板固定句做前瞻锚定**：
#
#       (?<![A-Za-z0-9])(\d{6})(?=\s+is\s+your\s+one-time\s+code)
#
#   好处：自校验（必须紧跟模板句），且不会误命中正文里散落的数字。
#
#   客户端此前仍是 `re.findall(r"\b\d{6}\b", body)` 取 `codes[0]` ——
#   **同一个坑的孪生版本**，而且失败表现是 `401 Code expired`，
#   runbook 对它的处置是"重新发码"，会把排查引向完全错误的方向。
#   2026-09-20 审计后改为与服务端同构。

#: 锚定式：6 位数字必须紧跟在模板固定句前。
#: 先用 `_norm` 归一化（NFKC 顺带把全角数字折成半角），所以这里不用 IGNORECASE。
#: 连字符收 `-` / U+2010 / U+2011 —— NFKC 不保证把各种破折号都折成 ASCII。
OTP_ANCHORED_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{6})(?=\s+is\s+your\s+one[\-\u2010\u2011]time\s+code)"
)

#: 降级式：任意 6 位数字（两侧不能再贴数字）。
#: **只在锚定式失配时使用**，且调用方必须显式记录自己走了降级路径 ——
#: 走了降级就等于回到了"可能抽到 MTA 标识"的老坑，必须能从日志里看出来。
OTP_LOOSE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def extract_otp(text: str) -> tuple[str, str]:
    """从邮件正文里抽 6 位验证码。返回 `(code, how)`。

    `how` 取值：

    | 值 | 含义 | 可信度 |
    |---|---|---|
    | `"anchored"` | 命中模板固定句锚定 | 高，与服务端同构 |
    | `"loose"` | 锚定失配，退回"第一个 6 位数字" | **低** —— 可能抽到报文头里的标识 |
    | `"none"` | 一个都没找到 | — |

    调用方（`stages.stage_login`）必须在 `how == "loose"` 时**把这件事写进日志**，
    否则"站点改了邮件模板"会伪装成"验证码过期"。
    """
    if not text:
        return "", "none"
    m = OTP_ANCHORED_RE.search(_norm(text))
    if m:
        return m.group(1), "anchored"
    m = OTP_LOOSE_RE.search(text)
    if m:
        return m.group(1), "loose"
    return "", "none"


def get(name: str) -> MailRule:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"未知规则 {name!r}，可用：{sorted(_BY_NAME)}") from None


def any_of(*rules: MailRule):
    """把多条规则合成一个谓词（OR）。

    🔴 参数是 **`MailRule` 对象**，不是规则名 —— 2026-09-20 二轮审计改的。

    以前收的是名字（`any_of("signin_code", "verify_code")`），于是"哪些规则算验证码"
    有了**两份定义**：本文件里的 `CODE_RULES` / `LINK_RULES` 常量，
    和 `stages.py` 里按名字重写一遍的 `any_of("signin_code", "verify_code")`。
    危害不是"多写了几个字"，而是：加一条新验证码规则时**改一边漏一边** ⇒
    新规则不生效；而 `--mode scan` 会显示"窗口内所有 TypeSafe 邮件都被规则覆盖"
    （因为 `RULES` 表里确实有这条），把排查引向"D1 窗口被挤爆 / 邮箱坏了"。

    ⇒ 现在只有一处定义：`any_of(*CODE_RULES)` / `any_of(*LINK_RULES)`。
    """
    return lambda m: any(r.matches(m) for r in rules)


# ── 分诊断用的谓词 ────────────────────────────────────────────────────
def sender_ok(mail: Any, sender_contains: str = SENDER_TYPESAFE) -> bool:
    """只看发件人。用来把"不是我们的邮件"与"是我们的但主题不认识"分开。"""
    return _norm(sender_contains) in _norm(getattr(mail, "sender", "") or "")


def subject_ok(mail: Any, subject_contains: str) -> bool:
    return _norm(subject_contains) in _norm(getattr(mail, "subject", "") or "")


def classify(mail: Any) -> str:
    """返回命中的规则名；一条都不命中返回 "unknown"。"""
    for r in RULES:
        if r.matches(mail):
            return r.name
    return "unknown"


def diagnose(mails: Iterable[Any]) -> dict[str, Any]:
    """把一批邮件按规则分桶 + 列出"是我们的但没规则认领"的漏网主题。

    用于 `--mode scan`：**漏网主题必须显式列出来**，
    否则站点改了文案我们只会看到"没收到邮件"，查半天。
    """
    buckets: dict[str, list[Any]] = {r.name: [] for r in RULES}
    buckets["unknown"] = []
    foreign = 0
    for m in mails:
        if not sender_ok(m):
            foreign += 1
            continue
        buckets[classify(m)].append(m)
    unclaimed = sorted({(getattr(m, "subject", "") or "").strip()
                        for m in buckets["unknown"]})
    return {"buckets": buckets, "foreign": foreign, "unclaimed_subjects": unclaimed}
