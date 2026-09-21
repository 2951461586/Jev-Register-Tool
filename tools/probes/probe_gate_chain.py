"""探针：把 onboarding 的**完整门禁链**逐跳打出来。

为什么单开一个探针
──────────────────
2026-09-21 实跑发现：`/hook` 的门禁链里出现了 `console-survey`，
而它**不在** `typesafe.KNOWN_ONBOARDING_GATES` 里 ⇒ `complete_onboarding()`
在第一跳之后就直接报"未知门禁"。

两个旧探针都没抓到这件事，各有盲点：
  · `probe_onboarding_state.py` 在**接受 ToS 之前**就 GET 所有 `/setup/*`，
    那时它们一律 307 回 `/setup/tos`，看不出后续；
  · `probe_onboarding_state2.py` 只看"ToS 前 / ToS 后"两个**静态快照**，
    而门禁是**串行**的 —— 快照里 `/hook` 指向 set-name，不代表链路上没有
    console-survey 这一跳。

本探针改用**逐跳循环**：每次都走产品代码的 `onboarding_gate()`，
把原始 `location` 一并打出来（不只看解析后的 slug）。

用法：
    python tools/probes/probe_gate_chain.py [--rounds 6] [--email <已有邮箱>]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import config  # noqa: E402
from src.parsing import actions_from_html, extract_magic_link, visible_text  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import MODE_LINK, TypeSafeClient  # noqa: E402

#: 每一步要提交的字段 —— 站点新增步骤时这里自然没有，探针会把它标成"未覆盖"。
STEP_FIELDS: dict[str, dict[str, str]] = {
    "tos": {"legalAcknowledged": "true", "returnTo": "/hook",
            "marketingOptIn": "true", "marketingOptedOutInitial": ""},
    "set-name": {"returnTo": "/hook", "jobFunction": "", "skip": "true"},
}


def raw_location(cl: TypeSafeClient) -> str:
    """直接看 `/hook` 的原始 `location`（产品代码只暴露解析后的 slug）。"""
    r = cl.s.get(f"{config.SITE_ORIGIN}/hook", allow_redirects=False, timeout=30)
    return f"HTTP {r.status_code} loc={r.headers.get('location', '')!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--email", default="", help="用已有邮箱（需已在台账里登录过）")
    args = ap.parse_args()

    mail = TempMailClient()
    email = args.email or mail.create_mailbox()
    print(f"=== 邮箱 {email} ===")

    cl = TypeSafeClient()
    since = int(time.time() * 1000) - 5_000
    cl.send_login_email(email, mode=MODE_LINK)
    m = mail.wait_for_mail(email, lambda x: "confirm your email" in x.subject.lower(),
                           timeout=180, interval=2.0, since_ms=since)
    if m is None:
        print("✗ 没收到确认邮件")
        return 1
    redirect = cl.exchange_magic_link(extract_magic_link(m.body))
    res = cl.auth_callback(cl.token_from_redirect_url(redirect), "magic_links", email)
    print(f"auth_callback HTTP {res.status} ok={res.ok}")
    if not res.ok:
        print(f"  data={res.data}")
        return 2

    print("\n### 逐跳走门禁（产品代码 onboarding_gate + post_setup）")
    for i in range(1, args.rounds + 1):
        gate = cl.onboarding_gate()
        print(f"\n--- 第 {i} 跳 ---")
        print(f"  onboarding_gate() = {gate!r}")
        print(f"  /hook 原始：{raw_location(cl)}")
        if not gate:
            print("  ⇒ 门禁已通过（/hook 200）")
            break
        if gate not in STEP_FIELDS:
            print(f"  ⇒ 门禁 {gate!r} **不在 STEP_FIELDS 里**，本探针不知道要提交什么。")
            # 站点新增步骤时，把它那一页的隐藏域与可见文案打出来，供人补字段。
            path = f"/setup/{gate}?returnTo=%2Fhook"
            r = cl.s.get(f"{config.SITE_ORIGIN}{path}", timeout=30,
                         allow_redirects=False)
            print(f"     GET {path} → HTTP {r.status_code} "
                  f"loc={r.headers.get('location', '')!r}")
            if r.status_code == 200:
                acts = actions_from_html(r.text)
                print(f"     actions={sorted(acts)} "
                      f"ids={[acts[k]['id'][:20] + '…' for k in sorted(acts)]}")
                print(f"     可见文案={visible_text(r.text)[:200]!r}")
            break
        fields = dict(STEP_FIELDS[gate])
        if gate == "set-name":
            fields["accountEmail"] = email
            fields["displayName"] = email.split("@")[0][:24]
        r = cl.post_setup(f"/setup/{gate}?returnTo=%2Fhook", fields)
        print(f"  post_setup({gate}) → ok={r.ok} via={r.data.get('via')} "
              f"HTTP {r.status} redirect={r.data.get('redirect')!r}")
        print(f"  err={r.error!r}")
        print(f"  提交后 /hook 原始：{raw_location(cl)}")
    else:
        print(f"\n（跑满 {args.rounds} 跳仍未通过）")

    print("\n### 收尾：/api/me")
    st = cl.onboarding_state()
    print(f"  needs_tos={st['needs_tos']} needs_name={st['needs_name']} "
          f"needs_survey={st['needs_survey']} "
          f"human_name={st['profile'].get('human_name')!r}")

    # 🔴 决定性一问：门禁没走完时，`POST /api/api-keys` 到底放不放行？
    #    放行 ⇒ `/hook` 的重定向只是"引导"，不是硬门禁（旧实现把它当硬门禁，
    #    于是把"已经能拿 key"的账号判成 partial）；
    #    不放行 ⇒ 它确实是硬门禁，必须把这一跳的表单形态搞清楚。
    print("\n### 决定性一问：门禁未走完时试建 key")
    try:
        key = cl.create_api_key("gate-chain-probe")
        print(f"  ✓ **放行** api_key={str(key.get('api_key'))[:32]}… id={key.get('id')}")
        print("  ⇒ `/hook` 的重定向是**引导**，不是硬门禁：拿 key 不应要求它归零")
    except Exception as exc:  # noqa: BLE001 —— 探针要把失败原因原样打出来
        print(f"  ✗ **不放行** {type(exc).__name__}: {exc}")
        print("  ⇒ 它确实是硬门禁：需要把该步的表单形态搞清楚（见上面 GET 的 dump）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
