"""编排层自测：错误码分流 / 登录（含编号漂移与链接回捞）/ onboarding 门禁 /
resume 去重 / 重跑不丢 key / 并发不串号 / worker 崩溃不静默丢弃 / 链路无外部阻断点。

全部**离线**：出网点由 `support.offline()` 换成替身。
"""

from __future__ import annotations

import inspect
import json
import re
import tempfile
from pathlib import Path

from .support import (RANK, Mail, _FakeTypeSafe, _confirm_mail, _link_mail,
                      _make_pipe, _otp_mail, _tmp_ledger, check, offline,
                      pl, ps, st, ts)

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
    check("[负对照] 401 不该打上邀请制门禁标记",
          rec.stages.get("invite_gate") != "pending", str(rec.stages))

    # 401 Authentication failed（token 已用过）
    with offline(ts_status=401, ts_body={"error": "Authentication failed"},
                 mails=[_otp_mail("e2@example-mail.test")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).resume(["e2@example-mail.test"])[0]
    check("401 Authentication failed → failed", rec.status == "failed", rec.status)
    check("401 Authentication failed → 错误里点名'token 已被使用'（与过期区分开）",
          "已被使用" in rec.error, rec.error)

    # 403 Access restricted —— 邀请制已取消，但这条分流**必须保留**：
    # 站点随时可能恢复白名单，删掉它会让"没被邀请"伪装成"凭据错误"。
    with offline(ts_status=403, ts_body={"error": "Access restricted"},
                 mails=[_otp_mail("e3@example-mail.test")]) as P:
        rec = _make_pipe(P, _tmp_ledger()).resume(["e3@example-mail.test"])[0]
    check("403 → failed", rec.status == "failed", rec.status)
    check("403 → 标为 invite_only（run_e2e 的阻断统计靠这个串）",
          "invite_only" in rec.error, rec.error)
    check("403 → stages.invite_gate=pending（如实反映'被白名单拦住'）",
          rec.stages.get("invite_gate") == "pending", str(rec.stages))
    check("[负对照] 403 不再使用已删除的 approved 阶段名",
          "approved" not in rec.stages, str(rec.stages))


class _FakeResp:
    def __init__(self, text: str, status: int = 200, headers: dict | None = None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        """`requests` 的 `.json()` 失败时抛的 `JSONDecodeError` 是 `ValueError`
        的子类，而调用方（如 `auth_callback`）正是靠捕获 `ValueError` 兜底的，
        所以这里直接用 `json.loads`，失败行为与真会话一致。"""
        return json.loads(self.text)


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

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        i = min(self.calls, len(self.pages) - 1)
        self.calls += 1
        return _FakeResp(self.pages[i])

    def post(self, url, files=None, headers=None, timeout=None, json=None):
        # `json` 形参与 `requests` 对齐（这里遮住的是模块名，不是别的）；
        # 记录它是为了能逐键断言请求体。
        self.posts.append({"url": url, "files": files or {}, "headers": headers or {},
                           "json": json})
        return self.post_resp


class _GateSession:
    """按 URL 应答的假会话 —— 模拟站点的 **onboarding 串行门禁**。

    真实行为（2026-09-21 实测）：
        ToS 未接受   → `GET /hook` 307 → `/setup/tos?returnTo=…`
        接受 ToS 后  → `GET /hook` 307 → `/setup/set-name?returnTo=…`
        set-name 后  → `GET /hook` 200（欢迎页，链路打通）

    `gate_seq` 是每次 `GET /hook` 依次返回的门禁名（`""` = 已通过）。
    `post_status` 控制 `/setup/*` 的 POST 结果。
    `redirect_to` 让 `/hook` 固定跳到某个**非 `/setup/*`** 的目标（测"门禁不可识别"）；
    `hook_ok` 让 `/hook` 固定 200（测"真的已通过"）。
    """

    def __init__(self, *, gate_seq: list[str] | None = None, post_status: int = 200,
                 redirect_to: str = "", hook_ok: bool = False):
        self.headers: dict = {}
        self.gate_seq = list(gate_seq or [])
        self.post_status = post_status
        self.redirect_to = redirect_to
        self.hook_ok = hook_ok
        self.hook_calls = 0
        self.posts: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        if url.endswith("/hook"):
            self.hook_calls += 1
            if self.hook_ok:
                return _FakeResp("<html><body>Welcome inside TypeSafe</body></html>", 200)
            if self.redirect_to:
                return _FakeResp("", 307, {"location": self.redirect_to})
            i = min(self.hook_calls - 1, len(self.gate_seq) - 1) if self.gate_seq else 0
            gate = self.gate_seq[i] if self.gate_seq else ""
            if gate:
                return _FakeResp("", 307,
                                 {"location": f"/setup/{gate}?returnTo=%2Fhook"})
            return _FakeResp("<html><body>Welcome inside TypeSafe</body></html>", 200)
        if url.endswith("/api/me"):
            return _FakeResp(json.dumps({"human_name": None, "org_memberships": []}), 200)
        return _FakeResp("", 200)

    def post(self, url, files=None, headers=None, timeout=None, json=None):
        self.posts.append({"url": url, "files": files or {}, "headers": headers or {}})
        return _FakeResp('{"error":"Server action not found."}', self.post_status)


def test_auth_callback_payload_shape() -> None:
    """`/api/auth/callback` 的请求体**键集**逐字钉住。

    🔴 2026-09-21 实测：站点把该接口的 schema 收紧成 strict —— 多一个未知键
    直接 400，响应体是 Zod 的 flatten 格式：

        {"error":"Bad request",
         "details":{"formErrors":["Unrecognized key: \\"waitlistEmail\\""],
                    "fieldErrors":{}}}

    当时**所有**账号登录全灭（含 122 个已拿到 key 的），
    而错误文案只有一句 `HTTP 400: Bad request`，完全看不出
    是"我们多发了一个键" —— 排查方向会跑偏到验证码/白名单上。

    这条护栏的作用：下次站点再改 schema，**离线自测先报红**，
    而不是等到真打站点时以一句无指向性的 400 暴露。
    """
    print("\n[auth_callback 请求体形态]")
    ses = _FakeSession([], post_resp=_FakeResp('{"success":true}', 200))
    cl = ts.TypeSafeClient(session=ses)
    cl.auth_callback("123456", "otp", "who@example-mail.test")

    check("确实打了一次 POST", len(ses.posts) == 1, str(len(ses.posts)))
    sent = ses.posts[-1]["json"] or {}
    check("★ 请求体键集 == 站点 strict schema 允许的键",
          set(sent) == {"token", "tokenType", "returnTo", "preferredOrgId",
                        "inviteId", "oauthState"},
          f"实际键集 {sorted(sent)}")
    check("★ 不含 waitlistEmail（站点已删该字段，带上必 400）",
          "waitlistEmail" not in sent, str(sorted(sent)))
    check("token / tokenType 原样透传",
          sent.get("token") == "123456" and sent.get("tokenType") == "otp", str(sent))
    check("打到的是 /api/auth/callback",
          str(ses.posts[-1]["url"]).endswith("/api/auth/callback"),
          str(ses.posts[-1]["url"]))
    check("带 Origin / Referer / Accept",
          {"Origin", "Referer", "Accept"} <= set(ses.posts[-1]["headers"]),
          str(sorted(ses.posts[-1]["headers"])))

    # 负对照：站点报"未知键"时必须翻成一句人能直接执行的话。
    # 不特判的话只剩 `HTTP 400: Bad request` —— 这条正是当时的实际体验。
    with offline() as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = st.AccountRecord(key="x@example-mail.test", email="x@example-mail.test")
        pipe._fail_auth(rec, ts.Result(
            ok=False, stage="auth_callback", status=400,
            data={"error": "Bad request",
                  "details": {"formErrors": ['Unrecognized key: "waitlistEmail"'],
                              "fieldErrors": {}}}))
        check("★ 400 Unrecognized key → 报出具体键名，并指向 auth_callback",
              "Unrecognized key" in rec.error and "auth_callback" in rec.error,
              rec.error)


def test_onboarding_gate_drives_steps() -> None:
    """🔴 onboarding 的判据是**站点的重定向**，不是 `/api/me` 的字段。

    2026-09-21 实测：站点把 onboarding 从三步缩成**两步**（删掉了 console-survey），
    而旧实现用 `/api/me` 的 `console_survey_completed_at` 判断"要不要跑 survey"
    ⇒ 该字段永远为 `None` ⇒ 每次都多发一次 survey POST ⇒ 而那时页面渲染的是
    欢迎页、**没有 `$ACTION_*` 隐藏域** ⇒ 退化到已作废的 fallback ⇒ 404
    ⇒ **账号明明已经完全 onboard，却被记成 `partial`**（实跑 25 个误报 19 个）。

    ⇒ 改成"问站点下一步是什么"（`GET /hook` 不跟随重定向），站点增删步骤时
      本逻辑**自动适应**。
    """
    print("\n[onboarding：站点门禁驱动]")

    # 正例：tos → set-name → 通过
    ses = _GateSession(gate_seq=["tos", "set-name", ""], post_status=200)
    cl = ts.TypeSafeClient(session=ses)
    cl.email = "u1@example-mail.test"
    ob = cl.complete_onboarding(display_name="u1")
    check("★ 两步都跑完 ⇒ 整体 ok", ob.ok, f"ok={ob.ok} error={ob.error!r}")
    check("★ 走的是站点报的步骤（tos → set-name）",
          ob.data.get("completed") == ["tos", "set-name"],
          str(ob.data.get("completed")))
    check("确实 POST 了两次（每步一次）", len(ses.posts) == 2, str(len(ses.posts)))
    check("★ 没有出现 console-survey（站点已删这一步）",
          not any("console-survey" in str(p["url"]) for p in ses.posts),
          str([p["url"] for p in ses.posts]))

    # 正例 2：门禁一次就通过（onboarding 已完成）⇒ 不发任何 POST
    ses2 = _GateSession(gate_seq=[""])
    cl2 = ts.TypeSafeClient(session=ses2)
    ob2 = cl2.complete_onboarding()
    check("[正对照] 门禁已通过 ⇒ 直接成功且零 POST",
          ob2.ok and not ses2.posts, f"ok={ob2.ok} posts={len(ses2.posts)}")

    # 🔴 负对照 1：POST 失败但门禁**推进了** ⇒ 判成功（"上一步顺带完成"）
    ses3 = _GateSession(gate_seq=["tos", "", ""], post_status=404)
    cl3 = ts.TypeSafeClient(session=ses3)
    ob3 = cl3.complete_onboarding()
    check("★ POST 报 404 但门禁已推进 ⇒ 判成功（不误报 partial）",
          ob3.ok, f"ok={ob3.ok} error={ob3.error!r}")
    check("★ 该步被标成「上一步已顺带完成」",
          any("顺带完成" in str(d) for d in ob3.data.get("completed", [])),
          str(ob3.data.get("completed")))

    # 负对照 2：POST 失败且门禁**没动** ⇒ 如实报失败
    ses4 = _GateSession(gate_seq=["tos"], post_status=404)
    cl4 = ts.TypeSafeClient(session=ses4)
    ob4 = cl4.complete_onboarding()
    check("★ [负对照] 门禁没动 ⇒ 如实报失败（不许把真失败放行）",
          not ob4.ok, f"ok={ob4.ok} error={ob4.error!r}")

    # 负对照 3：站点出现我们不认识的步骤 ⇒ 报出来，不硬猜字段
    ses5 = _GateSession(gate_seq=["brand-new-step"])
    cl5 = ts.TypeSafeClient(session=ses5)
    ob5 = cl5.complete_onboarding()
    check("★ [负对照] 未知门禁 ⇒ 明确报出步骤名（不静默死循环）",
          not ob5.ok and "brand-new-step" in ob5.error, ob5.error)

    # 🔴 负对照 4：`/hook` 跳到**非 `/setup/*`** 的地方（会话失效被送回 /login）
    # 旧实现只匹配两个写死路径，未匹配就 `return ""` ⇒ 被上游当成"onboarding
    # 已通过"而放行 —— 门禁明明没过却继续去建 key，是**静默放行**。
    ses6 = _GateSession(gate_seq=[], redirect_to="/login?returnTo=%2Fhook")
    cl6 = ts.TypeSafeClient(session=ses6)
    ob6 = cl6.complete_onboarding()
    check("★ [负对照] 非 /setup/* 的跳转 ⇒ 判失败，不许当成『已通过』",
          not ob6.ok and "/login" in ob6.error, f"ok={ob6.ok} err={ob6.error!r}")

    # 正对照：`/hook` 直接 200 ⇒ 才是真的"已通过"
    ses7 = _GateSession(gate_seq=[], hook_ok=True)
    cl7 = ts.TypeSafeClient(session=ses7)
    ob7 = cl7.complete_onboarding()
    check("  [正对照] /hook 200 ⇒ 判通过（证明上面那条不是把所有跳转都打死）",
          ob7.ok, f"ok={ob7.ok} err={ob7.error!r}")


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

    ⚠️ 2026-09-21 复测：`/login` 页**仍是这个形态**（2/3/4，各带 :0/:1/:2），
    所以 `ACTION_LINK='2'` / `ACTION_CODE='3'` 这两个语义位继续有效。
    """
    print("\n[登录：Server Action 编号漂移 → 重试]")

    ok = ts.TypeSafeClient(session=_FakeSession([_login_html("234")]))
    acts = ok.fetch_actions()
    check("正常页一次就拿到 action", sorted(acts) == ["2", "3", "4"], str(sorted(acts)))
    check("正常页不浪费重试", ok.s.calls == 1, f"GET 次数={ok.s.calls}")

    flaky = ts.TypeSafeClient(session=_FakeSession([_login_html("245"),
                                                    _login_html("234")]))
    acts = flaky.fetch_actions()
    check("★ 编号漂移时重试后能拿到（不再直接判死）",
          sorted(acts) == ["2", "3", "4"], str(sorted(acts)))
    check("★ 确实只重试了一次（GET 2 次）", flaky.s.calls == 2, f"GET 次数={flaky.s.calls}")

    dead = ts.TypeSafeClient(session=_FakeSession([_login_html("245")] * 5))
    try:
        dead.fetch_actions()
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

    ⚠️ 2026-09-21 起默认走 link 模式，这条回捞只在显式 `--login-mode code` 时生效，
    但**不能删** —— 站点仍支持码模式，而它的失败表现与"邮箱坏了"完全一样。
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

    # 负对照：两种邮件都没有 ⇒ 才允许判"验证码邮件也没等到"。
    # 文案必须**同时**点明"码邮件"与"回捞链接"都试过 —— 只说一句"没收到"
    # 会让运维以为回捞没跑（或以为邮箱坏了），把处置引向改请求。
    with offline(mails=[]) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = pl.AccountRecord(key="c@x.com", email="c@x.com")
        cl = pipe.stage_login(rec, mail_timeout=0.1)
    check("[负对照] 两种凭据都没有 → 才判『验证码邮件也没等到』",
          cl is None and "验证码邮件" in (rec.error or "")
          and "回捞魔法链接" in (rec.error or ""),
          f"cl={cl} err={rec.error}")

    # 护栏：回捞窗口不能设太大，否则失败账号每次重跑都要多白等
    check("★ 回捞窗口 <= 60s（不给失败账号拖长重跑）",
          0 < st.LINK_FALLBACK_TIMEOUT <= 60.0, str(st.LINK_FALLBACK_TIMEOUT))


def _setup_page(*, with_action: bool) -> str:
    """造一个 `/setup/*` 页面。

    真实形态（2026-09-20 起）是索引 **1**、**只有 `:0` `:1`**、另加 `$ACTION_KEY`
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
    `onboarding 失败: HTTP 404` —— 与真因毫无字面关联。

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


def test_mail_timeout_headroom() -> None:
    """🔴 回归护栏：等确认邮件的阈值**必须有依据，且与重发成对**。

    2026-09-21 依据**实测延迟分布**把阈值从 300s 下调到 60s ——
    43 个成功账号实测（`signup.mail_at ÷ 1000 − created_at`，50 批次跑批）：
        min 2.66s · P50 3.26s · P90 3.80s · max 4.57s
    ⇒ 60s 仍有 **13 倍**余量。

    ⚠️ 旧断言守的是「≥ 240s，因为 2026-09-20 实测最大延迟 198s」——
    那个 198s **当天就被证伪**：那批是**批量发出**的 waitlist 回执
    （一次性涌入 47 封、延迟约 25 分钟），被误读成"单调爬升"；
    而且那条链路（等审批回执）**已随邀请制取消整体删除**。

    ⇒ 本用例不再钉死具体秒数（那是注定漂移的第二份真源），改成守**设计意图**：
    ① 阈值有下限（防手滑调到秒级）；② 降阈值**必须**配批内重发。
    """
    print("\n[编排：确认邮件等待阈值]")

    d = inspect.signature(st.StageMixin.stage_login).parameters["mail_timeout"].default
    check("★ 默认阈值有下限（防手滑调到秒级，把慢邮件直接判死）",
          isinstance(d, (int, float)) and d >= 30, f"default={d}")
    check("★ 默认阈值取自常量 MAIL_TIMEOUT（单一真源，别写字面量）",
          d == pl.MAIL_TIMEOUT, f"{d} vs {pl.MAIL_TIMEOUT}")
    # 🔴 本轮的核心约束：**降阈值与重发是一个改动，不是两个**。
    # 只降阈值不重发 ⇒ 偶发的慢邮件被直接判死，成功率反而**下降**。
    check("★ 🔴 批内重发已开启（降阈值的**配套**，缺了它成功率会反降）",
          st.RETRY_SEND_ON_TIMEOUT, "RETRY_SEND_ON_TIMEOUT=False")
    check("★ 重发次数有界（防死循环把站点打爆）",
          1 <= st.MAX_SEND_RETRIES <= 3, f"MAX_SEND_RETRIES={st.MAX_SEND_RETRIES}")

    # 超时文案必须点明"丢包 + 链接 7 天有效 ⇒ 重跑比改请求有效"。
    # 否则运维只会看到一句"没收到"，而它的正确处置（重跑）在假阴性场景下
    # 与"站点坏了"的处置完全不同。
    with offline(mails=[]) as P:
        rec = _make_pipe(P, _tmp_ledger()).resume(["t@example-mail.test"],
                                                  mail_timeout=0.2)[0]
    check("★ 邮件超时 → failed", rec.status == "failed", rec.status)
    check("★ 超时文案点明'站点发信丢包'与'链接 7 天有效'（指明下一步是重跑）",
          "丢包" in rec.error and "7 天" in rec.error, rec.error)
    check("★ 超时文案带上了邮箱接口的轮询计数（区分'读不出来'与'没发'）",
          "轮询" in rec.error and "5xx" in rec.error, rec.error)

    # ── 行为验证：超时后**真的**重发了，而不是只把常量改了个数 ──────────
    # 🔴 为什么必须有这条：常量改对、重发逻辑没接上线，是最容易出现的
    # "看着改了其实没生效"。只有数**发信次数**才钉得住它。
    with offline(mails=[]) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        r2 = pl.AccountRecord(key="", email="")
        pipe.stage_login(r2, mail_timeout=0.2)
    check("★ 超时后真的重发了（发信次数 == 1 + 重发上限）",
          _FakeTypeSafe.send_calls == 1 + st.MAX_SEND_RETRIES,
          f"send_calls={_FakeTypeSafe.send_calls}")
    check("★ 重发次数记进台账（signup.send_retries，便于事后复盘）",
          r2.signup.get("send_retries") == st.MAX_SEND_RETRIES, str(r2.signup))
    check("★ 🔴 失败路径也写了耗时（mail_wait / login）—— 旧实现这里是**空的**",
          "mail_wait" in r2.timings and "login" in r2.timings, str(r2.timings))
    check("★ 重发后仍超时 ⇒ 判 failed（重发不是「无限续命」）",
          r2.status == "failed", r2.status)


def test_resume_skips_keyed() -> None:
    """🔴 `resume` 必须跳过台账里**已有 api_key** 的地址。

    重跑会给同一账号**造出第二把 key**。两把在服务端都有效，但
    `Ledger.load()` 按邮箱去重、**末行胜出** ⇒ 交付物里少一把。
    这与 P0 / 阈值误判同一类：**不报错，只是行数不对**。

    （这条保护原先长在 `watch()` 里；`watch()` 随邀请制取消而删除，
      保护移到了 `resume` —— 它是现在唯一的"会被重复调用"入口。）
    """
    print("\n[编排：resume 不重复建 key]")
    dup, fresh = "dup@example-mail.test", "fresh@example-mail.test"
    led = _tmp_ledger()
    led.append({"email": dup, "key": dup, "status": "keyed",
                "api_key": "apikey_ORIGINAL", "api_key_id": "kid_orig"})

    with offline(mails=[_confirm_mail(dup), _confirm_mail(fresh)]) as P:
        recs = _make_pipe(P, led).resume([dup, fresh])

    got = {r.email for r in recs}
    check("★ 已有 api_key 的地址被跳过（不重复建 key）", dup not in got, str(got))
    check("  [正对照] 没有 key 的地址照常被处理（证明跳过是**有选择性**的）",
          fresh in got, str(got))
    after = {r["email"]: r for r in led.load()}[dup]
    check("★ 原 key 未被覆盖（交付物不会少一把）",
          after.get("api_key") == "apikey_ORIGINAL", str(after.get("api_key")))

    # 负对照：显式 skip_keyed=False 时必须真的重跑 ——
    # 否则"保护"就变成了"不能重跑"，而重跑恰恰是补丢包账号的手段。
    with offline(mails=[_confirm_mail(dup)]) as P:
        recs2 = _make_pipe(P, led).resume([dup], skip_keyed=False)
    check("[负对照] skip_keyed=False 时确实重跑（保护是可关的）",
          len(recs2) == 1 and recs2[0].email == dup, str([r.email for r in recs2]))


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
        # skip_keyed=False —— 本次要测的**正是**"重跑"这条路
        rec2 = _make_pipe(P, led).resume(["keep@example-mail.test"],
                                         skip_keyed=False)[0]
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


def test_chain_has_no_external_gate() -> None:
    """🔴 结构护栏：邀请制取消后，链路里**不许再有"为等待而存在"的机制**。

    这条守的是"重构真的删干净了"。任何一处回归（例如有人把
    `stage_wait_approval` 或 `watch()` 加回来）都会在这里报红，
    而不是等到实跑时以"多等了 10 分钟什么也没发生"的形式暴露。
    """
    print("\n[编排：链路无外部阻断点]")

    for name in ("stage_apply", "stage_wait_approval"):
        check(f"★ stages 不再有 {name}（申请/审批段已删除）",
              not hasattr(st.StageMixin, name), f"StageMixin.{name} 还在")
    for name in ("claim", "watch"):
        check(f"★ Pipeline 不再有 {name}（为等待而存在的机制）",
              not hasattr(pl.Pipeline, name), f"Pipeline.{name} 还在")

    rb = inspect.signature(pl.Pipeline.run_batch).parameters
    check("★ run_batch 不再有 mode / approval_timeout / confirm_timeout",
          not ({"mode", "approval_timeout", "confirm_timeout"} & set(rb)),
          str(sorted(rb)))

    # 正向验证：`stage_login` 在 email 为空时**自己建邮箱**
    # （旧链路这一步在 stage_apply 里；删了 stage_apply 就必须有人接住它）
    with offline(mails=[_confirm_mail("*")]) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = pl.AccountRecord(key="", email="")
        cl = pipe.stage_login(rec, mail_timeout=1.0)
    check("★ stage_login 在 email 为空时自建邮箱（接住 stage_apply 的职责）",
          bool(rec.email) and rec.stages.get("mailbox") == "ok" and cl is not None,
          f"email={rec.email!r} stages={rec.stages}")

    check("★ 确认邮件的规则在主路径上（welcome_confirm 是唯一入口凭据）",
          st.MATCH_LINK(_confirm_mail("x@example-mail.test")),
          "MATCH_LINK 不认确认邮件 —— 主路径会直接超时")


def test_onboarding_gate_is_guidance_not_a_gate() -> None:
    """🔴 **建 key 才是判据，`/hook` 的门禁只是引导。**

    2026-09-21 实跑取证（`tools/probes/probe_gate_chain.py`）：

      1. 门禁链其实是**三步** `tos → set-name → console-survey`，而
         `console-survey` 那一页**没有 `$ACTION_*` 隐藏域**
         （渲染的是 "Get started / Let's create your org" 向导）⇒ 我们提交不了；
      2. 就在这个"门禁未归零"的状态下，`POST /api/api-keys` 返回
         **200 + 明文 key**；
      3. 更狠的是 `/hook` **自己非确定性**：同一次运行里相邻两次 `GET /hook`
         给出不同答案（`set-name` 与 200 交替出现）。

    ⇒ 拿门禁当门槛有两个独立后果：① 把"已经能拿 key"的账号判成 `partial`
      （误报，实测踩过）；② 门禁非确定性时循环空转、最后报一句空错误。

    本用例钉住新语义：门禁没归零 **不阻止**建 key；只有 key 建不出来才算失败。
    """
    print("\n[编排：门禁是引导，不是门槛]")

    # 门禁永远停在不认识的步骤上 ⇒ onboarding 判 not ok，但 key 必须照建
    ses = _GateSession(gate_seq=["console-survey"] * 4, post_status=200)
    cl = ts.TypeSafeClient(session=ses)
    cl.email = "g1@example-mail.test"
    ob = cl.complete_onboarding()
    check("★ 不认识的步骤 ⇒ onboarding 报 not ok（不假装成功）",
          not ob.ok, f"ok={ob.ok} err={ob.error!r}")
    check("★ 报出了步骤名与门禁序列（否则没法定位是哪一跳）",
          "console-survey" in (ob.error or "")
          and ob.data.get("gates") == ["console-survey"],
          f"err={ob.error!r} gates={ob.data.get('gates')}")
    check("★ 不认识的步骤**只查一次**就停（门禁非确定性，别空转）",
          ses.hook_calls == 1, f"GET /hook 次数={ses.hook_calls}")

    # 同一跳重复出现（门禁非确定性）⇒ 也必须有界停下
    ses2 = _GateSession(gate_seq=["tos", "tos", "tos", "tos"], post_status=200)
    cl2 = ts.TypeSafeClient(session=ses2)
    ob2 = cl2.complete_onboarding()
    check("★ 同一跳重复出现 ⇒ 有界停下（不把 4 步空推满）",
          not ob2.ok and ses2.hook_calls == 2,
          f"ok={ob2.ok} GET /hook 次数={ses2.hook_calls} "
          f"gates={ob2.data.get('gates')}")
    check("★ 已提交过的步骤不会被重复 POST",
          len(ses2.posts) == 1, f"POST 次数={len(ses2.posts)}")

    # 端到端：门禁卡住 + key 建得出来 ⇒ 必须记 keyed，且留痕说明门禁没归零
    with offline(mails=[_confirm_mail("g2@example-mail.test")],
                 onboarding_ok=False) as P:
        pipe = _make_pipe(P, _tmp_ledger())
        rec = pl.AccountRecord(key="g2@example-mail.test",
                               email="g2@example-mail.test")
        cl3 = pipe.stage_login(rec, mail_timeout=1.0)
        check("  前置：会话已建立", cl3 is not None, f"err={rec.error!r}")
        pipe.stage_create_key(rec, cl3, name="1")
    check("★★ 门禁卡住但 key 建出来 ⇒ status=keyed（不误报 partial）",
          rec.status == "keyed" and bool(rec.api_key),
          f"status={rec.status} key={rec.api_key!r} err={rec.error!r}")
    check("★ 同时留下『onboarding 未归零』的痕迹（不许静默当成功）",
          rec.stages.get("onboarding") == "partial",
          str(rec.stages))
    check("★ 成功路径不往 error 里写东西", rec.error == "", repr(rec.error))


def test_mail_backend_selection() -> None:
    """邮箱后端选择：工厂分派 / 并发克隆 / 域名语义 / 凭证落盘。

    2026-09-21 接入 Remail（第二个邮箱后端）时新增。每条断言都对应一个
    **真实会出事**的场景，不是形式主义：

      ① `_clone()` 丢后端 —— 串行**永远复现不出来**（`concurrency=1` 走的是
         另一条路径），只在 `--concurrency > 1` 时把一部分账号建到另一个后端上，
         而且不报任何错。这正是本项目最忌讳的静默替换。
      ② `domain` 语义混用 —— CF 传完整域名、Remail 传商品名。传错要么被服务端
         拒掉，要么**买到意外商品**（TypeSafe 项目下 `gmail` 是 500 积分/单，
         是 `domain` 的 5 万倍）。
      ③ 取件凭证不落盘 —— `resume` 另起进程时全部邮箱取不了信，而错误看起来
         像"后端坏了"，真因却是"我们自己没存 token"。

    ⚠️ 替身里的 `created` 与"能不能收到信"是**两件事**：`pending` 邮件池是
    共享的，选错后端时信照样收得到（替身没有真实后端的隔离）⇒
    **只看收信成功是查不出后端选错的**，必须看 `created`。
    """
    print("\n[编排：邮箱后端选择]")
    from . import support as S

    # ⚠️ 邮件必须显式喂进去：`offline()` 默认 `mails=None` ⇒ 待收池是空的。
    #    收件人写 `"*"` —— 这批邮箱地址是**运行时**由 `create_mailbox()` 建的，
    #    写死任何具体地址都匹配不上（替身的 `wait_for_mail` 支持 `"*"` 通配）。
    with offline(mails=[_confirm_mail("*")]) as P:
        # ① 工厂：按名字分派；未知名字**抛错而不是回落默认值**
        check("★ make_mail_client('cf') 得到 CF 客户端",
              type(P.make_mail_client("cf")) is S._FakeTempMailClient,
              type(P.make_mail_client("cf")).__name__)
        check("★ make_mail_client('remail') 得到 Remail 客户端",
              type(P.make_mail_client("remail")) is S._FakeRemailClient,
              type(P.make_mail_client("remail")).__name__)
        try:
            P.make_mail_client("remial")           # 拼错一个字母
            check("★ 未知后端抛 ValueError（不静默回落成默认后端）", False, "没抛")
        except ValueError:
            check("★ 未知后端抛 ValueError（不静默回落成默认后端）", True)

        # ② 域名语义必须按后端取 —— 两者不是一回事
        check("★ remail 默认后缀取 REMAIL_EMAIL_SUFFIX（不是 CF 域名）",
              P.default_mail_domain("remail") == P.config.REMAIL_EMAIL_SUFFIX,
              f"remail={P.default_mail_domain('remail')!r} "
              f"cf={P.default_mail_domain('cf')!r}")

        # ③ `_clone()` 保留后端 —— 本轮核心修复
        pipe = P.Pipeline(backend="remail", login_mode=ts.MODE_LINK,
                          ledger=_tmp_ledger(), success_ledger=_tmp_ledger(),
                          verbose=False)
        check("★ Pipeline 记住后端名", pipe.backend == "remail", pipe.backend)
        check("★ 主实例用的是 Remail 客户端",
              type(pipe.mail) is S._FakeRemailClient, type(pipe.mail).__name__)
        kid = pipe._clone()
        check("★★ _clone() 不丢后端（并发 worker 不会静默换回 CF）",
              type(kid.mail) is S._FakeRemailClient, type(kid.mail).__name__)
        check("★ _clone() 的 mail 是**新实例**（不共享 Session）",
              kid.mail is not pipe.mail)
        # 源码级护栏：防止有人把 `mail=` 去掉又退回默认值（那正是本轮的 bug）
        _csrc = inspect.getsource(P.Pipeline._clone)
        check("★ _clone() 源码里显式传 mail（改回默认值就静默丢后端）",
              "mail=self._mail_factory()" in _csrc, _csrc.strip()[:90])

        # 决定性一问：**真跑一遍并发**，看邮箱到底是哪个后端建的。
        # 只看"跑成功了"不够 —— 替身的邮件池是共享的，选错后端也会成功。
        recs = pipe.run_batch(count=2, concurrency=2, mail_timeout=1.0)
        check("  前置：2 个账号都拿到 key",
              all(r.status == "keyed" for r in recs),
              str([(r.status, r.error[:40]) for r in recs]))
        check("★★ 并发 2 个 → 2 个邮箱都由 Remail 后端建出（后端没丢）",
              len(S._FakeRemailClient.created) == 2,
              f"created={S._FakeRemailClient.created}")
        check("★ 同一批里没有 CF 后端掺进来",
              all("remail" in e for e in S._FakeRemailClient.created),
              str(S._FakeRemailClient.created))

    # ④ 取件凭证落盘 —— 跨进程 resume 的**唯一**依靠
    from src import config as cfg
    from src.remail import RemailClient
    with tempfile.TemporaryDirectory() as td:
        old = cfg.REMAIL_STATE_PATH
        cfg.REMAIL_STATE_PATH = Path(td) / "remail_orders.jsonl"
        try:
            c1 = RemailClient(base="http://unused.invalid", api_key="k")
            c1._tokens["a@b.test"] = "tok-1"
            c1._orders["a@b.test"] = "ORD-1"
            c1._save_state("a@b.test", "domain")
            c1._tokens["c@d.test"] = "tok-2"
            c1._save_state("c@d.test", "domain")

            # 另起一个实例 = 模拟 `resume` 的**新进程**
            c2 = RemailClient(base="http://unused.invalid", api_key="k")
            check("★ 凭证能跨实例恢复（resume 另起进程的前提）",
                  c2.restored == 2 and c2._tokens.get("a@b.test") == "tok-1",
                  f"restored={c2.restored} tokens={c2._tokens}")
            check("★ 恢复后能通过取件前的凭证检查",
                  c2._token_for("a@b.test") == "tok-1")

            # 缺 token 的报错必须**可诊断**：三种原因处置完全不同
            try:
                c2._token_for("nope@x.test")
                check("★ 缺 token 抛 RemailError", False, "没抛")
            except Exception as exc:            # noqa: BLE001
                _m = str(exc)
                check("★ 缺 token 的错误文案覆盖三种原因（否则排查必跑偏）",
                      "CF Worker" in _m and "台账" in _m, _m[:130])

            # 坏行不该让整份凭证作废（JSONL 是 append-only，断电会留半行）
            with open(cfg.REMAIL_STATE_PATH, "a", encoding="utf-8", newline="") as f:
                f.write("{这不是合法 JSON\n")
            c3 = RemailClient(base="http://unused.invalid", api_key="k")
            check("★ 坏行被跳过（不因半行垃圾丢掉全部凭证）",
                  c3.restored == 2, f"restored={c3.restored}")
        finally:
            cfg.REMAIL_STATE_PATH = old

    # ⑤ 三个入口都必须有 `--mail-backend`，且 resume_pending 必须**透传**给子进程
    #    （漏一个 = 那个入口静默用默认后端，而日志里写的却是另一个）
    _root = Path(__file__).resolve().parents[2]
    for _name in ("run_e2e.py", "resume_pending.py", "relogin_pending.py"):
        _s = (_root / "tools" / _name).read_text(encoding="utf-8")
        check(f"★ tools/{_name} 有 --mail-backend 参数",
              '"--mail-backend"' in _s, "缺少参数")
    check("★ resume_pending 把后端**透传**给子进程（漏了会静默用 cf）",
          '"--mail-backend", args.mail_backend' in
          (_root / "tools" / "resume_pending.py").read_text(encoding="utf-8"),
          "没透传")
    check("★ runner 模块级持有 RemailClient（patch 接缝，不能延迟 import）",
          hasattr(pl, "RemailClient") and hasattr(pl, "TempMailClient"))

    # ⑥ 取件必须把 `bodyPreview` 换成**全文** —— 预览会把正文里的链接截掉
    from src.remail import RemailClient
    _MAGIC = ("https://login.typesafe.ai/v1/magic_links/redirect"
              "?stytch_token_type=magic_links&token=FULLTOK")

    class _PreviewOnly(RemailClient):
        """列表只给**没有链接的预览**，全文才有链接 —— 复刻 Remail 的真实形态。

        2026-09-21 实测数据：Remail 的 `bodyPreview` 是 **248 字符**的纯文本摘要
        （`"Confirm your email to finish setting up TypeSafe. …"`），
        **完全没有链接**；而全文 **4012 字符**里才有魔法链接。
        不补"取全文"这一步，`extract_magic_link` 永远返回空，外层报的是
        `魔法链接邮件里没找到链接` —— 把"我们只读了预览"说成"站点没发链接"，
        属于典型的"不报错、只是数据变错"。

        ⚠️ 继承**真类**而不是替身：要测的就是真类自己的 `wait_for_mail`。
        只覆写两个取数方法，不发任何网络请求。
        """

        def __init__(self):
            super().__init__(base="http://unused.invalid", api_key="k")
            self._tokens["p@q.test"] = "tok"

        def list_mails(self, email=None, limit=None):
            return [Mail(id="m-full", to="p@q.test", sender="login@typesafe.ai",
                         subject="Welcome to TypeSafe \u2014 confirm your email",
                         body="Confirm your email to finish setting up TypeSafe.",
                         received_at=1)]

        def fetch_body(self, email, message_id):
            return f'<a href="{_MAGIC}">Confirm</a>'

    _pv = _PreviewOnly()
    _got = _pv.wait_for_mail("p@q.test", st.MATCH_LINK, timeout=2.0, interval=0.1)
    check("  前置：匹配到了确认邮件", _got is not None)
    if _got is not None:
        check("★★ wait_for_mail 返回的是**全文**（预览里没有链接）",
              "token=FULLTOK" in _got.body, _got.body[:90])
        _links = st.extract_magic_links(_got.body)
        check("★ 因此取链接拿得到（阶段层用的是复数版，按序试候选）",
              bool(_links) and _links[0].endswith("token=FULLTOK"), _links)
    check("[负对照] 直接拿预览提取**确实为空**（证明这个坑真实存在）",
          st.extract_magic_links("Confirm your email to finish setting up "
                                 "TypeSafe.") == [])

    _rsrc = inspect.getsource(RemailClient.wait_for_mail)
    check("★ 取全文发生在**匹配成功后**（轮询里逐封取会打爆取件配额）",
          _rsrc.index("_hydrate") > _rsrc.index("if match(m)"), _rsrc.strip()[:90])
    check("★ list_mails 保持轻量（不在里面逐封取全文）",
          "_hydrate" not in inspect.getsource(RemailClient.list_mails),
          "list_mails 里调了取全文")


def test_magic_link_tries_all_candidates() -> None:
    """魔法链接候选：**完整的排第一**，失败则**按序继续试**下一条。

    2026-09-21 补跑实测（4/100 账号被误判死的根因）：正文里同一个魔法链接有
    12 份，**8 份被截断了 4 个字符**（`&token=D8SZNL…` → `&tokenSZNL…`）。
    残缺那条 GET 回 `400 invalid_public_token_id`，外层把它读成
    "链接可能已被使用/过期" ⇒ 账号被误判死 —— 而**完整那条就在同一封邮件里**。

    两种形态都要覆盖，缺一个就是假绿：
      A. 正文里同时有残缺与完整 ⇒ 完整的**第一个**被试（不浪费请求）；
      B. 完整的也可能因"已被消费"失败 ⇒ 必须**继续试**下一条完整候选。
    判据是**终态**（`auth_callback` 被调用、URL 顺序正确），不是报错文案好不好看。

    ⚠️ 安全性依据：残缺那条只会拿回 400、**不消耗**一次性 token，
    所以逐条试不会把好链接试坏（`_exchange_link` 一拿到 redirect 就立刻返回）。
    """
    print("\n[编排：魔法链接候选 —— 完整优先 + 失败继续试]")
    head = ("https://login.typesafe.ai/v1/magic_links/redirect"
            "?public_token=public-token-live-620c0996-4644-4af3-b592-31cb05006523"
            "&stytch_token_type=magic_links&token")
    bad = head + "SZNLCEWqnwIucRySURNiIZob5Y5B8rVl7nEenAmiWJ"
    good = head + "=D8SZNLCEWqnwIucRySURNiIZob5Y5B8rVl7nEenAmiWJ"
    good2 = head + "=OTHERSECONDVARIANT"

    tried: list[str] = []
    called: list[tuple] = []

    class _Cli:
        def exchange_magic_link(self, url):
            tried.append(url)
            # 判据用"这条 URL 带不带 `&token=`" —— 与 `_looks_complete` 同源，
            # 但这里是**独立的第二实现**，避免"用被测代码验证被测代码"。
            if not any(p.startswith("token=") for p in url.split("&")[1:]):
                raise ts.TypeSafeError("魔法链接页面未找到 dfp 交换参数"
                                       "（链接可能已被使用/过期，页面长度 256）")
            return ("https://console.typesafe.ai/?stytch_token_type=magic_links"
                    "&token=OK")

        def token_from_redirect_url(self, url):
            return "OK"

        def auth_callback(self, token, token_type, email):
            called.append((token, token_type, email))
            return ts.Result(ok=True, status=200, stage="auth_callback", data={})

    def _mail(body: str, i: str = "m1"):
        return Mail(id=i, to="a@b.test", sender="s@x.test",
                    subject="Welcome to TypeSafe \u2014 confirm your email",
                    body=body, received_at=1)

    # ── 形态 A：正文里**残缺在前、完整在后**（实测就是这个顺序）──
    with offline():
        pipe = _make_pipe(pl, _tmp_ledger())
        rec = st.AccountRecord(key="a@b.test", email="a@b.test")
        res = pipe._exchange_link(rec, _mail(f"Continue:\n{bad}\n\nTrouble:\n{good}\n"),
                                  _Cli())
    check("★★ 完整的候选排第一，只发**一次**请求（不浪费往返）",
          tried == [good], [u[-26:] for u in tried])
    check("★ 最终交换成功并回调（终态）",
          res is not None and res.ok and called == [("OK", "magic_links", "a@b.test")],
          f"res={res!r} called={called}")
    check("★ 没被记成失败", not rec.error, repr(rec.error))

    # ── 形态 B：完整候选也可能已失效 ⇒ 必须继续试下一条 ──
    class _CliFirstCompleteDead(_Cli):
        def exchange_magic_link(self, url):
            tried.append(url)
            if "D8SZNL" in url or not any(p.startswith("token=")
                                          for p in url.split("&")[1:]):
                raise ts.TypeSafeError("魔法链接页面未找到 dfp 交换参数"
                                       "（链接可能已被使用/过期，页面长度 256）")
            return ("https://console.typesafe.ai/?stytch_token_type=magic_links"
                    "&token=OK2")

    tried.clear(); called.clear()
    with offline():
        pipe2 = _make_pipe(pl, _tmp_ledger())
        rec2 = st.AccountRecord(key="a@b.test", email="a@b.test")
        res2 = pipe2._exchange_link(
            rec2, _mail(f"{bad}\n{good}\n{good2}\n", "m2"), _CliFirstCompleteDead())
    check("★★ 第一条完整候选失效后**继续试**下一条，共 2 次请求",
          tried == [good, good2], [u[-26:] for u in tried])
    check("★ 第二条成功 ⇒ 终态成功", res2 is not None and res2.ok, repr(res2))

    # ── 负对照 ──
    check("[负对照] 残缺形态 `_looks_complete` 为假",
          not ps._looks_complete(bad), bad[-40:])
    check("[负对照] 完整形态 `_looks_complete` 为真",
          ps._looks_complete(good), good[-40:])

    class _CliAlwaysFail(_Cli):
        def exchange_magic_link(self, url):
            tried.append(url)
            raise ts.TypeSafeError("魔法链接页面未找到 dfp 交换参数"
                                   "（链接可能已被使用/过期，页面长度 256）")

    tried.clear()
    with offline():
        pipe3 = _make_pipe(pl, _tmp_ledger())
        rec3 = st.AccountRecord(key="c@d.test", email="c@d.test")
        res3 = pipe3._exchange_link(rec3, _mail(f"Continue:\n{bad}\n", "m3"),
                                    _CliAlwaysFail())
    check("[负对照] 全部候选都失败时返回 None（不假装成功）", res3 is None, repr(res3))
    check("[负对照] 错误里带候选条数（1 条时不写，避免噪声）",
          "魔法链接交换失败" in rec3.error and "已试" not in rec3.error, rec3.error)
    check("[负对照] 失败落在 login 阶段", rec3.stages.get("login") == "failed",
          str(rec3.stages))


def test_remail_probe_is_readonly() -> None:
    """`tools/probes/probe_remail.py` 必须**只读** —— 它跑在**付费**后端上。

    🔴 为什么值得一条源码级护栏：这个探针的**全部价值**是"花钱之前先看一眼"。
    哪天有人为了"顺便验证下单能不能通"在里面加一行 `cl.create_mailbox(...)`，
    它就变成"每次体检都真实扣积分"，而且**不报错**、只是账户余额悄悄变少 ——
    与本项目已发生过的"自测替身漏覆盖会花钱的方法"是同一类失效。

    判据是**调用点**而不是关键词：模块 docstring 里**必须**能提到
    `create_mailbox`（用来解释"为什么这里不做这件事"），所以不能简单地
    `"create_mailbox" not in src` —— 那是把文档也一起禁掉。
    """
    print("\n[工具：Remail 探针必须只读]")
    path = Path(__file__).resolve().parent.parent / "probes" / "probe_remail.py"
    check("★ 探针文件存在", path.is_file(), str(path))
    src = path.read_text(encoding="utf-8")

    # 1. 没有任何"付费动作"的调用点
    paid = re.findall(r"\.(create_mailbox|order|purchase)\s*\(", src)
    check("★★ 没有付费动作的调用点（create_mailbox / order / purchase）",
          not paid, f"发现 {paid}")

    # 2. 所有 HTTP 调用都是 GET
    methods = re.findall(r'_request\(\s*"([A-Z]+)"', src)
    check("★★ 所有 _request 调用都是 GET",
          bool(methods) and set(methods) == {"GET"}, f"实际 {methods}")
    check("★ 没有裸 POST/PUT/DELETE 调用",
          not re.search(r"\.(post|put|delete)\s*\(", src), "出现了写方法调用")

    # 3. 负对照：正则本身要能判负 —— 否则"全绿"可能只是它不会判红
    fake = 'cl.create_mailbox("x")\ncl.s.post("/v1/open/orders")\n'
    check("[负对照] 正则能识别出付费调用（证明上面不是假绿）",
          bool(re.findall(r"\.(create_mailbox|order|purchase)\s*\(", fake))
          and bool(re.search(r"\.(post|put|delete)\s*\(", fake)), fake)

    # 4. 退出码契约：凭据缺失 2 / 接口不可用 3 / 有告警 1 / 全绿 0
    for code in ("return 2", "return 3", "return 1 if WARN else 0"):
        check(f"★ 退出码契约含 `{code}`", code in src, code)


def test_check_deliverables_is_readonly_and_loose() -> None:
    """`tools/check_deliverables.py` 必须**只读**，且**不许写死 key 的 hex 长度**。

    🔴 为什么需要（2026-09-21 实测，两个都会静默误导）：

    ① **它跑在交付物上**。这是验收链路的最后一环，工具自己写坏 `result/` 是最贵的失效 ——
       而且 `verify_keys.py` 已经把 `keys.txt` 覆盖写一遍了，再来一个写手就没人能分清
       是哪一步改的。判据是**调用点**（`open(..., "w")` / `.write_text` / `os.remove` …），
       不是关键词 —— 模块 docstring 里**必须**能提"不写任何文件"。

    ② **写死 key 的 hex 长度就是假警报的源头**。第一版把形态写成
       `apikey_<32hex>_<64hex>` ⇒ 459 条全被判"畸形"、`apikeys.txt` 与 `keys.txt` 的差集
       凭空出现 **918**（459+459），看起来像"两份交付物严重不一致"，其实**只是我的正则错了**。
       实测形态是 `apikey_<35 hex>_<64 hex>`。
       ⇒ 护栏：①源码里不许出现 `{32}`/`{35}`/`{64}` 这类**写死的长度量词**；
               ②拿真实形态喂给它，必须能匹配；③负对照证明它**确实在匹配**而不是恒真。
    """
    print("\n[工具：交付物复核必须只读 + 正则不许写死长度]")
    path = Path(__file__).resolve().parent.parent / "check_deliverables.py"
    check("★ 复核工具存在", path.is_file(), str(path))
    src = path.read_text(encoding="utf-8")

    # 1. 没有任何写盘调用点
    writers = re.findall(
        r'(open\([^)]*["\'](?:w|a|x)b?["\']|\.write_text\s*\(|\.write_bytes\s*\(|'
        r'os\.remove\s*\(|os\.rename\s*\(|os\.replace\s*\(|shutil\.)', src)
    check("★★ 没有写盘调用点（open w/a/x · write_text · os.remove/rename · shutil）",
          not writers, f"发现 {writers}")

    # 2. 退出码契约：0 全绿 / 1 有不一致
    check("★ 退出码契约含 `return 0`", "return 0" in src, "0")
    check("★ 退出码契约含 `return 1`", "return 1" in src, "1")

    # 3. 🔴 不许写死 hex 长度（本次假警报的源头）
    hard = re.findall(r"apikey_\[0-9a-f\]\{(\d+)\}", src)
    check("★★ 正则里没有写死的 hex 长度量词（`apikey_[0-9a-f]{N}`）",
          not hard, f"发现写死长度 {hard}")

    # 4. 宽松正则必须真能匹配实测形态，且**不是恒真**
    real = ("apikey_" + "a" * 35 + "_" + "b" * 64)
    m = re.search(r"apikey_[0-9a-f_]{20,}", real)
    check("★ 宽松正则可匹配实测形态 `apikey_<35hex>_<64hex>`",
          bool(m) and m.group(0) == real, str(m.group(0) if m else None)[:40])
    check("[负对照] 同一正则对不含 apikey_ 的串返回 None（证明它在真匹配）",
          re.search(r"apikey_[0-9a-f_]{20,}", "oai-2de1e64293f748d7@x.cloud") is None,
          "恒真正则会误报")


