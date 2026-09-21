"""收件规则自测：分类 + OTP 抽取（锚定 / 降级两条路径）。"""

from __future__ import annotations

from .support import _link_mail, check, st

def test_mailrules() -> None:
    """收件规则 —— 用**实测抓到的真实 sender/subject**做样本，不是编的。"""
    print("\n[收件规则 mailrules]")
    from src import mailrules as mr

    class M:
        def __init__(self, sender: str, subject: str):
            self.sender, self.subject = sender, subject

    UPD = "envelope.updates.typesafe.ai"
    EM = "em5082.typesafe.ai"
    # ① 两封关键邮件的信封发件人**完全相同** —— 这是本规则表存在的理由
    ready = M(f"010001a0bbb0ae2e-c50c1c38@envelope.updates.typesafe.ai",
              "TypeSafe AI: Your account is ready")
    wait = M(f"010001a0bb95b4b5-722f1aae@envelope.updates.typesafe.ai",
             "TypeSafe AI: You\u2019re on the waitlist for Jev!")
    check("获批邮件命中 account_ready", mr.classify(ready) == "account_ready",
          mr.classify(ready))
    check("申请确认命中 waitlist_confirm", mr.classify(wait) == "waitlist_confirm",
          mr.classify(wait))
    check("两封的信封发件人确实同域（否则不需要叠加 subject）",
          ready.sender.split("@")[-1] == wait.sender.split("@")[-1] == UPD)
    # 🔴 负对照：只按发件人过滤会把"等待名单"误判成"已获批"
    only_sender = mr.MailRule("only_sender", "x", UPD, "")
    check("[负对照] 只按发件人过滤时，两封**无法区分**",
          only_sender.matches(ready) and only_sender.matches(wait))
    check("[负对照] 获批规则不吃申请确认邮件", not mr.get("account_ready").matches(wait))
    check("[负对照] 申请确认规则不吃获批邮件",
          not mr.get("waitlist_confirm").matches(ready))

    # ② 弯引号：直引号写法必须不中，无标点片段必须中
    check("[负对照] 直引号 you're 匹配不到弯引号 You\u2019re",
          not mr.subject_ok(wait, "you're"))
    check("无标点片段 on the waitlist 能中", mr.subject_ok(wait, "on the waitlist"))

    # ③ 确认邮件不能被误判成申请确认（两者都含 'TypeSafe'）
    welcome = M(f"bounces+<acct>-<shard>-oai-x=example-mail.test@em5082.typesafe.ai",
                "Welcome to TypeSafe \u2014 confirm your email")
    check("确认邮件命中 welcome_confirm", mr.classify(welcome) == "welcome_confirm",
          mr.classify(welcome))
    check("[负对照] 确认邮件不被判成申请确认",
          mr.classify(welcome) != "waitlist_confirm")

    # ④ 验证码两种文案都要认（并集）
    # 🔴 这里用 `mr.CODE_RULES`（生产用的同一份分组），**不再按规则名重写一遍** ——
    #    否则自测验的是"我抄的名字对不对"，而不是"生产的分组对不对"。
    c1 = M(f"bounces+<acct>-22e6-a@em5082.typesafe.ai", "Your TypeSafe sign-in code")
    c2 = M("pm_bounces@pm-bounces.typesafe.ai", "Your TypeSafe verification code")
    check("sign-in code 命中", mr.any_of(*mr.CODE_RULES)(c1))
    check("verification code 也命中", mr.any_of(*mr.CODE_RULES)(c2))
    check("[负对照] 验证码规则不吃获批邮件",
          not mr.any_of(*mr.CODE_RULES)(ready))

    # ④b 生产分组必须与 `stages` 实际用的谓词一致 —— 这是"第二份真源"的护栏。
    #     2026-09-20 二轮审计：`stages`（当时叫 `pipeline`）曾用
    #     `any_of("signin_code","verify_code")` 重写一遍分组，与 `CODE_RULES`
    #     是同一件事的两份定义 ⇒ 加规则会漏改。
    check("★ stages.MATCH_CODE 就是 any_of(*CODE_RULES)",
          st.MATCH_CODE(c1) and st.MATCH_CODE(c2) and not st.MATCH_CODE(ready))
    check("★ stages.MATCH_LINK 就是 any_of(*LINK_RULES)",
          st.MATCH_LINK(_link_mail("x@example-mail.test"))
          and not st.MATCH_LINK(ready))
    check("[负对照] CODE_RULES 与 LINK_RULES 不重叠（码/链接不能互相命中）",
          not any(r.name in {x.name for x in mr.CODE_RULES} for r in mr.LINK_RULES))

    # ⑤ 邻居项目的邮件必须被划为"外来"，不能污染我们的桶
    oxl = M("no-reply@dm.openxlab.org.cn", "【OpenXLab】注册激活")
    check("[负对照] OpenXLab 邮件被判为外来", not mr.sender_ok(oxl))
    check("OpenXLab 邮件不进任何桶", mr.classify(oxl) == "unknown")

    # ⑥ diagnose：漏网主题必须被列出来（站点改文案的唯一可见信号）
    d = mr.diagnose([ready, wait, welcome, c1, c2, oxl,
                     M(f"x@em5082.typesafe.ai", "TypeSafe: 全新文案 we never saw")])
    check("diagnose 统计外来邮件数", d["foreign"] == 1, str(d["foreign"]))
    check("diagnose 列出漏网主题", len(d["unclaimed_subjects"]) == 1,
          str(d["unclaimed_subjects"]))
    check("diagnose 分桶正确", len(d["buckets"]["account_ready"]) == 1
          and len(d["buckets"]["waitlist_confirm"]) == 1)


