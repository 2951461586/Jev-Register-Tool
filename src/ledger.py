"""JSONL 台账：追加写、按唯一键并集合并、幂等、可补录。

四条硬规矩（都踩过坑）：

1. **并集**合并，不"比大小/比字段数" —— 启发式总有相等或边界的死角。
2. **能读自己的输出** —— 导出文件很容易变成唯一副本，重跑不能缩水。
3. **补录按事件真实发生时间入账**，不是补录那一刻。
4. 🔴 **状态等级表的词汇必须与写入方实际写的词汇一致。**
   否则等级判断静默失效 —— 详见 `RANK` 上方的说明。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.Lock()

#: 记录状态优先级：只用于"升级 / 降级"判断，不用来决定"是否写入"。
#:
#: 🔴 **本表的键必须覆盖 `src/pipeline.py` 实际写入的每一个 status 字面量。**
#:
#: 历史教训（2026-09-20 审计发现）：本表曾照搬兄弟项目的口径
#: （`success` / `skipped`），而本项目写的是 `keyed` / `registered` / `approved` …。
#: 两边对不上 ⇒ 所有状态并列 0 分 ⇒ `keyed`（已拿到 api_key）与 `failed` 同级
#: ⇒ 走"同级并集、新值胜出" ⇒ **一次重跑失败就把 api_key 覆盖成空串**。
#: 而 `verify_keys.py` 是按 `api_key` 有没有值来筛验收清单的 ⇒
#: 这些账号会**从验收结果里静默消失**（不报错，只是少几行）。
#:
#: 当时的真实台账侥幸无损，纯粹因为那 5 个重复 key 的 failed 行**恰好都排在
#: keyed 行之前**（全部同分 ⇒ `load()` 退化成"末行胜出"）。
#:
#: 现在由 `tools/selftest.py::test_status_vocabulary` 用 AST 扫源码钉住：
#: 新增任何 status 字面量而没登记进本表，自测立刻失败。
RANK: dict[str, int] = {
    # ── pipeline 实际写入的词汇（唯一真源：src/pipeline.py） ──
    "keyed": 5,         # 终态成功：拿到 api_key
    "partial": 4,       # 注册成功但没拿到 key（onboarding / 建 key 失败）
    "registered": 4,    # 会话已建立（过渡态，正常情况下会被 keyed/partial 覆盖）
    "approved": 3,      # 已获批（解锁注册段）
    "confirmed": 2,     # 确认邮件已到
    "applied": 1,       # 申请已投递
    "code_sent": 1,     # claim 只发了码，还没提交
    "failed": 0,
    "": 0,
    # ── 兼容兄弟项目的旧词汇 ──
    # 本项目不再写这两个，但历史 / 外部文件里可能出现。
    # 留着是为了避免"未知词汇静默落到 0 分"（= 和 failed 同级）这个坑复发。
    "success": 5,
    "skipped": 0,
}

#: 一旦赚到就**不允许被空值覆盖**的字段。
#:
#: 为什么需要：`AccountRecord.to_dict()` **无条件输出**这些键，失败时是空串。
#: 并集合并的"新值胜出"会把已经拿到的凭据清掉 —— 只靠修 `RANK` 挡不住
#: "同级覆盖"（例如 keyed 之后又来一条 keyed 的空壳）。
EARNED_FIELDS: tuple[str, ...] = ("api_key", "api_key_id", "email")

#: 累积型字段：合并时做**并集**，不用后来者整体替换。
DICT_FIELDS: tuple[str, ...] = ("stages", "timings", "waitlist", "user")


def rank(rec: dict[str, Any]) -> int:
    return RANK.get(rec.get("status") or "", 0)


def merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """两条同键记录的合并规则。

    - 新记录等级更高 → 新记录胜出（但仍要保住旧记录里已赚到的字段）
    - 同级 → 并集，新值胜出，旧字段一个不丢
    - 新记录等级更低 → **整体保留旧记录**，只把这次的失败记进 `last_error`

    最后一条是关键：不这么做的话，"重跑一次失败"会把已经成功的记录打回原形，
    连凭据一起清掉。
    """
    r_old, r_new = rank(old), rank(new)

    if r_new < r_old:
        out = dict(old)
        err = str(new.get("error") or "").strip()
        if err and err != str(old.get("error") or "").strip():
            out["last_error"] = err
        return out

    out = dict(new) if r_new > r_old else {**old, **new}
    for k in DICT_FIELDS:
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            out[k] = {**a, **b}
    for k in EARNED_FIELDS:
        if not out.get(k) and old.get(k):
            out[k] = old[k]
    return out


class Ledger:
    """JSONL 台账。append 是线程安全的（多 producer 并发实测 100 行零丢失）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._extra_sources: list[Path] = []

    # ── 写 ────────────────────────────────────────────────────────────
    def append(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def upsert_many(self, records: Iterable[dict[str, Any]]) -> dict[str, int]:
        """把一批记录并入台账（按 `key` 字段去重合并）。

        返回 {"added": n, "updated": n, "kept": n} —— 增量写回必须能自证"真的写进去了"。
        """
        incoming = [r for r in records if r.get("key")]
        current = self.load()
        by_key: dict[str, dict[str, Any]] = {r["key"]: r for r in current}

        added = updated = kept = 0
        for rec in incoming:
            k = rec["key"]
            if k not in by_key:
                by_key[k] = rec
                added += 1
            else:
                merged = merge(by_key[k], rec)
                if merged != by_key[k]:
                    by_key[k] = merged
                    updated += 1
                else:
                    kept += 1

        order = [r["key"] for r in current]
        for r in incoming:
            if r["key"] not in order:
                order.append(r["key"])

        out = [by_key[k] for k in order if k in by_key]
        self._rewrite(out)
        return {"added": added, "updated": updated, "kept": kept}

    def _rewrite(self, records: list[dict[str, Any]]) -> None:
        with _LOCK:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(self.path)

    # ── 读 ───────────────────────────────────────────────────────────
    def add_source(self, path: Path) -> None:
        """把"自己产出的导出文件"也列为输入来源 —— 防止原始来源被删后重跑缩水。"""
        p = Path(path)
        if p != self.path:
            self._extra_sources.append(p)

    def load(self) -> list[dict[str, Any]]:
        """读台账。同键后写覆盖前写（与 append 语义一致），并按等级升级 / 降级保护。"""
        by_key: dict[str, dict[str, Any]] = {}
        for path in [*self._extra_sources, self.path]:
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                k = rec.get("key")
                if not k:
                    continue
                by_key[k] = merge(by_key[k], rec) if k in by_key else rec
        return list(by_key.values())

    def keys(self) -> set[str]:
        return {r["key"] for r in self.load() if r.get("key")}
