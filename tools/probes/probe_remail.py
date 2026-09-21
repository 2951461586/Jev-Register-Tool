#!/usr/bin/env python
"""Remail 后端诊断探针：**查库存 + 校验凭证台账**（全只读，绝不下单）。

为什么需要它
────────────
Remail 是**付费**后端（`create_mailbox()` = 真实下单扣积分），所以"先跑一次试试"
的代价是钱。下面两件事都是本探针能在**花钱之前**发现的：

  1. **默认后缀无货。** `domain` 商品曾从 0.01 积分/单变成 `totalAvailable: 0`，
     下单直接 `HTTP 422 Insufficient inventory.` —— 代码没错，是**上游库存变了**，
     但默认值必须换。实测当时最便宜的有货档是 `outlook.com` **8 积分/单**
     （原价的 **800 倍**）。⇒ 本探针每次都会把"默认后缀还有多少货、单价多少、
     按当前余额最多还能买几单"摆出来。

  2. **凭证台账缺失/损坏。** `GET /v1/pickup` **不认 API Key**，只认
     `email + serviceToken`，而 token 只存在于**下单那个进程**的内存里 ⇒
     跨进程补跑全靠 `result/remail_orders.jsonl`。这个文件坏了或被清掉，
     表现是"补跑时全部报没有 serviceToken"，读起来像站点坏了。

回答四个问题
────────────
  1. 凭据齐不齐、API Key 还能不能用（`enabled` / 余额）
  2. 默认后缀**现在**有没有货、单价多少、按余额最多还能买几单
  3. 凭证台账有没有坏行 / 缺字段 / 同地址多单
  4. 有没有**"花了钱但没拿到 key"**的订单（真金白银的浪费，必须报出来）

用法：
    python tools/probes/probe_remail.py
    python tools/probes/probe_remail.py --json exports/remail_probe.json

退出码：0 全绿 / 1 有告警 / 2 凭据缺失 / 3 接口不可用

🔴 本探针**只发 GET**。任何会扣积分的调用都不在这里 —— 想验证"下单链路能不能通"，
   用 `tools/run_e2e.py --doctor --mail-backend remail`（那条也刻意只读）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import ROOT  # noqa: E402,F401

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402
from src.remail import RemailClient, RemailError  # noqa: E402

#: 告警收集器。**不抛异常** —— 探针的职责是"把所有问题一次报完"，
#: 遇到第一条就退出会让人来回跑好几趟。
WARN: list[str] = []


def warn(msg: str) -> None:
    WARN.append(msg)
    print(f"  ⚠ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── 1. 凭据 ──────────────────────────────────────────────────────────────
def check_credentials() -> bool:
    print("\n[1/4] 凭据")
    missing = config.validate_remail()
    if missing:
        print(f"  ✗ 缺 {missing}（Remail 后端需要 REMAIL_BASE + REMAIL_API_KEY）")
        print("    ⇒ 写进 .env 后重跑。凭据只走环境变量，代码里不留默认值。")
        return False
    ok(f"REMAIL_BASE = {config.REMAIL_BASE}")
    ok(f"REMAIL_API_KEY 已配置（长度 {len(config.REMAIL_API_KEY)}，不回显）")
    ok(f"REMAIL_PROJECT_ID = {config.REMAIL_PROJECT_ID}"
       f"  REMAIL_SERVICE_MODE = {config.REMAIL_SERVICE_MODE}")
    ok(f"REMAIL_EMAIL_SUFFIX = {config.REMAIL_EMAIL_SUFFIX}")
    return True


# ── 2. API Key 体检 ──────────────────────────────────────────────────────
def check_profile(cl: RemailClient) -> dict:
    print("\n[2/4] API Key 体检（GET /v1/open/apikey/profile）")
    prof = cl.health()
    ak = prof.get("apiKey") or {}
    if not ak:
        warn(f"响应里没有 apiKey 字段，原始 keys={sorted(prof)}")
        return {}
    balance = _f(ak.get("balance"))
    ok(f"key id={ak.get('id')}  name={ak.get('name')!r}  "
       f"enabled={ak.get('enabled')}  balance={ak.get('balance')}")
    if ak.get("enabled") is False:
        warn("API Key 的 enabled=False —— 所有调用都会失败，先去控制台启用")
    if balance <= 0:
        warn(f"余额 {balance} ≤ 0 —— 下单必然失败")
    return ak


# ── 3. 成本与库存 ────────────────────────────────────────────────────────
def check_inventory(cl: RemailClient, balance: float) -> dict:
    """返回 `{"default": {...}, "rows": [...]}` 供 JSON 落盘。"""
    print(f"\n[3/4] 项目与库存（GET /v1/open/projects/{config.REMAIL_PROJECT_ID}）")
    # ⚠️ 这个端点**要鉴权**（漏了 Authorization 会得到 401 Authentication is required）。
    data = cl._request("GET", f"/v1/open/projects/{config.REMAIL_PROJECT_ID}",
                       headers=cl._auth()).json()
    proj = data.get("project") or {}
    products = data.get("products") or []
    print(f"  项目 {proj.get('id')} {proj.get('name')!r} "
          f"status={proj.get('status')} 商品数={proj.get('productCount')} "
          f"收信规则数={proj.get('mailRuleCount')}")

    mode = (config.REMAIL_SERVICE_MODE or "code").strip().lower()
    price_field = "purchasePrice" if mode == "purchase" else "codePrice"
    enable_field = "purchaseEnabled" if mode == "purchase" else "codeEnabled"
    print(f"  ⓘ serviceMode={mode!r} ⇒ 看 `{price_field}` / `{enable_field}`")

    want = (config.REMAIL_EMAIL_SUFFIX or "").strip()
    rows: list[dict] = []
    hit: dict | None = None

    print(f"  {'商品':<14}{'状态':<10}{'单价':>10}{'库存':>14}  默认后缀命中")
    for p in products:
        ptype = str(p.get("type") or "")
        price = _f(p.get(price_field))
        avail = int(p.get("totalAvailable") or 0)
        enabled = bool(p.get(enable_field))
        suf_hit = ""
        for s in (p.get("suffixes") or []):
            if str(s.get("suffix")) == want:
                suf_hit = f"★ {want} 库存 {s.get('totalAvailable')}"
                hit = {"type": ptype, "suffix": want, "price": price,
                       "available": int(s.get("totalAvailable") or 0),
                       "enabled": enabled, "codeWindowMinutes":
                           p.get("codeWindowMinutes"),
                       "activationWindowMinutes": p.get("activationWindowMinutes")}
        rows.append({"type": ptype, "status": p.get("status"),
                     "price": price, "totalAvailable": avail,
                     "enabled": enabled,
                     "suffixes": [{"suffix": s.get("suffix"),
                                   "available": int(s.get("totalAvailable") or 0)}
                                  for s in (p.get("suffixes") or [])]})
        print(f"  {ptype:<14}{str(p.get('status')):<10}{price:>10.2f}{avail:>14,}  "
              f"{suf_hit}")

    # 全局最便宜的有货档 —— 换 suffix 时的决策依据（比"库存最大"更有用）
    cheapest = sorted(
        ((_f(p.get(price_field)), str(s.get("suffix")), str(p.get("type")),
          int(s.get("totalAvailable") or 0))
         for p in products if p.get(enable_field)
         for s in (p.get("suffixes") or []) if int(s.get("totalAvailable") or 0) > 0),
        key=lambda x: (x[0], -x[3]))
    if cheapest:
        c = cheapest[0]
        print(f"  ⓘ 最便宜的有货档：{c[1]!r}（{c[2]}）{c[0]:.2f} 积分/单，库存 {c[3]:,}")

    # 🔴 决定性判据：默认后缀到底能不能下单
    if hit is None:
        warn(f"默认后缀 {want!r} **不在任何商品的 suffixes 里** ⇒ 下单会 422")
        # 给出替代：有货且启用的商品里，最便宜的 suffix
        cands = [(int(s.get("totalAvailable") or 0), _f(p.get(price_field)),
                  str(s.get("suffix")), str(p.get("type")))
                 for p in products if p.get(enable_field)
                 for s in (p.get("suffixes") or []) if int(s.get("totalAvailable") or 0) > 0]
        if cands:
            cands.sort(key=lambda x: (x[1], -x[0]))
            print(f"    ⇒ 有货且最便宜的替代：{cands[0][2]!r}"
                  f"（{cands[0][3]}，{cands[0][1]:.2f} 积分/单，"
                  f"库存 {cands[0][0]:,}）")
            print(f"       改法：.env 里 REMAIL_EMAIL_SUFFIX={cands[0][2]}")
    elif not hit["enabled"]:
        warn(f"默认后缀 {want!r} 所在商品 `{hit['type']}` 的 {enable_field}=False "
             f"⇒ 该商品当前不可下单")
    elif hit["available"] <= 0:
        warn(f"默认后缀 {want!r} 库存为 0（商品 {hit['type']}）⇒ 下单会 422")
    else:
        n = int(balance // hit["price"]) if hit["price"] > 0 else 0
        ok(f"默认后缀 {want!r} 可下单：商品 {hit['type']}，"
           f"{hit['price']:.2f} 积分/单，库存 {hit['available']:,}")
        ok(f"按当前余额 {balance:.2f} 最多还能买 **{n}** 单"
           f"（成本上限 {n * hit['price']:.2f} 积分）")
        if hit.get("codeWindowMinutes"):
            ok(f"ⓘ codeWindowMinutes={hit['codeWindowMinutes']} —— 过窗口收不到新信，"
               f"只能用 relogin_pending 复用历史链接")
    return {"project": proj, "rows": rows, "default": hit, "price_field": price_field}


# ── 4. 凭证台账 ──────────────────────────────────────────────────────────
def check_state() -> dict:
    print(f"\n[4/4] 凭证台账（{config.REMAIL_STATE_PATH}）")
    path: Path = config.REMAIL_STATE_PATH
    if not path.is_file():
        ok("文件不存在 —— 正常（说明这个账号还没用 Remail 下过单）")
        return {"exists": False, "rows": [], "bad_lines": 0}

    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    good: list[dict] = []
    bad: list[str] = []
    for i, line in enumerate(raw, 1):
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except json.JSONDecodeError:
            bad.append(f"第 {i} 行不是 JSON（append-only 文件断电可能留半行）")
            continue
        good.append(d)

    ok(f"总行 {len(raw)}，可解析 {len(good)}，坏行 {len(bad)}")
    if bad:
        warn(f"坏行 {len(bad)} 条 —— 客户端恢复时会跳过它们（不整体失败），"
             f"但对应订单的取件凭证**永久丢了**：{bad[:3]}")

    # 字段完整性
    need = ("email", "token", "orderNo", "suffix", "at")
    for i, d in enumerate(good, 1):
        miss = [k for k in need if not d.get(k)]
        if miss:
            warn(f"第 {i} 条缺字段 {miss}（email={d.get('email')!r}）")

    # 同地址多单 —— 末行胜出，前面的订单凭证会被覆盖
    by_email: dict[str, list[dict]] = {}
    for d in good:
        by_email.setdefault(str(d.get("email") or ""), []).append(d)
    dup = {e: v for e, v in by_email.items() if len(v) > 1}
    if dup:
        warn(f"{len(dup)} 个地址**下过多次单**（读取时末行胜出，前面的凭证被覆盖）"
             f"⇒ 白花的钱：{ {e: len(v) for e, v in list(dup.items())[:3]} }")
    else:
        ok(f"{len(by_email)} 个地址，无重复下单")

    # 与主台账交叉：花了钱有没有换到 key
    led = Ledger(config.LEDGER_PATH)
    merged = {r.get("email"): r for r in led.load()}
    with_key = [e for e in by_email if (merged.get(e, {}).get("api_key") or "").strip()]
    no_key = [e for e in by_email if e not in with_key]
    ok(f"其中 {len(with_key)}/{len(by_email)} 个地址已拿到 api_key")
    if no_key:
        warn(f"**{len(no_key)} 个地址花了钱但没拿到 key**（真金白银的浪费）：")
        for e in no_key[:8]:
            r = merged.get(e, {})
            print(f"      {e}  status={r.get('status')!r}  "
                  f"err={(r.get('error') or '')[:70]!r}")
        if len(no_key) > 8:
            print(f"      …还有 {len(no_key) - 8} 个")

    # suffix 漂移：历史用过什么、当前默认是什么
    used = sorted({str(d.get("suffix")) for d in good if d.get("suffix")})
    print(f"  历史用过的 suffix：{used}；当前默认 {config.REMAIL_EMAIL_SUFFIX!r}")
    if config.REMAIL_EMAIL_SUFFIX not in used and used:
        print("  ⓘ 当前默认与历史不同 —— 只是提示，不是错误（换过商品）")

    return {"exists": True, "rows": good, "bad_lines": len(bad),
            "emails": sorted(by_email), "no_key": no_key, "used_suffixes": used}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把结果落盘（供后续对账）")
    args = ap.parse_args()

    print("=" * 72)
    print("Remail 后端诊断（只读；本探针**不会下单、不会扣积分**）")
    print("=" * 72)

    if not check_credentials():
        return 2

    cl = RemailClient()
    try:
        ak = check_profile(cl)
    except RemailError as exc:
        print(f"  ✗ 接口不可用：{exc}")
        return 3

    try:
        inv = check_inventory(cl, _f(ak.get("balance")))
    except RemailError as exc:
        print(f"  ✗ 项目/库存查询失败：{exc}")
        return 3

    state = check_state()

    print("\n" + "=" * 72)
    if WARN:
        print(f"结果：{len(WARN)} 条告警")
        for w in WARN:
            print(f"  ⚠ {w}")
    else:
        print("结果：全绿 ✅")
    print("=" * 72)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"profile": ak, "inventory": inv,
                                   "state": state, "warnings": WARN},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"已写到 {out}")

    return 1 if WARN else 0


if __name__ == "__main__":
    sys.exit(main())
