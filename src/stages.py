"""阶段层：单个账号从"注册"到"拿到 Key"的**全部出网动作**。

与 `runner.py` 的分工（2026-09-20 二轮审计 ⑪）：

    runner.py   调度：批量 / 并发 / 台账写入
    stages.py   单账号：signup → login → onboarding → api_key

2026-09-21：链路从 6 段缩到 4 段（邀请制取消）
─────────────────────────────────────────────
旧链路（已删除的 3 段用 ~~划掉~~）：

    ~~apply~~       建临时邮箱 + 提交 Framer waitlist 表单
    ~~confirm~~     收 "You're on the waitlist" 回执
    ~~approved~~    等 "Your account is ready"（**唯一外部阻断点**）
    login          /login 发信 → 收凭据 → POST /api/auth/callback
    onboarding     /setup/tos → /setup/set-name（站点侧串行门禁）
    api_key        POST /api/api-keys

TypeSafe 取消邀请制后，`/login` 提交邮箱**直接**发确认邮件，点链接即建会话
⇒ 前 3 段整体消失，`stage_login` 现在是**唯一入口**（邮箱为空时它自己建一个）。
实测判据（2026-09-21，两次独立跑批）：
  · `POST /login` 回 `x-action-redirect: /login?sent=true&email=…`
  · 收到 `Welcome to TypeSafe — confirm your email`，含 7 天有效的魔法链接
  · `POST /api/auth/callback` 直接 200 —— **不再有 `403 Access restricted`**
  · 接受 ToS 后即可 `POST /api/api-keys` 拿到 key

⚠️ `403 Access restricted` 的分流**保留**（见 `_fail_auth`）：它不再是常态，
   但站点随时可能恢复白名单，删掉这条分流会让"没被邀请"伪装成"凭据错误"。

🔴 为什么是 `StageMixin` 而不是"纯函数"
────────────────────────────────────────
报告 §8.1 原计划把阶段做成 `apply(rec, mail, ...)` 这类自由函数（理由是
"无实例状态 ⇒ 可直接单测"）。落地时**没有照做**，因为阶段方法实际读 4 个
实例字段：`self.mail` / `self.login_mode` / `self.success_ledger` / `self.log`。
改成自由函数必须引入一个 ctx 参数对象把它们打包传进去 —— 那是**另一档改动**
（生产调用点 9 处 + 自测 14 处），换来的收益却只是"不造 Pipeline 实例就能调"，
而现行自测早已通过 `Pipeline` 实例覆盖了全部阶段（161 项）。

`StageMixin` 用**零缩进改动**达成模块级切分，因此可以做字节级等价验证。

⚠️ 依赖方向：`stages` **绝不** import `runner`。`AccountRecord` 放在本模块
（而不是 `runner`）就是为了让方向保持单向 —— 由 `runner` 导入本模块。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .mailrules import (CODE_RULES, LINK_RULES, any_of, extract_otp,
                        get as get_rule)
from .parsing import extract_magic_links
# ⚠️ `MODE_CODE` **不在**这里 —— 它只用于 `runner.Pipeline.__init__` 的默认参数。
#    import 一个本模块用不到的名字，死符号扫描会报，且会误导读者以为阶段层要看它。
from .typesafe import MODE_LINK, Result, TypeSafeClient, TypeSafeError

# 邮件匹配：**统一走 `mailrules` 的规则表**，不再在这里散落 subject 子串。
#
# 🔴 为什么必须按规则表来（实测教训，长期有效）：
#   历史上"申请确认"与"获批"这两封的信封发件人**完全相同**，
#   只能靠主题区分。早期只按 subject 匹配是能跑的，但一旦有人想
#   "按发件人过滤一下更稳"，就会把未获批的账号当成已获批去跑注册段。
#   规则表把 sender + subject 一起钉死，并留了 `diagnose()` 报漏网主题。
#   现在只剩一类流量，但"主题是必需判别位"这条结论不变。
# 🔴 分组取自 `mailrules` 的常量，**不要在这里按规则名重写一遍**。
# 以前这里是 `any_of("signin_code", "verify_code")` —— 与 `CODE_RULES` 表达同一件事
# 的两份定义，加新规则时会改一边漏一边（详见 `mailrules.any_of` 的说明）。
MATCH_CODE = any_of(*CODE_RULES)
MATCH_LINK = any_of(*LINK_RULES)
#: 码模式等不到 6 位码后，回捞魔法链接的**额外**等待秒数。
#:
#: 这个回捞不是"再等等看"，而是应对站点对同一次发码请求回了链接形态的凭据
#: （2026-09-20 实测：同一账号 4 次发码里 1 次回的是 "Sign in to TypeSafe"）。
#: 链接和码是同一次 SMTP 投递，通常已经在窗口里，所以给 30s 足够；
#: 给太长只会让失败的账号在每一次重跑里多白等几十秒。
LINK_FALLBACK_TIMEOUT = 30.0
#: 等确认邮件（魔法链接）的秒数。
#:
#: 🔴 **2026-09-21 从 300s 下调到 60s** —— 依据是**实测延迟分布**，不是拍脑袋。
#:
#: 43 个成功账号实测（`signup.mail_at ÷ 1000 − created_at`，50 批次跑批）：
#:     min 2.66s · P50 3.26s · P90 3.80s · max 4.57s
#: 60s 对实测最大延迟仍有 **13 倍**余量。
#:
#: ⚠️ **旧值 300s 的两条依据都已失效**（留档，避免有人再引回去）：
#:   ① 旧 docstring 引的是"2026-09-20 实测延迟单调爬升到 198s" —— 该结论**当天就被
#:      证伪**（`memory/2026-09-20.md`：那批是**批量发出**的 waitlist 回执，
#:      一次性涌入 47 封、延迟约 25 分钟，被误读成"单调爬升"）；
#:   ② 那条链路（等 waitlist 审批回执）**已随邀请制取消整体删除**，
#:      现在等的是**实时**登录确认邮件（3s 级）。
#: ⇒ 阈值是"针对某个延迟分布"的；分布变了，它就该变。
#:
#: ⚠️ **下调必须与"超时后批内重发"成对使用**（见 `RETRY_SEND_ON_TIMEOUT`）：
#: 只降阈值不重发，偶发的慢邮件会被直接判死，成功率**反而下降**。
#: ⚠️ 另注：站点发信存在**丢包**（2026-09-21 实测 12 次发码只到 1 封），
#: 但魔法链接 **7 天有效** ⇒ 超时后重跑时，`relogin_pending` 会把手上的历史链接
#: 也用上（它不设 `since_ms` 门槛），所以"这次没等到"不等于"这个账号废了"。
MAIL_TIMEOUT = 60.0

#: 确认邮件超时后，是否在**同一批次内**重发一次再等一轮。
#:
#: 依据：正常邮件 3s 内必到（P90 3.80s），所以"等到 60s 还没有"几乎必然是
#: **丢包**而非"慢"。重发一次的成本是一次 `POST /login`，而收益是把成功率
#: 从 ~86% 拉到 95%+ —— 比让账号落到下一轮 `relogin_pending` 划算得多。
#:
#: ⚠️ 它和 `MAIL_TIMEOUT` 是**一个改动**，不是两个：只降阈值不重发，
#: 会把偶发的慢邮件直接判死，成功率反而下降。
RETRY_SEND_ON_TIMEOUT = True
#: 重发次数上限。刻意只给 1 次 —— 站点对"短时间重复发信"的容忍度未知，
#: 而且真有第二次丢包时，让账号落到 `relogin_pending`（不设 `since_ms`，
#: 能复用历史链接）比在批内死磕更有效。
MAX_SEND_RETRIES = 1
# 本模块写入的 `status` 字面量（registered / keyed / partial / failed）必须在
# `ledger.RANK` 里有登记 —— 台账的升级/降级判断依赖它。
# 自测 `test_status_vocabulary` 会用 AST 扫本文件，漏登记新状态会直接失败。
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
    #: 注册段的元数据（收到的邮件主题 / 到达时间等）。
    #: 2026-09-21 由 `waitlist` 改名 —— 那个名字属于"等审批"的旧链路，
    #: 而字段里存的东西（确认邮件主题、到达时间）本身是有诊断价值的，
    #: 所以改名保留而不是删除。历史台账里的 `waitlist` 键由
    #: `ledger.DICT_FIELDS` 继续兜住（合并时不会丢）。
    signup: dict[str, Any] = field(default_factory=dict)
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
            "signup": self.signup, "user": self.user,
        }


class StageMixin:
    """阶段方法集合。**只能**被 `runner.Pipeline` 继承 —— 它依赖宿主提供的
    `self.mail` / `self.log` / `self.login_mode` / `self.success_ledger`。

    ⚠️ 本模块里的 `TypeSafeClient` 是 **patch 接缝**：
    `tools/tests/support.py::offline()` 会替换 `src.stages` 命名空间里的这个名字。
    因此这里必须是 `from .typesafe import TypeSafeClient` 的**模块级名字**、
    且在**调用时**通过模块 globals 解析 —— 写成 `typesafe.TypeSafeClient()`
    或 `import src.typesafe` 都会让 patch 静默落空（自测会全线失败）。

    （`framer_submit` 曾是本文件第二个接缝，2026-09-21 随申请段一起删除。
      `support.offline()` 的接缝守卫会 `assert` 覆盖集，少一个立刻炸。）
    """
    # ── 失败登记 ──────────────────────────────────────────────────────
    def _mail_timeout_msg(self, mail_timeout: float, *, code_first: bool) -> str:
        """等确认邮件超时的**诊断文案** —— 两种登录模式共用一份。

        🔴 为什么文案本身要当代码写：超时在旧链路里的处置是"改请求/换模式"，
        在新链路里是"**重跑**"（魔法链接 7 天有效，重跑会连历史链接一起用）。
        只写一句"没收到"会让运维按老经验去改请求，白跑一轮。

        轮询计数（`polls` / `http_5xx`）的作用是把"**读不出来**"（邮箱接口
        在报 5xx、轮询次数很高）与"**站点没发**"（接口正常、计数正常）分开 ——
        前者要修 Worker，后者只需重跑，两条路的处置不同。
        """
        polls, bad = self.mail.stats.polls, self.mail.stats.http_5xx
        what = ("未在 %.0fs 内收到验证码邮件，回捞魔法链接 %s 也没等到"
                % (mail_timeout, LINK_FALLBACK_TIMEOUT)) if code_first else \
               f"未在 {mail_timeout:.0f}s 内收到确认邮件"
        return (f"{what}（邮箱接口轮询 {polls} 次，5xx {bad} 次）—— "
                f"站点发信存在丢包，但魔法链接 7 天有效："
                f"重跑一次（relogin_pending 会连历史链接一起用）比改请求更有效")

    def _wait_with_retry(self, rec: AccountRecord, cl: TypeSafeClient,
                         match: Callable[[Any], bool], *,
                         mail_timeout: float, since_ms: int,
                         allow_retry: bool = True) -> Any:
        """等一封满足条件的邮件；超时后按 `RETRY_SEND_ON_TIMEOUT` **批内重发**再等。

        返回 `Mail`，或 `None`（等不到，且重发也没救回来）。

        `allow_retry=False` 用于码模式的**回捞链接**那一步：它是在**同一次**
        发信周期内换凭据形态（站点把码回成了链接），不是"丢包"，
        再发一次信既无必要、又白白增加站点压力。

        🔴 **计时成功失败都写**（`rec.timings["mail_wait"]`，**累加** ——
        码模式会调它两次）。旧实现在失败路径上完全不留耗时：50 批次复跑里
        6 个超时账号各白等 300s、占总墙钟 58%，而台账里连"等了多久"都查不到，
        只能靠 `mail_at − created_at` 反推。**任何"等待"都要留痕，成功失败都要留。**
        """
        t_wait = time.time()
        tries = 0
        while True:
            m = self.mail.wait_for_mail(rec.email, match, timeout=mail_timeout,
                                        interval=2.0, since_ms=since_ms)
            if m is not None:
                break
            if (not allow_retry or not RETRY_SEND_ON_TIMEOUT
                    or tries >= MAX_SEND_RETRIES):
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return None
            tries += 1
            self.log(f"  [login] ⚠ {mail_timeout:.0f}s 未收到确认邮件"
                     f"（正常 3s 内必到 ⇒ 疑似丢包），批内重发第 {tries} 次")
            since_ms = int(time.time() * 1000) - 5_000
            try:
                r = cl.send_login_email(rec.email, mode=self.login_mode)
            except TypeSafeError as exc:
                self.log(f"  [login] ⚠ 重发异常: {exc}")
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return None
            if not r.ok:
                self.log(f"  [login] ⚠ 重发 HTTP {r.status}")
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return None
            rec.signup["send_retries"] = tries
            self.log(f"  [login] 重发成功，再等 {mail_timeout:.0f}s")

        rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                    + time.time() - t_wait)
        return m

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
        | `400 Bad request` + `Unrecognized key` | 请求体多/少了键（站点改了 schema） | 改 `typesafe.auth_callback` 的键集 |

        混掉的代价：把"取码 bug"当成"邀请制拦截"（或反过来），
        会把排查引向完全错误的方向 —— 这两条路的处置**恰好相反**。
        `tools/tests/test_orchestration.py::test_auth_error_triage` 用真实响应体钉住了这几条。

        ⚠️ 2026-09-21 起 `403` **不再是常态**（邀请制已取消，实测回调直接 200），
        但这条分流**必须保留**：站点随时可能恢复白名单，而删掉它会让
        "没被邀请"伪装成"凭据错误"，把排查引向重新发码的死循环。
        """
        code = str(res.data.get("error") or res.data.get("code") or "")
        suffix = f" {hint}" if hint else ""
        if "Access restricted" in code:
            # 凭据是对的，只是没被邀请 —— 这是业务门槛，不是技术故障。
            # 阶段名用 `invite_gate` 而不是已删除的 `approved`：
            # 那个名字属于"等审批"的旧链路，留着会让日志读起来像还有审批环节。
            rec.stages["invite_gate"] = "pending"
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
        # 站点 schema 收紧/放宽时走这里：Zod 的 strict 校验会把"未知键"单独列进
        # `details.formErrors`。不特判的话就只剩一句 `HTTP 400: Bad request`，
        # 完全看不出是"请求体多了一个键" —— 2026-09-21 实测正是这个形态：
        # 全链路 100% 失败（含 122 个已获批账号），文案毫无指向性，排查跑偏到
        # 验证码与白名单上。这里把它翻成人能直接执行的一句话。
        form_errs = (res.data.get("details") or {}).get("formErrors") or []
        if any("Unrecognized key" in str(e) for e in form_errs):
            return self._fail(
                rec, "login",
                f"认证回调 HTTP {res.status}: 请求体里有站点不认识的键 —— "
                f"{form_errs}；改 src/typesafe.py::auth_callback（键集有测试钉住）{suffix}")
        return self._fail(rec, "login", f"认证回调 HTTP {res.status}: {code}{suffix}")

    # ── 魔法链接交换（两个调用点共用的唯一实现） ───────────────────────
    def _exchange_link(self, rec: AccountRecord, mail: Any,
                       cl: TypeSafeClient) -> Result | None:
        """魔法链接 → dfp 交换 → 认证回调。失败时记 `_fail` 并返回 `None`。

        🔴 为什么必须抽成一个函数（2026-09-20 二轮审计）：
        它有**两个调用点** —— `MODE_LINK` 分支，以及**码模式回捞链接**那条路
        （站点对同一次发码请求可能回链接形态）。以前是 18 行**完全相同**的复制，
        而这是全项目唯一一处"改一边忘另一边会**直接导致登录失败**"的重复：
        两个入口的行为可以悄悄分叉，且分叉后只在其中一条路径上复现。

        抽取后 `stage_login` 从 97 行降到约 75 行（`src/` 最热函数）。

        🔴 **按序试所有候选**（2026-09-21 补跑实测）：正文里同一个 URL 可能有多份、
        且**只有一部分是完整的**（实测 12 份里 8 份被截断了 4 个字符，见
        `parsing._looks_complete`）。只试第一条 ⇒ 恰好取到残缺那条就必失败。
        残缺那条 GET 只会拿回 400（不消耗一次性 token），所以逐条试是安全的；
        而**成功那条会消耗 token** ⇒ 一旦拿到 redirect 就立刻返回，不再往下试。
        """
        links = extract_magic_links(mail.body)
        if not links:
            self._fail(rec, "login", "魔法链接邮件里没找到链接")
            return None
        last_err = ""
        for link in links:
            try:
                redirect = cl.exchange_magic_link(link)
                token = cl.token_from_redirect_url(redirect)
            except TypeSafeError as exc:
                # 只记**最后一条**的错误：前面的失败是"这条候选残缺"，
                # 最后一条的报错才最接近真正的原因（若全都残缺，就都一样）。
                last_err = str(exc)
                continue
            return cl.auth_callback(token, "magic_links", rec.email)
        suffix = f"（已试 {len(links)} 条候选）" if len(links) > 1 else ""
        self._fail(rec, "login", f"魔法链接交换失败{suffix}: {last_err}")
        return None

    # ── 阶段 1+2：发信 + 收凭据 + 认证回调（**唯一入口**） ─────────────
    def stage_login(self, rec: AccountRecord, *,
                    mail_timeout: float = MAIL_TIMEOUT) -> TypeSafeClient | None:
        """建邮箱（若缺）→ 发信 → 收凭据 → 认证回调。成功时**返回**已建立会话的 client。

        2026-09-21 起本方法是链路的**唯一入口**，取代了原先的
        `stage_apply → stage_wait_approval → stage_login` 三段：

            旧：Framer 投递申请 → 等回执 → 等人工审批 → 才能登录
            新：`POST /login` 提交邮箱 → 直接收确认邮件 → 点链接建会话

        ⇒ `rec.email` 为空时**本方法自己建一个临时邮箱**（旧代码里这一步在
        `stage_apply` 内）。`resume()` 传的是已知邮箱，走同一条路。

        🔴 返回 client 而不是挂 `self.client`：挂实例字段在串行时看不出问题，
        但并发时会**串号**（A 账号的 api_key 建在 B 账号的会话上）。
        返回值传递是开并发的前置条件。
        """
        t0 = time.time()
        if not rec.email:
            rec.email = self.mail.create_mailbox(self.domain)
            rec.key = rec.email
            rec.stages["mailbox"] = "ok"
        self.log(f"  [signup] 邮箱 {rec.email}")

        cl = TypeSafeClient()
        since = int(time.time() * 1000) - 5_000
        try:
            r = cl.send_login_email(rec.email, mode=self.login_mode)
        except TypeSafeError as exc:
            rec.timings["login"] = time.time() - t0
            self._fail(rec, "login", f"发信失败: {exc}")
            return None
        if not r.ok:
            rec.timings["login"] = time.time() - t0
            self._fail(rec, "login",
                       f"发信 HTTP {r.status}: {r.data.get('page_text', '')[:120]}")
            return None
        rec.stages["send"] = "ok"

        if self.login_mode == MODE_LINK:
            m = self._wait_with_retry(rec, cl, MATCH_LINK,
                                      mail_timeout=mail_timeout, since_ms=since)
            if m is None:
                # 🔴 失败路径也要写 `timings`（旧实现只在成功路径写 ⇒ 失败账号
                # 的耗时是空的，300s 空等不留痕）。下同。
                rec.timings["login"] = time.time() - t0
                self._fail(rec, "login",
                           self._mail_timeout_msg(mail_timeout, code_first=False))
                return None
            rec.signup["mail_subject"] = m.subject
            rec.signup["mail_at"] = m.received_at
            res = self._exchange_link(rec, m, cl)
            if res is None:
                rec.timings["login"] = time.time() - t0
                return None
        else:
            m = self._wait_with_retry(rec, cl, MATCH_CODE,
                                      mail_timeout=mail_timeout, since_ms=since)
            if m is None:
                # 🔴 2026-09-20 实测：站点对**同一次**发码请求可能回魔法链接
                # （主题 "Sign in to TypeSafe"）而不是 6 位码——同一账号 4 次发码里
                # 就有 1 次是链接。此时继续等码必然超时；若直接判
                # "未收到验证码邮件"，排查会被引向"D1 窗口被挤爆 / 邮箱坏了"，
                # 而真相只是凭据形态换了一种。先回捞一次链接再判死。
                m = self._wait_with_retry(rec, cl, MATCH_LINK,
                                          mail_timeout=LINK_FALLBACK_TIMEOUT,
                                          since_ms=since, allow_retry=False)
                if m is None:
                    rec.timings["login"] = time.time() - t0
                    self._fail(rec, "login",
                               self._mail_timeout_msg(mail_timeout, code_first=True))
                    return None
                self.log(f"  [login] ⚠ 码模式未收到 6 位码，但收到魔法链接"
                         f"（主题={m.subject!r}）—— 改走链接交换回捞")
                res = self._exchange_link(rec, m, cl)
                if res is None:
                    rec.timings["login"] = time.time() - t0
                    return None
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

    # ── 已删除：`stage_login_with_token()`（2026-09-21）──────────────────
    # 用途曾是"跳过读邮箱、由人把验证码/魔法链接 token 粘进来"（`--mode claim`
    # 的 `--token` 分支）。删除理由**不是"修好了"**，而是三条同时成立：
    #   ① 它的唯一调用者 `runner.claim()` 已随 `watch()` 一并删除 ⇒ 无入口；
    #   ② 它本身**已知失效**：`claim` 的两进程用法实测必报
    #      `401 Code expired`（`--send` 与 `--token` 各建会话、cookie 不传递，
    #      而本函数**没有** `GET /login` 那一步来种 cookie）—— 根因候选至今
    #      **未实测**，见 `docs/runbook.md` §1.5（那一节已改写成"已移除"的事后复盘）；
    #   ③ 新链路里"人工接力"不再有场景：魔法链接 7 天有效，`resume` 一条命令
    #      就能把没拿到 key 的地址重跑（含历史链接复用）。
    # 需要恢复时：`src/stages.py` 的原版在
    # `.workbuddy-ai/backup/refactor-20260921-180438/src/stages.py`。

    # ── 阶段 5+6：onboarding + 建 key ─────────────────────────────────
    def stage_create_key(self, rec: AccountRecord, cl: TypeSafeClient,
                         *, name: str = "1") -> bool:
        """建 key —— **这才是"这个账号成不成"的判据**。

        🔴 onboarding 的 `ok` **不再是门槛**（2026-09-21 实测）：
        站点把 `/hook` 的重定向当**引导**用，而它本身还是非确定性的
        （同一次运行里相邻两次 `GET /hook` 可以给出不同答案）。
        实测证据：门禁停在 `console-survey`（那一页没有 `$ACTION_*` 隐藏域、
        我们无法自动提交）时，`POST /api/api-keys` 照样返回 200 + 明文 key。

        ⇒ 这里改成"先尽力推进、再直接建 key"：
          · onboarding 没归零 ⇒ 记 `partial` + 打日志，**继续建 key**；
          · key 建出来 ⇒ `keyed`，并把 onboarding 的实况一并留在 `user` 里；
          · key 建不出来 ⇒ 才判失败，且把 onboarding 的实况附在错误里
            （否则"建 key 失败"会让人去查配额，而真因可能是门禁形态变了）。
        """
        t0 = time.time()
        ob = cl.complete_onboarding(display_name=rec.email.split("@")[0][:24])
        rec.user["onboarding"] = ob.data.get("completed", [])
        if ob.ok:
            rec.stages["onboarding"] = "ok"
        else:
            rec.stages["onboarding"] = "partial"
            self.log(f"  [onboarding] ⚠ 门禁未归零：{ob.error}")
            self.log(f"  [onboarding]   （门禁序列 {ob.data.get('gates')}；"
                     f"仍继续建 key —— 门禁是引导，不是硬门槛）")
        try:
            key = cl.create_api_key(name)
        except TypeSafeError as exc:
            detail = "" if ob.ok else f"｜onboarding 未归零：{ob.error}"
            return self._fail(rec, "api_key", f"建 key 失败: {exc}{detail}",
                              status="partial")

        rec.timings["create_key"] = time.time() - t0
        rec.api_key = key.get("api_key", "")
        rec.api_key_id = key.get("id", "")
        rec.stages["api_key"] = "ok"
        rec.status = "keyed"
        rec.error = ""
        # 🔴 这里是**唯一**产出 key 的地方 ⇒ 成功数据挂在这里写，
        # 就自动覆盖了全部调用路径（run_batch / resume），
        # 不需要在每个调用点各写一遍（那种写法迟早漏一处）。
        self.success_ledger.append(rec.to_dict())
        self.log(f"  [api_key] {rec.api_key[:24]}…  ({rec.timings['create_key']:.1f}s)")
        return True
