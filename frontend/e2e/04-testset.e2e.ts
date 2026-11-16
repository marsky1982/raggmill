import { test, expect, type Page } from "@playwright/test";
import { API } from "./support/state";
import { ensureDefaultPresets, ensureKg, ensureTestset, listTestsets } from "./support/seed";
import { expectPageTitle, field, goto } from "./support/ui";

const TASK_TIMEOUT = 45_000;

/** 详情页的筛选下拉（第一个 select 是知识图谱，筛选用的是 w-56 那个） */
function filterSelect(page: Page) {
  return page.locator("select.w-56").first();
}

/** 把该测试集的所有样本恢复为「未采纳」，让「仅看已采纳」的空态可判定 */
async function resetAdoptions(page: Page, id: string) {
  const detail = await (await page.request.get(`${API}/api/testset/${id}`)).json() as {
    groups: Record<string, { index: number; adopted?: boolean }[]>;
  };
  for (const samples of Object.values(detail.groups)) {
    for (const s of samples) {
      if (s.adopted) await page.request.patch(`${API}/api/testset/${id}/samples/${s.index}`, { data: { adopted: false } });
    }
  }
}

test.describe("D 组 · 测试集生成", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goto(page, "测试集");
    await expectPageTitle(page, "测试集");
  });

  test("D-01 成本预估展示下限/预期/最坏，改条数后清空", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "测试集");
    await page.locator("select").first().selectOption(kg.id);
    await page.getByRole("button", { name: "预估 token 消耗" }).click();
    const estimate = page.getByText(/下限 .* · 预期 .* · 最坏 /);
    await expect(estimate).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/次 LLM 调用/)).toBeVisible();

    await field(page, "测试集条数").fill("24");
    await expect(page.getByText(/下限 .* · 预期 .* · 最坏 /)).toHaveCount(0);
  });

  test("D-02 比例和 ≠ 1 时提示并可一键归一化", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "测试集");
    await page.locator("select").first().selectOption(kg.id);
    await page.getByLabel("使用默认分布（0.5 / 0.25 / 0.25）").uncheck();

    const sliders = page.locator('input[type="range"]');
    await expect(sliders).toHaveCount(3);
    await sliders.nth(0).fill("60");  // 0.6 + 0.25 + 0.25 = 1.1
    const normalize = page.getByRole("button", { name: "比例和 ≠ 1 · 点击归一化" });
    await expect(normalize).toBeVisible();
    await normalize.click();
    await expect(normalize).toHaveCount(0);
  });

  test("D-03 生成：SSE 进度 → 终态 → 自动打开详情并可导出", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await ensureDefaultPresets(request);
    await page.reload();
    await goto(page, "测试集");
    const before = (await listTestsets(request)).length;

    await page.locator("select").first().selectOption(kg.id);
    await page.getByLabel("使用默认分布（0.5 / 0.25 / 0.25）").uncheck();
    const sliders = page.locator('input[type="range"]');
    await sliders.nth(0).fill("0");   // demo KG 无 entities：单跳·具体必须置 0
    await sliders.nth(1).fill("50");
    await sliders.nth(2).fill("50");
    await field(page, "测试集条数").fill("5");

    await page.getByRole("button", { name: "生成测试集" }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    await expect(page.getByText(/阶段: /)).toBeVisible();

    // 终态：进度卡立即卸载，以「列表 +1 + 自动打开详情」判定
    await expect(page.getByText(/已生成测试集（/)).toContainText(
      `已生成测试集（${before + 1}）`, { timeout: TASK_TIMEOUT },
    );
    await expect(page.getByText(/共 \d+ 条/)).toBeVisible();
    await expect(page.getByRole("button", { name: "导出", exact: true })).toBeVisible();
    await expect(page.locator("div.grid > div").filter({ hasText: /#\d+/ }).first()).toBeVisible();
  });

  test("D-04 生成中取消：无二次确认，任务卡直接消失", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await ensureDefaultPresets(request);
    await page.reload();
    await goto(page, "测试集");

    await page.locator("select").first().selectOption(kg.id);
    await page.getByLabel("使用默认分布（0.5 / 0.25 / 0.25）").uncheck();
    const sliders = page.locator('input[type="range"]');
    await sliders.nth(0).fill("0");
    await sliders.nth(1).fill("50");
    await sliders.nth(2).fill("50");
    await field(page, "测试集条数").fill("20");

    let dialogSeen = false;
    page.on("dialog", (d) => { dialogSeen = true; void d.accept(); });
    await page.getByRole("button", { name: "生成测试集" }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByRole("status")).toHaveCount(0, { timeout: TASK_TIMEOUT });
    expect(dialogSeen, "测试集页取消按约定不应弹二次确认").toBe(false);
  });

  test("D-06 demo 图谱缺实体素材：单跳·具体不可用并禁用生成按钮", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "测试集");
    await page.locator("select").first().selectOption(kg.id);

    await expect(page.getByText(/不可用：缺少 实体标签（entities）/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("button", { name: "生成测试集" })).toBeDisabled();
    await expect(page.getByText("当前分布需要的题型缺素材，请调整比例或重建 KG")).toBeVisible();

    // 把单跳·具体的比例置 0 后，按钮恢复可用
    await page.getByLabel("使用默认分布（0.5 / 0.25 / 0.25）").uncheck();
    const sliders = page.locator('input[type="range"]');
    await sliders.nth(0).fill("0");
    await expect(page.getByRole("button", { name: "生成测试集" })).toBeEnabled();
  });

  test("D-07 三重筛选（类型 / 关键词 / 仅已采纳）与空态", async ({ page, request }) => {
    const ts = await ensureTestset(request);
    await resetAdoptions(page, ts.id);
    await page.reload();
    await goto(page, "测试集");
    await page.getByRole("button", { name: new RegExp(ts.name) }).click();

    const detail = await (await request.get(`${API}/api/testset/${ts.id}`)).json() as {
      groups: Record<string, unknown[]>;
    };
    const typeKeys = Object.keys(detail.groups);
    expect(typeKeys.length).toBeGreaterThan(0);

    await filterSelect(page).selectOption(typeKeys[0]);
    await expect(page.locator("div.grid > div").filter({ hasText: typeKeys[0] }).first()).toBeVisible();

    await page.getByPlaceholder("按关键词筛选 query…").fill("zzzz-no-match-any");
    await expect(page.getByText("无匹配样本。")).toBeVisible();

    await page.getByPlaceholder("按关键词筛选 query…").fill("");
    await filterSelect(page).selectOption("");
    await page.getByLabel("仅看已采纳").check();
    await expect(page.getByText("无匹配样本。")).toBeVisible();
  });

  test("D-08 采纳/丢弃切换与参考上下文折叠", async ({ page, request }) => {
    const ts = await ensureTestset(request);
    await resetAdoptions(page, ts.id);
    await page.reload();
    await goto(page, "测试集");
    await page.getByRole("button", { name: new RegExp(ts.name) }).click();

    const firstCard = page.locator("div.grid > div").filter({ hasText: /#\d+/ }).first();
    await firstCard.getByRole("button", { name: "丢弃" }).click();
    await expect(firstCard.getByRole("button", { name: /✓ 已采纳/ })).toBeVisible();
    await firstCard.getByRole("button", { name: /✓ 已采纳/ }).click();
    await expect(firstCard.getByRole("button", { name: "丢弃" })).toBeVisible();

    const summary = firstCard.locator("details summary");
    await expect(summary).toContainText(/参考上下文（\d+）/);
    await summary.click();
    await expect(firstCard.locator("details[open]")).toBeVisible();
  });

  test("D-09 质检报告弹层可打开、Esc 关闭", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.reload();
    await goto(page, "测试集");
    await page.locator("select").first().selectOption(kg.id);
    await page.getByRole("button", { name: "质检报告" }).click();
    await expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("质检报告").first()).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("D-10 导出 CSV / JSONL：触发下载且文件名后缀正确", async ({ page, request }) => {
    const ts = await ensureTestset(request);
    await page.reload();
    await goto(page, "测试集");
    await page.getByRole("button", { name: new RegExp(ts.name) }).click();

    for (const [menu, suffix] of [["CSV · 全部", ".csv"], ["JSONL · 全部", ".jsonl"]] as const) {
      await page.getByRole("button", { name: "导出", exact: true }).click();
      const download = page.waitForEvent("download");
      await page.getByRole("menuitem", { name: menu }).click();
      const file = await download;
      expect(file.suggestedFilename()).toMatch(new RegExp(`${suffix.replace(".", "\\.")}$`));
      await expect(page.getByText(/已导出 /)).toBeVisible();
    }
  });

  test("D-11 来源检查：自建图谱判为「测试集自身的图谱」", async ({ page, request }) => {
    const ts = await ensureTestset(request);
    await page.reload();
    await goto(page, "测试集");
    await page.getByRole("button", { name: new RegExp(ts.name) }).click();
    await expect(page.getByText("来源检查").first()).toBeVisible();
    await expect(page.getByText(/对照测试集自身的图谱/)).toBeVisible();
    await expect(page.getByText(/有效 \d+/)).toBeVisible();
  });

  test("D-12 局部补生成计划面板：无受影响样本时给出明确说明", async ({ page, request }) => {
    const ts = await ensureTestset(request);
    await page.reload();
    await goto(page, "测试集");
    await page.getByRole("button", { name: new RegExp(ts.name) }).click();
    await expect(page.getByText(/没有内容变化或来源被删除的样本，不需要补生成|可局部补生成 \d+ 条/)).toBeVisible();
  });
});
