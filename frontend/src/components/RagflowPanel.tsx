import { useCallback, useEffect, useState, type ReactNode } from "react";
import { get, post, put } from "../api";
import type { ApiError } from "../api";
import { pushError, pushInfo } from "../lib/errorLog";
import {
  ENTITY_SWITCH_HINT_OFF, ENTITY_SWITCH_HINT_ON, ENTITY_SWITCH_OFF, ENTITY_SWITCH_ON,
  entityResolutionLabel, entityStatsText, entitySwitchOn, entitySwitchState, pendingText,
} from "../lib/entityResolution";
import { errorText, ragflowVersionText, verdictAdvice } from "../lib/ragflowText";
import { useTrackedTask } from "../lib/useTrackedTask";
import TaskProgress from "./TaskProgress";
import type {
  KgCapabilities, Preset, RagflowConnection, RagflowDataset, RagflowDatasetList, RagflowEntitySettings,
  RagflowProbeResult, RagflowSnapshot, RagflowStage, RagflowSyncResult,
} from "../types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";

/** 7 个阶段固定顺序（后端 §4.3 保证不省略，前端按此渲染） */
const STAGE_ORDER = ["connect", "auth", "datasets", "documents", "chunks", "kb_graph", "document_graph"];

/** 每行只展示该阶段自己的计数，避免 kb_graph 携带的全量 counts 重复显示 */
const STAGE_COUNTS: Record<string, string[]> = {
  connect: [],
  auth: [],
  datasets: ["datasets"],
  documents: ["documents"],
  chunks: ["sampled_documents", "chunks"],
  kb_graph: ["graph_nodes", "graph_edges"],
  document_graph: ["sampled_graphs", "entity_sources"],
};

const COUNT_LABELS: Record<string, string> = {
  datasets: "知识库",
  documents: "文档",
  sampled_documents: "可读文档",
  unfinished_documents: "未解析完",
  chunks: "分块",
  graph_nodes: "实体",
  graph_edges: "关系",
  sampled_graphs: "有图谱文档",
  entity_sources: "带来源实体",
};

const VERDICT_LABEL: Record<string, string> = {
  ready: "可用于出题",
  partial: "部分可用",
  graph_empty: "图谱为空",
  auth_failed: "鉴权失败",
  incompatible: "接口不兼容",
  unreachable: "连不上",
};

function statusIcon(status: RagflowStage["status"]) {
  if (status === "ok") return <span className="text-emerald-700 dark:text-emerald-300">✓</span>;
  if (status === "fail") return <span className="text-red-600 dark:text-red-400">✗</span>;
  return <span className="text-muted-foreground">·</span>;
}

interface Props {
  /** 图谱生成任务进行中 → 禁止再发起同步/探测 */
  busy: boolean;
  syncTaskId: string | null;
  setSyncTaskId: (id: string | null) => void;
  /** 同步任务终态回调（由 KgPage 负责刷新列表并预览新图谱） */
  onSyncDone: (result: Record<string, unknown> | null, error?: string) => void;
}

