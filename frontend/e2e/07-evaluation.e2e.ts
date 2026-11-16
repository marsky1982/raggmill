import { test, expect, type APIRequestContext, type Page } from "@playwright/test";
import { ensureDefaultPresets, listPresets } from "./support/seed";
import { expectDebugText, expectPageTitle, goto } from "./support/ui";

const TASK_TIMEOUT = 45_000;

/** 等指标清单加载完（checkbox 渲染即已就绪），再把默认 LLM / Embedding 预设填进表单。 */
async function fillPresetIds(page: Page, request: APIRequestContext) {
  await expect(page.getByRole("checkbox").first()).toBeVisible({ timeout: 15_000 });
  await ensureDefaultPresets(request);
  const presets = await listPresets(request);
  const llm = presets.find((p) => p.kind === "llm");
  const emb = presets.find((p) => p.kind === "embedding");
  await page.getByLabel("LLM 预设 ID").fill(llm!.id);
  await page.getByLabel("Embedding 预设 ID（按需）").fill(emb!.id);
}

test.describe("G 组 · 质量评估（演示模式走通三种任务终态）", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goto(page, "质量评估");
    await expectPageTitle(page, "五指标质量评估");
  });

  test("G-01 预检字段能力并保存为离线数据集", async ({ page }) => {
    await page.getByRole("button", { name: "预检" }).click();
    await expect(page.getByText("字段能力预检")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/可评估 1/).first()).toBeVisible();

    await page.getByRole("button", { name: "保存为离线数据集" }).click();
    await expect(page.getByText(/已保存：/)).toBeVisible({ timeout: 15_000 });
  });

  test("G-02 演示模式评分：任务卡显示任务 ID 与 MOCK 告警，完成后出导出卡片", async ({ page, request }) => {
    await fillPresetIds(page, request);
    await page.getByRole("button", { name: "启动五指标评分" }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    // 任务 ID 直接上进度卡片（此前只在调试窗口可见），排查时可从卡片抄走
    await expect(page.getByTestId("task-id")).toHaveText(/^task-/);
    // 演示模式的 MOCK 告警进任务日志
    await expect(page.getByText(/MOCK 演示评分/).first()).toBeVisible();

    // 终态：进度卡收起，导出卡片按评估 ID 出现
    await expect(page.getByText(/评估 ID：eval-/)).toBeVisible({ timeout: TASK_TIMEOUT });
    await expect(page.getByRole("link", { name: "JSONL" })).toBeVisible();
    await expect(page.getByRole("link", { name: "CSV" })).toBeVisible();
    await expect(page.getByRole("status")).toHaveCount(0);
    // 模拟分数要在导出卡片上标出来（导出文件另有 -演示数据 后缀与 is_mock 列）
    await expect(page.getByTestId("mock-badge")).toHaveText("⚠ MOCK 演示评分");
  });

  test("G-03 observation id 重复：任务终态失败，页面报错且不出导出卡片", async ({ page, request }) => {
    const row = { id: "dup", user_input: "问题", response: "回答", retrieved_contexts: ["上下文"], reference: "标准答案" };
    await page.getByLabel("观察数据（JSON 数组）").fill(JSON.stringify([row, row], null, 2));
    await fillPresetIds(page, request);
    await page.getByRole("button", { name: "启动五指标评分" }).click();

    await expect(page.getByRole("alert")).toContainText("observation id 重复", { timeout: TASK_TIMEOUT });
    await expect(page.getByText(/评估 ID：eval-/)).toHaveCount(0);
    await expect(page.getByRole("status")).toHaveCount(0);
  });

  test("G-04 评分中取消：任务卡消失，无导出结果、无报错，调试抽屉可查 canceled 终态", async ({ page, request }) => {
    await fillPresetIds(page, request);
    await page.getByRole("button", { name: "启动五指标评分" }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    await page.getByRole("button", { name: "取消", exact: true }).click();

    await expect(page.getByRole("status")).toHaveCount(0, { timeout: TASK_TIMEOUT });
    // 进度卡片整个收起（任务 ID 一并消失），不是停在「已取消」
    await expect(page.getByTestId("task-id")).toHaveCount(0);
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.getByText(/评估 ID：eval-/)).toHaveCount(0);
    await expectDebugText(page, /任务 task-[a-z0-9]+: canceled/);
  });
});
