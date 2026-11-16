/** 与 playwright.config.ts 共享的运行期信息（配置先求值并写入 process.env，worker 继承）。 */
export const DATA_DIR = process.env.RAGAS_E2E_DATA_DIR || "";
export const API = process.env.RAGAS_E2E_API || "http://127.0.0.1:8137";
export const UI = process.env.RAGAS_E2E_UI || "http://127.0.0.1:5184";
