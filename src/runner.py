"""端到端编排：注册 → 登录 → 创建 API Key → 入库。

阶段与可自动化程度（实测结论，不是推断）：

    1. signup      建临时邮箱 + POST /login 发确认邮件              ✅ 全自动
    2. login       收邮件 → 魔法链接换 token → POST /api/auth/callback  ✅ 全自动
    3. onboarding  /setup/tos → /setup/set-name（站点门禁驱动）      ✅ 全自动
    4. api_key     POST /api/api-keys                              ✅ 全自动
    5. store       写入 JSONL 台账                                  ✅ 全自动

2026-09-21：链路**没有外部阻断点了**
────────────────────────────────────
旧链路在第 3 阶段有个硬门槛：TypeSafe 是邀请制，未获批的邮箱会拿到
`403 Access restricted`，只能等人工/批量审批 ⇒ 跑批必须拆成
"投申请（apply）"和"对已获批邮箱跑后续（resume）"两个模式，再加一个
`watch`（监听获批邮件自动续跑）。

取消邀请制后 `/login` 提交邮箱**直接**发确认邮件、回调直接 200
⇒ 全链路**无人值守可跑**：

    旧：apply → confirm → approved(❌人工) → login → onboarding → api_key
    新：signup+login → onboarding → api_key

⇒ 随之删除三样东西（都是"为等待而存在"的机制）：
  · `--mode apply`（申请段本身没了）
  · `watch()`（不再需要"等获批邮件"；它读全表共享窗口，是纯负债）
  · `claim()`（两进程人工接力，**2026-09-20 已实测失效**：`--send` 与 `--token`
    各建会话、无传递 ⇒ 必报 `401 Code expired`）

并发的边界（`--concurrency`）
──────────────────────────
每个账号只读**自己的**收件箱索引端点（`/api/inbox?email=`），彼此独立，可以并发。
默认仍是 1（串行）—— 站点侧对"短时间大量发信"的容忍度未知，小批试跑更稳。

⚠️ 并发的前置条件是"**不共享可变状态**"：本模块以前把会话 client 挂在
`self.client` 上（串行看不出问题，并发会**串号** —— A 账号的 api_key 建在 B 的会话上）。
现在 `stage_login()` 改为**返回** client，`run_batch`/`resume` 每个 worker 用独立的
`Pipeline` 实例，唯一共享的是带锁的 `Ledger`。

两份台账（2026-09-20 起）
──────────────────────
    exports/ledger.jsonl    运行台账：**全部尝试**（含失败的），用于复盘
    result/success.jsonl    成功数据：**只记拿到 key 的**，是交付物

成功那份在 `stage_create_key()` 里写 —— 那是**唯一**产出 key 的地方，
挂在那里就自动覆盖了全部调用路径（`run_batch` / `resume`）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import config
from .ledger import Ledger
# 🔴 `AccountRecord` 刻意定义在 `stages`（不在本模块）：依赖必须单向
#    runner → stages，反过来就成环了。这里导入是为了让 `from src.runner import
#    AccountRecord` 继续可用（生产与自测都这么引）。
from .stages import MAIL_TIMEOUT, AccountRecord, StageMixin
from .tempemail import TempMailClient, TempMailError
# ⚠️ 本模块只留 `MODE_CODE` —— 它是 `Pipeline.__init__` 的默认值。
# `MODE_LINK` 与 `TypeSafeClient` / `TypeSafeError` 曾经因为 `claim()` 的
# `send_first` 分支而出现在这里；`claim()` 删除后它们在本模块**零引用**，
# 已一并移除（死符号扫描会报，且它们正是"runner 也发请求"这个错误印象的来源 ——
# 出网动作**全部**在 stages）。
from .typesafe import MODE_CODE

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
    def run_one(self, *, email: str = "", mail_timeout: float = MAIL_TIMEOUT,
                name: str = "1") -> AccountRecord:
        """跑一个账号：注册 → 登录 → onboarding → 建 key。

        `email` 为空时由 `stage_login` 现场建一个临时邮箱（这是默认用法，
        取代了旧链路里 `stage_apply` 的建邮箱职责）。
        """
        rec = AccountRecord(key=email, email=email)
        try:
            cl = self.stage_login(rec, mail_timeout=mail_timeout)
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

    def run_batch(self, *, count: int = 1, name: str = "1",
                  concurrency: int = 1,
                  mail_timeout: float = MAIL_TIMEOUT) -> list[AccountRecord]:
        """建 `count` 个新邮箱，各跑一遍全链路。

        旧签名里有 `mode` / `approval_timeout` / `confirm_timeout` 三个参数
        （分别用于"只投申请"与"等审批"），随邀请制取消一并删除。
        """
        def job(pipe: "Pipeline", i: int) -> AccountRecord:
            pipe.log(f"[{i + 1}/{count}] 开始")
            rec = pipe.run_one(mail_timeout=mail_timeout, name=name)
            pipe.ledger.append(rec.to_dict())
            pipe.log(f"[{i + 1}/{count}] status={rec.status} {rec.error}")
            return rec

        return self._fan_out([(i, job) for i in range(count)], concurrency=concurrency)

    def resume(self, emails: list[str], *, name: str = "1",
               concurrency: int = 1,
               mail_timeout: float = MAIL_TIMEOUT,
               skip_keyed: bool = True) -> list[AccountRecord]:
        """对**已知邮箱**跑全链路（不新建邮箱）。

        与 `run_batch` 的唯一区别是邮箱来源：这里用调用方给的地址，
        所以同一个地址可以**反复重跑** —— 站点发信存在丢包，
        而魔法链接 7 天有效，重跑往往比改请求更有效。

        🔴 `skip_keyed=True`（默认）跳过台账里**已有 api_key** 的地址。
        这不是优化，是数据完整性：重跑会给同一账号**造出第二把 key**，
        两把在服务端都有效，但 `Ledger.load()` 按邮箱去重、**末行胜出**
        ⇒ 交付物里少一把（不报错，只是行数不对）。
        这条保护原先长在 `watch()` 里，`watch()` 删除后移到这里 ——
        它是 `resume` 唯一的"会重复调用"入口，所以必须由它兜住。
        要**故意**重跑（例如换 key 名）时显式传 `skip_keyed=False`。
        """
        have_key = ({r["email"] for r in self.ledger.load() if r.get("api_key")}
                    if skip_keyed else set())
        todo: list[str] = []
        for e in emails:
            if e in have_key:
                self.log(f"[resume] 跳过 {e}（台账里已有 api_key，"
                         f"重跑会造第二把 key；确需重跑请传 skip_keyed=False）")
                continue
            todo.append(e)

        def job(pipe: "Pipeline", email: str) -> AccountRecord:
            rec = AccountRecord(key=email, email=email)
            pipe.log(f"[resume] {email}")
            cl = pipe.stage_login(rec, mail_timeout=mail_timeout)
            if cl is not None:
                pipe.stage_create_key(rec, cl, name=name)
            pipe.ledger.append(rec.to_dict())
            pipe.log(f"[resume] status={rec.status} {rec.error}")
            return rec

        return self._fan_out([(e, job) for e in todo], concurrency=concurrency)

    # ⚠️ `claim()` 与 `watch()` 已删除（2026-09-21，随邀请制取消）
    # ────────────────────────────────────────────────────────────────
    # 两个方法都是"为等待而存在"的机制，链路全自动之后它们只剩负债：
    #
    #   claim()  两进程人工接力（--send 发码 / --token 提交）。**2026-09-20 已实测失效**：
    #            两个进程各自建 TypeSafeClient、中间没有任何会话传递 ⇒ 码到 2 分钟内
    #            提交仍报 401 Code expired，而 runbook 对该码的处置是"重新发码"
    #            ⇒ 把人推回死循环。它当时只对"真人邮箱、Worker 读不到"一种场景有意义。
    #
    #   watch()  轮询**全表共享窗口**找"获批邮件"并自动续跑。取消邀请制后
    #            "获批"这个事件不再存在；而它读的是 /admin/all 全表（retention 100 行、
    #            被同机邻居项目刷屏），是纯粹的配额负债。
    #
    # 连带删除：`stages.stage_login_with_token()` —— 它是 `claim()` 的 `--token`
    # 分支唯一的落点，`claim()` 一走它就无入口了（细节与回滚路径见 `stages.py`
    # 里那段说明）。需要"人工粘凭据"时改用 `resume`：魔法链接 7 天有效。
