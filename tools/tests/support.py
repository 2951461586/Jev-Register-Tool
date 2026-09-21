"""自测**共享夹具**：`check()` 计数器 + 离线替身 + 模块别名转手。

拆自 `tools/selftest.py`（2026-09-20 二轮审计 ⑩）。

⚠️ 本模块刻意**不使用** `conftest.py` 这个名字 —— 那是 pytest 的保留名，
本项目零第三方依赖、跑法仍是 `python tools/selftest.py`，用它只会让人以为装了 pytest。

⚠️ 本模块是**转手**面：`__all__` 里有些名字本模块自己不用（`fw` / `ps` / `RANK` …）。
原因很实际 —— 测试模块要在 `sys.path` 引导**之后**才能 `from src import ...`，
而引导就写在下面；别名统一从这里取，各测试文件就不必各写一遍引导 + `# noqa: E402`。
"""

from __future__ import annotations

import contextlib
import itertools
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import framer_waitlist as fw  # noqa: E402
from src import parsing as ps  # noqa: E402
from src import runner as pl  # noqa: E402
from src import stages as st  # noqa: E402
from src import typesafe as ts  # noqa: E402
from src.ledger import RANK, Ledger  # noqa: E402
from src.tempemail import Mail  # noqa: E402
from src.typesafe import Result, TypeSafeError  # noqa: E402

#: 计数放在可变容器里，跨模块共享（`check()` 不再用 `global`）。
_COUNTS = {"pass": 0, "fail": 0}


def counts() -> tuple[int, int]:
    """返回 (通过, 失败)。聚合入口用它打印总计。"""
    return _COUNTS["pass"], _COUNTS["fail"]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _COUNTS["pass"] += 1
        print(f"  ✓ {name}")
    else:
        _COUNTS["fail"] += 1
        print(f"  ✗ {name}  {detail}")


# ══════════════════════════════════════════════════════════════════════
# 离线替身：只实现 runner / stages 真正用到的方法，不碰网络
# ══════════════════════════════════════════════════════════════════════

class _FakeTempMailClient:
    """替换 `src.runner.TempMailClient`（含 `_clone()` 里新建的那些）。

    `pending` 是**类属性**：所有实例共享同一批待收邮件，模拟"同一批邮箱窗口"。
    收件人匹配 `email` 或通配 `"*"`（申请段建邮箱时地址是运行时生成的）。
    """

    pending: list = []
    _seq = itertools.count(1)

    def __init__(self, *a, **kw):
        # 字段要与真实的 `tempemail.Stats` 一致 —— 替身多出真实类型没有的字段
        # 会掩盖"某个计数器其实没人读"这类问题（2026-09-20 二轮审计删了
        # `http_4xx` / `created` 两个只写不读的计数器，替身同步收窄）。
        self.stats = SimpleNamespace(polls=0, http_5xx=0)
        self._n = next(_FakeTempMailClient._seq)

    def create_mailbox(self, domain: str | None = None) -> str:
        return f"fake-{self._n}@example-mail.test"

    def wait_for_mail(self, email, match, *, timeout=0.0, interval=0.0, since_ms=None):
        for m in type(self).pending:
            if m.recipient in (email, "*") and match(m):
                return m
        return None

    def first_mail_matching(self, email, match):
        for m in type(self).pending:
            if m.recipient in (email, "*") and match(m):
                return m
        return None

    def list_mails(self, email=None, limit=None):
        return list(type(self).pending)

    def scan_all(self, limit: int = 100):
        return list(type(self).pending)


class _FakeTypeSafe:
    """替换 `src.stages.TypeSafeClient`（`runner.claim` 里那份也一起换）。

    ⚠️ 阶段方法建会话的地方在 `stages`，`claim()` 的 `send_first` 分支在 `runner`
    —— 两个模块的命名空间**都要**替换，见 `offline()`。

    ★ `create_api_key` 把**本会话的邮箱**编进 key 里 —— 这是"串号"检测器：
    如果两个并发 worker 共用了会话对象，返回的 key 里就会是**别人的**邮箱。
    """

    callback_status = 200
    callback_body: dict = {}
    fail_key = False

    def __init__(self, *a, **kw):
        self.email = ""

    #: 最近一次 `auth_callback` 用的凭据类型（"otp" / "magic_links"）。
    #: 用于断言"码模式回捞链接"时确实走的是链接分支。
    last_token_type = ""

    def send_login_email(self, email, mode="code"):
        self.email = email
        return Result(ok=True, status=200, stage="send_login_email",
                      data={"page_text": "Check your email"})

    def exchange_magic_link(self, url):
        return "https://console.typesafe.ai/?stytch_token_type=magic_links&token=FAKETOKEN"

    def token_from_redirect_url(self, url):
        return "FAKETOKEN"

    def auth_callback(self, token, token_type, email):
        # 先睡再写 email：放大竞态窗口，让"共享会话"的 bug 有机会暴露
        time.sleep(0.005)
        self.email = email
        type(self).last_token_type = token_type
        ok = type(self).callback_status == 200
        return Result(ok=ok, status=type(self).callback_status,
                      stage="auth_callback", data=dict(type(self).callback_body))

    def me(self):
        return {"human_name": "tester",
                "latest_tos_acceptance": {"tos_version": 1},
                "console_survey_completed_at": 1,
                "org_memberships": []}

    def complete_onboarding(self, display_name="x"):
        return Result(ok=True, stage="onboarding", data={"completed": []})

    def create_api_key(self, name="1"):
        if type(self).fail_key:
            raise TypeSafeError("建 key 失败 HTTP 500")
        return {"api_key": f"apikey_FAKE_{self.email}", "id": f"kid_{self.email}"}


