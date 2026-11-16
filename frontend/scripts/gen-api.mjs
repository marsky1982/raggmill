/**
 * 生成前端 API 类型：后端离线导出 OpenAPI → openapi-typescript 生成 src/api-types.ts。
 *
 * 用法（仓库根目录或 frontend/ 均可）：
 *   npm run gen:api
 *
 * 前置条件：backend/.venv 可用（只需离线导入 app，不启动服务、不连外部依赖）。
 * 退出码：0 成功；2 环境没准备好（找不到解释器 / 依赖缺失）；1 生成失败。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(frontendDir, "..");
const backendDir = join(repoRoot, "backend");
const outFile = join(frontendDir, "src", "api-types.ts");

function fail(code, message) {
  console.error(message);
  process.exit(code);
}

const venvPython =
  process.platform === "win32"
    ? join(backendDir, ".venv", "Scripts", "python.exe")
    : join(backendDir, ".venv", "bin", "python");
const python = existsSync(venvPython)
  ? venvPython
  : process.platform === "win32"
    ? "python"
    : "python3";

const cli = join(frontendDir, "node_modules", "openapi-typescript", "bin", "cli.js");
if (!existsSync(cli)) {
  fail(2, `缺少 openapi-typescript（${cli}），请先在 frontend/ 执行 npm install`);
}

const tmp = mkdtempSync(join(tmpdir(), "ragas-openapi-"));
const specPath = join(tmp, "openapi.json");

function run(exec, args, label) {
  const r = spawnSync(exec, args, { cwd: repoRoot, stdio: "inherit", encoding: "utf-8" });
  if (r.error) {
    fail(2, `${label}无法执行（${exec}）：${r.error.message}`);
  }
  if (r.status !== 0) {
    fail(r.status === 2 ? 2 : 1, `${label}失败（exit=${r.status}）`);
  }
}

try {
  run(
    python,
    [join(backendDir, "scripts", "export_openapi.py"), "--out", specPath],
    "OpenAPI 导出",
  );
  run(process.execPath, [cli, specPath, "--output", outFile], "前端类型生成");
  const size = statSync(outFile).size;
  console.log(`已生成 ${outFile}（${size} 字节）`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
