"""页面 / 邮件文本 → 结构。**纯函数，零第三方依赖。**

这个模块是 2026-09-20 二轮审计 §8.2 从 `typesafe.py` 里拆出来的解析层。

**边界判据不是"这段代码里有没有正则"，而是"它能不能脱离 `requests` 被单独测"**
（后者才是拆分的实际收益）。所以 `TypeSafeClient` 里**刻意保留**了两处就地正则：
`exchange_magic_link()` 里抠 `xhr.send(JSON.stringify(…))` 那一句，以及
`token_from_redirect_url()`。两者都是"一次请求的后续处理"，各自只有一个调用点，
抽出来只会多一层间接；而它们的输入（`page.text` / `redirect_url`）本来就来自
那一次请求。判据一旦放宽成"见到正则就挪"，这个模块就会退化成杂物间。

拆出来的**可验证收益**（不是审美）：

1. `python -c "from src import parsing"` **不需要 `requests`** —— 本模块只 import
   `config` + stdlib（`html` / `json` / `re`）。以前想单测 `actions_from_html`
   必须 `from src import typesafe`，而那个 import 链会拉进 `requests`；
2. 解析器不再挂在 client 的模块命名空间下 —— 以前是 `typesafe._actions_from_html`
   这种"私有但被三个探针 + 自测 import"的别扭状态；
3. `typesafe.py` 只剩 `TypeSafeClient` + `Result` + 异常，读起来就是一条 HTTP 链路。

⚠️ 本模块里的函数是**公开名**（不再带 `_` 前缀）。旧名 `_actions_from_html` 等
已随拆分删除，**没有留兼容别名** —— 同一份实现挂两个名字正是本项目一直在清的那种
"第二份真源"。引用面（`tools/selftest.py` + `tools/probes/*`）已同步改成公开名。

`MAGIC_LINK_RE` / `extract_magic_link` 也在这里，尽管审计报告 §8.2 的枚举里没列它：
它同样是"可独立测的纯文本变换"，留在 `typesafe.py` 会让本模块的边界变成
"一部分解析器"而不是"解析层"。代价是它和 `exchange_magic_link()` 分居两个模块 ——
但两者的共同知识（Stytch 域名）本来就已经收敛到 `config.STYTCH_LOGIN_HOST` 了。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from typing import Any

from . import config

#: 页面里渲染的 Server Action 隐藏域：`name="$ACTION_<n>:<idx>" value="…"`。
#:
#: 🔴 **不要把 `<n>` 的集合写死。** 以前是 `for n in ("2", "3", "4")`，并且要求
#: `:0` 与 `:2` **同时存在**。2026-09-20 站点改版后 `/setup/*` 渲染的是索引 **1**、
#: 且**只有 `:0` 和 `:1`**（另加一个 `$ACTION_KEY`）—— 两个条件都不满足，
#: 于是 `acts` 恒为空 ⇒ 退化到 `FALLBACK_SETUP_ACTIONS` 里陈旧的 action id
#: ⇒ POST 404 ⇒ 最终只看到 `onboarding 失败: HTTP 404`，
#: 跟"索引集合被写死了"毫无字面关联。
ACTION_FIELD_RE = re.compile(r'name="\$ACTION_(\d+):(\d+)"\s+value="([^"]*)"')
ACTION_KEY_RE = re.compile(r'name="\$ACTION_KEY"\s+value="([^"]*)"')

#: 魔法链接落地页 URL（`login.typesafe.ai/v1/magic_links/redirect?...`）。
#:
#: 🔴 用 `config.STYTCH_LOGIN_HOST` 派生，**不要在这里再写一遍域名**。
#: 2026-09-20 二轮审计发现：该常量当时**零引用**，而同一个 URL 在
#: `typesafe.py`（2 处）与两个探针（4 处）里被硬编码 —— 改配置不生效。
#: 现在生产侧全部收敛到常量；探针仍各自硬编码（探针的定位是
#: "一次性、自包含、随时能删"，见 `docs/audit-2026-09-20-round2.md` §5.4）。
#:
#: 用 `re.escape` 而不是手写 `\.`：手写容易漏（少一个反斜杠就会匹配
#: `loginXtypesafeYai`），而且常量一改这里就得跟着改。
MAGIC_LINK_RE = re.compile(
    re.escape(config.STYTCH_LOGIN_HOST) + r"/v1/magic_links/redirect\?[^\s\"<>\)\]]+"
)

#: 一次性 token 的参数名。判"这条链接完不完整"就看它（**必须带 `=`**）。
#: 用 `[?&]` 锚定参数名起点，避免被 `public_token=` 里的子串误命中。
MAGIC_LINK_TOKEN_PARAM_RE = re.compile(r"[?&]token=")


def _looks_complete(url: str) -> bool:
    """这条链接是否**同时**带齐 `public_token=` 与 `token=` 两个参数。

    🔴 为什么需要（2026-09-21 补跑实测，4/100 账号必失败的根因）：
    正文里**同一个 URL 会出现 12 次，其中一部分被截断了 4 个字符** ——
        完整：`…&stytch_token_type=magic_links&token=D8SZNL…`
        残缺：`…&stytch_token_type=magic_links&tokenSZNL…`   ← `=D8` 整个没了
    残缺形态等于**根本没传 `token`**，Stytch 回 `400 invalid_public_token_id`
    （报的是 `public_token` 的格式问题，跟"少了个参数"毫无字面关联），
    外层于是把它读成"链接可能已被使用/过期"，把排查引向"重新发信"，白烧账号。

    ⚠️ `=D8` 是 token 的**字面字符**，不是 quoted-printable 转义 —— 受控实验：
    `&token=D8SZNL…` → **200 + dfp payload**；补成 `&token=SZNL…` → 400；
    把 `=D8` 解成字节 0xD8 → 400。所以**不能靠"还原转义"修**，只能靠"换一条候选"。

    旧实现取**第一个**匹配，恰好取到残缺那条 ⇒ 该账号必失败。
    """
    return "public_token=" in url and bool(MAGIC_LINK_TOKEN_PARAM_RE.search(url))


def extract_magic_links(text: str) -> list[str]:
    """取出正文里**所有**魔法链接候选：去重，**完整的排在前面**。

    刻意返回列表而不是单个 —— 正文里同一 URL 可能有多份、且只有一部分是完整的，
    "取第一个"不可靠。调用方应当**按序逐个试**，直到某一条真的换到会话为止：
    判据是终态（拿到会话），不是"URL 长得对不对"。

    去重按**反转义后**的字符串做（同一 URL 在 HTML 里可能一处 `&`、一处 `&amp;`）。
    """
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in MAGIC_LINK_RE.finditer(text):
        # 🔴 **必须做 HTML 实体反转义**（2026-09-21 接入 Remail 时实测踩到）：
        #    不同后端给的正文形态不同 —— CF Worker 是纯文本，Remail 是 **HTML**，
        #    后者的链接里 `&` 被转义成 `&amp;`。不还原的话，提取出来的查询串是
        #        ?public_token=X&amp;stytch_token_type=magic_links&amp;token=Y
        #    解析方（Stytch）看到的参数名是 `amp;stytch_token_type` / `amp;token`
        #    ⇒ **等于根本没传 token**，交换必然失败，而报错读起来像"链接无效/过期"，
        #    会把排查引向"重新发信"，白烧账号。
        #    `unescape` 对纯文本是无操作 ⇒ 对 CF 后端零影响（已由自测钉住）。
        seen.setdefault(html_mod.unescape(m.group(0)), None)
    cands = list(seen)
    # 稳定分区：完整的一律排在残缺的前面（各自保持正文里的出现顺序）。
    return ([u for u in cands if _looks_complete(u)]
            + [u for u in cands if not _looks_complete(u)])


def extract_magic_link(text: str) -> str:
    """从邮件正文里取出**最优**魔法链接 URL；一条都没有则返回空串。

    放在解析层而不是调用方，是因为这个 URL 的**域名属于 Stytch**，
    与 `typesafe.exchange_magic_link()` 是同一个知识（改一处就该一起改）；
    域名本身又来自 `config.STYTCH_LOGIN_HOST`，所以三处不会各写一份。

    ⚠️ 只返回一条时**不保证可用**（正文里可能压根没有完整的那条）。
    需要"换一条再试"的调用方应当直接用 `extract_magic_links()`。
    """
    links = extract_magic_links(text)
    return links[0] if links else ""


def actions_from_html(page: str) -> dict[str, dict[str, Any]]:
    """抓出页面渲染的 Server Action 隐藏域，**原样**留着待回填。

    返回 `{n: {"id": …, "bound": …, "fields": {"0": …, "1": …}, "key": …}}`。

    只要求 `:0` 存在（它带 action id）；`:1` / `:2` 有就收、没有就不发 ——
    浏览器提交表单时也只回填页面上真实存在的隐藏域。
    """
    fields: dict[str, dict[str, str]] = {}
    for n, idx, val in ACTION_FIELD_RE.findall(page):
        fields.setdefault(n, {})[idx] = html_mod.unescape(val)

    km = ACTION_KEY_RE.search(page)
    key = html_mod.unescape(km.group(1)) if km else ""

    out: dict[str, dict[str, Any]] = {}
    for n, vals in fields.items():
        if "0" not in vals:
            continue                      # 没有 :0 就没有 action id，这条用不了
        try:
            ref = json.loads(vals["0"])
        except ValueError:
            continue                      # 不是 JSON ⇒ 不是 Server Action 引用
        out[n] = {"id": ref.get("id", ""), "bound": ref.get("bound", "$@1"),
                  "fields": vals, "key": key}
    return out


def action_form_fields(n: str, a: dict[str, Any]) -> dict[str, tuple]:
    """把 `actions_from_html` 的产物还原成"要提交的隐藏域"（multipart 元组形式）。

    `:0` 走 `compact_ref()` 重新序列化 —— 保证紧凑 JSON（带空格会被 Next.js 判 500）。
    """
    parts: dict[str, tuple] = {f"$ACTION_REF_{n}": (None, "")}
    for idx, val in sorted(a["fields"].items()):
        parts[f"$ACTION_{n}:{idx}"] = (None, compact_ref(a) if idx == "0" else val)
    if a.get("key"):
        parts["$ACTION_KEY"] = (None, a["key"])
    return parts


def compact_ref(a: dict[str, str]) -> str:
    """还原 `$ACTION_<n>:0` 的取值。

    🔴 **必须是紧凑 JSON，不能带空格。** 页面里渲染的是
    `{"id":"60e492...","bound":"$@1"}`，而 `json.dumps` 默认分隔符是
    `", "` / `": "`，会产出 `{"id": "60e492...", "bound": "$@1"}`。
    服务端对此**不做容错**，直接抛 `500 Internal Server Error` ——
    报的是"服务器内部错误"，跟"多了一个空格"看起来毫无关系。
    实测：紧凑 200 / 带空格 500，唯一变量就是这个字符串。
    """
    return json.dumps({"id": a["id"], "bound": a["bound"]},
                      separators=(",", ":"), ensure_ascii=False)


def parse_js_object(text: str) -> dict[str, str]:
    """解析 JS 对象字面量（键**没有**引号，所以不是 JSON）。

    Stytch 的落地页里是 `xhr.send(JSON.stringify({ public_token: '...', ... }))` ——
    单引号字符串 + 裸键名。`json.loads` 会报
    `Expecting property name enclosed in double quotes`，别把它当 JSON 解。
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*'((?:[^'\\]|\\.)*)'", text):
        out[m.group(1)] = m.group(2).replace("\\'", "'").replace("\\\\", "\\")
    return out


def visible_text(page: str) -> str:
    """剥掉 script / style / 标签，得到压缩过的可见文案（用于人读日志与报错）。"""
    t = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(html_mod.unescape(t).split())
