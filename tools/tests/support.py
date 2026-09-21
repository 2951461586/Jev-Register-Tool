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

    def list_mails(self, email=None, limit=None):
        return list(type(self).pending)

    def scan_all(self, limit: int = 100):
        return list(type(self).pending)


class _FakeTypeSafe:
    """替换 `src.stages.TypeSafeClient`（`runner` 里那份也一起换）。

    ⚠️ 出网符号在**两个**模块的命名空间里各有一份读者 —— 都要替换，
    见 `offline()` 里的 `_PATCH_TARGETS`。漏一个不会报 ImportError，
    只会让自测开始发真请求（表现是"突然全线超时"）。

    ★ `create_api_key` 把**本会话的邮箱**编进 key 里 —— 这是"串号"检测器：
    如果两个并发 worker 共用了会话对象，返回的 key 里就会是**别人的**邮箱。
    """

    callback_status = 200
    callback_body: dict = {}
    fail_key = False
    #: `complete_onboarding` 的替身行为。默认 True（门禁归零）。
    #: 置 False 用来测"**门禁没归零但 key 建得出来**"这条新语义
    #: （2026-09-21 实测：站点把 `/hook` 的重定向当引导用，不是硬门槛）。
    onboarding_ok = True

    def __init__(self, *a, **kw):
        self.email = ""

    #: 最近一次 `auth_callback` 用的凭据类型（"otp" / "magic_links"）。
    #: 用于断言"码模式回捞链接"时确实走的是链接分支。
    last_token_type = ""

    #: `send_login_email` 的累计调用次数（**类级**；`offline()` 进入时清零）。
    #: 用来断言"超时后**真的**重发了"，而不是只把常量改了个数 ——
    #: 常量改对但重发没接上线，是本轮最容易出现的"看着改了其实没生效"。
    send_calls = 0

    def send_login_email(self, email, mode="code"):
        type(self).send_calls += 1
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
        if type(self).onboarding_ok:
            return Result(ok=True, stage="onboarding", data={"completed": []})
        return Result(ok=False, stage="onboarding",
                      error="遇到不认识的引导步骤 'console-survey'",
                      data={"completed": [], "gates": ["console-survey"],
                            "via": [], "stopped": "不认识的引导步骤"})

    def create_api_key(self, name="1"):
        if type(self).fail_key:
            raise TypeSafeError("建 key 失败 HTTP 500")
        return {"api_key": f"apikey_FAKE_{self.email}", "id": f"kid_{self.email}"}


#: 出网符号。⚠️ **读**它们的模块不止一个 —— patch 必须覆盖全部读者，见 `offline()`。
#:
#: 2026-09-21：`framer_submit` 已从本集合移除 —— 它随 `src/framer_waitlist.py`
#: 一起删除，`runner` / `stages` 两个命名空间里都不再有这个名字。
#: 下面的 `assert` 会自动发现"某个符号已经不在任何读者里"这件事。
_PATCH_NAMES = ("TypeSafeClient", "TempMailClient")
_PATCH_TARGETS = (pl, st)


@contextlib.contextmanager
def offline(*, ts_status: int = 200, ts_body: dict | None = None,
            fail_key: bool = False, mails: list | None = None,
            onboarding_ok: bool = True):
    """把出网点换成替身。退出时**必定**还原。

    🔴 为什么必须 patch **两个**模块（2026-09-20 二轮审计 ⑪，拆分 `pipeline` 后）：
    monkeypatch 生效与否只看一件事 —— **读**这个符号的那个模块的 globals。
    拆完之后读者分家了：

        stages.TypeSafeClient   ← stage_login 建会话（`stage_login_with_token`
                                   已于 2026-09-21 删除，见 `stages.py` 说明）
        runner.TempMailClient   ← Pipeline.__init__ / _clone() 建邮箱客户端

    只 patch 其中一个模块，另一个会静默拿到**真类** ⇒ 自测开始发真请求。
    这种失效**不报 ImportError**，只是突然全线超时，极难定位。
    ⇒ 下面那个 `assert` 就是接缝守卫：符号集必须与 `_PATCH_NAMES` 完全一致，
      多一个（某个模块又 import 了已删除的东西）少一个都会立刻炸。
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
    _FakeTypeSafe.onboarding_ok = onboarding_ok
    _FakeTypeSafe.send_calls = 0
    fake = {
        "TypeSafeClient": _FakeTypeSafe,
        "TempMailClient": _FakeTempMailClient,
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


def _confirm_mail(to: str) -> Mail:
    """**主路径凭据邮件** —— 主题与 2026-09-21 实测逐字一致。

    它取代了旧的 `_waitlist_mail`（"You're on the waitlist"，申请回执）。
    邀请制取消后 `/login` 提交邮箱直接回这一封，所以它现在既是"注册确认"
    也是"登录凭据来源"，一个替身覆盖两件事。
    """
    return Mail(id="m-confirm", to=to,
                # 🔴 sender 里的域名是**我们的收信域**，不是站点的 —— 站点把
                #    `bounces+<id>-<hash>-x=<我们的域>@em5082.typesafe.ai` 作为
                #    信封发件人。这里换成文档用假域（`example-mail.test`），
                #    避免把自有域名写进公开仓库；规则只按**主题**匹配，
                #    所以 sender 的具体值不影响本夹具的有效性。
                sender="bounces+17391058-efc7-x=example-mail.test@em5082.typesafe.ai",
                subject="Welcome to TypeSafe \u2014 confirm your email",
                body="Welcome to TypeSafe\n"
                     "Click the button below to finish creating your account. "
                     "Your link expires in 7 days.\n"
                     "https://login.typesafe.ai/v1/magic_links/redirect?"
                     "public_token=public-token-live-x&stytch_token_type=magic_links"
                     "&token=FAKETOKEN",
                received_at=1)


def _tmp_ledger() -> Ledger:
    return Ledger(Path(tempfile.mkdtemp()) / "ledger.jsonl")
