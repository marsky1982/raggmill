import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 端到端验收配置（本地与 CI 共用；CI 里由 `.github/workflows/ci.yml` 的 `e2e` job 跑）。
 *
 * 环境约定：
 * - 后端跑 RAGAS_DEMO=1（演示模式，不连外部服务、不烧 token），数据目录指向
 *   本次运行独占的临时目录，绝不碰仓库内 data/。
 * - 前端 dev server 由 webServer 自动拉起，代理目标用环境变量给（优先级高于 .env.local）。
 * - 浏览器：本地默认复用本机 Edge（channel: msedge，不下载额外内核）；CI 传
 *   `RAGAS_E2E_BROWSER=chromium` 用 Playwright 自带的 chromium（runner 上没有 Edge）。
 * - 后端解释器：本地用 `backend/.venv`，CI 没有 venv 时退回 PATH 上的 `python`。
 * - 单 worker 串行：前后端是共享单例（单实例锁 + 进程内索引），并行会互相干扰。
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BACKEND_PORT = 8137;
const FRONTEND_PORT = 5184;
const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`;
const FRONTEND = `http://127.0.0.1:${FRONTEND_PORT}`;

/** 后端解释器：本地固定用 backend/.venv；CI 没有 venv，退回 PATH 上的 python。 */
function resolvePython(): string {
  const venv = path.resolve(HERE, "../backend/.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  return fs.existsSync(venv) ? `"${venv}"` : "python";
}

/** 浏览器通道：默认复用本机 Edge；CI 传 RAGAS_E2E_BROWSER=chromium 走自带内核。 */
const BROWSER_CHANNEL = process.env.RAGAS_E2E_BROWSER === "chromium" ? undefined : "msedge";

/** 每次运行独占一个临时 data 目录：配置在 main 进程先求值 → 写进 env → worker 继承。 */
function resolveDataDir(): string {
  const existing = process.env.RAGAS_E2E_DATA_DIR;
  if (existing) return existing;
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..*/, "");
  const dir = path.join(os.tmpdir(), `ragas-e2e-data-${stamp}-${process.pid}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

const DATA_DIR = resolveDataDir();
process.env.RAGAS_E2E_DATA_DIR = DATA_DIR;
process.env.RAGAS_E2E_API = BACKEND;
process.env.RAGAS_E2E_UI = FRONTEND;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts",
  globalTeardown: "./e2e/support/teardown.ts",
  fullyParallel: false,
  workers: 1,
  // 本地 0 次（失败立刻暴露）；CI 上给 1 次，兜住 C-05 / C-06 这类高负载下的时序抖动，
  // 真回归仍会失败，且报告里会把「重试后通过」标成 flaky
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: FRONTEND,
    channel: BROWSER_CHANNEL,
    headless: true,
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    permissions: ["clipboard-read", "clipboard-write"],
  },
  webServer: [
    {
      // 后端：demo 模式 + 临时数据目录；本地用 backend/.venv，CI 用 PATH 上的 python
      command: `${resolvePython()} -m uvicorn app.main:app --host 127.0.0.1 --port ${BACKEND_PORT}`,
      cwd: path.resolve(HERE, "../backend"),
      url: `${BACKEND}/api/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        ...process.env,
        RAGAS_DEMO: "1",
        RAGAS_STUDIO_DATA_DIR: DATA_DIR,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
      },
    },
    {
      command: `npm run dev -- --port ${FRONTEND_PORT} --strictPort`,
      cwd: HERE,
      url: FRONTEND,
      timeout: 120_000,
      reuseExistingServer: false,
      env: { ...process.env, VITE_API_TARGET: BACKEND },
    },
  ],
});
