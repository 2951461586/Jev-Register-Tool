#!/usr/bin/env python
"""端到端跑批 CLI。

用法：

    # 只投递申请（阶段 1+2），不碰登录
    python tools/run_e2e.py --mode apply --count 3

    # 全链路（会停在邀请制门槛，如实报告）
    python tools/run_e2e.py --count 1

    # 对已获批的邮箱跑 4→7（零注册请求，可反复跑）
    python tools/run_e2e.py --mode resume --email a@b.com --email c@d.com

    # 获批邮箱是真人邮箱（Worker 读不到）时的人工接力：
    #   a) 先让站点发码到你邮箱
    python tools/run_e2e.py --mode claim --email me@real.com --send
    #   b) 从邮箱里取到 6 位码后立刻提交，一次跑完 4→7
    python tools/run_e2e.py --mode claim --email me@real.com --token 123456

    # 环境体检
    python tools/run_e2e.py --doctor
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402


def cmd_doctor() -> int:
    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值（TEMPMAIL_ADMIN_KEY）", file=sys.stderr)
        return 1
    c = TempMailClient()
    try:
        h = c.health()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 邮箱服务不可用：{exc}", file=sys.stderr)
        return 1
    print(f"✓ 邮箱服务 ok，storage={h.get('storage')} db={h.get('database')}")
    print(f"  可用域名：{', '.join(h.get('domains') or [])}")
    try:
        mb = c.create_mailbox()
        print(f"✓ 建邮箱 ok：{mb}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 建邮箱失败：{exc}", file=sys.stderr)
        return 1
    led = Ledger(config.LEDGER_PATH)
    print(f"✓ 台账 {config.LEDGER_PATH} 现有 {len(led.load())} 条")
    return 0


def cmd_scan() -> int:
    """按 `src/mailrules.py` 的规则表给窗口内全部邮件分桶。

    🔴 关键：**把"漏网主题"显式列出来**。
    站点改文案时，只报"没收到邮件"是查不出原因的；
    列出漏网主题能一眼看出是文案变了还是真没发。
    """
    from src.mailrules import RULES, diagnose

    c = TempMailClient()
    msgs = c.scan_all()
    if not msgs:
        print("窗口内没有邮件")
        return 0
    lo = min(m.received_at for m in msgs)
    hi = max(m.received_at for m in msgs)
    print(f"窗口 {len(msgs)} 封，时间跨度 {(hi - lo) / 60000:.1f} 分钟"
          f"（服务端保留最近 100 行，超出即删）")
    print(f"发件人过滤：sender 含 {RULES[0].sender_contains!r} 或 typesafe.ai\n")

    d = diagnose(msgs)
    for rule in RULES:
        hits = d["buckets"][rule.name]
        mark = "★" if rule.name in ("account_ready",) else "·"
        print(f"  {mark} {rule.name:<18} {len(hits):>3} 封   [{rule.stage}]")
        for m in hits[:3]:
            print(f"        {m.recipient:<42} {m.received_at}")
        if len(hits) > 3:
            print(f"        …（共 {len(hits)} 封）")
    print(f"\n  邻居项目/无关邮件（发件人不含 typesafe.ai）：{d['foreign']} 封")

    un = d["unclaimed_subjects"]
    if un:
        print(f"\n  ⚠ 是我们的但**没有规则认领**的主题 {len(un)} 种 —— 站点可能改了文案：")
        for s in un:
            print(f"      {s[:76]}")
    else:
        print("\n  ✓ 没有漏网主题：窗口内所有 TypeSafe 邮件都被规则覆盖")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="TypeSafe 端到端注册链路")
    ap.add_argument("--mode",
                    choices=["full", "apply", "resume", "watch", "scan", "claim"],
                    default="full",
                    help="full=全链路；apply=只投递申请；resume=对已获批邮箱跑 4→7；"
                         "watch=监听获批邮件并自动续跑；scan=列出邮箱窗口内全部邮件；"
                         "claim=用外部提供的验证码/链接 token 对单个已获批邮箱跑 4→7")
    ap.add_argument("--count", type=int, default=1, help="批次数量（mode=full/apply）")
    ap.add_argument("--email", action="append", default=[], help="指定邮箱（mode=resume/claim）")
    ap.add_argument("--token", default="", help="mode=claim 的验证码或魔法链接 token")
    ap.add_argument("--token-kind", choices=["otp", "magic_links"], default="otp",
                    help="mode=claim 的 token 类型")
    ap.add_argument("--send", action="store_true",
                    help="mode=claim：先触发发码（不提交），把码发到该邮箱")
    ap.add_argument("--domain", default="", help="邮箱域名，默认取配置")
    ap.add_argument("--login-mode", choices=["code", "link"], default="code",
                    help="code=6 位验证码；link=魔法链接")
    ap.add_argument("--approval-timeout", type=float, default=0.0,
                    help="等审批的秒数，0=只做一次快照检查")
    ap.add_argument("--watch-timeout", type=float, default=900.0,
                    help="mode=watch 的监听时长（秒）")
    ap.add_argument("--watch-interval", type=float, default=15.0,
                    help="mode=watch 的轮询间隔（秒）")
    ap.add_argument("--key-name", default="1", help="API Key 名称")
    ap.add_argument("--json", default="", help="把结果写到这个文件")
    ap.add_argument("--doctor", action="store_true", help="只做环境体检")
    args = ap.parse_args()

    if args.doctor:
        return cmd_doctor()

    missing = config.validate(need_tempmail=args.mode != "claim")
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值", file=sys.stderr)
        return 1

    pipe = Pipeline(domain=args.domain or None, login_mode=args.login_mode)

    if args.mode == "scan":
        return cmd_scan()

    if args.mode == "claim":
        if not args.email:
            print("✗ mode=claim 需要一个 --email", file=sys.stderr)
            return 1
        if len(args.email) > 1:
            print("✗ mode=claim 一次只处理一个邮箱（验证码 10 分钟且一次性）",
                  file=sys.stderr)
            return 1
        recs = [pipe.claim(args.email[0], args.token,
                           kind=args.token_kind, name=args.key_name,
                           send_first=args.send)]
        if recs[0].status == "code_sent":
            print("\n已发码。请到该邮箱取 6 位验证码，然后在 10 分钟内执行：")
            print(f"  python tools/run_e2e.py --mode claim --email {args.email[0]} "
                  f"--token <验证码>")
            return 0

    if args.mode == "watch":
        recs = pipe.watch(timeout=args.watch_timeout, interval=args.watch_interval,
                          name=args.key_name)
        print(f"\n监听结束：共处理 {len(recs)} 个获批账号")
        for r in recs:
            print(f"  - {r.email:<42} {r.status:<10} api_key={(r.api_key or '')[:28]}")
        return 0

    if args.mode == "resume":
        if not args.email:
            print("✗ mode=resume 需要至少一个 --email", file=sys.stderr)
            return 1
        recs = pipe.resume(args.email, name=args.key_name)
    elif args.mode == "claim":
        pass  # recs 已在上面的 claim 分支里算好
    else:
        recs = pipe.run_batch(count=args.count, mode=args.mode,
                              approval_timeout=args.approval_timeout,
                              name=args.key_name)

    # ── 报告 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("结果汇总")
    print("=" * 74)
    keyed = sum(1 for r in recs if r.status == "keyed")
    blocked = sum(1 for r in recs if "invite_only" in (r.error or ""))
    # mode=apply 跑到 confirmed 就是正常终点，不能算失败
    stopped = sum(1 for r in recs
                  if r.status == "confirmed" and not r.error and args.mode == "apply")
    other = len(recs) - keyed - blocked - stopped
    print(f"  拿到 key {keyed} / 受邀请制阻断 {blocked} / 申请段正常结束 {stopped}"
          f" / 其它失败 {other} / 合计 {len(recs)}")
    for r in recs:
        print(f"  - {r.email:<42} {r.status:<10} {r.stages}")
        if r.error:
            print(f"      ↳ {r.error}")

    # 最慢那条的阶段分解（关键路径永远是最慢那条，不是第一个）
    timed = [r for r in recs if r.timings]
    if timed:
        slow = max(timed, key=lambda r: sum(r.timings.values()))
        print(f"\n  最慢账号 {slow.email} 阶段分解：")
        for k, v in slow.timings.items():
            print(f"    {k:<12} {v:6.2f}s")

    led = Ledger(config.LEDGER_PATH)
    print(f"\n  台账 {config.LEDGER_PATH} 现有 {len(led.load())} 条")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps([r.to_dict() for r in recs], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
