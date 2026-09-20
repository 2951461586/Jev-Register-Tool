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

- `--mode apply`    只跑到第 2 阶段，用来批量投递申请
- `--mode resume`   对**已获批**的邮箱跑 4→7
- 默认全链路        跑到第 3 阶段停住，如实报告 `invite_only`

并发的边界（`--concurrency`）
──────────────────────────
申请段与注册段**每个账号只读自己的收件箱索引端点**，彼此独立，可以并发。
`watch` 读的是**全表共享窗口**（Worker retention 只有 100 行），并发读只会互相挤，
必须保持串行 —— 见 `watch()` 的说明。

⚠️ 并发的前置条件是"**不共享可变状态**"：本模块以前把会话 client 挂在
`self.client` 上（串行看不出问题，并发会**串号** —— A 账号的 api_key 建在 B 的会话上）。
现在 `stage_login()` 改为**返回** client，`run_batch`/`resume` 每个 worker 用独立的
`Pipeline` 实例，唯一共享的是带锁的 `Ledger`。
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config
from .framer_waitlist import submit as framer_submit
from .ledger import Ledger
from .mailrules import any_of, extract_otp, get as get_rule
from .tempemail import TempMailClient, TempMailError
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

# 本模块写入的 `status` 字面量（applied / confirmed / approved / registered /
# keyed / partial / code_sent / failed）必须在 `ledger.RANK` 里有登记 ——
# 台账的升级/降级判断依赖它。以前这里有个 `STATUS_ORDER` 列表，但全项目零引用
# （真正的等级表在 ledger.RANK），已于 2026-09-20 审计后删除。
# 自测 `test_status_vocabulary` 会用 AST 扫本文件，漏登记新状态会直接失败。

