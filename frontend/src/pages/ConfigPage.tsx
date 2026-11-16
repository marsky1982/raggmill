import { useCallback, useEffect, useRef, useState } from "react";
import { del, get, patch, post, put } from "../api";
import { pushError, pushInfo } from "../lib/errorLog";
import { normBaseUrl, ragflowVersionText } from "../lib/ragflowText";
import type { Preset, RagflowConnection, RagflowStage } from "../types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader } from "@/components/ui/card";

interface TestResult { ok: boolean; message: string; latency_ms?: number; version?: string | null }

/** 连接测试 verdict → 中文短标签。
 *  `/test` 只判「连接能否使用」（读首个知识库，不看图谱），ready 不代表能出题——
 *  能否出题要在知识图谱页对具体知识库「检查图谱」。 */
const VERDICT_LABEL: Record<string, string> = {
  ready: "连接可用",
  partial: "部分可用",
  graph_empty: "图谱为空",
  auth_failed: "鉴权失败",
  incompatible: "接口不兼容",
  unreachable: "连不上",
};

/** 推理强度选项：值与后端 storage.REASONING_EFFORTS 一致（backend/tests/test_route_smoke.py 守护）。空 = 模型默认 */
const REASONING_EFFORT_OPTIONS = ["none", "minimal", "low", "medium", "high"] as const;

/** DashScope 兼容层主机：与后端 ragas_adapter/clients.is_dashscope_endpoint 同口径。
 *  只有这些端点认 `enable_thinking`，其它服务商收到会直接 400。 */
const DASHSCOPE_HOSTS = ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"];

function isDashscopeEndpoint(url: string): boolean {
  const raw = url.trim();
  if (!raw) return false;
  let host = "";
  try {
    host = new URL(raw.includes("://") ? raw : `https://${raw}`).hostname.toLowerCase();
  } catch {
    return false;
  }
  return DASHSCOPE_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
}

