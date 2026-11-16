import { useEffect, useState } from "react";
import { get, post } from "../../api";
import { entityResolutionLabel } from "../../lib/entityResolution";
import { pushError } from "../../lib/errorLog";
import { STYLE_HINT, TYPE_COLORS, TYPE_TEXT, regenStatusText, stopLabel } from "../../lib/testsetView";
import type { RagflowConnection, RegeneratePlan, Sample, TestsetDetail } from "../../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import ExportMenu, { type ExportFormat, type ExportScope } from "./ExportMenu";
import ProvenanceCard, { type PlanError } from "./ProvenanceCard";

/** 分类预览与样本列表：头部元信息、终态与账单、筛选、导出入口、逐条样本卡片。 */
export default function SampleList({
  detail, visible, busy,
  filterType, onFilterType, keyword, onKeyword, adoptOnly, onAdoptOnly,
  exporting, onExport, onToggleAdopt, affectedStatus,
  plan, planErr, onRegenerate, langOptions, onOpenTestset,
}: {
  detail: TestsetDetail;
  /** 已按题型 / 关键词 / 采纳状态过滤后的样本 */
  visible: Sample[];
  /** 有任务在跑：补生成入口禁用 */
  busy: boolean;
  filterType: string;
  onFilterType: (value: string) => void;
  keyword: string;
  onKeyword: (value: string) => void;
  adoptOnly: boolean;
  onAdoptOnly: (value: boolean) => void;
  exporting: boolean;
  onExport: (format: ExportFormat, scope: ExportScope) => void;
  onToggleAdopt: (sample: Sample) => void;
  /** 对照当前投影的判定结果：orphan / stale，用于样本卡片徽章 */
  affectedStatus: (index?: number | null) => string | undefined;
  plan: RegeneratePlan | null;
  planErr: PlanError | null;
  /** 局部补生成：名称与语言由本组件持有，页面只管发请求 */
  onRegenerate: (name: string, language: string) => void;
  langOptions: { value: string; label: string }[];
  onOpenTestset: (id: string) => void;
}) {
  /** 补生成结果名称（留空 = 后端用「原名·局部补生成」）与出题语言（留空 = 沿用原测试集） */
  const [regenName, setRegenName] = useState("");
  const [regenLang, setRegenLang] = useState("");

  return (
    <Card className="p-5 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="text-sm font-medium">{detail.name}</h2>
        <span className="text-xs text-muted-foreground">共 {detail.total} 条</span>
        {detail.partial && (
          <Badge variant="warning" size="xs">⚠ 部分结果</Badge>
        )}
        {/* 类型占比条 */}
        <div className="flex h-2 rounded-full overflow-hidden w-48">
          {Object.entries(detail.stats).map(([t, c]) => (
            <div key={t} style={{ width: `${detail.total ? (c / detail.total) * 100 : 0}%`, background: TYPE_COLORS[t] || "#8E8E96" }} />
          ))}
        </div>
      </div>

      {/* 模型与实体口径元信息：实体口径取生成时数据源投影的配置，存量测试集没有该字段（显示「未记录」） */}
      <div className="text-xs text-muted-foreground -mt-2">
        {(detail.llm_model || detail.embedding_model) && (
          <>模型：{detail.llm_model || "未记录"} / 向量：{detail.embedding_model || "未记录"}{" / "}</>
        )}
        实体口径：{entityResolutionLabel(detail.source_projection_config?.entity_resolution)}
      </div>

      {/* MOCK 数据告警条 */}
      {detail.is_mock && (
        <div className="bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 rounded-md p-3 text-xs text-amber-700 dark:text-amber-300 leading-relaxed">
          <div className="font-medium mb-1">⚠ 此测试集为 MOCK 数据，不可用于 RAG 评测</div>
          <div className="text-xs text-amber-700 dark:text-amber-300">
            问题来自硬编码模板，答案用 .format() 填占位符，reference_contexts 是假字符串。
            请安装 ragas（或确认 .venv 内 ragas 可导入）后重新生成。
          </div>
        </div>
      )}
      {detail.stale && <div className="text-xs text-amber-700 dark:text-amber-300 border-l-2 border-amber-300 dark:border-amber-700 pl-3">{detail.stale_sample_count || 0} 条样本来源已变化；历史内容仍可查看和导出。</div>}
      {detail.source_unbound && (
        <UnboundSourceNotice detail={detail} onRebound={() => onOpenTestset(detail.id)} />
      )}
      {detail.projection_missing && !detail.source_unbound && (
        <div className="bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 rounded-md p-3 text-xs text-amber-700 dark:text-amber-300 leading-relaxed">
          <div className="font-medium mb-1">⚠ 投影图谱已删除，本测试集暂时失去图谱关联</div>
          <div>
            题目本身仍可查看、导出与评测；预览原图、质检与按旧图补生成在重新同步前不可用。
            到「知识图谱」页对该数据源执行一次同步即可重建投影（不消耗 LLM token），
            同步成功后此提示自动消失，无需改绑。
          </div>
        </div>
      )}
      {detail.regeneration && (
        <div className="text-xs text-muted-foreground border-l-2 border-sky-300 dark:border-sky-700 pl-3">
          由
          <button className="mx-1 text-primary-strong hover:underline" onClick={() => onOpenTestset(detail.regeneration!.from_testset_id)}>
            「{detail.regeneration.from_testset_name || detail.regeneration.from_testset_id}」
          </button>
          局部补生成：替换 {detail.regeneration.replaced_indices.length} 条
          （{regenStatusText(detail.regeneration.replaced_by_status)}），原样保留 {detail.regeneration.kept} 条；
          下方 token 账单只含本次补生成。
        </div>
      )}
      <ProvenanceCard plan={plan} error={planErr} busy={busy}
        onRegenerate={() => onRegenerate(regenName, regenLang)}
        name={regenName} onNameChange={setRegenName}
        language={regenLang} onLanguageChange={setRegenLang} langOptions={langOptions} />
      {/* 生成终态 / 部分结果的差额 / 失败条数：历史测试集没有这些字段时中性显示「未记录」，不报错 */}
      <div className={`rounded-md border px-3 py-2 text-xs leading-relaxed space-y-1 ${
        detail.partial
          ? "border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-200"
          : detail.stop_reason === "completed"
            ? "border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-800 dark:text-emerald-200"
            : "border-border bg-background text-muted-foreground"
      }`}>
        <div className="flex items-center gap-2 flex-wrap">
          <span>生成终态：<span className="font-medium">{stopLabel(detail.stop_reason)}</span></span>
          {detail.testset_size
            ? <span>请求 {detail.testset_size} 条，实际 {detail.total} 条</span>
            : <span>实际 {detail.total} 条（历史记录未保存请求条数）</span>}
          {!!detail.failed_samples && (
            <Badge variant="danger" size="xs">
              {detail.failed_samples} 条生成失败已跳过
            </Badge>
          )}
        </div>
        {detail.partial && (
          <div className="text-xs text-muted-foreground">
            这批题只出了一部分，不能当完整测试集使用。
            {detail.undelivered && (
              <>少出 {detail.undelivered.total_missing} 条（其中场景不足 {detail.undelivered.not_planned ?? 0} 条、样本生成失败 {detail.undelivered.not_delivered ?? 0} 条）。</>
            )}
            失败条目的单条原因未随测试集落盘，仅生成过程中的日志可见。
          </div>
        )}
        {detail.error && <div className="text-xs break-all">错误：{detail.error}</div>}
      </div>
      {!!detail.usage?.total_tokens && (
        <div className="text-xs text-muted-foreground">
          token 合计 <span className="font-mono text-foreground">{detail.usage.total_tokens.toLocaleString()}</span>
          （LLM 调用 {detail.usage.llm_calls ?? 0} 次）
          {!!detail.usage.enhancement?.total_tokens && (
            <>，其中生成前增强 <span className="font-mono text-amber-700 dark:text-amber-300">{detail.usage.enhancement.total_tokens.toLocaleString()}</span>
            （LLM {(detail.usage.enhancement.llm_tokens ?? 0).toLocaleString()} + 向量 {(detail.usage.enhancement.embedding_tokens ?? 0).toLocaleString()}）</>
          )}
        </div>
      )}
      {/* 类型图例 —— 占比条下方 */}
      <div className="flex items-center gap-4 flex-wrap -mt-2">
        {Object.entries(detail.stats).map(([t, c]) => (
          <span key={t} className="text-xs text-muted-foreground whitespace-nowrap">
            <span className="inline-block w-2 h-2 rounded-full mr-1" style={{ background: TYPE_COLORS[t] || "#8E8E96" }} />
            {t}: {c}（{(detail.total ? Math.round((c / detail.total) * 100) : 0)}%）
          </span>
        ))}
      </div>

      {/* 筛选栏 */}
      <div className="flex items-center gap-3 flex-wrap">
        <select aria-label="按题型筛选" className="w-56" value={filterType} onChange={(e) => onFilterType(e.target.value)}>
          <option value="">全部类型</option>
          {Object.keys(detail.groups).map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <Label htmlFor="testset-keyword-filter" className="sr-only">按关键词筛选</Label>
        <Input id="testset-keyword-filter" className="!w-56" placeholder="按关键词筛选 query…" value={keyword} onChange={(e) => onKeyword(e.target.value)} />
        <label className="flex items-center gap-1.5 text-xs cursor-pointer whitespace-nowrap">
          <input type="checkbox" checked={adoptOnly} onChange={(e) => onAdoptOnly(e.target.checked)} className="accent-primary size-4" />
          仅看已采纳
        </label>
        <div className="ml-auto">
          <ExportMenu disabled={exporting} onExport={onExport} />
        </div>
      </div>

      {/* 样本卡片 */}
      <div className="grid gap-3">
        {visible.map((s) => (
          <div key={s.index} className="bg-background border border-border rounded-md p-4 space-y-2">
            <div className="flex items-center gap-2">
              <Badge variant="outline" size="pill"
                className={`border-0 bg-transparent ${TYPE_TEXT[s.synthesizer_name] || "text-muted-foreground"}`}
                style={{ background: (TYPE_COLORS[s.synthesizer_name] || "#8E8E96") + "1A", border: `1px solid ${TYPE_COLORS[s.synthesizer_name] || "#8E8E96"}55` }}>
                {s.synthesizer_name}
              </Badge>
              <span className="text-[11px] text-muted-foreground">#{s.index}</span>
              {/* 提问风格：ragas 4 挡里 3 挡是故意不通顺的，标出来才不会误以为是模型坏了 */}
              {s.query_style && (
                <Badge
                  variant={STYLE_HINT[s.query_style]?.bad ? "warning" : "outline"}
                  size="xs"
                  className={STYLE_HINT[s.query_style]?.bad ? "" : "bg-muted text-muted-foreground"}
                  title={STYLE_HINT[s.query_style]?.tip || s.query_style}>
                  {STYLE_HINT[s.query_style]?.label || s.query_style}
                  {s.query_length ? ` · ${s.query_length}` : ""}
                </Badge>
              )}
              {s.persona_name && (
                <span className="text-xs text-muted-foreground" title="ragas 生成的虚构提问人设定，会被写进题目">
                  人格: {s.persona_name}
                </span>
              )}
              {s.is_mock && (
                <Badge variant="warning" size="xs">
                  MOCK
                </Badge>
              )}
              {s.stale && !affectedStatus(s.index) && <Badge variant="warning" size="xs">来源已变化</Badge>}
              {affectedStatus(s.index) && (
                <Badge variant="warning" size="xs"
                  title="对照数据源当前图谱的判定；可用上方「局部补生成」只重出这些题">
                  {affectedStatus(s.index) === "orphan" ? "来源已删除" : "内容已变"}
                </Badge>
              )}
              {s.regenerated && <Badge variant="info" size="xs">补生成</Badge>}
              <Button size="xs" variant="outline" className={`ml-auto rounded ${s.adopted ? "border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300" : "text-muted-foreground"}`}
                onClick={() => onToggleAdopt(s)}>
                {s.adopted ? "✓ 已采纳" : "丢弃"}
              </Button>
            </div>
            <div className="text-sm">{s.user_input}</div>
            <div className="text-xs text-muted-foreground">
              <span className="text-primary-strong" title="ragas 字段名 reference —— 出题时依据上下文同时生成的参考答案">
                标准答案：
              </span>
              {s.reference}
            </div>
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer">参考上下文（{s.reference_contexts.length}）</summary>
              {s.reference_contexts.map((c, i) => <div key={i} className="mt-1 text-muted-foreground">{c}</div>)}
            </details>
          </div>
        ))}
        {visible.length === 0 && <div className="text-sm text-muted-foreground py-4 text-center">无匹配样本。</div>}
      </div>
    </Card>
  );
}

/** 数据源已停止跟踪（原连接被删或已失效）：显式告警 + 一键改绑到另一条连接。
 * 后端 rebind 会先备份旧 source 目录再改写归属，失败自动回滚。 */
function UnboundSourceNotice({ detail, onRebound }: { detail: TestsetDetail; onRebound: () => void }) {
  const [conns, setConns] = useState<RagflowConnection[]>([]);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const canRebind = !!detail.source_dataset_id && !!detail.source_connection_id;

  useEffect(() => {
    get<RagflowConnection[]>("/api/ragflow/connections")
      .then((list) => { setConns(list); setTarget(list[0]?.id || ""); })
      .catch((e) => pushError("测试集·重新绑定", "读取 RAGFlow 连接列表失败", String(e)));
  }, []);

  const rebind = async () => {
    if (!canRebind || !target) return;
    setBusy(true); setNote(null);
    try {
      const r = await post<{ testset_ids: string[] }>(
        `/api/ragflow/connections/${target}/datasets/${encodeURIComponent(detail.source_dataset_id!)}/rebind`,
        { from_connection_id: detail.source_connection_id });
      setNote(`已改绑 ${r.testset_ids?.length ?? 0} 个测试集`);
      onRebound();
    } catch (e) {
      setNote(`重新绑定失败：${String(e)}`);
      pushError("测试集·重新绑定", "重新绑定数据源失败", String(e));
    } finally { setBusy(false); }
  };

  return (
    <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs leading-relaxed space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant="warning">已停止跟踪</Badge>
        <span className="text-foreground font-medium">
          {detail.source_unbound_reason === "connection_deleted" ? "原连接已删除" : "原连接已不存在"}
        </span>
      </div>
      <div className="text-muted-foreground">
        这批题仍可查看与导出，但不会再跟随 RAGFlow 同步；来源检查也无法对照数据源当前图谱。
        {canRebind ? "可改绑到另一条能看到该知识库的连接。" : "记录里没有原连接信息，请用新连接重新同步。"}
      </div>
      {canRebind && (
        <div className="flex items-center gap-2 flex-wrap pt-0.5">
          <select className="w-60" value={target} onChange={(e) => setTarget(e.target.value)}
            disabled={busy || conns.length === 0} aria-label="选择要改绑到的连接">
            {conns.length === 0 && <option value="">暂无可用连接</option>}
            {conns.map((c) => <option key={c.id} value={c.id}>{c.name}（{c.base_url}）</option>)}
          </select>
          <Button size="xs" variant="outline" disabled={busy || !target} onClick={rebind}>
            {busy ? "改绑中…" : "重新绑定"}
          </Button>
          {note && <span className="text-muted-foreground">{note}</span>}
        </div>
      )}
    </div>
  );
}
