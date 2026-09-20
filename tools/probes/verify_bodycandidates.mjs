// 用真实 bundle 复现/验证 `bodyCandidates is not defined`。
//
// 背景（2026-09-20）：共享 temp-email-worker 从 07:46 起，**每一封**邮件的
// email 事件都以 `outcome: exception` 结束，日志消息 `bodyCandidates is not defined`。
// 后果：`storeEmail` 在 insertMessage 之前就抛，D1 里一封新邮件都没有
// （最后一行 06:12:02），所有依赖收信的项目一起断。
//
// 这个脚本对**多个构建产物**跑同一封真实形状的邮件，直接给出"哪个构建会炸"：
//   - 线上 bundle（从 CF 拉的）
//   - 各次 dry-run 产物（对齐部署时间）
//   - 修复后的构建
//
// 判据不是"看起来对"，是 `storeEmail` 有没有抛、抛的是什么。
//
// 用法（在 Jev-Register-Tool 根目录）：
//   node tools/probes/verify_bodycandidates.mjs <bundle.js> [<bundle.js> ...]

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const HTML_IMPORT_RE = /^import ADMIN_HTML from "\.\/[^"]+\.html";$/m;

// 一封**多部分**邮件：只有非 body-only 的邮件才会走到出问题的那一行，
// 所以 fixture 必须让 `bodyOnly` 为 falsy（发件人不在 BODY_ONLY_RULES 里）。
const RAW = [
  "From: TypeSafe <bounces+<acct>-x@em5082.typesafe.ai>",
  "To: probe@example-mail.test",
  "Subject: Your TypeSafe sign-in code",
  "MIME-Version: 1.0",
  'Content-Type: multipart/alternative; boundary="bnd42"',
  "",
  "--bnd42",
  "Content-Type: text/plain; charset=utf-8",
  "",
  "123456 is your one-time code to sign in to your account",
  "--bnd42",
  "Content-Type: text/html; charset=utf-8",
  "",
  "<html><body><p>123456 is your one-time code to sign in to your account</p></body></html>",
  "--bnd42--",
  ""
].join("\r\n");

function makeEnv() {
  const calls = [];
  const stmt = {
    bind(...args) { calls.push(args); return stmt; },
    async all() { return { results: [] }; },
    async run() { return { success: true, meta: {} }; },
    async first() { return null; }
  };
  return {
    calls,
    DB: { prepare: () => stmt },
    ADMIN_TOKEN: "x", ADMIN_PASSWORD: "x"
  };
}

function makeMessage() {
  const headers = new Map([["subject", "Your TypeSafe sign-in code"],
                           ["message-id", "<probe-1@example-mail.test>"]]);
  return {
    to: "probe@example-mail.test",
    from: "bounces+<acct>-x@em5082.typesafe.ai",
    headers: { get: (k) => headers.get(String(k).toLowerCase()) ?? null },
    raw: new Response(RAW).body
  };
}

async function loadModule(bundlePath) {
  const code = fs.readFileSync(bundlePath, "utf8");
  let stubbed = code.replace(HTML_IMPORT_RE, 'const ADMIN_HTML = "";');
  if (stubbed === code) {
    // 已经打过桩的候选（如 tmp/_harness/index.mjs）本来就带 `const ADMIN_HTML`，
    // 直接用；两者都没有才算失败 —— 否则会把"已打桩"误报成"stub 失败"。
    if (!/const ADMIN_HTML\s*=/.test(code)) {
      return { error: '找不到 `import ADMIN_HTML from "./*.html";` 行，也没有现成的桩' };
    }
    stubbed = code;
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bc-"));
  const file = path.join(dir, "mod.mjs");
  fs.writeFileSync(file, stubbed, "utf8");
  return { mod: await import(pathToFileURL(file).href) };
}

let failures = 0;
for (const bundlePath of process.argv.slice(2)) {
  const label = path.basename(path.dirname(bundlePath)) + "/" + path.basename(bundlePath);
  let verdict;
  try {
    const { mod, error } = await loadModule(bundlePath);
    if (error) throw new Error(error);
    const env = makeEnv();
    await mod.storeEmail(makeMessage(), env);
    verdict = env.calls.length
      ? `✓ 未抛异常，且真的走到了 D1（prepare/bind 调用 ${env.calls.length} 次）`
      : "✓ 未抛异常（但没有 D1 调用 —— 可能被规则提前 return 了）";
  } catch (err) {
    failures += 1;
    verdict = `🔴 ${err.name}: ${err.message}`;
  }
  console.log(`  ${label.padEnd(46)} ${verdict}`);
}
console.log(failures ? `\n有 ${failures} 个构建会炸` : "\n全部构建都正常");
process.exit(0);
