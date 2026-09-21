#!/usr/bin/env python
"""端到端跑批 CLI。

用法：

    # 只投递申请（阶段 1+2），不碰登录
    python tools/run_e2e.py --mode apply --count 3

    # 全链路（会停在邀请制门槛，如实报告）
    python tools/run_e2e.py --count 1

    # 对已获批的邮箱跑 4→7（零申请请求，可反复跑）
    python tools/run_e2e.py --mode resume --email a@b.com --email c@d.com

    # 并发跑（每个 worker 用独立会话；watch 不支持并发）
    python tools/run_e2e.py --mode resume --email a@b.com --email c@d.com --concurrency 4

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402
from src.runner import AccountRecord, Pipeline  # noqa: E402
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
    print("发件人过滤：sender 含 typesafe.ai（规则表逐条见 src/mailrules.py）\n")

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


def cmd_claim(pipe: Pipeline, args: argparse.Namespace) -> int:
    """人工接力：先发码 / 再提交外部凭据。

    独立成函数，而不是嵌在 `main` 的 if/else 链里。

    🔴 以前它是**两段拼起来的**：上面 `if args.mode == "claim":` 里算出 `recs`
    并在 `code_sent` 时提前 `return`，下面再靠 `elif args.mode == "claim": pass`
    接住 —— `recs` 的"算"与"用"隔着 30 行和两个 return 点。
    这种形态下，任何人改动上面的 return 条件或新增模式，
    都可能让 `recs` 未定义、或串到 `run_batch` 上去。

    ⚠️ **本模式的两进程用法已知失效（2026-09-20 实测）**：
    `--send` 与 `--token` 是两个独立进程、各自新建 `TypeSafeClient()`，
    中间没有任何会话传递 ⇒ 码到 2 分钟内提交仍报 `401 Code expired`。
    **优先用 `--mode resume`**（同一进程内发码+提交，会话连续）。
    根因候选与验证方法见 `docs/runbook.md` §1.5。
    """
    if not args.email:
        print("✗ mode=claim 需要一个 --email", file=sys.stderr)
        return 1
    if len(args.email) > 1:
        print("✗ mode=claim 一次只处理一个邮箱（验证码 10 分钟且一次性）",
              file=sys.stderr)
        return 1

    if args.send:
        # 把告警打在**动作发生之前**，而不是等 401 出来再让人去查。
        # 用户照 runbook 走会先看到这一句，不至于掉进"重新发码"的死循环。
        print("⚠️  --mode claim 的两进程用法**已知失效**：本进程发完码就退出了，\n"
              "    而 --token 是另一个进程、另一套会话 ⇒ 实测提交时必报\n"
              "    `401 Code expired`（与码对不对无关）。\n"
              "    ⇒ 邮箱在 Worker 覆盖域内时请改用：\n"
              f"       python tools/run_e2e.py --mode resume --email {args.email[0]}\n"
              "    只有真人邮箱（Worker 读不到）才需要继续用 claim，\n"
              "    且请先看 docs/runbook.md §1.5 的根因候选与验证方法。\n",
              file=sys.stderr)

    rec = pipe.claim(args.email[0], args.token, kind=args.token_kind,
                     name=args.key_name, send_first=args.send)
    if rec.status == "code_sent":
        print("\n已发码。请到该邮箱取 6 位验证码，然后在 10 分钟内执行：")
        print(f"  python tools/run_e2e.py --mode claim --email {args.email[0]} "
              f"--token <验证码>")
        return 0
    return report([rec], mode="claim", json_path=args.json)


def cmd_watch(pipe: Pipeline, args: argparse.Namespace) -> int:
    recs = pipe.watch(timeout=args.watch_timeout, interval=args.watch_interval,
                      name=args.key_name)
    print(f"\n监听结束：共处理 {len(recs)} 个获批账号")
    for r in recs:
        print(f"  - {r.email:<42} {r.status:<10} api_key={(r.api_key or '')[:28]}")
    return 0


def report(recs: list[AccountRecord], *, mode: str, json_path: str = "") -> int:
    """结果汇总。分类口径与台账状态一一对应，不要在这里另造一套词。"""
    print("\n" + "=" * 74)
    print("结果汇总")
    print("=" * 74)
    keyed = sum(1 for r in recs if r.status == "keyed")
    blocked = sum(1 for r in recs if "invite_only" in (r.error or ""))
    # mode=apply 的正常终点有**两个**：confirmed（回执已到）与
    # applied（表单已 201 接受、申请已注册，只是回执没在阈值内到达）。
    # 后者不是失败 —— 2026-09-20 实证：确认邮件超时的账号后来全部获批。
    stopped = sum(1 for r in recs
                  if mode == "apply" and r.status in ("confirmed", "applied"))
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

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(
            json.dumps([r.to_dict() for r in recs], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  结果已写入 {json_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="TypeSafe 端到端注册链路")
    ap.add_argument("--mode",
                    choices=["full", "apply", "resume", "watch", "scan", "claim"],
                    default="full",
                    help="full=全链路；apply=只投递申请；resume=对已获批邮箱跑 4→7；"
                         "watch=监听获批邮件并自动续跑；scan=列出邮箱窗口内全部邮件；"
                         "claim=⚠️实验性：用外部提供的验证码/链接 token 对单个已获批邮箱"
                         "跑 4→7。**两进程用法已知失效**（--send 与 --token 各自建会话 ⇒ "
                         "必报 401 Code expired），优先用 resume；见 docs/runbook.md §1.5")
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
    ap.add_argument("--confirm-timeout", type=float, default=None,
                    help="等 waitlist 确认邮件的秒数（默认取 stages.CONFIRM_TIMEOUT=300；"
                         "调小会漏掉迟到的邮件并把成功申请记成 failed）")
    ap.add_argument("--watch-timeout", type=float, default=900.0,
                    help="mode=watch 的监听时长（秒）")
    ap.add_argument("--watch-interval", type=float, default=15.0,
                    help="mode=watch 的轮询间隔（秒）")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发账号数（mode=full/apply/resume）。默认 1=串行；"
                         ">1 时每个 worker 用独立的会话与邮箱客户端，只共享台账。"
                         "mode=watch 不支持——它读的是全表共享窗口，并发只会互相挤")
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

    # scan 是纯只读诊断，自己建 client，不需要 Pipeline
    if args.mode == "scan":
        return cmd_scan()

    if args.concurrency > 1 and args.mode == "watch":
        print("✗ mode=watch 不支持 --concurrency（见 --concurrency 的说明）",
              file=sys.stderr)
        return 1

    pipe = Pipeline(domain=args.domain or None, login_mode=args.login_mode)

    # 每个模式一个函数，主流程只做分派 —— 不再共享局部变量 recs
    if args.mode == "claim":
        return cmd_claim(pipe, args)
    if args.mode == "watch":
        return cmd_watch(pipe, args)

    if args.mode == "resume":
        if not args.email:
            print("✗ mode=resume 需要至少一个 --email", file=sys.stderr)
            return 1
        recs = pipe.resume(args.email, name=args.key_name,
                           concurrency=args.concurrency)
    else:
        kw = {}
        if args.confirm_timeout is not None:
            kw["confirm_timeout"] = args.confirm_timeout
        recs = pipe.run_batch(count=args.count, mode=args.mode,
                              approval_timeout=args.approval_timeout,
                              name=args.key_name, concurrency=args.concurrency, **kw)

    return report(recs, mode=args.mode, json_path=args.json)


if __name__ == "__main__":
    sys.exit(main())
