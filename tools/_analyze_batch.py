"""批次分析（**通用版**）：接受日志路径与基线备份路径，五项判据一次算完。

判据（缺一条就不能说"无回归"）：
  1. 成功率与失败类**分布**：有没有**新增**失败类（旧失败类是站点噪声，不算回归）；
  2. 丢包率（批内重发触发率）—— 铁律：**报速率必须同时报丢包率**；
  3. 阶段耗时：`login` / `mail_wait` / `create_key` 的 最快 / 中位 / 最慢 / 极差；
  4. **A1 接线证据**：`onboarding=skipped` 的比例、有没有任何一次真的走了全链
     （有 ⇒ A1 没生效或 strict 混进来了）；
  5. **A2 接线证据**：超时文案里的「轮询 N 次」—— 按新间隔 0.5s，同样时长的轮询次数
     应显著**高于**旧批次（旧：120s 窗口 53 次 ≈ 2.26s/轮）。

⚠️ 只读脚本：不写任何文件、不碰网络、不建账号。

用法：
    python tools/_analyze_batch.py --log exports/run_cf100_a1a2_<TS>.log \
        --base exports/ledger.jsonl.bak-<TS>
    # 不给 --base 时默认取 exports/ 下 mtime 最新的 ledger.jsonl.bak-*
"""
import argparse
import json
import re
import statistics as st
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.ledger import Ledger  # noqa: E402
#: 首次超时阈值。**必须从源码读**，不能在本脚本里抄一个数 ——
#: 抄了就会在"有人调 `MAIL_TIMEOUT`"时静默过期（本项目已有多次"注释里的
#: 具体数字自己变成假的"的先例）。
from src.stages import MAIL_TIMEOUT  # noqa: E402

EX = ROOT / "exports"
LIVE = EX / "ledger.jsonl"


def load_map(p: Path) -> dict:
    return {r.get("email"): r for r in Ledger(p).load() if r.get("email")}


