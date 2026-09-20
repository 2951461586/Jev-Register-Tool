"""端到端编排：申请 → 确认邮件 → 注册 → 创建 API Key → 入库。

阶段与可自动化程度（实测结论，不是推断）：

    1. apply       建临时邮箱 + 提交 Framer waitlist 表单          ✅ 全自动
    2. confirm     收 "You're on the waitlist" 确认邮件            ✅ 全自动
    3. approved    收 "Your account is ready"（邀请获批）           ❌ **人工/批量审批**
    4. login       /login 发码 → 收码 → POST /api/auth/callback     ✅ 全自动（但需 3 已过）
    5. onboarding  /setup/tos → set-name → console-survey          ✅ 全自动（但需 3 已过）
    6. api_key     POST /api/api-keys                              ✅ 全自动（但需 3 已过）
    7. store       写入 JSONL 台账                                  ✅ 全自动

第 3 阶段是本链路的**唯一外部阻断点**：TypeSafe 是邀请制，未被邀请的邮箱在第 4 阶段
拿到 `403 {"error":"Access restricted"}`（前端文案 "TypeSafe is currently invite-only"）。
这个门槛在服务端，客户端无法绕过。因此：

- `--stage apply`   只跑到第 2 阶段，用来批量投递申请
- `--stage resume`  对**已获批**的邮箱跑 4→7
- 默认全链路        跑到第 3 阶段停住，如实报告 `blocked: invite_only`
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import config
from .framer_waitlist import submit as framer_submit
from .ledger import Ledger
from .mailrules import any_of, get as get_rule
from .tempemail import Mail, TempMailClient, TempMailError
from .typesafe import (MODE_CODE, MODE_LINK, Result, TypeSafeClient, TypeSafeError)

# 邮件匹配：**统一走 `mailrules` 的规则表**，不再在这里散落 subject 子串。
#
# 🔴 为什么必须按规则表来（实测教训）：
#   "申请确认"与"获批"这两封的信封发件人**完全相同**
#   （都是 `envelope.updates.typesafe.ai`），只能靠主题区分。
#   早期只按 subject 匹配是能跑的，但一旦有人想"按发件人过滤一下更稳"，
#   就会把未获批的账号当成已获批去跑注册段 —— 拿到 403 还以为是白名单问题。
#   规则表把 sender + subject 一起钉死，并留了 `diagnose()` 报漏网主题。
MATCH_WAITLIST_CONFIRM = get_rule("waitlist_confirm")
MATCH_ACCOUNT_READY = get_rule("account_ready")
MATCH_CODE = any_of("signin_code", "verify_code")
MATCH_LINK = any_of("welcome_confirm", "signin_link")

STATUS_ORDER = ["applied", "confirmed", "approved", "registered", "keyed", "failed"]


@dataclass
class AccountRecord:
    key: str
    email: str
    status: str = "applied"
    error: str = ""
    created_at: float = field(default_factory=time.time)
    stages: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    api_key: str = ""
    api_key_id: str = ""
    waitlist: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "email": self.email, "status": self.status,
            "error": self.error, "created_at": self.created_at,
            "stages": self.stages, "timings": self.timings,
            "api_key": self.api_key, "api_key_id": self.api_key_id,
            "waitlist": self.waitlist, "user": self.user,
        }


class Pipeline:
    def __init__(self, *, mail: TempMailClient | None = None,
                 ledger: Ledger | None = None, domain: str | None = None,
                 login_mode: str = MODE_CODE, verbose: bool = True):
        self.mail = mail or TempMailClient()
        self.ledger = ledger or Ledger(config.LEDGER_PATH)
        self.domain = domain or config.TEMPMAIL_DOMAIN
        self.login_mode = login_mode
        self.verbose = verbose
        self.client: TypeSafeClient | None = None

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # ── 阶段 1+2：申请 + 确认邮件 ─────────────────────────────────────
    def stage_apply(self, rec: AccountRecord, *, confirm_timeout: float = 180.0) -> bool:
        t0 = time.time()
        if not rec.email:
            rec.email = self.mail.create_mailbox(self.domain)
            rec.key = rec.email
        self.log(f"  [apply] 邮箱 {rec.email}")
        since = int(time.time() * 1000) - 5_000

        sub = framer_submit(rec.email)
        rec.waitlist["framer_status"] = sub["status"]
        rec.stages["apply"] = "ok" if sub["ok"] else "failed"
        if not sub["ok"]:
            rec.error = f"framer submit HTTP {sub['status']}: {sub['body'][:160]}"
            rec.status = "failed"
            return False

        mail = self.mail.wait_for_mail(rec.email, MATCH_WAITLIST_CONFIRM,
                                       timeout=confirm_timeout, interval=2.0, since_ms=since)
        rec.timings["apply"] = time.time() - t0
        if mail is None:
            rec.stages["confirm"] = "failed"
            rec.error = (f"未在 {confirm_timeout:.0f}s 内收到 waitlist 确认邮件"
                         f"（邮箱接口轮询 {self.mail.stats.polls} 次，5xx {self.mail.stats.http_5xx} 次）")
            rec.status = "failed"
            return False
        rec.stages["confirm"] = "ok"
        rec.waitlist["confirm_subject"] = mail.subject
        rec.waitlist["confirm_at"] = mail.received_at
        rec.status = "confirmed"
        self.log(f"  [confirm] {mail.subject}  ({rec.timings['apply']:.1f}s)")
        return True

    # ── 阶段 3：等审批 ────────────────────────────────────────────────
    def stage_wait_approval(self, rec: AccountRecord, *, timeout: float = 0.0) -> bool:
        """等 "Your account is ready"。

        timeout<=0 表示**不等待**，只做一次快照检查 —— 因为审批是人工/批量，
        等下去没有意义（实测申请后 30 分钟仍未获批）。
        """
        mail = self.mail.wait_for_mail(
            rec.email, MATCH_ACCOUNT_READY,
            timeout=timeout, interval=max(5.0, timeout / 20 if timeout else 5.0),
            since_ms=int(rec.created_at * 1000) - 5_000) if timeout > 0 else \
            self.mail.first_mail_matching(rec.email, MATCH_ACCOUNT_READY)

        if mail is None:
            rec.stages["approved"] = "pending"
            rec.error = "invite_only: 尚未获批（TypeSafe 邀请制，需等待人工/批量审批）"
            self.log("  [approved] 未获批 —— TypeSafe 邀请制，链路由此外部阻断")
            return False
        rec.stages["approved"] = "ok"
        rec.waitlist["ready_subject"] = mail.subject
        rec.waitlist["ready_at"] = mail.received_at
        rec.status = "approved"
        self.log(f"  [approved] {mail.subject}")
        return True

    # ── 阶段 4：注册（登录 + 认证回调） ───────────────────────────────
    def stage_login(self, rec: AccountRecord, *, mail_timeout: float = 120.0) -> bool:
        t0 = time.time()
        cl = TypeSafeClient()
        since = int(time.time() * 1000) - 5_000
        try:
            r = cl.send_login_email(rec.email, mode=self.login_mode)
        except TypeSafeError as exc:
            rec.stages["login"] = "failed"
            rec.error = f"发码失败: {exc}"
            rec.status = "failed"
            return False
        if not r.ok:
            rec.stages["login"] = "failed"
            rec.error = f"发码 HTTP {r.status}: {r.data.get('page_text', '')[:120]}"
            rec.status = "failed"
            return False

        if self.login_mode == MODE_LINK:
            m = self.mail.wait_for_mail(rec.email, MATCH_LINK, timeout=mail_timeout,
                                        interval=2.0, since_ms=since)
            if m is None:
                rec.stages["login"] = "failed"
                rec.error = "未收到魔法链接邮件"
                rec.status = "failed"
                return False
            link = re.search(r"https://login\.typesafe\.ai/v1/magic_links/redirect\?[^\s\"<>\)\]]+",
                             m.body)
            if not link:
                rec.stages["login"] = "failed"
                rec.error = "魔法链接邮件里没找到链接"
                rec.status = "failed"
                return False
            try:
                red = cl.exchange_magic_link(link.group(0))
                token = cl.token_from_redirect_url(red)
            except TypeSafeError as exc:
                rec.stages["login"] = "failed"
                rec.error = f"魔法链接交换失败: {exc}"
                rec.status = "failed"
                return False
            res = cl.auth_callback(token, "magic_links", rec.email)
        else:
            m = self.mail.wait_for_mail(rec.email, MATCH_CODE, timeout=mail_timeout,
                                        interval=2.0, since_ms=since)
            if m is None:
                rec.stages["login"] = "failed"
                rec.error = "未收到验证码邮件"
                rec.status = "failed"
                return False
            codes = re.findall(r"\b\d{6}\b", m.body)
            if not codes:
                rec.stages["login"] = "failed"
                rec.error = "验证码邮件里没找到 6 位码"
                rec.status = "failed"
                return False
            res = cl.auth_callback(codes[0], "otp", rec.email)

        rec.timings["login"] = time.time() - t0
        if not res.ok:
            rec.stages["login"] = "failed"
            code = str(res.data.get("error") or res.data.get("code") or "")
            if "Access restricted" in code:
                rec.error = "invite_only: 403 Access restricted（该邮箱未被邀请）"
                rec.status = "failed"
                rec.stages["approved"] = "pending"
            else:
                rec.error = f"认证回调 HTTP {res.status}: {code}"
                rec.status = "failed"
            return False

        rec.stages["login"] = "ok"
        rec.status = "registered"
        self.client = cl
        rec.user["profile"] = cl.me() or {}
        self.log(f"  [login] 会话建立 ({rec.timings['login']:.1f}s)")
        return True

    # ── 阶段 5+6：onboarding + 建 key ─────────────────────────────────
    def stage_create_key(self, rec: AccountRecord, cl: TypeSafeClient,
                         *, name: str = "1") -> bool:
        t0 = time.time()
        try:
            ob = cl.complete_onboarding(display_name=rec.email.split("@")[0][:24])
            if not ob.ok:
                rec.stages["onboarding"] = "failed"
                rec.error = f"onboarding 失败: {ob.error or ob.data}"
                rec.status = "partial"
                return False
            rec.stages["onboarding"] = "ok"
            rec.user["onboarding"] = ob.data.get("completed", [])

            key = cl.create_api_key(name)
        except TypeSafeError as exc:
            rec.stages["api_key"] = "failed"
            rec.error = f"建 key 失败: {exc}"
            rec.status = "partial"
            return False

        rec.timings["create_key"] = time.time() - t0
        rec.api_key = key.get("api_key", "")
        rec.api_key_id = key.get("id", "")
        rec.stages["api_key"] = "ok"
        rec.status = "keyed"
        rec.error = ""
        self.log(f"  [api_key] {rec.api_key[:24]}…  ({rec.timings['create_key']:.1f}s)")
        return True

    # ── 全链路 ────────────────────────────────────────────────────────
    def run_one(self, *, email: str = "", mode: str = "full",
                approval_timeout: float = 0.0,
                name: str = "1") -> AccountRecord:
        rec = AccountRecord(key=email, email=email)
        try:
            if mode in ("full", "apply"):
                if not self.stage_apply(rec):
                    return rec
                if mode == "apply":
                    return rec
            if not self.stage_wait_approval(rec, timeout=approval_timeout):
                return rec
            if not self.stage_login(rec):
                return rec
            assert self.client is not None
            self.stage_create_key(rec, self.client, name=name)
        except TempMailError as exc:
            rec.status = "failed"
            rec.error = f"邮箱服务异常: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 兜底，保证台账一定写得进去
            rec.status = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"
        return rec

    def run_batch(self, *, count: int = 1, mode: str = "full",
                  approval_timeout: float = 0.0, name: str = "1") -> list[AccountRecord]:
        out: list[AccountRecord] = []
        for i in range(count):
            self.log(f"[{i + 1}/{count}] 开始")
            rec = self.run_one(mode=mode, approval_timeout=approval_timeout, name=name)
            self.ledger.append(rec.to_dict())
            self.log(f"[{i + 1}/{count}] status={rec.status} {rec.error}")
            out.append(rec)
        return out

    # ── 人工接力：用外部提供的验证码/魔法链接 token 直接领号 ──────────
    def stage_login_with_token(self, rec: AccountRecord, token: str,
                               kind: str = "otp") -> bool:
        """跳过"读邮箱"，直接用外部给的 token 走第 4 步。

        用途：获批邮箱是**真人邮箱**（Worker 读不到）时，由人把验证码/链接粘过来。
        """
        t0 = time.time()
        cl = TypeSafeClient()
        res = cl.auth_callback(token, kind, rec.email)
        rec.timings["login"] = time.time() - t0
        if not res.ok:
            rec.stages["login"] = "failed"
            code = str(res.data.get("error") or res.data.get("code") or "")
            if "Access restricted" in code:
                rec.error = "invite_only: 403 Access restricted（该邮箱未被邀请）"
                rec.stages["approved"] = "pending"
            elif res.status == 401:
                rec.error = (f"token 无效/过期: 401 {code}"
                             f"（验证码 10 分钟且一次性，请重新发码后立刻提交）")
            else:
                rec.error = f"认证回调 HTTP {res.status}: {code}"
            rec.status = "failed"
            return False
        rec.stages["login"] = "ok"
        rec.status = "registered"
        self.client = cl
        rec.user["profile"] = cl.me() or {}
        self.log(f"  [login] 会话建立 ({rec.timings['login']:.1f}s)")
        return True

    def claim(self, email: str, token: str, *, kind: str = "otp",
              name: str = "1", send_first: bool = False) -> AccountRecord:
        """对单个**已获批**邮箱完成 4→7。

        send_first=True 时先触发一次发码（发到该邮箱），调用方拿到码后再用
        token 调一次（不带 send_first）。这样把"发码"和"提交"解耦，
        便于人工在两个动作之间去邮箱里取码。
        """
        rec = AccountRecord(key=email, email=email)
        if send_first:
            try:
                r = TypeSafeClient().send_login_email(
                    email, mode=MODE_CODE if kind == "otp" else MODE_LINK)
            except TypeSafeError as exc:
                rec.status = "failed"
                rec.error = f"发码失败: {exc}"
                self.ledger.append(rec.to_dict())
                return rec
            if not r.ok:
                rec.status = "failed"
                rec.error = f"发码 HTTP {r.status}"
                self.ledger.append(rec.to_dict())
                return rec
            rec.stages["send_code"] = "ok"
            self.log(f"  [send-code] 已向 {email} 发出"
                     f"{'6 位验证码' if kind == 'otp' else '魔法链接'}（10 分钟有效）")
            if not token:
                rec.status = "code_sent"
                self.ledger.append(rec.to_dict())
                return rec
        if not token:
            rec.status = "failed"
            rec.error = "缺少 token（验证码或魔法链接 token）"
            self.ledger.append(rec.to_dict())
            return rec
        if self.stage_login_with_token(rec, token, kind=kind):
            assert self.client is not None
            self.stage_create_key(rec, self.client, name=name)
        self.ledger.append(rec.to_dict())
        return rec

    def resume(self, emails: list[str], *, name: str = "1") -> list[AccountRecord]:
        """对已获批的邮箱跑 4→7，全程零注册请求。"""
        out: list[AccountRecord] = []
        for email in emails:
            rec = AccountRecord(key=email, email=email)
            self.log(f"[resume] {email}")
            if self.stage_login(rec):
                assert self.client is not None
                self.stage_create_key(rec, self.client, name=name)
            self.ledger.append(rec.to_dict())
            self.log(f"[resume] status={rec.status} {rec.error}")
            out.append(rec)
        return out

    # ── 监听：获批即自动续跑 ──────────────────────────────────────────
    def watch(self, *, timeout: float = 600.0, interval: float = 15.0,
              name: str = "1", known: list[str] | None = None) -> list[AccountRecord]:
        """轮询邮箱池，一旦出现"获批"邮件就立刻对该地址跑 4→7。

        **不依赖台账里的地址**：直接扫 Worker 窗口内**所有**邮件，
        这样即使申请是在别处（网页 UI）提交的，也能接上。
        """
        deadline = time.time() + timeout
        watched = set(known or [])
        watched |= {r["email"] for r in self.ledger.load() if r.get("email")}
        done: set[str] = set()
        results: list[AccountRecord] = []
        self.log(f"[watch] 监听 {timeout:.0f}s，间隔 {interval:.0f}s，"
                 f"已知候选 {len(watched)} 个地址")

        round_no = 0
        while time.time() < deadline:
            round_no += 1
            try:
                all_msgs = self.mail.scan_all()
            except TempMailError as exc:
                self.log(f"[watch] #{round_no} 读邮箱失败：{exc}")
                time.sleep(interval)
                continue

            ready = [m for m in all_msgs if MATCH_ACCOUNT_READY(m)]
            if ready:
                self.log(f"[watch] #{round_no} 命中 {len(ready)} 封获批邮件")
            for m in ready:
                addr = m.recipient
                if addr in done:
                    continue
                done.add(addr)
                self.log(f"[watch] ★ 获批：{addr} —— 立刻续跑 4→7")
                rec = AccountRecord(key=addr, email=addr)
                rec.waitlist["ready_subject"] = m.subject
                rec.waitlist["ready_at"] = m.received_at
                rec.status = "approved"
                rec.stages["approved"] = "ok"
                if self.stage_login(rec):
                    assert self.client is not None
                    self.stage_create_key(rec, self.client, name=name)
                self.ledger.append(rec.to_dict())
                results.append(rec)
                self.log(f"[watch] {addr} -> status={rec.status} {rec.error}")

            if not ready:
                self.log(f"[watch] #{round_no} 窗口 {len(all_msgs)} 封，暂无获批邮件"
                         f"（剩 {max(0, deadline - time.time()):.0f}s）")
            time.sleep(interval)
        return results
