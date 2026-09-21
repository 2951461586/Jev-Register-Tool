"""编排层自测：错误码分流 / 申请段 / 登录（含编号漂移与链接回捞）/
监听去重 / claim / 重跑不丢 key / 并发不串号 / worker 崩溃不静默丢弃。

全部**离线**：出网点由 `support.offline()` 换成替身。
"""

from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path

from .support import (RANK, Mail, _FakeTypeSafe, _link_mail, _make_pipe,
                      _otp_mail, _tmp_ledger, _waitlist_mail, check, offline,
                      pl, st, ts)

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
    # 计时器必须**不重叠**：`apply` 只算提交表单，等邮件单独记 `confirm_wait`。
    # 以前两者合一，报告里会印出 "阶段分解: apply 181.13s"，读起来像表单提交卡了 3 分钟。
    check("★ timings 里 apply 与 confirm_wait 分开记（不重叠，可求和）",
          "apply" in rec.timings and "confirm_wait" in rec.timings,
          str(sorted(rec.timings)))
    check("★ apply 计时只覆盖提交表单（远小于等邮件的耗时）",
          rec.timings.get("apply", 9e9) < 5.0,
          f"apply={rec.timings.get('apply')}")

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


class _FakeResp:
    def __init__(self, text: str, status: int = 200, headers: dict | None = None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}


class _FakeSession:
    """按序吐出预置页面，并记录 GET 次数（用来断言重试次数）。

    `post_resp` 用于测 `post_setup()` 的提交结果 —— 默认 404，即降级通路的
    典型响应（该通路的 action id 已作废）。`posts` 记录每次 POST 的入参，
    用来断言"走的是哪条通路"。
    """

    def __init__(self, pages: list[str], post_resp: _FakeResp | None = None):
        self.pages = list(pages)
        self.headers: dict = {}
        self.calls = 0
        self.posts: list[dict] = []
        self.post_resp = post_resp if post_resp is not None else _FakeResp("", 200)

    def get(self, url, params=None, timeout=None):
        i = min(self.calls, len(self.pages) - 1)
        self.calls += 1
        return _FakeResp(self.pages[i])

    def post(self, url, files=None, headers=None, timeout=None):
        self.posts.append({"url": url, "files": files or {}, "headers": headers or {}})
        return self.post_resp


def _login_html(nums) -> str:
    """造一个带 `$ACTION_<n>` 隐藏域的 /login 页。

    值里的引号必须写成 `&quot;`：解析正则是 `value="([^"]*)"`，
    直接写 `"` 会截断匹配（页面里本来也就是转义过的）。
    """
    parts = []
    for n in nums:
        ref = json.dumps({"id": f"act{n}", "bound": "$@1"}).replace('"', "&quot;")
        parts.append(f'<input type="hidden" name="$ACTION_{n}:0" value="{ref}">')
        parts.append(f'<input type="hidden" name="$ACTION_{n}:1" value="v{n}">')
    return "<form>" + "".join(parts) + "</form>"


def test_login_action_index_drift_retries() -> None:
    """🔴 `/login` 的 Server Action **编号会漂移**，缺索引时必须先重试再判死。

    2026-09-20 实测：正常渲染 `('2','3','4')`，某次拿到 `('2','4','5')`
    —— 中间被插入了一个新表单，`ACTION_CODE='3'` 直接消失。
    15 分钟后复测 15/15 又全回到 `('2','3','4')`。
    ⇒ 写死索引的**前提**（编号稳定）会被部署/边缘缓存短暂破坏，
    判死之前必须原样重试一次；不做语义识别是因为 LINK/CODE 只能靠编号区分。
    """
    print("\n[登录：Server Action 编号漂移 → 重试]")

    ok = ts.TypeSafeClient(session=_FakeSession([_login_html("234")]))
    acts = ok.fetch_actions("a@x.com")
    check("正常页一次就拿到 action", sorted(acts) == ["2", "3", "4"], str(sorted(acts)))
    check("正常页不浪费重试", ok.s.calls == 1, f"GET 次数={ok.s.calls}")

    flaky = ts.TypeSafeClient(session=_FakeSession([_login_html("245"),
                                                    _login_html("234")]))
    acts = flaky.fetch_actions("a@x.com")
    check("★ 编号漂移时重试后能拿到（不再直接判死）",
          sorted(acts) == ["2", "3", "4"], str(sorted(acts)))
    check("★ 确实只重试了一次（GET 2 次）", flaky.s.calls == 2, f"GET 次数={flaky.s.calls}")

    dead = ts.TypeSafeClient(session=_FakeSession([_login_html("245")] * 5))
    try:
        dead.fetch_actions("a@x.com")
        raised = ""
    except ts.TypeSafeError as exc:
        raised = str(exc)
    check("[负对照] 一直漂移 → 最终仍判死", "未渲染出预期的 Server Action" in raised, raised)
    check("[负对照] 判死前已经重试过（GET 2 次，不是 1 次就放弃）",
          dead.s.calls == 2, f"GET 次数={dead.s.calls}")

    check("★ 重试次数 >= 2（至少给漂移一次机会）",
          ts.TypeSafeClient.ACTION_FETCH_ATTEMPTS >= 2,
          str(ts.TypeSafeClient.ACTION_FETCH_ATTEMPTS))


