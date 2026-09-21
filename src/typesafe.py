"""TypeSafe 控制台客户端。

链路（全部为纯 HTTP，无需浏览器）：

    1. GET  /login                        -> 从 HTML 里抓 3 个 Server Action
    2a. POST /login  ACTION_2             -> 确认邮件（Stytch magic_links）
    2b. POST /login  ACTION_3             -> 6 位验证码邮件（Stytch otp）
    3.  (仅魔法链接) GET  login.typesafe.ai/v1/magic_links/redirect?...  -> 拿到 dfp 交换参数
        POST login.typesafe.ai/v1/magic_links/redirect/dfp             -> 拿 Stytch session token
    4. POST /api/auth/callback  {token, tokenType, …}                 -> 建立控制台会话
    5. GET  /hook（**不跟随重定向**）                                  -> 读出 onboarding 门禁
    6. POST /setup/tos → /setup/set-name                              -> 完成 onboarding
    7. POST /api/api-keys  {name}                                     -> 拿到明文 api_key

2026-09-21：邀请制取消，链路缩短
────────────────────────────────
旧链路必须先"投 Framer 表单申请 → 等回执 → 等人工审批"才轮得到第 1 步，
且 `/login?waitlist=<email>` 的参数用来**预填邮箱**。现在：

  · `?waitlist=` 参数**已失效**（实测带与不带渲染的页面逐字节相同，
    邮箱不再出现在页面里）⇒ 已从本模块移除，不要再加回去；
  · `/login` 提交后**直接**发 "Welcome to TypeSafe — confirm your email"；
  · `POST /api/auth/callback` 直接 200，**不再有 `403 Access restricted`**；
  · onboarding 只剩两步，且由**站点重定向**驱动（见 `onboarding_gate`）。

⇒ 注册与登录合并成一次动作，`stages.stage_login` 是链路的唯一入口。

**Server Action 的处理方式**：不硬编码 action id。
`/login` 页把 `$ACTION_<n>:0/1/2` 三个隐藏域直接渲染在 HTML 里（React 的渐进增强路径），
其中 `:2` 是服务端加密的 bound args，必须**原样回传**。每次 GET 都会变，
所以每次提交前都要重新抓一遍 —— 这样也顺带免疫了部署换 hash。

**解析层不在这里**：`$ACTION_*` 隐藏域、JS 对象字面量、可见文案、魔法链接的抠取
全部在 `src/parsing.py` —— 纯函数、零第三方依赖，可以脱离 `requests` 单独测。
本模块只负责"发请求 / 判断状态码 / 组装 Result"。

**邀请制门槛（保留防御）**：TypeSafe 已取消邀请制，但服务端随时可能恢复白名单。
届时未被邀请的邮箱会在第 4 步返回 `403 {"error":"Access restricted"}`。
`stages._fail_auth` 保留了这条分流 —— 删掉它会让"没被邀请"伪装成"凭据错误"。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config
from .parsing import (action_form_fields, actions_from_html, parse_js_object,
                      visible_text)

ACTION_LINK = "2"      # "Continue" -> 魔法链接
ACTION_CODE = "3"      # "Email me a code instead" -> 6 位验证码
# ⚠️ 这里曾有一个 `ACTION_GOOGLE = "4"`，零引用，2026-09-20 二轮审计后删除。
# 删的理由不是"没用到"，而是**它会误导**：action 编号会整体漂移
# （实测 `('2','3','4')` → `('2','4','5')`，见 `fetch_actions` 的说明），
# 留一个 `ACTION_GOOGLE = "4"` 会让人以为"4 号表单 = Google 登录"，
# 从而写出 `acts["4"]` 这种脆弱依赖。真要支持 Google 登录，
# 得先解决"如何在不写死编号的前提下识别 Google 表单"，那是另一件事。

MODE_LINK = "link"
MODE_CODE = "code"


# /setup/* 页面的 Server Action ID，取自录制 HAR。
#
# 🔴 **这些 id 已知会随部署失效，不是可用通路**（2026-09-20 实测全部作废：
#    POST 回 `404 Server action not found.`）。三处文档都写明了这一点
#    （`architecture.md` §5 · `runbook.md` §4.6 · `runbook.md` §5）。
#    保留它只是为了"页面跳过了隐藏域"这类边缘场景留一条最后手段 ——
#    首选**永远**是运行时抓 `$ACTION_*` 隐藏域。
#    ⇒ 走这条路必须留痕：`post_setup()` 会打显式告警，失败时 error 指向真因。
#
# 2026-09-21 更新：`/setup/console-survey` 条目**已删除** —— 站点把 onboarding
# 简化成两步（ToS → set-name），那个路由不再存在（实测所有 `/setup/*` 与 `/hook`
# 在 ToS 未接受时一律 307 到 `/setup/tos`，接受后一律 307 到 `/setup/set-name`）。
# 下面两个 id 是 2026-09-21 实测值（与同日 HAR 的 `next-action` 头逐字一致）。
FALLBACK_SETUP_ACTIONS = {
    "/setup/tos": "6046a5522e4a3fd387f720ff2166e85612b1b5b9db",
    "/setup/set-name": "6040f80f99bc6e6200e02120c32264d7eb784c6f2d",
}

#: `/hook` 是 onboarding 的**终点页**：站点把未完成 onboarding 的访问者从这里
#: 307 到"当前该做的第一步"。所以它的重定向目标就是**站点权威的门禁状态**。
#: 这里只列**我们真的会提交**的步骤；站点若新增步骤，`onboarding_gate()` 会
#: 把新步骤名原样报出来（而不是当成"已通过"）。
KNOWN_ONBOARDING_GATES: tuple[str, ...] = ("tos", "set-name")

#: 从重定向目标里抠出步骤名：`/setup/<slug>?returnTo=…` → `<slug>`。
#: 🔴 不写死 slug 集合：站点加一步就会静默落到"未匹配 ⇒ 已通过"那条路上去。
SETUP_PATH_RE = re.compile(r"/setup/([A-Za-z0-9._-]+)")

#: `complete_onboarding` 最多推几步。当前站点是 2 步（tos → set-name），
#: 留一倍余量；这个上限的存在意义是**防死循环**（站点若把门禁写成
#: "接受 ToS 后又回到 ToS"，没有它就会无限 POST）。
MAX_ONBOARDING_STEPS = 4


class TypeSafeError(RuntimeError):
    def __init__(self, msg: str, *, status: int | None = None, code: str = ""):
        super().__init__(msg)
        self.status = status
        self.code = code


@dataclass
class Result:
    ok: bool = False
    stage: str = ""
    error: str = ""
    status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


# 解析层（`$ACTION_*` 隐藏域 / JS 对象字面量 / 可见文案 / 魔法链接）已整体挪到
# `src/parsing.py`（2026-09-20 二轮审计 §8.2）。本模块只保留 HTTP 链路：
# 抓页面 → 发信 → 换 session → 回调 → onboarding → 建 key。
# ⚠️ 那两个模块的耦合只有"文本"这一层：本模块 `from .parsing import …`，
#    `parsing` 不反向依赖本模块（也不依赖 `requests`，所以它能被单独 import）。


class TypeSafeClient:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": config.UA})
        self.email = ""
        self.log: list[str] = []

    # ── 内部 ──────────────────────────────────────────────────────────
    def _login_url(self) -> str:
        """登录页 URL。

        ⚠️ 这里曾经是 `/login?waitlist=<email>` —— 那个参数用于**预填邮箱**。
        2026-09-21 实测已失效：带与不带它渲染的页面逐字节相同，邮箱不再出现在
        页面里。留着它会让人以为"发信还依赖它"，所以删掉。
        """
        return config.SITE_LOGIN

    def _login_headers(self) -> dict[str, str]:
        return {
            "Origin": config.SITE_ORIGIN,
            "Referer": self._login_url(),
            "Accept": "text/html,application/xhtml+xml",
        }

    # ── 1. 抓 Server Action ───────────────────────────────────────────
    #: `/login` 页抓 Server Action 的尝试次数。
    #:
    #: 🔴 2026-09-20 实测：action **编号会整体位移**。正常渲染是 `('2','3','4')`，
    #: 某次拿到 `('2','4','5')` —— 中间被插入了一个新表单，`ACTION_CODE='3'` 就不见了，
    #: 15 分钟后复测 15/15 又全是 `('2','3','4')`。
    #: 也就是说"编号稳定"是**写死索引的前提，而这个前提会被部署/边缘缓存短暂破坏**。
    #: 所以缺索引时先**原样重试一次**再判死（与"整批 HTTP 404 先重试"同类处置），
    #: 不做语义识别是因为 LINK/CODE 只能靠编号区分，硬猜会发错凭据类型。
    ACTION_FETCH_ATTEMPTS = 2

    def fetch_actions(self) -> dict[str, dict[str, str]]:
        last: tuple[str, ...] = ()
        for i in range(self.ACTION_FETCH_ATTEMPTS):
            r = self.s.get(config.SITE_LOGIN, timeout=30)
            if r.status_code != 200:
                raise TypeSafeError(f"GET /login -> HTTP {r.status_code}",
                                    status=r.status_code)
            acts = actions_from_html(r.text)
            if ACTION_LINK in acts and ACTION_CODE in acts:
                return acts
            last = tuple(sorted(acts))
            if i + 1 < self.ACTION_FETCH_ATTEMPTS:
                time.sleep(1.0)
        raise TypeSafeError(
            f"/login 页未渲染出预期的 Server Action（拿到 {sorted(last)}，"
            f"已重试 {self.ACTION_FETCH_ATTEMPTS} 次）"
            " —— 页面结构可能变了，或该 URL 被改成了无表单版本"
        )

    # ── 2. 触发发信 ───────────────────────────────────────────────────
    def send_login_email(self, email: str, mode: str = MODE_CODE) -> Result:
        """提交登录表单，触发 Stytch 发信。返回页面的可见文案。

        2026-09-21 起这是链路的**第一步**（不再需要先申请/等审批）。
        `MODE_LINK` 会发 "Welcome to TypeSafe — confirm your email"，
        `MODE_CODE` 会发 6 位验证码。
        """
        n = ACTION_LINK if mode == MODE_LINK else ACTION_CODE
        acts = self.fetch_actions()
        a = acts[n]
        files = action_form_fields(n, a)
        files["email"] = (None, email)
        r = self.s.post(config.SITE_LOGIN, files=files,
                        headers=self._login_headers(), timeout=40)
        txt = visible_text(r.text)
        self.email = email
        self.log.append(f"send_login_email(mode={mode}) HTTP {r.status_code}")
        return Result(ok=r.status_code == 200, stage="send_login_email",
                      status=r.status_code, data={"page_text": txt,
                                                  "action_id": a["id"], "mode": mode})

    # ── 3. 魔法链接 -> Stytch session token ───────────────────────────
    def exchange_magic_link(self, magic_url: str) -> str:
        """GET 魔法链接 -> POST dfp 交换 -> 返回控制台回调 URL。"""
        page = self.s.get(magic_url, timeout=40)
        m = re.search(r"xhr\.send\(JSON\.stringify\((\{.*?\})\)\);", page.text, re.S)
        if not m:
            raise TypeSafeError(
                "魔法链接页面未找到 dfp 交换参数（链接可能已被使用/过期，"
                f"页面长度 {len(page.text)}）"
            )
        payload = parse_js_object(m.group(1))
        if "public_token" not in payload or "redirect_url" not in payload:
            raise TypeSafeError(f"dfp 参数解析不完整: {sorted(payload)}")
        payload["telemetry_id"] = ""      # 浏览器侧是 Promise.race(5s) 兜底成 ''
        r = self.s.post(f"{config.STYTCH_LOGIN_HOST}/v1/magic_links/redirect/dfp",
                        json=payload,
                        headers={"Accept": "application/json",
                                 "Content-Type": "application/json;charset=UTF-8",
                                 "Origin": config.STYTCH_LOGIN_HOST,
                                 "Referer": magic_url},
                        timeout=40)
        if r.status_code != 200:
            raise TypeSafeError(f"dfp 交换失败 HTTP {r.status_code}: {r.text[:200]}",
                                status=r.status_code)
        redirect_url = (r.json() or {}).get("redirect_url")
        if not redirect_url:
            raise TypeSafeError(f"dfp 未返回 redirect_url: {r.text[:200]}")
        return redirect_url

    @staticmethod
    def token_from_redirect_url(redirect_url: str) -> str:
        m = re.search(r"[?&]token=([^&]+)", redirect_url)
        if not m:
            raise TypeSafeError(f"redirect_url 中没有 token: {redirect_url[:160]}")
        return requests.utils.unquote(m.group(1))

    # ── 4. 认证回调 ───────────────────────────────────────────────────
    def auth_callback(self, token: str, token_type: str, email: str) -> Result:
        # 🔴 `waitlistEmail` 已于 2026-09-21 从请求体**删除**，不要加回来。
        #
        # 站点把该接口的 schema 收紧成了 strict：**多一个未知键直接 400**，
        # 响应体是 Zod 的 flatten 格式 ——
        #     {"error":"Bad request",
        #      "details":{"formErrors":["Unrecognized key: \"waitlistEmail\""],
        #                 "fieldErrors":{}}}
        # 当时**所有**账号（含 122 个已获批、早已拿到 key 的）登录全部失败，
        # 而错误文案只有一句 `HTTP 400: Bad request` —— 完全看不出
        # 是"我们多发了一个键"，排查方向会跑偏到验证码/白名单上。
        #
        # 邮箱现在由 token/session 在**服务端**推导：200 响应体里自带
        # `"email":"<该账号>"`，客户端不需要（也不允许）再传。
        # `email` 形参仍然保留 —— 它还在给 `Referer` 用。
        #
        # 护栏：`tools/tests/test_orchestration.py::test_auth_callback_payload_shape`
        # 逐键钉住请求体，多键/少键都会报红。
        payload = {
            "token": token,
            "tokenType": token_type,
            "returnTo": None,
            "preferredOrgId": None,
            "inviteId": None,
            "oauthState": None,
        }
        r = self.s.post(f"{config.SITE_ORIGIN}/api/auth/callback", json=payload,
                        headers={"Origin": config.SITE_ORIGIN,
                                 "Referer": self._login_url(),
                                 "Accept": "application/json"}, timeout=40)
        body: dict[str, Any] = {}
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        code = str(body.get("error") or body.get("code") or "")
        res = Result(ok=r.status_code == 200, stage="auth_callback",
                     status=r.status_code, data=body,
                     error="" if r.status_code == 200 else f"HTTP {r.status_code} {code}")
        # `email` **不参与请求体**（见上），只用于 Referer 与这条日志 ——
        # 它标识"这次回调是为哪个邮箱做的"，并发排查时靠它对齐。
        self.log.append(f"auth_callback({token_type}, {email}) HTTP {r.status_code} {code}")
        return res

    def me(self) -> dict[str, Any] | None:
        r = self.s.get(f"{config.SITE_ORIGIN}/api/me",
                       headers={"Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # ── 6. onboarding ─────────────────────────────────────────────────
    def fetch_setup_actions(self, path: str) -> dict[str, dict[str, str]]:
        """GET 一个 /setup/* 页面，抓出它渲染的 Server Action 隐藏域。"""
        r = self.s.get(f"{config.SITE_ORIGIN}{path}", timeout=30)
        if r.status_code != 200:
            raise TypeSafeError(f"GET {path} -> HTTP {r.status_code}", status=r.status_code)
        acts = actions_from_html(r.text)
        if not acts:
            raise TypeSafeError(
                f"{path} 未渲染出 Server Action 隐藏域 —— 该页可能直接跳过了"
                "（onboarding 已完成），或页面结构变了"
            )
        return acts

    def post_setup(self, path: str, fields: dict[str, str], *,
                   action_n: str | None = None) -> Result:
        """提交 /setup/* 表单。

        两条通路，按页面实际渲染的内容自动选：

        A. **渐进增强形态**（首选）：页面里带 `$ACTION_*` 隐藏域 → 用无 JS 表单提交。
           与 /login 完全同构，已被实测验证。
        B. **带 JS 形态**（降级 / 最后手段）：`next-action: <id>` 头 + multipart，
           字段名加 `_1_` 前缀，另带 `0 = [{},"$K1"]`。action id 取
           `FALLBACK_SETUP_ACTIONS`。

        🔴 **降级通路必须留痕**（2026-09-21 第三轮扫描修的洞）。
        抓不到隐藏域只有两种可能：① 页面跳过了（onboarding 已完成）；
        ② 站点改版。B 通路用的 action id 是**录制值，实测已全部作废**
        （POST 回 `404 Server action not found.`）⇒ 它对②根本救不了。

        以前这里 `except TypeSafeError: acts = {}` 是**静默**的，于是②最终只表现为
        `onboarding 失败: HTTP 404` —— 与真因（没抓到隐藏域）毫无字面关联，
        正是 runbook §4.6 那段"为什么难定位"复盘的结构性成因。现在：
          · 降级时往 `self.log` 写一条**可辨识的告警**；
          · 失败时 `Result.error` 点出真因 + 下一步动作，不再只回 `HTTP 404`。

        判据对照 `stages.stage_login` 的 OTP 降级（`how != "anchored"` 必打告警）——
        同一模式，两处处置必须一致。自测 `test_post_setup_degrade_is_observable` 钉住。
        """
        base = path.split("?", 1)[0]
        fetch_err = ""
        try:
            acts = self.fetch_setup_actions(path)
        except TypeSafeError as exc:
            acts, fetch_err = {}, str(exc)
            self.log.append(
                f"⚠ post_setup({path}) 未抓到 $ACTION_* 隐藏域 —— 退化到降级通路；"
                f"该通路的 action id 是录制值，**已知会随部署失效**（runbook §4.6）"
                f"｜原因：{exc}")

        if acts:
            n = action_n or next(iter(acts))
            a = acts[n]
            files = action_form_fields(n, a)
            for k, v in fields.items():
                files[k] = (None, v)
            headers = {"Origin": config.SITE_ORIGIN,
                       "Referer": f"{config.SITE_ORIGIN}{path}",
                       "Accept": "text/html"}
            used = f"nojs(action_{n})"
        else:
            aid = FALLBACK_SETUP_ACTIONS.get(base)
            if not aid:
                raise TypeSafeError(f"{path} 既没有 $ACTION_* 隐藏域，也没有降级 action id")
            files = {f"_1_{k}": (None, v) for k, v in fields.items()}
            files["0"] = (None, '[{},"$K1"]')
            headers = {"Origin": config.SITE_ORIGIN,
                       "Referer": f"{config.SITE_ORIGIN}{path}",
                       "Accept": "text/x-component",
                       "next-action": aid}
            used = f"next-action({aid[:12]}…)"

        r = self.s.post(f"{config.SITE_ORIGIN}{path}", files=files, headers=headers, timeout=40)
        redirect = r.headers.get("x-action-redirect", "")
        ok = r.status_code == 200 and "Internal Server Error" not in r.text[:200]
        self.log.append(f"post_setup({path}) via {used} HTTP {r.status_code} -> {redirect}")
        # 失败时把**真因**写进 error。只回 "HTTP 404" 会把排查引向"站点挂了"，
        # 而真相通常是"页面没渲染出隐藏域 ⇒ 退化到已作废的 id"。
        err = "" if ok else f"HTTP {r.status_code}"
        if not ok and fetch_err:
            err += (f" —— 且本次走的是**降级通路**（未抓到 $ACTION_* 隐藏域：{fetch_err}）；"
                    f"该通路的 action id 为录制值，站点改版后必失效。"
                    f"下一步：跑 tools/probes/probe_onboarding.py 看页面实际渲染形态")
        return Result(ok=ok, stage=f"setup:{base}", status=r.status_code,
                      data={"redirect": redirect, "via": used, "fetch_error": fetch_err},
                      error=err)

    def onboarding_gate(self) -> str:
        """站点把我们挡在 onboarding 的**哪一步**？

        返回值三态（调用方**必须**区分，否则会静默放行）：
            `""`        —— `/hook` 200，onboarding 已通过
            `"<slug>"`  —— `/setup/<slug>`，当前该做这一步（含我们不认识的步骤）
            `"!<码> <loc>"` —— 跳到非 `/setup/*` 的地方（多半是会话失效）

        🔴 判据必须来自**站点的重定向**，不能来自 `/api/me` 的字段。

        历史教训（2026-09-21 实测，误报率 19/25 = 76%）：
        旧实现用 `/api/me` 的 `console_survey_completed_at` 判断"要不要跑 survey"。
        可站点**已经把 survey 这一步删掉了**（onboarding 从三步变两步），
        该字段于是永远为 `None` ⇒ 每次都多发一次 survey POST ⇒
        而那时页面渲染的是欢迎页、**没有 `$ACTION_*` 隐藏域**
        ⇒ 退化到已作废的 fallback ⇒ 404
        ⇒ **账号明明已经完全 onboard，却被记成 `partial`**。

        ⇒ 改成"问站点下一步是什么"：`GET /hook` 不跟随重定向，看它把我们送去哪。
          站点增删步骤时本方法**自动适应**，不必跟着改字段名。
          实测门禁链（2026-09-21）：
              未接受 ToS     → `/hook` 307 → `/setup/tos?returnTo=…`
              接受 ToS 后    → `/hook` 307 → `/setup/set-name?returnTo=…`
              完成 set-name  → `/hook` 200（欢迎页，链路打通）
        """
        r = self.s.get(f"{config.SITE_ORIGIN}/hook", allow_redirects=False, timeout=30)
        if r.status_code == 200:
            return ""
        loc = r.headers.get("location") or ""
        m = SETUP_PATH_RE.search(loc)
        if m:
            return m.group(1)
        # 🔴 不是 `/setup/*` 的跳转（例如会话失效被送回 `/login`）。
        # 旧实现这里 `return ""` ⇒ 被上游当成"onboarding 已通过"而放行，
        # 属于**静默放行**：门禁明明没过，却继续去建 key。
        # 加 `!` 前缀是刻意的——它保证不会与任何合法步骤名相撞。
        return f"!{r.status_code} {loc}".strip()

    def complete_onboarding(self, display_name: str = "Auto User") -> Result:
        """尽力推进 onboarding；**`ok=False` 不等于"这个账号废了"**。

        🔴 判据换过三次，每次的教训都留着（这是本项目最容易判错的一处）：

        | 版本 | 判据 | 怎么错的 |
        |---|---|---|
        | 旧 | `/api/me` 的 `console_survey_completed_at` | 视图有滞后、字段语义会变；站点一改步骤就永真 ⇒ 多发一次 POST ⇒ 404 ⇒ 假 `partial` |
        | 中 | `GET /hook` 重定向归零 | **站点自己就不稳定**：同一次运行里相邻两次 GET 给出不同答案（实测 `set-name` 与 200 交替），拿它当判据必然空转 |
        | **今** | **能不能建出 key**（见 `stage_create_key`） | —— |

        2026-09-21 实测（`tools/probes/probe_gate_chain.py`）：
          · 门禁链是**三步** `tos → set-name → console-survey`，
            而 `console-survey` 那一页**没有 `$ACTION_*` 隐藏域**（渲染的是
            "Get started / Let's create your org" 向导），我们无法自动提交；
          · 就在这个"门禁未归零"的状态下，`POST /api/api-keys` 返回
            **200 + 明文 key** ⇒ `/hook` 的重定向是**引导**，不是硬门槛。

        ⇒ 本方法只做两件事：① 把**我们认识的**步骤各提交一次；② 如实报告
          门禁序列与结果。**它不再有权判"账号失败"** —— 那个判断属于建 key 那一步。
          调用方（`stages.stage_create_key`）据此决定是继续还是收工。

        另外两条防御：
          · 同一跳**只提交一次**（`attempted`）—— 门禁非确定性时防空转；
          · 遇到不认识的步骤**停下并记录**，不判失败、也不硬猜字段。
        """
        gates: list[str] = []
        submitted: list[tuple[str, bool]] = []
        via: list[str] = []
        last_err = ""
        attempted: set[str] = set()
        stopped = f"连推 {MAX_ONBOARDING_STEPS} 步仍未归零"

        for _ in range(MAX_ONBOARDING_STEPS):
            gate = self.onboarding_gate()
            gates.append(gate or "(已通过)")
            if not gate:
                return self._onboarding_result(True, submitted, gates, via, "")
            if gate.startswith("!"):
                # 门禁不可识别（会话失效 / 站点换了跳转目标）——不许当成"已通过"
                return self._onboarding_result(
                    False, submitted, gates, via,
                    f"onboarding 门禁不可识别：{gate}"
                    f"（会话失效？站点改了跳转目标？）")
            if gate not in KNOWN_ONBOARDING_GATES:
                stopped = (f"遇到不认识的引导步骤 {gate!r}（站点新增？"
                           f"我们无法自动提交它，但它不影响建 key，故不判失败；"
                           f"要看它的页面形态跑 tools/probes/probe_gate_chain.py）")
                break
            if gate in attempted:
                # 门禁**非确定性**：同一跳又出现了 ⇒ 再提交也是白费。
                # 若上一跳是 HTTP 失败，如实报出来（这才是真失败）。
                why = (f"提交 {gate} 失败且门禁原地不动：{last_err}"
                       if last_err else f"门禁非确定性：{gate!r} 重复出现")
                stopped = why
                break
            attempted.add(gate)
            if gate == "tos":
                r = self.post_setup("/setup/tos?returnTo=%2Fhook", {
                    "legalAcknowledged": "true",
                    "returnTo": "/hook",
                    "marketingOptIn": "true",
                    "marketingOptedOutInitial": "",
                })
            elif gate == "set-name":
                r = self.post_setup("/setup/set-name?returnTo=%2Fhook", {
                    "returnTo": "/hook",
                    "accountEmail": self.email,
                    "displayName": display_name,
                    "jobFunction": "",
                    "skip": "true",
                })
            submitted.append((gate, r.ok))
            via.append(f"{gate}:via={r.data.get('via', '?')},HTTP {r.status}")
            if not r.ok:
                last_err = r.error
        return self._onboarding_result(False, submitted, gates, via, stopped)

    @staticmethod
    def _onboarding_result(ok: bool, submitted: list[tuple[str, bool]],
                           gates: list[str], via: list[str],
                           stopped: str) -> Result:
        """把"提交了什么 / 门禁怎么变"拼成一份可诊断的报告。

        `completed` 里带 `（上一步已顺带完成）` 标记的项 = POST 报了错、但门禁
        确实往前走了（站点"提交一次顺带完成多步"）。这个标记必须保留：
        它和"真失败"在台账里长得一样，丢了就分不出两者。
        """
        completed = [f"{g}（上一步已顺带完成）" if not was_ok else g
                     for g, was_ok in submitted]
        data = {"completed": completed, "gates": gates, "via": via}
        if not ok:
            data["stopped"] = stopped
        return Result(ok=ok, stage="onboarding",
                      error="" if ok else stopped,
                      data=data)

    def onboarding_state(self) -> dict[str, Any]:
        """`/api/me` 的快照 —— **仅供诊断留痕，不是任何判据**。

        它已经被换掉两次判据了（`console_survey_completed_at` 不可信，
        `/hook` 重定向也不可信，见 `complete_onboarding` 的表）。
        留下的理由：`needs_*` 对"为什么某一步没生效"有解释力
        （例如 set-name 提交后 `human_name` 仍为空 —— 那是 `skip=true` 的正常结果，
        不是失败）。生产代码里已无人读它，只有 `tools/probes/` 在读。
        """
        prof = self.me() or {}
        name = (prof.get("human_name") or "").strip()
        tos = (prof.get("latest_tos_acceptance") or {}).get("tos_version")
        survey = prof.get("console_survey_completed_at")
        return {"profile": prof, "needs_tos": tos is None,
                "needs_name": not name, "needs_survey": survey is None}

    # ── 7. API Key ────────────────────────────────────────────────────
    def list_api_keys(self) -> list[dict[str, Any]]:
        r = self.s.get(f"{config.SITE_ORIGIN}/api/api-keys",
                       headers={"Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            raise TypeSafeError(f"列 key 失败 HTTP {r.status_code}: {r.text[:200]}",
                                status=r.status_code)
        return (r.json() or {}).get("api_keys") or []

    def create_api_key(self, name: str = "1") -> dict[str, Any]:
        r = self.s.post(f"{config.SITE_ORIGIN}/api/api-keys", json={"name": name},
                        headers={"Origin": config.SITE_ORIGIN,
                                 "Referer": f"{config.SITE_ORIGIN}/keys",
                                 "Accept": "application/json"}, timeout=40)
        if r.status_code != 200:
            raise TypeSafeError(f"建 key 失败 HTTP {r.status_code}: {r.text[:300]}",
                                status=r.status_code)
        data = r.json() or {}
        if not data.get("api_key"):
            raise TypeSafeError(f"建 key 响应里没有明文 api_key: {str(data)[:200]}")
        return data
