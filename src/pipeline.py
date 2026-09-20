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

两份台账（2026-09-20 起）
──────────────────────
    exports/ledger.jsonl    运行台账：**全部尝试**（含失败的），用于复盘
    result/success.jsonl    成功数据：**只记拿到 key 的**，是交付物

成功那份在 `stage_create_key()` 里写 —— 那是**唯一**产出 key 的地方，
挂在那里就自动覆盖了全部四条路径（`run_batch` / `resume` / `watch` / `claim`）。
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

#: 码模式等不到 6 位码后，回捞魔法链接的**额外**等待秒数。
#:
#: 这个回捞不是"再等等看"，而是应对站点对同一次发码请求回了链接形态的凭据
#: （2026-09-20 实测：同一账号 4 次发码里 1 次回的是 "Sign in to TypeSafe"）。
#: 链接和码是同一次 SMTP 投递，通常已经在窗口里，所以给 30s 足够；
#: 给太长只会让未获批的账号在每一次重跑里多白等几十秒。
LINK_FALLBACK_TIMEOUT = 30.0

#: 等 "You're on the waitlist" 确认邮件的秒数。
#:
#: 🔴 **不要调回 180s。** 2026-09-20 实测：连跑 10 批次时确认邮件的到达延迟单调爬升
#: （41.7 / 52.9 / 52.8 / 43.9 / 68.6 / 86.7 / 93.6 / 192 / 198 s），阈值 180s 会把
#: 后两条**误判成 `failed`** —— 而它们其实在超时后 12s / 18s 就落了库。
#: 误判的代价不是多等一会儿，是台账里多两条"失败"记录、运营者据此重投申请。
#: 300s ≈ 健康期实测最大延迟的 1.5 倍。
#:
#: ⚠️ 但**阈值只是"本次不等了"，不是"这封邮件不会来了"**。另有两例（09:01 / 09:04
#: 提交）的确认邮件到 09:21:28 才入库（延迟 ~20 分钟）——那段时间正好横跨共享 Worker
#: 的故障恢复窗口（09:19:29 才恢复落库），所以这 20 分钟**更可能是故障期积压/重投**，
#: 不是常态。两例后来都正常获批了，说明**确认邮件没收到不影响审批**。
#: ⇒ 见到 confirm 超时，先回查收件箱再决定要不要重投，别直接当失败。
CONFIRM_TIMEOUT = 300.0

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

    def __post_init__(self) -> None:
        """把 `key` / `email` 的**首尾空白**去掉 —— 这是唯一真源，台账的键就是它。

        🔴 为什么必须在这里做（2026-09-20 实测事故）：邮箱是从 CLI 批量传进来的
        （`--email a@b.com --email ...`），而候选清单文件在 Windows 上很容易是
        **CRLF**（`io.open(..., "w")` 默认会把 `\\n` 翻成 `\\r\\n`）。
        `mapfile -t` 只吃掉 `\\n`，于是每个邮箱尾部带着 `\\r`。

        站点侧会 trim 掉它、照常发码建 key（所以**不报错**），但台账的 `key`
        是原始字符串 ⇒ 同一账号被写成 `x@y.com` 和 `x@y.com\\r` **两条**：
        交付数字虚高、`load()` 的"按邮箱去重"也失效。属于典型的
        "不报错的静默数据损坏"，只能在写入边界堵死。
        """
        self.key = str(self.key or "").strip()
        self.email = str(self.email or "").strip()

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
                 ledger: Ledger | None = None,
                 success_ledger: Ledger | None = None,
                 domain: str | None = None,
                 login_mode: str = MODE_CODE, verbose: bool = True):
        self.mail = mail or TempMailClient()
        self.ledger = ledger or Ledger(config.LEDGER_PATH)
        # 成功数据单独落一份到 `result/`（**交付物**），与 `exports/` 的运行台账分开：
        # 台账要留全部历史（含失败的，便于复盘），交付物只该有成功的。
        self.success_ledger = success_ledger if success_ledger is not None \
            else Ledger(config.SUCCESS_LEDGER_PATH)
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
    def stage_apply(self, rec: AccountRecord, *,
                    confirm_timeout: float = CONFIRM_TIMEOUT) -> bool:
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
        # `apply` 只算**提交表单**这一段。以前它把下面等邮件的时间也吞进去，
        # 于是报告里出现 "阶段分解: apply 181.13s" —— 读起来像表单提交花了 3 分钟，
        # 实际那 181s 全是等邮件。两个计时器不重叠，`sum(timings.values())` 才有意义。
        rec.timings["apply"] = time.time() - t0

        t1 = time.time()
        mail = self.mail.wait_for_mail(rec.email, MATCH_WAITLIST_CONFIRM,
                                       timeout=confirm_timeout, interval=2.0, since_ms=since)
        rec.timings["confirm_wait"] = time.time() - t1
        if mail is None:
            # 🔴 status 是 `applied`（申请已投递）而**不是** `failed`。
            # 表单已经 201 接受，申请已经注册到站点侧；没收到回执 ≠ 申请没成功。
            # 2026-09-20 实证：5 个"确认邮件超时"的账号**后来全部获批**，
            # 其中一个的确认邮件至今从未到达 ⇒ 回执与申请注册是**两件独立的事**。
            # 记成 `failed` 会让这些账号从"待复查"清单里消失（与 P0 同一类错误：
            # 不报错，只是少几行）。`applied` 在 ledger.RANK 里是 1 分，
            # 低于 confirmed(2) / approved(3) / keyed(5)，所以后续仍会被正确升级。
            return self._fail(
                rec, "confirm",
                f"未在 {confirm_timeout:.0f}s 内收到 waitlist 确认邮件"
                f"（邮箱接口轮询 {self.mail.stats.polls} 次，5xx {self.mail.stats.http_5xx} 次）"
                f" —— 表单已 201 接受，申请已注册；这只是**回执未到**，不是申请失败"
                f"（把回执超时当成申请失败就是误判；回查收件箱可确认迟到，勿重投）",
                status="applied")
        rec.stages["confirm"] = "ok"
        rec.waitlist["confirm_subject"] = mail.subject
        rec.waitlist["confirm_at"] = mail.received_at
        rec.status = "confirmed"
        self.log(f"  [confirm] {mail.subject}  ({rec.timings['confirm_wait']:.1f}s)")
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
            link_fallback = False
            if m is None:
                # 🔴 2026-09-20 实测：站点对**同一次**发码请求可能回魔法链接
                # （主题 "Sign in to TypeSafe"）而不是 6 位码——同一账号 4 次发码里
                # 就有 1 次是链接。此时继续等码必然超时；若直接判
                # "未收到验证码邮件"，排查会被引向"D1 窗口被挤爆 / 邮箱坏了"，
                # 而真相只是凭据形态换了一种。先回捞一次链接再判死。
                m = self.mail.wait_for_mail(rec.email, MATCH_LINK,
                                            timeout=LINK_FALLBACK_TIMEOUT,
                                            interval=2.0, since_ms=since)
                if m is None:
                    self._fail(rec, "login", "未收到验证码邮件")
                    return None
                link_fallback = True
                self.log(f"  [login] ⚠ 码模式未收到 6 位码，但收到魔法链接"
                         f"（主题={m.subject!r}）—— 改走链接交换回捞")

            if link_fallback:
                link = re.search(
                    r"https://login\.typesafe\.ai/v1/magic_links/redirect\?[^\s\"<>\)\]]+",
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
        # 🔴 这里是**唯一**产出 key 的地方 ⇒ 成功数据挂在这里写，
        # 就自动覆盖了全部四条路径（run_batch / resume / watch / claim），
        # 不需要在四个调用点各写一遍（那种写法迟早漏一处）。
        self.success_ledger.append(rec.to_dict())
        self.log(f"  [api_key] {rec.api_key[:24]}…  ({rec.timings['create_key']:.1f}s)")
        return True

    # ── 并发脚手架 ────────────────────────────────────────────────────
    def _clone(self) -> "Pipeline":
        """给一个并发 worker 用的**独立**实例。

        独立是硬要求，不是优化：`TempMailClient` 持有 `requests.Session`
        （不保证线程安全），且 `stats` 计数器会被多线程搅乱。
        唯一共享的是 `Ledger`（运行台账与成功台账）—— 它的读写都有锁。
        """
        return Pipeline(ledger=self.ledger, success_ledger=self.success_ledger,
                        domain=self.domain, login_mode=self.login_mode,
                        verbose=self.verbose)

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
                confirm_timeout: float = CONFIRM_TIMEOUT,
                name: str = "1") -> AccountRecord:
        rec = AccountRecord(key=email, email=email)
        try:
            if mode in ("full", "apply"):
                if not self.stage_apply(rec, confirm_timeout=confirm_timeout):
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
                  approval_timeout: float = 0.0,
                  confirm_timeout: float = CONFIRM_TIMEOUT,
                  name: str = "1",
                  concurrency: int = 1) -> list[AccountRecord]:
        def job(pipe: "Pipeline", i: int) -> AccountRecord:
            pipe.log(f"[{i + 1}/{count}] 开始")
            rec = pipe.run_one(mode=mode, approval_timeout=approval_timeout,
                               confirm_timeout=confirm_timeout, name=name)
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
        known_recs = self.ledger.load()
        watched = set(known or [])
        watched |= {r["email"] for r in known_recs if r.get("email")}
        # 🔴 已经有 key 的地址直接跳过。`watch` 读的是**全表窗口**，旧批次的
        # 获批邮件会在窗口里停留很久（窗口只受 100 行条数限制），重复处理
        # 会给同一账号**造出第二把 key** —— 两把在服务端都有效，但
        # `Ledger.load()` 按邮箱去重、末行胜出，交付物里就会少一把。
        have_key = {r["email"] for r in known_recs if r.get("api_key")}
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
                if addr in have_key:
                    self.log(f"[watch] 跳过 {addr}（台账里已有 api_key，不重复建）")
                    done.add(addr)
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
