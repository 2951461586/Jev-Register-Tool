#!/usr/bin/env python
"""离线自测**入口** —— 只做聚合与调度，测试本体在 `tools/tests/`。

拆分原因（2026-09-20 二轮审计 ⑩）：本文件曾 1155 行、20 个测试挤在一起，
改一条断言要在上千行里翻。现在按被测对象分四个模块：

    tests/test_parsing.py        解析层（紧凑 JSON / Server Action / JS 字面量）
    tests/test_ledger.py         台账（并集合并 / 状态词汇 / 身份字段归一化）
    tests/test_mailrules.py      收件规则 + OTP 抽取
    tests/test_orchestration.py  编排层（错误码分流 / 登录 / onboarding 门禁 /
                                 重跑去重 / 并发）

2026-09-21：邀请制取消后删掉 5 个用例（`test_pow` / `test_apply_and_approval` /
`test_confirm_timeout_headroom` / `test_watch_skips_keyed` / `test_claim`），
新增 5 个（`test_chain_has_no_external_gate` / `test_onboarding_gate_drives_steps` /
`test_onboarding_gate_is_guidance_not_a_gate` / `test_mail_timeout_headroom` /
`test_resume_skips_keyed`）。

同日接入 Remail（**第二个邮箱后端**）时新增 `test_mail_backend_selection` ——
覆盖工厂分派 / `_clone()` 不丢后端 / `domain` 语义按后端取 / 取件凭证落盘。

2026-09-21 补跑时新增 `test_magic_link_truncated_variant` —— 正文里同一个
魔法链接有多份、**只有一部分完整**时，必须挑完整的用（旧实现取第一个 ⇒
取到残缺那条就必失败，实测 4/100 账号栽在这里）。

⚠️ **仍然不是 pytest**（刻意不引）：本项目零第三方依赖，跑法不变 ——
    $PY tools/selftest.py
共享夹具见 `tests/support.py`。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))              # tools/

from tests import support  # noqa: E402
from tests.test_ledger import (test_identity_whitespace_normalization,  # noqa: E402
                               test_ledger_union, test_status_vocabulary)
from tests.test_mailrules import test_mailrules, test_otp_extraction  # noqa: E402
from tests.test_orchestration import (test_auth_callback_payload_shape,  # noqa: E402
                                      test_auth_error_triage,
                                      test_chain_has_no_external_gate,
                                      test_code_mode_falls_back_to_magic_link,
                                      test_concurrency_no_crosstalk,
                                      test_fan_out_worker_crash_is_recorded,
                                      test_key_survives_rerun_failure,
                                      test_login_action_index_drift_retries,
                                      test_magic_link_tries_all_candidates,
                                      test_mail_backend_selection,
                                      test_mail_timeout_headroom,
                                      test_onboarding_gate_drives_steps,
                                      test_onboarding_gate_is_guidance_not_a_gate,
                                      test_post_setup_degrade_is_observable,
                                      test_remail_probe_is_readonly,
                                      test_resume_skips_keyed,
                                      test_success_ledger)
from tests.test_parsing import (test_compact_ref, test_js_object,  # noqa: E402
                                test_magic_link_html_entity,
                                test_magic_link_truncated_variant,
                                test_setup_action_forms)


def _registry_gap() -> tuple[list[str], list[str]]:
    """对比 `tests/` 里定义的 `test_*` 与本文件 `main()` 里实际调用的，返回
    `(漏登记, 不存在)` 两个差集。

    🔴 为什么要这条（2026-09-21 第三轮扫描）：`main()` 是**手抄**的调用清单。
    新加一个 `test_*` 却忘了登记，它就**永远不会执行**，而汇总行照样打印
    "通过 N / 失败 0" —— 计数是**应该**增长的，所以"漏跑"看起来和"成功"
    一模一样。这与本项目反复出现的"不报错、只是少几行"是同一类失效。

    扫描面用 `glob("test_*.py")`，并 `assert` 非空 —— 目录改名后不许静默变绿。
    """
    tests_dir = Path(__file__).resolve().parent / "tests"
    files = sorted(tests_dir.glob("test_*.py"))
    assert files, f"扫描面为空：{tests_dir} 下没有 test_*.py（目录被改名了？）"

    defined: set[str] = set()
    for p in files:
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                defined.add(node.name)

    called: set[str] = set()
    for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id.startswith("test_")):
            called.add(node.func.id)

    return sorted(defined - called), sorted(called - defined)


def main() -> int:
    test_compact_ref()
    test_setup_action_forms()
    test_js_object()
    test_magic_link_html_entity()
    test_magic_link_truncated_variant()
    test_ledger_union()
    test_status_vocabulary()
    test_identity_whitespace_normalization()
    test_mailrules()
    test_otp_extraction()
    test_auth_error_triage()
    test_auth_callback_payload_shape()
    test_chain_has_no_external_gate()
    test_login_action_index_drift_retries()
    test_onboarding_gate_drives_steps()
    test_onboarding_gate_is_guidance_not_a_gate()
    test_code_mode_falls_back_to_magic_link()
    test_mail_timeout_headroom()
    test_resume_skips_keyed()
    test_post_setup_degrade_is_observable()
    test_key_survives_rerun_failure()
    test_success_ledger()
    test_concurrency_no_crosstalk()
    test_fan_out_worker_crash_is_recorded()
    test_mail_backend_selection()
    test_magic_link_tries_all_candidates()
    test_remail_probe_is_readonly()

    # 元检查：登记完整性。放在最后跑，因为它扫的是本文件自己。
    gap, ghost = _registry_gap()
    support.check("★ 用例登记完整（tests/ 里定义的每个 test_* 都在上面被调用）",
                  not gap and not ghost, f"未登记={gap}；调用了但不存在的={ghost}")

    passed, failed = support.counts()
    print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
