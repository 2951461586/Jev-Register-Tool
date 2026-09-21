"""台账自测：并集合并语义 / 状态词汇覆盖（AST）/ 身份字段空白归一化。

后两条都是**静默数据损坏**类：不报错，只是台账里少几行或多几条脏键。
"""

from __future__ import annotations

import ast

from .support import ROOT, RANK, _tmp_ledger, check, pl

def test_ledger_union() -> None:
    """台账并集合并 —— 🔴 **用生产真实词汇（keyed/failed），不用 success/failed**。

    用 `success` 是 2026-09-20 审计发现的那个洞：那套词汇是兄弟项目的口径，
    正好是 `RANK` 认的，所以断言必过，而真实语义（keyed vs failed）全废。
    """
    print("\n[台账并集合并（真实词汇）]")
    led = _tmp_ledger()
    led.append({"key": "a@x.com", "status": "keyed", "email": "a@x.com",
                "api_key": "apikey_K1"})
    check("基线 1 条", len(led.load()) == 1)

    # 增量写回：**只带增量字段**（不交超集），必须仍然真的写进去
    led.upsert_many([{"key": "a@x.com", "status": "keyed", "token": "T1"}])
    rec = {r["key"]: r for r in led.load()}["a@x.com"]
    check("旧字段没丢（api_key 还在）", rec.get("api_key") == "apikey_K1", str(rec))
    check("新字段真的写进去了（token）", rec.get("token") == "T1", str(rec))

    # 同级、字段数相等 —— 启发式算法会在这里失手，并集不会
    led.upsert_many([{"key": "a@x.com", "status": "keyed", "extra": "E"}])
    rec = {r["key"]: r for r in led.load()}["a@x.com"]
    check("字段数相等时新字段仍写入", rec.get("extra") == "E", str(rec))
    check("此前所有字段仍在",
          all(k in rec for k in ("api_key", "token", "extra")), str(rec))

    # ★ 核心回归：keyed 之后来一条 failed，**不能把成功记录打回原形**
    led.upsert_many([{"key": "a@x.com", "status": "failed", "api_key": "",
                      "api_key_id": "", "error": "认证回调 HTTP 401: Code expired"}])
    rec = {r["key"]: r for r in led.load()}["a@x.com"]
    check("★ 重跑失败后 status 未被降级", rec.get("status") == "keyed", str(rec.get("status")))
    check("★ 重跑失败后 api_key 仍在", rec.get("api_key") == "apikey_K1",
          repr(rec.get("api_key")))
    check("失败原因记进了 last_error（信息没丢）",
          "Code expired" in (rec.get("last_error") or ""), repr(rec.get("last_error")))

    # 升级方向仍要生效
    led.upsert_many([{"key": "c@x.com", "status": "failed", "error": "x"}])
    led.upsert_many([{"key": "c@x.com", "status": "keyed", "api_key": "apikey_K3"}])
    rec = {r["key"]: r for r in led.load()}["c@x.com"]
    check("failed -> keyed 升级仍生效",
          rec.get("status") == "keyed" and rec.get("api_key") == "apikey_K3", str(rec))

    # EARNED_FIELDS：同级空壳不许清掉已赚到的凭据
    led.upsert_many([{"key": "d@x.com", "status": "keyed", "api_key": "apikey_K4"}])
    led.upsert_many([{"key": "d@x.com", "status": "keyed", "api_key": "",
                      "api_key_id": ""}])
    rec = {r["key"]: r for r in led.load()}["d@x.com"]
    check("同级空壳不清掉 api_key（EARNED_FIELDS 保护）",
          rec.get("api_key") == "apikey_K4", repr(rec.get("api_key")))

    # 不缩水 / 幂等
    led.upsert_many([{"key": "b@x.com", "status": "keyed"}])
    check("新增不丢旧数据", len(led.load()) == 4, str(len(led.load())))
    led.upsert_many([{"key": "b@x.com", "status": "keyed"}])
    check("重复写入幂等", len(led.load()) == 4)

    # ★ 合并视图按邮箱去重 ⇒ 同账号的第二把 key 会挤掉第一把。
    # `verify_keys.py` 的候选集必须是"合并视图 ∪ 原始行"，否则会静默少交付一行。
    led.append({"key": "a@x.com", "status": "keyed", "email": "a@x.com",
                "api_key": "apikey_K1_SECOND"})
    merged_keys = {r["api_key"] for r in led.load() if r.get("api_key")}
    raw_keys = {r["api_key"] for r in led.raw_rows() if r.get("api_key")}
    # 末行胜出 ⇒ 被吃掉的是**第一把**（不是第二把）。这个方向别写反：
    # 真实台账里被吃掉的那把同样是先建的那把。
    check("★ [负对照] 合并视图确实吃掉了第一把 key",
          "apikey_K1" not in merged_keys, str(sorted(merged_keys)))
    check("★ 原始行里两把 key 都在",
          {"apikey_K1", "apikey_K1_SECOND"} <= raw_keys, str(sorted(raw_keys)))
    check("原始行不合并 ⇒ 行数多于合并视图",
          len(led.raw_rows()) > len(led.load()),
          f"{len(led.raw_rows())} vs {len(led.load())}")


