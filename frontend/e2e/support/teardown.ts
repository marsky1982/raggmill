import fs from "node:fs";

/** 运行结束清理本次独占的临时数据目录（只在目录名符合本次运行前缀时删除）。 */
export default async function teardown() {
  const dir = process.env.RAGAS_E2E_DATA_DIR;
  if (!dir || !/ragas-e2e-data-\d{8}T/.test(dir.replace(/\\/g, "/"))) return;
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    // 清理失败不影响测试结论，临时目录留给下次手动删
  }
}
