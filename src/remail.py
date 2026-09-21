"""Remail 邮箱接口聚合客户端（remail.aishop6.com）。

与 `tempemail.TempMailClient` **并列的第二个邮箱后端**，接口刻意对齐
（`create_mailbox` / `list_mails` / `wait_for_mail`），使 `stages` 层可以无感切换。

🔴 与 CF Worker 的四个根本差异 —— 写这个模块前必须记住：

1. **建邮箱 = 真实下单花钱**。`POST /v1/open/orders` 会扣积分。
   TypeSafe 项目（id=155）下 `domain` 类型最便宜：code 模式 0.01、purchase 0.02。
   它**不是**"免费建一个地址"，所以别在探针里随手循环调用。
2. **取件凭证是 per-order 的**。`GET /v1/pickup` **不认 API Key**，只认
   下单返回的 `email` + `serviceToken` 这一对 ⇒ token 必须**按邮箱**存起来。
3. **取件有频率限制**。响应里的 `fetch.nextFetchAllowedAt` 是下次允许取件的时间。
   （CF Worker 那边是"读信当场读走、retention 100 行"，机制完全不同。）
4. **邮件正文是"预览"**。列表返回的 `bodyPreview` 可能被截断；完整正文要拿
   `MailMessage.id` 再调 `GET /v1/pickup/messages/{id}`。
   注意：**聚合结果不返回 id**，那时只能靠 `bodyPreview`。

规格真源：`exports/remail_openapi.json`（从 `GET /openapi.json` 拉的 OpenAPI 3.0.3）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

import requests

from . import config
from .tempemail import Mail, Stats


class RemailError(RuntimeError):
    pass


def iso_to_ms(s: str | None) -> int:
    """ISO-8601（含 `Z`）→ 毫秒时间戳。

    `Mail.received_at` 的既有约定是**毫秒**（CF Worker 的 `received_at` 就是毫秒），
    而 Remail 返回的是 ISO 字符串 ⇒ 必须在边界上换算，否则 `since_ms` 过滤会全错
    （秒/毫秒混用会差 1000 倍，这类错算起来不会报错、只是永远筛不出邮件）。
    """
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(dt.timestamp() * 1000)


class RemailClient:
    """Remail 后端。**每次 `create_mailbox()` 都是一次真实下单。**"""

    def __init__(self, base: str | None = None, api_key: str | None = None,
                 session: requests.Session | None = None):
        self.base = (base or config.REMAIL_BASE).rstrip("/")
        self.api_key = api_key if api_key is not None else config.REMAIL_API_KEY
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
        self.stats = Stats()
        #: 进程内锁：`_tokens` 是可变 dict，多线程同时写会坏掉。
        #: ⚠️ 它**只护进程内** —— 跨进程靠 append-only 文件（见 `_save_state`）。
        self._lock = threading.Lock()
        #: `{email: serviceToken}` —— 取件凭证是 per-order 的，必须按邮箱存。
        self._tokens: dict[str, str] = {}
        #: `{email: orderNo}` —— 留档，便于对账与退款。
        self._orders: dict[str, str] = {}
        #: 服务端给的"下次允许取件时间"（毫秒）。0 = 无限制。
        self._next_fetch_ms: int = 0
        # 🔴 从磁盘恢复历史 token —— **不是可选的便利功能**，是 `resume` 的前提。
        self.restored = self._load_state()

    # ── 取件凭证台账（跨进程）───────────────────────────────────────────
    def _load_state(self) -> int:
        """从 `config.REMAIL_STATE_PATH` 恢复 `{email: serviceToken}`，返回恢复条数。

        🔴 为什么必须有它：`GET /v1/pickup` **不认 API Key**，只认下单返回的
        `email` + `serviceToken`。token 只存在于**下单那个进程的内存**里 ⇒
        `resume_pending.py` / `relogin_pending.py` 另起进程补跑时，邮箱在台账里、
        token 却没有，全部报"没有 serviceToken"，看起来像"站点坏了"。
        CF Worker 后端没有这个问题（一个全局 admin key 走天下），所以这是
        Remail **独有**的坑，不落盘就等于这个后端只能一次跑完、不能补跑。

        坏行跳过而不是整体失败：JSONL 是 append-only，断电可能留半行；
        为了半行垃圾把几百条凭证全丢掉，是拿小概率事件换大损失。
        """
        path = config.REMAIL_STATE_PATH
        if not path.is_file():
            return 0
        n = 0
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            email = str(d.get("email") or "").strip()
            token = str(d.get("token") or "").strip()
            if not email or not token:
                continue
            # 末行胜出：同一邮箱被重新下单时会拿到新 token，新的必须覆盖旧的。
            self._tokens[email] = token
            self._orders[email] = str(d.get("orderNo") or "")
            n += 1
        return n

    def _save_state(self, email: str, suffix: str = "") -> None:
        """把一条 `{email, token, orderNo}` 追加进凭证台账。

        🔴 刻意用 **append-only JSONL**，不用"读整个 JSON → 改 → 写回"：
        并发 worker 各持一个 `RemailClient`（`runner._clone()` 给的），
        读改写会**互相覆盖**（最后写的赢，前面 worker 的订单凭证全丢，
        且不报错 —— 只会在补跑时才暴露成"没有 serviceToken"）。
        追加只受进程内锁保护，跨进程天然安全。
        """
        path = config.REMAIL_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "email": email,
            "token": self._tokens.get(email, ""),
            "orderNo": self._orders.get(email, ""),
            "suffix": suffix,
            "at": int(time.time() * 1000),
        }
        # newline="" —— Windows 上必须显式关掉 `\n` -> `\r\n` 翻译（项目铁律）。
        with self._lock:
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _token_for(self, email: str) -> str:
        """取某邮箱的 serviceToken，取不到就抛**可诊断**的错误。

        🔴 抽成单点的理由：这是本项目最容易被误诊的一条失败。
        三种完全不同的原因，处置各不相同 ——
          ① 该地址是 **CF Worker 后端**建的（换后端跑批时最常见，不是故障）；
          ② 凭证台账文件丢了 / 被清空（`result/` 被误清理）；
          ③ 该地址在本 Remail 账号下**确实没下过单**。
        只报一句"没有 token"会让人去查 Remail 的 API，而真因往往在我们这边。
        文案必须把这三条都摆出来，否则排查方向直接跑偏。
        """
        token = self._tokens.get(email)
        if not token:
            raise RemailError(
                f"没有 {email} 的 serviceToken（本次已从台账恢复 {self.restored} 条）。"
                f"三种可能：①该地址是 CF Worker 后端建的（换后端跑批时最常见）；"
                f"②凭证台账 {config.REMAIL_STATE_PATH} 丢了或被清空；"
                f"③该地址在本 Remail 账号下确实没下过单"
            )
        return token

    # ── 底层 ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, retries: int = 4,
                 backoff: float = 0.8, **kw) -> requests.Response:
        """5xx 重试、4xx 立刻失败（与 `tempemail` 同一套语义）。

        ⚠️ 4xx 的错误体必须带出来：Remail 把"余额不足 / 库存不足 / token 失效"
        都放在响应体里，只报状态码等于把可诊断信息丢掉。
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, f"{self.base}{path}", timeout=30, **kw)
            except requests.RequestException as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise RemailError(f"网络失败 {path}: {exc}") from exc

            if 500 <= r.status_code < 600:
                self.stats.http_5xx += 1
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise RemailError(
                    f"Remail 持续 5xx（{self.stats.http_5xx} 次，最近 HTTP {r.status_code}）"
                    f"—— 不是邮件没到，是读不出来"
                )
            if 400 <= r.status_code < 500:
                raise RemailError(f"HTTP {r.status_code} {path}: {r.text[:200]}")
            return r
        raise RemailError(f"重试耗尽: {path} ({last})")

    def _auth(self, **extra: str) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        h.update(extra)
        return h

    # ── 健康检查 / 账户 ────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        """Remail 没有 `/health`，用「查 API Key」当体检端点。

        返回 `{"apiKey": {"id":…, "enabled":…, "balance":…}, …}` ——
        **余额就在这个响应里**，所以不需要再包一个 `balance()` 方法。
        （2026-09-21 审计：那个薄包装零调用点，已删；`cmd_doctor` 直接读这里。）
        """
        return self._request("GET", "/v1/open/apikey/profile",
                             headers=self._auth()).json()

    # ── 下单（= 建邮箱）────────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None,
                       *, wait_active: float = 30.0) -> str:
        """下单买一个邮箱，返回**交付地址**（`deliveryEmail`）。

        🔴 **这是真实下单，会扣积分。** 想先确认成本请读 `config.REMAIL_*` 的注释。

        `domain` 传的是 `emailSuffix`（如 `domain` / `outlook.com` / `icloud.com`），
        **不是完整邮箱地址** —— 服务端明确"不接受完整邮箱地址"。
        """
        suffix = domain or config.REMAIL_EMAIL_SUFFIX
        body = {"projectId": config.REMAIL_PROJECT_ID, "emailSuffix": suffix}
        r = self._request(
            "POST", "/v1/open/orders",
            params={"serviceMode": config.REMAIL_SERVICE_MODE},
            headers=self._auth(**{
                "Content-Type": "application/json",
                # 幂等键**必填**。每次下单都要新订单 ⇒ 用 uuid4，不要复用。
                "Idempotency-Key": str(uuid.uuid4()),
            }),
            data=json.dumps(body),
        )
        order = r.json() or {}
        email = str(order.get("deliveryEmail") or "").strip()
        token = str(order.get("serviceToken") or "").strip()
        if not email or not token:
            raise RemailError(
                f"下单未返回可用邮箱: orderNo={order.get('orderNo')} "
                f"status={order.get('status')} failure={order.get('failureCode')} "
                f"body={json.dumps(order, ensure_ascii=False)[:200]}"
            )
        self._tokens[email] = token
        self._orders[email] = str(order.get("orderNo") or "")
        # 🔴 下单成功**立刻**落盘。放在这里而不是 `create_mailbox` 返回前 ——
        #    下面还有一段"等 active"的轮询，那期间进程被杀（Ctrl-C / 超时）
        #    就白花了这一单的钱：地址有了、token 没了 ⇒ 永远取不了那封信。
        #    落盘后再等，最坏情况是"订单状态未知但凭证在手"，可继续取件。
        self._save_state(email, suffix)

        # 下单可能是异步的（`paid` → `active` 才真正开始收件）。
        # 但**判据要选终态**：能不能取到邮件。所以这里只做有限等待，
        # 拿不到 active 也不直接判失败 —— 交给 `wait_for_mail` 去暴露真实结果。
        if order.get("status") not in ("active", "completed"):
            self._await_active(email, timeout=wait_active)
        return email

    def _await_active(self, email: str, *, timeout: float) -> str:
        order_no = self._orders.get(email, "")
        if not order_no:
            return ""
        deadline = time.time() + timeout
        status = ""
        while time.time() < deadline:
            try:
                o = self._request("GET", f"/v1/open/orders/{order_no}",
                                  headers=self._auth()).json() or {}
            except RemailError:
                return status
            status = str(o.get("status") or "")
            if status in ("active", "completed"):
                return status
            if status in ("failed", "refunded", "closed"):
                raise RemailError(
                    f"订单 {order_no} 终态失败: status={status} "
                    f"failure={o.get('failureCode')}"
                )
            time.sleep(1.0)
        return status

    # ── 取件（= 收信）──────────────────────────────────────────────────
    def list_mails(self, email: str | None = None,
                   limit: int | None = None) -> list[Mail]:
        """取件。**必须给 email** —— Remail 没有"扫全窗口"的端点。

        响应结构：`{items: [MailMessage], fetch: FetchState}`。
        `MailMessage.bodyPreview` 只是预览，够不够用取决于站点邮件形态
        （TypeSafe 的确认邮件是短文本 + 魔法链接，实测够用；不够时再调
        `GET /v1/pickup/messages/{id}` 取全文）。
        """
        if not email:
            raise RemailError("Remail 取件必须指定 email（没有全窗口扫描端点）")
        token = self._token_for(email)

        self.stats.polls += 1
        r = self._request("GET", "/v1/pickup",
                          params={"email": email, "token": token})
        body = r.json() or {}
        fetch = body.get("fetch") or {}
        self._next_fetch_ms = iso_to_ms(fetch.get("nextFetchAllowedAt"))

        out: list[Mail] = []
        for m in (body.get("items") or []):
            out.append(Mail(
                id=str(m.get("id") or ""),
                to=str(m.get("recipient") or email),
                sender=str(m.get("sender") or ""),
                subject=str(m.get("subject") or ""),
                body=str(m.get("bodyPreview") or ""),
                received_at=iso_to_ms(m.get("receivedAt")),
            ))
        out.sort(key=lambda x: x.received_at)
        return out

    def fetch_body(self, email: str, message_id: str) -> str:
        """读单封邮件全文（`bodyPreview` 不够用时）。"""
        token = self._token_for(email)
        r = self._request("GET", f"/v1/pickup/messages/{message_id}",
                          params={"email": email, "token": token})
        m = r.json() or {}
        return str(m.get("body") or m.get("bodyHtml") or m.get("bodyPreview") or "")

    def _hydrate(self, email: str, m: Mail) -> Mail:
        """把 `bodyPreview` 换成邮件**全文**（就地改 `m.body`，返回同一个 `m`）。

        🔴 为什么必须有（2026-09-21 首次实测就踩到）：列表接口返回的
        `bodyPreview` 是**截断的预览**，而 TypeSafe 的魔法链接在正文中后段
        —— preview 里必然被切掉。实测的失败链：

            下单 ok → 邮箱 chadharmon6645@outlook.com
            发信 ok → 6.98s 收到邮件
            → `魔法链接邮件里没找到链接`

        最后那句把真因（**我们只读了预览**）说成了"站点没发链接"，
        排查方向完全错 —— 这属于"不报错、只是数据变错"那一类。

        ⚠️ 只在**匹配成功后**调一次，**不**在 `list_mails` 里对每封都调：
        轮询是每秒一次的，N+1 请求会把取件配额打爆（`nextFetchAllowedAt`
        会一路往后推）。

        ⚠️ 取全文失败时**显式抛错**，不静默退回 preview：退回的话错误会重新
        变成"邮件里没找到链接"，而真因是"全文接口不可用"（例如该商品模式不
        支持），两者的处置完全不同。
        """
        if not m.id:
            raise RemailError(
                f"邮件没有 id，无法取全文（subject={m.subject!r}）—— "
                f"只能靠被截断的 bodyPreview，取不到正文里的链接"
            )
        body = self.fetch_body(email, m.id)
        if body:
            m.body = body
        return m

    def wait_for_mail(self, email: str, match: Callable[[Mail], bool], *,
                      timeout: float = 300.0, interval: float = 2.0,
                      since_ms: int | None = None) -> Mail | None:
        """轮询等一封满足条件的邮件。

        与 `TempMailClient.wait_for_mail` 同签名 —— 这是两个后端能被 stages
        层互换的关键。区别只在**限流**：Remail 会在响应里给
        `fetch.nextFetchAllowedAt`，轮询间隔要尊重它，否则白打请求。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.list_mails(email=email):
                if since_ms is not None and m.received_at < since_ms:
                    continue
                if match(m):
                    # 🔴 匹配用 `bodyPreview` 就够（规则表按 subject/sender 判），
                    #    但**返回前必须换成全文** —— 正文里的链接会被 preview 切掉。
                    #    见 `_hydrate()` 的实测记录。
                    return self._hydrate(email, m)
            # 尊重服务端的取件节流；但仍要有上限，别被一个远期时间戳卡死。
            now_ms = int(time.time() * 1000)
            wait = max(interval, (self._next_fetch_ms - now_ms) / 1000.0)
            time.sleep(min(wait, 10.0))
        return None

    def scan_all(self, limit: int = 100) -> list[Mail]:
        """Remail **没有**全窗口扫描端点 —— 这是与 CF Worker 的能力差。

        保留这个方法只为让两个后端接口形状一致；真调到就显式炸，
        不要静默返回空列表（那会让调用方以为"窗口里没有邮件"）。
        """
        raise RemailError(
            "Remail 不支持扫全窗口（没有 /admin/all 对应端点）；"
            "取件必须按 email 走 list_mails(email=...)"
        )
