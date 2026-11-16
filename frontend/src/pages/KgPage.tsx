import { useCallback, useEffect, useRef, useState } from "react";
import { del, get, post, type ApiError } from "../api";
import { pushError, pushInfo } from "../lib/errorLog";
import TaskProgress from "../components/TaskProgress";
import GraphView from "../components/GraphView";
import { NODE_LABELS } from "../lib/graphStyle";
import { formatPropValue, PROP_LABELS } from "../lib/kgFormat";
import { ragflowVersionText } from "../lib/ragflowText";
import RagflowPanel from "../components/RagflowPanel";
import { setSelectedKg } from "../lib/kgSelection";
import { useTrackedTask } from "../lib/useTrackedTask";
import type { Checkpoint, DocItem, KgMeta, KgNode, KgPreview, Preset } from "../types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";

/** 数据源展示标签：优先「连接名/知识库 ID」，缺连接名时退回 dataset_id / source_id */
function sourceLabel(src: { connection_name?: string | null; dataset_id?: string | null; source_id?: string | null }) {
  if (src.connection_name && src.dataset_id) return `${src.connection_name}/${src.dataset_id}`;
  return src.dataset_id || src.source_id || "";
}

/** 右侧详情面板：缩略 / 完整内容 Tab 切换 */
function DetailPanelTabs({ selected }: { selected: KgNode }) {
  const [tab, setTab] = useState<"summary" | "full">("summary");
  const fullText = (() => {
    const pc = selected.properties.page_content;
    return typeof pc === "string" ? pc : "";
  })();

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Tab 栏 */}
      <div className="flex border-b border-border px-4">
        <button
          className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            tab === "summary"
              ? "border-primary-strong text-primary-strong"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
          onClick={() => setTab("summary")}
        >缩略内容</button>
        <button
          className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            tab === "full"
              ? "border-primary-strong text-primary-strong"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
          onClick={() => setTab("full")}
          disabled={!fullText}
        >完整内容</button>
      </div>
      {/* Tab 内容区 */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {tab === "summary" && (
          Object.entries(selected.properties).map(([k, v]) => (
            <div key={k}>
              <div className="text-primary-strong font-medium">{PROP_LABELS[k] || k}</div>
              <div className="text-muted-foreground break-words line-clamp-6">{formatPropValue(v)}</div>
            </div>
          ))
        )}
        {tab === "full" && (
          <div className="whitespace-pre-wrap break-words text-foreground leading-relaxed">{fullText}</div>
        )}
      </div>
    </div>
  );
}

