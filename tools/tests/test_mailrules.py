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

    EM = "em5082.typesafe.ai"
    # ① 主路径凭据：确认邮件（2026-09-21 起它是链路的**唯一入口**）
    welcome = M("bounces+<acct>-<shard>-oai-x=example-mail.test@em5082.typesafe.ai",
                "Welcome to TypeSafe \u2014 confirm your email")
    check("确认邮件命中 welcome_confirm", mr.classify(welcome) == "welcome_confirm",
          mr.classify(welcome))

    # ② 弯引号：直引号写法必须不中，无标点片段必须中
    #    （这条与邀请制无关 —— 站点的营销文案里出现过 U+2019，长期有效）
    curly = M(f"x@{EM}", "You\u2019re on the waitlist")
    check("[负对照] 直引号 you're 匹配不到弯引号 You\u2019re",
          not mr.subject_ok(curly, "you're"))
    check("无标点片段 on the waitlist 能中", mr.subject_ok(curly, "on the waitlist"))

    # ③ 验证码两种文案都要认（并集）
    c1 = M("bounces+<acct>-22e6-a@em5082.typesafe.ai", "Your TypeSafe sign-in code")
    c2 = M("pm_bounces@pm-bounces.typesafe.ai", "Your TypeSafe verification code")
    check("sign-in code 命中", mr.any_of(*mr.CODE_RULES)(c1))
    check("verification code 也命中", mr.any_of(*mr.CODE_RULES)(c2))
    check("[负对照] 验证码规则不吃确认邮件",
          not mr.any_of(*mr.CODE_RULES)(welcome))

    # ④ 生产分组必须与 `stages` 实际用的谓词一致 —— 这是"第二份真源"的护栏。
    #    2026-09-20 二轮审计：`stages`（当时叫 `pipeline`）曾用
    #    `any_of("signin_code","verify_code")` 重写一遍分组，与 `CODE_RULES`
    #    是同一件事的两份定义 ⇒ 加规则会漏改。
    check("★ stages.MATCH_CODE 就是 any_of(*CODE_RULES)",
          st.MATCH_CODE(c1) and st.MATCH_CODE(c2) and not st.MATCH_CODE(welcome))
    check("★ stages.MATCH_LINK 就是 any_of(*LINK_RULES)",
          st.MATCH_LINK(_link_mail("x@example-mail.test"))
          and not st.MATCH_LINK(c1))
    check("[负对照] CODE_RULES 与 LINK_RULES 不重叠（码/链接不能互相命中）",
          not any(r.name in {x.name for x in mr.CODE_RULES} for r in mr.LINK_RULES))

    # ⑤ 邀请制取消后，**营销流规则必须不再存在** —— 这条是"负向护栏"：
    #    留着 account_ready 会让人以为"还要等获批邮件"，从而把链路读成 6 段。
    names = {r.name for r in mr.RULES}
    check("★ 营销流规则已删除（account_ready / waitlist_confirm）",
          not (names & {"account_ready", "waitlist_confirm"}), str(sorted(names)))
    check("★ 主路径凭据规则在（welcome_confirm）", "welcome_confirm" in names)
    check("★ 营销流发件人常量 SENDER_UPDATES 已删除",
          not hasattr(mr, "SENDER_UPDATES"))

    # ⑥ 邻居项目的邮件必须被划为"外来"，不能污染我们的桶
    oxl = M("no-reply@dm.openxlab.org.cn", "【OpenXLab】注册激活")
    check("[负对照] OpenXLab 邮件被判为外来", not mr.sender_ok(oxl))
    check("OpenXLab 邮件不进任何桶", mr.classify(oxl) == "unknown")

    # ⑦ diagnose：漏网主题必须被列出来（站点改文案的唯一可见信号）
    d = mr.diagnose([welcome, c1, c2, oxl,
                     M(f"x@{EM}", "TypeSafe: 全新文案 we never saw")])
    check("diagnose 统计外来邮件数", d["foreign"] == 1, str(d["foreign"]))
    check("diagnose 列出漏网主题", len(d["unclaimed_subjects"]) == 1,
          str(d["unclaimed_subjects"]))
    check("diagnose 分桶正确", len(d["buckets"]["welcome_confirm"]) == 1,
          str({k: len(v) for k, v in d["buckets"].items() if v}))


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
