#!/usr/bin/env python
"""把台账里被尾部空白/CR 污染的 key 归一化。

默认**只 strip `key` / `email`，不折叠行** —— 见下面"为什么不去重"。

背景（2026-09-20 实测事故）
────────────────────────
`tools/run_e2e.py --mode resume --email <addr>` 的邮箱列表是用

    mapfile -t EM < file.txt

从文件里读的。`-t` 只剥行尾的 `\\n`，**不剥 `\\r`**；而文件是 Python 文本模式写的
（`\\n` → `\\r\\n`）。于是每个地址都带上了尾部 `\\r` ⇒ 台账里多出一类
`"oai-xxx@<邮箱域名>\\r"` 的键，与干净键**互不相同** ⇒ 同一账号出现两条记录。

更糟的是带 `\\r` 的地址在站点侧有时能过（多数拿到 403）、有时不能（验证码邮件
永远等不到，超时 120s 才失败），所以"取码失败"的分布看起来像随机噪声。

为什么必须归一化而不是留着
────────────────────────
这些幽灵键是 `failed`（0 分）。它们不会让 `applied/confirmed` 记录降级
（`merge` 的降级分支会保住旧记录），但会：
  · 让 `Ledger.load()` 的条数虚高（127 → 235），掩盖真实账号数；
  · 让"失败清单"里出现上百条并不存在的账号。

🔴 为什么**不**顺便按 key 去重（本工具第一版踩过）
──────────────────────────────────────────────────
同一账号被重跑拿到**第二把** key 时，`merge()` 的"同级并集、新值胜出"会让
第二把吃掉第一把 —— 而**两把在服务端都有效**。所以：
  · `Ledger.load()`（合并视图）本来就比原始行少几把 key，这是**预期行为**；
  · `tools/verify_keys.py` 因此特意取"合并视图 ∪ 原始行"做并集；
  · 一旦这里把文件**物理折叠**成去重后的形态，那几把只在原始行里存在的 key
    就**永久消失**了 —— 交付物静默少行，且不报错。
⇒ 归一化只改 `key` / `email` 的字符串，行数与 `api_key` 一个都不能少。

🔴 硬规矩：**改台账前先备份**；写回后必须自证"行数不减、key 不减、api_key 不减"。

用法：
    python tools/normalize_ledger.py             # 演练（默认 --apply 前先看）
    python tools/normalize_ledger.py --apply     # 真的写回
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: E402,F401

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402


def load_raw(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(config.LEDGER_PATH))
    ap.add_argument("--apply", action="store_true", help="真的写回（默认只演练）")
    args = ap.parse_args()

    path = Path(args.ledger)
    rows = load_raw(path)
    before_rows = len(rows)
    before_keys = {r.get("key") for r in rows if r.get("key")}

    dirty = [r for r in rows if r.get("key") and r["key"] != r["key"].strip()]
    dirty_keys = {r["key"] for r in dirty}
    print(f"台账 {path}：{before_rows} 行 / {len(before_keys)} 个唯一 key")
    print(f"  带尾部空白的 key：{len(dirty)} 行 / {len(dirty_keys)} 个")

    # 1) 归一化：key 与 email 都 strip（两处必须一起，否则台账里键与地址不一致）
    norm: list[dict] = []
    for r in rows:
        r = dict(r)
        if isinstance(r.get("key"), str):
            r["key"] = r["key"].strip()
        if isinstance(r.get("email"), str):
            r["email"] = r["email"].strip()
        norm.append(r)

    # 2) **不折叠行**：只改字符串，行数保持不变（理由见模块 docstring）
    out = norm
    after_keys = {r.get("key") for r in out if r.get("key")}
    print(f"  归一化后：{len(out)} 行 / {len(after_keys)} 个唯一 key"
          f"（合并视图 `Ledger.load()` 会自动去重到这个数）")

    # 3) 自证三条：行数不减、key 不减、api_key 不减
    if len(out) != before_rows:
        print(f"✗ 行数变了（{before_rows} → {len(out)}），拒绝写回")
        return 1
    lost = {k.strip() for k in before_keys} - after_keys
    if lost:
        print(f"✗ 归一化会丢掉 {len(lost)} 个 key，拒绝写回：{list(lost)[:5]}")
        return 1
    before_creds = {r.get("api_key") for r in rows if r.get("api_key")}
    after_creds = {r.get("api_key") for r in out if r.get("api_key")}
    lost_creds = before_creds - after_creds
    if lost_creds:
        print(f"✗ 归一化会丢掉 {len(lost_creds)} 把 api_key，拒绝写回")
        return 1
    print(f"  ✓ 自证通过：行数 {len(out)} 不减；唯一 key {len(after_keys)}；"
          f"api_key {len(before_creds)} 把全部保留")

    if not args.apply:
        print("\n（演练模式，未写回。加 --apply 真的执行）")
        return 0

    # 4) 备份 → 临时文件 → 原子替换
    bak = path.with_name(
        f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    bak.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".normtmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    print(f"\n已写回 {path}（备份 {bak.name}）")

    # 5) 复核：重读一遍，确认能读回同样条数
    chk = Ledger(path).load()
    print(f"复核：重读 {len(chk)} 条，其中有 api_key 的 "
          f"{sum(1 for r in chk if r.get('api_key'))} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
