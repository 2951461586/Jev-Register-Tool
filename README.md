# Jev-Register-Tool

TypeSafe（Jev / System One）**申请 → 确认邮件 → 获批 → 注册 → 建 API Key → 入库**
全链路工具，**纯 HTTP、无浏览器**。

结合 CF Temp Email Worker 收信，配合 `--mode watch` 常驻监听实现**边到边取**，
可直接批量出号。

**实测战果**：15 个账号全部拿到 API Key，`verify_keys.py` 验收 **可用 16 / 不可用 0**
（真打 `api.typesafe.ai` 推理接口）。16 > 15 是因为其中一个账号被重跑过、
服务端给了**两把** key，两把都有效 —— 台账按邮箱去重会吃掉一把，
所以 `verify_keys.py` 的候选集是「合并视图 ∪ 原始行」。单账号关键路径
`login 6.4s + create_key 3.7s ≈ 10s`。

> **本仓库会含凭据。** 凭据只进 `.env`（代码里一律 `os.getenv()` 且默认空），
> `.env` / `*.har` / `*.eml` / `exports/` / `result/` / `.workbuddy-ai/` 全部 gitignore
> （用通配，不逐条列举）。
> `result/keys.txt` 里是**明文 API Key**，别提交。
> 🔴 `result/` 必须**显式**列目录：`*.txt` 没有任何通配规则覆盖，
> 光靠 `*.json` / `*.jsonl` 挡不住 `result/keys.txt`。

## 快速开始

```bash
PY="F:/epsoft/workbuddy-work/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

cp .env.example .env      # 填 TEMPMAIL_ADMIN_KEY
$PY tools/run_e2e.py --doctor          # 环境体检
$PY tools/selftest.py                  # 自测 123 项，离线可跑
$PY tools/run_e2e.py --mode apply --count 5   # 投递申请
$PY tools/run_e2e.py --mode watch --watch-timeout 900   # 等获批并自动续跑 4→7
$PY tools/run_e2e.py --mode resume --email a@b.com --concurrency 4   # 并发补跑
$PY tools/verify_keys.py               # ★ 验收：真打一次推理接口
```

> `--concurrency` 只对**申请段 / 注册段**有效（每账号只读自己的收件箱索引端点）。
> `--mode watch` **刻意不支持** —— 它读的是全表共享窗口，并发只会互相挤。

## 链路与可自动化程度

| 阶段 | 方式 | 可自动化 | 实测耗时 |
|---|---|---|---|
| 1. 申请 | 纯 HTTP（Framer 表单 + PoW） | ✅ | 201，~3.6s |
| 2. 确认邮件 | 纯 HTTP（Worker 收信） | ✅ | 到达 3~6s |
| 3. **获批** | — | ❌ **外部批量定时审批** | **延迟约 25 分钟** |
| 4. 注册（发码→收码→回调） | 纯 HTTP（Server Action 复刻 + Stytch） | ✅ | 4.5~6.5s |
| 5. onboarding | 纯 HTTP（`/setup/*`） | ✅ | 含在 4 内 |
| 6. 建 API Key | 纯 HTTP（`/api/api-keys`） | ✅ | 1.8~4.1s |
| 7. 入库 | 本地 JSONL 台账 | ✅ | — |
| 8. **验收** | 纯 HTTP（真打推理接口） | ✅ | 每个 ~0.6s |

**唯一的阻断点是阶段 3**，且它**不是技术门槛而是业务门槛**（服务端白名单）。
所以交付形态是**两段式**：`apply` 批量投递 + `watch`/`resume` 跑下游。

> 实测教训：阶段 3 曾让我误判。在 T 时刻做的三重核查（HTTP 索引端点、
> D1 直查、实跑 403）**每一条都准**，但审批批次在 **T+9 分钟**才落地。
> ⇒ **"此刻没查到" ≠ "不存在"。** 对批量流程，正确交付是架定时复查，
> 不是下结论让人放弃。

## 目录结构