def test_code_mode_falls_back_to_magic_link() -> None:
    """🔴 码模式等不到 6 位码时，站点可能回的是**魔法链接**，必须回捞。

    反直觉点：`未收到验证码邮件` **不等于** 邮箱坏了 / D1 窗口被挤爆。
    2026-09-20 实测：同一账号 4 次发码里 1 次回的是 "Sign in to TypeSafe"
    （魔法链接），4 次是 "Your TypeSafe sign-in code"。等码的那次必然超时，
    于是该账号被重试 10 次、每次都记 `未收到验证码邮件`，排查被一路引向
    "共享 D1 窗口被刷爆"，而真相只是**凭据形态换了**。
    改用 link 模式一跑就进去了（随后 403 —— 那才是它真正的状态）。
    """
    print("\n[登录：码模式 → 魔法链接回捞]")

    # 只有链接邮件、没有码邮件 ⇒ 旧实现会在这里判死
    with offline(mails=[_link_mail("a@x.com")]) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = pl.AccountRecord(key="a@x.com", email="a@x.com")
        cl = pipe.stage_login(rec, mail_timeout=1.0)
    check("★ 只有魔法链接时也能建立会话（不再报『未收到验证码邮件』）",
          cl is not None, f"status={rec.status} err={rec.error}")
    check("★ 走的是 magic_links 分支（不是 otp）",
          _FakeTypeSafe.last_token_type == "magic_links",
          _FakeTypeSafe.last_token_type or "(未调用 auth_callback)")

    # 负对照：有码邮件时必须照旧走 otp，不能被回捞逻辑抢走
    with offline(mails=[_otp_mail("b@x.com", "123456")]) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = pl.AccountRecord(key="b@x.com", email="b@x.com")
        cl = pipe.stage_login(rec, mail_timeout=1.0)
    check("[负对照] 有 6 位码时仍走 otp 分支", cl is not None
          and _FakeTypeSafe.last_token_type == "otp",
          _FakeTypeSafe.last_token_type or "(未调用 auth_callback)")

    # 负对照：两种邮件都没有 ⇒ 才允许判"未收到验证码邮件"
    with offline(mails=[]) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = pl.AccountRecord(key="c@x.com", email="c@x.com")
        cl = pipe.stage_login(rec, mail_timeout=0.1)
    check("[负对照] 两种凭据都没有 → 才判『未收到验证码邮件』",
          cl is None and "未收到验证码邮件" in (rec.error or ""),
          f"cl={cl} err={rec.error}")

    # 护栏：回捞窗口不能设太大，否则未获批账号每次重跑都要多白等
    check("★ 回捞窗口 <= 60s（不给未获批账号拖长重跑）",
          0 < st.LINK_FALLBACK_TIMEOUT <= 60.0, str(st.LINK_FALLBACK_TIMEOUT))


