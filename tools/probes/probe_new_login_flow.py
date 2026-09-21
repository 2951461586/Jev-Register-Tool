"""探针：取消邀请制之后，/login 发信 → 收信 的真实形态。

回答三个问题：
  1. 现有 no-JS 形态（`$ACTION_REF_2` + `$ACTION_2:0/1/2` + `email`）还能不能发信？
  2. 收到的邮件主题是什么？（"Welcome to TypeSafe — confirm your email" vs "Sign in to TypeSafe"）
  3. 邮件里的凭据是魔法链接还是 6 位码？

用法：
    python tools/probes/probe_new_login_flow.py [link|code]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import *  # noqa: F401,F403,E402

from src import config  # noqa: E402
from src.parsing import extract_magic_link, visible_text  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import MODE_CODE, MODE_LINK, TypeSafeClient, TypeSafeError  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else MODE_LINK

mail = TempMailClient()
email = mail.create_mailbox()
print(f"[1] 临时邮箱: {email}")

cl = TypeSafeClient()
try:
    acts = cl.fetch_actions(email)
    print(f"[2] 抓到 actions: {sorted(acts)}")
    for n in sorted(acts):
        print(f"      [{n}] id={acts[n]['id'][:32]}… fields={sorted(acts[n]['fields'])}")
except TypeSafeError as exc:
    print(f"[2] ✗ 抓 action 失败: {exc}")
    sys.exit(2)

since = int(time.time() * 1000) - 5_000
print(f"[3] POST 发信 mode={mode} …")
try:
    r = cl.send_login_email(email, mode=mode)
except TypeSafeError as exc:
    print(f"[3] ✗ 发信异常: {exc}")
    sys.exit(3)
print(f"    HTTP {r.status} ok={r.ok}")
print(f"    page_text: {r.data.get('page_text', '')[:300]!r}")
print(f"    client.log: {cl.log}")

print("[4] 等邮件（最多 180s）…")
t0 = time.time()
seen: set[str] = set()
found = None
while time.time() - t0 < 180:
    for m in mail.list_mails(email=email):
        if m.received_at < since:
            continue
        if m.id in seen:
            continue
        seen.add(m.id)
        print(f"    ★ 收到: subject={m.subject!r} from={m.sender!r} at={m.received_at}")
        print(f"      body[:600]={m.body[:600]!r}")
        link = extract_magic_link(m.body)
        print(f"      extract_magic_link -> {link[:200]!r}")
        if found is None:
            found = m
    if found is not None:
        break
    time.sleep(3)

if found is None:
    print("[4] ✗ 180s 内没收到任何邮件")
    sys.exit(4)
print(f"[4] ✓ 收到 {len(seen)} 封")