def test_otp_extraction() -> None:
    """OTP 抽取：锚定优先、宽松降级，且降级必须**可被察觉**。"""
    print("\n[OTP 抽取（锚定 / 降级）]")
    from src.mailrules import extract_otp

    code, how = extract_otp("Your code:\n\n123456 is your one-time code\n")
    check("模板句在 → 走锚定", (code, how) == ("123456", "anchored"), f"{code}/{how}")

    # ★ 核心回归：正文里**先**出现一个诱饵 6 位数字（模拟报文头里的 MTA 标识）
    decoy = "Message-ID: <MTA74-AB1>\nTicket 999888 created\n\n123456 is your one-time code"
    code, how = extract_otp(decoy)
    check("★ 诱饵在前时仍取真码（这就是 MTA74-AB1 那个坑的客户端版本）",
          code == "123456", f"取到了 {code!r}")
    check("该用例走的是锚定路径（不是靠运气）", how == "anchored", how)

    # 负对照：把锚定关掉，同一段文本就会抽错 —— 证明锚定确实在起作用
    from src.mailrules import OTP_LOOSE_RE
    loose = OTP_LOOSE_RE.search(decoy)
    check("[负对照] 只用宽松正则时确实会抽到诱饵（证明锚定不是摆设）",
          loose is not None and loose.group(1) == "999888",
          loose.group(1) if loose else "None")

    check("无模板句 → 降级到 loose（可察觉）",
          extract_otp("Your TypeSafe verification code: 654321") == ("654321", "loose"))
    check("全角数字经 NFKC 归一后可锚定命中",
          extract_otp("１２３４５６ is your one-time code") == ("123456", "anchored"))
    check("非 ASCII 连字符（U+2011）也能锚定命中",
          extract_otp("123456 is your one\u2011time code") == ("123456", "anchored"))
    check("无码 → none", extract_otp("Your TypeSafe sign-in code") == ("", "none"))
    check("空串 → none", extract_otp("") == ("", "none"))
    check("[负对照] 7 位数字不该被截成 6 位",
          extract_otp("1234567 is your one-time code") == ("", "none"))
