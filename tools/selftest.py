#!/usr/bin/env python
"""自测：把几个"静默失败"的点钉住。

每个检查项都配**负对照** —— 只验"该过的过了"是不够的，
还要证明检查器本身有能力判负。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import framer_waitlist as fw  # noqa: E402
from src import typesafe as ts  # noqa: E402
from src.ledger import Ledger  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def test_pow() -> None:
    print("\n[PoW]")
    secret, digest = fw.pow_secret()
    ts_ms, _, token = secret.partition(":")
    check("secret 形如 <ms>:<token>", ts_ms.isdigit() and len(token) == 30,
          f"got {secret!r}")
    check("sha256(salt+secret) 以 000 开头",
          hashlib.sha256(("framer" + secret).encode()).hexdigest().startswith("000"))
    check("与浏览器实测格式一致",
          digest == hashlib.sha256(("framer" + secret).encode()).hexdigest())
    # 负对照：换 salt 必须不再满足
    bad = hashlib.sha256(("wrong" + secret).encode()).hexdigest()
    check("[负对照] 换 salt 后不再满足难度", not bad.startswith("000"), bad[:8])
    check("Framer-Form-Fields 与 HAR 逐字一致",
          fw.form_fields_header()
          == "email,__framer_0,__framer_1,__framer_2,__framer_3,__framer_4,__framer_5")


def test_compact_ref() -> None:
    print("\n[Server Action bound 参数]")
    a = {"id": "60e492c6afe6018dbb5fb596f90cddf1a3e0b3db7d", "bound": "$@1"}
    s = ts._compact_ref(a)
    check("紧凑、无空格", s == '{"id":"60e492c6afe6018dbb5fb596f90cddf1a3e0b3db7d","bound":"$@1"}', s)
    check("[负对照] 默认 json.dumps 会带空格（这就是 500 的根因）",
          json.dumps(a) != s and '"id": ' in json.dumps(a))


def test_js_object() -> None:
    print("\n[Stytch 落地页 JS 对象字面量]")
    sample = ("{ public_token: 'public-token-live-abc', "
              "redirect_url: 'https://console.typesafe.ai/auth/callback?token=T&waitlist=a%40b.com', "
              "magic_id: 'magic-live-1' }")
    d = ts._parse_js_object(sample)
    check("裸键名可解析", d.get("public_token") == "public-token-live-abc", str(d))
    check("URL 值完整", d.get("redirect_url", "").endswith("waitlist=a%40b.com"))
    check("token 可从中抽出",
          ts.TypeSafeClient.token_from_redirect_url(d["redirect_url"]) == "T")
    # 负对照：标准 JSON 解析必须失败（证明它不是 JSON）
    try:
        json.loads(sample)
        check("[负对照] json.loads 应该失败", False, "居然解析成功了")
    except json.JSONDecodeError:
        check("[负对照] json.loads 确实失败（所以不能用 JSON 解析）", True)


def test_ledger_union() -> None:
    print("\n[台账并集合并]")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.jsonl"
        led = Ledger(p)
        # 基线
        led.append({"key": "a@x.com", "status": "success", "email": "a@x.com",
                    "api_key": "k1"})
        base = len(led.load())
        check(f"基线 {base} 条", base == 1)

        # 增量写回：**只带增量字段**（不交超集），必须仍然真的写进去
        led.upsert_many([{"key": "a@x.com", "status": "success", "token": "T1"}])
        rec = {r["key"]: r for r in led.load()}["a@x.com"]
        check("旧字段没丢（api_key 还在）", rec.get("api_key") == "k1", str(rec))
        check("新字段真的写进去了（token）", rec.get("token") == "T1", str(rec))

        # 同级、字段数相等 —— 启发式算法会在这里失手，并集不会
        led.upsert_many([{"key": "a@x.com", "status": "success", "extra": "E"}])
        rec = {r["key"]: r for r in led.load()}["a@x.com"]
        check("字段数相等时新字段仍写入", rec.get("extra") == "E", str(rec))
        check("此前所有字段仍在",
              all(k in rec for k in ("api_key", "token", "extra")), str(rec))

        # 升级替换
        led.upsert_many([{"key": "a@x.com", "status": "failed", "error": "x"}])
        rec = {r["key"]: r for r in led.load()}["a@x.com"]
        check("[负对照] 低等级不会覆盖高等级", rec.get("status") == "success", str(rec))

        # 不缩水
        led.upsert_many([{"key": "b@x.com", "status": "success"}])
        check("新增不丢旧数据", len(led.load()) == 2, str(len(led.load())))

        # 幂等
        led.upsert_many([{"key": "b@x.com", "status": "success"}])
        check("重复写入幂等", len(led.load()) == 2)


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
    welcome = M(f"bounces+<acct>-[<shard>-]oai-x=example-mail.test@em5082.typesafe.ai",
                "Welcome to TypeSafe \u2014 confirm your email")
    check("确认邮件命中 welcome_confirm", mr.classify(welcome) == "welcome_confirm",
          mr.classify(welcome))
    check("[负对照] 确认邮件不被判成申请确认",
          mr.classify(welcome) != "waitlist_confirm")

    # ④ 验证码两种文案都要认（并集）
    c1 = M(f"bounces+<acct>-22e6-a@em5082.typesafe.ai", "Your TypeSafe sign-in code")
    c2 = M("pm_bounces@pm-bounces.typesafe.ai", "Your TypeSafe verification code")
    check("sign-in code 命中", mr.any_of("signin_code", "verify_code")(c1))
    check("verification code 也命中",
          mr.any_of("signin_code", "verify_code")(c2))
    check("[负对照] 验证码规则不吃获批邮件",
          not mr.any_of("signin_code", "verify_code")(ready))

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


def main() -> int:
    test_pow()
    test_compact_ref()
    test_js_object()
    test_ledger_union()
    test_mailrules()
    print(f"\n{'=' * 50}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 50}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
