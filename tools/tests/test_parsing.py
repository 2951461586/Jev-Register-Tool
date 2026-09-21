"""解析层自测：`$ACTION` 紧凑 JSON / Server Action 表单 / Stytch JS 字面量。

样本 HTML 是**真实抓取**的（见各常量的注释），不是编的 —— 用编的样本会造出
"断言全过、线上全废"的假安全感。

⚠️ 这里曾有 `test_pow`（Framer 表单的 Proof-of-Work 复刻）。
2026-09-21 邀请制取消，`src/framer_waitlist.py` 整体删除，PoW 不再有生产
调用点 ⇒ 该用例连同 `hashlib` / `fw` 依赖一并删除。
"""

from __future__ import annotations

import html
import json
import re

from .support import check, ps, ts


def test_compact_ref() -> None:
    print("\n[Server Action bound 参数]")
    a = {"id": "60e492c6afe6018dbb5fb596f90cddf1a3e0b3db7d", "bound": "$@1"}
    s = ps.compact_ref(a)
    check("紧凑、无空格", s == '{"id":"60e492c6afe6018dbb5fb596f90cddf1a3e0b3db7d","bound":"$@1"}', s)
    check("[负对照] 默认 json.dumps 会带空格（这就是 500 的根因）",
          json.dumps(a) != s and '"id": ' in json.dumps(a))


#: 站点 2026-09-20 起的 `/setup/*` 真实渲染形态：索引是 **1**、只有 `:0`/`:1`，
#: 另加一个 `$ACTION_KEY`。旧实现枚举 ("2","3","4") 且要求 `:2` 存在 ⇒ 一条都抓不到。
_SETUP_HTML_NEW = (
    '<form action="" method="POST" encType="multipart/form-data">'
    '<input type="hidden" name="$ACTION_REF_1"/>'
    '<input type="hidden" name="$ACTION_1:0" value="{&quot;id&quot;:'
    '&quot;60cda63fb0752ab5b5cd77aa976e845c5feef9ae16&quot;,&quot;bound&quot;:&quot;$@1&quot;}"/>'
    '<input type="hidden" name="$ACTION_1:1" value="[{}]"/>'
    '<input type="hidden" name="$ACTION_KEY" value="kd64c2077489fba7459c31df809cd11b9"/>'
    "</form>"
)

#: `/login` 页至今仍是旧形态：索引 2/3/4，各带 `:0`/`:1`/`:2`。
_LOGIN_HTML_OLD = (
    '<input type="hidden" name="$ACTION_REF_2"/>'
    '<input type="hidden" name="$ACTION_2:0" value="{&quot;id&quot;:&quot;aaa&quot;,&quot;bound&quot;:&quot;$@1&quot;}"/>'
    '<input type="hidden" name="$ACTION_2:1" value="[{}]"/>'
    '<input type="hidden" name="$ACTION_2:2" value="[&quot;x&quot;]"/>'
    '<input type="hidden" name="$ACTION_REF_3"/>'
    '<input type="hidden" name="$ACTION_3:0" value="{&quot;id&quot;:&quot;bbb&quot;,&quot;bound&quot;:&quot;$@1&quot;}"/>'
    '<input type="hidden" name="$ACTION_3:1" value="[{}]"/>'
    '<input type="hidden" name="$ACTION_3:2" value="[&quot;y&quot;]"/>'
)


def _legacy_actions_from_html(page: str) -> dict:
    """**复刻旧实现**，只用于负对照：证明 fixture 真的能暴露那个 bug。"""
    out = {}
    for n in ("2", "3", "4"):
        m0 = re.search(r'name="\$ACTION_%s:0"\s+value="([^"]*)"' % n, page)
        m2 = re.search(r'name="\$ACTION_%s:2"\s+value="([^"]*)"' % n, page)
        if not (m0 and m2):
            continue
        out[n] = json.loads(html.unescape(m0.group(1)))["id"]
    return out


