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
TEMPMAIL_BASE = os.getenv("TEMPMAIL_BASE", "https://temp-email-worker.example.workers.dev")
TEMPMAIL_ADMIN_KEY = os.getenv("TEMPMAIL_ADMIN_KEY", "")
TEMPMAIL_DOMAIN = os.getenv("TEMPMAIL_DOMAIN", "example-mail.test")

# ── TypeSafe 站点 ───────────────────────────────────────────────────────
SITE_ORIGIN = "https://console.typesafe.ai"
SITE_LOGIN = f"{SITE_ORIGIN}/login"
STYTCH_LOGIN_HOST = "https://login.typesafe.ai"

# ── Framer 表单（waitlist 申请） ─────────────────────────────────────────
FRAMER_SITE_ID = "f8111b111e9ce8d3e21d0f9765b6ce69c2a2f3f8b0f0744a436382f9f9a8231e"
FRAMER_FORM_ID = "ed4ea778-4721-4bcd-bedb-15f8de22eb0b"
FRAMER_SUBMIT_URL = f"https://api.framer.com/forms/v1/forms/{FRAMER_FORM_ID}/submit"
FRAMER_REFERER = "https://typesafe.ai/"

# PoW 参数，从 framer.CwAF0H4T.mjs 里读出来的常量（FE/IE/LE/RE）
POW_SALT = "framer"
POW_DIFFICULTY = 3          # sha256 十六进制前缀需要 3 个 '0'
POW_TOKEN_LENGTH = 30
POW_MAX_TIME_MS = 10_000

# 蜜罐字段（__framer_0..5）的静态取值，与浏览器实测一致
HONEYPOT_FIELD_COUNT = 11   # VE 列表长度
HONEYPOT_VERSION = "3"

# ── 邮箱匹配规则 ────────────────────────────────────────────────────────
MAIL_FROM_WAITLIST = "updates.typesafe.ai"     # 申请确认 / 账号就绪
MAIL_FROM_STYTCH = "typesafe.ai"               # Stytch 登录邮件（login@typesafe.ai）
SUBJ_WAITLIST_CONFIRM = "on the waitlist"
SUBJ_ACCOUNT_READY = "account is ready"
SUBJ_LOGIN = "confirm your email"

# ── 网络 ────────────────────────────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# ── 输出 ────────────────────────────────────────────────────────────────
EXPORT_DIR = ROOT / "exports"
LEDGER_PATH = EXPORT_DIR / "ledger.jsonl"
QUOTA_PATH = EXPORT_DIR / "quota.jsonl"


def validate(*, need_tempmail: bool = True) -> list[str]:
    """返回缺失的必需配置项。

    刻意不在 import 时抛错 —— 那样连 --help 都跑不起来。
    """
    missing: list[str] = []
    if need_tempmail and not TEMPMAIL_ADMIN_KEY:
        missing.append("TEMPMAIL_ADMIN_KEY")
    return missing
