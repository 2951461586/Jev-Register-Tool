#!/usr/bin/env python
"""离线自测**入口** —— 只做聚合与调度，测试本体在 `tools/tests/`。

拆分原因（2026-09-20 二轮审计 ⑩）：本文件曾 1155 行、20 个测试挤在一起，
改一条断言要在上千行里翻。现在按被测对象分四个模块：

    tests/test_parsing.py        解析层（PoW / 紧凑 JSON / Server Action / JS 字面量）
    tests/test_ledger.py         台账（并集合并 / 状态词汇 / 身份字段归一化）
    tests/test_mailrules.py      收件规则 + OTP 抽取
    tests/test_orchestration.py  编排层（错误码分流 / 申请 / 登录 / 监听 / 并发）

⚠️ **仍然不是 pytest**（刻意不引）：本项目零第三方依赖，跑法不变 ——
    $PY tools/selftest.py
共享夹具见 `tests/support.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))              # tools/

from tests import support  # noqa: E402
from tests.test_ledger import (test_identity_whitespace_normalization,  # noqa: E402
                               test_ledger_union, test_status_vocabulary)
from tests.test_mailrules import test_mailrules, test_otp_extraction  # noqa: E402
from tests.test_orchestration import (test_apply_and_approval,  # noqa: E402
                                      test_auth_error_triage, test_claim,
                                      test_code_mode_falls_back_to_magic_link,
                                      test_concurrency_no_crosstalk,
                                      test_confirm_timeout_headroom,
                                      test_fan_out_worker_crash_is_recorded,
                                      test_key_survives_rerun_failure,
                                      test_login_action_index_drift_retries,
                                      test_success_ledger,
                                      test_watch_skips_keyed)
from tests.test_parsing import (test_compact_ref, test_js_object,  # noqa: E402
                                test_pow, test_setup_action_forms)


def main() -> int:
    test_pow()
    test_compact_ref()
    test_setup_action_forms()
    test_js_object()
    test_ledger_union()
    test_status_vocabulary()
    test_identity_whitespace_normalization()
    test_mailrules()
    test_otp_extraction()
    test_auth_error_triage()
    test_apply_and_approval()
    test_login_action_index_drift_retries()
    test_code_mode_falls_back_to_magic_link()
    test_confirm_timeout_headroom()
    test_watch_skips_keyed()
    test_claim()
    test_key_survives_rerun_failure()
    test_success_ledger()
    test_concurrency_no_crosstalk()
    test_fan_out_worker_crash_is_recorded()
    passed, failed = support.counts()
    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