def _status_literals(source: str) -> set[str]:
    """从源码里抽出"状态字面量"：

    ① 赋值给 `X.status` / `status` 的字符串（含 dataclass 默认值）
    ② 与 `X.status` 比较的字符串

    抽得出来才谈得上"覆盖检查" —— 所以这个函数自己也要有负对照。
    """
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            hit = any(
                (isinstance(t, ast.Attribute) and t.attr == "status")
                or (isinstance(t, ast.Name) and t.id == "status")
                for t in targets
            )
            if hit and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out.add(node.value.value)
        elif isinstance(node, ast.Compare):
            left = node.left
            if isinstance(left, ast.Attribute) and left.attr == "status":
                for c in node.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        out.add(c.value)
    return out


def test_status_vocabulary() -> None:
    """🔴 台账等级表必须覆盖管线实际写的**每一个** status。

    这是 2026-09-20 那个 P0 的结构性修复：以前靠"人记得同步改两个文件"，
    现在靠 AST 扫源码。新增状态而不登记进 `ledger.RANK` ⇒ 本测试立刻失败。
    """
    print("\n[状态词汇覆盖（AST）]")
    found: set[str] = set()
    # 🔴 扫描面必须**递归**到 `tools/tests/`（2026-09-20 二轮审计 ⑩）：
    # 自测拆进 `tools/tests/` 之后，`(ROOT / "tools").glob("*.py")` 这个 glob
    # 就**匹配不到子目录**了 ⇒ 测试文件里写的 status 字面量会逃出覆盖检查。
    # 这类"覆盖面悄悄缩小"不会让任何一条断言变红，只是让护栏对**将来**
    # 新增的状态失去反应 —— 典型的假绿。改成 `rglob` 后扫描文件数
    # 16 → 28（多出 `tests/` 与 `probes/`），实测字面量集合**不变**（8 个），
    # 即只增不减。
    for p in sorted(list((ROOT / "src").glob("*.py"))
                    + list((ROOT / "tools").rglob("*.py"))):
        found |= _status_literals(p.read_text(encoding="utf-8"))

    missing = sorted(found - set(RANK))
    check(f"源码里出现的 status 字面量 {sorted(found)}", bool(found))
    check("★ 全部已在 ledger.RANK 里登记（否则等级判断静默失效）",
          not missing, f"未登记：{missing} —— 请加到 src/ledger.py 的 RANK")

    # 负对照：证明提取器有能力发现"未登记的新状态"
    fake = 'rec.status = "brand_new_state"\nif rec.status == "another_new": pass\n'
    got = _status_literals(fake)
    check("[负对照] 提取器能发现未登记的新状态",
          got == {"brand_new_state", "another_new"}, str(got))
    check("[负对照] 这些新状态确实不在 RANK 里（所以真加了会失败）",
          not (got & set(RANK)), str(got & set(RANK)))

