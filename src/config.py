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

# ── Remail 邮箱接口聚合（remail.aishop6.com）─────────────────────────────
# 与上面的 CF Worker **并列的第二个邮箱后端**：一个 API 切换多种邮箱
# （outlook / gmail / icloud / proto / 自有域名）。2026-09-21 接入。
#
# 🔴 与 CF Worker 的**根本差异**（设计 `src/remail.py` 时必须记住）：
#   下单 `POST /v1/open/orders` 用 Bearer API Key，返回 `deliveryEmail` +
#   `serviceToken`；而取件 `GET /v1/pickup` **不用 API Key**，只认
#   `email` + `token` 这一对 ⇒ token 必须按邮箱存起来，不能只存全局 key。
#
# 规格真源：`exports/remail_openapi.json`（从 `/openapi.json` 拉的 OpenAPI 3.0.3）。
REMAIL_BASE = os.getenv("REMAIL_BASE", "")
REMAIL_API_KEY = os.getenv("REMAIL_API_KEY", "")
# TypeSafe 在 Remail 上的项目 id（`GET /v1/open/projects` 里 name='TypeSafe' 那条）。
# 这不是凭据，是公开商品编号，可以留默认值。
REMAIL_PROJECT_ID = int(os.getenv("REMAIL_PROJECT_ID") or 155)
# 商品后缀（`emailSuffix`）。⚠️ **不是完整邮箱地址**，服务端明确不接受。
#
# 🔴 2026-09-21 实测：**`domain` 商品已无货**，下单直接
#    `HTTP 422 {"message":"Insufficient inventory."}`。
#    `GET /v1/open/projects/155` 当天各商品的有货情况：
#      microsoft      code 8.00   purchase 10.00   库存 4,539,067（24 个后缀）
#      gmail_variant  code 8.00   purchase 10.00   库存 1,000,534,521
#      proto          code 10.00  purchase 20.00   库存 5        ← 几乎等于没货
#      gmail          code **关闭**  purchase 500.00  库存 6
#      icloud         code **关闭**  purchase 10.00   库存 221,677
#      domain         code 0.01   purchase 0.02    库存 **0**   ← 曾经最便宜，现已无货
#    ⇒ 默认取 `outlook.com`（microsoft 商品下库存最大的后缀，4,008,800）。
#      刻意用**具体后缀**而不是加权随机的 `outlook`：注册场景要的是可控、主流，
#      而不是被随机分到 `outlook.com.gr` 这类冷门域名（对站点风控的影响未知）。
#    ⚠️ **成本量级变了**：8 积分/单，是原来 domain 的 **800 倍**。跑批前先看余额
#      （`--doctor --mail-backend remail` 会打出来），别按 0.01/单 的旧印象估预算。
#    ⚠️ 库存是**会变**的（`domain` 就从有货变成 0）⇒ 报"库存不足"时先去
#      `GET /v1/open/projects/155` 看当天哪个后缀有货，不要改代码猜。
REMAIL_EMAIL_SUFFIX = os.getenv("REMAIL_EMAIL_SUFFIX", "outlook.com")
# `code` = 短效接码（10 分钟窗口，更便宜）；`purchase` = 长效购买。
REMAIL_SERVICE_MODE = os.getenv("REMAIL_SERVICE_MODE", "code")

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
# 纯 api_key，一行一个（`keys.txt` 的无元数据版：不带邮箱、不带 key_id）。
# 面向"直接把 key 灌进别的系统"的场景；与 keys.txt 由**同一次** verify 写出，故集合恒等。
APIKEYS_TXT_PATH = RESULT_DIR / "apikeys.txt"

# Remail 的**取件凭证台账**（JSONL，append-only）。
#
# 🔴 为什么必须落盘：`GET /v1/pickup` **不认 API Key**，只认下单时返回的
#    `email` + `serviceToken` 这一对。而 `serviceToken` 只在**下单那个进程的内存**里
#    ⇒ `resume_pending.py` / `relogin_pending.py` 这类**另起进程**的补跑入口
#    会拿着台账里的邮箱却取不了信，报"没有 serviceToken"。
#    CF Worker 那边没这个问题（全局 admin key 就够），所以这是 Remail **独有**的坑。
#
# ⚠️ 放 `result/` 下：它含 per-order 凭证，必须被 `.gitignore` 的 `result/` 覆盖。
# ⚠️ 它是 `result/` 下**唯一非交付物**的文件 —— 不要把它混进交付清单。
REMAIL_STATE_PATH = RESULT_DIR / "remail_orders.jsonl"


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


def validate_remail() -> list[str]:
    """Remail 后端两项。**只有选了 Remail 后端才需要** —— 所以不并进 `validate()`，
    否则用 CF Worker 跑批时会被无谓地拦下来。"""
    return [k for k, v in (("REMAIL_BASE", REMAIL_BASE),
                           ("REMAIL_API_KEY", REMAIL_API_KEY)) if not v]
