"""探针：接受 ToS **之后**，onboarding 还剩哪几步。

上一轮探针的盲点：它在**接受 ToS 之前**就 GET 了所有 /setup/* 页面，
于是四个路径都返回同一张 ToS 页（onboarding 门禁），看不出 set-name 的真实形态。

本轮顺序：登录 → 先只提交 ToS → 再逐个 GET 页面 → 再提交 set-name → 再 GET。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import *  # noqa: F401,F403,E402

from src.parsing import actions_from_html, extract_magic_link, visible_text  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import MODE_LINK, TypeSafeClient  # noqa: E402

BASE = "https://console.typesafe.ai"
mail = TempMailClient()
email = mail.create_mailbox()
print(f"=== 邮箱 {email} ===")

cl = TypeSafeClient()
since = int(time.time() * 1000) - 5_000
cl.send_login_email(email, mode=MODE_LINK)
m = mail.wait_for_mail(email, lambda x: "confirm your email" in x.subject.lower(),
                       timeout=180, interval=2.0, since_ms=since)
redirect = cl.exchange_magic_link(extract_magic_link(m.body))
res = cl.auth_callback(cl.token_from_redirect_url(redirect), "magic_links", email)
print(f"auth_callback HTTP {res.status} ok={res.ok}")


def dump_pages(tag: str) -> None:
    print(f"\n### 各页面形态（{tag}）")
    for path in ("/setup/tos?returnTo=%2Fhook", "/setup/set-name?returnTo=%2Fhook",
                 "/setup/console-survey?returnTo=%2Fhook", "/hook", "/home"):
        r = cl.s.get(f"{BASE}{path}", timeout=30, allow_redirects=False)
        loc = r.headers.get("location", "")
        body = r.text if r.status_code == 200 else ""
        acts = actions_from_html(body) if body else {}
        aid = ""
        if acts:
            n = sorted(acts)[0]
            aid = f" id={acts[n]['id'][:20]}…"
        print(f"  {path:44s} HTTP {r.status_code} len={len(body):6d} "
              f"actions={sorted(acts)}{aid} loc={loc[:60]!r}")
        if body:
            print(f"        txt={visible_text(body)[:120]!r}")


dump_pages("接受 ToS 前")

print("\n### 只提交 ToS")
r1 = cl.post_setup("/setup/tos?returnTo=%2Fhook", {
    "legalAcknowledged": "true", "returnTo": "/hook",
    "marketingOptIn": "true", "marketingOptedOutInitial": ""})
print(f"  tos -> ok={r1.ok} via={r1.data.get('via')} redirect={r1.data.get('redirect')!r} "
      f"err={r1.error!r}")
st = cl.onboarding_state()
print(f"  /api/me: needs_tos={st['needs_tos']} needs_name={st['needs_name']} "
      f"needs_survey={st['needs_survey']} human_name={st['profile'].get('human_name')!r}")

dump_pages("接受 ToS 后")
