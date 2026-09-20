#!/usr/bin/env python
"""自测：把几个"静默失败"的点钉住。

每个检查项都配**负对照** —— 只验"该过的过了"是不够的，
还要证明检查器本身有能力判负。

⚠️ 写负对照时先问一句：**"这条断言的输入词汇，和线上真的会出现的词汇，是同一套吗？"**
2026-09-20 审计发现过一个反面教材：`test_ledger_union` 用 `success`/`failed` 断言
台账等级语义，而生产实际写的是 `keyed`/`failed` —— 负对照全过、真实语义全废。
`test_status_vocabulary` 就是为堵这个洞加的。

覆盖：
  - Framer PoW
  - `$ACTION:0` 紧凑 JSON
  - Stytch JS 字面量解析
  - 台账并集合并 / 等级语义（**真实词汇**）
  - 状态词汇覆盖（AST 扫源码）
  - 收件规则
  - OTP 抽取（锚定 / 降级）
  - 编排层：错误码分流、申请段、claim、并发不串号

**全程离线**，不碰网络。
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import itertools
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import framer_waitlist as fw  # noqa: E402
from src import typesafe as ts  # noqa: E402
from src.ledger import RANK, Ledger  # noqa: E402
from src.tempemail import Mail  # noqa: E402
from src.typesafe import Result, TypeSafeError  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ══════════════════════════════════════════════════════════════════════
# 离线替身：只实现 pipeline 真正用到的方法，不碰网络
# ══════════════════════════════════════════════════════════════════════

class _FakeTempMailClient:
    """替换 `src.pipeline.TempMailClient`（含 `_clone()` 里新建的那些）。

    `pending` 是**类属性**：所有实例共享同一批待收邮件，模拟"同一批邮箱窗口"。
    收件人匹配 `email` 或通配 `"*"`（申请段建邮箱时地址是运行时生成的）。
    """

    pending: list = []
    _seq = itertools.count(1)

    def __init__(self, *a, **kw):
        self.stats = SimpleNamespace(polls=0, http_5xx=0, http_4xx=0, created=0)
        self._n = next(_FakeTempMailClient._seq)

    def create_mailbox(self, domain: str | None = None) -> str:
        return f"fake-{self._n}@example-mail.test"

    def wait_for_mail(self, email, match, *, timeout=0.0, interval=0.0, since_ms=None):
        for m in type(self).pending:
            if m.recipient in (email, "*") and match(m):
                return m
        return None

    def first_mail_matching(self, email, match):
        for m in type(self).pending:
            if m.recipient in (email, "*") and match(m):
                return m
        return None

    def list_mails(self, email=None, limit=None):
        return list(type(self).pending)

    def scan_all(self, limit: int = 100):
        return list(type(self).pending)


class _FakeTypeSafe:
    """替换 `src.pipeline.TypeSafeClient`。行为由类属性驱动，测试逐条设置。

    ★ `create_api_key` 把**本会话的邮箱**编进 key 里 —— 这是"串号"检测器：
    如果两个并发 worker 共用了会话对象，返回的 key 里就会是**别人的**邮箱。
    """

    callback_status = 200
    callback_body: dict = {}
    fail_key = False

    def __init__(self, *a, **kw):
        self.email = ""

    def send_login_email(self, email, mode="code"):
        self.email = email
        return Result(ok=True, status=200, stage="send_login_email",
                      data={"page_text": "Check your email"})

    def auth_callback(self, token, token_type, email):
        # 先睡再写 email：放大竞态窗口，让"共享会话"的 bug 有机会暴露
        time.sleep(0.005)
        self.email = email
        ok = type(self).callback_status == 200
        return Result(ok=ok, status=type(self).callback_status,
                      stage="auth_callback", data=dict(type(self).callback_body))

    def me(self):
        return {"human_name": "tester",
                "latest_tos_acceptance": {"tos_version": 1},
                "console_survey_completed_at": 1,
                "org_memberships": []}

    def complete_onboarding(self, display_name="x"):
        return Result(ok=True, stage="onboarding", data={"completed": []})

    def create_api_key(self, name="1"):
        if type(self).fail_key:
            raise TypeSafeError("建 key 失败 HTTP 500")
        return {"api_key": f"apikey_FAKE_{self.email}", "id": f"kid_{self.email}"}


@contextlib.contextmanager
def offline(*, ts_status: int = 200, ts_body: dict | None = None,
            fail_key: bool = False, mails: list | None = None,
            framer_ok: bool = True):
    """把 pipeline 的三个出网点换成替身。退出时**必定**还原。"""
    from src import pipeline as P

    saved = (P.TypeSafeClient, P.TempMailClient, P.framer_submit)
    _FakeTempMailClient.pending = list(mails or [])
    _FakeTypeSafe.callback_status = ts_status
    _FakeTypeSafe.callback_body = dict(ts_body or {})
    _FakeTypeSafe.fail_key = fail_key
    P.TypeSafeClient = _FakeTypeSafe
    P.TempMailClient = _FakeTempMailClient
    P.framer_submit = lambda email, **kw: (
        {"status": 201, "ok": True, "body": ""} if framer_ok
        else {"status": 422, "ok": False, "body": "form error"})
    try:
        yield P
    finally:
        P.TypeSafeClient, P.TempMailClient, P.framer_submit = saved
        _FakeTempMailClient.pending = []


def _make_pipe(P, ledger: Ledger):
    return P.Pipeline(mail=P.TempMailClient(), ledger=ledger, verbose=False)


def _otp_mail(to: str, code: str = "123456") -> Mail:
    return Mail(id="m-otp", to=to, sender="bounces+x-a@em5082.typesafe.ai",
                subject="Your TypeSafe sign-in code",
                body=f"{code} is your one-time code", received_at=1)


def _waitlist_mail(to: str) -> Mail:
    return Mail(id="m-wait", to=to, sender="010001a0bb95b4b5-722f1aae@envelope.updates.typesafe.ai",
                subject="TypeSafe AI: You\u2019re on the waitlist for Jev!",
                body="We'll be in touch when it's your turn.", received_at=1)


def _tmp_ledger() -> Ledger:
    return Ledger(Path(tempfile.mkdtemp()) / "ledger.jsonl")


# ══════════════════════════════════════════════════════════════════════

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
    """台账并集合并 —— 🔴 **用生产真实词汇（keyed/failed），不用 success/failed**。

    用 `success` 是 2026-09-20 审计发现的那个洞：那套词汇是兄弟项目的口径，
    正好是 `RANK` 认的，所以断言必过，而真实语义（keyed vs failed）全废。
    """
    print("\n[台账并集合并（真实词汇）]")
    led = _tmp_ledger()
    led.append({"key": "a@x.com", "status": "keyed", "email": "a@x.com",
                "api_key": "apikey_K1"})
    check("基线 1 条", len(led.load()) == 1)

    # 增量写回：**只带增量字段**（不交超集），必须仍然真的写进去
    led.upsert_many([{"key": "a@x.com", "status": "keyed", "token": "T1"}])
    rec = {r["key"]: r for r in led.load()}["a@x.com"]
    check("旧字段没丢（api_key 还在）", rec.get("api_key") == "apikey_K1", str(rec))
    check("新字段真的写进去了（token）", rec.get("token") == "T1", str(rec))

    # 同级、字段数相等 —— 启发式算法会在这里失手，并集不会
    led.upsert_many([{"key": "a@x.com", "status": "keyed", "extra": "E"}])
    rec = {r["key"]: r for r in led.load()}["a@x.com"]
    check("字段数相等时新字段仍写入", rec.get("extra") == "E", str(rec))
    check("此前所有字段仍在",
          all(k in rec for k in ("api_key", "token", "extra")), str(rec))

    # ★ 核心回归：keyed 之后来一条 failed，**不能把成功记录打回原形**
    led.upsert_many([{"key": "a@x.com", "status": "failed", "api_key": "",
                      "api_key_id": "", "error": "认证回调 HTTP 401: Code expired"}])
    rec = {r["key"]: r for r in led.load()}["a@x.com"]
    check("★ 重跑失败后 status 未被降级", rec.get("status") == "keyed", str(rec.get("status")))
    check("★ 重跑失败后 api_key 仍在", rec.get("api_key") == "apikey_K1",
          repr(rec.get("api_key")))
    check("失败原因记进了 last_error（信息没丢）",
          "Code expired" in (rec.get("last_error") or ""), repr(rec.get("last_error")))

    # 升级方向仍要生效
    led.upsert_many([{"key": "c@x.com", "status": "failed", "error": "x"}])
    led.upsert_many([{"key": "c@x.com", "status": "keyed", "api_key": "apikey_K3"}])
    rec = {r["key"]: r for r in led.load()}["c@x.com"]
    check("failed -> keyed 升级仍生效",
          rec.get("status") == "keyed" and rec.get("api_key") == "apikey_K3", str(rec))

    # EARNED_FIELDS：同级空壳不许清掉已赚到的凭据
    led.upsert_many([{"key": "d@x.com", "status": "keyed", "api_key": "apikey_K4"}])
    led.upsert_many([{"key": "d@x.com", "status": "keyed", "api_key": "",
                      "api_key_id": ""}])
    rec = {r["key"]: r for r in led.load()}["d@x.com"]
    check("同级空壳不清掉 api_key（EARNED_FIELDS 保护）",
          rec.get("api_key") == "apikey_K4", repr(rec.get("api_key")))

    # 不缩水 / 幂等
    led.upsert_many([{"key": "b@x.com", "status": "keyed"}])
    check("新增不丢旧数据", len(led.load()) == 4, str(len(led.load())))
    led.upsert_many([{"key": "b@x.com", "status": "keyed"}])
    check("重复写入幂等", len(led.load()) == 4)


def _status_literals(source: str) -> set[str]:
    """从源码里抽出"状态字面量"：

    ① 赋值给 `X.status` / `status` 的字符串（含 dataclass 默认值）
    ② 与 `X.status` 比较的字符串

    抽得出来才谈得上"覆盖检查" —— 所以这个函数自己也要有负对照。
    """
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            hit = any(
                (isinstance(t, ast.Attribute) and t.attr == "status")
                or (isinstance(t, ast.Name) and t.id == "status")
                for t in targets
            )
            if hit and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out.add(node.value.value)
        elif isinstance(node, ast.Compare):
            left = node.left
            if isinstance(left, ast.Attribute) and left.attr == "status":
                for c in node.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        out.add(c.value)
    return out


def test_status_vocabulary() -> None:
    """🔴 台账等级表必须覆盖管线实际写的**每一个** status。

    这是 2026-09-20 那个 P0 的结构性修复：以前靠"人记得同步改两个文件"，
    现在靠 AST 扫源码。新增状态而不登记进 `ledger.RANK` ⇒ 本测试立刻失败。
    """
    print("\n[状态词汇覆盖（AST）]")
    found: set[str] = set()
    for p in sorted(list((ROOT / "src").glob("*.py")) + list((ROOT / "tools").glob("*.py"))):
        found |= _status_literals(p.read_text(encoding="utf-8"))

    missing = sorted(found - set(RANK))
    check(f"源码里出现的 status 字面量 {sorted(found)}", bool(found))
    check("★ 全部已在 ledger.RANK 里登记（否则等级判断静默失效）",
          not missing, f"未登记：{missing} —— 请加到 src/ledger.py 的 RANK")

    # 负对照：证明提取器有能力发现"未登记的新状态"
    fake = 'rec.status = "brand_new_state"\nif rec.status == "another_new": pass\n'
    got = _status_literals(fake)
    check("[负对照] 提取器能发现未登记的新状态",
          got == {"brand_new_state", "another_new"}, str(got))
    check("[负对照] 这些新状态确实不在 RANK 里（所以真加了会失败）",
          not (got & set(RANK)), str(got & set(RANK)))


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


def test_auth_error_triage() -> None:
    """🔴 三个错误码的分流 —— 业务核心。混掉的代价是把排查引向相反方向。"""
    print("\n[编排：认证错误码分流]")

    # 成功路径
    with offline(mails=[_otp_mail("ok@example-mail.test")]) as P:
        led = _tmp_ledger()
        rec = _make_pipe(P, led).resume(["ok@example-mail.test"])[0]
    check("200 → status=keyed", rec.status == "keyed", rec.status)
    check("200 → 拿到 api_key", bool(rec.api_key), repr(rec.api_key))
    check("200 → 三个阶段都记 ok",
          all(rec.stages.get(k) == "ok" for k in ("login", "onboarding", "api_key")),
          str(rec.stages))

    # 401 Code expired
    with offline(ts_status=401, ts_body={"error": "Code expired"},
                 mails=[_otp_mail("e1@example-mail.test")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).resume(["e1@example-mail.test"])[0]
    check("401 Code expired → failed", rec.status == "failed", rec.status)
    check("401 Code expired → 错误里点名'重新发码'（区别于邀请制）",
          "Code expired" in rec.error and "重新发码" in rec.error, rec.error)
    check("[负对照] 401 不该被误判成 invite_only",
          "invite_only" not in rec.error, rec.error)
    check("[负对照] 401 不该把 approved 标成 pending",
          rec.stages.get("approved") != "pending", str(rec.stages))

    # 401 Authentication failed（token 已用过）
    with offline(ts_status=401, ts_body={"error": "Authentication failed"},
                 mails=[_otp_mail("e2@example-mail.test")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).resume(["e2@example-mail.test"])[0]
    check("401 Authentication failed → failed", rec.status == "failed", rec.status)
    check("401 Authentication failed → 错误里点名'token 已被使用'（与过期区分开）",
          "已被使用" in rec.error, rec.error)

    # 403 Access restricted
    with offline(ts_status=403, ts_body={"error": "Access restricted"},
                 mails=[_otp_mail("e3@example-mail.test")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).resume(["e3@example-mail.test"])[0]
    check("403 → failed", rec.status == "failed", rec.status)
    check("403 → 标为 invite_only（run_e2e 的阻断统计靠这个串）",
          "invite_only" in rec.error, rec.error)
    check("403 → stages.approved=pending（如实反映'没获批'）",
          rec.stages.get("approved") == "pending", str(rec.stages))


def test_apply_and_approval() -> None:
    """申请段 + 邀请制门槛：没获批**不等于**这次执行失败。"""
    print("\n[编排：申请段 / 邀请制门槛]")

    with offline(mails=[_waitlist_mail("*")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).run_batch(count=1, mode="apply")[0]
    check("mode=apply 的终点是 confirmed", rec.status == "confirmed", rec.status)
    check("apply / confirm 两阶段都记 ok",
          rec.stages.get("apply") == "ok" and rec.stages.get("confirm") == "ok",
          str(rec.stages))

    # 负对照：framer 提交失败必须落到 apply 阶段
    with offline(framer_ok=False) as P:
        rec = _make_pipe(P, _tmp_ledger()).run_batch(count=1, mode="apply")[0]
    check("[负对照] framer 提交失败 → status=failed", rec.status == "failed", rec.status)
    check("[负对照] 失败记在 apply 阶段（不是 confirm）",
          rec.stages.get("apply") == "failed", str(rec.stages))

    # 全链路：只有确认邮件、没有获批邮件 ⇒ 停在邀请制门槛
    with offline(mails=[_waitlist_mail("*")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).run_batch(count=1, mode="full")[0]
    check("未获批 → 如实报 invite_only", "invite_only" in rec.error, rec.error)
    check("★ 未获批**不把 status 打成 failed**（它不算执行失败）",
          rec.status != "failed", rec.status)
    check("未获批 → stages.approved=pending", rec.stages.get("approved") == "pending",
          str(rec.stages))


def test_claim() -> None:
    """claim 的两段式语义：发码不提交 ⇒ code_sent。"""
    print("\n[编排：claim 两段式]")

    with offline() as P:
        rec = _make_pipe(P, _tmp_ledger()).claim("m@real.com", "", send_first=True)
    check("send_first 且无 token → code_sent", rec.status == "code_sent", rec.status)
    check("[负对照] code_sent 不是 failed（不该进失败统计）",
          rec.status != "failed", rec.status)
    check("发码动作记进 stages", rec.stages.get("send_code") == "ok", str(rec.stages))

    with offline() as P:
        rec = _make_pipe(P, _tmp_ledger()).claim("m@real.com", "")
    check("无 send_first 也无 token → failed", rec.status == "failed", rec.status)
    check("错误说明了缺什么", "缺少 token" in rec.error, rec.error)

    with offline() as P:
        led = _tmp_ledger()
        rec = _make_pipe(P, led).claim("m@real.com", "123456")
    check("带 token → 一次跑完 4→7，status=keyed", rec.status == "keyed", rec.status)
    check("claim 的结果也落进台账", len(led.load()) == 1, str(len(led.load())))


def test_key_survives_rerun_failure() -> None:
    """🔴 P0 回归：**同一账号"成功之后再重跑一次失败"，凭据不能丢。**

    这是 2026-09-20 审计发现的那个会静默丢数据的洞的端到端复现。
    台账层已有 `test_ledger_union` 的单测，这里再走一遍真实编排路径。
    """
    print("\n[编排：重跑失败不丢凭据（P0 回归）]")
    led = _tmp_ledger()

    with offline(mails=[_otp_mail("keep@example-mail.test")]) as P:
        rec1 = _make_pipe(P, led).resume(["keep@example-mail.test"])[0]
    check("第一次：拿到 key", rec1.status == "keyed" and bool(rec1.api_key), rec1.status)
    key1 = rec1.api_key

    with offline(ts_status=401, ts_body={"error": "Code expired"},
                 mails=[_otp_mail("keep@example-mail.test")]) as P:
        rec2 = _make_pipe(P, led).resume(["keep@example-mail.test"])[0]
    check("第二次：这次失败了", rec2.status == "failed", rec2.status)

    merged = {r["key"]: r for r in led.load()}["keep@example-mail.test"]
    check("★ 台账里 api_key 仍在（这是审计发现的 P0）",
          merged.get("api_key") == key1, repr(merged.get("api_key")))
    check("★ 台账里 status 未被降级为 failed",
          merged.get("status") == "keyed", str(merged.get("status")))
    check("失败原因留在 last_error，信息没丢",
          "Code expired" in (merged.get("last_error") or ""),
          repr(merged.get("last_error")))
    # 负对照：验收脚本是按 api_key 有没有值来筛的，所以这条直接决定验收清单少不少行
    withkey = [r for r in led.load() if r.get("api_key")]
    check("[负对照] 按 api_key 筛（verify_keys 的口径）仍能筛到它",
          len(withkey) == 1, str(len(withkey)))


def test_success_ledger() -> None:
    """成功数据单独落 `result/`（交付物），且**只**在真的拿到 key 时写。

    这条守的是"交付物目录"的语义：`exports/ledger.jsonl` 留全部历史（含失败），
    `result/success.jsonl` 只该有成功的。判据不是"文件存在"，是
    **失败那次一条都不许写进去** —— 否则交付物又变成需要自己筛的东西了。
    """
    print("\n[编排：成功台账只收成功]")
    led = _tmp_ledger()
    succ = _tmp_ledger()

    with offline(mails=[_otp_mail("ok@example-mail.test")]) as P:
        pipe = P.Pipeline(mail=P.TempMailClient(), ledger=led,
                          success_ledger=succ, verbose=False)
        rec = pipe.resume(["ok@example-mail.test"])[0]

    check("拿到 key", rec.status == "keyed" and bool(rec.api_key), rec.status)
    rows = succ.load()
    check("★ 成功记录写进了成功台账", len(rows) == 1, str(len(rows)))
    check("写进去的是完整记录（含 api_key / email）",
          rows and rows[0].get("api_key") == rec.api_key
          and rows[0].get("email") == "ok@example-mail.test",
          repr(rows[:1]))
    check("_clone() 共享同一个成功台账",
          pipe._clone().success_ledger is pipe.success_ledger)

    # 失败路径：建 key 抛错 ⇒ 成功台账**一条都不许增加**
    with offline(mails=[_otp_mail("bad@example-mail.test")], fail_key=True) as P:
        bad = P.Pipeline(mail=P.TempMailClient(), ledger=_tmp_ledger(),
                         success_ledger=succ, verbose=False).resume(["bad@example-mail.test"])[0]
    check("第二次（建 key 失败）确实失败了", bad.status == "partial", bad.status)
    check("★ [负对照] 失败那次没有写进成功台账",
          len(succ.load()) == 1, str(len(succ.load())))


def test_concurrency_no_crosstalk() -> None:
    """并发：不串号、不丢数、顺序稳定。"""
    print("\n[编排：并发不串号]")
    emails = [f"c{i}@example-mail.test" for i in range(6)]

    with offline(mails=[_otp_mail(e) for e in emails]) as P:
        led = _tmp_ledger()
        pipe = _make_pipe(P, led)
        check("_clone() 返回**独立**实例（并发不共享会话）", pipe._clone() is not pipe)
        check("_clone() 共享同一个台账", pipe._clone().ledger is pipe.ledger)
        check("Pipeline 不再有实例级 client 字段（串号的根源）",
              not hasattr(P.Pipeline, "client"))
        recs = pipe.resume(emails, concurrency=4)

    check("并发返回条数正确", len(recs) == 6, str(len(recs)))
    check("返回顺序与传入一致", [r.email for r in recs] == emails, str([r.email for r in recs]))
    bad = [(r.email, r.api_key) for r in recs
           if r.api_key != f"apikey_FAKE_{r.email}"]
    check("★ 无串号：每个 key 都建在**自己**的会话上", not bad, str(bad))
    check("全部 status=keyed", all(r.status == "keyed" for r in recs),
          str([r.status for r in recs]))
    check("台账 6 条，无丢行", len(led.load()) == 6, str(len(led.load())))

    # 负对照：串行与并发结果必须一致（并发不改变业务结果）
    with offline(mails=[_otp_mail(e) for e in emails]) as P:
        serial = _make_pipe(P, _tmp_ledger()).resume(emails, concurrency=1)
    check("[负对照] 串行与并发的 key 集合完全一致",
          {r.api_key for r in serial} == {r.api_key for r in recs},
          f"serial={sorted(r.api_key for r in serial)}")


def main() -> int:
    test_pow()
    test_compact_ref()
    test_js_object()
    test_ledger_union()
    test_status_vocabulary()
    test_mailrules()
    test_otp_extraction()
    test_auth_error_triage()
    test_apply_and_approval()
    test_claim()
    test_key_survives_rerun_failure()
    test_success_ledger()
    test_concurrency_no_crosstalk()
    print(f"\n{'=' * 50}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 50}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