def _setup_page(*, with_action: bool) -> str:
    """造一个 `/setup/*` 页面。

    新形态（2026-09-20 起）是索引 **1**、**只有 `:0` `:1`**、另加 `$ACTION_KEY`
    —— 旧代码枚举 `("2","3","4")` 且要求 `:2` 同时存在，在新形态上一条都抓不到。
    这里刻意用新形态，测的就是"抓不到时怎么办"。
    """
    if not with_action:
        return "<html><body>onboarding already done</body></html>"
    ref = json.dumps({"id": "60ec39e20e3115ab", "bound": "$@1"}).replace('"', "&quot;")
    return (f'<form><input type="hidden" name="$ACTION_1:0" value="{ref}">'
            f'<input type="hidden" name="$ACTION_1:1" value="v1">'
            f'<input type="hidden" name="$ACTION_KEY" value="KEY1"></form>')


def test_post_setup_degrade_is_observable() -> None:
    """🔴 降级通路必须**可观测**，失败时 error 要指向真因 —— 不许静默退化。

    2026-09-21 第三轮扫描发现的洞：`post_setup()` 里
    `except TypeSafeError: acts = {}` 是**静默**的，然后退化到
    `FALLBACK_SETUP_ACTIONS`；而那张表的 id 三处文档都写明**已全部作废**
    （POST 回 `404 Server action not found.`）。于是"站点改版"最终只表现为
    `onboarding 失败: HTTP 404` —— 与真因毫无字面关联，这正是 runbook §4.6
    那段"为什么难定位"复盘的结构性成因。

    对照：`stages.stage_login` 的 OTP 降级（`how != "anchored"`）是**打告警**的。
    同一模式两处处置必须一致 —— 这条测试把它钉住。
    """
    print("\n[编排：setup 降级通路可观测]")

    # A. 抓不到隐藏域 ⇒ 走降级通路：必须有告警 + error 指向真因 + 给下一步
    sess = _FakeSession([_setup_page(with_action=False)],
                        post_resp=_FakeResp("Server action not found.", 404))
    cl = ts.TypeSafeClient(session=sess)
    r = cl.post_setup("/setup/tos?returnTo=%2Fhook", {"legalAcknowledged": "true"})

    check("[负对照] 降级通路确实失败了（该表 id 已作废）", not r.ok, str(r.ok))
    check("★ 确实走了降级通路（via 标出 next-action）",
          str(r.data.get("via", "")).startswith("next-action"), str(r.data.get("via")))
    check("★ 降级**不是静默**的：self.log 里有可辨识告警",
          any("未抓到" in line for line in cl.log), str(cl.log))
    check("★ error 点出真因（未抓到隐藏域），而不只是 HTTP 404",
          "未抓到" in r.error and "降级" in r.error, r.error)
    check("★ error 给了下一步动作（跑探针看页面实际形态）",
          "probe_onboarding" in r.error, r.error)
    check("★ fetch 的失败原因也带进了 data（可程序化取用）",
          "未渲染出 Server Action" in str(r.data.get("fetch_error", "")),
          str(r.data.get("fetch_error"))[:80])

    # B. 正对照：页面渲染了隐藏域 ⇒ 走 nojs 通路，且**不**打降级告警
    sess2 = _FakeSession([_setup_page(with_action=True)], post_resp=_FakeResp("", 200))
    cl2 = ts.TypeSafeClient(session=sess2)
    r2 = cl2.post_setup("/setup/tos?returnTo=%2Fhook", {"legalAcknowledged": "true"})
    check("[正对照] 抓到隐藏域 → 走 nojs 通路且成功",
          r2.ok and str(r2.data.get("via", "")).startswith("nojs"), str(r2.data.get("via")))
    check("[正对照] 正常路径**不**打降级告警（告警是有选择性的）",
          not any("未抓到" in line for line in cl2.log), str(cl2.log))
    check("[正对照] 提交用 $ACTION_* 隐藏域，不带 next-action 头",
          "next-action" not in sess2.posts[0]["headers"],
          str(sorted(sess2.posts[0]["headers"])))
    check("[正对照] 提交确实回填了 $ACTION_KEY（新形态必需）",
          "$ACTION_KEY" in sess2.posts[0]["files"],
          str(sorted(sess2.posts[0]["files"])))