#: 出网符号。⚠️ **读**它们的模块不止一个 —— patch 必须覆盖全部读者，见 `offline()`。
_PATCH_NAMES = ("TypeSafeClient", "TempMailClient", "framer_submit")
_PATCH_TARGETS = (pl, st)


@contextlib.contextmanager
def offline(*, ts_status: int = 200, ts_body: dict | None = None,
            fail_key: bool = False, mails: list | None = None,
            framer_ok: bool = True):
    """把三个出网点换成替身。退出时**必定**还原。

    🔴 为什么必须 patch **两个**模块（2026-09-20 二轮审计 ⑪，拆分 `pipeline` 后）：
    monkeypatch 生效与否只看一件事 —— **读**这个符号的那个模块的 globals。
    拆完之后读者分家了：

        stages.TypeSafeClient   ← stage_login / stage_login_with_token 建会话
        stages.framer_submit    ← stage_apply 提交申请表单
        runner.TypeSafeClient   ← claim() 的 `send_first` 分支
        runner.TempMailClient   ← Pipeline.__init__ / _clone() 建邮箱客户端

    只 patch 其中一个模块，另一个会静默拿到**真类** ⇒ 自测开始发真请求。
    这种失效**不报 ImportError**，只是突然全线超时，极难定位。
    ⇒ 下面那个 `assert` 就是接缝守卫：三个符号必须都被覆盖到，少一个立刻炸。
    """
    seams = [(m, n) for m in _PATCH_TARGETS for n in _PATCH_NAMES
             if hasattr(m, n)]
    assert {n for _, n in seams} == set(_PATCH_NAMES), (
        f"出网符号没被全覆盖（有模块已经不再 import 它了？）："
        f"{sorted({n for _, n in seams})} vs {sorted(_PATCH_NAMES)}")
    saved = [(m, n, getattr(m, n)) for m, n in seams]
    _FakeTempMailClient.pending = list(mails or [])
    _FakeTypeSafe.callback_status = ts_status
    _FakeTypeSafe.callback_body = dict(ts_body or {})
    _FakeTypeSafe.fail_key = fail_key
    fake = {
        "TypeSafeClient": _FakeTypeSafe,
        "TempMailClient": _FakeTempMailClient,
        "framer_submit": lambda email, **kw: (
            {"status": 201, "ok": True, "body": ""} if framer_ok
            else {"status": 422, "ok": False, "body": "form error"}),
    }
    for m, n in seams:
        setattr(m, n, fake[n])
    try:
        yield pl
    finally:
        for m, n, v in saved:
            setattr(m, n, v)
        _FakeTempMailClient.pending = []


def _make_pipe(P, ledger: Ledger, success: Ledger | None = None):
    """造一个**全离线**的 Pipeline。

    🔴 `success_ledger` 必须显式传临时台账。它的默认值是
    `config.SUCCESS_LEDGER_PATH` —— 那是**交付物**（`result/success.jsonl`）。
    不传就等于让自测往交付物里写 `apikey_FAKE_*`：实测灌进去 9 条假记录，
    而 `verify_keys` 读的是**主台账**，所以一路没人发现。
    """
    return P.Pipeline(mail=P.TempMailClient(), ledger=ledger,
                      success_ledger=success or _tmp_ledger(), verbose=False)


def _otp_mail(to: str, code: str = "123456") -> Mail:
    return Mail(id="m-otp", to=to, sender="bounces+x-a@em5082.typesafe.ai",
                subject="Your TypeSafe sign-in code",
                body=f"{code} is your one-time code", received_at=1)


def _link_mail(to: str) -> Mail:
    """站点对**同一次**发码请求回了魔法链接形态（2026-09-20 实测存在）。"""
    return Mail(id="m-link", to=to, sender="bounces+x-b@em5082.typesafe.ai",
                subject="Sign in to TypeSafe",
                body='<a href="https://login.typesafe.ai/v1/magic_links/redirect?'
                     'stytch_token_type=magic_links&token=FAKETOKEN">Sign in</a>',
                received_at=1)


def _waitlist_mail(to: str) -> Mail:
    return Mail(id="m-wait", to=to, sender="010001a0bb95b4b5-722f1aae@envelope.updates.typesafe.ai",
                subject="TypeSafe AI: You\u2019re on the waitlist for Jev!",
                body="We'll be in touch when it's your turn.", received_at=1)


def _tmp_ledger() -> Ledger:
    return Ledger(Path(tempfile.mkdtemp()) / "ledger.jsonl")
