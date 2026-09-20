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

**已知门槛**：TypeSafe 是邀请制。未被邀请的邮箱在第 4 步返回
`403 {"error":"Access restricted"}`，前端文案为
"Sorry, TypeSafe is currently invite-only"。这一步无法绕过。
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config

ACTION_GOOGLE = "4"
ACTION_LINK = "2"      # "Continue" -> 魔法链接
ACTION_CODE = "3"      # "Email me a code instead" -> 6 位验证码

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


#: 页面里渲染的 Server Action 隐藏域：`name="$ACTION_<n>:<idx>" value="…"`。
#:
#: 🔴 **不要把 `<n>` 的集合写死。** 以前是 `for n in ("2", "3", "4")`，并且要求
#: `:0` 与 `:2` **同时存在**。2026-09-20 站点改版后 `/setup/*` 渲染的是索引 **1**、
#: 且**只有 `:0` 和 `:1`**（另加一个 `$ACTION_KEY`）—— 两个条件都不满足，
#: 于是 `acts` 恒为空 ⇒ 退化到 `FALLBACK_SETUP_ACTIONS` 里陈旧的 action id
#: ⇒ POST 404 ⇒ 最终只看到 `onboarding 失败: HTTP 404`，
#: 跟"索引集合被写死了"毫无字面关联。
_ACTION_FIELD_RE = re.compile(r'name="\$ACTION_(\d+):(\d+)"\s+value="([^"]*)"')
_ACTION_KEY_RE = re.compile(r'name="\$ACTION_KEY"\s+value="([^"]*)"')


def _actions_from_html(page: str) -> dict[str, dict[str, Any]]:
    """抓出页面渲染的 Server Action 隐藏域，**原样**留着待回填。

    返回 `{n: {"id": …, "bound": …, "fields": {"0": …, "1": …}, "key": …}}`。

    只要求 `:0` 存在（它带 action id）；`:1` / `:2` 有就收、没有就不发 ——
    浏览器提交表单时也只回填页面上真实存在的隐藏域。
    """
    fields: dict[str, dict[str, str]] = {}
    for n, idx, val in _ACTION_FIELD_RE.findall(page):
        fields.setdefault(n, {})[idx] = html_mod.unescape(val)

    km = _ACTION_KEY_RE.search(page)
    key = html_mod.unescape(km.group(1)) if km else ""

    out: dict[str, dict[str, Any]] = {}
    for n, vals in fields.items():
        if "0" not in vals:
            continue                      # 没有 :0 就没有 action id，这条用不了
        try:
            ref = json.loads(vals["0"])
        except ValueError:
            continue                      # 不是 JSON ⇒ 不是 Server Action 引用
        out[n] = {"id": ref.get("id", ""), "bound": ref.get("bound", "$@1"),
                  "fields": vals, "key": key}
    return out


def _action_form_fields(n: str, a: dict[str, Any]) -> dict[str, tuple]:
    """把 `_actions_from_html` 的产物还原成"要提交的隐藏域"（multipart 元组形式）。

    `:0` 走 `_compact_ref()` 重新序列化 —— 保证紧凑 JSON（带空格会被 Next.js 判 500）。
    """
    parts: dict[str, tuple] = {f"$ACTION_REF_{n}": (None, "")}
    for idx, val in sorted(a["fields"].items()):
        parts[f"$ACTION_{n}:{idx}"] = (None, _compact_ref(a) if idx == "0" else val)
    if a.get("key"):
        parts["$ACTION_KEY"] = (None, a["key"])
    return parts


def _compact_ref(a: dict[str, str]) -> str:
    """还原 `$ACTION_<n>:0` 的取值。

    🔴 **必须是紧凑 JSON，不能带空格。** 页面里渲染的是
    `{"id":"60e492...","bound":"$@1"}`，而 `json.dumps` 默认分隔符是
    `", "` / `": "`，会产出 `{"id": "60e492...", "bound": "$@1"}`。
    服务端对此**不做容错**，直接抛 `500 Internal Server Error` ——
    报的是"服务器内部错误"，跟"多了一个空格"看起来毫无关系。
    实测：紧凑 200 / 带空格 500，唯一变量就是这个字符串。
    """
    return json.dumps({"id": a["id"], "bound": a["bound"]},
                      separators=(",", ":"), ensure_ascii=False)


def _parse_js_object(text: str) -> dict[str, str]:
    """解析 JS 对象字面量（键**没有**引号，所以不是 JSON）。

    Stytch 的落地页里是 `xhr.send(JSON.stringify({ public_token: '...', ... }))` ——
    单引号字符串 + 裸键名。`json.loads` 会报
    `Expecting property name enclosed in double quotes`，别把它当 JSON 解。
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*'((?:[^'\\]|\\.)*)'", text):
        out[m.group(1)] = m.group(2).replace("\\'", "'").replace("\\\\", "\\")
    return out


def _visible_text(page: str) -> str:
    t = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(html_mod.unescape(t).split())


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
    def fetch_actions(self, email: str) -> dict[str, dict[str, str]]:
        r = self.s.get(config.SITE_LOGIN, params={"waitlist": email}, timeout=30)
        if r.status_code != 200:
            raise TypeSafeError(f"GET /login -> HTTP {r.status_code}", status=r.status_code)
        acts = _actions_from_html(r.text)
        if ACTION_LINK not in acts or ACTION_CODE not in acts:
            raise TypeSafeError(
                f"/login 页未渲染出预期的 Server Action（拿到 {sorted(acts)}）"
                " —— 页面结构可能变了，或该 URL 被改成了无表单版本"
            )
        return acts

    # ── 2. 触发发信 ───────────────────────────────────────────────────
    def send_login_email(self, email: str, mode: str = MODE_CODE) -> Result:
        """提交登录表单，触发 Stytch 发信。返回页面的可见文案。"""
        n = ACTION_LINK if mode == MODE_LINK else ACTION_CODE
        acts = self.fetch_actions(email)
        a = acts[n]
        files = _action_form_fields(n, a)
        files["email"] = (None, email)
        r = self.s.post(config.SITE_LOGIN, params={"waitlist": email}, files=files,
                        headers=self._login_headers(email), timeout=40)
        txt = _visible_text(r.text)
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
        payload = _parse_js_object(m.group(1))
        if "public_token" not in payload or "redirect_url" not in payload:
            raise TypeSafeError(f"dfp 参数解析不完整: {sorted(payload)}")
        payload["telemetry_id"] = ""      # 浏览器侧是 Promise.race(5s) 兜底成 ''
        r = self.s.post("https://login.typesafe.ai/v1/magic_links/redirect/dfp",
                        json=payload,
                        headers={"Accept": "application/json",
                                 "Content-Type": "application/json;charset=UTF-8",
                                 "Origin": "https://login.typesafe.ai",
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
        acts = _actions_from_html(r.text)
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
            files = _action_form_fields(n, a)
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