export default function KgPage({ onNext, onBack }: { onNext: () => void; onBack?: () => void }) {
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [kgs, setKgs] = useState<KgMeta[]>([]);
  const [ckpts, setCkpts] = useState<Checkpoint[]>([]);
  const [form, setForm] = useState({ name: "" });
  const [taskId, setTaskId] = useTrackedTask("kg", ["build_kg"]);
  const [ragflowTaskId, setRagflowTaskId] = useTrackedTask("ragflow", ["ragflow_sync"]);
  const [preview, setPreview] = useState<KgPreview | null>(null);
  /** 当前预览的是哪份 KG：删除别的 KG 时不应关掉正在看的预览（同 F3） */
  const [previewKgId, setPreviewKgId] = useState<string | null>(null);
  const [selected, setSelected] = useState<KgNode | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [repulsion, setRepulsion] = useState(900);  // 力导向斥力强度（传给 GraphView）
  // chunk 切分参数（HeadlineSplitter 的 min_tokens/max_tokens，传给后端）
  const [minTokens, setMinTokens] = useState(500);
  const [maxTokens, setMaxTokens] = useState(1000);
  // 追踪用户是否手动改过名称（手动改过就不再自动覆盖）
  const nameTouchedRef = useRef(false);
  /** 409 二次确认：被数据源投影引用的图谱，确认后带 detach_sources 切断指针再删除 */
  const [detachPrompt, setDetachPrompt] = useState<{
    kg: KgMeta; detail: string; sources: NonNullable<ApiError["sources"]>;
  } | null>(null);
  const [detachBusy, setDetachBusy] = useState(false);
  /** 取消生成的二次确认：三选一（继续生成 / 仅取消保留断点 / 取消并删除断点） */
  const [cancelPrompt, setCancelPrompt] = useState<{ taskId: string; runId?: string } | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);

  const loadAll = useCallback(async () => {
    const [d, p, k, c] = await Promise.all([
      get<DocItem[]>("/api/documents"), get<Preset[]>("/api/presets"), get<KgMeta[]>("/api/kg"),
      get<Checkpoint[]>("/api/kg/checkpoints"),
    ]);
    setDocs(d); setPresets(p); setKgs(k); setCkpts(c); setLoaded(true);
    // 自动生成默认名称：第一个文档名 + 时间戳（仅用户未手动编辑时）
    if (!nameTouchedRef.current && d.length > 0) {
      const firstName = d[0].name.replace(/\.[^.]+$/, ""); // 去掉扩展名
      const ts = new Date().toISOString().slice(0, 16).replace("T", "T"); // 2026-08-20T23:19
      setForm((f) => ({ ...f, name: `${firstName}-${ts}` }));
    }
  }, []);
  useEffect(() => { loadAll().catch((e) => setErr(String(e))); }, [loadAll]);

  // 自动取第一页配置的默认 LLM / Embedding 预设（无需在本题重复选择）
  const defaultLlm = presets.find((x) => x.kind === "llm" && x.is_default) || presets.find((x) => x.kind === "llm");
  const defaultEmb = presets.find((x) => x.kind === "embedding" && x.is_default) || presets.find((x) => x.kind === "embedding");

  const openPreview = async (kgId: string) => {
    setSelected(null);
    try { setPreview(await get<KgPreview>(`/api/kg/${kgId}/preview?max_nodes=150`)); setPreviewKgId(kgId); }
    catch (e) { setErr(String(e)); }
  };

  /** 删除 KG：仍被 RAGFlow 数据源当投影引用时后端返回 409，需要用户在弹层里再次确认后才带 detach_sources。 */
  const removeKg = async (k: KgMeta) => {
    if (!confirm(`确定删除知识图谱「${k.name}」？此操作不可恢复。`)) return;
    try {
      await del(`/api/kg/${k.id}`);
    } catch (e) {
      const err = e as ApiError;
      if (err.errorCode !== "kg_in_use") { setErr(`删除失败：${err.message}`); return; }
      // 409 原文与引用方明细都放进弹层作为依据，不吞掉后端说明
      setDetachPrompt({ kg: k, detail: err.message, sources: err.sources || [] });
      return;
    }
    afterKgDeleted(k);
  };

  const afterKgDeleted = (k: KgMeta) => {
    if (previewKgId === k.id) { setPreview(null); setPreviewKgId(null); }
    loadAll();
  };

  /** 二次确认后：先切断所有引用该图谱的数据源指针，再由后端删除图谱（失败会回滚，图谱仍在） */
  const confirmDetachDelete = async () => {
    if (!detachPrompt) return;
    setDetachBusy(true);
    try {
      const r = await del<{ detached_sources: NonNullable<ApiError["sources"]> }>(
        `/api/kg/${detachPrompt.kg.id}?detach_sources=true`);
      const names = (r.detached_sources || []).map(sourceLabel).filter(Boolean).join("、") || "数据源";
      pushInfo("知识图谱·删除", `已删除「${detachPrompt.kg.name}」，并重置 ${names} 的投影指针（下次同步重建投影）`);
      const k = detachPrompt.kg;
      setDetachPrompt(null);
      afterKgDeleted(k);
    } catch (e) {
      setErr(`删除失败：${(e as Error).message}`);
      pushError("知识图谱·删除", "切断数据源指针后删除失败", String(e));
    } finally { setDetachBusy(false); }
  };

  const generate = async (opts?: { resumeRunId?: string }) => {
    setErr(null);
    if (!defaultLlm || !defaultEmb) { setErr("请先在「模型配置」创建并设好默认 LLM / Embedding 预设"); return; }
    if (!docs.length) { setErr("请先在「文档输入」上传文档"); return; }
    const isResume = !!opts?.resumeRunId;
    try {
      const r = await post<{ task_id: string }>("/api/kg/generate", {
        document_ids: docs.map((d) => d.id),
        llm_preset_id: defaultLlm.id, embedding_preset_id: defaultEmb.id, name: form.name,
        run_id: opts?.resumeRunId, resume: isResume,
        min_tokens: minTokens, max_tokens: maxTokens,
      });
      setTaskId(r.task_id);
      // 续跑发起后从断点列表移除该项，避免重复点击
      if (isResume && opts?.resumeRunId) setCkpts((cs) => cs.filter((c) => c.run_id !== opts.resumeRunId));
    } catch (e) {
      setErr(String(e));
      pushError("知识图谱·发起", isResume ? "从断点继续失败" : "发起生成失败", String(e));
    }
  };

  const discardCheckpoint = async (c: Checkpoint) => {
    if (!confirm(`确定放弃断点「${c.name || c.run_id}」？此操作不可恢复。`)) return;
    try {
      const r = await del<{ deferred?: boolean }>(`/api/kg/checkpoints/${c.run_id}`);
      // 文档是「文档输入」页的全局资源（可能被历史 KG 引用），放弃断点不动文档；
      // 若需重新生成，文档仍在可直接再次发起
      pushInfo("知识图谱·断点", r?.deferred
        ? `断点「${c.name || c.run_id}」的任务正在收尾，结束后自动删除`
        : `断点「${c.name || c.run_id}」已删除`);
      await loadAll(); // 刷新列表，让断点从界面消失
    } catch (e) {
      setErr(`断点删除失败：${e}`);
      pushError("知识图谱·断点", "断点删除失败", String(e));
    }
  };

  /** 取消生成的二次确认：先发取消，再按选择决定是否删断点。取消语义不变——在跑的那一步会跑完、结果丢弃、不写该步断点。 */
  const confirmCancel = async (deleteCheckpoint: boolean) => {
    if (!cancelPrompt) return;
    const { taskId: id, runId } = cancelPrompt;
    setCancelBusy(true);
    try {
      await post(`/api/tasks/${id}/cancel`).catch(() => {});
      if (deleteCheckpoint && runId) {
        // 取消是异步的：任务还在收尾时后端返回 202 deferred，收尾后自动删除断点（不再偶发 409）
        await del<{ deferred?: boolean }>(`/api/kg/checkpoints/${runId}`)
          .then((r) => pushInfo("知识图谱·取消", r?.deferred
            ? "任务已取消，正在收尾，结束后自动删除断点" : "任务已取消，断点已删除"))
          .catch((e) => pushError("知识图谱·取消", "断点删除失败", String(e)));
        await loadAll();
      }
    } finally {
      setCancelBusy(false);
      setCancelPrompt(null);
    }
  };

  // demo 模式生成的图谱也要列出来（加「演示」标记），否则演示模式下列表永远为空、进不了测试集页
  const realKgs = kgs;

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-6">
      <header>
        {onBack && (
          <button className="text-xs text-muted-foreground hover:text-primary-strong mb-3 -ml-1 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card" onClick={onBack}>← 返回上一页</button>
        )}
        <h1 className="text-xl font-semibold">知识图谱</h1>
        <p className="text-sm text-muted-foreground mt-1">
          KnowledgeGraph → default_transforms → apply_transforms。产物为 ragas 原生 JSON，可复用跳过本步直接生成测试集。
        </p>
      </header>

      {/* 生成表单（模型自动取第一页默认预设，无需在此配置） */}
      <Card className="p-5 space-y-4">
        <div className="space-y-1 text-xs text-muted-foreground">
          {/* 两行分开展示，避免把「出题模型」误读成向量模型 */}
          <div className="flex items-center gap-2 flex-wrap">
            <span data-testid="kg-llm-preset" className="px-2 py-1 rounded border border-border">出题模型：{!loaded ? "加载中…" : defaultLlm ? `${defaultLlm.name}（${defaultLlm.model}）` : "未配置"}</span>
            <span className="px-2 py-1 rounded border border-border">向量模型：{!loaded ? "加载中…" : defaultEmb ? `${defaultEmb.name}（${defaultEmb.model}）` : "未配置"}</span>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* chunk 切分参数（HeadlineSplitter） */}
            <div className="flex items-center gap-1 px-2 py-1 rounded border border-border">
              <span>min_tokens</span>
              <Input aria-label="min_tokens：分块最小 token 数" type="number" min={50} max={5000} step={50} value={minTokens}
                onChange={(e) => setMinTokens(Math.max(50, Number(e.target.value) || 500))}
                className="w-16 bg-background border border-border rounded px-1 py-0.5 text-xs" title="chunk 最小 token 数（低于此值累积合并）" />
            </div>
            <div className="flex items-center gap-1 px-2 py-1 rounded border border-border">
              <span>max_tokens</span>
              <Input aria-label="max_tokens：分块最大 token 数" type="number" min={100} max={10000} step={100} value={maxTokens}
                onChange={(e) => setMaxTokens(Math.max(100, Number(e.target.value) || 1000))}
                className="w-16 bg-background border border-border rounded px-1 py-0.5 text-xs" title="chunk 最大 token 数（超过此值按词拆分）" />
            </div>
          </div>
        </div>
        <div className="flex items-end gap-4">
          <div className="flex-1"><Label>图谱名称（可选）</Label>
            <Input aria-label="图谱名称（可选）" value={form.name} onChange={(e) => { nameTouchedRef.current = true; setForm({ ...form, name: e.target.value }); }} placeholder="自动生成：文档名-时间" /></div>
          <Button onClick={() => generate()} disabled={!!taskId || !!ragflowTaskId}>生成知识图谱（{docs.length} 个文档）</Button>
        </div>
        <span className="text-xs text-muted-foreground block">⚡ 异步任务：调用 LLM 从文档中抽取实体与关系，生成后自动保存并预览。下方「已保存知识图谱」列表中的是历史记录。</span>
      </Card>

      <RagflowPanel
        busy={!!taskId}
        syncTaskId={ragflowTaskId}
        setSyncTaskId={setRagflowTaskId}
        onSyncDone={(result, error) => {
          setRagflowTaskId(null);
          if (error) { setErr(`RAGFlow 同步失败：${error}`); return; }
          if (result?.kg_id) { loadAll(); openPreview(String(result.kg_id)); }
        }}
      />

      {/* 可恢复的生成断点：上次中断/失败后可从最近 checkpoint 继续 */}
      {ckpts.length > 0 && (
        <Card className="divide-y divide-border border-amber-200 dark:border-amber-800">
          <CardHeader className="px-5 py-3 text-sm font-medium text-amber-700 dark:text-amber-300">
            <span>↻ 发现可恢复的生成断点（{ckpts.length}）</span>
            <span className="text-xs text-muted-foreground font-normal">· 每完成一步自动落盘，中断后可继续后续提取</span>
          </CardHeader>
          {ckpts.map((c) => {
            const pct = c.total_stages ? Math.round((c.stage_index / c.total_stages) * 100) : 0;
            return (
              <div key={c.run_id} className="px-5 py-3 flex items-center gap-4 text-sm">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium">{c.name || "未命名生成"}</span>
                    <Badge variant={c.status === "failed" ? "danger" : "warning"}
                      size="xs">{c.status === "failed" ? "失败" : "运行中"}</Badge>
                    <span className="text-xs text-muted-foreground">步骤 {c.stage_index}/{c.total_stages} · {pct}%</span>
                    <span className="text-xs text-muted-foreground">{c.node_count} 节点 / {c.relationship_count} 关系</span>
                    {c.mode === "demo" && <span className="text-xs text-muted-foreground">（演示）</span>}
                  </div>
                  <div className="text-xs text-muted-foreground truncate mt-0.5">
                    {c.doc_names.join("、")} · {c.updated_at}
                  </div>
                  <div className="h-1 bg-muted rounded-full overflow-hidden mt-2 max-w-xs">
                    <div className="h-full bg-amber-500" style={{ width: `${pct}%` }} />
                  </div>
                </div>
                <Button size="xs" disabled={!!taskId || !!ragflowTaskId}
                  onClick={() => generate({ resumeRunId: c.run_id })}>从断点继续</Button>
                <Button variant="outline" size="xs" onClick={() => discardCheckpoint(c)}>放弃</Button>
              </div>
            );
          })}
        </Card>
      )}

      {err && <div className="text-xs text-red-600 dark:text-red-400">{err}</div>}

      <TaskProgress
        taskId={taskId}
        onDone={(result, error, stopReason) => {
          setTaskId(null);
          setCancelPrompt(null); // 任务先结束时，残留的取消确认一并收起
          void loadAll();
          if (error) {
            // 超预算不是异常失败：断点已保留，提示用户从断点继续（任务 status 仍是 failed）
            setErr(stopReason === "budget_exceeded"
              ? "超预算（可续跑）：已达到 token 预算，已保留断点，可从下方断点继续"
              : `知识图谱生成失败：${error}`);
            return;
          }
          if (result?.kg_id) { loadAll(); openPreview(String(result.kg_id)); }
        }}
        onCancel={(runId?: string) => {
          if (!taskId) return;
          // 不直接取消，先弹三选一确认层；runId 用于「取消并删除断点」
          setCancelPrompt({ taskId, runId });
        }}
      />

      {/* 历史 KG — 仅显示真实生成的，过滤掉 demo 模式数据 */}
      <Card className="divide-y divide-border">
        <CardHeader className="px-5 py-3 text-sm font-medium text-muted-foreground">已保存知识图谱（{realKgs.length}）· 点击预览</CardHeader>
        {realKgs.length === 0 && <div className="px-5 py-6 text-sm text-muted-foreground">暂无。点击上方「生成知识图谱」创建。</div>}
        {realKgs.map((k) => (
          <div key={k.id} className="px-5 py-3 flex items-center gap-4 text-sm">
            <button className="flex-1 text-left min-w-0 -mx-2 px-2 py-1 rounded hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card" onClick={() => openPreview(k.id)}>
              <span className="font-medium">{k.name}</span>
              {k.mode === "demo" && (
                <Badge variant="warning" size="xs" className="ml-2 align-middle">演示</Badge>
              )}
              <span className="text-xs text-muted-foreground ml-2">{k.node_count} 节点 / {k.relationship_count} 关系 · {k.created_at}</span>
              {k.mode === "ragflow-projection" && (
                // 版本锚定：投影生成那次快照记录的版本；存量投影没记过就写「版本未记录」
                <span className="text-xs text-muted-foreground ml-2" data-testid="kg-ragflow-version">
                  {ragflowVersionText(
                    typeof k.ragflow_version === "string" ? k.ragflow_version : null,
                    "RAGFlow 版本未记录",
                  )}
                </span>
              )}
              <div className="text-xs text-muted-foreground truncate">{k.source_files.join("、")}</div>
            </button>
            <Button size="xs" title="跳转到测试集页，并预选该知识图谱"
              onClick={() => { setSelectedKg(k.id); onNext(); }}>
              生成测试集
            </Button>
            <Button variant="danger" size="xs" onClick={() => removeKg(k)}>删除</Button>
          </div>
        ))}
      </Card>

      {/* 图谱预览 */}
      {preview && (
        <Card className="p-5 space-y-4">
          <div className="flex items-center gap-4 flex-wrap">
            <h2 className="text-sm font-medium">图谱预览</h2>
            <span className="text-xs text-muted-foreground">
              {preview.stats.node_count} 节点 / {preview.stats.relationship_count} 关系
              {preview.stats.total_nodes > preview.stats.node_count && `（已裁剪，共 ${preview.stats.total_nodes}）`}
            </span>
            {Object.entries(preview.stats.by_type).map(([t, c]) => (
              <span key={t} className="px-2 py-0.5 rounded-full border border-border text-xs text-muted-foreground">{NODE_LABELS[t] || t}: {c}</span>
            ))}
            {/* 斥力调节：水平滑柱，置于标题行最右侧 */}
            <div className="ml-auto flex items-center gap-2">
              <span className="text-xs text-muted-foreground">斥力</span>
              {/* 与 TestsetPage 的滑块同一外观：原生 input，不用 Input 组件（避免继承 h-9 / border / px-3） */}
              <input aria-label="斥力"
                type="range" min={100} max={3000} step={50} value={repulsion}
                onChange={(e) => setRepulsion(Number(e.target.value))}
                className="accent-primary h-1.5"
                style={{ width: 120 }}
                title={`斥力强度：${repulsion}`}
              />
              <span className="text-xs text-muted-foreground tabular-nums w-10 text-right">{repulsion}</span>
            </div>
          </div>
          <div className="grid grid-cols-[1fr_280px] gap-4">
            <GraphView data={preview} onSelect={setSelected} repulsion={repulsion} />
            <div className="bg-background border border-border rounded-md overflow-hidden flex flex-col max-h-[560px] text-xs">
              {/* 节点信息头部 */}
              {!selected && (
                <div className="p-4 text-muted-foreground">点击节点查看属性（主题摘要 / 实体 / 分块缩略内容 / 嵌入向量维度…）</div>
              )}
              {selected && (
                <>
                  <div className="p-4 pb-2 flex items-center gap-2 border-b border-border">
                    <span className="px-2 py-0.5 rounded-full border border-border">
                      {selected.type === "document" ? "文档" : selected.type === "chunk" ? "分块" : selected.type || "unknown"}
                    </span>
                    <span className="text-muted-foreground truncate">{selected.id.slice(0, 13)}…</span>
                  </div>
                  {/* Tab 切换栏 */}
                  <DetailPanelTabs selected={selected} />
                </>
              )}
            </div>
          </div>
        </Card>
      )}
      {/* 取消生成的二次确认：三选一，明确「断点保留还是删除」 */}
      <Dialog open={!!cancelPrompt} onOpenChange={(open) => { if (!open && !cancelBusy) setCancelPrompt(null); }}>
        <DialogContent className="max-w-md">
          <DialogTitle>取消知识图谱生成？</DialogTitle>
          <DialogDescription className="mt-2 leading-relaxed">
            取消不会立刻停下正在执行的那一步：该步会跑完，结果丢弃，且不写这一步的断点。
            {cancelPrompt?.runId
              ? "已完成步骤的断点可以保留（之后可「从断点继续」），也可以一并删除。"
              : "当前任务还没有可保留的断点。"}
          </DialogDescription>
          <div className="mt-5 flex flex-col gap-2">
            <Button variant="outline" disabled={cancelBusy} onClick={() => setCancelPrompt(null)}>继续生成</Button>
            <Button variant="outline" disabled={cancelBusy} onClick={() => confirmCancel(false)}>取消任务（保留断点）</Button>
            <Button variant="danger" disabled={cancelBusy || !cancelPrompt?.runId} onClick={() => confirmCancel(true)}>
              {cancelBusy ? "处理中…" : "取消并删除断点"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      {/* 删除被引用图谱的二次确认：展示 409 原文 + 受影响数据源，确认后才切断指针再删除 */}
      <Dialog open={!!detachPrompt} onOpenChange={(open) => { if (!open && !detachBusy) setDetachPrompt(null); }}>
        <DialogContent className="max-w-xl">
          <DialogTitle>知识图谱仍是数据源的当前投影</DialogTitle>
          <DialogDescription className="mt-2 leading-relaxed">
            {detachPrompt?.detail}
          </DialogDescription>
          <div className="mt-4 space-y-2">
            <div className="text-xs font-medium">会被切断的数据源</div>
            <ul className="space-y-1 rounded-md border border-border bg-muted/40 p-3 text-xs">
              {(detachPrompt?.sources || []).map((s, i) => (
                <li key={`${s.source_id || s.dataset_id || i}`} className="flex items-center justify-between gap-3">
                  <span className="truncate">{sourceLabel(s) || "未知数据源"}</span>
                  <span className="text-muted-foreground shrink-0">{s.source_id || (s.connection_id ? `连接 ${s.connection_id}` : "")}</span>
                </li>
              ))}
            </ul>
            <div className="text-xs text-muted-foreground leading-relaxed">
              确认后会清空这些数据源的投影指针，再删除该图谱；它们会在下次同步时按首次同步口径重建投影（不消耗 LLM token）。
              引用这张旧图谱的测试集会读时显示「投影已删除、等待重新同步」，同步成功后自动恢复，无需改绑。
              快照文件、数据源配置与测试集记录本身都不会被改写；中途失败会回滚，图谱不会被删除。
            </div>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" disabled={detachBusy} onClick={() => setDetachPrompt(null)}>取消</Button>
            <Button variant="danger" disabled={detachBusy} onClick={confirmDetachDelete}>
              {detachBusy ? "处理中…" : "确认切断并删除"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
