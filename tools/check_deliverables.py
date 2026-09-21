"""独立复核 `result/` 下的四份交付物 —— **只读，不写任何文件**。

为什么需要它（项目规则：**别只信自己的汇总/验收脚本**）：
`verify_keys.py` 自己会打印"可用 N / 不可用 0"，但那句话是**同一个进程的自述**。
本工具从**产物文件**出发，独立地把三件事验一遍：

1. **CR 污染**（原始字节层）—— `mapfile` / shell 拼参数会把 `\\r` 带进来，
   而它**不报错**，只是让台账键、key 值悄悄变错。四份交付物必须 CR == 0。
2. **`apikeys.txt` 与 `keys.txt` 的 key 本体集合恒等** —— 两份由**同一次**
   `verify_keys.py` 写出，集合必须相等。
   ⚠️ 必须**先抽 `apikey_...` 再比集合**：按**整行**比会得到"双向独有 N"的**假不一致**
   （`keys.txt` 的行格式是 `<email>----<key>----<id>`）。
3. **与最近一次 `.bak-<时间戳>` 的差集** —— 新增/消失各多少，消失的必须能解释。

⚠️ 关于 key 形态（**踩过**）：实测是 `apikey_<35 hex>_<64 hex>`。
第一版把长度写死成 32 ⇒ 459 条全被判"畸形"、差集凭空出现 918 ——
**写死长度就是假警报的源头**，所以这里用宽松匹配 `apikey_[0-9a-f_]{20,}`。

用法：
    python tools/check_deliverables.py

退出码：0 = 全绿 / 1 = 有不一致或污染（**可当门禁**）。
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = os.path.join(ROOT, "result")

#: 宽松匹配：`apikey_<35 hex>_<64 hex>`。**别写死长度**（见模块 docstring）。
KEY_RE = re.compile(r"apikey_[0-9a-f_]{20,}")

FILES = ("apikeys.txt", "keys.txt", "keys_verified.json", "success.jsonl")


def read(p):
    with io.open(p, encoding="utf-8", newline="") as fh:
        return fh.read()


def keys_of(path):
    """从 `keys.txt` 形态（`<email>----<key>----<id>`）里抽出 key 本体集合。"""
    out, bad = set(), []
    for ln in read(path).split("\n"):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        parts = ln.split("----")
        if len(parts) != 3:
            bad.append(ln[:60])
            continue
        m = KEY_RE.search(parts[1])
        if m:
            out.add(m.group(0))
        else:
            bad.append(ln[:60])
    return out, bad


def main():
    fails = []

    print("=== [1] CR 污染（原始字节层） ===")
    for name in FILES:
        p = os.path.join(R, name)
        if not os.path.exists(p):
            print("  %-20s 缺失" % name)
            fails.append("%s 缺失" % name)
            continue
        b = open(p, "rb").read()
        cr = b.count(b"\r")
        ok = cr == 0
        print("  %-20s bytes=%9d  CR=%3d  CRLF=%3d  %s"
              % (name, len(b), cr, b.count(b"\r\n"), "OK" if ok else "★ 污染"))
        if not ok:
            fails.append("%s 有 %d 个 CR" % (name, cr))

    print()
    print("=== [2] apikeys.txt 与 keys.txt 的 key 本体集合 ===")
    ak_lines = [ln.strip() for ln in read(os.path.join(R, "apikeys.txt")).split("\n")]
    ak = {ln for ln in ak_lines if ln}
    ak_bad = [ln for ln in ak_lines if ln and not KEY_RE.fullmatch(ln)]
    print("  apikeys.txt 非空行 %d / 去重 %d / 非 key 形态 %d"
          % (len(ak_lines) - ak_lines.count(""), len(ak), len(ak_bad)))
    if ak_bad:
        print("     ★ 样例: %s" % [s[:50] for s in ak_bad[:2]])
        fails.append("apikeys.txt 有 %d 行不是 key 形态" % len(ak_bad))

    k_set, k_bad = keys_of(os.path.join(R, "keys.txt"))
    print("  keys.txt 抽出 key %d / 畸形行 %d" % (len(k_set), len(k_bad)))
    if k_bad:
        print("     ★ 样例: %s" % k_bad[:2])
        fails.append("keys.txt 有 %d 个畸形行" % len(k_bad))

    only_ak, only_k = ak - k_set, k_set - ak
    print("  ★ 只在 apikeys.txt: %d   ★ 只在 keys.txt: %d" % (len(only_ak), len(only_k)))
    verdict = not only_ak and not only_k
    print("  判定: %s" % ("恒等 ✓" if verdict else "不一致 ✗"))
    if not verdict:
        fails.append("apikeys/keys 集合不一致（%d / %d）" % (len(only_ak), len(only_k)))

    print()
    print("=== [3] keys_verified.json 与 apikeys.txt 对账 ===")
    j = json.loads(read(os.path.join(R, "keys_verified.json")))
    if isinstance(j, dict):
        j = j.get("results") or j.get("items") or list(j.values())
    ok_n = sum(1 for r in j if str(r.get("ok", r.get("available", ""))).lower()
               in ("true", "1"))
    jkeys = set()
    for r in j:
        m = KEY_RE.search(str(r.get("api_key", r.get("key", ""))))
        if m:
            jkeys.add(m.group(0))
    print("  条目 %d / ok %d / 抽出 key %d / 与 apikeys 差集 %d"
          % (len(j), ok_n, len(jkeys), len(jkeys ^ ak)))
    if len(jkeys) != ok_n:
        fails.append("keys_verified.json 里 ok=%d 但只抽出 %d 个 key" % (ok_n, len(jkeys)))
    if jkeys ^ ak:
        fails.append("keys_verified.json 与 apikeys.txt 的 key 集合不一致")

    print()
    print("=== [4] 与最近一次备份的差集 ===")
    baks = sorted((os.path.getmtime(os.path.join(R, f)), f)
                  for f in os.listdir(R) if f.startswith("keys.txt.bak-"))
    if not baks:
        print("  没有 .bak-* 备份（首次运行正常）")
    else:
        _, last = baks[-1]
        old, _ = keys_of(os.path.join(R, last))
        gone = old - k_set
        print("  %-34s 旧 %d → 新 %d，新增 %d，**消失 %d**"
              % (last, len(old), len(k_set), len(k_set - old), len(gone)))
        if gone:
            print("     ★ 消失的必须能解释（不是本轮新增则属异常）: %s" % sorted(gone)[:3])
            fails.append("相对 %s 消失了 %d 个 key" % (last, len(gone)))

    print()
    print("=" * 50)
    if fails:
        print("★ 复核不通过：%d 项" % len(fails))
        for f in fails:
            print("   - %s" % f)
        return 1
    print("复核通过（四份交付物自洽，CR 污染 0，集合恒等）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
