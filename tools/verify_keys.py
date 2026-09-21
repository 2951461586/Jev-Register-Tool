#!/usr/bin/env python
"""验证台账里的 API Key 是否**真的能调通**，并导出可用凭据到 `result/`。

为什么必须做这一步：拿到 `apikey_...` 只证明"创建接口返回了字符串"，
不证明这个 key 在推理网关上有效（可能被风控、配额、组织未激活挡掉）。

候选集 = 台账**合并视图** ∪ **原始行**：合并视图按邮箱去重（同一账号重跑
拿到第二把 key 时第一把会被吃掉），而两把在服务端都有效 ⇒ 交付不能少行。

产物（全部落在 **`result/`**，即交付物目录）：

    result/success.jsonl        成功**账号**（账号级：每邮箱一行；从台账补录，幂等）
    result/keys.txt             email----api_key----api_key_id（人可读，直接复制）
    result/keys_verified.json   机器可读的验收结果
    result/apikeys.txt          纯 api_key，一行一个（keys.txt 的**无元数据版**）

用法：
    python tools/verify_keys.py                      # 验证全部有 key 的记录
    python tools/verify_keys.py --limit 3            # 只验前 3 个
    python tools/verify_keys.py --ledger other.jsonl  # 换源台账
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
    ap.add_argument("--out", default=str(config.KEYS_TXT_PATH),
                    help=f"人可读凭据清单（默认 {config.KEYS_TXT_PATH}）")
    ap.add_argument("--json", default=str(config.KEYS_JSON_PATH),
                    help=f"机器可读验收结果（默认 {config.KEYS_JSON_PATH}）")
    ap.add_argument("--apikeys", default=str(config.APIKEYS_TXT_PATH),
                    help=f"纯 api_key 清单，一行一个（默认 {config.APIKEYS_TXT_PATH}）")
    ap.add_argument("--ledger", default=str(config.LEDGER_PATH),
                    help=f"源台账（默认 {config.LEDGER_PATH}）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    led = Ledger(Path(args.ledger))
    recs = [r for r in led.load() if r.get("api_key")]

    # 🔴 再补一遍**原始行**里的 key。
    # `load()` 是按 key（邮箱）去重的合并视图 ⇒ 同一账号被重跑拿到**第二把**
    # key 时，第一把会被吃掉 —— 而它在服务端仍然有效。
    # 对"交付凭据"来说，静默少一行与上一轮审计里 P0 的后果是同一类错误。
    # 所以候选集 = 合并视图（权威元数据）∪ 原始行（全部 key），按 api_key 去重。
    have = {r["api_key"] for r in recs}
    extra = [r for r in led.raw_rows() if r.get("api_key") and r["api_key"] not in have]
    if extra:
        for r in extra:
            have.add(r["api_key"])
        recs += extra
        print(f"另有 {len(extra)} 把 key 只存在于原始行里"
              f"（同账号的第二次建 key，合并视图按邮箱去重时会被吃掉）—— 一并验收")

    # 补录：把成功记录同步进 result/success.jsonl（**交付物**目录）。
    # 幂等（按 key 并集去重，且 EARNED_FIELDS 保证空值不覆盖已有凭据），可反复跑。
    # 这样"改目录约定之前"拿到的成功数据也会被补齐，不必手工搬。
    #
    # 🔴 `success.jsonl` 是**账号级**台账：`Ledger` 以 `key`（= 邮箱）为主键，
    # 同账号重跑拿到的**第二把** key 会覆盖前一把 ⇒ 落盘行数 == 唯一邮箱数，
    # 天然少于 `recs` 条数。这不丢数据（第二把 key 仍在 keys.txt /
    # keys_verified.json 里，那两个是**凭据级**的），但**日志必须自证落盘行数**：
    # 只印 "更新 129 / 未变 113"（= 242 条输入）会被读成"242 行都写进去了"，
    # 而 `upsert_many` 自己的 docstring 就要求"增量写回必须能自证真的写进去了"。
    # 这条原则必须在**调用点**兑现，不能只写在被调方。
    succ = Ledger(config.SUCCESS_LEDGER_PATH)
    st = succ.upsert_many(recs)
    n_rows = len(succ.raw_rows())
    emails_in = {r["key"] for r in recs}
    n_keys = len({r["api_key"] for r in recs})
    # 每条输入都必须被处理到（added/updated/kept 覆盖全集）
    assert st["added"] + st["updated"] + st["kept"] == len(recs), (
        f"upsert 只处理了 {st['added'] + st['updated'] + st['kept']} / {len(recs)} 条输入")
    # 「没丢行」的准确判据是**输入里的每个邮箱都出现在落盘结果里**。
    # ⚠️ 不能写成 `落盘行数 == 输入邮箱数`：success.jsonl 是**累积**文件，
    # 可能含主台账里已不存在的历史邮箱，那样会误炸。
    missing = emails_in - {r["key"] for r in succ.load()}
    assert not missing, (
        f"补录后仍有 {len(missing)} 个邮箱不在 success.jsonl 里：{sorted(missing)[:3]}")
    print(f"成功台账 {config.SUCCESS_LEDGER_PATH}："
          f"新增 {st['added']} / 更新 {st['updated']} / 未变 {st['kept']}"
          f"（输入 {len(recs)} 条）")
    print(f"  落盘 {n_rows} 行（**账号级**，每行一个邮箱）；输入落在 "
          f"{len(emails_in)} 个邮箱上"
          + (f"，比输入少 {len(recs) - len(emails_in)} 行 —— "
             f"同账号重跑的第二把 key 被按邮箱合并掉了。"
             f"**要凭据清单请看 keys.txt / keys_verified.json（{n_keys} 把，凭据级）**"
             if len(recs) > len(emails_in) else "，与输入一致"))

    # 同一邮箱可能有多条（重跑过），按 key 去重
    seen: dict[str, dict] = {}
    for r in recs:
        seen.setdefault(r["api_key"], r)
    items = list(seen.items())
    if args.limit:
        items = items[:args.limit]
    print(f"台账 {args.ledger}：{len(recs)} 条有 key 的记录，去重后 {len(items)} 个唯一 key\n")

    results = []
    for i, (key, rec) in enumerate(items, 1):
        v = verify(key)
        # 削首尾空白：候选清单若是 CRLF，邮箱尾部会带 `\r`，
        # 直接写进 keys.txt 会把交付物的一行拆坏（`\r` 是行分隔符）。
        v["email"] = str(rec.get("email", "") or "").strip()
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
    #
    # 🔴 必须显式 `newline=""`（= 只写 LF）。
    # 默认的文本模式在 Windows 上会把 `\n` 翻成 `\r\n`，于是**每一行尾部都带 CR**：
    #   · `keys.txt` 是凭据清单，在 Linux/WSL 下 `while read line` 读出来
    #     api_key 会变成 `apikey_xxx\r` ⇒ 直接 401，而肉眼完全看不出区别；
    #   · `success.jsonl` 是 JSONL，CR 会让某些严格解析器报错。
    # 这和"邮箱清单被 CR 污染"是同一个根因，别只修输入不修输出。
    def _write_lf(path: str, text: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    _write_lf(args.json, json.dumps(results, ensure_ascii=False, indent=2))
    lines = ["# TypeSafe / Jev API Keys（已实测可用）",
             f"# 验证时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"# 端点：POST {API_URL}",
             "# 认证：Authorization: Bearer <key>",
             "# 格式：<email>----<api_key>----<api_key_id>", ""]
    for v in results:
        if v["ok"]:
            lines.append(f"{v['email']}----{v['api_key']}----{v['api_key_id']}")
    _write_lf(args.out, "\n".join(lines) + "\n")

    # 纯 key 版：**与 keys.txt 同源、同一次运行**写出 ⇒ 两者集合恒等，只差元数据形态。
    # 单独产出的理由：下游常只要 key 本身，不该被迫解析 `email----key----id`。
    # ⚠️ 同样**只写 ok 的行** —— 把不可用的 key 放进交付清单等于交付坏数据。
    # 护栏：去重后条数必须等于可用数。`results` 已按 api_key 去重，这里再自证一次；
    # 将来若有人只给其中一个清单加过滤条件，这条会立刻炸，而不是让两份交付物静默分叉。
    apikeys = [v["api_key"] for v in results if v["ok"]]
    assert len(apikeys) == len(set(apikeys)), "apikeys.txt 出现重复 key"
    _write_lf(args.apikeys, "".join(f"{k}\n" for k in apikeys))

    print(f"已写入 {args.out}、{args.apikeys} 和 {args.json}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