def test_setup_action_forms() -> None:
    """🔴 回归：`$ACTION_<n>` 的索引集合**不许写死**。

    2026-09-20 站点改版，`/setup/*` 把隐藏域换成索引 `1` + 只有 `:0`/`:1` + `$ACTION_KEY`。
    旧实现（枚举 2/3/4 且要求 `:2`）一条都抓不到 ⇒ `acts` 恒空 ⇒ 退化到录制 HAR 里
    陈旧的 action id ⇒ POST 返回 `404 Server action not found.` ⇒
    对外只看到 `onboarding 失败: HTTP 404`。
    """
    print("\n[Server Action 渲染形态]")

    acts = ps.actions_from_html(_SETUP_HTML_NEW)
    check("★ 新形态能抓到索引 1（旧实现抓不到）", "1" in acts, str(sorted(acts)))
    check("★ 只要求 `:0`，`:2` 缺失不再直接 skip",
          sorted(acts.get("1", {}).get("fields", {})) == ["0", "1"],
          str(acts.get("1", {}).get("fields")))
    check("★ `$ACTION_KEY` 被带出来（浏览器也会回填它）",
          acts.get("1", {}).get("key") == "kd64c2077489fba7459c31df809cd11b9",
          str(acts.get("1", {}).get("key")))
    check("action id 解析正确",
          acts.get("1", {}).get("id") == "60cda63fb0752ab5b5cd77aa976e845c5feef9ae16",
          str(acts.get("1", {}).get("id")))

    # 负对照 1：旧实现在同一份 fixture 上必须**什么都抓不到**
    check("★ [负对照] 旧实现在新形态上返回空（证明 fixture 能暴露该 bug）",
          _legacy_actions_from_html(_SETUP_HTML_NEW) == {},
          str(_legacy_actions_from_html(_SETUP_HTML_NEW)))

    # 负对照 2：新实现不能把 /login 的旧形态弄坏
    old = ps.actions_from_html(_LOGIN_HTML_OLD)
    check("[负对照] /login 旧形态仍能抓到 2 和 3",
          sorted(old) == ["2", "3"], str(sorted(old)))
    check("[负对照] 旧形态的 `:2` 仍然被保留",
          sorted(old.get("2", {}).get("fields", {})) == ["0", "1", "2"],
          str(old.get("2", {}).get("fields")))
    check("[负对照] 旧形态没有 `$ACTION_KEY` 就不该凭空造一个",
          old.get("2", {}).get("key") == "", str(old.get("2", {}).get("key")))

    # 回填：只发页面上真实存在的隐藏域
    files = ps.action_form_fields("1", acts["1"])
    check("回填含 $ACTION_REF_1", "$ACTION_REF_1" in files, str(sorted(files)))
    check("★ `:0` 被重新序列化成紧凑 JSON",
          files["$ACTION_1:0"][1] == '{"id":"60cda63fb0752ab5b5cd77aa976e845c5feef9ae16","bound":"$@1"}',
          files["$ACTION_1:0"][1])
    check("★ 页面没有 `:2` ⇒ 回填里也不许出现 `$ACTION_1:2`（别凭空造字段）",
          "$ACTION_1:2" not in files, str(sorted(files)))
    check("$ACTION_KEY 被回填", files.get("$ACTION_KEY", (None, ""))[1] == acts["1"]["key"])


def test_js_object() -> None:
    print("\n[Stytch 落地页 JS 对象字面量]")
    sample = ("{ public_token: 'public-token-live-abc', "
              "redirect_url: 'https://console.typesafe.ai/auth/callback?token=T&waitlist=a%40b.com', "
              "magic_id: 'magic-live-1' }")
    d = ps.parse_js_object(sample)
    check("裸键名可解析", d.get("public_token") == "public-token-live-abc", str(d))
    check("URL 值完整", d.get("redirect_url", "").endswith("waitlist=a%40b.com"))
    check("token 可从中抽出",
          ts.TypeSafeClient.token_from_redirect_url(d["redirect_url"]) == "T")
    # 负对照：标准 JSON 解析必须失败（证明它不是 JSON）
    try:
        json.loads(sample)
        check("[负对照] json.loads 应该失败", False, "居然解析成功了")
    except json.JSONDecodeError:
        check("[负对照] json.loads 确实失败（所以不能用 JSON 解析）", True)


def test_magic_link_html_entity() -> None:
    """魔法链接提取必须做 **HTML 实体反转义**。

    2026-09-21 接入 Remail（第二个邮箱后端）时实测踩到：两个后端给的正文形态
    **不同** —— CF Worker 是纯文本，Remail 是 **HTML**，后者的链接里 `&`
    被转义成 `&amp;`。不还原的话，提取出的查询串是

        ?public_token=X&amp;stytch_token_type=magic_links&amp;token=Y

    解析方（Stytch）看到的参数名是 `amp;stytch_token_type` / `amp;token`
    ⇒ **等于根本没传 token**，交换必然失败，而报错读起来像"链接无效/过期"，
    会把排查引向"重新发信"，白烧账号。

    ⚠️ 样本用的是**实测抓到的原文形态**（真实参数顺序 + 真实 `&amp;`），不是编的。
    """
    print("\n[魔法链接：HTML 实体反转义]")
    html_body = ('<a href="https://login.typesafe.ai/v1/magic_links/redirect'
                 '?public_token=public-token-live-abc&amp;stytch_token_type=magic_links'
                 '&amp;token=TOK123">Confirm</a>')
    u = ps.extract_magic_link(html_body)
    check("★ HTML 正文里的链接被反转义（&amp; → &）",
          "&amp;" not in u and "&token=TOK123" in u, u)
    check("★ token 能正常抽出（不带上 `amp;` 前缀）",
          ts.TypeSafeClient.token_from_redirect_url(u) == "TOK123", u)
    check("★ 参数名没被污染成 `amp;token`", "amp;token" not in u, u)

    # 负对照：纯文本形态（CF Worker 的正文）必须**逐字不变**
    plain = ("https://login.typesafe.ai/v1/magic_links/redirect"
             "?stytch_token_type=magic_links&token=PLAIN1")
    check("[负对照] 纯文本形态不受影响（CF 后端零回归）",
          ps.extract_magic_link(plain) == plain, ps.extract_magic_link(plain))
    check("空输入返回空串（不抛异常）", ps.extract_magic_link("") == "")
    check("正文里没有链接时返回空串",
          ps.extract_magic_link("hello, no link here") == "")


