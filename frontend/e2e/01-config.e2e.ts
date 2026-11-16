import { test, expect, type Page } from "@playwright/test";
import { API } from "./support/state";
import {
  card,
  clearDebugLogs,
  expectDebugText,
  expectPageTitle,
  fieldIn,
} from "./support/ui";

/**
 * A 组：模型配置（对应 设计文档 的 A-01 ~ A-13）
 * 所有断言都在 demo 后端 + 临时 data 目录里做，不连真实服务、不写仓库 data/。
 */

const PRESET_FORM = "Endpoint (base_url)";
const LIST_CARD = /已保存预设（/;

function form(page: Page) {
  return card(page, PRESET_FORM);
}

function list(page: Page) {
  return card(page, LIST_CARD);
}

function row(page: Page, name: string) {
  return list(page).locator("div.px-5.py-3").filter({ hasText: name }).first();
}

async function seedPreset(
  page: Page,
  data: { name: string; kind?: string; api_key?: string; base_url?: string; model?: string },
) {
  const res = await page.request.post(`${API}/api/presets`, {
    data: {
      kind: data.kind ?? "llm",
      name: data.name,
      model: data.model ?? "e2e-llm-model",
      base_url: data.base_url ?? "http://127.0.0.1:9/v1",
      api_key: data.api_key ?? "sk-e2e-seed-secret",
      extra: { temperature: 0.2 },
    },
  });
  expect(res.ok(), `seed 预设 ${data.name} 失败：${res.status()}`).toBeTruthy();
}

