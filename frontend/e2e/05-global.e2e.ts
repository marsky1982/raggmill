import { test, expect } from "@playwright/test";
import { ensureCoreDoc, ensureDefaultPresets, ensureKg, ensureTestset, pollTask } from "./support/seed";
import { API } from "./support/state";
import { closeDebug, expectDebugText, expectPageTitle, goto, openDebug, waitKgPresets } from "./support/ui";

const PAGES = [
  ["模型配置", "模型配置"],
  ["文档输入", "文档输入"],
  ["知识图谱", "知识图谱"],
  ["测试集", "测试集生成"],
  ["质量评估", "五指标质量评估"],
] as const;

test.describe("E 组 · 全局", () => {
  test("E-01/E-02 主题切换立即生效，刷新后保持", async ({ page }) => {
    await page.goto("/");
    const html = page.locator("html");
    await expect(html).not.toHaveClass(/dark/);

    await page.getByTitle("切换到深色主题").click();
    await expect(html).toHaveClass(/dark/);
    expect(await page.evaluate(() => document.documentElement.style.colorScheme)).toBe("dark");
    expect(await page.evaluate(() => localStorage.getItem("ragas-theme"))).toBe("dark");

    await page.reload();
    await expect(html).toHaveClass(/dark/);
    await expect(page.getByTitle("切换到浅色主题")).toBeVisible();

    // 复位成浅色，避免影响后续用例的截图判定
    await page.getByTitle("切换到浅色主题").click();
    await expect(html).not.toHaveClass(/dark/);
    expect(await page.evaluate(() => localStorage.getItem("ragas-theme"))).toBe("light");
  });

  test("E-03 调试抽屉：开合、清空、复制全部", async ({ page }) => {
    await page.goto("/");
    // 先在界面上制造一条错误日志（空表单点保存 → pushError）
    await page.getByRole("button", { name: "新增配置" }).click();
    await openDebug(page);
    await expectDebugText(page, "名称与模型名为必填");

    await page.getByRole("button", { name: "复制全部" }).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied.length).toBeGreaterThan(0);
    expect(copied).toContain("名称与模型名为必填");

    await page.getByRole("button", { name: "清空" }).click();
    await expectDebugText(page, "无日志。");
    await closeDebug(page);
  });

  test("E-04 五步侧栏导航 + 演示模式徽章 + health= demo", async ({ page, request }) => {
    await page.goto("/");
    for (const [label, title] of PAGES) {
      await goto(page, label);
      await expectPageTitle(page, title);
    }
    await expect(page.getByText("演示模式（模拟数据）")).toBeVisible();
    const health = await (await request.get(`${API}/api/health`)).json() as { mode: string };
    expect(health.mode).toBe("demo");
  });

  test("E-05 离开页面再刷新：进行中的任务能恢复", async ({ page, request }) => {
    await ensureCoreDoc(request);
    await ensureDefaultPresets(request);
    await page.goto("/");
    await goto(page, "知识图谱");
    await expectPageTitle(page, "知识图谱");
    await waitKgPresets(page);

    await page.getByRole("button", { name: /生成知识图谱（\d+ 个文档）/ }).click();
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });

    // 切走再刷新，回到图谱页时应按后端 /api/tasks 恢复同一个任务
    await goto(page, "测试集");
    await expectPageTitle(page, "测试集");
    await page.reload();
    await goto(page, "知识图谱");
    await expect(page.getByRole("status")).toHaveText("任务执行中", { timeout: 15_000 });
    // 等它跑完，避免任务残留影响后续用例
    const tasks = await (await request.get(`${API}/api/tasks`)).json() as { task_id: string; status: string }[];
    for (const t of tasks) {
      if (!["succeeded", "failed", "canceled"].includes(t.status)) await pollTask(request, t.task_id);
    }
  });

  test("F-01 键盘 Tab 从侧栏开始，焦点环可见", async ({ page }) => {
    await page.goto("/");
    // 从侧栏顶部（品牌区，不可聚焦）开始按 Tab，顺序焦点应从第 1 个导航按钮开始
    await page.mouse.click(100, 100);
    await page.keyboard.press("Tab");

    const firstInNav = await page.evaluate(() => !!document.activeElement?.closest("nav"));
    expect(firstInNav).toBe(true);
    const ring = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      return el ? getComputedStyle(el).boxShadow : "";
    });
    expect(ring, "键盘聚焦时应有可见焦点环（box-shadow）").not.toBe("none");

    // 连续 Tab 能进入主区域的可交互控件，说明没有把主内容挡在 tab 序列外
    for (let i = 0; i < 12; i++) await page.keyboard.press("Tab");
    const inMain = await page.evaluate(() => !!document.activeElement?.closest("main"));
    expect(inMain).toBe(true);
  });

  test("F-03 导出下拉可用键盘打开、Esc 关闭", async ({ page, request }) => {
    const ts = await ensureTestset(request);
    await page.goto("/");
    await goto(page, "测试集");
    await page.getByRole("button", { name: new RegExp(ts.name) }).click();

    const exportBtn = page.getByRole("button", { name: "导出", exact: true });
    await exportBtn.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("menuitem", { name: "CSV · 全部" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menuitem", { name: "CSV · 全部" })).toHaveCount(0);
  });

  test("F-04 质检弹层：Esc 关闭且 Tab 不跑出弹层（焦点陷阱）", async ({ page, request }) => {
    const kg = await ensureKg(request);
    await page.goto("/");
    await goto(page, "测试集");
    await page.locator("select").first().selectOption(kg.id);
    await page.getByRole("button", { name: "质检报告" }).click();
    await expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 });

    for (let i = 0; i < 6; i++) await page.keyboard.press("Tab");
    const inside = await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'));
    expect(inside, "Tab 应被限制在弹层内").toBe(true);

    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
});
