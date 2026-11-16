import { expect, type Page } from "@playwright/test";

const NAV_INDEX = { 模型配置: 0, 文档输入: 1, 知识图谱: 2, 测试集: 3, 质量评估: 4 } as const;

/** 点侧栏导航（按钮文字里同时有编号/标题/描述，按 STEPS 顺序取第 n 个最稳）。 */
export async function goto(page: Page, label: keyof typeof NAV_INDEX) {
  await page.locator("aside nav button").nth(NAV_INDEX[label]).click();
}

/** 表单字段：按 <Label> 文字取它后面的第一个 input（Label 没有 htmlFor，只能按结构定位）。 */
export function field(page: Page, label: string) {
  return page.locator(`xpath=//label[normalize-space(.)="${label}"]/following::input[1]`);
}

/** 卡片类容器：ui/card 统一带 data-slot="card"，按卡片内文字定位最稳。 */
export function card(page: Page, text: string | RegExp) {
  return page.locator('div[data-slot="card"]').filter({ hasText: text });
}

/** 卡片内的表单字段。 */
export function fieldIn(root: ReturnType<Page["locator"]>, label: string) {
  return root.locator(`xpath=.//label[normalize-space(.)="${label}"]/following::input[1]`);
}

/** 表单字段之后的第一个 select（同上策略）。 */
export function selectAfter(page: Page, label: string) {
  return page.locator(`xpath=//label[normalize-space(.)="${label}"]/following::select[1]`);
}

export function debugPanel(page: Page) {
  return page.locator('div.fixed[class*="shadow-2xl"]');
}

/** 展开侧栏底部调试抽屉并返回面板。 */
export async function openDebug(page: Page) {
  if (await page.locator('aside button[title="收起调试抽屉"]').isVisible().catch(() => false)) return debugPanel(page);
  await page.locator('aside button[title="打开调试抽屉"]').click();
  const panel = debugPanel(page);
  await expect(panel).toBeVisible();
  return panel;
}

export async function closeDebug(page: Page) {
  const panel = debugPanel(page);
  if (await panel.isVisible().catch(() => false)) {
    await panel.getByRole("button", { name: "收起" }).click();
    await expect(panel).toBeHidden();
  }
}

/** 调试抽屉里是否已有某条日志（先展开，断言后保持展开，便于失败时看截图）。 */
export async function expectDebugText(page: Page, text: string | RegExp) {
  const panel = await openDebug(page);
  await expect(panel.getByText(text).first()).toBeVisible();
}

/** 清空错误日志，保证后续断言只看本次产生的记录。 */
export async function clearDebugLogs(page: Page) {
  const panel = await openDebug(page);
  await panel.getByRole("button", { name: "清空" }).click();
  await expect(panel.getByText("无日志。")).toBeVisible();
  await closeDebug(page);
}

/** 页面切换后等首屏渲染完成（各页 h1 文案不同）。 */
export async function expectPageTitle(page: Page, title: string) {
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
}

/**
 * KG 页点「生成知识图谱」前先等默认预设加载完。
 * 预设未返回时页面上是「出题模型：加载中…」，没有默认预设时是「出题模型：未配置」，
 * 两种情况下点击都会被「请先在模型配置…」拦下，后面的断言（任务卡片 / 文档提示）就永远等不到。
 * 定位走 data-testid（不绑定文案），只判它不再是加载中 / 未配置——默认预设的名字在不同用例里不一定相同。
 */
export async function waitKgPresets(page: Page) {
  const badge = page.getByTestId("kg-llm-preset");
  await expect(badge).toBeVisible();
  await expect(badge).not.toHaveText(/加载中…|未配置/);
}
