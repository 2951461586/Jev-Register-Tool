#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次报告脱敏自检 —— 落盘后必须跑，**全部检查项必须 0 命中**。

为什么要有这个脚本（而不是每次手敲 grep）：

🔴 **模式字面量会自命中。** 如果把检查项连同**字面量**一起写进报告
   （例如把密钥前缀、邮箱域名、JWT 前缀写进报告的"脱敏自检"表格里），
   那几行**自己就会被 grep 命中** —— 报告里出现若干命中，逐行核对发现
   **全是表格自己**，与数据无关。本项目实测踩到一次（第 6 批报告第一版）。

⇒ 所以：**正则只存在这个脚本里，报告里只写检查项的名字。**

🔴 **本文件是公开仓库的一部分，绝不能写真实域名。** 自有邮箱域名属于
   本机基础设施标识，仓库里其他地方一律靠环境变量（`TEMPMAIL_DOMAIN`）注入。
   ⇒ 裸域名清单**从外部加载**（见 `load_domains`），**加载不到就大声报"未生效"**，
   **绝不静默降级**（"检查面缩水"比漏检更危险）。
   ⇒ 顺带把「邮箱」这一项升级成**通用正则**：抓任意「本地部分@域名.顶级域」形态的地址
   （形如 `user@域名.tld`），不依赖任何清单，公开仓库里也能跑。

用法：
  python tools/_desense_check.py exports/batch-100-r6-2026-09-22.md
  python tools/_desense_check.py 报告 --domains a.example,b.example
  DESENSE_DOMAINS=a.example,b.example python tools/_desense_check.py 报告

退出码：0 = 全 0（可交付）；1 = 有命中（**不可交付**，先修报告）；2 = 用法/环境错误
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 裸域名清单的默认位置 —— **必须在 .gitignore 里**（本仓 `exports/` 已忽略）。
# 一行一个域名，`#` 开头是注释。
DOMAIN_FILE = ROOT / "exports" / "desense_domains.txt"

# 通用邮箱：抓「本地部分@域名.顶级域」形态（形如 user@域名.tld），不含任何本机信息。
EMAIL_RE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"

# 🔴 下面每一条都必须**自匹配安全**：把判据用在自己源码上时不许命中。
#    自查方法：`python tools/_desense_check.py tools/_desense_check.py` ⇒ 只该 0 命中。
CHECKS: list[tuple[str, str]] = [
    ("1 密钥前缀（SaaS key）", r"sk-[A-Za-z0-9]"),
    ("2 邮箱地址（通用正则）", EMAIL_RE),
    ("3 JWT 头部前缀", r"eyJ[A-Za-z0-9_\-]{6,}"),
    ("4 IPv4 字面量", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ("5 本机绝对路径", r"[A-Za-z]:\\{1,2}(?:Users|epsoft|software|IDE)\b"),
    ("6 凭据字面量", r"apikey_[0-9a-f]{8,}"),
    ("7 本机 AI 工作区路径", r"workbuddy[-_]?(?:work|ai|builtin)"),
]


def load_domains(arg: str | None) -> tuple[list[str], str]:
    """返回 `(域名清单, 来源说明)`。**加载不到就返回空清单 + 原因**（由调用方报警）。"""
    if arg:
        ds = [d.strip() for d in arg.split(",") if d.strip()]
        if ds:
            return ds, "--domains 参数"
    env = os.getenv("DESENSE_DOMAINS", "")
    if env.strip():
        ds = [d.strip() for d in env.split(",") if d.strip()]
        if ds:
            return ds, "环境变量 DESENSE_DOMAINS"
    if DOMAIN_FILE.is_file():
        ds = [ln.strip() for ln in DOMAIN_FILE.read_text(
            encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
        if ds:
            return ds, f"{DOMAIN_FILE.relative_to(ROOT)}（gitignored）"
    return [], f"未提供（试过 --domains / $DESENSE_DOMAINS / {DOMAIN_FILE.name}）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="报告路径")
    ap.add_argument("--show", type=int, default=3, help="每项最多显示几条命中")
    ap.add_argument("--domains", default=None,
                    help="逗号分隔的裸域名清单（覆盖默认来源）")
    ap.add_argument("--allow-no-domains", action="store_true",
                    help="允许「裸域名」这一项未生效（默认**不允许** —— 会退 2）")
    a = ap.parse_args()

    p = Path(a.report)
    if not p.is_file():
        print(f"找不到报告：{p}", file=sys.stderr)
        return 2
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    domains, src = load_domains(a.domains)
    checks = list(CHECKS)
    if domains:
        checks.append(("8 裸域名（外部清单）",
                       "|".join(re.escape(d) for d in domains)))

    print("=" * 78)
    print(f"脱敏自检：{p.name}")
    print("=" * 78)

    bad = 0
    for name, pat in checks:
        rx = re.compile(pat)
        hits = [(i + 1, ln.strip()[:110]) for i, ln in enumerate(lines) if rx.search(ln)]
        mark = "✓" if not hits else "✗"
        print(f"  {mark} {name:<26} 命中 {len(hits)}")
        if hits:
            bad += len(hits)
            for ln_no, ln in hits[: a.show]:
                print(f"        L{ln_no}: {ln}")
            if len(hits) > a.show:
                print(f"        … 另有 {len(hits) - a.show} 条")

    print("-" * 78)

    if not domains:
        # 🔴 绝不静默降级：这一项没跑，就必须让调用方知道，并默认判失败。
        print(f"  ⚠ 「裸域名」这一项**未生效**：{src}")
        print("    ⇒ 报告里若只出现裸域名（没有 `user@` 前缀）就抓不到。")
        print("    ⇒ 提供清单：--domains a.example,b.example"
              " 或写 exports/desense_domains.txt（已 gitignored）")
        if not a.allow_no_domains:
            print("    ⇒ 判为**不可交付**（确要放行请显式加 --allow-no-domains）")
            return 2
        print("    ⇒ --allow-no-domains 已显式放行，仅此一次。")

    if bad:
        print(f"✗ 共 {bad} 处命中 ⇒ **不可交付**，先修报告")
        print("  提示：若命中的是报告里的『检查项表格』，那是**自命中** ——")
        print("        表格里不许写模式字面量，只写检查项名字。")
        return 1
    print(f"✓ {len(checks)} 项全 0 ⇒ 可交付")
    return 0


if __name__ == "__main__":
    sys.exit(main())
