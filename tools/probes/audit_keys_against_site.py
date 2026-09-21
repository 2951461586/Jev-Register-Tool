#!/usr/bin/env python
"""把交付物跟**站点侧**对一次账（不跟自己的台账比）。

为什么单开一个探针
──────────────────
`verify_keys.py` 验的是"我们记下来的 key 能不能用"，它回答不了另一个方向的问题：
**站点上有没有我们根本没记下来的 key？**

台账和交付物是**同一条写路径**的产物——生产流程写台账、交付物从台账导出。
两者对得再齐，也只能证明"没抄错"，不能证明"没漏掉"。
能对上的独立真源只有一个：站点自己的 `GET /api/api-keys`。

比对键位用 **`api_key_id`**（建 key 时服务端返回的 id），不用明文 key：
列表接口只给元数据，明文只在**创建**那一次返回，本来就再也拿不回来。

    python tools/probes/audit_keys_against_site.py --sample exports/_audit_sample.txt
    python tools/probes/audit_keys_against_site.py --limit 5
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import ROOT  # noqa: E402,F401

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402
from src.runner import Pipeline  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402


def ours(rows: list[dict], email: str) -> set[str]:
    """台账**原始行**里该账号记下的 api_key_id（不去重、不合并视图）。"""
    return {r["api_key_id"] for r in rows
            if r.get("key", "").strip() == email
            and r.get("api_key_id")
            and r.get("api_key")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", help="邮箱清单文件（每行一个）")
    ap.add_argument("--limit", type=int, default=5, help="未给清单时的随机抽样数")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--mail-timeout", type=float, default=90.0)
    a = ap.parse_args()

    rows = [json.loads(l) for l in
            Path(config.LEDGER_PATH).read_text(encoding="utf-8").splitlines() if l.strip()]

    if a.sample:
        emails = [l.strip() for l in Path(a.sample).read_text(encoding="utf-8").splitlines()
                  if l.strip()]
    else:
        pool = sorted({r["key"].strip() for r in rows if r.get("api_key")})
        random.seed(a.seed)
        emails = random.sample(pool, min(a.limit, len(pool)))

    print(f"抽样 {len(emails)} 个账号，逐登录并拉站点侧 api_keys 列表\n")

    # 🔴 必须给临时 success_ledger：默认值是 result/success.jsonl（**交付物**），
    # 用默认跑等于往交付物里灌假记录（实测灌进去过 9 条 apikey_FAKE_*）。
    tmp = Path(tempfile.mkdtemp())
    miss_total = extra_total = 0
    audited = 0

    for em in emails:
        pipe = Pipeline(mail=TempMailClient(), ledger=Ledger(tmp / "l.jsonl"),
                        success_ledger=Ledger(tmp / "s.jsonl"), verbose=False)
        rec = pipe.new_record(em) if hasattr(pipe, "new_record") else None
        if rec is None:
            from src.stages import AccountRecord
            rec = AccountRecord(key=em, email=em)
        try:
            cl = pipe.stage_login(rec, mail_timeout=a.mail_timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"  {em}: 登录异常 {type(exc).__name__}: {exc}")
            continue
        if cl is None:
            print(f"  {em}: 登录失败 —— {rec.status} / {rec.error}")
            continue
        try:
            site = cl.list_api_keys()
        except Exception as exc:  # noqa: BLE001
            print(f"  {em}: 列表接口异常 {type(exc).__name__}: {exc}")
            continue

        site_ids = {str(k.get("id") or k.get("api_key_id") or "") for k in site}
        site_ids.discard("")
        our_ids = ours(rows, em)
        miss = site_ids - our_ids      # 站点有、我们没记 ⇒ 漏了
        extra = our_ids - site_ids     # 我们记了、站点没有 ⇒ 可能是别的账号/已删
        audited += 1
        miss_total += len(miss)
        extra_total += len(extra)
        flag = "OK" if not miss and not extra else "⚠ 不一致"
        print(f"  {em}: 站点 {len(site_ids)} / 台账 {len(our_ids)}  [{flag}]")
        if miss:
            print(f"      ⚠ 站点有、台账漏记 {len(miss)}: {sorted(miss)[:5]}")
        if extra:
            print(f"      ⚠ 台账有、站点没有 {len(extra)}: {sorted(extra)[:5]}")

    print(f"\n审计 {audited}/{len(emails)} 个账号：漏记 {miss_total} / 多余 {extra_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
