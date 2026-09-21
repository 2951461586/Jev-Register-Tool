"""TypeSafe 控制台客户端。

链路（全部为纯 HTTP，无需浏览器）：

    1. GET  /login?waitlist=<email>              -> 从 HTML 里抓 3 个 Server Action
    2a. POST /login?waitlist=<email>  ACTION_2   -> 魔法链接邮件（Stytch magic_links）
    2b. POST /login?waitlist=<email>  ACTION_3   -> 6 位验证码邮件（Stytch otp）
    3.  (仅魔法链接) GET  login.typesafe.ai/v1/magic_links/redirect?...  -> 拿到 dfp 交换参数
        POST login.typesafe.ai/v1/magic_links/redirect/dfp            -> 拿 Stytch session token
    4. POST /api/auth/callback  {token, tokenType, waitlistEmail}    -> 建立控制台会话
    5. GET  /api/me                                                  -> 判断是否需要 onboarding
    6. POST /setup/tos, /setup/set-name, /setup/console-survey        -> 完成 onboarding
    7. POST /api/api-keys  {name}                                    -> 拿到明文 api_key

**Server Action 的处理方式**：不硬编码 action id。
`/login` 页把 `$ACTION_<n>:0/1/2` 三个隐藏域直接渲染在 HTML 里（React 的渐进增强路径），
其中 `:2` 是服务端加密的 bound args，必须**原样回传**。每次 GET 都会变，
所以每次提交前都要重新抓一遍 —— 这样也顺带免疫了部署换 hash。

**解析层不在这里**：`$ACTION_*` 隐藏域、JS 对象字面量、可见文案、魔法链接的抠取
全部在 `src/parsing.py` —— 纯函数、零第三方依赖，可以脱离 `requests` 单独测。
本模块只负责"发请求 / 判断状态码 / 组装 Result"。

**已知门槛**：TypeSafe 是邀请制。未被邀请的邮箱在第 4 步返回
`403 {"error":"Access restricted"}`，前端文案为
"Sorry, TypeSafe is currently invite-only"。这一步无法绕过。
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


# /setup/* 页面的 Server Action ID，取自录制 HAR（部署 id 787c0047… 与当时一致）。
# 仅作为**降级**用：首选仍是运行时从页面 HTML 抓 `$ACTION_*` 隐藏域。
FALLBACK_SETUP_ACTIONS = {
    "/setup/tos": "6016020c2c1d719d5d6a7c8f6ab594c8de691556c2",
    "/setup/set-name": "60ec39e20e3115ab87f21a60e7f7cfe7169dada4ef",
    "/setup/console-survey": "60f5cf7bebef1c5652bc5e860c24de2872de20d39e",
}


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
    def _login_url(self, email: str) -> str:
        return f"{config.SITE_LOGIN}?waitlist={requests.utils.quote(email, safe='')}"

    def _login_headers(self, email: str) -> dict[str, str]:
        return {
            "Origin": config.SITE_ORIGIN,
            "Referer": self._login_url(email),
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

    def fetch_actions(self, email: str) -> dict[str, dict[str, str]]:
        last: tuple[str, ...] = ()
        for i in range(self.ACTION_FETCH_ATTEMPTS):
            r = self.s.get(config.SITE_LOGIN, params={"waitlist": email}, timeout=30)
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
        """提交登录表单，触发 Stytch 发信。返回页面的可见文案。"""
        n = ACTION_LINK if mode == MODE_LINK else ACTION_CODE
        acts = self.fetch_actions(email)
        a = acts[n]
        files = action_form_fields(n, a)
        files["email"] = (None, email)
        r = self.s.post(config.SITE_LOGIN, params={"waitlist": email}, files=files,
                        headers=self._login_headers(email), timeout=40)
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
        payload = {
            "token": token,
            "tokenType": token_type,
            "returnTo": None,
            "preferredOrgId": None,
            "inviteId": None,
            "oauthState": None,
            "waitlistEmail": email,
        }
        r = self.s.post(f"{config.SITE_ORIGIN}/api/auth/callback", json=payload,
                        headers={"Origin": config.SITE_ORIGIN,
                                 "Referer": self._login_url(email),
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
        self.log.append(f"auth_callback({token_type}) HTTP {r.status_code} {code}")
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

        A. **渐进增强形态**：页面里带 `$ACTION_*` 隐藏域 → 用无 JS 表单提交。
           与 /login 完全同构，已被实测验证。
        B. **带 JS 形态**（降级）：`next-action: <id>` 头 + multipart，
           字段名加 `_1_` 前缀，另带 `0 = [{},"$K1"]`。action id 取
           `FALLBACK_SETUP_ACTIONS`（来自录制 HAR，部署 id 相同）。
        """
        base = path.split("?", 1)[0]
        try:
            acts = self.fetch_setup_actions(path)
        except TypeSafeError:
            acts = {}

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
        return Result(ok=ok, stage=f"setup:{base}", status=r.status_code,
                      data={"redirect": redirect, "via": used},
                      error="" if ok else f"HTTP {r.status_code}")

    def complete_onboarding(self, display_name: str = "Auto User") -> Result:
        """按 /api/me 的缺口依次补 TOS / 姓名 / 问卷。"""
        st = self.onboarding_state()
        done: list[str] = []
        if st["needs_tos"]:
            r = self.post_setup("/setup/tos?returnTo=%2Fhook", {
                "legalAcknowledged": "true",
                "returnTo": "/hook",
                "marketingOptIn": "true",
                "marketingOptedOutInitial": "",
            })
            if not r.ok:
                return r
            done.append("tos")
        st = self.onboarding_state()
        if st["needs_name"]:
            r = self.post_setup("/setup/set-name?returnTo=%2Fhook", {
                "returnTo": "/hook",
                "accountEmail": self.email,
                "displayName": display_name,
                "jobFunction": "",
                "skip": "true",
            })
            if not r.ok:
                return r
            done.append("set-name")
        st = self.onboarding_state()
        if st["needs_survey"]:
            org = ""
            for m in (st["profile"].get("org_memberships") or []):
                org = (m.get("org") or {}).get("id", "") or org
            r = self.post_setup("/setup/console-survey?returnTo=%2Fhook", {
                "returnTo": "/hook",
                "needsOrgFields": "false",
                "orgSurveyOrgId": org,
                "needsUserFields": "true",
                "skip": "true",
            })
            if not r.ok:
                return r
            done.append("console-survey")
        return Result(ok=True, stage="onboarding", data={"completed": done,
                                                        "state": self.onboarding_state()})

    def onboarding_state(self) -> dict[str, Any]:
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
