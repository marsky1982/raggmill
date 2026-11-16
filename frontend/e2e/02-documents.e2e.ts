import { test, expect, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { deleteDoc, FIXTURES, listDocs } from "./support/seed";
import { card, expectPageTitle, goto } from "./support/ui";

/**
 * B 组：文档输入（B-01 ~ B-08）
 * 上传走真实的 multipart 接口，落在本次运行的临时 data 目录里。
 */

const DOCS_CARD = /已加载文档（/;

function docsCard(page: Page) {
  return card(page, DOCS_CARD);
}

function docRow(page: Page, name: string) {
  return docsCard(page).locator("div.px-5.py-3").filter({ hasText: name }).first();
}

async function clearDocs(page: Page) {
  for (const d of await listDocs(page.request)) await deleteDoc(page.request, d.id);
  await page.reload();
  await goto(page, "文档输入");
  await expectPageTitle(page, "文档输入");
  await expect(docsCard(page).getByText("暂无文档。")).toBeVisible();
}

async function uploadInput(page: Page, payload: { name: string; content: string; mimeType?: string }) {
  const input = page.locator('input[type="file"]').first();
  await input.setInputFiles({
    name: payload.name,
    mimeType: payload.mimeType ?? "text/markdown",
    buffer: Buffer.from(payload.content, "utf8"),
  });
}

test.describe("B 组 · 文档输入", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await goto(page, "文档输入");
    await expectPageTitle(page, "文档输入");
  });

  test("B-01 点击上传：文件进列表并给出 token 预估", async ({ page }) => {
    await clearDocs(page);
    await uploadInput(page, FIXTURES.mid);
    await expect(docRow(page, FIXTURES.mid.name)).toBeVisible();
    await expect(page.getByText("已上传 1 个文件")).toBeVisible();
    await expect(page.getByText(/平均 \d+ tokens\/文档/)).toBeVisible();
  });

  test("B-02 拖拽上传：drop 事件同样写入索引", async ({ page }) => {
    await clearDocs(page);
    const file = FIXTURES.tiny;
    const dataTransfer = await page.evaluateHandle(
      ({ name, content }: { name: string; content: string }) => {
        const dt = new DataTransfer();
        dt.items.add(new File([content], name, { type: "text/markdown" }));
        return dt;
      },
      { name: file.name, content: file.content },
    );
    await page.getByRole("button", { name: "点击或拖拽上传文档" }).dispatchEvent("drop", { dataTransfer });
    await expect(docRow(page, file.name)).toBeVisible();
  });

  test("B-03 键盘可达：Enter / 空格能唤起文件选择", async ({ page }) => {
    const zone = page.getByRole("button", { name: "点击或拖拽上传文档" });
    await zone.focus();
    const chooser = page.waitForEvent("filechooser");
    await page.keyboard.press("Enter");
    const fc = await chooser;
    await fc.setFiles({
      name: "e2e-keyboard.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(FIXTURES.tiny.content, "utf8"),
    });
    await expect(docRow(page, "e2e-keyboard.md")).toBeVisible();
  });

  test("B-04 不支持的格式：后端拒绝并上屏原因", async ({ page }) => {
    await clearDocs(page);
    await uploadInput(page, FIXTURES.unsupported);
    await expect(page.getByText(/不支持的文件类型/)).toBeVisible();
  });

  test("B-05 同名重复上传：提示已存在且不产生重复行", async ({ page }) => {
    await clearDocs(page);
    await uploadInput(page, FIXTURES.tiny);
    await expect(docRow(page, FIXTURES.tiny.name)).toBeVisible();
    await uploadInput(page, FIXTURES.tiny);
    await expect(page.getByText(/同名文档已存在/)).toBeVisible();
    await expect(docsCard(page).locator("div.px-5.py-3").filter({ hasText: FIXTURES.tiny.name })).toHaveCount(1);
  });

  test("B-06 文件夹：只读取支持格式，确认后写入索引", async ({ page }) => {
    await clearDocs(page);
    // webkitdirectory 只接受目录路径，先把三个文件落盘到临时目录
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "ragas-e2e-folder-"));
    fs.writeFileSync(path.join(folder, "a.md"), FIXTURES.tiny.content, "utf8");
    fs.writeFileSync(path.join(folder, "b.html"), "<h1>b</h1>", "utf8");
    fs.writeFileSync(path.join(folder, "c.pdf"), "%PDF-1.4 fake", "utf8");
    try {
      const folderInput = page.locator('input[webkitdirectory]');
      await folderInput.setInputFiles(folder);
      await expect(page.getByText("已选中 2 个支持格式的文件")).toBeVisible();
      await page.getByRole("button", { name: /确认读取（2 个文件）/ }).click();
      await expect(page.getByText("已从文件夹读取 2 个文件")).toBeVisible();
      await expect(docRow(page, "a.md")).toBeVisible();
      await expect(docRow(page, "b.html")).toBeVisible();
    } finally {
      fs.rmSync(folder, { recursive: true, force: true });
    }
  });

  test("B-07 token 三档徽章：<100 / 101–500 / >500", async ({ page }) => {
    await clearDocs(page);

    await uploadInput(page, FIXTURES.tiny);
    await expect(page.getByText("<100 tokens · 不满足 ragas 要求")).toBeVisible();
    await clearDocs(page);

    await uploadInput(page, FIXTURES.mid);
    await expect(page.getByText("101–500 tokens · 中等管线")).toBeVisible();
    await clearDocs(page);

    await uploadInput(page, FIXTURES.long);
    await expect(page.getByText(">500 tokens · 长文档管线")).toBeVisible();
  });

  test("B-08 移除文档：先弹确认，取消不动、确认后消失", async ({ page }) => {
    await clearDocs(page);
    await uploadInput(page, FIXTURES.tiny);
    await expect(docRow(page, FIXTURES.tiny.name)).toBeVisible();

    const dialogs: string[] = [];
    page.on("dialog", (d) => {
      dialogs.push(d.message());
      void d.dismiss();
    });
    await docRow(page, FIXTURES.tiny.name).getByRole("button", { name: "移除" }).click();
    await expect(docRow(page, FIXTURES.tiny.name)).toBeVisible();
    expect(dialogs[0]).toContain("确定从工作台移除");

    page.removeAllListeners("dialog");
    page.once("dialog", (d) => void d.accept());
    await docRow(page, FIXTURES.tiny.name).getByRole("button", { name: "移除" }).click();
    await expect(docRow(page, FIXTURES.tiny.name)).toHaveCount(0);
  });
});
