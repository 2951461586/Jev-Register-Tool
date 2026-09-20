"""CF Temp Email Worker 客户端。

关键设计（都是踩过坑换来的）：

- 收信走 **按收件人索引的端点** `/api/inbox?email=`，不走 `/admin/all`（拉最新 N 条再自己筛）。
  该 Worker 被同机其它项目共用（实测被 OpenXLab 激活邮件刷屏），
  retention 是"全表 100 个 id 的窗口"，拉列表会把我们的邮件挤出窗口。
- **5xx 要重试**：那是"服务端现在读不出来"，不是"邮件没到"。4xx 才立刻失败。
- 每次轮询打了几次接口、其中几次 5xx 都要计数并上报 —— 否则配额类故障只能靠猜。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from . import config


class TempMailError(RuntimeError):
    pass


@dataclass
class Stats:
    polls: int = 0
    http_5xx: int = 0
    http_4xx: int = 0
    created: int = 0
    last_error: str = ""


@dataclass
class Mail:
    id: str
    to: str
    sender: str
    subject: str
    body: str
    received_at: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """主题 + 正文的合并文本，供正则抽取。"""
        return f"{self.subject}\n{self.body}"

    @property
    def recipient(self) -> str:
        """收件人。`to` 是内置名，用 `recipient` 读起来不歧义。"""
        return self.to


class TempMailClient:
    def __init__(self, base: str | None = None, admin_key: str | None = None,
                 session: requests.Session | None = None):
        self.base = (base or config.TEMPMAIL_BASE).rstrip("/")
        self.admin_key = admin_key if admin_key is not None else config.TEMPMAIL_ADMIN_KEY
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
        self.stats = Stats()

    # ── 底层 ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, retries: int = 4,
                 backoff: float = 0.8, **kw) -> requests.Response:
        """5xx 重试、4xx 立刻失败。"""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, f"{self.base}{path}", timeout=30, **kw)
            except requests.RequestException as exc:
                last = exc
                self.stats.last_error = f"network: {exc}"
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise TempMailError(f"网络失败 {path}: {exc}") from exc

            if 500 <= r.status_code < 600:
                self.stats.http_5xx += 1
                self.stats.last_error = f"HTTP {r.status_code} {path}"
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise TempMailError(
                    f"邮箱服务持续 5xx（{self.stats.http_5xx} 次，最近 HTTP {r.status_code}）"
                    f"—— 不是邮件没到，是读不出来"
                )
            if 400 <= r.status_code < 500:
                self.stats.http_4xx += 1
                raise TempMailError(f"HTTP {r.status_code} {path}: {r.text[:200]}")
            return r
        raise TempMailError(f"重试耗尽: {path} ({last})")

    def health(self) -> dict[str, Any]:
        r = self._request("GET", "/health")
        return r.json()

    # ── 建邮箱 ────────────────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None) -> str:
        body: dict[str, Any] = {}
        if domain or config.TEMPMAIL_DOMAIN:
            body["domain"] = domain or config.TEMPMAIL_DOMAIN
        r = self._request(
            "POST", "/api/mailboxes",
            headers={"Authorization": f"Bearer {self.admin_key}",
                     "Content-Type": "application/json"},
            data=json.dumps(body),
        )
        data = r.json()
        emails = data.get("emails") or []
        if not emails:
            raise TempMailError(f"建邮箱未返回地址: {data}")
        self.stats.created += 1
        return emails[0]

    # ── 收信 ──────────────────────────────────────────────────────────
    def list_mails(self, email: str | None = None, limit: int | None = None) -> list[Mail]:
        """给了 email 走索引端点（读 0~2 行）；不给则退回 /admin/all（保留旧路径）。"""
        if email:
            self.stats.polls += 1
            r = self._request("GET", "/api/inbox", params={"email": email})
            msgs = (r.json() or {}).get("messages") or []
        else:
            self.stats.polls += 1
            params = {"limit": limit or 20}
            r = self._request("GET", "/admin/all", params=params,
                              headers={"Authorization": f"Bearer {self.admin_key}"})
            body = r.json() or {}
            msgs = body.get("messages") if isinstance(body, dict) else body
            msgs = msgs or []

        out: list[Mail] = []
        for m in msgs:
            out.append(Mail(
                id=str(m.get("id", "")),
                to=m.get("to_address") or m.get("to") or "",
                sender=m.get("from_address") or m.get("from") or "",
                subject=m.get("subject") or "",
                body=m.get("body") or m.get("text") or m.get("raw_text") or "",
                received_at=int(m.get("received_at") or 0),
                raw=m,
            ))
        out.sort(key=lambda x: x.received_at)
        return out

    def wait_for_mail(self, email: str, match: Callable[[Mail], bool], *,
                      timeout: float = 300.0, interval: float = 2.0,
                      since_ms: int | None = None) -> Mail | None:
        """轮询等一封满足条件的邮件。

        `since_ms` 用于跳过历史邮件 —— 同一个地址可能已经收过旧邮件。
        读信必须当场读走：该 Worker 的 retention 是"全表 100 行"，
        我们自己跑批次时写入速率可达 ~600 封/时，未读邮件存活仅约 10 分钟。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.list_mails(email=email):
                if since_ms is not None and m.received_at < since_ms:
                    continue
                if match(m):
                    return m
            time.sleep(interval)
        return None

    def first_mail_matching(self, email: str, match: Callable[[Mail], bool]) -> Mail | None:
        for m in self.list_mails(email=email):
            if match(m):
                return m
        return None

    def scan_all(self, limit: int = 100) -> list[Mail]:
        """扫整个窗口（不按收件人过滤）。

        只用于"监听获批邮件"这类**不知道收件人是谁**的场景：
        申请可能在别处（网页 UI）提交，地址不在我们台账里。

        ⚠️ 这条通路会占满窗口读取额度，**不要**用它做常规收信 ——
        常规收信一律走 `list_mails(email=...)` 的索引端点。
        另：服务端对该参数有硬上限，实测 `limit=200/500/1000` 都只返回 100 条。
        """
        return self.list_mails(email=None, limit=limit)
