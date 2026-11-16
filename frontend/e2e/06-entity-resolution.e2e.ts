import { test, expect, type Page } from "@playwright/test";
import { API } from "./support/state";
import { ensureDefaultPresets, listPresets } from "./support/seed";
import { card, expectPageTitle, goto } from "./support/ui";

/**
 * 实体消歧开关（RAGFlow 面板）的界面口径。
 *
 * 该开关要求「已选连接 + 已选知识库」，而 E2E 环境没有真实 RAGFlow 上游，
 * 因此这里用 page.route 打桩连接/库/设置三个接口——**只验证前端行为**：
 * 默认关、禁用原因、切换只写配置不触发探测或同步、生效提示文案。
 * 「开关打开后实体是否真的被合并、投影是否按新口径重建」必须对真实上游人工验证。
 */

const CONN = "conn-e2e";
const DS = "ds-e2e";

interface SettingsView {
  entity_resolution: string;
  llm_preset_id: string | null;
  config_fingerprint: string;
  applied_fingerprint: string | null;
  pending_rebuild: boolean;
}

const OFF_VIEW: SettingsView = {
  entity_resolution: "disabled",
  llm_preset_id: null,
  config_fingerprint: "fp-off",
  applied_fingerprint: "fp-off",
  pending_rebuild: false,
};

interface StubHooks {
  /** PUT 保存设置时收到的请求体 */
  putBodies: Record<string, unknown>[];
  /** 探测 / 同步被触发的次数（切换开关时应当保持 0） */
  sideEffects: string[];
  /** PUT 的返回值 */
  putView: SettingsView;
}

async function stubRagflow(page: Page, hooks: StubHooks): Promise<void> {
  await page.route(new RegExp(`/api/ragflow/connections$`), (route) =>
    route.fulfill({
      json: [{
        id: CONN, name: "E2E 连接", base_url: "http://127.0.0.1:9",
        dataset_id: null, legacy: false, api_key_mask: "ragflow-****",
        created_at: "2026-11-08T00:00:00Z", updated_at: "2026-11-08T00:00:00Z",
      }],
    }));

  await page.route(new RegExp(`/api/ragflow/connections/${CONN}/datasets\\?`), (route) =>
    route.fulfill({ json: { items: [{ id: DS, name: "E2E 库", doc_count: 3 }], total: 1 } }));

  await page.route(new RegExp(`/api/ragflow/connections/${CONN}/datasets/${DS}/settings$`), async (route) => {
    if (route.request().method() === "PUT") {
      hooks.putBodies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
      return route.fulfill({ json: hooks.putView });
    }
    return route.fulfill({ json: OFF_VIEW });
  });

  // 切换开关不应触发这两类请求；一旦触发就记下来，用于反向断言
  await page.route(new RegExp(`/api/ragflow/connections/${CONN}/datasets/${DS}/(probe|sync)$`), (route) => {
    hooks.sideEffects.push(new URL(route.request().url()).pathname);
    return route.fulfill({ json: { task_id: "task-should-not-be-created" } });
  });
}

function entityCard(page: Page) {
  return card(page, "从 RAGFlow 同步");
}

async function createTestsetPreset(): Promise<string> {
  const res = await fetch(`${API}/api/presets`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      kind: "llm_testset",
      name: "E2E 出题用 LLM",
      model: "e2e-llm-testset",
      base_url: "http://127.0.0.1:9/v1",
      api_key: "sk-e2e-dummy",
    }),
  });
  if (!res.ok) throw new Error(`创建 llm_testset 预设失败：${res.status} ${await res.text()}`);
  const body = (await res.json()) as { id?: string };
  if (!body.id) throw new Error("创建 llm_testset 预设未返回 id");
  return body.id;
}

test.describe("A 组补充 · RAGFlow 实体消歧开关", () => {
  test("A-14 默认关闭：无测试集生成用预设时禁用并给出原因", async ({ page, request }) => {
    await ensureDefaultPresets(request);
    // 环境里不应该有 llm_testset 预设；有则清掉，保证「无预设」这一分支可复现
    for (const p of await listPresets(request)) {
      if (p.kind === "llm_testset") {
        await request.delete(`${API}/api/presets/${p.id}`);
      }
    }

    const hooks: StubHooks = { putBodies: [], sideEffects: [], putView: OFF_VIEW };
    await stubRagflow(page, hooks);
    await page.goto("/");
    await goto(page, "知识图谱");
    await expectPageTitle(page, "知识图谱");

    const panel = entityCard(page);
    const checkbox = panel.getByRole("checkbox", { name: "同步时合并同义实体" });
    await expect(checkbox).toBeVisible();
    await expect(checkbox).not.toBeChecked();
    await expect(checkbox).toBeDisabled();
    await expect(panel.getByText("关闭", { exact: true })).toBeVisible();
    await expect(panel.getByText(/关闭：仅做字符串规范化，不调用 LLM（默认）/)).toBeVisible();
    await expect(panel.getByText("先到配置页添加测试集生成用 LLM 预设")).toBeVisible();
  });

  test("A-15 有预设时可开启：只写配置，不触发探测或同步，并提示下次同步生效", async ({ page, request }) => {
    await ensureDefaultPresets(request);
    const presetId = await createTestsetPreset();

    const hooks: StubHooks = {
      putBodies: [],
      sideEffects: [],
      putView: {
        entity_resolution: "hybrid",
        llm_preset_id: presetId,
        config_fingerprint: "fp-on",
        applied_fingerprint: "fp-off",
        pending_rebuild: true,
      },
    };
    await stubRagflow(page, hooks);

    try {
      await page.goto("/");
      await goto(page, "知识图谱");
      await expectPageTitle(page, "知识图谱");

      const panel = entityCard(page);
      const checkbox = panel.getByRole("checkbox", { name: "同步时合并同义实体" });
      await expect(checkbox).toBeEnabled();
      await expect(checkbox).not.toBeChecked();
      await expect(panel.getByText("关闭", { exact: true })).toBeVisible();

      // 受控复选框：点击后要等 PUT 返回才回勾，因此用 click 而不是 check（check 会立即校验勾选态）
      await checkbox.click();
      await expect(checkbox).toBeChecked();

      // 保存成功 → 徽章与提示切到「开」
      await expect(panel.getByText("LLM 混合", { exact: true })).toBeVisible();
      await expect(panel.getByText(/开启：规则优先，规则判断不了的再调用 LLM（消耗 token）/)).toBeVisible();
      // 已应用口径仍是 off → 提示下次同步会重建投影
      await expect(panel.getByText("下次同步会重建投影")).toBeVisible();
      await expect(panel.getByText("先到配置页添加测试集生成用 LLM 预设")).toHaveCount(0);

      // 只发了一次 PUT，且内容为「开 = hybrid + 预设 id」
      expect(hooks.putBodies).toHaveLength(1);
      expect(hooks.putBodies[0]).toMatchObject({ entity_resolution: "hybrid", llm_preset_id: presetId });
      // 切换开关绝不顺带发起探测或同步
      expect(hooks.sideEffects, `切换开关触发了 ${hooks.sideEffects.join(", ")}`).toHaveLength(0);
    } finally {
      await request.delete(`${API}/api/presets/${presetId}`);
    }
  });
});