#: 并发时多线程会同时 print，不加锁会在一行中间交错，日志直接没法读。
_LOG_LOCK = threading.Lock()


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
        # 注意：这里**不输出** `last_error` —— 那个字段是台账合并时按需加的
        # （只在"降级"时写入）。如果这里输出一个空的 `last_error`，
        # 每次写入都会把之前记下的降级原因清掉。
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

    def log(self, msg: str) -> None:
        if self.verbose:
            with _LOG_LOCK:
                print(msg, flush=True)

    # ── 失败登记 ──────────────────────────────────────────────────────
    @staticmethod
    def _fail(rec: AccountRecord, stage: str, msg: str, *,
              status: str | None = "failed", mark: str = "failed") -> bool:
        """登记一次阶段失败，并返回 `False`（供 `return self._fail(...)` 用）。

        🔴 为什么集中在一个地方：这三件事必须**同时**发生 ——
        写 `stages[stage]`、写 `rec.error`、改 `rec.status`。
        以前是 11 处人肉复制粘贴，漏一处就会出现"有 error 但 stages 显示 ok"
        这种自相矛盾的台账记录，而且只在特定分支上出现，很难发现。

        `status=None` 表示**不改** status（例如"尚未获批"不该把 confirmed 打成 failed）。
        """
        rec.stages[stage] = mark
        rec.error = msg
        if status is not None:
            rec.status = status
        return False

    def _fail_auth(self, rec: AccountRecord, res: Result, *, hint: str = "") -> bool:
        """认证回调失败的**错误码分流** —— 业务核心，三个码不能混。

        | 响应 | 含义 | 处置 |
        |---|---|---|
        | `401 Code expired` | OTP 错/过期（凭据校验在**前**，不看邮箱） | 重新发码，10 分钟内提交 |
        | `401 Authentication failed` | 魔法链接 token **已被用过**（一次性） | 换一封邮件里的链接 |
        | `403 Access restricted` | 凭据有效，但**不在白名单** | 等获批，别改请求 |

        混掉的代价：把"取码 bug"当成"邀请制拦截"（或反过来），
        会把排查引向完全错误的方向 —— 这两条路的处置**恰好相反**。
        `tools/selftest.py::test_auth_error_triage` 用真实响应体钉住了这三条。
        """
        code = str(res.data.get("error") or res.data.get("code") or "")
        suffix = f" {hint}" if hint else ""
        if "Access restricted" in code:
            # 凭据是对的，只是没被邀请 —— 这是业务门槛，不是技术故障
            rec.stages["approved"] = "pending"
            return self._fail(rec, "login",
                              "invite_only: 403 Access restricted（该邮箱未被邀请）")
        if res.status == 401 and "Code expired" in code:
            return self._fail(
                rec, "login",
                f"认证回调 HTTP 401: {code}（验证码错/过期，需重新发码）{suffix}")
        if res.status == 401 and "Authentication failed" in code:
            return self._fail(
                rec, "login",
                f"认证回调 HTTP 401: {code}（token 已被使用，链接一次性）{suffix}")
        return self._fail(rec, "login", f"认证回调 HTTP {res.status}: {code}{suffix}")

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
        if not sub["ok"]:
            return self._fail(rec, "apply",
                              f"framer submit HTTP {sub['status']}: {sub['body'][:160]}")
        rec.stages["apply"] = "ok"

        mail = self.mail.wait_for_mail(rec.email, MATCH_WAITLIST_CONFIRM,
                                       timeout=confirm_timeout, interval=2.0, since_ms=since)
        rec.timings["apply"] = time.time() - t0
        if mail is None:
            return self._fail(
                rec, "confirm",
                f"未在 {confirm_timeout:.0f}s 内收到 waitlist 确认邮件"
                f"（邮箱接口轮询 {self.mail.stats.polls} 次，5xx {self.mail.stats.http_5xx} 次）")
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
            self.log("  [approved] 未获批 —— TypeSafe 邀请制，链路由此外部阻断")
            # 注意 status=None：**不把 status 打成 failed**。
            # 没获批不等于这次执行失败，把 confirmed 降级会污染台账统计。
            return self._fail(
                rec, "approved",
                "invite_only: 尚未获批（TypeSafe 邀请制，需等待人工/批量审批）",
                status=None, mark="pending")
        rec.stages["approved"] = "ok"
        rec.waitlist["ready_subject"] = mail.subject
        rec.waitlist["ready_at"] = mail.received_at
        rec.status = "approved"
        self.log(f"  [approved] {mail.subject}")
        return True

    # ── 阶段 4：注册（登录 + 认证回调） ───────────────────────────────
    def stage_login(self, rec: AccountRecord, *,
                    mail_timeout: float = 120.0) -> TypeSafeClient | None:
        """发码 → 收码 → 认证回调。成功时**返回**已建立会话的 client。

        🔴 返回 client 而不是挂 `self.client`：挂实例字段在串行时看不出问题，
        但并发时会**串号**（A 账号的 api_key 建在 B 账号的会话上）。
        返回值传递是开并发的前置条件。
        """
        t0 = time.time()
        cl = TypeSafeClient()
        since = int(time.time() * 1000) - 5_000
        try:
            r = cl.send_login_email(rec.email, mode=self.login_mode)
        except TypeSafeError as exc:
            self._fail(rec, "login", f"发码失败: {exc}")
            return None
        if not r.ok:
            self._fail(rec, "login",
                       f"发码 HTTP {r.status}: {r.data.get('page_text', '')[:120]}")
            return None

        if self.login_mode == MODE_LINK:
            m = self.mail.wait_for_mail(rec.email, MATCH_LINK, timeout=mail_timeout,
                                        interval=2.0, since_ms=since)
            if m is None:
                self._fail(rec, "login", "未收到魔法链接邮件")
                return None
            link = re.search(r"https://login\.typesafe\.ai/v1/magic_links/redirect\?[^\s\"<>\)\]]+",
                             m.body)
            if not link:
                self._fail(rec, "login", "魔法链接邮件里没找到链接")
                return None
            try:
                red = cl.exchange_magic_link(link.group(0))
                token = cl.token_from_redirect_url(red)
            except TypeSafeError as exc:
                self._fail(rec, "login", f"魔法链接交换失败: {exc}")
                return None
            res = cl.auth_callback(token, "magic_links", rec.email)
        else:
            m = self.mail.wait_for_mail(rec.email, MATCH_CODE, timeout=mail_timeout,
                                        interval=2.0, since_ms=since)
            if m is None:
                self._fail(rec, "login", "未收到验证码邮件")
                return None
            token, how = extract_otp(m.body)
            if not token:
                self._fail(rec, "login", "验证码邮件里没找到 6 位码")
                return None
            if how != "anchored":
                # 走了降级 = 回到了"可能抽到报文头里的 MTA 标识"那个老坑
                # （服务端曾抽到 `MTA74-AB1`）。必须能从日志里看出来，
                # 否则"站点改了邮件模板"会伪装成"验证码过期"，把人引向重新发码。
                self.log(f"  [login] ⚠ 取码走了**降级**路径（模板锚定失配）"
                         f"主题={m.subject!r} —— 站点可能改了邮件模板，"
                         f"建议跑 --mode scan 看漏网主题")
            res = cl.auth_callback(token, "otp", rec.email)

        rec.timings["login"] = time.time() - t0
        if not res.ok:
            self._fail_auth(rec, res)
            return None

        rec.stages["login"] = "ok"
        rec.status = "registered"
        rec.user["profile"] = cl.me() or {}
        self.log(f"  [login] 会话建立 ({rec.timings['login']:.1f}s)")
        return cl

    def stage_login_with_token(self, rec: AccountRecord, token: str,
                               kind: str = "otp") -> TypeSafeClient | None:
        """跳过"读邮箱"，直接用外部给的 token 走第 4 步。

        用途：获批邮箱是**真人邮箱**（Worker 读不到）时，由人把验证码/链接粘过来。
        """
        t0 = time.time()
        cl = TypeSafeClient()
        res = cl.auth_callback(token, kind, rec.email)
        rec.timings["login"] = time.time() - t0
        if not res.ok:
            self._fail_auth(rec, res,
                            hint="（验证码 10 分钟且一次性，请重新发码后立刻提交）")
            return None
        rec.stages["login"] = "ok"
        rec.status = "registered"
        rec.user["profile"] = cl.me() or {}
        self.log(f"  [login] 会话建立 ({rec.timings['login']:.1f}s)")
        return cl

    # ── 阶段 5+6：onboarding + 建 key ─────────────────────────────────
    def stage_create_key(self, rec: AccountRecord, cl: TypeSafeClient,
                         *, name: str = "1") -> bool:
        t0 = time.time()
        try:
            ob = cl.complete_onboarding(display_name=rec.email.split("@")[0][:24])
            if not ob.ok:
                return self._fail(rec, "onboarding",
                                  f"onboarding 失败: {ob.error or ob.data}",
                                  status="partial")
            rec.stages["onboarding"] = "ok"
            rec.user["onboarding"] = ob.data.get("completed", [])

            key = cl.create_api_key(name)
        except TypeSafeError as exc:
            return self._fail(rec, "api_key", f"建 key 失败: {exc}", status="partial")

        rec.timings["create_key"] = time.time() - t0
        rec.api_key = key.get("api_key", "")
        rec.api_key_id = key.get("id", "")
        rec.stages["api_key"] = "ok"
        rec.status = "keyed"
        rec.error = ""
        self.log(f"  [api_key] {rec.api_key[:24]}…  ({rec.timings['create_key']:.1f}s)")
        return True

    # ── 并发脚手架 ────────────────────────────────────────────────────
    def _clone(self) -> "Pipeline":
        """给一个并发 worker 用的**独立**实例。

        独立是硬要求，不是优化：`TempMailClient` 持有 `requests.Session`
        （不保证线程安全），且 `stats` 计数器会被多线程搅乱。
        唯一共享的是 `Ledger` —— 它的 `append` 有锁。
        """
        return Pipeline(ledger=self.ledger, domain=self.domain,
                        login_mode=self.login_mode, verbose=self.verbose)

    def _fan_out(self, jobs: list[tuple[Any, Callable[["Pipeline", Any], AccountRecord]]],
                 *, concurrency: int) -> list[AccountRecord]:
        """按并发度跑一批任务，返回顺序与传入一致。

        `concurrency <= 1` 时走**原来的串行路径**，行为与加并发前逐字一致
        （这是刻意的：不并发的人不该承担并发的复杂度）。
        """
        concurrency = max(1, int(concurrency))
        if concurrency <= 1:
            return [fn(self, key) for key, fn in jobs]

        out: list[AccountRecord | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {pool.submit(fn, self._clone(), key): pos
                    for pos, (key, fn) in enumerate(jobs)}
            for fut in as_completed(futs):
                try:
                    out[futs[fut]] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    # 单个 worker 炸了不该带走整批；但要留下痕迹，
                    # 不能让"少了一个账号"变成静默事件。
                    self.log(f"✗ 并发任务异常: {type(exc).__name__}: {exc}")
        return [r for r in out if r is not None]

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
            cl = self.stage_login(rec)
            if cl is None:
                return rec
            self.stage_create_key(rec, cl, name=name)
        except TempMailError as exc:
            rec.status = "failed"
            rec.error = f"邮箱服务异常: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 兜底，保证台账一定写得进去
            rec.status = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"
        return rec

    def run_batch(self, *, count: int = 1, mode: str = "full",
                  approval_timeout: float = 0.0, name: str = "1",
                  concurrency: int = 1) -> list[AccountRecord]:
        def job(pipe: "Pipeline", i: int) -> AccountRecord:
            pipe.log(f"[{i + 1}/{count}] 开始")
            rec = pipe.run_one(mode=mode, approval_timeout=approval_timeout, name=name)
            pipe.ledger.append(rec.to_dict())
            pipe.log(f"[{i + 1}/{count}] status={rec.status} {rec.error}")
            return rec

        return self._fan_out([(i, job) for i in range(count)], concurrency=concurrency)

    # ── 人工接力：用外部提供的验证码/魔法链接 token 直接领号 ──────────
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
                self._fail(rec, "send_code", f"发码失败: {exc}")
                self.ledger.append(rec.to_dict())
                return rec
            if not r.ok:
                self._fail(rec, "send_code", f"发码 HTTP {r.status}")
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
            self._fail(rec, "login", "缺少 token（验证码或魔法链接 token）")
            self.ledger.append(rec.to_dict())
            return rec
        cl = self.stage_login_with_token(rec, token, kind=kind)
        if cl is not None:
            self.stage_create_key(rec, cl, name=name)
        self.ledger.append(rec.to_dict())
        return rec

    def resume(self, emails: list[str], *, name: str = "1",
               concurrency: int = 1) -> list[AccountRecord]:
        """对已获批的邮箱跑 4→7，全程零申请请求。"""

        def job(pipe: "Pipeline", email: str) -> AccountRecord:
            rec = AccountRecord(key=email, email=email)
            pipe.log(f"[resume] {email}")
            cl = pipe.stage_login(rec)
            if cl is not None:
                pipe.stage_create_key(rec, cl, name=name)
            pipe.ledger.append(rec.to_dict())
            pipe.log(f"[resume] status={rec.status} {rec.error}")
            return rec

        return self._fan_out([(e, job) for e in emails], concurrency=concurrency)

    # ── 监听：获批即自动续跑 ──────────────────────────────────────────
    def watch(self, *, timeout: float = 600.0, interval: float = 15.0,
              name: str = "1", known: list[str] | None = None) -> list[AccountRecord]:
        """轮询邮箱池，一旦出现"获批"邮件就立刻对该地址跑 4→7。

        **不依赖台账里的地址**：直接扫 Worker 窗口内**所有**邮件，
        这样即使申请是在别处（网页 UI）提交的，也能接上。

        🔴 **本方法是刻意串行的，不要给它加并发。**
        它读的是**全表共享窗口**（`scan_all` → `/admin/all`，retention 只有 100 行，
        且被同机邻居项目刷屏）。并发读不会更快，只会互相抢同一批行，
        还会放大 D1 读配额消耗。要提速就缩短 `interval`，不是加线程。
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
                cl = self.stage_login(rec)
                if cl is not None:
                    self.stage_create_key(rec, cl, name=name)
                self.ledger.append(rec.to_dict())
                results.append(rec)
                self.log(f"[watch] {addr} -> status={rec.status} {rec.error}")

            if not ready:
                self.log(f"[watch] #{round_no} 窗口 {len(all_msgs)} 封，暂无获批邮件"
                         f"（剩 {max(0, deadline - time.time()):.0f}s）")
            time.sleep(interval)
        return results