def test_confirm_timeout_headroom() -> None:
    """🔴 回归护栏：等确认邮件的阈值不能退回 180s。

    2026-09-20 连跑 10 批次的实测延迟（秒）：
        41.7 / 52.9 / 52.8 / 43.9 / 68.6 / 86.7 / 93.6 / 192 / 198 / ∞
    后两条在阈值 180s 处被判 `failed`，但收件箱复核显示它们分别**在超时后
    12s / 18s 就落了库** —— 是假阴性。台账里多两条"失败"，运营者会据此重投申请。

    阈值必须留出实测最大延迟的余量；这条断言把"不许调回 180s"钉在测试里。
    """
    print("\n[编排：确认邮件等待阈值]")

    d = inspect.signature(pl.Pipeline.stage_apply).parameters["confirm_timeout"].default
    check("★ 默认阈值 ≥ 240s（实测最大延迟 198s，180s 会误判成失败）",
          isinstance(d, (int, float)) and d >= 240, f"default={d}")
    check("★ 默认阈值取自常量 CONFIRM_TIMEOUT（单一真源，别写字面量）",
          d == pl.CONFIRM_TIMEOUT, f"{d} vs {pl.CONFIRM_TIMEOUT}")

    # 超时文案要能提示"迟到 vs 未发"这个歧义 —— 否则运维只会看到一句"没收到"，
    # 而 runbook 对它的处置（重投申请）在假阴性场景下是错的。
    # 注意：这里必须显式传一个极小的阈值，否则会真等满默认的 300s。
    with offline(mails=[]) as P:
        rec = _make_pipe(P, _tmp_ledger()).run_batch(
            count=1, mode="apply", confirm_timeout=0.2)[0]
    check("★ 回执超时 → status=applied（**不是 failed**）且记在 confirm 阶段",
          rec.status == "applied" and rec.stages.get("confirm") == "failed",
          f"{rec.status} {rec.stages}")
    check("  [负对照] 回执超时**不许**被记成 failed"
          "（记成 failed 会让账号从待复查清单里消失）",
          rec.status != "failed", rec.status)
    check("★ 超时文案点明'申请已注册 / 勿重投'（假阴性靠 watch + 回查排除）",
          "回执未到" in rec.error and "误判" in rec.error and "回查" in rec.error,
          rec.error)
    # `applied` 必须在 RANK 里，且**低于** confirmed —— 否则要么静默落 0 分，
    # 要么反过来把 confirmed 覆盖掉。
    check("★ applied 在 RANK 中且低于 confirmed（保证后续能被升级）",
          0 < RANK.get("applied", -1) < RANK["confirmed"],
          f"applied={RANK.get('applied')} confirmed={RANK['confirmed']}")


def _ready_mail(to: str) -> Mail:
    return Mail(id="m-ready", to=to,
                sender="010001a0bb95b4b5-722f1aae@envelope.updates.typesafe.ai",
                subject="TypeSafe AI: Your account is ready",
                body="Your account is ready.", received_at=2)


def test_watch_skips_keyed() -> None:
    """🔴 `watch` 读的是**全表窗口**，旧批次的获批邮件会在窗口里停留很久。

    没有跳过逻辑时，同一账号会被再跑一次 4→7 ⇒ **造出第二把 key**。
    两把在服务端都有效，但 `Ledger.load()` 按邮箱去重、**末行胜出** ⇒
    交付物里少一把。这与 P0 / 阈值误判同一类：**不报错，只是行数不对**。
    """
    print("\n[监听：不重复建 key]")
    dup, fresh = "dup@example-mail.test", "fresh@example-mail.test"
    led = _tmp_ledger()
    led.append({"email": dup, "key": dup, "status": "keyed",
                "api_key": "apikey_ORIGINAL", "api_key_id": "kid_orig"})

    with offline(mails=[_ready_mail(dup), _ready_mail(fresh), _otp_mail(fresh)]) as P:
        recs = _make_pipe(P, led).watch(timeout=0.5, interval=0.05)

    got = {r.email for r in recs}
    check("★ 已有 api_key 的地址被跳过（不重复建 key）", dup not in got, str(got))
    check("  [正对照] 没有 key 的地址照常被处理（证明跳过是**有选择性**的）",
          fresh in got, str(got))
    after = {r["email"]: r for r in led.load()}[dup]
    check("★ 原 key 未被覆盖（交付物不会少一把）",
          after.get("api_key") == "apikey_ORIGINAL", str(after.get("api_key")))


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

    # 🔴 护栏：自测**绝不能**写到交付物 `result/success.jsonl`。
    # 这条是被真事逼出来的 —— 实测自测往交付物里灌了 9 条 `apikey_FAKE_*`，
    # 而 verify_keys 读的是主台账，所以一路没人发现。
    with offline(mails=[]) as P:
        guard = _make_pipe(P, _tmp_ledger())
    check("★ [护栏] 自测的 Pipeline 不指向交付物 result/success.jsonl",
          Path(guard.success_ledger.path).resolve()
          != Path(pl.config.SUCCESS_LEDGER_PATH).resolve(),
          str(guard.success_ledger.path))
    check("★ [护栏] 自测的成功台账落在临时目录",
          "tmp" in str(guard.success_ledger.path).lower()
          or tempfile.gettempdir().lower() in str(guard.success_ledger.path).lower(),
          str(guard.success_ledger.path))

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


