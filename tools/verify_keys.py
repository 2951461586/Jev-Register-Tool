#!/usr/bin/env python
"""验证台账里的 API Key 是否**真的能调通**，并导出可用凭据。

为什么必须做这一步：拿到 `apikey_...` 只证明"创建接口返回了字符串"，
不证明这个 key 在推理网关上有效（可能被风控、配额、组织未激活挡掉）。

用法：
    python tools/verify_keys.py                 # 验证台账里全部有 key 的记录
    python tools/verify_keys.py --out exports/keys.txt
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401  （副作用：把仓库根加进 sys.path）

import requests  # noqa: E402

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402

API_URL = "https://api.typesafe.ai/v1/systemone"

PROBE_BODY = {
    "state": "The build has been failing on CI for three days and the release is tomorrow.",
    "model": "jev-latest",
    "questions": {
        "is_urgent": {"type": "noul",
                      "instructions": "Does this message convey urgency or time-sensitivity?"},
    },
}


def verify(key: str, *, timeout: float = 90.0) -> dict:
    t0 = time.time()
    try:
        r = requests.post(API_URL,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=PROBE_BODY, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}",
                "elapsed": time.time() - t0}
    el = time.time() - t0
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:200]}
    if r.status_code == 200 and "answers" in body:
        return {"ok": True, "status": 200, "model": body.get("model", ""),
                "noul": (body.get("answers", {}).get("is_urgent", {}) or {}).get("noul"),
                "usage": body.get("usage", {}), "elapsed": el}
    err = body.get("error") or body.get("code") or str(body)[:160]
    return {"ok": False, "status": r.status_code, "error": str(err)[:200], "elapsed": el}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="exports/keys.txt")
    ap.add_argument("--json", default="exports/keys_verified.json")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    led = Ledger(config.LEDGER_PATH)
    recs = [r for r in led.load() if r.get("api_key")]
    # 同一邮箱可能有多条（重跑过），按 key 去重
    seen: dict[str, dict] = {}
    for r in recs:
        seen.setdefault(r["api_key"], r)
    items = list(seen.items())
    if args.limit:
        items = items[:args.limit]
    print(f"台账 {config.LEDGER_PATH}：{len(recs)} 条有 key 的记录，去重后 {len(items)} 个唯一 key\n")

    results = []
    for i, (key, rec) in enumerate(items, 1):
        v = verify(key)
        v["email"] = rec.get("email", "")
        v["api_key"] = key
        v["api_key_id"] = rec.get("api_key_id", "")
        results.append(v)
        flag = "✓" if v["ok"] else "✗"
        extra = (f"model={v.get('model')} noul={v.get('noul')} "
                 f"tokens={v.get('usage', {}).get('input_tokens', '?')}") if v["ok"] \
            else f"HTTP {v['status']} {v.get('error', '')[:70]}"
        print(f"  [{i}/{len(items)}] {flag} {rec.get('email', ''):<42} "
              f"{v['elapsed']:.1f}s  {extra}")

    ok = sum(1 for v in results if v["ok"])
    print(f"\n可用 {ok} / 不可用 {len(results) - ok} / 合计 {len(results)}")

    # 落盘：一份机器可读，一份人可读（复制粘贴用）
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    lines = ["# TypeSafe / Jev API Keys（已实测可用）",
             f"# 验证时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"# 端点：POST {API_URL}",
             "# 认证：Authorization: Bearer <key>",
             "# 格式：<email>----<api_key>----<api_key_id>", ""]
    for v in results:
        if v["ok"]:
            lines.append(f"{v['email']}----{v['api_key']}----{v['api_key_id']}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {args.out} 和 {args.json}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