export default function RagflowPanel({ busy, syncTaskId, setSyncTaskId, onSyncDone }: Props) {
  const [conns, setConns] = useState<RagflowConnection[]>([]);
  const [connId, setConnId] = useState("");
  const [datasets, setDatasets] = useState<RagflowDataset[]>([]);
  const [dsId, setDsId] = useState("");
  const [dsLoading, setDsLoading] = useState(false);
  const [dsNotice, setDsNotice] = useState<string | null>(null);
  const [probeTaskId, setProbeTaskId] = useTrackedTask("ragflow_probe", ["ragflow_probe"]);
  const [probe, setProbe] = useState<RagflowProbeResult | null>(null);
  const [probeErr, setProbeErr] = useState<string | null>(null);
  const [syncReport, setSyncReport] = useState<{ result: RagflowSyncResult; caps: KgCapabilities | null } | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [fullRefresh, setFullRefresh] = useState(false);
  const [legacyForm, setLegacyForm] = useState({ base_url: "", dataset_id: "", api_key: "", name: "" });
  /** 实体口径开关：按「连接 + 知识库」独立存储，界面只暴露二态（关 / 开=hybrid） */
  const [presets, setPresets] = useState<Preset[]>([]);
  const [entitySettings, setEntitySettings] = useState<RagflowEntitySettings | null>(null);
  const [entitySaving, setEntitySaving] = useState(false);
  const [entityErr, setEntityErr] = useState<string | null>(null);
  /** 当前数据源的快照摘要：undefined=还没读（未选库）；ok/missing/error 三态，缺省与读取失败分开 */
  const [snapshot, setSnapshot] = useState<
    { status: "ok"; snapshot: RagflowSnapshot } | { status: "missing" | "error" } | undefined
  >(undefined);

  useEffect(() => {
    get<Preset[]>("/api/presets")
      .then(setPresets)
      .catch((e) => pushError("RAGFlow·实体口径", "读取 LLM 预设失败", String(e)));
  }, []);

  const loadConns = useCallback(
    () => get<RagflowConnection[]>("/api/ragflow/connections").then(setConns),
    [],
  );
  useEffect(() => {
    loadConns().catch((e) => pushError("RAGFlow·连接", "读取连接列表失败", String(e)));
  }, [loadConns]);

  // 连接列表变化时保证选中项仍然有效
  useEffect(() => {
    if (!conns.length) { setConnId(""); return; }
    if (!conns.some((c) => c.id === connId)) setConnId(conns[0].id);
  }, [conns, connId]);

  const currentConn = conns.find((c) => c.id === connId);

  // 连接变化 → 拉该连接可见的知识库
  useEffect(() => {
    if (!connId) { setDatasets([]); setDsId(""); setDsNotice(null); return; }
    let cancelled = false;
    setDsLoading(true);
    setDsNotice(null);
    setProbeErr(null);
    get<RagflowDatasetList>(`/api/ragflow/connections/${connId}/datasets?page=1&page_size=100`)
      .then((r) => {
        if (cancelled) return;
        // 历史连接只能用它原来的知识库：锁定到该库，不再默认选第一个
        const lockedId = currentConn?.legacy ? currentConn.dataset_id : undefined;
        const items = lockedId ? r.items.filter((d) => d.id === lockedId) : r.items;
        setDatasets(items);
        setDsId(items[0]?.id ?? "");
        if (lockedId && items.length === 0) {
          setDsNotice(`历史连接绑定的知识库 ${lockedId} 在该账号下不可见；如需同步其他知识库请新建连接`);
        } else if (items.length === 0) setDsNotice("该连接下没有可见知识库，请回 RAGFlow 确认权限");
        else if (lockedId) setDsNotice("历史连接只能同步它原来的知识库；同步其他知识库请新建连接");
      })
      .catch((e) => {
        if (cancelled) return;
        setDatasets([]);
        setDsId("");
        setDsNotice(errorText(e, currentConn?.base_url ?? ""));
      })
      .finally(() => { if (!cancelled) setDsLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connId]);

  // 换知识库时清掉错误提示；检查结果不清空——它自带连接与知识库，只在对应的库下显示（N14：
  // 刷新后恢复的检查任务可能先于知识库列表返回，旧实现在列表到达时把结果清掉，任务已结束、再也取不回）
  useEffect(() => { setProbeErr(null); }, [dsId]);

  const startProbe = async () => {
    if (!connId || !dsId) return;
    setProbeErr(null);
    setProbe(null);
    try {
      const r = await post<{ task_id: string }>(
        `/api/ragflow/connections/${connId}/datasets/${encodeURIComponent(dsId)}/probe`, {},
      );
      setProbeTaskId(r.task_id);
    } catch (e) {
      const text = errorText(e, currentConn?.base_url ?? "");
      setProbeErr(text);
      pushError("RAGFlow·检查图谱", "发起检测失败", text);
    }
  };

  const startSync = async () => {
    if (!connId || !dsId) return;
    setSyncReport(null);
    try {
      const r = await post<{ task_id: string }>(
        `/api/ragflow/connections/${connId}/datasets/${encodeURIComponent(dsId)}/sync`,
        { full_refresh: fullRefresh },
      );
      setSyncTaskId(r.task_id);
    } catch (e) {
      const text = errorText(e, currentConn?.base_url ?? "");
      setDsNotice(text);
      pushError("RAGFlow·同步", "发起同步失败", text);
    }
  };

  const syncLegacy = async () => {
    if (!legacyForm.base_url.trim() || !legacyForm.dataset_id.trim()) {
      pushError("RAGFlow·高级同步", "请填写 RAGFlow 地址和知识库 ID");
      return;
    }
    try {
      const r = await post<{ task_id: string }>("/api/ragflow/sync", {
        ...legacyForm,
        base_url: legacyForm.base_url.trim(),
        dataset_id: legacyForm.dataset_id.trim(),
        api_key: legacyForm.api_key || undefined,
        full_refresh: fullRefresh,
      });
      setSyncTaskId(r.task_id);
      setLegacyForm((f) => ({ ...f, api_key: "" }));
    } catch (e) {
      pushError("RAGFlow·高级同步", "发起同步失败", String(e));
    }
  };

  const canPick = !!connId && !!dsId && !dsLoading;
  const syncing = !!syncTaskId;
  const probing = !!probeTaskId;
  const disabled = busy || syncing || probing;

  // 实体口径按「连接 + 知识库」独立保存：设置接口只读本地配置，选库后不需要拉 RAGFlow
  const entitySettingsUrl = (connId && dsId)
    ? currentConn?.legacy
      ? `/api/ragflow/datasets/${encodeURIComponent(dsId)}/settings`
      : `/api/ragflow/connections/${connId}/datasets/${encodeURIComponent(dsId)}/settings`
    : null;
  const entityOn = entitySwitchOn(entitySettings?.entity_resolution);
  // 开关只切二态：优先沿用已保存的预设，其次默认的 llm_testset（与测试集页的选择口径一致）
  const llmTestsetPresets = presets.filter((p) => p.kind === "llm_testset");
  const effectivePresetId = entitySettings?.llm_preset_id
    || llmTestsetPresets.find((p) => p.is_default)?.id
    || llmTestsetPresets[0]?.id
    || "";
  const switchState = entitySwitchState({
    hasDataset: !!dsId,
    legacy: !!currentConn?.legacy,
    busy: disabled || entitySaving,
    hasPreset: !!effectivePresetId,
  });
  const entityPending = entitySettings ? pendingText(entitySettings.pending_rebuild) : null;

  useEffect(() => {
    if (!entitySettingsUrl) { setEntitySettings(null); setEntityErr(null); return; }
    let cancelled = false;
    setEntitySettings(null);
    setEntityErr(null);
    get<RagflowEntitySettings>(entitySettingsUrl)
      .then((r) => { if (!cancelled) setEntitySettings(r); })
      .catch((e) => {
        if (cancelled) return;
        const text = errorText(e, currentConn?.base_url ?? "");
        setEntityErr(text);
        pushError("RAGFlow·实体口径", "读取实体口径设置失败", text);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entitySettingsUrl]);

  // 已同步数据源：读当前快照摘要拿 ragflow_version（本地文件，选库即读；同步结束后重读一次）。
  // legacy 连接的同步一直写旧布局（routers/ragflow.py 传 connection_id=None），快照只在旧接口，
  // 所以直接请求旧接口，不再先打一次注定 404 的新布局地址（与实体口径设置的取法一致）。
  const snapshotUrl = (connId && dsId)
    ? currentConn?.legacy
      ? `/api/ragflow/datasets/${encodeURIComponent(dsId)}/snapshot`
      : `/api/ragflow/sources/${encodeURIComponent(`${connId}__${dsId}`)}/snapshot`
    : null;
  useEffect(() => {
    if (!snapshotUrl) { setSnapshot(undefined); return; }
    let cancelled = false;
    setSnapshot(undefined);
    get<RagflowSnapshot>(snapshotUrl)
      .then((r) => { if (!cancelled) setSnapshot({ status: "ok", snapshot: r }); })
      .catch((e) => {
        if (cancelled) return;
        // 只有「快照不存在」才算没同步过；500 / 网络错误要说读取失败，不能冒充「还没同步」
        const missing = (e as ApiError | null)?.errorCode === "snapshot_not_found";
        setSnapshot({ status: missing ? "missing" : "error" });
      });
    return () => { cancelled = true; };
  }, [snapshotUrl, syncTaskId]);

  /** 切换开关只写配置，不触发同步 / 检查：口径在下次同步时才会重建投影 */
  const toggleEntitySwitch = async (on: boolean) => {
    if (!entitySettingsUrl) return;
    setEntityErr(null);
    setEntitySaving(true);
    try {
      const r = await put<RagflowEntitySettings>(entitySettingsUrl, {
        entity_resolution: on ? ENTITY_SWITCH_ON : ENTITY_SWITCH_OFF,
        // 关闭时保留已选预设，下次开启不必重选（后端只在开启态校验预设）
        llm_preset_id: on ? effectivePresetId : (entitySettings?.llm_preset_id ?? null),
      });
      setEntitySettings(r);
      pushInfo("RAGFlow·实体口径", on ? "已开启，下次同步会按新口径重建投影" : "已关闭，下次同步会恢复原口径");
    } catch (e) {
      const text = errorText(e, currentConn?.base_url ?? "");
      setEntityErr(text);
      pushError("RAGFlow·实体口径", "保存实体口径设置失败", text);
    } finally { setEntitySaving(false); }
  };

  // 检查结果只挂在它所属的「连接 + 知识库」下；属于别的库时给一条可切换的提示，不张冠李戴
  const probeMatches = (p: RagflowProbeResult) =>
    p.dataset_id === dsId && (!p.connection_id || p.connection_id === connId);
  const shownProbe = probe && probeMatches(probe) ? probe : null;
  const otherProbe = probe && !shownProbe ? probe : null;
  const otherProbeDataset = otherProbe && (!otherProbe.connection_id || otherProbe.connection_id === connId)
    ? datasets.find((d) => d.id === otherProbe.dataset_id) : undefined;

  const stages = shownProbe
    ? STAGE_ORDER
        .map((key) => shownProbe.stages.find((s) => s.key === key))
        .filter((s): s is RagflowStage => !!s)
    : [];

  const finishSync = (result: Record<string, unknown> | null, error?: string) => {
    if (!error && result?.kg_id) {
      const r = result as unknown as RagflowSyncResult;
      setSyncReport({ result: r, caps: null });
      get<KgCapabilities>(`/api/testset/capabilities/${encodeURIComponent(r.kg_id)}`)
        .then((caps) => setSyncReport((cur) => (cur?.result.kg_id === r.kg_id ? { ...cur, caps } : cur)))
        .catch((e) => pushError("RAGFlow·同步报告", "读取可出题能力失败", String(e)));
    }
    onSyncDone(result, error);
  };

  return (
    <Card className="p-5 space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-sm font-medium">从 RAGFlow 同步</h2>
        <span className="text-xs text-muted-foreground">先选连接，再选知识库；读不到图时先点「检查图谱」</span>
      </div>

      {conns.length === 0 && (
        <div className="text-xs text-muted-foreground">还没有 RAGFlow 连接。请到「模型配置」页新增一条连接后再回来同步。</div>
      )}

      {conns.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <Label>RAGFlow 连接</Label>
            <select aria-label="RAGFlow 连接" value={connId} onChange={(e) => setConnId(e.target.value)} disabled={disabled}>
              {conns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}{c.legacy ? "（历史配置）" : ""}
                </option>
              ))}
            </select>
          </div>
          <div>
            <Label>知识库{dsLoading ? "（读取中…）" : ""}</Label>
            <select aria-label="知识库"
              value={dsId}
              onChange={(e) => setDsId(e.target.value)}
              disabled={disabled || !canPick}
            >
              {datasets.length === 0 && <option value="">（无可选知识库）</option>}
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}{d.doc_count != null ? `（${d.doc_count} 篇）` : ""}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      {dsNotice && <div className="text-xs text-amber-700 dark:text-amber-300">{dsNotice}</div>}

      {/* 已同步数据源的版本锚定：显示上次同步快照记录的 RAGFlow 版本；
          历史快照没有该字段时写「快照未记录版本」，不拿当前值冒充 */}
      {conns.length > 0 && dsId && snapshot !== undefined && (
        <div className="text-xs text-muted-foreground" data-testid="ragflow-snapshot-version">
          {snapshot.status === "ok"
            ? `上次同步快照：${ragflowVersionText(snapshot.snapshot.ragflow_version, "快照未记录版本")}`
              + (snapshot.snapshot.fetched_at ? ` · ${snapshot.snapshot.fetched_at}` : "")
            : snapshot.status === "missing"
              ? "该知识库还没有同步过快照"
              : "快照版本读取失败，可稍后重试（不影响同步 / 出题）"}
        </div>
      )}

      {/* 实体口径是库级配置：切换只落配置，不自动同步；口径在下次同步时固化到投影里 */}
      {conns.length > 0 && dsId && (
        <div className="rounded-md border border-border bg-background p-3 space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <label className="flex items-center gap-2 text-sm whitespace-nowrap">
              <input
                type="checkbox"
                checked={entityOn}
                disabled={switchState.disabled}
                onChange={(e) => { void toggleEntitySwitch(e.target.checked); }}
                className="accent-primary size-4"
              />
              同步时合并同义实体
            </label>
            {entitySettings && (
              <Badge variant={entityOn ? "info" : "outline"} size="pill" className="px-1.5">
                {entityResolutionLabel(entitySettings.entity_resolution)}
              </Badge>
            )}
            {entitySaving && <span className="text-xs text-muted-foreground">保存中…</span>}
            {entityPending && <span className="text-xs text-amber-700 dark:text-amber-300">{entityPending}</span>}
          </div>
          <div className="text-xs text-muted-foreground">
            {entityOn ? ENTITY_SWITCH_HINT_ON : ENTITY_SWITCH_HINT_OFF}
          </div>
          {switchState.reason && <div className="text-xs text-muted-foreground">{switchState.reason}</div>}
          {entityErr && <div className="text-xs text-red-600 dark:text-red-400">{entityErr}</div>}
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap">
        {/* R7：复选框与标签一行，位置在「同步到工作台」左侧，不再 ml-auto 贴右 */}
        <label className="flex items-center gap-2 text-sm text-muted-foreground whitespace-nowrap">
          <input type="checkbox" checked={fullRefresh} onChange={(e) => setFullRefresh(e.target.checked)}
            className="accent-primary size-4" />
          强制重新读取全部文档
        </label>
        {conns.length > 0 && (
          <>
            <Button variant="outline" onClick={startProbe} disabled={disabled || !canPick}>
              {probing ? "检测中…" : "检查图谱"}
            </Button>
            <Button onClick={startSync} disabled={disabled || !canPick}>
              {syncing ? "同步中…" : "同步到工作台"}
            </Button>
          </>
        )}
      </div>

      {probeErr && <div className="text-xs text-red-600 dark:text-red-400">{probeErr}</div>}

      {otherProbe && (
        <div className="text-xs text-muted-foreground flex items-center gap-2 flex-wrap">
          <span>上次「检查图谱」的结果属于{otherProbeDataset ? `知识库「${otherProbeDataset.name}」` : "另一个连接或知识库"}。</span>
          {otherProbeDataset && (
            <Button variant="outline" size="xs" disabled={disabled} onClick={() => setDsId(otherProbeDataset.id)}>
              切换过去查看
            </Button>
          )}
        </div>
      )}

      {shownProbe && (
        <div className="rounded-md border border-border bg-background p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-medium">检查结果</span>
            <Badge
              variant={shownProbe.verdict === "ready" ? "success" : shownProbe.verdict === "partial" ? "warning" : "danger"}
              size="pill" className="px-1.5"
            >{shownProbe.error_code === "dataset_not_found" ? "找不到知识库" : (VERDICT_LABEL[shownProbe.verdict] || shownProbe.verdict)}</Badge>
            <span className="text-xs text-muted-foreground">
              共 {shownProbe.request_count} 次请求{shownProbe.stopped_reason ? "（达上限提前停止）" : ""}
            </span>
          </div>
          <div className="divide-y divide-border/60">
            {stages.map((s) => (
              <div key={s.key} className="flex items-start gap-3 py-1.5">
                <span className="w-4 shrink-0 text-xs leading-5">{statusIcon(s.status)}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs">{s.label}</span>
                    {s.status === "skipped" && <span className="text-xs text-muted-foreground">未检查</span>}
                    {s.upstream_status != null && <span className="text-xs text-muted-foreground">HTTP {s.upstream_status}</span>}
                  </div>
                  <div className="text-xs text-muted-foreground break-words">{s.detail}</div>
                </div>
                <div className="flex gap-1 flex-wrap justify-end">
                  {STAGE_COUNTS[s.key]?.map((k) => (
                    s.counts?.[k] != null ? (
                      <span key={k} className="text-xs px-1.5 py-0.5 rounded border border-border text-muted-foreground whitespace-nowrap">
                        {COUNT_LABELS[k] || k} {s.counts[k]}
                      </span>
                    ) : null
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div className="text-xs text-amber-700 dark:text-amber-300">按此原因处理：{verdictAdvice(shownProbe, currentConn?.base_url ?? "")}</div>
        </div>
      )}

      {syncReport && <SyncReportView report={syncReport.result} caps={syncReport.caps} onClose={() => setSyncReport(null)} />}

      <button className="text-xs text-muted-foreground hover:text-primary-strong rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card" onClick={() => setAdvanced(!advanced)}>
        {advanced ? "收起高级选项" : "高级：直接填知识库 ID"}
      </button>

      {advanced && (
        <div className="space-y-3 border-t border-border pt-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div><Label>RAGFlow 地址</Label>
              <Input aria-label="RAGFlow 地址" value={legacyForm.base_url} onChange={(e) => setLegacyForm((f) => ({ ...f, base_url: e.target.value }))} placeholder="http://127.0.0.1:9380" /></div>
            <div><Label>知识库 ID</Label>
              <Input aria-label="知识库 ID" value={legacyForm.dataset_id} onChange={(e) => setLegacyForm((f) => ({ ...f, dataset_id: e.target.value }))} /></div>
            <div><Label>API Key</Label>
              <Input aria-label="API Key" type="password" autoComplete="off" value={legacyForm.api_key} onChange={(e) => setLegacyForm((f) => ({ ...f, api_key: e.target.value }))} placeholder="已配置时可留空" /></div>
            <div><Label>投影名称（可选）</Label>
              <Input aria-label="投影名称（可选）" value={legacyForm.name} onChange={(e) => setLegacyForm((f) => ({ ...f, name: e.target.value }))} /></div>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <Button variant="outline" size="xs" onClick={syncLegacy} disabled={disabled}>手动同步（旧接口）</Button>
            <span className="text-xs text-muted-foreground">沿用 POST /api/ragflow/sync；已配置过地址与 key 的环境可继续使用。</span>
          </div>
        </div>
      )}

      <TaskProgress
        taskId={probeTaskId}
        onDone={(result, error) => {
          setProbeTaskId(null);
          if (error) { setProbeErr(error); return; }
          setProbe((result as unknown as RagflowProbeResult) ?? null);
          pushInfo("RAGFlow·检查图谱", "检测完成");
        }}
        onCancel={async () => {
          if (probeTaskId) await post(`/api/tasks/${probeTaskId}/cancel`).catch(() => {});
        }}
      />

      <TaskProgress
        taskId={syncTaskId}
        onDone={finishSync}
        onCancel={async () => {
          if (syncTaskId) await post(`/api/tasks/${syncTaskId}/cancel`).catch(() => {});
        }}
      />
    </Card>
  );
}

/** 能力判定的缺项文案很长（带「—— 原因」），报告里只留素材名；完整原因在测试集页查看 */
const shortMissing = (missing: string[]) =>
  missing.map((m) => m.split(/——|；|（当前/)[0].trim()).join("、");

/** 同步报告：读取情况 / 来源质量 / 本次变化 / 可出题能力分开展示 */
function SyncReportView({ report, caps, onClose }: {
  report: RagflowSyncResult; caps: KgCapabilities | null; onClose: () => void;
}) {
  const r = report.report;
  const stale = report.stale_testset_ids?.length ?? 0;
  const retained = report.retained_kg_ids?.length ?? 0;
  const reused = r?.changes.reused_projection ?? !!report.reused;
  const withEntities = r?.source_quality.chunks_with_entities ?? 0;
  const chunkTotal = r?.source_quality.chunks ?? 0;
  const pct = chunkTotal ? Math.round((withEntities / chunkTotal) * 100) : 0;
  // 关闭态返回 null，报告里不出现这一行（与关闭时行为一致）
  const entityLine = entityStatsText(report.entity_resolution);
  const row = (label: string, body: ReactNode) => (
    <div className="flex items-start gap-3 py-1.5">
      <span className="w-16 shrink-0 text-xs text-muted-foreground">{label}</span>
      <div className="min-w-0 flex-1 text-xs break-words">{body}</div>
    </div>
  );
  return (
    <div className="rounded-md border border-border bg-background p-3 space-y-1">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-medium">同步报告</span>
        <Badge variant={reused ? "info" : "success"} size="pill" className="px-1.5">
          {reused ? "内容未变，复用原图谱" : "已生成新图谱"}
        </Badge>
        <Button variant="ghost" size="xs" className="ml-auto" onClick={onClose}>关闭</Button>
      </div>
      <div className="divide-y divide-border/60">
        {r && row("读取", <>
          文档 {r.read.documents} 篇、分块 {r.read.chunks} 个
          {r.read.unfinished_documents > 0 && (
            <span className="text-amber-700 dark:text-amber-300">；{r.read.unfinished_documents} 篇还没解析完，读不到分块，解析完后再同步一次</span>
          )}
        </>)}
        {r && row("来源质量", <>
          带实体的分块 {withEntities}/{chunkTotal}（{pct}%）
          {chunkTotal > 0 && withEntities === 0 && (
            <span className="text-amber-700 dark:text-amber-300">；分块都没有实体，单跳具体题、多跳具体题出不了，请先在 RAGFlow 编译图谱</span>
          )}
        </>)}
        {entityLine && row("实体口径", entityLine)}
        {r && row("本次变化", <>
          新增 {r.changes.added_chunks}、变化 {r.changes.changed_chunks}、删除 {r.changes.deleted_chunks} 个分块
          {r.changes.deleted_documents > 0 && `；删除 ${r.changes.deleted_documents} 篇文档`}
          {(r.changes.inherited_chunks > 0 || r.changes.inherited_documents > 0) &&
            `；沿用旧图谱的增强成果 ${r.changes.inherited_chunks} 个分块、${r.changes.inherited_documents} 篇文档（不再重复花 token）`}
        </>)}
        {row("测试集", stale > 0
          ? <span className="text-amber-700 dark:text-amber-300">{stale} 个历史测试集引用了变化的内容，已标为过期</span>
          : "没有测试集受影响"
          + (retained > 0 ? `；保留 ${retained} 个仍被测试集引用的旧图谱` : ""))}
        {row("可出题", caps
          ? (
            <div className="space-y-0.5">
              {Object.entries(caps.synthesizers).map(([key, s]) => (
                <div key={key}>
                  <span className={s.available ? "text-emerald-700 dark:text-emerald-300" : s.effective_available ? "text-amber-700 dark:text-amber-300" : "text-red-600 dark:text-red-400"}>
                    {s.available ? "✓" : s.effective_available ? "◐" : "✗"} {s.label}
                  </span>
                  {!s.available && (
                    <span className="text-muted-foreground">
                      ：{s.effective_available ? "生成前会自动补齐 " : "缺少 "}{shortMissing(s.missing)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )
          : <span className="text-muted-foreground">读取中…</span>)}
      </div>
    </div>
  );
}
