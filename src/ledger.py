"""JSONL 台账：追加写、按唯一键并集合并、幂等、可补录。

三条硬规矩（都踩过坑）：
1. **并集**合并，不"比大小/比字段数" —— 启发式总有相等或边界的死角。
2. **能读自己的输出** —— 导出文件很容易变成唯一副本，重跑不能缩水。
3. **补录按事件真实发生时间入账**，不是补录那一刻。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

_LOCK = threading.Lock()

# 记录状态优先级：只用于"升级"判断，不用来决定"是否写入"
RANK = {"success": 2, "partial": 1, "skipped": 0, "failed": 0, "": 0}


def rank(rec: dict[str, Any]) -> int:
    return RANK.get(rec.get("status") or "", 0)


def merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """两条同键记录的合并规则。

    - 新记录等级更高 → 整体替换（failed -> success）
    - 同级 → 并集，新值胜出，旧字段一个不丢
    """
    r_old, r_new = rank(old), rank(new)
    if r_new > r_old:
        return dict(new)
    if r_new == r_old:
        union = {**old, **new}
        return union
    return dict(old)


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

    # ── 读 ────────────────────────────────────────────────────────────
    def add_source(self, path: Path) -> None:
        """把"自己产出的导出文件"也列为输入来源 —— 防止原始来源被删后重跑缩水。"""
        p = Path(path)
        if p != self.path:
            self._extra_sources.append(p)

    def load(self) -> list[dict[str, Any]]:
        """读台账。同键后写覆盖前写（与 append 语义一致），并按等级升级。"""
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


class QuotaLedger:
    """滚动窗口配额账本：只记成功，按 scope 分维度。"""

    def __init__(self, path: Path, window_seconds: float, limit: int):
        self.path = Path(path)
        self.window = window_seconds
        self.limit = limit
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, key: str, scope: str = "global", ts: float | None = None) -> None:
        rec = {"ts": ts if ts is not None else time.time(), "key": key, "scope": scope}
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def used(self, scope: str | None = None, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        cutoff = now - self.window
        n = 0
        if not self.path.is_file():
            return 0
        seen: set[str] = set()
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            if scope is not None and rec.get("scope", "global") != scope:
                continue
            k = rec.get("key", "")
            if k in seen:
                continue
            seen.add(k)
            n += 1
        return n

    def backfill(self, records: Iterable[dict[str, Any]],
                 key_of: Callable[[dict], str | None],
                 ts_of: Callable[[dict], float | None],
                 scope_of: Callable[[dict], str] | None = None) -> int:
        """从历史结果补录。判据必须与业务口径一致，时间戳按记录还原，幂等。"""
        existing: set[str] = set()
        if self.path.is_file():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    existing.add(json.loads(raw).get("key", ""))
                except json.JSONDecodeError:
                    continue
        added = 0
        for r in records:
            k = key_of(r)
            ts = ts_of(r)
            if not k or ts is None or k in existing:
                continue
            self.record(k, scope=(scope_of(r) if scope_of else "global"), ts=ts)
            existing.add(k)
            added += 1
        return added
