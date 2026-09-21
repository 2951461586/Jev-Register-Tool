"""集中配置。

凭据一律从环境变量读取，默认值留空 —— 避免 `os.getenv(k, "真实值")` 这种泄漏点。
用 .env 加载（stdlib 实现，不引 python-dotenv）。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载器：真实环境变量优先级更高。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")

# ── CF Temp Email Worker ────────────────────────────────────────────────
# 🔴 三个都**不留真实默认值**。以前 `TEMPMAIL_BASE` 的默认值是真实的
#    `https://temp-email-worker.<子域>.workers.dev`、`TEMPMAIL_DOMAIN` 是真实域名，
#    于是"基础设施标识"被写进了仓库（Worker 子域 + 自有邮箱域名）。
#    这类东西单独看不是凭据，但合起来足以被针对性打击，且一旦公开就永久公开。
#    现在一律走 .env，缺失由 `validate()` 显式报出来（不是静默用默认值连上去）。
TEMPMAIL_BASE = os.getenv("TEMPMAIL_BASE", "")
TEMPMAIL_ADMIN_KEY = os.getenv("TEMPMAIL_ADMIN_KEY", "")
TEMPMAIL_DOMAIN = os.getenv("TEMPMAIL_DOMAIN", "")

# ── Cloudflare API（只有 tools/probes/* 诊断脚本用得到）──────────────────
# 同理：账号 id / D1 库 id / API Token 一个都不进仓库。
# `tools/probes/probe_worker_health.py` 与 `probe_email_routing.py` 会读这三个。
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_D1_ID = os.getenv("CF_D1_ID", "")

# ── TypeSafe 站点 ───────────────────────────────────────────────────────
SITE_ORIGIN = "https://console.typesafe.ai"
SITE_LOGIN = f"{SITE_ORIGIN}/login"
STYTCH_LOGIN_HOST = "https://login.typesafe.ai"

# ── Framer 表单（waitlist 申请）—— **已于 2026-09-21 整体移除** ─────────
#
# 这里曾有 `FRAMER_SITE_ID` / `FRAMER_FORM_ID` / `FRAMER_SUBMIT_URL` /
# `FRAMER_REFERER` 与一套 PoW 常量（`POW_SALT` / `POW_DIFFICULTY` / …）。
# 它们服务于"向 Framer 表单投递 waitlist 申请"这一步，而 TypeSafe
# **已取消邀请制**：现在 `/login` 直接发确认邮件，注册即登录。
#
# ⇒ 连同 `src/framer_waitlist.py` 一起删除。**不要凭印象加回来**：
#    判据是 `POST /login` 是否直接回 `x-action-redirect: /login?sent=true`，
#    以及邮件是否直接是 "Welcome to TypeSafe — confirm your email"。
#    两条 2026-09-21 实测均成立（见 docs/architecture.md §1）。

# ── 邮箱匹配规则 ────────────────────────────────────────────────────────
# 🔴 收件规则**不在本文件**，唯一真源是 `src/mailrules.py` 的 `RULES` 表。
#
# 这里曾经有一份副本（MAIL_FROM_WAITLIST / MAIL_FROM_STYTCH / SUBJ_*），
# 全项目零引用，但恰好是 README、mailrules docstring、docs/mail-filters.md
# 三处都在禁止的"散落 subject 子串"。留着它的实际危害是：
# 下一个人改文案时看到 `SUBJ_ACCOUNT_READY = "account is ready"` 会去改它，
# 而真正生效的是规则表 ⇒ 改了不生效，且查不出原因。
# 2026-09-20 审计后删除。要加规则请改 `src/mailrules.py`。

# ── 网络 ────────────────────────────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# ── 输出 ────────────────────────────────────────────────────────────────
# 两类产物**分开放**（2026-09-20 起）：
#
#   exports/  运行产物 —— 台账（含全部尝试，含失败的）、日志、诊断残留
#   result/   **成功数据** —— 拿到 key 的账号 + 验收结果。这是**交付物**目录。
#
# 分开的理由：台账要留全部历史（失败的也留，便于复盘），
# 而交付物只该有成功的。以前两者混在 exports/ 里，取交付物时得自己筛。
EXPORT_DIR = ROOT / "exports"
LEDGER_PATH = EXPORT_DIR / "ledger.jsonl"

RESULT_DIR = ROOT / "result"
SUCCESS_LEDGER_PATH = RESULT_DIR / "success.jsonl"     # 成功账号（append-only，按 key 去重）
KEYS_TXT_PATH = RESULT_DIR / "keys.txt"                # email----api_key----api_key_id
KEYS_JSON_PATH = RESULT_DIR / "keys_verified.json"     # 机器可读的验收结果


def validate(*, need_tempmail: bool = True) -> list[str]:
    """返回缺失的必需配置项。

    刻意不在 import 时抛错 —— 那样连 --help 都跑不起来。
    """
    missing: list[str] = []
    if need_tempmail:
        for key, val in (("TEMPMAIL_BASE", TEMPMAIL_BASE),
                         ("TEMPMAIL_ADMIN_KEY", TEMPMAIL_ADMIN_KEY),
                         ("TEMPMAIL_DOMAIN", TEMPMAIL_DOMAIN)):
            if not val:
                missing.append(key)
    return missing


def validate_cf() -> list[str]:
    """Cloudflare API 三项。只有诊断探针需要，主流程不需要 —— 所以单独一个函数。"""
    return [k for k, v in (("CF_API_TOKEN", CF_API_TOKEN),
                           ("CF_ACCOUNT_ID", CF_ACCOUNT_ID),
                           ("CF_D1_ID", CF_D1_ID)) if not v]
