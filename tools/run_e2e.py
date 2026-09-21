#!/usr/bin/env python
"""端到端跑批 CLI。

用法：

    # 全链路：建临时邮箱 → 发确认邮件 → 登录 → onboarding → 拿 key
    python tools/run_e2e.py --count 3

    # 对**已知邮箱**跑（可反复重跑；站点发信丢包时靠这个补）
    python tools/run_e2e.py --mode resume --email a@b.com --email c@d.com

    # 并发（每个 worker 独立会话与邮箱客户端，只共享台账）
    python tools/run_e2e.py --count 10 --concurrency 4

    # 诊断：列出邮箱窗口内全部邮件并按规则分桶（看有没有漏网主题）
    python tools/run_e2e.py --mode scan

    # 环境体检
    python tools/run_e2e.py --doctor

2026-09-21：邀请制取消后的模式收缩
──────────────────────────────────
旧 CLI 有 6 个模式，其中三个是"邀请制"的产物，已一并删除：

    apply   只投递 Framer waitlist 申请（申请环节本身不存在了）
    watch   轮询全表窗口找"获批邮件"并自动续跑（"获批"事件不存在了）
    claim   人工接力，**两进程用法 2026-09-20 已实测失效**（必报 401 Code expired）

⇒ 现在只剩 `full` / `resume` / `scan`，以及 `--doctor`。
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
from src.runner import (DEFAULT_MAIL_BACKEND, MAIL_BACKENDS,  # noqa: E402
                        AccountRecord, Pipeline, make_mail_client)
from src.stages import MAIL_TIMEOUT  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402


def cmd_doctor(backend: str = DEFAULT_MAIL_BACKEND) -> int:
    """环境体检。

    ⚠️ 两个后端的体检**深度刻意不同**：
      · CF     —— 建邮箱免费，所以直接建一个，端到端验一遍；
      · Remail —— **建邮箱 = 真实下单扣积分**（TypeSafe 项目下 `domain`
                  商品 0.01 积分/单）。体检**不该悄悄花钱**，所以只做只读检查
                  （API Key 是否有效 / 余额 / 当前项目与商品配置），
                  下单留给真实跑批 —— 那笔钱要花得看得见。
    """
    missing = (config.validate_remail() if backend == "remail"
               else config.validate())
    if missing:
        print(f"✗ 后端 {backend} 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值", file=sys.stderr)
        return 1

    c = make_mail_client(backend)
    try:
        h = c.health()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 邮箱服务不可用：{exc}", file=sys.stderr)
        return 1

    if backend == "remail":
        # Remail 没有 `/health`，`health()` 走的是 `GET /v1/open/apikey/profile`。
        k = (h or {}).get("apiKey") or {}
        print(f"✓ Remail ok：key id={k.get('id')} enabled={k.get('enabled')} "
              f"余额={k.get('balance')}")
        print(f"  项目 id={config.REMAIL_PROJECT_ID} "
              f"商品后缀={config.REMAIL_EMAIL_SUFFIX} "
              f"模式={config.REMAIL_SERVICE_MODE}")
        print(f"  凭证台账 {config.REMAIL_STATE_PATH} 已恢复 {c.restored} 条"
              f"（跨进程 resume 靠它）")
        print("  ⚠ 建邮箱 = 真实下单扣积分 ⇒ 体检不下单；验证下单请直接跑 --count 1")
    else:
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


def cmd_scan(backend: str = DEFAULT_MAIL_BACKEND) -> int:
    """按 `src/mailrules.py` 的规则表给窗口内全部邮件分桶。

    🔴 关键：**把"漏网主题"显式列出来**。
    站点改文案时，只报"没收到邮件"是查不出原因的；
    列出漏网主题能一眼看出是文案变了还是真没发。

    ⚠️ 本模式**只读全表窗口**（`/admin/all`，retention 100 行），
    是纯诊断用途，不要用它做常规收信（常规收信走按收件人索引的端点）。

    ⚠️ **只支持 CF 后端**：Remail 根本没有"扫全窗口"的端点
    （取件必须带 `email` + `token`）。这里显式拒绝并给出替代做法，
    而不是让它落到 `scan_all()` 里抛异常 —— 那个异常长得像故障，
    而真相是"这个后端没有这个能力"，两者处置不同。
    """
    if backend == "remail":
        print("✗ scan 只支持 CF 后端：Remail 没有'扫全窗口'端点"
              "（取件必须按 email + serviceToken 走）。", file=sys.stderr)
        print("  替代：对单个地址跑 `--mode resume --email <地址>`，"
              "看能不能取到信；规则分桶请用 CF 后端跑。", file=sys.stderr)
        return 1

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
        mark = "★" if rule.name == "welcome_confirm" else "·"
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


def report(recs: list[AccountRecord], *, json_path: str = "") -> int:
    """结果汇总。分类口径与台账状态一一对应，不要在这里另造一套词。"""
    print("\n" + "=" * 74)
    print("结果汇总")
    print("=" * 74)
    keyed = sum(1 for r in recs if r.status == "keyed")
    # `invite_only` 已不是常态（邀请制取消），但 403 仍可能随时回来 ——
    # 保留这个分类，否则它会静默混进"其它失败"，看不出是站点改了策略。
    blocked = sum(1 for r in recs if "invite_only" in (r.error or ""))
    other = len(recs) - keyed - blocked
    print(f"  拿到 key {keyed} / 受邀请制阻断 {blocked} / 其它失败 {other}"
          f" / 合计 {len(recs)}")
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
    ap.add_argument("--mode", choices=["full", "resume", "scan"], default="full",
                    help="full=建新邮箱跑全链路（默认）；"
                         "resume=对 --email 给的已知邮箱跑（可反复重跑）；"
                         "scan=列出邮箱窗口内全部邮件并按规则分桶（只读诊断）")
    ap.add_argument("--count", type=int, default=1, help="批次数量（mode=full）")
    ap.add_argument("--email", action="append", default=[], help="指定邮箱（mode=resume）")
    ap.add_argument("--mail-backend", choices=list(MAIL_BACKENDS),
                    default=DEFAULT_MAIL_BACKEND,
                    help="邮箱后端。cf=Cloudflare 临时邮箱 Worker（默认，建邮箱免费）；"
                         "remail=remail.aishop6.com 聚合（**每建一个邮箱真实下单扣积分**，"
                         "可买 outlook/gmail/icloud/自有域名，取件凭证按邮箱落盘）")
    ap.add_argument("--domain", default="",
                    help="地址后缀，默认按后端取配置。⚠️ 两个后端语义不同："
                         "cf 传**完整域名**；remail 传**商品名/emailSuffix**"
                         "（如 domain / outlook.com，不接受完整邮箱地址）")
    ap.add_argument("--login-mode", choices=["link", "code"], default="link",
                    help="link=确认邮件里的魔法链接（**默认，站点主路径**）；"
                         "code=6 位验证码（站点仍支持，但码 10 分钟过期且实测会丢包）")
    ap.add_argument("--mail-timeout", type=float, default=None,
                    help=f"等确认邮件的秒数（默认取 stages.MAIL_TIMEOUT="
                         f"{MAIL_TIMEOUT:.0f}；调小会把迟到但成功的邮件误判成失败）")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发账号数。默认 1=串行；>1 时每个 worker 用独立的会话与"
                         "邮箱客户端，只共享台账。站点对'短时间大量发信'的容忍度未知，"
                         "建议先小批串行试跑")
    ap.add_argument("--key-name", default="1", help="API Key 名称")
    ap.add_argument("--json", default="", help="把结果写到这个文件")
    ap.add_argument("--doctor", action="store_true", help="只做环境体检")
    args = ap.parse_args()

    if args.doctor:
        return cmd_doctor(args.mail_backend)

    # 必需配置**按后端取**：用 CF 跑批时不该被 Remail 的配置缺失拦住（反之亦然）。
    missing = (config.validate_remail() if args.mail_backend == "remail"
               else config.validate())
    if missing:
        print(f"✗ 后端 {args.mail_backend} 缺少必需配置：{'、'.join(missing)}",
              file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值", file=sys.stderr)
        return 1

    # scan 是纯只读诊断，自己建 client，不需要 Pipeline
    if args.mode == "scan":
        return cmd_scan(args.mail_backend)

    pipe = Pipeline(backend=args.mail_backend, domain=args.domain or None,
                    login_mode=args.login_mode)
    kw = {}
    if args.mail_timeout is not None:
        kw["mail_timeout"] = args.mail_timeout

    if args.mode == "resume":
        if not args.email:
            print("✗ mode=resume 需要至少一个 --email", file=sys.stderr)
            return 1
        recs = pipe.resume(args.email, name=args.key_name,
                           concurrency=args.concurrency, **kw)
    else:
        recs = pipe.run_batch(count=args.count, name=args.key_name,
                              concurrency=args.concurrency, **kw)

    return report(recs, json_path=args.json)


if __name__ == "__main__":
    sys.exit(main())
