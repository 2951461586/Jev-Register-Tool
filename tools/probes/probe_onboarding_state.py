"""探针：onboarding 现状 —— 站点到底要哪几步、/api/me 现在返回什么字段。

背景：`complete_onboarding()` 依赖 `/api/me` 的
`latest_tos_acceptance` / `human_name` / `console_survey_completed_at` 三个字段
判断缺口。大 HAR 里**只出现了 /setup/tos 与 /setup/set-name**，没有
/setup/console-survey ⇒ 当时怀疑站点已把三步简化成两步。

⚠️ 该怀疑**已被推翻**：`/setup/console-survey` 存在，且**时有时无** ——
2026-09-21 晚 100 批实测，走到该跳的 68 个账号里 28 个遇到它。
站点步数在 2~3 之间摆动，**不要写死步数**。真问题不是"站点删了这步"，
而是"`needs_survey` 判据永远为 True ⇒ 永远多跑一步 ⇒ 404 ⇒ 假 partial"。

用法：
    python tools/probes/probe_onboarding_state.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import *  # noqa: F401,F403,E402

from src.parsing import actions_from_html, visible_text  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import MODE_LINK, TypeSafeClient  # noqa: E402

mail = TempMailClient()
email = mail.create_mailbox()
print(f"=== 邮箱 {email} ===")

cl = TypeSafeClient()
since = int(time.time() * 1000) - 5_000
cl.send_login_email(email, mode=MODE_LINK)
m = mail.wait_for_mail(email, lambda x: "confirm your email" in x.subject.lower(),
                       timeout=180, interval=2.0, since_ms=since)
if m is None:
    print("✗ 没收到邮件")
    sys.exit(1)
from src.parsing import extract_magic_link  # noqa: E402
redirect = cl.exchange_magic_link(extract_magic_link(m.body))
token = cl.token_from_redirect_url(redirect)
res = cl.auth_callback(token, "magic_links", email)
print(f"auth_callback: HTTP {res.status} ok={res.ok}")
if not res.ok:
    print(f"  data={res.data}")
    sys.exit(2)

print("\n### 登录后立刻 GET /api/me（onboarding 前）")
st = cl.onboarding_state()
print(json.dumps(st["profile"], ensure_ascii=False, indent=2)[:2500])
print(f"needs_tos={st['needs_tos']} needs_name={st['needs_name']} "
      f"needs_survey={st['needs_survey']}")

print("\n### 各 /setup/* 页面渲染形态")
for path in ("/setup/tos?returnTo=%2Fhook", "/setup/set-name?returnTo=%2Fhook",
             "/setup/console-survey?returnTo=%2Fhook", "/hook"):
    r = cl.s.get(f"https://console.typesafe.ai{path}", timeout=30)
    acts = actions_from_html(r.text)
    print(f"  {path:44s} HTTP {r.status_code} len={len(r.text):6d} "
          f"actions={sorted(acts)} txt={visible_text(r.text)[:110]!r}")

print("\n### 跑 tos + set-name（跳过 console-survey）")
r1 = cl.post_setup("/setup/tos?returnTo=%2Fhook", {
    "legalAcknowledged": "true", "returnTo": "/hook",
    "marketingOptIn": "true", "marketingOptedOutInitial": ""})
print(f"  tos      -> ok={r1.ok} via={r1.data.get('via')} redirect={r1.data.get('redirect')!r}")
r2 = cl.post_setup("/setup/set-name?returnTo=%2Fhook", {
    "returnTo": "/hook", "accountEmail": email,
    "displayName": email.split("@")[0][:24], "jobFunction": "", "skip": "true"})
print(f"  set-name -> ok={r2.ok} via={r2.data.get('via')} redirect={r2.data.get('redirect')!r}")

st2 = cl.onboarding_state()
print(f"\n### 两步之后 /api/me: needs_tos={st2['needs_tos']} "
      f"needs_name={st2['needs_name']} needs_survey={st2['needs_survey']}")
print(json.dumps(st2["profile"], ensure_ascii=False, indent=2)[:2500])

print("\n### 直接建 key")
try:
    key = cl.create_api_key("probe")
    print(f"  ✓ api_key={key.get('api_key', '')[:40]}… id={key.get('id')}")
except Exception as exc:
    print(f"  ✗ {type(exc).__name__}: {exc}")
