"""探针：取消邀请制之后的**完整**链路 —— 建邮箱 → 发信 → 换 token → 登录 → onboarding → 建 key。

与 `resume_pending.py` 的区别：本探针**自己建邮箱**，不依赖台账，一次跑完。

用法：
    python tools/probes/probe_full_new_flow.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import *  # noqa: F401,F403,E402

from src.runner import Pipeline  # noqa: E402
from src.tempemail import TempMailClient  # noqa: E402
from src.typesafe import MODE_LINK  # noqa: E402

t0 = time.time()
mail = TempMailClient()
p = Pipeline(mail=mail, login_mode=MODE_LINK, verbose=True)
email = mail.create_mailbox()
print(f"=== 临时邮箱: {email} ===")

recs = p.resume([email])
rec = recs[0]
print("=" * 90)
print(f"status       = {rec.status}")
print(f"error        = {rec.error}")
print(f"stages       = {rec.stages}")
print(f"api_key      = {rec.api_key}")
print(f"api_key_id   = {rec.api_key_id}")
print(f"timings      = { {k: round(v, 1) for k, v in rec.timings.items()} }")
print(f"profile      = {str(rec.user.get('profile'))[:300]}")
print(f"onboarding   = {rec.user.get('onboarding')}")
print(f"总耗时       = {time.time() - t0:.1f}s")
