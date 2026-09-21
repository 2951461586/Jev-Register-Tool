"""TypeSafe 端到端注册链路（注册 → 登录 → 创建 API Key → 入库）。

2026-09-21：邀请制取消，链路从 6 段缩到 4 段
────────────────────────────────────────────
    ~~申请（Framer 表单）~~ → ~~等回执~~ → ~~等人工审批~~ → 登录 → onboarding → 建 Key
    →  注册+登录（`POST /login` 直接发确认邮件，点链接即建会话）→ onboarding → 建 Key

`src/framer_waitlist.py` 已整体删除（申请环节不存在了）。

模块分层（依赖方向单向：tools → runner → stages → 叶子 → config，无循环）：

    叶子（互不依赖，只依赖 config）
      config.py           常量集中地 + .env 加载 + 启动校验
      mailrules.py        收件过滤规则表 —— **唯一零内部依赖的模块**
      parsing.py          纯解析：Server Action / JS 字面量 / 魔法链接（零第三方依赖）
      ledger.py           JSONL 台账（并集合并 / 幂等 / 升级语义）
      tempemail.py        CF Temp Email Worker 客户端
      typesafe.py         Server Action / Stytch / onboarding / 建 Key

    聚合（两层，方向仍是单向）
      stages.py           单账号阶段实现（`StageMixin`）+ `AccountRecord`
      runner.py           `Pipeline`：批量 / 并发 / 台账写入

`runner` 继承 `stages.StageMixin`，`stages` **不**反向引用 `runner`。

入口在 `tools/`（`run_e2e.py` 主入口、`verify_keys.py` 验收、
`relogin_pending.py` 重试登录、`selftest.py` 自测、`probes/` 一次性探针）。
架构与耦合细节见 `docs/architecture.md`。
"""

__all__ = [
    "config",
    "mailrules",
    "ledger",
    "tempemail",
    "typesafe",
    "parsing",
    "stages",
    "runner",
]
