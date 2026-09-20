#!/usr/bin/env python
"""探针：只走"确认邮件"这一步，把每一跳的原始响应都打出来。

用法：
    python tools/probe_confirm.py <email> [--link <magic_url>] [--callback]

不写台账、不发新邮件。--callback 才会去 POST /api/auth/callback。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 本文件在 tools/probes/ —— 先把 tools/ 加进 path 才能 import 到 _bootstrap，
# 再由 _bootstrap 按标记文件定位仓库根（不靠数层级）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

import requests  # noqa: E402

from src import config  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import _parse_js_object, _visible_text  # noqa: E402

LINK_RE = re.compile(r"https://login\.typesafe\.ai/v1/magic_links/redirect\?[^\s\"<>\)\]]+")


def hr(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def dump(r: requests.Response, *, body: int = 500) -> None:
    print(f"  HTTP {r.status_code}  {r.request.method} {r.url[:120]}")
    ct = r.headers.get("content-type", "")
    print(f"  content-type: {ct}")
    if r.history:
        print(f"  history: {[h.status_code for h in r.history]}")
    cookies = r.headers.get("set-cookie")
    if cookies:
        print(f"  set-cookie: {cookies[:200]}")
    loc = r.headers.get("location")
    if loc:
        print(f"  location: {loc[:300]}")
    txt = r.text
    print(f"  len={len(txt)}")
    if txt:
        print(f"  body[:{body}]: {txt[:body]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--link", default="", help="直接给魔法链接，不从邮箱读")
    ap.add_argument("--callback", action="store_true", help="最后 POST /api/auth/callback")
    args = ap.parse_args()

    link = args.link
    if not link:
        hr("0. 从邮箱窗口取确认邮件里的链接")
        c = TempMailClient()
        mails = c.list_mails(args.email, limit=100)
        cand = None
        for m in mails:
            if "confirm your email" in (m.subject or "").lower():
                u = LINK_RE.search(m.body or "")
                if u:
                    cand = (m, u.group(0))
                    break
        if not cand:
            print("  ✗ 窗口里没有 'confirm your email' 邮件（可能已被 retention 淘汰）")
            for m in mails:
                print(f"    - {m.id} | {m.subject}")
            return 1
        m, link = cand
        print(f"  ✓ id={m.id} subject={m.subject!r} at={m.received_at}")
    print(f"  link = {link[:200]}")

    s = requests.Session()
    s.headers.update({"User-Agent": config.UA})

    # ── 1. GET 落地页 ────────────────────────────────────────────────
    hr("1. GET 魔法链接落地页")
    page = s.get(link, timeout=40)
    dump(page, body=200)
    mm = re.search(r"xhr\.send\(JSON\.stringify\((\{.*?\})\)\);", page.text, re.S)
    if not mm:
        print("  ✗ 未找到 dfp 交换参数（链接可能已被用过/过期）")
        print("  —— 页面可见文案：", _visible_text(page.text)[:400])
        return 1
    payload = _parse_js_object(mm.group(1))
    print(f"  dfp payload 键: {sorted(payload)}")
    print(f"  payload = {json.dumps(payload, ensure_ascii=False)[:400]}")
    if "redirect_url" in payload:
        print(f"  ★ redirect_url = {payload['redirect_url'][:300]}")
    if "stytch_token_type" in payload:
        print(f"  ★ stytch_token_type = {payload['stytch_token_type']}")

    # ── 2. POST dfp 交换 ─────────────────────────────────────────────
    hr("2. POST /v1/magic_links/redirect/dfp")
    body = dict(payload)
    body["telemetry_id"] = ""
    r2 = s.post("https://login.typesafe.ai/v1/magic_links/redirect/dfp", json=body,
                headers={"Accept": "application/json",
                         "Content-Type": "application/json;charset=UTF-8",
                         "Origin": "https://login.typesafe.ai",
                         "Referer": link}, timeout=40)
    dump(r2, body=800)
    redirect_url = ""
    try:
        redirect_url = (r2.json() or {}).get("redirect_url", "")
    except ValueError:
        pass
    if not redirect_url:
        print("  ✗ 没有 redirect_url，到此为止")
        return 1
    print(f"  ★ redirect_url = {redirect_url[:300]}")

    # ── 3. GET redirect_url（控制台回调落地） ────────────────────────
    hr("3. GET redirect_url（控制台 /auth/callback 落地）")
    r3 = s.get(redirect_url, timeout=40, allow_redirects=True)
    dump(r3, body=400)
    tok = re.search(r"[?&]token=([^&]+)", redirect_url)
    ttype = re.search(r"[?&]stytch_token_type=([^&]+)", redirect_url)
    print(f"  token      = {tok.group(1)[:60] if tok else '(无)'}")
    print(f"  token_type = {ttype.group(1) if ttype else '(无)'}")

    if not args.callback:
        print("\n（未加 --callback，停在这里）")
        return 0

    # ── 4. POST /api/auth/callback ───────────────────────────────────
    hr("4. POST /api/auth/callback")
    if not tok:
        print("  ✗ redirect_url 里没有 token")
        return 1
    tk = requests.utils.unquote(tok.group(1))
    tt = ttype.group(1) if ttype else "magic_links"
    pl = {"token": tk, "tokenType": tt, "returnTo": None, "preferredOrgId": None,
          "inviteId": None, "oauthState": None, "waitlistEmail": args.email}
    r4 = s.post(f"{config.SITE_ORIGIN}/api/auth/callback", json=pl,
                headers={"Origin": config.SITE_ORIGIN,
                         "Referer": f"{config.SITE_LOGIN}?waitlist={args.email}",
                         "Accept": "application/json"}, timeout=40)
    dump(r4, body=400)

    # ── 5. /api/me ───────────────────────────────────────────────────
    hr("5. GET /api/me")
    dump(s.get(f"{config.SITE_ORIGIN}/api/me", headers={"Accept": "application/json"},
               timeout=30), body=300)
    return 0


if __name__ == "__main__":
    sys.exit(main())
