#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立复核（不与跑批共用校验逻辑）。

判据（按 pooled-batch-run-execution 规程）：
  ① 另起脚本、不复用 run_e2e / verify_keys 的函数；
  ② 取样来自**权威台账**，且用**与开跑前备份做差分**（不是时间窗、不是导出快照）；
  ③ 打真实接口（由 verify_keys.py 承担；本脚本只做差分与覆盖对账）。

用法：
  python tools/_recheck_diff.py --base exports/ledger.jsonl.bak-<TS>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rows(p: Path) -> list[dict]:
    rows = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="跑批前的台账备份")
    ap.add_argument("--cur", default=str(ROOT / "exports" / "ledger.jsonl"))
    ap.add_argument("--keys", default=str(ROOT / "result" / "keys.txt"))
    ap.add_argument("--apikeys", default=str(ROOT / "result" / "apikeys.txt"))
    ap.add_argument("--expect", type=int, default=101, help="本批预期新增账号数")
    a = ap.parse_args()

    base_p = Path(a.base)
    if not base_p.is_absolute():
        base_p = ROOT / base_p
    cur_p = Path(a.cur)

    print("=" * 88)
    print("独立复核 · 台账差分 + 交付物覆盖对账")
    print("=" * 88)
    print(f"基线：{base_p.name}")
    print(f"当前：{cur_p.name}")

    # ── 口径说明：按**邮箱**取集合（账号级）──
    base_rows = load_rows(base_p)
    cur_rows = load_rows(cur_p)
    base_em = {r.get("email") for r in base_rows if r.get("email")}
    cur_em = {r.get("email") for r in cur_rows if r.get("email")}
    print(f"\n原始行：{len(base_rows)} → {len(cur_rows)}"
          f"（+{len(cur_rows) - len(base_rows)}）")
    print(f"去重邮箱：{len(base_em)} → {len(cur_em)}")

    added = cur_em - base_em
    removed = base_em - cur_em
    print(f"\n★ 本批新增邮箱：{len(added)}（期望 {a.expect}）")
    print(f"★ 旧记录丢失：{len(removed)}（必须为 0）")
    if removed:
        print(f"  ⚠ 丢失样例：{sorted(removed)[:5]}")

    # 新增里带凭据（key）的
    #
    # 🔴 凭据字段是 **`api_key`**，不是 `key` —— 台账里 `key` 存的是**邮箱**
    #    （`AccountRecord(key=email, email=email)`），拿它当凭据会把计数虚增。
    #    实测踩到：`key or api_key` 的回退写法把 288 条无凭据记录算成凭据
    #    （1579 vs 真实 1291），而"只在交付物、不在台账"那一侧照样是 0 ⇒
    #    **单侧判据掩盖了另一侧的口径错误**。
    #    护栏：抽出来的凭据集合里出现 `@` 就说明字段选错了。
    def key_of(r: dict) -> str:
        v = (r.get("api_key") or "").strip()
        return v

    def _guard_creds(name: str, creds: set[str]) -> None:
        bad = [c for c in creds if "@" in c]
        if bad:
            raise SystemExit(
                f"{name}：抽出的凭据里有 {len(bad)} 条含 `@` ⇒ 字段选错了"
                f"（样例 {bad[:2]}）—— 凭据字段是 `api_key`，`key` 是邮箱")

    new_keyed = [r for r in cur_rows
                 if r.get("email") in added and key_of(r)]
    print(f"★ 新增里带凭据的记录：{len(new_keyed)}")

    # ── 凭据级：交付物抽取 ──
    keys_p = Path(a.keys)
    apikeys_p = Path(a.apikeys)
    kt_emails: set[str] = set()
    kt_creds: set[str] = set()
    for line in keys_p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 3:
            kt_emails.add(parts[0])
            kt_creds.add(parts[1])
    ak_creds = {ln.strip() for ln in
                apikeys_p.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()}

    print(f"\n交付物 keys.txt：{len(kt_emails)} 个邮箱 / {len(kt_creds)} 把凭据")
    print(f"交付物 apikeys.txt：{len(ak_creds)} 把凭据")

    # ── 覆盖判据：本批新增的邮箱是否都进了交付清单 ──
    miss = sorted(added - kt_emails)
    print(f"\n★ 本批新增里**未进 keys.txt** 的：{len(miss)}（必须为 0）")
    for e in miss[:10]:
        print(f"    ✗ {e}")

    # ── 强交叉验证：台账原始行去重 key vs 交付物凭据 ──
    ledger_keys = {key_of(r) for r in cur_rows if key_of(r)}
    _guard_creds("台账原始行", ledger_keys)
    _guard_creds("交付物 keys.txt", kt_creds)
    _guard_creds("交付物 apikeys.txt", ak_creds)
    print(f"\n★ 强交叉验证：")
    print(f"    台账原始行去重 key       = {len(ledger_keys)}")
    print(f"    交付物 keys.txt 凭据     = {len(kt_creds)}")
    print(f"    交付物 apikeys.txt 凭据  = {len(ak_creds)}")
    only_ledger = ledger_keys - kt_creds
    only_deliv = kt_creds - ledger_keys
    print(f"    只在台账、不在交付物：{len(only_ledger)}")
    print(f"    只在交付物、不在台账：{len(only_deliv)}")
    if only_ledger:
        print(f"      ⚠ 样例：{sorted(only_ledger)[:3]}")
        print("      ⚠ 两种可能：① 凭据字段选错（该用 `api_key` 用了 `key`）"
              "② 有 key 验收未通过（401/403）被排除")
    if only_deliv:
        print(f"      ⚠ 样例：{sorted(only_deliv)[:3]}")

    # ⚠ 这一侧也要进判据 —— 只查"交付物 ⊆ 台账"会漏掉口径错误：
    #   本项目实测 `key or api_key` 的回退写法把 288 条无凭据记录算成凭据，
    #   而 only_deliv 照样是 0 ⇒ **单侧判据掩盖了另一侧的口径错误**。
    ok = (not removed) and (not miss) and len(added) == a.expect \
        and not only_ledger and not only_deliv
    print("\n" + ("✓ 差分与覆盖判据全部通过" if ok else "✗ 有判据未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
