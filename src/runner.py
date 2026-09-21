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

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import config
from .ledger import Ledger
# 🔴 `AccountRecord` 刻意定义在 `stages`（不在本模块）：依赖必须单向
#    runner → stages，反过来就成环了。这里导入是为了让 `from src.runner import
#    AccountRecord` 继续可用（生产与自测都这么引）。
from .stages import (CONFIRM_TIMEOUT, MATCH_ACCOUNT_READY, AccountRecord,
                     StageMixin)
from .tempemail import TempMailClient, TempMailError
from .typesafe import MODE_CODE, MODE_LINK, TypeSafeClient, TypeSafeError

# 与 `stages.py` 的分工见该文件头部。**本模块不写任何出网动作**（出网全在
# stages），只负责：建实例、并发扇出、写台账、以及 `claim` 里"先发码"那一步
# —— 后者是唯一的例外，因为它属于"人工接力"的调度侧（`claim` 的两个进程
# 各自建 `TypeSafeClient`，中间不传会话，见 `claim()` 的 🔴 说明）。
#: 并发时多线程会同时 print，不加锁会在一行中间交错，日志直接没法读。
_LOG_LOCK = threading.Lock()


class Pipeline(StageMixin):
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
            # 串行路径**刻意不吞异常**（fail-fast）：崩了就直接抛出去，看得见。
            # 这个不对称是有意的，也是这个洞隐蔽的原因 —— 串行跑一万次也复现不出来。
            return [fn(self, key) for key, fn in jobs]

        out: list[AccountRecord | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {pool.submit(fn, self._clone(), key): pos
                    for pos, (key, fn) in enumerate(jobs)}
            for fut in as_completed(futs):
                pos = futs[fut]
                try:
                    out[pos] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    # 单个 worker 炸了不该带走整批，但**必须留下一条台账记录**。
                    # 详见 `_worker_crash_record()` 的说明。
                    rec = self._worker_crash_record(jobs[pos][0], exc)
                    self.ledger.append(rec.to_dict())
                    out[pos] = rec
                    self.log(f"✗ 并发任务异常（已记入台账，不再静默丢弃）: "
                             f"{type(exc).__name__}: {exc}")
        return [r for r in out if r is not None]

    @staticmethod
    def _worker_crash_record(key: Any, exc: BaseException) -> AccountRecord:
        """并发 worker 崩溃时补一条台账记录 —— 保证 **提交数 == 结果数**。

        🔴 为什么必须补（2026-09-20 二轮审计，已复现 4 提交 / 3 返回 / 3 落账）：
        以前 `except` 里只 `self.log(...)` 一行，**不写台账**；而 `job()` 是先跑完
        再 `ledger.append()` ⇒ 崩溃的那个账号**既不在返回值里、也不在台账里**。
        更麻烦的是 `report()` 的"合计"用的是 `len(recs)` ⇒ 提交 100 个、崩 3 个，
        报告会写"合计 97"，**数字自洽、看不出缺口**。
        这与"RANK 词汇错配导致账号从验收清单消失"是同一类：不报错，只是少几行。
        **只在 `--concurrency > 1` 时存在** —— 串行路径没有这条代码路径。

        ⚠️ `key` 是**批次序号**而不是邮箱：`run_batch` 的邮箱是在崩溃的 worker
        **内部**才建出来的（`create_mailbox()`），崩了就无从得知。
        所以键写成 `worker-crash#<n>`，明确不是邮箱 —— 并由
        `tools/resume_pending.py::pending()` 显式跳过（否则补跑会拿 `worker-crash#0`
        当邮箱去登录）。
        """
        rec = AccountRecord(key=f"worker-crash#{key}", email="")
        rec.status = "failed"
        rec.error = f"worker 崩溃: {type(exc).__name__}: {exc}"
        rec.stages["worker"] = "failed"
        return rec

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

        🔴 **这个"两进程"用法实测失效（2026-09-20）**：`--send` 与 `--token`
        是两个独立进程，各自 `TypeSafeClient()`，**中间没有任何会话传递** ⇒
        实测**码到 2 分钟内提交仍报 `401 Code expired`**。
        而 runbook §4.2 对该错误码的处置是"重新发码" ⇒ **把人推回死循环**。

        ⇒ **邮箱在 Worker 覆盖域内时用 `resume()`**（同一进程内发码+收码+提交，
        会话连续，实测正常）。`claim` 只留给"真人邮箱、Worker 读不到"这一种场景，
        且那条路当前是坏的 —— 根因候选与验证方法见 `docs/runbook.md` §1.5。
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