export default function ConfigPage({ onNext }: { onNext: () => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [kind, setKind] = useState<"llm" | "llm_testset" | "embedding">("llm");
  const [form, setForm] = useState({ name: "", base_url: "", model: "", api_key: "", temperature: "", reasoning_effort: "", enable_thinking: "" });
  /** 编辑中预设原有的 extra：保存时在它的基础上改，不丢表单之外的配置项（如 streaming） */
  const [editingExtra, setEditingExtra] = useState<Record<string, unknown>>({});
  const [saveOk, setSaveOk] = useState<string | null>(null);  // 仅显示成功提示
  /** 提交拦截原因：渲染到对应输入框下方（调试抽屉默认收起，只记日志等于「点了没反应」） */
  const [formErr, setFormErr] = useState<{ name?: string; model?: string; duplicate?: string } | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingMask, setEditingMask] = useState<string>("");
  const [showKey, setShowKey] = useState(false);
  const formTopRef = useRef<HTMLDivElement>(null);
  // ---- RAGFlow 连接----
  const [conns, setConns] = useState<RagflowConnection[]>([]);
  const [connForm, setConnForm] = useState({ name: "", base_url: "", api_key: "" });
  const [connEditingId, setConnEditingId] = useState<string | null>(null);
  const [connEditing, setConnEditing] = useState<RagflowConnection | null>(null);
  const [connTesting, setConnTesting] = useState<string | null>(null);
  const [connDeleting, setConnDeleting] = useState<string | null>(null);
  const [connResults, setConnResults] = useState<Record<string, TestResult>>({});
  const [connSaveOk, setConnSaveOk] = useState<string | null>(null);
  const [connErr, setConnErr] = useState<string | null>(null);
  const [showConnKey, setShowConnKey] = useState(false);

  const loadConns = useCallback(() => get<RagflowConnection[]>("/api/ragflow/connections").then(setConns), []);
  useEffect(() => {
    loadConns().catch((e) => pushError("模型配置·RAGFlow 连接", "读取连接列表失败", String(e)));
  }, [loadConns]);

  const load = useCallback(() => get<Preset[]>("/api/presets").then((ps) => {
    setPresets(ps);
  }), []);
  useEffect(() => { load().catch((e) => pushError("模型配置·加载预设", String(e))); }, [load]);

  // 非编辑态不再回填默认预设。
  // 原逻辑回填默认预设但 editingId 为空，用户改完 Model 点保存会走 POST，静默造出一条
  // 同名同 kind 副本。现在改为「空表单 + 显式新增模式」，改默认预设必须点列表里的「编辑」。
  useEffect(() => {
    if (editingId) return;  // 编辑中不打断
    setForm({ name: "", base_url: "", model: "", api_key: "", temperature: "", reasoning_effort: "", enable_thinking: "" });
    setEditingExtra({});
  }, [kind, editingId]);

  const list = presets.filter((p) => p.kind === kind);
  const defaultPreset = list.find((p) => p.is_default === true) || null;

  const edit = (p: Preset) => {
    setKind(p.kind);
    setEditingId(p.id);
    setEditingMask(p.api_key_mask || "");
    setForm({
      name: p.name,
      base_url: p.base_url || "",
      model: p.model,
      api_key: "",  // 不回显明文密钥；留空表示不变
      temperature: String((p.extra as Record<string, unknown>)?.temperature ?? ""),
      reasoning_effort: String((p.extra as Record<string, unknown>)?.reasoning_effort ?? ""),
      enable_thinking: typeof (p.extra as Record<string, unknown>)?.enable_thinking === "boolean"
        ? String((p.extra as Record<string, unknown>).enable_thinking) : "",
    });
    setEditingExtra({ ...(p.extra || {}) });
    setSaveOk(null);
    setFormErr(null);
    formTopRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditingMask("");
    setForm({ name: "", base_url: "", model: "", api_key: "", temperature: "", reasoning_effort: "", enable_thinking: "" });
    setEditingExtra({});
    setSaveOk(null);
    setFormErr(null);
  };

  const save = async () => {
    setSaveOk(null);
    setFormErr(null);
    // 名称 / 模型 / 地址一律去首尾空格再校验与提交：只有空格的名称不算填写，
    // 带空格的名称会让同名判重与列表显示对不上（后端 PresetIn 也会再兜底一次）
    const name = form.name.trim();
    const model = form.model.trim();
    if (!name || !model) {
      setFormErr({
        name: name ? undefined : "预设名称为必填",
        model: model ? undefined : "Model 为必填",
      });
      pushError("模型配置·保存", "名称与模型名为必填");
      return;
    }
    // 防呆：密钥栏被填成了 URL（常见手滑）
    if (form.api_key && /^https?:\/\//i.test(form.api_key.trim())) {
      pushError("模型配置·保存", "API Key 栏填的是 URL，不是密钥", `你填的是：${form.api_key.slice(0, 40)}…\n请到模型服务商控制台复制 API Key。`);
      return;
    }
    // 同名同 kind 已存在时先拦下（后端 POST 也会返回 409 兜底）
    const duplicate = !editingId && presets.find(
      (p) => p.kind === kind && (p.name || "").trim().toLowerCase() === name.toLowerCase(),
    );
    if (duplicate) {
      setFormErr({ duplicate: `已存在同名预设「${duplicate.name}」，请改名或直接点列表里该预设的「编辑」` });
      pushError(
        "模型配置·保存",
        `已存在同名预设「${duplicate.name}」`,
        "同名同 kind 会产生难以区分的副本。请改名，或直接点列表里该预设的「编辑」修改它。",
      );
      return;
    }
    const payload = {
      kind, name, provider: "openai_compatible",
      base_url: form.base_url.trim(), model, api_key: form.api_key.trim() || undefined,
      // 在原 extra 上改：表单只管 temperature / reasoning_effort / enable_thinking，其余配置项（如 streaming）原样保留
      extra: kind === "embedding" ? { ...editingExtra } : {
        ...editingExtra,
        temperature: Number(form.temperature) || 0,
        reasoning_effort: form.reasoning_effort || undefined,
        // 三态："" = 端点默认（DashScope 关、其它不发该字段），"true"/"false" = 显式开关
        enable_thinking: form.enable_thinking === "" ? undefined : form.enable_thinking === "true",
      },
    };
    try {
      if (editingId) {
        await put(`/api/presets/${editingId}`, payload);
        setSaveOk("预设已更新");
        pushInfo("模型配置·更新", `已更新 ${kind}/${name}`);
      } else {
        // 非编辑态保存 = 新建；表单里回填的默认预设字段会变成一条新副本（见上方 useEffect 注释）
        await post("/api/presets", payload);
        setSaveOk("预设已保存（密钥已加密落盘）");
        pushInfo("模型配置·保存", `已保存 ${kind}/${name}`);
      }
      setEditingId(null);
      setEditingMask("");
    } catch (e) {
      pushError("模型配置·保存", "保存失败", String(e));
      return;
    }
    // 保存成功后刷新列表：load 自身没有 catch，这里必须 await + 兜住，
    // 否则刷新失败会变成未处理的 Promise rejection
    try {
      await load();
    } catch (e) {
      pushError("模型配置·保存", "保存成功，但刷新预设列表失败", String(e));
    }
  };

  const test = async (id: string, name: string) => {
    setTesting(id);
    setResults((r) => ({ ...r, [id]: { ok: false, message: "正在测试…", latency_ms: 0 } }));
    try {
      const r = await post<{ ok: boolean; latency_ms: number; detail?: string }>(`/api/presets/${id}/test`);
      const msg = r.ok ? `连通正常 · ${r.latency_ms}ms` : `连通失败：${r.detail || "未知错误"}`;
      setResults((m) => ({ ...m, [id]: { ok: r.ok, message: msg, latency_ms: r.latency_ms } }));
      if (r.ok) pushInfo("模型配置·自检", `${name}：${msg}`);
      else pushError("模型配置·自检", `${name}：${r.detail || "连通失败"}`);
    } catch (e) {
      const msg = String(e);
      setResults((m) => ({ ...m, [id]: { ok: false, message: msg } }));
      pushError("模型配置·自检", `${name}：${msg}`);
    }
    setTesting(null);
  };

  const setAsDefault = async (id: string, name: string, pKind: string) => {
    try {
      await post(`/api/presets/${id}/set-default?kind=${pKind}`, {});
      pushInfo("模型配置·默认", `已将 ${name} 设为默认${pKind === "embedding" ? "Embedding" : "LLM"}预设`);
      try {
        await load();
      } catch (e) {
        pushError("模型配置·默认", "设为默认成功，但刷新预设列表失败", String(e));
      }
    } catch (e) {
      pushError("模型配置·默认", `设为默认失败`, String(e));
    }
  };

  // ---------------- RAGFlow 连接 ----------------
  const verdictMessage = (verdict: string, baseUrl: string) => {
    if (verdict === "unreachable") return `连不上 RAGFlow：${baseUrl}。请确认地址能从工作台所在机器打开`;
    if (verdict === "auth_failed") return "这个 API Key 无效，或它没有该知识库的权限。请在 RAGFlow 右上角头像→配置→API 里核对";
    if (verdict === "incompatible") return "服务返回的不是 RAGFlow 数据（可能被网关或登录页拦截）。请检查反向代理是否放行 /api/v1";
    if (verdict === "graph_empty") return "连通正常，但没有可用素材：该账号在此实例下没有可见知识库，或首个知识库还没有文档。请先在 RAGFlow 建库/上传文档，或换一个有权限的 API Key";
    if (verdict === "ready") return "连通正常，可以同步。能否出题请到「知识图谱」页对具体知识库点「检查图谱」";
    return `连通正常（${VERDICT_LABEL[verdict] || verdict}）`;
  };

  const editConn = (c: RagflowConnection) => {
    setConnEditingId(c.id);
    setConnEditing(c);
    // key 永不回填：编辑态只显示掩码，留空表示不变
    setConnForm({ name: c.name, base_url: c.base_url, api_key: "" });
    setConnSaveOk(null);
    setConnErr(null);
  };

  const cancelConnEdit = () => {
    setConnEditingId(null);
    setConnEditing(null);
    setConnForm({ name: "", base_url: "", api_key: "" });
    setConnSaveOk(null);
    setConnErr(null);
  };

  const saveConn = async () => {
    setConnSaveOk(null);
    setConnErr(null);
    const block = (msg: string, hint?: string) => {
      setConnErr(hint ? `${msg}。${hint}` : msg);
      pushError("RAGFlow 连接·保存", msg, hint);
    };
    const name = connForm.name.trim();
    const baseUrl = connForm.base_url.trim();
    if (!name) {
      block("连接名称不能为空");
      return;
    }
    if (!connEditingId) {
      if (!baseUrl || !connForm.api_key.trim()) {
        block("新增连接必须填写名称、服务地址和 API Key");
        return;
      }
    }
    // 与后端 400 对齐：换地址必须同时给 key，前端先拦，避免用户白填一遍
    const baseChanged = !!connEditing && normBaseUrl(baseUrl) !== normBaseUrl(connEditing.base_url);
    if (connEditingId && baseChanged && !connForm.api_key.trim()) {
      block(
        "服务地址已变更，请重新填写 API Key",
        "已保存的 key 不会发往新地址。换地址时必须同时填写 key。",
      );
      return;
    }
    try {
      const wasEditing = !!connEditingId;
      if (connEditingId) {
        const payload: { name: string; base_url?: string; api_key?: string } = { name };
        if (baseChanged) payload.base_url = baseUrl;
        if (connForm.api_key.trim()) payload.api_key = connForm.api_key.trim();
        await patch(`/api/ragflow/connections/${connEditingId}`, payload);
        // 地址或 key 变了：后端已作废上次测试结果，这里同步清掉本页的测试提示
        if (payload.base_url || payload.api_key) {
          const id = connEditingId;
          setConnResults((m) => { const next = { ...m }; delete next[id]; return next; });
        }
        pushInfo("RAGFlow 连接·更新", `已更新 ${name}`);
      } else {
        await post("/api/ragflow/connections", { name, base_url: baseUrl, api_key: connForm.api_key.trim() });
        pushInfo("RAGFlow 连接·保存", `已保存 ${name}`);
      }
      cancelConnEdit();
      setConnSaveOk(wasEditing ? "连接已更新" : "连接已保存（密钥已加密落盘）");
      loadConns();
    } catch (e) {
      pushError("RAGFlow 连接·保存", "保存失败", String(e));
      // 调试抽屉默认收起，只记日志等于「点了没反应」：失败原因必须上屏
      setConnSaveOk(null);
      setConnErr(`保存失败：${(e as Error).message}`);
    }
  };

  const testConn = async (c: RagflowConnection) => {
    setConnTesting(c.id);
    setConnResults((r) => ({ ...r, [c.id]: { ok: false, message: "正在测试…" } }));
    try {
      const r = await post<{ verdict: string; probed_version?: string | null; stages: RagflowStage[] }>(
        `/api/ragflow/connections/${c.id}/test`, {},
      );
      // graph_empty = 连通成功但没有可用素材，不能当成功提示
      const ok = !["unreachable", "auth_failed", "incompatible", "graph_empty"].includes(r.verdict);
      const msg = verdictMessage(r.verdict, c.base_url);
      // 版本锚定：原样透传接口的 probed_version，拿不到时由 ragflowVersionText 给「版本未知」。
      // 连不上 / 鉴权失败时版本必然拿不到，结果条已经在报错，再挂「版本未知」只是噪音，故不追加。
      const versionKnown = !["unreachable", "auth_failed"].includes(r.verdict);
      setConnResults((m) => ({
        ...m,
        [c.id]: { ok, message: msg, ...(versionKnown ? { version: r.probed_version ?? null } : {}) },
      }));
      if (ok) pushInfo("RAGFlow 连接·测试", `${c.name}：${msg}`);
      else pushError("RAGFlow 连接·测试", `${c.name}：${msg}`);
      loadConns();  // 刷新 last_test / probed_version
    } catch (e) {
      const msg = String(e);
      setConnResults((m) => ({ ...m, [c.id]: { ok: false, message: msg } }));
      pushError("RAGFlow 连接·测试", `${c.name}：测试失败`, msg);
    } finally {
      setConnTesting(null);
    }
  };

  const removeConn = async (c: RagflowConnection) => {
    if (!confirm(`确定删除连接「${c.name}」？只删除凭据；引用它的测试集会停止跟随该数据源（题仍可查看/导出），之后可在测试集详情里用「重新绑定」恢复。`)) return;
    setConnDeleting(c.id);
    try {
      const r = await del<{ unbound_testset_ids?: string[] }>(`/api/ragflow/connections/${c.id}`);
      const stopped = r?.unbound_testset_ids?.length ?? 0;
      pushInfo("RAGFlow 连接·删除",
        `已删除 ${c.name}（历史产物保留）${stopped ? `；${stopped} 个测试集已标记为停止跟踪，可重新绑定` : ""}`);
      if (connEditingId === c.id) cancelConnEdit();
      loadConns();
    } catch (e) {
      pushError("RAGFlow 连接·删除", `删除 ${c.name} 失败`, String(e));
    } finally {
      setConnDeleting(null);
    }
  };

  /** API Key 栏里填的是 URL：边输入边提示（不等提交），同时用于 aria-invalid / aria-describedby */
  const keyIsUrl = !!form.api_key && /^https?:\/\//i.test(form.api_key.trim());

  return (
    <div className="max-w-4xl mx-auto p-8 space-y-6">
      <header>
        <h1 className="text-xl font-semibold">模型配置</h1>
        <p className="text-sm text-muted-foreground mt-1">配置 LLM 与 Embedding 端点（OpenAI 兼容协议），保存前可自检连通性。密钥仅后端加密存储，前端只显示掩码。</p>
      </header>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {(["llm", "llm_testset", "embedding"] as const).map((k) => (
          <button key={k} onClick={() => setKind(k)}
            className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${kind === k ? "border-primary text-primary-strong" : "border-transparent text-muted-foreground hover:text-foreground"}`}>
            {k === "llm" ? "LLM（知识图谱）" : k === "llm_testset" ? "LLM（测试集生成）" : "Embedding（向量化）"}
          </button>
        ))}
      </div>

      {/* 新建/编辑表单 */}
      <Card ref={formTopRef} className="p-5 space-y-4 scroll-mt-4">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-medium text-muted-foreground">
            {editingId ? `编辑中：${form.name || "（未命名）"}` : `新增${kind === "embedding" ? " Embedding " : " LLM "}预设`}
          </h2>
          {editingId ? (
            <Badge variant="warning" size="pill">编辑中</Badge>
          ) : (
            <Badge variant="info" size="pill">新增模式</Badge>
          )}
        </div>
        {!editingId && (
          <p className="text-xs text-muted-foreground leading-relaxed">
            当前为新增模式，保存会创建一条新预设；要修改已有预设（含默认配置），请点列表中对应行的「编辑」。
            {defaultPreset && (
              <button className="ml-2 underline hover:text-primary-strong rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => edit(defaultPreset)}>
                编辑当前默认「{defaultPreset.name}」→
              </button>
            )}
            {!defaultPreset && (
              <span className="ml-1">本 tab 暂无默认预设，新建的第一条会自动设为默认。</span>
            )}
          </p>
        )}
        <div className="grid grid-cols-2 gap-4">
          <div><Label>预设名称 *</Label>
            <Input aria-label="预设名称" value={form.name} onChange={(e) => { setForm({ ...form, name: e.target.value }); setFormErr(null); }} placeholder=""
              aria-invalid={!!(formErr?.name || formErr?.duplicate)}
              aria-describedby={formErr?.name ? "preset-name-error" : formErr?.duplicate ? "preset-duplicate-error" : undefined} />
            {formErr?.name && (
              <div id="preset-name-error" className="mt-1 text-xs text-red-600 dark:text-red-400">⚠️ {formErr.name}</div>
            )}
            {formErr?.duplicate && (
              <div id="preset-duplicate-error" className="mt-1 text-xs text-red-600 dark:text-red-400">⚠️ {formErr.duplicate}</div>
            )}</div>
          <div><Label>Model *</Label>
            <Input aria-label="Model" value={form.model} onChange={(e) => { setForm({ ...form, model: e.target.value }); setFormErr(null); }} placeholder=""
              aria-invalid={!!formErr?.model}
              aria-describedby={formErr?.model ? "preset-model-error" : undefined} />
            {formErr?.model && (
              <div id="preset-model-error" className="mt-1 text-xs text-red-600 dark:text-red-400">⚠️ {formErr.model}</div>
            )}</div>
          <div className="col-span-2"><Label>Endpoint (base_url)</Label>
            <Input aria-label="Endpoint (base_url)" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="" /></div>
          <div><Label>API Key</Label>
            <div className="relative">
              <Input aria-label="API Key" type={showKey ? "text" : "password"} value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder={editingId && editingMask ? `${editingMask} · 留空则不变`
                  : editingId ? "留空 = 保持原密钥不变" : "sk-…（新建预设建议填写）"}
                aria-invalid={keyIsUrl}
                aria-describedby={keyIsUrl ? "preset-api-key-error" : undefined}
                className="pr-10" />
              <button type="button"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded text-muted-foreground hover:text-foreground active:text-primary-strong transition-colors select-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label="按住显示 API Key 明文"
                onMouseDown={(e) => { e.preventDefault(); setShowKey(true); }}
                onMouseUp={() => setShowKey(false)}
                onMouseLeave={() => setShowKey(false)}
                onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); setShowKey(true); } }}
                onKeyUp={() => setShowKey(false)}
                onBlur={() => setShowKey(false)}
                title="按住显示明文">
                {/* Eye SVG icon */}
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  {showKey ? (
                    <>
                      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                      <line x1="1" y1="1" x2="23" y2="23" />
                    </>
                  ) : (
                    <>
                      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                      <circle cx="12" cy="12" r="3" />
                    </>
                  )}
                </svg>
              </button>
            </div>
            {keyIsUrl && (
              <div id="preset-api-key-error" className="mt-1 text-xs text-red-600 dark:text-red-400">⚠️ 看起来你把 URL 填进了 API Key 栏（应填 sk- 开头密钥）</div>
            )}
            {editingId && !form.api_key && (
              <div className="mt-1 text-xs text-muted-foreground">留空 = 保持原密钥不变</div>
            )}
          </div>
          {kind !== "embedding" && (
            <div><Label>temperature</Label>
              <Input aria-label="temperature" value={form.temperature} onChange={(e) => setForm({ ...form, temperature: e.target.value })} /></div>
          )}
          {kind !== "embedding" && (
            <div><Label>推理强度（reasoning_effort）</Label>
              <select aria-label="推理强度" value={form.reasoning_effort}
                onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}>
                <option value="">模型默认</option>
                {REASONING_EFFORT_OPTIONS.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
              <div className="mt-1 text-xs text-muted-foreground">
                带思考链的模型建议选 minimal 或 none（默认思考很长，易超过网关 60 秒超时）；个别模型只有在 none 时才接受 temperature（否则会 400）
              </div></div>
          )}
          {kind !== "embedding" && (
            <div><Label>思考模式（enable_thinking）</Label>
              <select aria-label="思考模式" value={form.enable_thinking}
                disabled={!isDashscopeEndpoint(form.base_url)}
                onChange={(e) => setForm({ ...form, enable_thinking: e.target.value })}>
                <option value="">端点默认</option>
                <option value="false">关闭</option>
                <option value="true">开启</option>
              </select>
              <div className="mt-1 text-xs text-muted-foreground">
                {isDashscopeEndpoint(form.base_url)
                  ? "DashScope 端点默认关闭思考（省输出 token 与耗时）；开启后单次调用更慢、输出 token 更多。"
                  : "仅 DashScope 端点适用：其它服务商收到该字段会报 400，所以这里不发也不可设置。"}
              </div></div>
          )}
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={save}>{editingId ? "保存修改" : "新增配置"}</Button>
          {editingId && <Button variant="outline" onClick={cancelEdit}>取消编辑</Button>}
          {saveOk && <span className="text-xs text-emerald-700 dark:text-emerald-300">{saveOk}</span>}
        </div>
      </Card>

      {/* 预设列表 */}
      <Card className="divide-y divide-border">
        <CardHeader className="px-5 py-3 text-sm font-medium text-muted-foreground">已保存预设（{list.length}）</CardHeader>
        {list.length === 0 && <div className="px-5 py-6 text-sm text-muted-foreground">暂无，先在上方创建。</div>}
        {list.map((p) => {
          const tr = results[p.id];
          const isDefault = p.is_default === true;
          return (
            <div key={p.id} className="px-5 py-3">
              <div className="flex items-center gap-4">
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium flex items-center gap-2">
                    {isDefault && (
                      <Badge variant="success" size="pill" className="px-1.5 shrink-0">
                        默认 · 当前生效
                      </Badge>
                    )}
                    {p.name} <span className="text-xs text-muted-foreground ml-1">{p.model}</span>
                    {editingId === p.id && <Badge variant="warning" size="pill" className="ml-2 px-1.5">编辑中</Badge>}
                  </div>
                  <div className="text-xs text-muted-foreground truncate">{p.base_url || "未设置"} · {p.api_key_mask || "未设密钥"}</div>
                  {!p.api_key_mask && (
                    <div className="mt-1 text-xs text-red-600 dark:text-red-400 flex items-center gap-1">
                      <span>⚠️ 缺少 API Key，使用此预设会报 401 错误</span>
                      <button className="underline hover:text-primary-strong rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => edit(p)}>立即设置 →</button>
                    </div>
                  )}
                </div>
                <Button
                  variant="outline"
                  size="xs"
                  className={isDefault ? "border-border text-muted-foreground cursor-not-allowed opacity-50" : undefined}
                  disabled={isDefault}
                  onClick={() => setAsDefault(p.id, p.name, p.kind)}
                  title={isDefault ? "当前已是默认预设" : "设为默认预设"}
                >{isDefault ? "已默认" : "设为默认"}</Button>
                <Button variant="outline" size="xs" onClick={() => edit(p)}>编辑</Button>
                <Button variant="outline" size="xs" disabled={testing === p.id} onClick={() => test(p.id, p.name)}>
                  {testing === p.id ? "自检中…" : "连通自检"}
                </Button>
                <Button variant="danger" size="xs" disabled={editingId === p.id || deleting === p.id} onClick={async () => {
                  setDeleting(p.id);
                  try {
                    await del(`/api/presets/${p.id}`);
                    pushInfo("模型配置·删除", `已删除 ${p.name}`);
                    if (editingId === p.id) cancelEdit();
                    try {
                      await load();
                    } catch (e) {
                      pushError("模型配置·删除", `已删除 ${p.name}，但刷新预设列表失败`, String(e));
                    }
                  } catch (e) {
                    pushError("模型配置·删除", `删除 ${p.name} 失败`, String(e));
                  } finally {
                    setDeleting(null);
                  }
                }}>{deleting === p.id ? "删除中…" : "删除"}</Button>
              </div>
              {tr && (
                <div className={`mt-2 text-xs px-2 py-1 rounded border ${
                  tr.message === "正在测试…" ? "border-border text-muted-foreground" :
                  tr.ok ? "border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300" :
                  "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300"
                }`}>
                  {tr.message}
                </div>
              )}
            </div>
          );
        })}
      </Card>

      {/* RAGFlow 连接：key 永不回填，legacy 连接地址只读 */}
      <Card className="p-5 space-y-4">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-medium text-muted-foreground">
            {connEditingId ? `编辑中：${connEditing?.name || "（未命名）"}` : "新增 RAGFlow 连接"}
          </h2>
          {connEditingId ? (
            <Badge variant="warning" size="pill">编辑中</Badge>
          ) : (
            <Badge variant="info" size="pill">新增模式</Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground leading-relaxed">
          连接 = RAGFlow 服务地址 + API Key。密钥仅后端加密存储，前端只显示掩码；编辑时留空表示保持原密钥不变。
        </p>
        <div className="grid grid-cols-2 gap-4">
          <div><Label>连接名称 *</Label>
            <Input aria-label="连接名称" value={connForm.name} onChange={(e) => setConnForm({ ...connForm, name: e.target.value })} placeholder="例如：本机 RAGFlow" /></div>
          <div><Label>服务地址 *</Label>
            <Input aria-label="服务地址" value={connForm.base_url} readOnly={!!connEditing?.legacy}
              onChange={(e) => setConnForm({ ...connForm, base_url: e.target.value })}
              placeholder="http://127.0.0.1:9380" />
            {connEditing?.legacy && (
              <div className="mt-1 text-xs text-muted-foreground">历史配置来自旧目录，服务地址不可修改；如需换地址请新建一条连接。</div>
            )}
          </div>
          <div className="col-span-2"><Label>API Key {connEditingId ? "" : "*"}</Label>
            <div className="relative">
              <Input aria-label="API Key" type={showConnKey ? "text" : "password"} autoComplete="off"
                value={connForm.api_key}
                onChange={(e) => setConnForm({ ...connForm, api_key: e.target.value })}
                placeholder={connEditingId ? `${connEditing?.api_key_mask || "已保存"} · 留空则不变` : "ragflow-…（新增必填）"}
                className="pr-10" />
              <button type="button"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded text-muted-foreground hover:text-foreground active:text-primary-strong transition-colors select-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label="按住显示 API Key 明文"
                onMouseDown={(e) => { e.preventDefault(); setShowConnKey(true); }}
                onMouseUp={() => setShowConnKey(false)}
                onMouseLeave={() => setShowConnKey(false)}
                onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); setShowConnKey(true); } }}
                onKeyUp={() => setShowConnKey(false)}
                onBlur={() => setShowConnKey(false)}
                title="按住显示明文">
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  {showConnKey ? (
                    <>
                      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                      <line x1="1" y1="1" x2="23" y2="23" />
                    </>
                  ) : (
                    <>
                      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                      <circle cx="12" cy="12" r="3" />
                    </>
                  )}
                </svg>
              </button>
            </div>
            {connEditingId && !connForm.api_key && (
              <div className="mt-1 text-xs text-muted-foreground">留空 = 保持原密钥不变；只有换服务地址时才必须重填。</div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={saveConn}>{connEditingId ? "保存修改" : "新增连接"}</Button>
          {connEditingId && <Button variant="outline" onClick={cancelConnEdit}>取消编辑</Button>}
          {connSaveOk && <span className="text-xs text-emerald-700 dark:text-emerald-300">{connSaveOk}</span>}
        </div>
        {connErr && <div className="text-xs text-red-600 dark:text-red-400">{connErr}</div>}
      </Card>

      <Card className="divide-y divide-border">
        <CardHeader className="px-5 py-3 text-sm font-medium text-muted-foreground">已保存连接（{conns.length}）</CardHeader>
        {conns.length === 0 && <div className="px-5 py-6 text-sm text-muted-foreground">暂无，先在上方创建。</div>}
        {conns.map((c) => {
          const tr = connResults[c.id];
          const verdict = c.last_test?.verdict;
          return (
            <div key={c.id} className="px-5 py-3">
              <div className="flex items-center gap-4">
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium flex items-center gap-2 flex-wrap">
                    {c.name}
                    {c.legacy && (
                      <Badge variant="outline" size="pill" className="px-1.5 text-muted-foreground shrink-0">历史配置</Badge>
                    )}
                    {verdict && (
                      <Badge
                        variant={verdict === "ready" ? "success" : verdict === "partial" ? "warning" : "danger"}
                        size="pill"
                        className="px-1.5 shrink-0"
                        title={c.last_test?.at ? `上次测试：${c.last_test.at}` : undefined}>
                        {VERDICT_LABEL[verdict] || verdict}
                      </Badge>
                    )}
                    {c.last_test && (
                      // 版本锚定：测过一次就常驻显示；上游没返回时明确写「版本未知」，不留空白
                      <Badge
                        variant="outline"
                        size="pill"
                        className="px-1.5 shrink-0 font-normal text-muted-foreground"
                        title={c.last_test.at ? `上次测试：${c.last_test.at}` : undefined}>
                        {ragflowVersionText(c.probed_version)}
                      </Badge>
                    )}
                    {connEditingId === c.id && <Badge variant="warning" size="pill" className="px-1.5">编辑中</Badge>}
                  </div>
                  <div className="text-xs text-muted-foreground truncate">{c.base_url} · {c.api_key_mask || "未设密钥"}</div>
                </div>
                <Button variant="outline" size="xs" onClick={() => editConn(c)}>编辑</Button>
                <Button variant="outline" size="xs" disabled={connTesting === c.id} onClick={() => testConn(c)}>
                  {connTesting === c.id ? "测试中…" : "测试"}
                </Button>
                <Button variant="danger" size="xs" disabled={connDeleting === c.id} onClick={() => removeConn(c)}>
                  {connDeleting === c.id ? "删除中…" : "删除"}
                </Button>
              </div>
              {tr && (
                <div className={`mt-2 text-xs px-2 py-1 rounded border ${
                  tr.message === "正在测试…" ? "border-border text-muted-foreground" :
                  tr.ok ? "border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300" :
                  "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300"
                }`}>
                  {tr.message}
                  {tr.version !== undefined && (
                    <span className="ml-2 opacity-80" data-testid="conn-test-version">
                      · {ragflowVersionText(tr.version)}
                    </span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </Card>

      {/* 底部操作区：下一步 */}
      <div className="flex justify-end pt-2">
        <Button onClick={onNext}>下一步：文档输入 →</Button>
      </div>

    </div>
  );
}