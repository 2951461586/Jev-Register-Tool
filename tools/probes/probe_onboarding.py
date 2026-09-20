#!/usr/bin/env python
"""探针：`onboarding 失败: HTTP 404` 到底卡在哪一跳。

背景（2026-09-20 10:0x）
──────────────────────
两条**已获批**的账号（`oai-e7502518a4774fd8` / `oai-10753b6526484d99`）在
`--mode resume` 里 `login` 成功（说明邀请门槛已过），却统一报
`onboarding 失败: HTTP 404`。

`post_setup()` 把两件事混在一个 `Result` 里，只看 error 分不出是哪一件：

  1. `GET /setup/<page>` 拿不到 `$ACTION_*` 隐藏域（页面结构变了 / 已跳过）
  2. 退化到 `next-action` 通路后，**POST** 本身 404

这个探针把两跳分别打出来。

⚠️ 默认**只读**：只 GET。加 `--post` 才会真的提交表单。

用法：
    python tools/probes/probe_onboarding.py <email>
    python tools/probes/probe_onboarding.py <email> --post
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import config  # noqa: E402
from src.pipeline import AccountRecord, Pipeline  # noqa: E402
from src.typesafe import (_action_form_fields,  # noqa: E402
                          _actions_from_html, _visible_text)
from src.typesafe import FALLBACK_SETUP_ACTIONS  # noqa: E402

PAGES = ["/setup/tos", "/setup/set-name", "/setup/console-survey"]


def hr(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--post", action="store_true", help="真的提交表单（默认只读）")
    args = ap.parse_args()

    pipe = Pipeline(verbose=True)
    rec = AccountRecord(key=args.email, email=args.email)

    hr("1. 建立会话（stage_login：发码 → 收码 → /api/auth/callback）")
    cl = pipe.stage_login(rec)
    if cl is None:
        print(f"  ✗ 登录失败：status={rec.status} error={rec.error}")
        return 1
    print(f"  ✓ 登录成功，status={rec.status}")

    hr("2. GET /api/me —— onboarding 的三项缺口")
    me = cl.me() or {}
    tos = (me.get("latest_tos_acceptance") or {}).get("tos_version")
    print(f"  human_name                 = {me.get('human_name')!r}")
    print(f"  latest_tos_acceptance      = {json.dumps(me.get('latest_tos_acceptance'), ensure_ascii=False)[:200]}")
    print(f"  console_survey_completed_at= {me.get('console_survey_completed_at')!r}")
    print(f"  org_memberships            = {json.dumps(me.get('org_memberships'), ensure_ascii=False)[:200]}")
    print(f"  → needs_tos={tos is None}  needs_name={not (me.get('human_name') or '').strip()}  "
          f"needs_survey={me.get('console_survey_completed_at') is None}")
    print(f"  me 的全部键: {sorted(me)}")

    hr("3. GET 每个 /setup/* 页 —— 有没有渲染出 $ACTION_* 隐藏域")
    for p in PAGES:
        full = f"{p}?returnTo=%2Fhook"
        r = cl.s.get(f"{config.SITE_ORIGIN}{full}", timeout=30)
        acts = _actions_from_html(r.text)
        print(f"\n  ── {full}")
        print(f"     HTTP {r.status_code}  content-type={r.headers.get('content-type','')!r}  "
              f"len={len(r.text)}  location={r.headers.get('location')!r}")
        print(f"     $ACTION 隐藏域: {sorted(acts) or '（无）'}")
        print(f"     降级 action id: {FALLBACK_SETUP_ACTIONS.get(p)!r}")
        if r.status_code != 200:
            print(f"     body[:300] = {r.text[:300]!r}")
        else:
            print(f"     可见文案[:220] = {_visible_text(r.text)[:220]!r}")

    if not args.post:
        print("\n（未加 --post，只做了只读探测）")
        return 0

    hr("4. 真提交 /setup/tos —— **两条通路分别裸打**，看 404 是谁返回的")

    path = "/setup/tos?returnTo=%2Fhook"
    base = "/setup/tos"
    fields = {"legalAcknowledged": "true", "returnTo": "/hook",
              "marketingOptIn": "true", "marketingOptedOutInitial": ""}

    # A. 渐进增强形态：页面里带 $ACTION_* 隐藏域 → 无 JS 表单提交
    print("\n  ── A. 渐进增强（$ACTION_* 隐藏域）")
    acts = _actions_from_html(cl.s.get(f"{config.SITE_ORIGIN}{path}", timeout=30).text)
    if not acts:
        print("     跳过：GET 页面里没有 $ACTION_* 隐藏域（这就是降级的触发条件）")
    else:
        n = next(iter(acts))
        a = acts[n]
        print(f"     action_{n}: id={a['id']}  fields={sorted(a['fields'])}  key={a['key'][:16]!r}")
        files = _action_form_fields(n, a)
        for k, v in fields.items():
            files[k] = (None, v)
        r = cl.s.post(f"{config.SITE_ORIGIN}{path}", files=files,
                      headers={"Origin": config.SITE_ORIGIN,
                               "Referer": f"{config.SITE_ORIGIN}{path}",
                               "Accept": "text/html"}, timeout=40)
        print(f"     HTTP {r.status_code}  x-action-redirect={r.headers.get('x-action-redirect')!r}")
        print(f"     body[:300] = {r.text[:300]!r}")

    # B. 降级形态：next-action 头 + multipart + `0 = [{},"$K1"]`
    print("\n  ── B. 降级（next-action + multipart）")
    aid = FALLBACK_SETUP_ACTIONS.get(base)
    print(f"     action id = {aid!r}（来自录制 HAR）")
    if not aid:
        print("     跳过：没有降级 action id")
    else:
        files = {f"_1_{k}": (None, v) for k, v in fields.items()}
        files["0"] = (None, '[{},"$K1"]')
        r = cl.s.post(f"{config.SITE_ORIGIN}{path}", files=files,
                      headers={"Origin": config.SITE_ORIGIN,
                               "Referer": f"{config.SITE_ORIGIN}{path}",
                               "Accept": "text/x-component",
                               "next-action": aid}, timeout=40)
        print(f"     HTTP {r.status_code}  x-action-redirect={r.headers.get('x-action-redirect')!r}")
        print(f"     body[:300] = {r.text[:300]!r}")

    print("\n  ── 判读")
    print("     若 A 没得打（GET 页面无隐藏域）且 B 返回 404 ⇒ 页面结构已变：")
    print("     /setup/tos 不再渲染渐进增强表单，且录制的 action id 已失效。")
    print("     若 A 打了且 200 ⇒ 是 post_setup 的降级选择逻辑选错了通路。")

    hr("5. 直接复现管线路径：cl.complete_onboarding()")
    ob = cl.complete_onboarding(display_name=args.email.split("@")[0][:24])
    print(f"  ok={ob.ok} stage={ob.stage} status={ob.status} error={ob.error!r}")
    print(f"  data={json.dumps(ob.data, ensure_ascii=False)[:400]}")
    print("\n  ── 客户端日志（每次 post_setup 记 via / 状态码 / 重定向）")
    for line in cl.log[-12:]:
        print("     ", line)
    print("\n  ── 打完之后的 /api/me 缺口")
    st = cl.onboarding_state()
    print(f"     needs_tos={st['needs_tos']}  needs_name={st['needs_name']}  "
          f"needs_survey={st['needs_survey']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