def test_magic_link_truncated_variant() -> None:
    """正文里**同一 URL 有多份、只有一部分完整**时，必须挑完整的用。

    2026-09-21 补跑实测（4/100 账号必失败的根因）：CF Worker 存下来的正文里
    同一个 URL 出现 **12 次**，其中 **8 次被截断了 4 个字符**：

        完整：`…&stytch_token_type=magic_links&token=D8SZNL…`
        残缺：`…&stytch_token_type=magic_links&tokenSZNL…`   ← `=D8` 整个没了

    残缺形态等于**根本没传 `token`**，Stytch 回 `400 invalid_public_token_id`
    （报的是 `public_token` 的格式问题，与"少了个参数"毫无字面关联），外层把它
    读成"链接可能已被使用/过期" ⇒ 排查被引向"重新发信"，白烧账号。

    ⚠️ `=D8` 是 token 的**字面字符**，不是 quoted-printable 转义。受控实验
    （四种形态各 GET 一次，见 `_looks_complete` docstring）：只有
    `&token=D8SZNL…` 回 **200 + dfp payload**，补 `=` 或解成 0xD8 都是 400。
    所以**不能靠"还原转义"修**，只能靠"换一条候选"。

    样本用的是**实测抓到的原文形态**（真实 token 前后段），不是编的。
    """
    print("\n[魔法链接：残缺候选必须排在完整候选之后]")
    head = ("https://login.typesafe.ai/v1/magic_links/redirect"
            "?public_token=public-token-live-620c0996-4644-4af3-b592-31cb05006523"
            "&stytch_token_type=magic_links&token")
    complete = head + "=D8SZNLCEWqnwIucRySURNiIZob5Y5B8rVl7nEenAmiWJ"
    truncated = head + "SZNLCEWqnwIucRySURNiIZob5Y5B8rVl7nEenAmiWJ"
    # 实测正文顺序：**残缺的在前面**（所以"取第一个"必然踩坑）
    body = f"Continue:\n{truncated}\n\nIf you're having trouble:\n{complete}\n"

    cands = ps.extract_magic_links(body)
    check("★ 两条候选都被收下（不丢）", len(cands) == 2, cands)
    check("★ 完整候选排在第一位", cands and cands[0] == complete, cands[:1])
    check("★ extract_magic_link 取到的是完整那条",
          ps.extract_magic_link(body) == complete, ps.extract_magic_link(body))
    check("★ 完整那条能抽出 token（不带 `D8` 截断）",
          ts.TypeSafeClient.token_from_redirect_url(complete).startswith("D8SZNL"),
          complete[-50:])
    check("残缺那条被判为不完整",
          not ps._looks_complete(truncated), truncated[-50:])
    check("完整那条被判为完整", ps._looks_complete(complete), complete[-50:])

    # 负对照 1：正文里**只有**残缺那条时，仍然返回它（不返回空串）——
    #   让调用方"试一次然后失败"，比"直接说没找到链接"更接近真相。
    only_bad = ps.extract_magic_links(truncated)
    check("[负对照] 只有残缺候选时仍返回它（不返回空）",
          len(only_bad) == 1 and only_bad[0] == truncated, only_bad)

    # 负对照 2：`public_token=` 的存在与否不能靠子串误判 ——
    #   `stytch_token_type=magic_links` 里也含 `token=` 子串，必须用 `[?&]token=` 锚定。
    no_pub = head.replace("?public_token=public-token-live-620c0996-4644-4af3-b592-"
                          "31cb05006523&", "?") + "=D8SZNL"
    check("[负对照] 缺 public_token 时不算完整（`&token=` 锚定正确）",
          not ps._looks_complete(no_pub), no_pub[-60:])
    check("[负对照] 仅有 `stytch_token_type=magic_links` 不被当成完整",
          not ps._looks_complete("https://login.typesafe.ai/v1/magic_links/redirect"
                                 "?stytch_token_type=magic_links"),
          "stytch_token_type=magic_links")

