import { DIST_KEYS, MAX_TESTSET_NAME_LEN, matchType, testsetNameError } from "../../lib/testsetView";
import type { KgMeta } from "../../types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import HealthReportDialog from "./HealthReportDialog";

export interface GenerateFormState {
  kg: string;
  llm: string;
  emb: string;
  size: number;
  name: string;
}

export interface CostSettings {
  maxRetries: number;
  timeout: number;
  workers: number;
  budget: number;
  personas: number;
}

/** 生成配置卡片：知识图谱 / 语言 / 条数 / 比例 / 成本与容错。只做展示与回填，请求由页面发起。 */
export default function GenerateForm({
  kgs, form, onFormChange,
  language, onLanguageChange, langOptions,
  relProp, onRelPropChange,
  useDefault, onUseDefaultChange, dist, onDistChange, splits, normalized, onNormalize,
  cost, onCostChange, estimate, estimating, onEstimate,
  caps, health, healthLoading, onCheckHealth, onCloseHealth, onAdoptRelProp,
  busy, onGenerate,
}: {
  kgs: KgMeta[];
  form: GenerateFormState;
  onFormChange: (next: GenerateFormState) => void;
  language: string;
  onLanguageChange: (value: string) => void;
  langOptions: { value: string; label: string }[];
  relProp: string;
  onRelPropChange: (value: string) => void;
  useDefault: boolean;
  onUseDefaultChange: (value: boolean) => void;
  dist: Record<string, number>;
  onDistChange: (next: Record<string, number>) => void;
  splits: Record<string, number> | null;
  normalized: boolean;
  onNormalize: () => void;
  cost: CostSettings;
  onCostChange: (next: CostSettings) => void;
  estimate: Record<string, any> | null;
  estimating: boolean;
  onEstimate: () => void;
  caps: Record<string, any> | null;
  health: Record<string, any> | null;
  healthLoading: boolean;
  onCheckHealth: () => void;
  onCloseHealth: () => void;
  onAdoptRelProp: (value: string) => void;
  /** 有任务在跑：生成按钮禁用 */
  busy: boolean;
  onGenerate: () => void;
}) {
  // 当前分布真正会用到的题型；能力缺失只拦这些，其余仅提示
  const neededKeys = useDefault
    ? DIST_KEYS.map((d) => d.key as string)
    : DIST_KEYS.filter((d) => (dist[d.key] ?? 0) > 0).map((d) => d.key as string);
  const unavailableKeys: string[] = caps
    ? DIST_KEYS.map((d) => d.key as string).filter(
        (k) => (caps.synthesizers?.[k]?.effective_available ?? caps.synthesizers?.[k]?.available) === false,
      )
    : [];
  // 投影 KG 缺的素材可在生成前自动补齐：只提示、不阻塞
  const autoFillKeys: string[] = caps
    ? DIST_KEYS.map((d) => d.key as string).filter((k) => caps.synthesizers?.[k]?.auto_enhanceable === true)
    : [];
  const blockedKeys = unavailableKeys.filter((k) => neededKeys.includes(k));

  return (
    <>
      <Card className="p-5 grid grid-cols-3 gap-4">
        <div className="col-span-2"><Label>知识图谱</Label>
          <div className="flex gap-2">
            <select aria-label="知识图谱" value={form.kg} onChange={(e) => onFormChange({ ...form, kg: e.target.value })} className="flex-1">
              <option value="">选择…</option>
              {kgs.map((k) => <option key={k.id} value={k.id}>{k.name}（{k.node_count} 节点{k.mode === "ragflow-projection" ? " · RAGFlow 投影" : ""}）</option>)}
            </select>
            <Button variant="outline" size="xs" className="whitespace-nowrap" onClick={onCheckHealth} disabled={!form.kg || healthLoading}>
              {healthLoading ? "检测中…" : "质检报告"}
            </Button>
          </div>
          {unavailableKeys.length > 0 && (
            <div className="mt-2 space-y-1">
              {unavailableKeys.map((k) => {
                const inUse = neededKeys.includes(k);
                const item = caps!.synthesizers[k];
                return (
                  <div key={k} className={`text-xs leading-relaxed ${inUse ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground"}`}>
                    {inUse ? "⚠ " : "· "}{item.label} 不可用：{item.reason}
                    {!inUse && "（当前分布未使用，不影响生成）"}
                  </div>
                );
              })}
            </div>
          )}
          {autoFillKeys.length > 0 && (
            <div className="mt-2 space-y-1">
              {autoFillKeys.map((k) => (
                <div key={k} className="text-xs leading-relaxed text-muted-foreground">
                  ℹ {caps!.synthesizers[k].label}：{caps!.synthesizers[k].reason}
                </div>
              ))}
            </div>
          )}
        </div>
        <div><Label>多跳连线依据</Label>
          <select aria-label="多跳连线依据" value={relProp} onChange={(e) => onRelPropChange(e.target.value)}>
            <option value="summary_similarity">语义相似边（summary_similarity）</option>
            <option value="entities_overlap_score">实体重叠边（entities_overlap_score）</option>
          </select>
          <div className="text-xs text-muted-foreground mt-1">
            多跳抽象题按哪种边串分块；用「质检报告」看该图谱推荐哪个
          </div>
        </div>

        <div><Label>出题语言</Label>
          <select aria-label="出题语言" value={language} onChange={(e) => onLanguageChange(e.target.value)}>
            {(langOptions.length ? langOptions : [{ value: "zh", label: "中文" }]).map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <div className="text-xs text-muted-foreground mt-1">
            ragas 出题 prompt 是英文的，不锁定会中英混杂
          </div>
        </div>

        <div><Label>测试集条数</Label>
          <Input aria-label="测试集条数" type="number" min={1} max={500} value={form.size}
            onChange={(e) => onFormChange({ ...form, size: Number(e.target.value) || 1 })} /></div>

        <div><Label>测试集名称</Label>
          <Input aria-label="测试集名称" value={form.name} maxLength={MAX_TESTSET_NAME_LEN}
            placeholder="留空则按知识图谱名自动命名"
            onChange={(e) => onFormChange({ ...form, name: e.target.value })} />
          <div className={`text-xs mt-1 ${testsetNameError(form.name) ? "text-destructive" : "text-muted-foreground"}`}>
            {testsetNameError(form.name) ?? `${form.name.length}/${MAX_TESTSET_NAME_LEN} 字`}
          </div>
        </div>

        {/* 比例配置 —— 纵向排列 */}
        <div className="col-span-3 border-t border-border pt-4 space-y-4">
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm cursor-pointer whitespace-nowrap">
              <input type="checkbox" checked={useDefault} onChange={(e) => onUseDefaultChange(e.target.checked)} className="accent-primary size-4" />
              使用默认分布（0.5 / 0.25 / 0.25）
            </label>
            {normalized && (
              <Button variant="outline" size="xs" onClick={onNormalize}>
                比例和 ≠ 1 · 点击归一化
              </Button>
            )}
          </div>
          {!useDefault && (
            <div className="space-y-5 pl-1">
              {DIST_KEYS.map(({ key, label, desc }) => (
                <div key={key} className="space-y-1.5">
                  <div className="flex items-baseline gap-3">
                    <span className="text-sm font-medium w-24 shrink-0">{label}</span>
                    <span className="text-xs text-muted-foreground leading-tight">{desc}</span>
                    <span className="ml-auto text-xs text-muted-foreground tabular-nums">
                      {Math.round(dist[key] * 100)}% · {splits ? (Object.entries(splits).find(([n]) => matchType(n, key))?.[1] ?? 0) : 0} 条
                    </span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-xs text-muted-foreground w-6 text-right">0%</span>
                    <input aria-label={`${label} 占比`} type="range" min={0} max={100} step={1} value={Math.round(dist[key] * 100)}
                      onChange={(e) => onDistChange({ ...dist, [key]: Number(e.target.value) / 100 })}
                      className="flex-1 accent-primary h-1.5" />
                    <span className="text-xs text-muted-foreground w-8">100%</span>
                  </div>
                </div>
              ))}
              <div className="text-xs text-muted-foreground pt-1 border-t border-border">
                💡 三类比例之和决定每类的实际条数（ragas 内部按 testset_size × 比例 取整分配）。建议单跳具体占主导（40–70%），多跳两类根据测试目标调整。
              </div>
            </div>
          )}
        </div>
        {/* 成本与容错 */}
        <div className="col-span-3 border-t border-border pt-3 space-y-2">
          <div className="text-xs font-medium text-muted-foreground">成本与容错</div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <label className="block">
              <span className="text-xs text-muted-foreground">失败重试上限</span>
              <Input type="number" min={1} max={10} value={cost.maxRetries}
                onChange={(e) => onCostChange({ ...cost, maxRetries: Math.max(1, Math.min(10, +e.target.value || 1)) })} />
              <span className="text-xs text-muted-foreground">ragas 默认 10，每次重试重发整份 prompt</span>
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground">单次超时(秒)</span>
              <Input type="number" min={30} max={600} value={cost.timeout}
                onChange={(e) => onCostChange({ ...cost, timeout: +e.target.value || 180 })} />
              <span className="text-xs text-muted-foreground">超时也算失败，会触发重试</span>
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground">并发数</span>
              <Input type="number" min={1} max={32} value={cost.workers}
                onChange={(e) => onCostChange({ ...cost, workers: Math.max(1, Math.min(32, +e.target.value || 1)) })} />
              <span className="text-xs text-muted-foreground">过高易触发限流→重试→烧 token</span>
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground">token 预算（0=不限）</span>
              <Input type="number" min={0} step={10000} value={cost.budget}
                onChange={(e) => onCostChange({ ...cost, budget: Math.max(0, +e.target.value || 0) })} />
              <span className="text-xs text-muted-foreground">超限自动中止，防账单失控</span>
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground">人格数</span>
              <Input type="number" min={1} max={10} value={cost.personas}
                onChange={(e) => onCostChange({ ...cost, personas: Math.max(1, Math.min(10, +e.target.value || 1)) })} />
              <span className="text-xs text-muted-foreground">最大省钱杠杆：3→2 约省 12% 总量</span>
            </label>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <Button variant="outline" size="xs" disabled={!form.kg || estimating} onClick={onEstimate}>
              {estimating ? "预估中…" : "预估 token 消耗"}
            </Button>
            {estimate && (
              <div className="text-xs text-muted-foreground">
                下限 <span className="font-mono text-foreground">{estimate.estimate_tokens.floor.toLocaleString()}</span>
                {" · "}预期 <span className="font-mono text-primary-strong">{estimate.estimate_tokens.expected.toLocaleString()}</span>
                {" · "}最坏 <span className="font-mono text-amber-700 dark:text-amber-300">{estimate.estimate_tokens.worst_case.toLocaleString()}</span>
                {" token"}
                <span className="ml-2">约 {estimate.llm_calls_estimate} 次 LLM 调用</span>
                {estimate.io_split && (
                  <span className="ml-2">入 {estimate.io_split.input.toLocaleString()} / 出 {estimate.io_split.output.toLocaleString()}</span>
                )}
              </div>
            )}
          </div>
          {estimate && (
            <div className="text-xs text-muted-foreground leading-relaxed bg-background rounded-md px-3 py-2">
              分项：{Object.entries(estimate.breakdown_tokens as Record<string, number>)
                .map(([k, v]) => `${k} ${v.toLocaleString()}`).join(" · ")}
              <br />
              分块 {estimate.chunk_stats.count} 个，中位 {estimate.chunk_stats.median_chars} 字符
              （多跳出题会把多个分块原文整篇塞进 prompt，是最大开销项）
              {estimate.enhancement?.will_enhance && (
                <div className="text-amber-700 dark:text-amber-300 mt-1">
                  ⚠ 生成前会自动增强图谱：{estimate.enhancement.chunk_summaries} 个分块摘要、
                  {estimate.enhancement.theme_chunks} 个分块主题、{estimate.enhancement.embeddings} 个摘要向量，
                  约 {estimate.enhancement.total_tokens.toLocaleString()} token（已含在上面的总量里；成功后写回图谱，下次不再重复）
                </div>
              )}
              {(estimate.abstract_context?.factor ?? 1) > 1 && (
                <div className="text-amber-700 dark:text-amber-300 mt-1">
                  ⚠ 多跳抽象题会把整篇文档展开进上下文（平均 {estimate.abstract_context.avg_chunks_per_document} 个分块/篇），
                  该题型输入按 {estimate.abstract_context.factor}× 估算
                </div>
              )}
            </div>
          )}
        </div>

        <div className="col-span-3">
          <Button onClick={onGenerate} disabled={busy || blockedKeys.length > 0}>生成测试集</Button>
          {blockedKeys.length > 0 && (
            <span className="ml-3 text-xs text-amber-700 dark:text-amber-300">当前分布需要的题型缺素材，请调整比例或重建 KG</span>
          )}
        </div>
      </Card>

      {health && (
        <HealthReportDialog data={health} onClose={onCloseHealth} onAdoptRelProp={onAdoptRelProp} />
      )}
    </>
  );
}