test.describe("A 组 · 模型配置", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await expectPageTitle(page, "模型配置");
  });

  test("A-01 新建 LLM 预设：保存成功、列表可见、明文密钥不上屏", async ({ page }) => {
    const f = form(page);
    await fieldIn(f, "预设名称 *").fill("E2E-LLM-主");
    await fieldIn(f, "Model *").fill("e2e-llm-model");
    await fieldIn(f, "Endpoint (base_url)").fill("http://127.0.0.1:9/v1");
    await fieldIn(f, "API Key").fill("sk-e2e-plaintext-1234");
    await fieldIn(f, "temperature").fill("0.2");
    await f.getByRole("button", { name: "新增配置" }).click();

    await expect(f.getByText("预设已保存（密钥已加密落盘）")).toBeVisible();
    await expect(row(page, "E2E-LLM-主")).toBeVisible();
    // 列表只显示掩码，不能把明文密钥渲染出来
    await expect(list(page)).not.toContainText("sk-e2e-plaintext-1234");
  });

  test("A-02 必填校验：名称/模型为空时保存被拦，原因进调试抽屉", async ({ page }) => {
    await clearDebugLogs(page);
    await form(page).getByRole("button", { name: "新增配置" }).click();
    await expectDebugText(page, "名称与模型名为必填");
  });

  test("A-03 同名拦截：同 tab 已存在同名预设时保存被拦", async ({ page }) => {
    await seedPreset(page, { name: "E2E-重复源" });
    await page.reload();
    await expectPageTitle(page, "模型配置");
    await clearDebugLogs(page);

    const f = form(page);
    await fieldIn(f, "预设名称 *").fill("E2E-重复源");
    await fieldIn(f, "Model *").fill("e2e-llm-model");
    await f.getByRole("button", { name: "新增配置" }).click();
    await expectDebugText(page, "已存在同名预设「E2E-重复源」");
  });

  test("A-04 编辑回填 + 留空 Key 不覆盖", async ({ page }) => {
    await seedPreset(page, { name: "E2E-留空密钥", api_key: "sk-e2e-original-9999" });
    await page.reload();
    await expectPageTitle(page, "模型配置");

    const maskBefore = await row(page, "E2E-留空密钥").locator(".truncate").innerText();
    await row(page, "E2E-留空密钥").getByRole("button", { name: "编辑", exact: true }).click();

    const f = form(page);
    await expect(fieldIn(f, "预设名称 *")).toHaveValue("E2E-留空密钥");
    await expect(fieldIn(f, "Model *")).toHaveValue("e2e-llm-model");
    await expect(fieldIn(f, "API Key")).toHaveAttribute("placeholder", /留空则不变/);
    await expect(fieldIn(f, "API Key")).toHaveValue("");

    await f.getByRole("button", { name: "保存修改" }).click();
    await expect(f.getByText("预设已更新")).toBeVisible();
    // 密钥未变：掩码那行应保持原样
    await expect(row(page, "E2E-留空密钥").locator(".truncate")).toHaveText(maskBefore);
  });

  test("A-05 设为默认：徽章与「已默认」按钮状态跟着迁移", async ({ page }) => {
    await seedPreset(page, { name: "E2E-待设默认" });
    await page.reload();
    await expectPageTitle(page, "模型配置");

    const target = row(page, "E2E-待设默认");
    await expect(target.getByRole("button", { name: "设为默认" })).toBeEnabled();
    await target.getByRole("button", { name: "设为默认" }).click();

    await expect(target.getByText("默认 · 当前生效")).toBeVisible();
    await expect(target.getByRole("button", { name: "已默认" })).toBeDisabled();
    // 之前那条默认预设想必不再带徽章（至少不会同时有两条「默认 · 当前生效」）
    await expect(list(page).getByText("默认 · 当前生效")).toHaveCount(1);
  });

  test("A-06 删除预设：无需二次确认；编辑中的行禁删", async ({ page }) => {
    await seedPreset(page, { name: "E2E-待删除" });
    await page.reload();
    await expectPageTitle(page, "模型配置");

    let dialogFired = false;
    page.on("dialog", (d) => {
      dialogFired = true;
      void d.accept();
    });

    const target = row(page, "E2E-待删除");
    await target.getByRole("button", { name: "编辑", exact: true }).click();
    await expect(target.getByRole("button", { name: "删除" })).toBeDisabled();
    await form(page).getByRole("button", { name: "取消编辑" }).click();

    await target.getByRole("button", { name: "删除" }).click();
    await expect(row(page, "E2E-待删除")).toHaveCount(0);
    expect(dialogFired, "删除预设不该弹二次确认").toBe(false);
  });

  test("A-07 按住显示明文（鼠标）", async ({ page }) => {
    const f = form(page);
    const key = fieldIn(f, "API Key");
    await key.fill("sk-e2e-hold-mouse");
    const eye = f.getByRole("button", { name: "按住显示 API Key 明文" });
    await expect(key).toHaveAttribute("type", "password");

    const box = await eye.boundingBox();
    if (!box) throw new Error("眼睛按钮不可见");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await expect(key).toHaveAttribute("type", "text");
    await page.mouse.up();
    await expect(key).toHaveAttribute("type", "password");
  });

  test("A-08 按住显示明文（键盘 Space）", async ({ page }) => {
    const f = form(page);
    const key = fieldIn(f, "API Key");
    await key.fill("sk-e2e-hold-keyboard");
    const eye = f.getByRole("button", { name: "按住显示 API Key 明文" });
    await eye.focus();
    await page.keyboard.down(" ");
    await expect(key).toHaveAttribute("type", "text");
    await page.keyboard.up(" ");
    await expect(key).toHaveAttribute("type", "password");
  });

  test("A-09 API Key 栏填 URL：内联红字 + 保存被拦且原因进抽屉", async ({ page }) => {
    await clearDebugLogs(page);
    const f = form(page);
    await fieldIn(f, "预设名称 *").fill("E2E-URL-Key");
    await fieldIn(f, "Model *").fill("e2e-llm-model");
    await fieldIn(f, "API Key").fill("http://dashscope.example/v1");
    await expect(f.getByText(/看起来你把 URL 填进了 API Key 栏/)).toBeVisible();

    await f.getByRole("button", { name: "新增配置" }).click();
    await expectDebugText(page, "API Key 栏填的是 URL");
  });

  test("A-11 连通自检失败：不可达地址给出失败结论", async ({ page }) => {
    await seedPreset(page, { name: "E2E-自检失败", base_url: "http://127.0.0.1:9/v1" });
    await page.reload();
    await expectPageTitle(page, "模型配置");

    const target = row(page, "E2E-自检失败");
    await target.getByRole("button", { name: "连通自检" }).click();
    await expect(target.getByText(/连通失败/)).toBeVisible({ timeout: 20_000 });
  });

  test("A-12 Embedding tab 无 temperature；切 tab 清空表单", async ({ page }) => {
    const f = form(page);
    await fieldIn(f, "预设名称 *").fill("切换前的临时名称");

    await page.getByRole("button", { name: "Embedding（向量化）" }).click();
    await expect(form(page).getByText("Endpoint (base_url)")).toBeVisible();
    await expect(form(page).locator('xpath=.//label[normalize-space(.)="temperature"]')).toHaveCount(0);
    await expect(fieldIn(form(page), "预设名称 *")).toHaveValue("");

    await page.getByRole("button", { name: "LLM（知识图谱）" }).click();
    await expect(fieldIn(form(page), "预设名称 *")).toHaveValue("");
  });

  test("A-13 RAGFlow 连接：必填校验、改地址必须重填 Key、删除有确认", async ({ page }) => {
    const connCard = card(page, "连接名称 *");
    // 分支一：只填名称 → 缺地址与 Key
    await fieldIn(connCard, "连接名称 *").fill("E2E 连接");
    await connCard.getByRole("button", { name: "新增连接" }).click();
    await expect(connCard.getByText("新增连接必须填写名称、服务地址和 API Key")).toBeVisible();

    // 分支二：补齐后创建成功
    await fieldIn(connCard, "服务地址 *").fill("http://127.0.0.1:18080");
    await fieldIn(connCard, "API Key *").fill("ragflow-e2e-key");
    await connCard.getByRole("button", { name: "新增连接" }).click();
    await expect(connCard.getByText("连接已保存（密钥已加密落盘）")).toBeVisible();
    const connRow = card(page, /已保存连接（/).locator("div.px-5.py-3").filter({ hasText: "E2E 连接" }).first();
    await expect(connRow).toBeVisible();

    // 分支三：改地址但不重填 Key → 前端先拦
    await connRow.getByRole("button", { name: "编辑", exact: true }).click();
    await fieldIn(connCard, "服务地址 *").fill("http://127.0.0.1:9380");
    await connCard.getByRole("button", { name: "保存修改" }).click();
    await expect(connCard.getByText("服务地址已变更，请重新填写 API Key")).toBeVisible();

    // 收尾：删除连接（确认框要出现并被接受）
    page.once("dialog", (d) => void d.accept());
    await connRow.getByRole("button", { name: "删除" }).click();
    await expect(card(page, /已保存连接（/).locator("div.px-5.py-3").filter({ hasText: "E2E 连接" })).toHaveCount(0);
  });
});
