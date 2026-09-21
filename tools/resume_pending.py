#!/usr/bin/env python
"""把台账里**还没拿到 api_key** 的账号全部补跑一遍（可反复跑，幂等）。

    python tools/resume_pending.py                # 默认每批 5 个、串行
    python tools/resume_pending.py --batch 10 --concurrency 3 --rounds 2

为什么单独成一个入口（而不是再写一段一次性 shell）：

1. 🔴 **进度判据必须用合并视图 `Ledger.load()`，不能用"末行胜出"。**
   2026-09-20 实测踩过：上一版守卫按"末行胜出"数 `api_key`，而
   `merge()` 的降级保护会把**重跑失败**的记录压掉、保留旧的 keyed 记录；
   可末行视图里那条 failed 是新的 ⇒ 计数反而**下降**（实测 `126 -> 104`）。
   于是守卫判成"连续两批没有新增"，**假阴性提前收工**，最后 2 批没跑。
   判据跑在错误的层上，比没有判据更危险 —— 它会让你以为已经跑完了。

2. 每批之间做一次真实的对账（`keyed` 增量 + 剩余待补），增量归零才停，
   避免对着站点无意义地刷。

3. 默认**串行**（`--concurrency 1`）：共享 Worker 的 D1 只有 100 行窗口，
   一次并发发太多验证码会把窗口打爆，早发的码被挤掉 ⇒ 报
   `未收到验证码邮件`。实测 100 个并发能丢 17 个，串行则几乎不丢。
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import ROOT  # noqa: E402,F401

from src import config  # noqa: E402
from src.ledger import Ledger  # noqa: E402


def keyed(led: Ledger) -> set[str]:
    """**合并视图**里有 api_key 的邮箱。"""
    return {r["email"] for r in led.load() if r.get("api_key")}


def pending(led: Ledger) -> list[str]:
    """**合并视图**里没有 api_key 的邮箱（= 待补跑候选）。

    注意这里刻意包含**所有**状态：`applied` / `confirmed` / `partial` /
    `failed` / `approved` 都要算候选。只挑某一个状态词会让账号静默漏掉。

    🔴 但要**排除 `worker-crash#<n>` 这类占位键**（2026-09-20 二轮审计）：
    并发 worker 崩溃时邮箱是未知的（它是在崩溃的 worker **内部**才由
    `create_mailbox()` 建出来的），只能用批次序号占位。拿它去
    `--email worker-crash#0` 只会白刷一次站点。

    判据刻意用"**像不像邮箱**"（`@`）而不是"是不是 failed"——
    真正的失败账号**必须**留在候选里，那正是补跑的意义所在。
    """
    out: list[str] = []
    for r in led.load():
        if r.get("api_key"):
            continue
        email = str(r.get("email") or "")
        if "@" not in email:
            continue                      # 占位键（worker-crash#N），不是可登录的地址
        out.append(email)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5, help="每批账号数（默认 5）")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="批内并发（默认 1=串行；共享 Worker 窗口小，别开大）")
    ap.add_argument("--rounds", type=int, default=3, help="最多跑几轮")
    ap.add_argument("--timeout", type=float, default=900.0, help="每批超时秒数")
    ap.add_argument("--log", default="", help="日志路径（默认 exports/resume_pending.log）")
    args = ap.parse_args()

    led = Ledger(config.LEDGER_PATH)
    # 🔴 默认落盘路径必须从 `config.EXPORT_DIR` 派生，**不要写相对路径**
    #    （`Path("exports/…")` 是 CWD 依赖：从别的目录起进程会把日志写到别处，
    #    而 `--log` 的默认值文档里写的是 `exports/resume_pending.log`，
    #    两者会静默不一致）。2026-09-20 二轮审计 §10-9。
    log_path = Path(args.log) if args.log else config.EXPORT_DIR / "resume_pending.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # newline="" —— Windows 上必须显式关掉 `\n` -> `\r\n` 的翻译。
    # 不关的话，任何从这份日志/清单再读邮箱的地方都会带上尾部 `\r`，
    # 被当成另一个地址写进台账（2026-09-20 实测污染了 39 条记录）。
    log = io.open(log_path, "w", encoding="utf-8", newline="")

    def w(line: str) -> None:
        log.write(line + "\n")
        log.flush()

    todo = pending(led)
    w(f"=== resume_pending {time.strftime('%F %T')} ===")
    w(f"起点：keyed={len(keyed(led))}  待补={len(todo)}")
    if not todo:
        w("没有待补账号。")
        log.close()
        return 0

    for rnd in range(1, args.rounds + 1):
        todo = pending(led)
        if not todo:
            w("\n全部账号都已拿到 key。")
            break
        before = len(keyed(led))
        w(f"\n########## 第 {rnd} 轮  待补 {len(todo)}  keyed基线={before}  "
          f"{time.strftime('%T')} ##########")

        for i in range(0, len(todo), args.batch):
            chunk = todo[i:i + args.batch]
            cmd = [sys.executable, str(ROOT / "tools" / "run_e2e.py"),
                   "--mode", "resume", "--concurrency", str(args.concurrency)]
            for e in chunk:
                cmd += ["--email", e]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=args.timeout)
                out = p.stdout
            except subprocess.TimeoutExpired as exc:
                out = f"[超时 {args.timeout}s] {(exc.stdout or '')}"
            w(f"---------- 批 {i // args.batch + 1}: {len(chunk)} 个  "
              f"{time.strftime('%T')} ----------")
            w(out[-4000:])

        after = len(keyed(led))
        rest = pending(led)
        w(f"  [第 {rnd} 轮] keyed {before} -> {after}  剩余待补 {len(rest)}  "
          f"{time.strftime('%T')}")
        if after <= before:
            w("!!! 本轮没有新增 keyed，停止（避免无意义刷站点） !!!")
            w("    注意：这不代表剩下的账号永远拿不到 key。")
            w("    若原因含 `Access restricted` 那是**尚未获批**（批量定时审批）；")
            w("    若含 `未收到验证码` 那是瞬时问题，换个时间重跑即可。")
            break

    rest = pending(led)
    w(f"\n=== 结束 {time.strftime('%F %T')}  keyed={len(keyed(led))} ===")
    for e in rest:
        rec = next((r for r in led.load() if r["email"] == e), {})
        w(f"  仍无 key: {e}  status={rec.get('status')}  "
          f"last_error={rec.get('last_error')!r}")
    log.close()
    print(f"完成。keyed={len(keyed(led))}  待补={len(rest)}  日志 {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
