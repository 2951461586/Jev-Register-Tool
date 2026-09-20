"""收件过滤规则表 —— TypeSafe 侧。

格式对齐同机 OpenXLab 项目的做法（`sender_contains="openxlab"`）：
**信封发件人子串做第一道过滤，主题子串做第二道。**

为什么必须两道（这是本文件存在的全部理由）
────────────────────────────────────────────
实测窗口内 6 类邮件的**信封发件人**（注意：是 VERP 信封地址，不是人类可读的 From）：

    envelope.updates.typesafe.ai   x15   TypeSafe AI: Your account is ready      ← 获批
    envelope.updates.typesafe.ai   x9    TypeSafe AI: You're on the waitlist...   ← 申请确认
    em5082.typesafe.ai             x2    Welcome to TypeSafe — confirm your email
    em5082.typesafe.ai / pm-bounces.typesafe.ai  Your TypeSafe sign-in code
    em5082.typesafe.ai / pm-bounces.typesafe.ai  Your TypeSafe verification code
    dm.openxlab.org.cn             x54   【OpenXLab】注册激活                      ← 邻居项目

🔴 **"申请确认"和"获批"的信封发件人完全相同**（都是 `envelope.updates.typesafe.ai`）。
   只按发件人过滤 ⇒ 会把"你在等待名单上"误判成"你已获批"，
   进而对未获批的账号去跑注册段，拿到 403 还以为是白名单问题。
   ⇒ **必须叠加主题。** 这也是 OpenXLab 那条规则不能直接照搬的原因：
      它一类邮件一个域，我们两类邮件共用一个域。

另外两个坑
──────────
1. 主题里的是**弯引号** U+2019：`You’re on the waitlist`。
   按 `you're`（直引号 U+0027）匹配**永远不中**。所以规则只用无标点的片段
   （`on the waitlist`），并在匹配前做一次 Unicode 归一化兜底。
2. 信封发件人里**内嵌了收件人地址**（VERP 回弹编码）：
   `bounces+<acct>-[<shard>-]oai-ecc230d8aa7f4bf2=example-mail.test@em5082.typesafe.ai`
   这可以当一条免费的收件人一致性校验用，但**不要**把它当收件人字段的替代
   （`to` 才是权威字段）。

引用方式
────────
    from .mailrules import RULES, get, sender_ok, subject_ok

    rule = get("account_ready")
    if rule.matches(mail): ...
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

# ── 发件人域常量（改这里就够了，不要在规则里散落字符串） ───────────────
# 全部 TypeSafe 流量的信封域都以此结尾（envelope.updates / em5082 / pm-bounces）
SENDER_TYPESAFE = "typesafe.ai"
# 营销/通知流：申请确认 + 获批**共用**这个域
SENDER_UPDATES = "envelope.updates.typesafe.ai"
# 事务流：确认邮件 + 验证码（SendGrid `em*` 与 Postmark `pm-bounces` 两条腿）
SENDER_TRANSACTIONAL = ("em", "pm-bounces")


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
RULES: tuple[MailRule, ...] = (
    MailRule(
        name="waitlist_confirm",
        stage="2-confirm",
        sender_contains=SENDER_UPDATES,
        subject_contains="on the waitlist",
        subject_excludes=("account is ready",),
        note="申请确认。正文 'We'll be in touch when it's your turn.' —— "
             "**不是**获批，别混。",
    ),
    MailRule(
        name="account_ready",
        stage="3-approved",
        sender_contains=SENDER_UPDATES,
        subject_contains="account is ready",
        subject_excludes=("waitlist",),
        note="🔴 获批信号，**唯一**能解锁注册段的邮件。"
             "与 waitlist_confirm 同域，所以 subject 是必需的判别位。",
    ),
    MailRule(
        name="welcome_confirm",
        stage="4-login",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="confirm your email",
        note="Stytch 注册确认信。实测**不是**解锁条件（没有它的地址也能注册成功），"
             "但它的魔法链接 7 天有效，是重试登录时最稳的凭据来源。",
    ),
    MailRule(
        name="signin_code",
        stage="4-login",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="sign-in code",
        note="6 位登录验证码，10 分钟有效、一次性。",
    ),
    MailRule(
        name="signin_link",
        stage="4-login",
        sender_contains=SENDER_TYPESAFE,
        subject_contains="sign in to typesafe",
        note="Stytch 登录魔法链接（由 /login 的 ACTION_2 触发）。"
             "与 welcome_confirm 都是魔法链接，但触发源不同："
             "welcome_confirm 来自注册表单，这条来自登录表单。",
    ),
    MailRule(
        name="verify_code",
        stage="4-login",
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


def get(name: str) -> MailRule:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"未知规则 {name!r}，可用：{sorted(_BY_NAME)}") from None


def any_of(*names: str):
    """把多条规则合成一个谓词（OR）。"""
    rs = [get(n) for n in names]
    return lambda m: any(r.matches(m) for r in rs)


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