def test_fan_out_worker_crash_is_recorded() -> None:
    """并发 worker 崩溃必须**留下一条台账记录** —— 不变量：提交数 == 结果数。

    2026-09-20 二轮审计复现的洞（`docs/audit-2026-09-20-round2.md` §1）：
    `_fan_out` 的 `except` 里只打一行日志、**不写台账**，而 `job()` 是先跑完
    再 `ledger.append()` ⇒ 崩溃的账号**既不在返回值里、也不在台账里**。
    实测 4 提交 / 3 返回 / 3 落账，而 `report()` 的"合计"用 `len(recs)` ⇒
    **数字自洽、看不出缺口**。只在 `--concurrency > 1` 时存在。
    """
    print("\n[编排：并发 worker 崩溃不静默丢弃]")
    emails = [f"f{i}@example-mail.test" for i in range(4)]
    boom = emails[2]

    def job(pipe, key):
        if key == boom:
            raise RuntimeError("模拟 worker 崩溃（如 requests 超时未捕获）")
        rec = pl.AccountRecord(key=key, email=key)
        rec.status = "keyed"
        rec.api_key = f"apikey_FAKE_{key}"
        pipe.ledger.append(rec.to_dict())
        return rec

    jobs = [(e, job) for e in emails]
    with offline(mails=[]):
        led = _tmp_ledger()
        pipe = _make_pipe(pl, led)
        out = pipe._fan_out(jobs, concurrency=4)
        rows = led.raw_rows()

        check("★ 返回条数 == 提交条数（不变量）", len(out) == len(jobs),
              f"{len(out)} != {len(jobs)}")
        check("★ 崩溃的那个也落了台账（提交数 == 落账数）", len(rows) == len(jobs),
              f"{len(rows)} != {len(jobs)}")
        crash = [r for r in rows if r.get("status") == "failed"]
        check("崩溃记录带 worker 崩溃原因",
              len(crash) == 1 and "worker 崩溃" in str(crash[0].get("error")),
              str(crash))
        check("崩溃记录的键**明确不是邮箱**（否则会被当账号去补跑）",
              bool(crash) and "worker-crash#" in str(crash[0].get("key")),
              str(crash[0].get("key")) if crash else "")
        check("崩溃记录也在返回值里（调用方数得出来）",
              any(r.status == "failed" for r in out))

        # 再补一条"正常的失败账号"（有邮箱、没 key）—— 它**必须**留在待补清单里
        pipe.ledger.append({"key": "real@example-mail.test",
                            "email": "real@example-mail.test", "status": "failed"})

        # 负对照：串行路径**不吞异常**（fail-fast），所以这个洞只在并发时存在 ——
        # 这正是它隐蔽的原因：串行跑一万次也复现不出来。
        raised = False
        try:
            pipe._fan_out([(boom, job)], concurrency=1)
        except RuntimeError:
            raised = True
        check("[负对照] 串行路径不吞异常（洞只在并发时存在）", raised)

    # 下游影响：占位键不能被当成"待补跑账号"
    # （`resume_pending.pending()` 的输出会被拼成 `--email <key>` 打给站点）
    import resume_pending as rp
    check("★ 待补清单排除 worker-crash 占位键、保留真失败账号",
          rp.pending(led) == ["real@example-mail.test"], str(rp.pending(led)))
