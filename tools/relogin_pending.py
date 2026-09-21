"""带重试的重新登录：对**无 key 的账号**反复跑"发信 → 收链接 → 建会话"，直到拿到 key。

为什么需要它（2026-09-21 实测）
────────────────────────────────
**站点发信存在丢包**（实测 12 次发码只到 1 封），而**魔法链接 7 天有效**。
⇒ 只要耐心重试、并且**把已经躺在收件箱里的链接也用上**，就能把账号拿下来。

⚠️ 后一条是本脚本存在的**唯一理由**：`stage_login` 的
`wait_for_mail(since_ms=now-5s)` 只认"本次请求**之后**到达"的信，
所以上次跑到一半留下的链接**永远用不上**。本脚本不设 `since_ms` 门槛，
先把历史链接全部试一遍。

与 `--mode resume` 的分工：
    resume          跑一次，失败就算了（正常批次用这个）
    relogin_pending 对已知会丢包的账号**多轮重试 + 复用历史链接**（补跑用这个）

2026-09-21 变更：邀请制取消后本脚本**不再需要**"先判断是否获批"那一步
（旧版曾用 `403 Access restricted` 当"未获批"的可信判据）。现在
`auth_callback` 回 200 就是成功、回 403 才是真被拦（防御分支保留）。

用法：
    python tools/relogin_pending.py --email a@x --email b@x
    python tools/relogin_pending.py --all-pending --rounds 4 --wait 180
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _bootstrap import ROOT  # noqa: E402,F401

from src.parsing import extract_magic_link  # noqa: E402
from src.runner import Pipeline  # noqa: E402
from src.stages import MAIL_TIMEOUT, MATCH_LINK, AccountRecord  # noqa: E402
from src.typesafe import MODE_LINK, TypeSafeClient, TypeSafeError  # noqa: E402


def link_mails(cl, email):
    """收件箱里所有匹配魔法链接规则的信，**新的在前**（不设 since 门槛）。"""
    hits = [m for m in cl.list_mails(email) if MATCH_LINK(m)]

    def ts(m):
        v = getattr(m, "received_at", 0) or 0
        return v / 1000 if v > 1e12 else v

    hits.sort(key=ts, reverse=True)
    return hits


def finish(pipe, rec, cl, res, name):
    """返回 True = 已定论（拿到 key，或被明确拒绝），不必再试。"""
    if res.ok:
        rec.stages["login"] = "ok"
        rec.status = "registered"
        rec.user["profile"] = cl.me() or {}
        pipe.stage_create_key(rec, cl, name=name)
        return True
    code = str(res.data.get("error") or res.data.get("code") or "")
    if "Access restricted" in code:
        # 凭据有效但不在白名单 —— 业务门槛，不是技术故障。这是**可信的未获批判据**。
        pipe._fail(rec, "login", "invite_only: 403 Access restricted（该邮箱未被邀请）")
        return True
    pipe.log(f"      callback 未通过 HTTP {res.status} {code[:70]}")
    return False


def relogin_one(pipe, email, *, rounds=3, wait=MAIL_TIMEOUT, name="1"):
    rec = AccountRecord(key=email, email=email)
    tried: set[str] = set()

    for rnd in range(1, rounds + 1):
        pipe.log(f"  [r{rnd}] {email}")

        # ── 1. 先把手上的历史链接用掉（7 天有效；一次性，失败的记进 tried）──
        for m in link_mails(pipe.mail, email):
            url = extract_magic_link(m.body)
            if not url or url in tried:
                continue
            tried.add(url)
            cl = TypeSafeClient()
            pipe.log(f"     试历史链接 {str(m.subject)[:34]!r} …{url[-22:]}")
            res = pipe._exchange_link(rec, m, cl)
            if res is None:
                continue
            if finish(pipe, rec, cl, res, name):
                return rec

        # ── 2. 没有可用的旧链接就发一封新的，等它到 ──────────────────
        cl = TypeSafeClient()
        since = int(time.time() * 1000) - 5_000
        try:
            r = cl.send_login_email(email, mode=MODE_LINK)
        except TypeSafeError as exc:
            pipe.log(f"     发信异常 {exc}")
            time.sleep(15)
            continue
        if not r.ok:
            pipe.log(f"     发信 HTTP {r.status}")
            time.sleep(15)
            continue
        pipe.log(f"     已发信 HTTP {r.status}，等确认邮件（{wait:.0f}s）…")

        m = pipe.mail.wait_for_mail(email, MATCH_LINK, timeout=wait,
                                    interval=3.0, since_ms=since)
        if m is None:
            pipe.log(f"     {wait:.0f}s 内没等到确认邮件")
            continue
        url = extract_magic_link(m.body)
        if url:
            tried.add(url)
        pipe.log(f"     收到 {str(m.subject)[:44]!r}")
        res = pipe._exchange_link(rec, m, cl)
        if res is not None and finish(pipe, rec, cl, res, name):
            return rec

        pipe.log("     本轮未拿到可用凭据")

    pipe._fail(rec, "login", f"{rounds} 轮均未拿到可用凭据（站点发信丢包）")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", action="append", default=[])
    ap.add_argument("--all-pending", action="store_true",
                    help="台账里所有**还没有 api_key** 的真实邮箱账号")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--wait", type=float, default=MAIL_TIMEOUT,
                    help=f"每轮等确认邮件的秒数（默认 {MAIL_TIMEOUT:.0f}）")
    ap.add_argument("--name", default="1")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    pipe = Pipeline(login_mode=MODE_LINK)
    led = pipe.ledger

    emails = list(args.email)
    if args.all_pending:
        # 判据是"**没有 api_key 且 key 看起来是邮箱**"，而不是状态白名单：
        #   · 状态词汇会随链路变化（2026-09-21 就废掉了 4 个），写白名单必然漂移；
        #   · `worker-crash#N` 这类占位键**不是邮箱**，拿去发信只会白跑一轮。
        # 用 `"@" in key` 一条同时解决两件事。
        emails += [r["key"] for r in led.load()
                   if r.get("key") and "@" in r["key"] and not r.get("api_key")]
    if not emails:
        ap.error("要么给 --email，要么给 --all-pending")

    # 🔴 刻意串行：项目铁律「发码必须小批 + 串行（默认 --concurrency 1）」
    out = []
    for e in emails:
        rec = relogin_one(pipe, e, rounds=args.rounds, wait=args.wait, name=args.name)
        led.append(rec.to_dict())
        out.append({"email": e, "status": rec.status,
                    "key_len": len(rec.api_key or ""), "error": rec.error[:150]})
        print(f"[{rec.status}] {e}  key={'(有)' if rec.api_key else '(无)'}  {rec.error[:90]}")

    print("\n=== 汇总 ===")
    keyed = [r for r in out if r["key_len"]]
    print(f"  拿到 key: {len(keyed)} / {len(out)}")
    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"  写到 {args.json}")


if __name__ == "__main__":
    main()
