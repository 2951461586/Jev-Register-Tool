#!/usr/bin/env python
"""干净实验：全新邮箱 → 申请 → **先完成确认邮件** → 再提交认证回调。

目的：验证"必须先走 Welcome to TypeSafe — confirm your email，再注册登录"这个假设。

判据：
    200                -> 假设成立（确认这一步是解锁条件）
    403 Access restricted -> 与确认无关，仍是邀请制白名单
    401 Authentication failed -> 链接已被消耗/过期，实验无效，需重做

用法：
    python tools/probe_confirm_flow.py [--count 1] [--domain example-mail.test]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# 本文件在 tools/probes/ —— 先把 tools/ 加进 path 才能 import 到 _bootstrap，
# 再由 _bootstrap 按标记文件定位仓库根（不靠数层级）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

import requests  # noqa: E402

from src import config  # noqa: E402
from src.framer_waitlist import submit as framer_submit  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import _parse_js_object  # noqa: E402

LINK_RE = re.compile(r"https://login\.typesafe\.ai/v1/magic_links/redirect\?[^\s\"<>\)\]]+")


def hr(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def confirm_flow(email: str, *, verbose: bool = True) -> dict:
    """返回 {"status": int, "body": str, "stage": str, ...}"""
    out: dict = {"email": email}
    s = requests.Session()
    s.headers.update({"User-Agent": config.UA})

    # ── 1. 拿确认邮件 ────────────────────────────────────────────────
    mail = TempMailClient()
    since = int(time.time() * 1000) - 5_000
    m = mail.wait_for_mail(email, lambda x: "confirm your email" in x.subject.lower(),
                           timeout=180.0, interval=2.0, since_ms=since)
    if m is None:
        out.update(stage="wait_mail", status=0, body="未收到确认邮件")
        return out
    u = LINK_RE.search(m.body or "")
    if not u:
        out.update(stage="parse_link", status=0, body="确认邮件里没有魔法链接")
        return out
    link = u.group(0)
    out["link"] = link
    if verbose:
        print(f"  [1] 确认邮件 id={m.id}，链接 token={link[-20:]}")

    # ── 2. GET 落地页 → dfp 参数 ─────────────────────────────────────
    page = s.get(link, timeout=40)
    mm = re.search(r"xhr\.send\(JSON\.stringify\((\{.*?\})\)\);", page.text, re.S)
    if not mm:
        out.update(stage="landing", status=page.status_code,
                   body="落地页没有 dfp 参数（链接可能已过期）")
        return out
    payload = _parse_js_object(mm.group(1))
    payload["telemetry_id"] = ""
    out["stytch_user_id"] = payload.get("user_id", "")
    if verbose:
        print(f"  [2] 落地页 HTTP {page.status_code}，stytch user_id={payload.get('user_id')}")

    # ── 3. POST dfp 交换 ─────────────────────────────────────────────
    r3 = s.post("https://login.typesafe.ai/v1/magic_links/redirect/dfp", json=payload,
                headers={"Accept": "application/json",
                         "Content-Type": "application/json;charset=UTF-8",
                         "Origin": "https://login.typesafe.ai",
                         "Referer": link}, timeout=40)
    redirect_url = ""
    try:
        redirect_url = (r3.json() or {}).get("redirect_url", "")
    except ValueError:
        pass
    if not redirect_url:
        out.update(stage="dfp", status=r3.status_code, body=r3.text[:300])
        return out
    out["redirect_url"] = redirect_url
    if verbose:
        print(f"  [3] dfp HTTP {r3.status_code} -> {redirect_url[:120]}")

    tk = re.search(r"[?&]token=([^&]+)", redirect_url)
    tt = re.search(r"[?&]stytch_token_type=([^&]+)", redirect_url)
    wl = re.search(r"[?&]waitlist=([^&]+)", redirect_url)
    if not tk:
        out.update(stage="token", status=0, body="redirect_url 里没有 token")
        return out

    # ── 4. 直接 POST /api/auth/callback（**不**先 GET 控制台 URL） ────
    body = {
        "token": requests.utils.unquote(tk.group(1)),
        "tokenType": tt.group(1) if tt else "magic_links",
        "returnTo": None, "preferredOrgId": None, "inviteId": None, "oauthState": None,
        # 用 URL 里带回来的 waitlist 值，而不是我们自己拼的
        "waitlistEmail": requests.utils.unquote(wl.group(1)) if wl else email,
    }
    r4 = s.post(f"{config.SITE_ORIGIN}/api/auth/callback", json=body,
                headers={"Origin": config.SITE_ORIGIN,
                         "Referer": redirect_url,
                         "Accept": "application/json"}, timeout=40)
    out["status"] = r4.status_code
    out["body"] = r4.text[:300]
    out["stage"] = "auth_callback"
    out["waitlistEmail_used"] = body["waitlistEmail"]
    if verbose:
        print(f"  [4] /api/auth/callback HTTP {r4.status_code} {r4.text[:200]}")

    # ── 5. 若 200，继续看 /api/me ────────────────────────────────────
    if r4.status_code == 200:
        r5 = s.get(f"{config.SITE_ORIGIN}/api/me",
                   headers={"Accept": "application/json"}, timeout=30)
        out["me_status"] = r5.status_code
        out["me"] = r5.text[:400]
        if verbose:
            print(f"  [5] /api/me HTTP {r5.status_code} {r5.text[:200]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--domain", default="")
    ap.add_argument("--email", default="", help="用已有邮箱重试（需重新申请）")
    args = ap.parse_args()

    mail = TempMailClient()
    rows = []
    for i in range(args.count):
        email = args.email or mail.create_mailbox(args.domain or None)
        hr(f"[{i + 1}/{args.count}] {email}")
        sub = framer_submit(email)
        print(f"  [0] framer submit HTTP {sub['status']} ok={sub['ok']}")
        if not sub["ok"]:
            rows.append({"email": email, "stage": "framer", "status": sub["status"],
                         "body": sub["body"][:200]})
            continue
        rows.append(confirm_flow(email))

    hr("汇总")
    for r in rows:
        print(f"  {r['email']:<42} stage={r.get('stage'):<14} "
              f"status={r.get('status')} {str(r.get('body'))[:90]}")
    print()
    st = {r.get("status") for r in rows}
    if 200 in st:
        print("  ⇒ 200：确认邮件这一步确实解锁了下游 —— 假设成立")
    elif 403 in st:
        print("  ⇒ 403 Access restricted：与确认无关，仍是邀请制白名单")
    else:
        print(f"  ⇒ 未出现 200/403，拿到的是 {st} —— 实验无效或链路有其它问题")
    print("\n" + json.dumps(rows, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
