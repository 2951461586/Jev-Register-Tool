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
from typing import Any, Iterable, Iterator

#: **可重入**锁。两点原因：
#:
#: 1. `upsert_many` 的"读-改-写"必须**整体原子** —— 它的 `_rewrite` 是整文件替换，
#:    如果不持锁贯穿全程，并发 `append` 刚写进去的行会被整文件替换覆盖掉（静默丢数据）。
#:    而 `_rewrite` 自己也取锁 ⇒ 必须是 RLock，否则自死锁。
#: 2. 并发跑批时"边跑边验收"是常规操作（跑批在 append，验收在读+回填）。
#:    同样的原因，`load()` 也要持锁 —— 否则可能读到写了一半的行。
_LOCK = threading.RLock()

#: 记录状态优先级：只用于"升级 / 降级"判断，不用来决定"是否写入"。
#:
#: 🔴 **本表的键必须覆盖 `src/stages.py` 实际写入的每一个 status 字面量。**
#:
#: 历史教训（2026-09-20 审计发现）：本表曾照搬兄弟项目的口径
#: （`success` / `skipped`），而本项目写的是 `keyed` / `registered` / `approved` …。
#: 两边对不上 ⇒ 所有状态并列 0 分 ⇒ `keyed`（已拿到 api_key）与 `failed` 同级
#: ⇒ 走"同级并集、新值胜出" ⇒ **一次重跑失败就把 api_key 覆盖成空串**。
#: 而 `verify_keys.py` 是按 `api_key` 有没有值来筛验收清单的 ⇒
#: 这些账号会**从验收结果里静默消失**（不报错，只是少几行）。
#:
#: 现在由 `tools/tests/test_ledger.py::test_status_vocabulary` 用 AST 扫源码钉住：
#: 新增任何 status 字面量而没登记进本表，自测立刻失败。
RANK: dict[str, int] = {
    # ── 当前链路实际写入的词汇（唯一真源：src/stages.py） ──
    "keyed": 5,         # 终态成功：拿到 api_key
    "partial": 4,       # 会话已建立但没拿到 key（onboarding / 建 key 失败）
    "registered": 4,    # 会话已建立（过渡态，正常情况下会被 keyed/partial 覆盖）
    "failed": 0,
    "": 0,
    # ── 历史词汇：邀请制时期的阶段状态，**已不再写入**，但必须留着 ──
    #
    # 2026-09-21 邀请制取消，`apply → confirm → approved → login` 四段缩成
    # `signup+login` 一段，于是 `applied` / `confirmed` / `approved` / `code_sent`
    # 四个字面量**不再由代码写入**。
    #
    # 🔴 **不要因为"代码里搜不到"就删掉它们。** 本表的键是给**历史台账**看的：
    #    `exports/ledger.jsonl` 里有数百行 `approved` / `confirmed` 记录，
    #    删掉这些键会让它们 `rank()` 落 0 分（= 与 failed 同级）⇒
    #    任何一次重跑都会把历史记录里的凭据按"同级覆盖"逻辑清掉。
    #    这正是上面那段教训的**同一个坑**，只是触发源从"词汇写错"变成"词汇被删"。
    "approved": 3,      # [历史] 已获批（当时用来解锁注册段）
    "confirmed": 2,     # [历史] waitlist 确认邮件已到
    "applied": 1,       # [历史] 申请已投递
    "code_sent": 1,     # [历史] claim 只发了码、还没提交
    # ── 兼容兄弟项目的旧词汇 ──
    # 本项目不再写这两个，但外部文件里可能出现。
    # 留着是为了避免"未知词汇静默落到 0 分"这个坑复发。
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
#:
#: ⚠️ `waitlist` 是 2026-09-21 之前的字段名（现名 `signup`，见 `stages.AccountRecord`）。
#: 两者都留着：`signup` 是当前写入的，`waitlist` 兜住**历史台账**里的同名键 ——
#: 去掉它，历史记录的 `waitlist` 就会在"升级/同级"合并时被丢掉（数据静默缩水）。
DICT_FIELDS: tuple[str, ...] = ("stages", "timings", "signup", "waitlist", "user")


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

    # ── 写 ────────────────────────────────────────────────────────────
    def append(self, rec: dict[str, Any]) -> None:
        # 写入边界再削一次首尾空白（`AccountRecord.__post_init__` 已经削过，
        # 但这里也常被直接 append 裸 dict —— 例如补录、修正脚本）。
        # 不做的话 `x@y.com\r` 会与 `x@y.com` 并列为两个键，
        # 台账"按邮箱去重"静默失效，交付数字虚高（2026-09-20 实测踩过）。
        rec = dict(rec)
        for f in ("key", "email"):
            if isinstance(rec.get(f), str):
                rec[f] = rec[f].strip()
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            # `newline=""` —— JSONL 只写 LF。Windows 默认会把 `\n` 翻成 `\r\n`，
            # 于是每行尾部带 CR：Linux/WSL 下 `while read` 读出来的字段带 `\r`，
            # 严格 JSONL 解析器也会报错。这与"邮箱清单被 CR 污染"同一根因。
            with self.path.open("a", encoding="utf-8", newline="") as fh:
                fh.write(line + "\n")

    def upsert_many(self, records: Iterable[dict[str, Any]]) -> dict[str, int]:
        """把一批记录并入台账（按 `key` 字段去重合并）。

        返回 {"added": n, "updated": n, "kept": n} —— 增量写回必须能自证"真的写进去了"。

        🔴 整个"读-改-写"在 `_LOCK` 内完成（不是只锁最后那次 `_rewrite`）：
        `_rewrite` 是**整文件替换**，若在它之前有并发 `append`，
        那些行会被这次替换**静默吞掉**。
        """
        with _LOCK:
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
            # 同 `append`：只写 LF（见那里的说明）。
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                for r in records:
                    # 同 `append`：整文件替换这条通路也必须削空白，
                    # 否则 `upsert_many`（verify_keys 用它写 result/success.jsonl）
                    # 会把 `x@y.com\r` 原样带进交付物。
                    r = dict(r)
                    for f in ("key", "email"):
                        if isinstance(r.get(f), str):
                            r[f] = r[f].strip()
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(self.path)

    # ── 读 ───────────────────────────────────────────────────────────
    #
    # ⚠️ 这里曾有一个 `add_source()`：把"自己产出的导出文件"也列为输入来源，
    # 声称"防止原始来源被删后重跑缩水"。**它零调用点** —— 机制是通的，
    # 但没有任何地方调用它 ⇒ 这条保护**实际从未生效**。
    # 属于"以为有护栏其实没有"，2026-09-20 二轮审计后连同 `_extra_sources`
    # 一起删除。真要这个能力，得在 `verify_keys.py` 里真的调一次，而不是留个悬空方法。
    def load(self) -> list[dict[str, Any]]:
        """读台账。同键后写覆盖前写（与 append 语义一致），并按等级升级 / 降级保护。

        持锁读 —— 否则可能与 `append` 的半行写入撞上（读到截断的 JSON）。
        """
        with _LOCK:
            by_key: dict[str, dict[str, Any]] = {}
            for rec in self._iter_raw():
                k = rec.get("key")
                if not k:
                    continue
                by_key[k] = merge(by_key[k], rec) if k in by_key else rec
            return list(by_key.values())

    def raw_rows(self) -> list[dict[str, Any]]:
        """**不做合并**，按文件顺序返回每一行。

        为什么需要：`load()` 是按 `key`（邮箱）去重的合并视图，**同一账号的第二把
        api_key 会被吃掉**。那把 key 在服务端仍然有效 —— 对"交付凭据"这类用途，
        静默少一行和 `verify_keys` 少一行是同一类错误。
        所以需要合并视图（拿权威元数据）**加**原始行（拿全部 key）两个视角。
        """
        with _LOCK:
            return list(self._iter_raw())

    def _iter_raw(self) -> Iterator[dict[str, Any]]:
        """逐行读台账文件，解析失败的行**静默跳过**（不炸整次读）。

        `load()` 与 `raw_rows()` 共用这一份读循环 —— 以前是两份完全相同的复制，
        合并规则改一处忘另一处就会让两个视图不一致。
        """
        if not self.path.is_file():
            return
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue
