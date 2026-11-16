import type { APIRequestContext } from "@playwright/test";
import { API } from "./state";

export interface Preset {
  id: string;
  kind: "llm" | "llm_testset" | "embedding";
  name: string;
  model: string;
  base_url?: string;
  is_default?: boolean;
  api_key_mask?: string;
}

export interface DocItem {
  id: string;
  name: string;
  size?: number;
  est_tokens: number;
}

export interface KgItem {
  id: string;
  name: string;
  node_count?: number;
  relationship_count?: number;
  mode?: string;
}

/** 造一段长度可控的英文正文（1 token ≈ 4 字符），用于命中 token 分档。 */
export function prose(topic: string, chars: number): string {
  const seed = `${topic} covers pricing, support windows, escalation paths, data retention and upload limits. `;
  let out = `# ${topic}\n\n`;
  while (out.length < chars) out += seed;
  return out.slice(0, chars) + "\n";
}

export const FIXTURES = {
  /** <100 tokens（约 50） */
  tiny: { name: "e2e-tiny.md", content: prose("E2E Tiny Note", 200) },
  /** 101–500 tokens（约 250） */
  mid: { name: "e2e-mid.md", content: prose("E2E Mid Handbook", 1000) },
  /** >500 tokens（约 1250） */
  long: { name: "e2e-long.md", content: prose("E2E Long Manual", 5000) },
  /** 后端不支持的格式 */
  unsupported: { name: "e2e-unsupported.pdf", content: "%PDF-1.4 e2e fake\n" },
};

export async function listPresets(req: APIRequestContext): Promise<Preset[]> {
  const res = await req.get(`${API}/api/presets`);
  return (await res.json()) as Preset[];
}

/** 保证 llm / embedding 各有一条默认预设（缺就建，建完没默认就调 set-default）。 */
export async function ensureDefaultPresets(req: APIRequestContext): Promise<void> {
  for (const kind of ["llm", "embedding"] as const) {
    let list = await listPresets(req);
    let ofKind = list.filter((p) => p.kind === kind);
    if (!ofKind.some((p) => p.is_default)) {
      if (!ofKind.length) {
        await req.post(`${API}/api/presets`, {
          data: {
            kind,
            name: kind === "llm" ? "E2E 默认 LLM" : "E2E 默认向量",
            model: kind === "llm" ? "e2e-llm" : "e2e-embedding",
            base_url: "http://127.0.0.1:9/v1",
            api_key: "sk-e2e-dummy",
            extra: kind === "llm" ? { temperature: 0.2 } : {},
          },
        });
        list = await listPresets(req);
        ofKind = list.filter((p) => p.kind === kind);
      }
      const target = ofKind.find((p) => p.is_default) ?? ofKind[0];
      if (target && !target.is_default) {
        await req.post(`${API}/api/presets/${target.id}/set-default?kind=${kind}`);
      }
    }
  }
  // 测试集表单优先用 llm_testset（可缺省），这里不强制创建
}

export async function listDocs(req: APIRequestContext): Promise<DocItem[]> {
  const res = await req.get(`${API}/api/documents`);
  return (await res.json()) as DocItem[];
}

export async function uploadDoc(req: APIRequestContext, name: string, content: string): Promise<void> {
  const res = await req.post(`${API}/api/documents/upload`, {
    multipart: { files: { name, mimeType: "text/markdown", buffer: Buffer.from(content, "utf8") } },
  });
  if (!res.ok()) throw new Error(`seed 上传 ${name} 失败：${res.status()} ${await res.text()}`);
}

export async function deleteDoc(req: APIRequestContext, id: string): Promise<void> {
  await req.delete(`${API}/api/documents/${id}`);
}

/** 保证至少一篇普通文档存在（供建图用）。 */
export async function ensureCoreDoc(req: APIRequestContext): Promise<DocItem> {
  const docs = await listDocs(req);
  if (docs.length) return docs[0];
  await uploadDoc(req, "e2e-core.md", FIXTURES.mid.content);
  const after = await listDocs(req);
  if (!after.length) throw new Error("seed：文档上传后列表仍为空");
  return after[0];
}

export async function listKgs(req: APIRequestContext): Promise<KgItem[]> {
  const res = await req.get(`${API}/api/kg`);
  return (await res.json()) as KgItem[];
}

export async function pollTask(req: APIRequestContext, taskId: string, timeoutMs = 60_000): Promise<void> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const res = await req.get(`${API}/api/tasks/${taskId}`);
    const body = (await res.json()) as { status: string; error?: string };
    if (["succeeded", "failed", "canceled"].includes(body.status)) {
      if (body.status !== "succeeded") throw new Error(`seed 任务 ${taskId} 终态 ${body.status}：${body.error ?? ""}`);
      return;
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`seed 任务 ${taskId} 超时未结束`);
}

/** 通过 API 建一张 demo 图谱（约 5s），返回 kg 条目。 */
export async function createKg(req: APIRequestContext, name: string): Promise<KgItem> {
  await ensureDefaultPresets(req);
  if (!(await listDocs(req)).length) await ensureCoreDoc(req);
  const docs = await listDocs(req);
  if (!docs.length) throw new Error("seed：建图前没有文档");
  const presets = await listPresets(req);
  const llm = presets.find((p) => p.kind === "llm" && p.is_default) ?? presets.find((p) => p.kind === "llm");
  const emb = presets.find((p) => p.kind === "embedding" && p.is_default) ?? presets.find((p) => p.kind === "embedding");
  if (!llm || !emb) throw new Error("seed：缺少 llm / embedding 预设");

  const res = await req.post(`${API}/api/kg/generate`, {
    data: { document_ids: docs.map((d) => d.id), llm_preset_id: llm.id, embedding_preset_id: emb.id, name },
  });
  if (!res.ok()) throw new Error(`seed 建图失败：${res.status()} ${await res.text()}`);
  const { task_id } = (await res.json()) as { task_id: string };
  await pollTask(req, task_id);
  const kgs = await listKgs(req);
  const created = kgs.find((k) => k.name === name);
  if (!created) throw new Error(`seed：建图完成但列表里找不到「${name}」`);
  return created;
}

/** 复用同名图谱，没有就建一张。 */
export async function ensureKg(req: APIRequestContext, name = "E2E-基线图谱"): Promise<KgItem> {
  const kgs = await listKgs(req);
  const hit = kgs.find((k) => k.name === name);
  if (hit) return hit;
  return createKg(req, name);
}

export async function deleteKg(req: APIRequestContext, id: string): Promise<void> {
  await req.delete(`${API}/api/kg/${id}`);
}

export interface TestsetItem { id: string; name: string; count: number }

export async function listTestsets(req: APIRequestContext): Promise<TestsetItem[]> {
  const res = await req.get(`${API}/api/testset`);
  return (await res.json()) as TestsetItem[];
}

/** 通过 API 生成一份 demo 测试集（约 3s）。demo KG 的分块没有 entities，
 *  单跳·具体不可用，所以比例里必须把它置 0，否则后端预检直接拒绝。 */
export async function ensureTestset(req: APIRequestContext, name = "E2E-基线测试集"): Promise<TestsetItem> {
  const hit = (await listTestsets(req)).find((t) => t.name === name);
  if (hit) return hit;
  const kg = await ensureKg(req);
  await ensureDefaultPresets(req);
  const presets = await listPresets(req);
  const llm = presets.find((p) => p.kind === "llm_testset" && p.is_default)
    ?? presets.find((p) => p.kind === "llm" && p.is_default)
    ?? presets.find((p) => p.kind === "llm");
  const emb = presets.find((p) => p.kind === "embedding" && p.is_default)
    ?? presets.find((p) => p.kind === "embedding");
  if (!llm || !emb) throw new Error("seed：缺少 llm / embedding 预设");

  const res = await req.post(`${API}/api/testset/generate`, {
    data: {
      kg_id: kg.id, llm_preset_id: llm.id, embedding_preset_id: emb.id,
      testset_size: 6, use_default_distribution: false,
      query_distribution: { single_hop_specific: 0, multi_hop_abstract: 0.5, multi_hop_specific: 0.5 },
      name,
    },
  });
  if (!res.ok()) throw new Error(`seed 生成测试集失败：${res.status()} ${await res.text()}`);
  const { task_id } = (await res.json()) as { task_id: string };
  await pollTask(req, task_id);
  const created = (await listTestsets(req)).find((t) => t.name === name);
  if (!created) throw new Error(`seed：生成完成但列表里找不到「${name}」`);
  return created;
}
