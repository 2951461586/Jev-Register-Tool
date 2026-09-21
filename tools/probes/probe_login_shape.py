"""探针：/login 页面在"取消邀请制"之后渲染成什么形态。

只做 GET，不发信 —— 先确认 Server Action 隐藏域是否还在、编号是什么。

用法：
    python tools/probes/probe_login_shape.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import *  # noqa: F401,F403,E402

import requests  # noqa: E402

from src import config  # noqa: E402
from src.parsing import (ACTION_FIELD_RE, ACTION_KEY_RE, actions_from_html,  # noqa: E402
                         visible_text)


def probe(url: str, label: str) -> str:
    print("=" * 100)
    print(f"### {label}")
    print(f"### GET {url}")
    s = requests.Session()
    s.headers.update({"User-Agent": config.UA,
                      "Accept": "text/html,application/xhtml+xml"})
    r = s.get(url, timeout=40)
    print(f"HTTP {r.status_code}  len={len(r.text)}  final={r.url}")
    page = r.text

    raw = ACTION_FIELD_RE.findall(page)
    print(f"$ACTION_<n>:<idx> 隐藏域: {len(raw)} 个")
    for n, idx, val in raw:
        print(f"    n={n} idx={idx} value={val[:100]}")
    km = ACTION_KEY_RE.search(page)
    print(f"$ACTION_KEY: {'有' if km else '无'}")

    acts = actions_from_html(page)
    print(f"actions_from_html -> {sorted(acts)}")
    for n, a in sorted(acts.items()):
        print(f"    [{n}] id={a['id'][:24]}… bound={a['bound']} fields={sorted(a['fields'])}")

    # JS 形态：next-action 头用的 action id 与加密 bound args
    ja = re.findall(r'\$ACTION_ID_([0-9a-f]{40,})', page)
    print(f"$ACTION_ID_<hash> 出现: {sorted(set(ja))}")
    enc = re.findall(r'\$ACTION_REF_\d+', page)
    print(f"$ACTION_REF_<n> 出现: {sorted(set(enc))}")
    print("--- 可见文案（前 400 字）---")
    print(visible_text(page)[:400])
    return page


if __name__ == "__main__":
    p1 = probe(config.SITE_LOGIN, "不带 waitlist 参数")
    p2 = probe(f"{config.SITE_LOGIN}?waitlist=probe-shape%40example.com", "带 waitlist 参数")
    print("=" * 100)
    print("两份 HTML 是否逐字节相同:", p1 == p2)
    Path("F:/epsoft/workbuddy-work/.workbuddy-ai/tmp/harx/login_plain.html").write_text(
        p1, encoding="utf-8")
    Path("F:/epsoft/workbuddy-work/.workbuddy-ai/tmp/harx/login_waitlist.html").write_text(
        p2, encoding="utf-8")
