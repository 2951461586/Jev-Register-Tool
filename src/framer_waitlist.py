"""Framer 表单提交（TypeSafe waitlist 申请），含 Proof-of-Work。

PoW 算法从 `framer.CwAF0H4T.mjs` 的 `ff()` / `df()` 里读出来的，不是猜的：

    salt = "framer"          (FE)
    difficulty = 3           (IE)   -> sha256 hex 需要以 "000" 开头
    tokenLength = 30         (LE)
    maxTime = 10000 ms       (RE)

    secret = f"{int(time.time()*1000)}:{random_alnum(30)}"
    直到 sha256_hex(salt + secret).startswith("000" * 1) 即以 3 个 '0' 开头

三个请求头缺一不可（少了会拿到通用的表单错误）：
    Framer-Site-Id     : <siteId>
    Framer-POW         : <secret>
    Framer-Form-Fields : <表单字段名,逗号连接>

`Framer-Form-Fields` 的算法来自 `hf(n, t)`：
    n = 表单内具名元素顺序（排除 __framer* 前缀、disabled、无名）
    t = FormData 的 key 集合
    -> [...n, ...(t.keys() - n)].map(encodeURIComponent).join(",")
实测结果：email,__framer_0,__framer_1,__framer_2,__framer_3,__framer_4,__framer_5
"""

from __future__ import annotations

import hashlib
import random
import string
import time

import requests

from . import config

ALNUM = string.ascii_uppercase + string.ascii_lowercase + string.digits


def pow_secret(*, salt: str = config.POW_SALT,
               difficulty: int = config.POW_DIFFICULTY,
               token_length: int = config.POW_TOKEN_LENGTH,
               max_time_ms: int = config.POW_MAX_TIME_MS) -> tuple[str, str]:
    """返回 (secret, hash)。secret 形如 `<ms>:<30 位随机字母数字>`。"""
    prefix = "0" * difficulty
    deadline = time.time() + max_time_ms / 1000.0
    attempts = 0
    while time.time() < deadline:
        token = "".join(random.choice(ALNUM) for _ in range(token_length))
        secret = f"{int(time.time() * 1000)}:{token}"
        digest = hashlib.sha256((salt + secret).encode()).hexdigest()
        attempts += 1
        if digest.startswith(prefix):
            return secret, digest
    raise RuntimeError(
        f"PoW 在 {max_time_ms}ms 内未解出（尝试 {attempts} 次，difficulty={difficulty}）"
    )


def form_fields_header(email_field: str = "email") -> str:
    """复刻 hf(pf(form), formData) 的输出。"""
    dom_order = [email_field]
    extra = [f"__framer_{i}" for i in range(6)]
    return ",".join(requests.utils.quote(x, safe="") for x in [*dom_order, *extra])


def submit(email: str, *, session: requests.Session | None = None,
           timeout: float = 40.0) -> dict:
    """提交 waitlist 申请。成功时 Framer 返回 201 / body `null`。"""
    s = session or requests.Session()
    secret, digest = pow_secret()

    files = {
        "email": (None, email),
        "__framer_0": (None, "[]"),
        "__framer_1": (None, str(config.HONEYPOT_FIELD_COUNT)),
        "__framer_2": (None, "0"),
        "__framer_3": (None, config.HONEYPOT_VERSION),
        "__framer_4": (None, config.FRAMER_SITE_ID),
        "__framer_5": (None, f"{random.uniform(3.0, 15.0):.2f}"),
    }
    headers = {
        "User-Agent": config.UA,
        "Origin": "https://typesafe.ai",
        "Referer": config.FRAMER_REFERER,
        "Accept": "*/*",
        "Framer-Site-Id": config.FRAMER_SITE_ID,
        "Framer-POW": secret,
        "Framer-Form-Fields": form_fields_header(),
    }
    r = s.post(config.FRAMER_SUBMIT_URL, files=files, headers=headers, timeout=timeout)
    return {
        "status": r.status_code,
        "ok": r.status_code in (200, 201),
        "body": r.text[:400],
        "pow_secret": secret,
        "pow_hash": digest,
    }
