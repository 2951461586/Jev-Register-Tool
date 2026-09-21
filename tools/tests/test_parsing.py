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