def test_identity_whitespace_normalization() -> None:
    """🔴 身份字段（`key`/`email`）的首尾空白必须在**写入边界**削掉；落盘只写 LF。

    2026-09-20 实测事故（**全程不报错**，属于典型的静默数据损坏）：
    邮箱清单是 CRLF（Windows 上 `io.open(...,'w')` 会把 `\\n` 翻成 `\\r\\n`），
    而 shell 的 `mapfile -t` / `read` **只剥 `\\n` 不剥 `\\r`** ⇒ 每个地址尾部带 CR。
    站点侧会 trim 掉它、照常发码建 key，所以没有任何报错；但台账里多出一类
    `"x@y.com\\r"` 的键，与干净键**互不相同** ⇒ 唯一账号数虚高（实测 127 → 166），
    "按邮箱去重"静默失效，交付数字跟着虚高。

    同源的第二条：JSONL / 凭据清单若按默认文本模式写，Windows 会给**每行**加 CR，
    Linux/WSL 下 `while read` 读出来的 api_key 会变成 `apikey_xxx\\r` ⇒ 直接 401，
    而肉眼完全看不出区别。所以这里把"只写 LF"也钉住。
    """
    print("\n[身份字段空白归一化 / 只写 LF]")

    # 1) AccountRecord 是写入真源，必须自己削
    rec = pl.AccountRecord(key="a@x.com\r\n", email=" a@x.com\r\n")
    check("AccountRecord.key 被 strip", rec.key == "a@x.com", repr(rec.key))
    check("AccountRecord.email 被 strip", rec.email == "a@x.com", repr(rec.email))

    led = _tmp_ledger()
    # 2) 裸 dict 走 append 也要削（补录 / 修正脚本走这条通路）
    led.append({"key": "b@x.com\r", "email": "b@x.com\r",
                "status": "keyed", "api_key": "apikey_B"})
    # 3) ★ 核心回归：CR 变体不能变成**第二个账号**
    led.append({"key": "c@x.com", "email": "c@x.com",
                "status": "keyed", "api_key": "apikey_C1"})
    led.append({"key": "c@x.com\r", "email": "c@x.com\r",
                "status": "keyed", "api_key": "apikey_C2"})
    by = {r["email"]: r for r in led.load()}
    check("append 的脏 key 被削成干净 key", "b@x.com" in by, str(sorted(by)))
    check("★ 同账号的 CR 变体不会分裂成两个账号",
          sorted(by) == ["b@x.com", "c@x.com"], str(sorted(by)))

    # 4) 落盘：既不能有 CRLF 行尾，也不能有 `\\r` 转义值
    #    （注意 json.dumps 会把值里的 CR 转义成两个字符 `\\` + `r`，
    #      所以"值有没有被削干净"靠上面第 2/3 条断言，这里只管行尾与残留）
    raw = led.path.read_bytes()
    check("★ 台账写入只用 LF（无 CRLF 行尾、无 \\r 转义）",
          b"\r" not in raw and b"\\r" not in raw, repr(raw[:160]))

    # 5) upsert_many 走 `_rewrite`（整文件替换）—— 同样要削、同样只写 LF
    led.upsert_many([{"key": "d@x.com\r\n", "email": "d@x.com\r\n",
                      "status": "keyed", "api_key": "apikey_D"}])
    by = {r["email"]: r for r in led.load()}
    check("upsert_many（_rewrite 通路）也削空白", "d@x.com" in by, str(sorted(by)))
    raw = led.path.read_bytes()
    check("★ upsert_many 重写后仍只用 LF",
          b"\r" not in raw and b"\\r" not in raw, repr(raw[:160]))

    # 6) 交付物侧：`verify_keys.py` 必须走 `_write_lf`。
    #    它是凭据清单的唯一写出口，写坏了就是"复制出去的 key 带 CR"。
    #    这里做源码级护栏（与 `test_status_vocabulary` 的 AST 扫源码同一思路），
    #    因为该写盘逻辑在 `main()` 内，无法直接 import 调用。
    vsrc = (ROOT / "tools" / "verify_keys.py").read_text(encoding="utf-8")
    check("★ verify_keys 落盘走 _write_lf（只写 LF）",
          "def _write_lf(" in vsrc and vsrc.count("_write_lf(") >= 3,
          f"_write_lf 出现 {vsrc.count('_write_lf(')} 次")
    check("verify_keys 不再用会翻译换行的 Path.write_text",
          "write_text(" not in vsrc, "还有 write_text 调用")
