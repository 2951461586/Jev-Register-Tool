"""TypeSafe 端到端注册链路（申请 → 确认邮件 → 获批 → 注册 → 创建 API Key → 入库）。

模块分层（依赖方向单向：tools → pipeline → 叶子 → config，无循环）：

    叶子（互不依赖，只依赖 config）
      config.py           常量集中地 + .env 加载 + 启动校验
      mailrules.py        收件过滤规则表 —— **唯一零内部依赖的模块**
      ledger.py           JSONL 台账（并集合并 / 幂等 / 升级语义）
      tempemail.py        CF Temp Email Worker 客户端
      framer_waitlist.py  申请表单 + PoW 复刻
      typesafe.py         Server Action / Stytch / onboarding / 建 Key

    聚合
      pipeline.py         阶段编排（唯一依赖全部叶子的模块）

入口在 `tools/`（`run_e2e.py` 主入口、`verify_keys.py` 验收、
`selftest.py` 自测、`probes/` 一次性探针）。
架构与耦合细节见 `docs/architecture.md`。
"""

__all__ = [
    "config",
    "mailrules",
    "ledger",
    "tempemail",
    "framer_waitlist",
    "typesafe",
    "pipeline",
]
