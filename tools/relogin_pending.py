"""带重试的重新登录：对**无 key 但已申请**的账号跑 4→7，直到拿到 key 或被明确拒绝。

为什么需要它（2026-09-21 实测得出的两条）
────────────────────────────────────────────
1. **`stage_wait_approval` 等的那封"获批邮件"不可信** —— 共享 Worker 的 D1 窗口只有
   100 行、被同机邻居项目刷屏，获批邮件会被挤掉。项目自己也记了
   「窗口里没看到 account_ready ≠ 未获批，可信判据只有站点侧实测」。
   ⇒ 正确做法是**直接尝试登录**：`auth_callback` 回 200 = 已获批；
   回 `403 Access restricted` = 未获批。这才是那个"站点侧实测"。
2. **站点发信严重丢包**（实测 12 次发码只到 1 封），但**魔法链接 7 天有效**。
   ⇒ 只要耐心重试、并且**把已经躺在收件箱里的链接也用上**，就能拿下来。
   `stage_login` 的 `wait_for_mail(since_ms=now-5s)` 只认"请求之后到达"的信，
   历史链接永远用不上 —— 这是本脚本要补的洞。

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

from src import config, ledger  # noqa: E402
from src.mailrules import extract_otp  # noqa: E402
from src.parsing import extract_magic_link  # noqa: E402
from src.runner import Pipeline  # noqa: E402
from src.stages import (LINK_FALLBACK_TIMEOUT, MATCH_CODE, MATCH_LINK,  # noqa: E402
                        AccountRecord)
from src.typesafe import MODE_CODE, TypeSafeClient, TypeSafeError  # noqa: E402


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


def relogin_one(pipe, email, *, rounds=3, wait=180.0, name="1"):
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

        # ── 2. 没有可用的旧链接就发一次，等新的 ──────────────────────
        cl = TypeSafeClient()
        since = int(time.time() * 1000) - 5_000
        try:
            r = cl.send_login_email(email, mode=MODE_CODE)
        except TypeSafeError as exc:
            pipe.log(f"     发码异常 {exc}")
            time.sleep(15)
            continue
        if not r.ok:
            pipe.log(f"     发码 HTTP {r.status}")
            time.sleep(15)
            continue
        pipe.log(f"     已发码 HTTP {r.status}，等信（码 {wait:.0f}s / 链接 {LINK_FALLBACK_TIMEOUT:.0f}s）…")

        m = pipe.mail.wait_for_mail(email, MATCH_CODE,
                                    timeout=wait, interval=3.0, since_ms=since)
        if m is not None:
            pipe.log(f"     收到码邮件 {str(m.subject)[:40]!r}")
            token, how = extract_otp(m.body)
            if token:
                res = cl.auth_callback(token, "otp", email)
                if finish(pipe, rec, cl, res, name):
                    return rec
            else:
                pipe.log(f"     码邮件里没抽出 6 位码（how={how}）")
        else:
            pipe.log(f"     {wait:.0f}s 内没等到码邮件")

        # 码没到 ⇒ 站点可能回的是链接形态（项目记录：4 次里 1 次）
        m2 = pipe.mail.wait_for_mail(email, MATCH_LINK, timeout=LINK_FALLBACK_TIMEOUT,
                                     interval=3.0, since_ms=since)
        if m2 is not None:
            pipe.log(f"     收到链接邮件 {str(m2.subject)[:40]!r}")
            url = extract_magic_link(m2.body)
            if url:
                tried.add(url)
            res = pipe._exchange_link(rec, m2, cl)
            if res is not None and finish(pipe, rec, cl, res, name):
                return rec
        else:
            pipe.log(f"     {LINK_FALLBACK_TIMEOUT:.0f}s 内也没等到链接邮件")

        pipe.log(f"     本轮未拿到可用凭据")

    pipe._fail(rec, "login", f"{rounds} 轮均未拿到可用凭据（站点发信丢包）")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", action="append", default=[])
    ap.add_argument("--all-pending", action="store_true",
                    help="台账里所有无 api_key 且已确认的账号")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--wait", type=float, default=180.0)
    ap.add_argument("--name", default="1")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    pipe = Pipeline(login_mode=MODE_CODE)
    led = pipe.ledger

    emails = list(args.email)
    if args.all_pending:
        emails += [r["key"] for r in led.load()
                   if not r.get("api_key") and r.get("status") in
                   ("confirmed", "applied", "approved")]
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
