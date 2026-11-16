import { test, expect, type Page } from "@playwright/test";
import { ensureCoreDoc, ensureDefaultPresets, ensureKg, listKgs } from "./support/seed";
import { closeDebug, expectDebugText, expectPageTitle, goto, openDebug, waitKgPresets } from "./support/ui";

/** demo 模式下每个阶段 0.7s，整图约 5s；给终态留足余量 */
const TASK_TIMEOUT = 45_000;

/** KG 列表行：行内主按钮的可访问名带图谱名 + 节点统计，取它的父节点即为整行 */
function kgRow(page: Page, name: string) {
  return page.getByRole("button", { name: new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) })
    .first().locator("xpath=..");
}

test.describe("C 组 · 知识图谱", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goto(page, "知识图谱");
    await expectPageTitle(page, "知识图谱");
  });

  test("C-03 提交建图：SSE 进度 → 终态 → 列表 +1 → 自动预览", async ({ page, request }) => {
    await ensureCoreDoc(request);
    await ensureDefaultPresets(request);
    await page.reload();
    await goto(page, "知识图谱");
    await waitKgPresets(page);
    const before = (await listKgs(request)).length;

    await page.getByRole("button", { name: /生成知识图谱（\d+ 个文档）/ }).click();
    // SSE 进度：状态行 + 阶段 + 步骤徽章 + 运行中可取消
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    await expect(page.getByText(/阶段: /)).toBeVisible();
    await expect(page.getByText(/步骤 \d+\/\d+/).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("button", { name: "取消", exact: true })).toBeVisible();

    // 终态后 TaskProgress 会被卸载（onDone 立刻清 taskId），只能从产物判断成功
    await expect(page.getByText(/已保存知识图谱（/)).toContainText(
      `已保存知识图谱（${before + 1}）`, { timeout: TASK_TIMEOUT },
    );
    await expect(page.getByText("图谱预览")).toBeVisible();
    await expect(page.getByText(/节点 \/ /).last()).toBeVisible();
    expect((await listKgs(request)).length).toBe(before + 1);
  });

  test("C-05 取消保留断点 → 从断点继续 → 完成", async ({ page }) => {
    await ensureCoreDoc(page.request);
    await ensureDefaultPresets(page.request);
    await page.reload();
    await goto(page, "知识图谱");
    await waitKgPresets(page);

    await page.getByRole("button", { name: /生成知识图谱（\d+ 个文档）/ }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    // 等到第一个断点落盘（阶段 1 完成）
    await expect(page.getByText(/步骤 1\/\d+/).first()).toBeVisible({ timeout: 20_000 });

    // 取消 → 三选一确认层：选「取消任务（保留断点）」
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByText("取消知识图谱生成？")).toBeVisible();
    await page.getByRole("button", { name: "取消任务（保留断点）" }).click();
    // 终态（canceled）到达后 TaskProgress 立即卸载，只能从断点卡片判断「取消是否生效」
    await expect(page.getByText(/发现可恢复的生成断点/)).toBeVisible({ timeout: TASK_TIMEOUT });
    await expect(page.getByRole("status")).toHaveCount(0);

    // 断点卡片出现
    await expect(page.getByText(/发现可恢复的生成断点/)).toBeVisible();
    const before = (await listKgs(page.request)).length;
    await page.getByRole("button", { name: "从断点继续" }).first().click();
    // 续跑完成后同样会立刻卸载进度卡：以「列表 +1 且自动预览」判定终态
    await expect(page.getByText("图谱预览")).toBeVisible({ timeout: TASK_TIMEOUT });
    await expect(page.getByText(/已保存知识图谱（/)).toContainText(`已保存知识图谱（${before + 1}）`);
    expect((await listKgs(page.request)).length).toBe(before + 1);
  });

  test("C-06 取消时应提供「取消并删除断点」选项（文档期望的二次确认）", async ({ page }) => {
    await ensureCoreDoc(page.request);
    await ensureDefaultPresets(page.request);
    await page.reload();
    await goto(page, "知识图谱");
    await waitKgPresets(page);

    await page.getByRole("button", { name: /生成知识图谱（\d+ 个文档）/ }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    await expect(page.getByText(/步骤 1\/\d+/).first()).toBeVisible({ timeout: 20_000 });

    // 验收文档要求：点取消先弹确认层，三选一（继续生成 / 保留断点 / 取消并删除断点）
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByText("取消知识图谱生成？")).toBeVisible();
    await expect(page.getByRole("button", { name: "继续生成" })).toBeVisible();
    await expect(page.getByRole("button", { name: "取消任务（保留断点）" })).toBeVisible();
    await page.getByRole("button", { name: "取消并删除断点" }).click();
    await expect(page.getByRole("status")).toHaveCount(0, { timeout: TASK_TIMEOUT });

    await openDebug(page);
    await expectDebugText(page, "任务已取消，断点已删除");
    await closeDebug(page);
    await expect(page.getByText(/发现可恢复的生成断点/)).toHaveCount(0);
  });

  test("C-01 无文档时点生成 → 明确提示，不发起任务", async ({ page }) => {
    await page.route("**/api/documents", (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
    await page.reload();
    await goto(page, "知识图谱");

    await expect(page.getByRole("button", { name: "生成知识图谱（0 个文档）" })).toBeVisible();
    await waitKgPresets(page);  // 预设先加载完，页面才会走到「没有文档」这一条提示
    await page.getByRole("button", { name: "生成知识图谱（0 个文档）" }).click();
    await expect(page.getByText("请先在「文档输入」上传文档")).toBeVisible();
    await expect(page.getByRole("status")).toHaveCount(0);  // 没起任务，就没有任务卡片
  });

  test("C-02 无默认预设时点生成 → 明确提示，不发起任务", async ({ page }) => {
    await ensureCoreDoc(page.request);
    await page.route("**/api/presets", (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
    await page.reload();
    await goto(page, "知识图谱");

    await page.getByRole("button", { name: /生成知识图谱（\d+ 个文档）/ }).click();
    await expect(page.getByText(/请先在「模型配置」创建并设好默认 LLM \/ Embedding 预设/)).toBeVisible();
    await expect(page.getByRole("status")).toHaveCount(0);
  });

  test("C-10 + C-11 缩放上下限（25%–300%）与重置", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "知识图谱");
    await page.getByRole("button", { name: new RegExp(kg.name) }).click();
    await expect(page.getByText("图谱预览")).toBeVisible();
    await expect(page.locator("svg g[data-node-id]").first()).toBeVisible();

    await expect(page.getByText(/tick \d+ · 100%/)).toBeVisible();
    const zoomIn = page.getByTitle("放大");
    for (let i = 0; i < 10; i++) await zoomIn.click();
    await expect(page.getByText(/tick \d+ · 300%/)).toBeVisible();
    await zoomIn.click();  // 到上限后再点也不应超过 300%
    await expect(page.getByText(/tick \d+ · 300%/)).toBeVisible();

    const zoomOut = page.getByTitle("缩小");
    for (let i = 0; i < 12; i++) await zoomOut.click();
    await expect(page.getByText(/tick \d+ · 25%/)).toBeVisible();
    await zoomOut.click();
    await expect(page.getByText(/tick \d+ · 25%/)).toBeVisible();

    await page.getByTitle("重置缩放与位置").click();
    await expect(page.getByText(/tick \d+ · 100%/)).toBeVisible();
  });

  test("C-12a 点节点看详情（类型徽章 / 属性面板 / 完整内容 Tab）", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "知识图谱");
    await page.getByRole("button", { name: new RegExp(kg.name) }).first().click();
    await expect(page.getByText("图谱预览")).toBeVisible();

    const placeholder = page.getByText("点击节点查看属性");
    await expect(placeholder).toBeVisible();
    await page.locator("svg g[data-node-id]").first().click();
    await expect(placeholder).toHaveCount(0);
    await expect(page.getByRole("button", { name: "完整内容" })).toBeVisible();
    await expect(page.getByRole("button", { name: "缩略内容" })).toBeVisible();
  });

  test("C-12b 点空白取消选中（文档期望：收起详情、恢复节点亮度）", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "知识图谱");
    await page.getByRole("button", { name: new RegExp(kg.name) }).first().click();
    await expect(page.getByText("图谱预览")).toBeVisible();
    await page.locator("svg g[data-node-id]").first().click();
    const placeholder = page.getByText("点击节点查看属性");
    await expect(placeholder).toHaveCount(0);

    // 点空白区域。GraphView 的 onPointerUp 只在命中节点时更新 selected，没有清空分支
    await page.locator("svg").click({ position: { x: 30, y: 60 } });
    await expect(placeholder).toBeVisible();
  });

  test("C-12c 点图例区域不取消选中（图例不参与画布交互）", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "知识图谱");
    await page.getByRole("button", { name: new RegExp(kg.name) }).first().click();
    await expect(page.getByText("图谱预览")).toBeVisible();
    await page.locator("svg g[data-node-id]").first().click();
    const placeholder = page.getByText("点击节点查看属性");
    await expect(placeholder).toHaveCount(0);

    // 图例底板带 data-legend，pointerdown 命中后提前返回：既不清空选中也不启动拖拽。
    // 图例文字/色块那层是 pointer-events: none（只负责显示），故这里点底板而不是文字
    await page.locator("svg g[data-legend] rect").first().click();
    await expect(placeholder).toHaveCount(0);
  });

  test("C-14a 行内「生成测试集」跳到测试集页并预选该图谱", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "知识图谱");
    await kgRow(page, kg.name).getByRole("button", { name: "生成测试集" }).click();
    await expectPageTitle(page, "测试集");
    await expect(page.locator("select").first()).toHaveValue(kg.id);
  });

  test("C-14b 删除图谱：先取消不动，再确认后行消失", async ({ page, request }) => {
    const name = "E2E-待删除图谱";
    const kg = await ensureKg(request, name);
    await page.reload();
    await goto(page, "知识图谱");
    const row = kgRow(page, name);

    let dialogText = "";
    page.once("dialog", (d) => { dialogText = d.message(); void d.dismiss(); });
    await row.getByRole("button", { name: "删除", exact: true }).click();
    await expect(row).toBeVisible();
    expect(dialogText).toContain(`确定删除知识图谱「${name}」`);

    page.once("dialog", (d) => { void d.accept(); });
    await row.getByRole("button", { name: "删除", exact: true }).click();
    await expect(kgRow(page, name)).toHaveCount(0, { timeout: 15_000 });
    expect((await listKgs(request)).some((k) => k.id === kg.id)).toBe(false);
  });

  test("C-15 被数据源引用的图谱删除 → 409 二次确认弹层", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "知识图谱");

    await page.route(`**/api/kg/${kg.id}`, (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      return route.fulfill({
        status: 409,
        json: {
          detail: "知识图谱正被数据源投影引用，无法删除",
          error_code: "kg_in_use",
          // 附加明细统一在 extra 里（错误体形状：{detail, error_code, extra?}）
          extra: {
            sources: [{ source_id: "conn-e2e__ds-e2e", dataset_id: "ds-e2e", connection_name: "E2E 连接" }],
          },
        },
      });
    });

    const row = kgRow(page, kg.name);
    page.once("dialog", (d) => { void d.accept(); });
    await row.getByRole("button", { name: "删除", exact: true }).click();

    await expect(page.getByText("知识图谱仍是数据源的当前投影")).toBeVisible();
    await expect(page.getByText("E2E 连接/ds-e2e")).toBeVisible();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByText("知识图谱仍是数据源的当前投影")).toHaveCount(0);
  });

  test("C-16 RAGFlow 面板：高级选项开合（无连接时给出指引）", async ({ page }) => {
    await expect(page.getByText("从 RAGFlow 同步")).toBeVisible();
    await page.getByRole("button", { name: "高级：直接填知识库 ID" }).click();
    await expect(page.locator("label", { hasText: "知识库 ID" })).toBeVisible();
    await expect(page.locator("label", { hasText: "投影名称（可选）" })).toBeVisible();
    await page.getByRole("button", { name: "收起高级选项" }).click();
    await expect(page.locator("label", { hasText: "知识库 ID" })).toHaveCount(0);
  });
});