def _q(v: list[float], p: float) -> float:
    """线性插值分位数（`statistics.quantiles` 在小样本上端点行为不稳，自己算）。"""
    v = sorted(v)
    i = (len(v) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def stats_block(rows: list[dict], label: str) -> None:
    print(f"\n--- {label}（n={len(rows)}）---")
    stages = sorted({k for r in rows for k in (r.get("timings") or {})})
    print(f"{'stage':<12}{'n':>4}{'最快':>9}{'中位':>9}{'最慢':>9}{'极差':>9}")
    for k in stages:
        v = [float((r.get("timings") or {})[k]) for r in rows
             if isinstance((r.get("timings") or {}).get(k), (int, float))]
        if not v:
            continue
        print(f"{k:<12}{len(v):>4}{min(v):>9.2f}{st.median(v):>9.2f}"
              f"{max(v):>9.2f}{max(v) - min(v):>9.2f}")


def newest_bak() -> Path:
    baks = sorted(EX.glob("ledger.jsonl.bak-*"), key=lambda p: p.stat().st_mtime)
    if not baks:
        raise SystemExit("找不到 ledger.jsonl.bak-*")
    return baks[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="批次日志路径")
    ap.add_argument("--base", default="", help="基线台账备份（默认取最新）")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="本批并发数（只用于折算吞吐；默认 4）")
    ap.add_argument("--wall", type=float, default=0.0,
                    help="本批**实测墙钟**秒数（给了就报实测吞吐，不给则用 "
                         "created_at 跨度估算并标注为估算）")
    a = ap.parse_args()

    log_p = Path(a.log)
    if not log_p.is_absolute():
        log_p = ROOT / a.log
    if not log_p.exists():
        raise SystemExit(f"找不到日志 {log_p}")
    base_p = Path(a.base) if a.base else newest_bak()
    if not base_p.is_absolute():
        base_p = ROOT / a.base

    log = log_p.read_text(encoding="utf-8", errors="replace")
    print("=" * 88)
    print(f"批次分析：{log_p.name}")
    print(f"基线台账：{base_p.name}")
    print("=" * 88)

    # ── 台账差分 ──────────────────────────────────────────────────────
    base, cur = load_map(base_p), load_map(LIVE)
    added = sorted(set(cur) - set(base))
    removed = sorted(set(base) - set(cur))
    new = [cur[e] for e in added]
    print(f"台账 {len(base)} → {len(cur)} 地址；本批新增 {len(added)}，"
          f"**旧记录丢失 {len(removed)}**")
    if removed:
        print(f"  ★ 消失样例：{removed[:3]}")
    print("新增 status 分布:", dict(Counter(r.get("status") for r in new).most_common()))

    keyed = [r for r in new if r.get("status") == "keyed"]
    failed = [r for r in new if r.get("status") == "failed"]
    n = len(new)
    print(f"★ 成功率 {len(keyed)}/{n} = {len(keyed) / max(n, 1) * 100:.0f}%")

    # ── 失败类分布 ────────────────────────────────────────────────────
    print("\n=== 失败类分布（看有没有**新增**类）===")
    for e, c in Counter(re.sub(r"\d{2,}", "N", str(r.get("error") or ""))[:96]
                        for r in failed).most_common():
        print(f"  {c:>3}  {e}")

    # ── A1 接线证据 ───────────────────────────────────────────────────
    print("\n=== A1 接线证据 ===")
    ob = Counter(str((r.get("stages") or {}).get("onboarding")) for r in new)
    print("  stages.onboarding 分布:", dict(ob.most_common()))
    skipped = sum(1 for r in new if (r.get("user") or {}).get("onboarding_skipped"))
    print(f"  user.onboarding_skipped=True 的账号: {skipped}/{n}")
    gates = Counter(str((r.get("user") or {}).get("onboarding_gates")) for r in new)
    print("  user.onboarding_gates 分布:", dict(gates.most_common(5)))
    bad = len(re.findall(r"\[onboarding\] ⚠ 门禁未归零", log))
    print(f"  ★ 日志里「门禁未归零」出现次数: {bad}"
          f"（>0 ⇒ 有账号走了全链 ⇒ A1 没生效或 strict 混进来了）")
    print(f"  ★ 日志里「跳过门禁链（A1）」出现次数: "
          f"{len(re.findall(r'跳过门禁链（A1）', log))}")

    # ── A2 接线证据 ───────────────────────────────────────────────────
    # 🔴 判据必须**数实参 / 数日志**，不能读常量 —— 读 `MAIL_POLL_INTERVAL == 0.5`
    #    是**无效**判据：「改了常量没改调用点」这个变异会照样绿。
    print("\n=== A2 接线证据（轮询间隔真的降了吗）===")

    # 证据 A（主）：`mail_wait` 分位数 —— 它是"从开始等"到"第一次命中"的时长，
    #   轮询越密该值越小，且**完全不依赖日志文案**。同口径实测：
    #     旧 interval=2.0 ⇒ P50 2.59s ／ 新 0.5 ⇒ P50 1.04s（两批独立复现 1.04）。
    #   ⚠️ 这里的 30s 是**保守的"明显没超时"阈值**，刻意**不**等于 `MAIL_TIMEOUT`：
    #     上面那组对照数字（2.59/3.22/14.68）就是按 <30 算的，改口径会让对照失效。
    #     要动这个数，必须同时重算对照 —— 否则就是拿两个口径的数字互相比。
    NORMAL_MAIL_WAIT = 30.0
    mw = [float((r.get("timings") or {})["mail_wait"]) for r in keyed
          if isinstance((r.get("timings") or {}).get("mail_wait"), (int, float))
          and float(r["timings"]["mail_wait"]) < NORMAL_MAIL_WAIT]
    if mw:
        print(f"  证据A：keyed 且正常收信（mail_wait<{NORMAL_MAIL_WAIT:.0f}s）n={len(mw)}")
        print(f"    mail_wait  min={min(mw):.2f}  P25={_q(mw, .25):.2f}  "
              f"P50={_q(mw, .50):.2f}  P75={_q(mw, .75):.2f}  "
              f"P90={_q(mw, .90):.2f}  max={max(mw):.2f}")
        print(f"    ★ 同口径对照（旧 interval=2.0，cf100）：P50 2.59 / P75 3.22 / P90 14.68")
        print(f"    ⇒ P50 差 {2.59 - _q(mw, .50):+.2f}s（间隔差 1.5s ⇒ 方向与量级都对得上）")

    # 证据 B（辅）：超时文案里的「轮询 N 次」。两个坑都实测踩到 ——
    #   ① 同一条文案在日志里出现**两次**（跑批中途的 `[N/100] status=failed …`
    #      与结尾汇总的 `      ↳ …`）⇒ 只取汇总行，否则同一账号被算两遍
    #      （实测 n=1 被算成 n=2，中位数直接失真）；
    #   ② `polls` 取自 mail 客户端的 `stats`，**跨两轮累计**（`_wait_with_retry`
    #      里 `mail_wait` 同样累加）⇒ 分母必须用**总等待**。用文案里的单轮 60s
    #      会把周期算小一半（实测 0.38s vs 真值 0.77s）。
    SUM = re.compile(r"^  - (\S+@\S+)\s+\S+\s+\{[^\n]*\n\s+↳ 未在 (\d+)s 内收到确认邮件"
                     r"（邮箱接口轮询 (\d+) 次，5xx (\d+) 次）", re.M)
    hits = SUM.findall(log)
    if hits:
        per = []
        for email, secs, polls, five in hits:
            tot = float((cur.get(email, {}).get("timings") or {}).get("mail_wait") or 0)
            cyc = tot / max(int(polls), 1) if tot else 0
            per.append(cyc)
            print(f"  证据B：{email} 单轮 {secs}s × 重发 ⇒ 总等待 {tot:.2f}s / "
                  f"轮询 {polls} 次（5xx {five} 次）⇒ 实测周期 {cyc:.2f}s")
        if any(per):
            print(f"    ★ 实测周期中位 {st.median(per):.2f}s"
                  f"（旧同口径 120s/53 次 ≈ 2.26s；源码预测 0.5s 间隔 ≈ 158 次/120s）")
        print(f"    ★ 超时账号的 Worker 5xx 合计: {sum(int(f) for *_, f in hits)}"
              f"（>0 ⇒ 放大请求量打出了错误）")
    else:
        print("  证据B：本批无超时账号 ⇒ 拿不到轮询计数（A2 只由证据A 支撑）")
    resend = len(re.findall("批内重发第", log))
    print(f"  批内重发触发次数: {resend}  ⇒ 丢包率 {resend / max(n, 1) * 100:.0f}%"
          f"（铁律：报速率必须同时报丢包率）")

    # ── 单号端到端速率（注册 → 取到 key）────────────────────────────
    # 🔴 这是"单号平均速率"的**唯一正确口径**：用 `run_one`/`resume` 在 `finally`
    #    里**实测**的 `timings["total"]`（进函数 → 出函数）。
    #    绝不能用 `login + create_key` 推导 —— 建邮箱抛异常时 `login` 根本不写，
    #    推导值在那些记录上缺失 ⇒ 分母变小 ⇒ 速率虚高，且不报错。
    print("\n=== 单号端到端速率（注册 → 取到 key）===")
    def _tot(rows: list[dict]) -> list[float]:
        return [float(r["timings"]["total"]) for r in rows
                if isinstance((r.get("timings") or {}).get("total"), (int, float))]

    tot, tot_fail = _tot(keyed), _tot(failed)
    if not tot:
        print("  ⚠ 记录里没有 `timings['total']` —— 说明本批跑的是**旧代码**。")
        print("    速率无法按单号统计。（这正是 total 必须**实测**、不能推导的原因。）")
    else:
        miss = n - len(tot) - len(tot_fail)
        print(f"  覆盖：keyed {len(tot)} / failed {len(tot_fail)} / 缺 total {miss}"
              f"（缺了就会把慢账号排除、速率虚高）")
        # 剔除走了"丢包 → 批内重发"的账号，单独报干净值。
        # 🔴 阈值取 `MAIL_TIMEOUT`（源码常量），**不许拍一个 30s**：
        #    重发路径的**下界就是首次超时阈值** —— 没等到 `MAIL_TIMEOUT` 就不会重发。
        #    用更小的数会把"正常但偏慢"的账号一起剔掉（本批实测：<30 剔 14 个，
        #    而真正走重发的只有 11 个 ⇒ 干净均值被**人为压低**到 7.29s，
        #    正确值是 8.48s）。这正是"分母本身是错的"那一类缺陷。
        clean = [x for x in tot if x < MAIL_TIMEOUT]
        slow = [x for x in tot if x >= MAIL_TIMEOUT]
        print(f"  ★ 单号平均耗时（keyed 全部，n={len(tot)}）："
              f"均值 **{st.mean(tot):.2f}s**  中位 {st.median(tot):.2f}s  "
              f"P90 {_q(tot, .90):.2f}s  最快 {min(tot):.2f}  最慢 {max(tot):.2f}")
        if clean and len(clean) != len(tot):
            print(f"  ★ 单号平均耗时（剔除丢包重发路径，n={len(clean)}）："
                  f"均值 **{st.mean(clean):.2f}s**  中位 {st.median(clean):.2f}s  "
                  f"P90 {_q(clean, .90):.2f}s")
        # 🔴 交叉验证：`total ≥ MAIL_TIMEOUT` 的账号数 必须 == 日志重发次数。
        #    两边是**独立来源**（一边是实测耗时，一边是日志文案），对得上才说明
        #    阈值没拍错、日志解析也没坏。对不上就**别信上面的干净值**。
        #    （实测把阈值改回 30 会立刻报不一致：14 ≠ 11。）
        if len(slow) != resend:
            print(f"  ⚠ 交叉验证**不一致**：total≥{MAIL_TIMEOUT:.0f}s 的账号 {len(slow)} 个 "
                  f"≠ 日志重发次数 {resend} ⇒ 阈值或日志解析可疑，上面的干净值不可信")
        else:
            print(f"  ✓ 交叉验证：total≥{MAIL_TIMEOUT:.0f}s 的账号 {len(slow)} 个 "
                  f"== 日志重发次数 {resend}（实测耗时与日志文案互证）")
        # 吞吐：两种口径都给，且标明是"折算"还是"实测"
        print(f"  ★ 折算吞吐（并发 {a.concurrency} ÷ 单号均值）="
              f"**{a.concurrency / st.mean(tot) * 60:.1f} 账号/分钟**")
        if a.wall > 0:
            print(f"  ★ 实测吞吐（{n} 账号 ÷ 墙钟 {a.wall:.0f}s）="
                  f"**{n / a.wall * 60:.1f} 账号/分钟**")
        else:
            ts = [r["created_at"] for r in new if r.get("created_at")]
            if len(ts) > 1:
                span = max(ts) - min(ts)
                print(f"  ⚠ 未给 --wall；用 created_at 跨度 **估算** 墙钟 ≈ {span:.0f}s"
                      f" ⇒ 吞吐 ≈ {n / span * 60:.1f} 账号/分钟"
                      f"（偏乐观：末位账号的耗时没算进去）")

    # ── 阶段耗时 ──────────────────────────────────────────────────────
    stats_block(new, "全部新增")
    stats_block(keyed, "keyed")
    stats_block(failed, "failed")

    print("\n" + "=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