```
src/                       库代码
  config.py                常量 + .env 加载 + 启动校验
  mailrules.py             收件规则表 + OTP 抽取（零内部依赖的纯叶子）
  ledger.py                JSONL 台账（并集合并 / 幂等 / 等级语义）
  tempemail.py             CF Worker 收信
  framer_waitlist.py       申请表单 + PoW
  typesafe.py              Server Action / Stytch / onboarding / 建 Key
  pipeline.py              阶段编排（唯一的上层聚合者，含并发扇出）

tools/                     入口脚本
  _bootstrap.py            按标记文件定位仓库根，统一 sys.path
  run_e2e.py               ★ 主入口：apply / watch / resume / claim / scan
  selftest.py              自测 123 项（含负对照，**全程离线**）
  verify_keys.py           ★ 验收 + 导出可用凭据
  probes/                  一次性诊断探针
    probe_confirm.py             看"确认邮件"那一步的每跳原始响应
    probe_confirm_flow.py        干净实验：先确认再回调，用状态码判假设

docs/
  architecture.md          目录 / 模块 / 耦合 / 数据流（依赖图由 AST 算出）
  mail-filters.md          收件过滤规则 + 取码锚定（格式对齐 OpenXLab 项目）
  runbook.md               怎么跑 + 故障处置
  audit-2026-09-20.md      架构/耦合/目录/文档审计（含一个 P0 与全部证据）

evidence/                  录制的证据（har / eml / 抓来的第三方 bundle）
exports/                   运行台账与记录：ledger.jsonl / run_*.json / *.log / _diag/
result/                    ★ 交付物（**只放成功的**）：success.jsonl / keys.txt / keys_verified.json
```

> **`exports/` 与 `result/` 的分工**（2026-09-20 起）：台账要留**全部**尝试
> （含失败的，便于复盘），交付物只该有成功的。以前两者混在 `exports/` 里，
> 取交付物时得自己筛一遍。成功数据由 `stage_create_key()` 直接落 `result/success.jsonl`
> —— 那是**唯一**产出 key 的地方，写在这里就自动覆盖全部四条路径
> （`run_batch` / `resume` / `watch` / `claim`），不需要在四个调用点各写一遍。

**依赖方向单向**：`tools → pipeline → 叶子 → config`，无循环。
第三方依赖**只有 `requests`**（刻意不用 `bs4` / `lxml`）。
详见 `docs/architecture.md`。

## 收件规则（摘要）

格式对齐同机 OpenXLab 项目的 `sender_contains="openxlab"` 做法：
**信封发件人子串第一道，主题子串第二道。**

| 规则名 | `sender_contains` | `subject_contains` |
|---|---|---|
| `waitlist_confirm` | `envelope.updates.typesafe.ai` | `on the waitlist` |
| `account_ready` | `envelope.updates.typesafe.ai` | `account is ready` |
| `welcome_confirm` | `typesafe.ai` | `confirm your email` |
| `signin_link` | `typesafe.ai` | `sign in to typesafe` |
| `signin_code` / `verify_code` | `typesafe.ai` | `sign-in code` / `verification code` |

🔴 **要区分的两封发件人完全相同**（都是 `envelope.updates.typesafe.ai`），
只按发件人过滤会把"在等待名单上"当成"已获批" ⇒ **subject 是必需判别位**。
这也是 OpenXLab 那条规则不能直接照搬的原因（它一类邮件一个域）。

详见 `docs/mail-filters.md`。

## 三个必须分清的 HTTP 状态

| 响应 | 含义 |
|---|---|
| `401 {"error":"Code expired"}` | OTP 错/过期（凭据校验在**前**，不看邮箱） |
| `401 {"error":"Authentication failed"}` | 魔法链接 token **已被用过**（一次性） |
| `403 {"error":"Access restricted"}` | 凭据有效，但身份**不在白名单** |

## 别做这些事

- ❌ 别用 `/admin/all` 拉列表自己筛（烧 D1 读配额 + 会被挤出窗口）
- ❌ 别打 `*.workers.dev` 不带浏览器 UA（CF 边缘 403 / `error code: 1010`）
- ❌ 别对 `api.typesafe.ai` 假设 OpenAI 兼容（它是 `choice`/`score`/`noul` 原语）
- ❌ 别把 `apikey_...` 字符串当终点，**必须真打一次推理接口**
- ❌ 别把"此刻没查到"讲成"不存在"（批量审批有时间差）
- ❌ 别在凭据有效性未验证前把 `403` 当结论
- ❌ 别把长等待放前台（~120s 被 SIGTERM，日志截断会伪造业务结论）
- ❌ 别在 `pipeline` 里就地写取码正则（走 `mailrules.extract_otp`；
  降级路径会在日志里显式警告，**看到警告要去查模板变更，不是重新发码**）
- ❌ 别把 `Pipeline` 的会话 client 挂成实例字段（并发会串号）
- ❌ 别给 `--mode watch` 加并发（它读的是全表共享窗口）

## 相关文档

- `docs/architecture.md` —— 模块耦合、依赖分层、数据流
- `docs/mail-filters.md` —— 收件规则的完整推导、实测数据、取码锚定
- `docs/runbook.md` —— 五种模式、验收、故障处置
- `docs/audit-2026-09-20.md` —— 架构审计（含一个已修的 P0 与全部证据）
