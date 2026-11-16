import { useEffect, useState } from "react";
import { get, post } from "../api";
import TaskProgress from "../components/TaskProgress";
import { useTrackedTask } from "../lib/useTrackedTask";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";

type Metric = { metric_key: string; label: string; required: string[]; needs_llm: boolean; needs_embedding: boolean };
type Report = { sample_count: number; metrics: Array<{ metric_key: string; label: string; ready_count: number; missing_count: number; missing_fields: Record<string, number>; empty_fields: Record<string, number> }> };

const DEFAULT_ROWS = JSON.stringify([
  { id: "q1", user_input: "问题", response: "回答", retrieved_contexts: ["上下文"], reference: "标准答案" },
], null, 2);

export default function EvaluationPage() {
  const [raw, setRaw] = useState(DEFAULT_ROWS);
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [datasetId, setDatasetId] = useState("");
  const [llmPresetId, setLlmPresetId] = useState("");
  const [embeddingPresetId, setEmbeddingPresetId] = useState("");
  const [taskId, setTaskId] = useTrackedTask("evaluation", ["offline_evaluation"]);
  const [evaluationId, setEvaluationId] = useState("");
  /** 演示模式（RAGAS_AVAILABLE=False）产出的模拟分数：导出卡片上标出来，免得和真实分数混淆 */
  const [isMock, setIsMock] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    get<Metric[]>("/api/evaluations/metrics").then((items) => {
      setMetrics(items);
      setSelected(items.map((item) => item.metric_key));
    }).catch((err) => setError(err.message));
  }, []);

  const parseRows = () => {
    const value = JSON.parse(raw);
    if (!Array.isArray(value)) throw new Error("观察数据必须是 JSON 数组");
    return value;
  };

  const preview = async () => {
    try {
      setError("");
      const result = await post<Report>("/api/evaluations/preview", { observations: parseRows(), metric_keys: selected });
      setReport(result);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  const importDataset = async () => {
    try {
      setError("");
      const result = await post<{ dataset_id: string }>("/api/evaluations/import", { name: "工作台观察数据", observations: parseRows(), metric_keys: selected });
      setDatasetId(result.dataset_id);
      await preview();
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  const score = async () => {
    try {
      setError("");
      setEvaluationId("");
      setIsMock(false);
      if (!llmPresetId) throw new Error("请填写 LLM 预设 ID");
      const result = await post<{ task_id: string }>("/api/evaluations/score", {
        dataset_id: datasetId || undefined, observations: datasetId ? undefined : parseRows(), metric_keys: selected,
        llm_preset_id: llmPresetId, embedding_preset_id: embeddingPresetId || undefined,
      });
      setTaskId(result.task_id);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  return <div className="max-w-5xl mx-auto p-8 space-y-6">
    <header>
      <div className="text-xs text-muted-foreground">D1A · 离线评估</div>
      <h1 className="text-xl font-semibold mt-1">五指标质量评估</h1>
      <p className="text-sm text-muted-foreground mt-1">导入 RAGFlow 问答观察数据，先检查字段，再启动可追踪的 RAGAS 评分任务。</p>
    </header>
    <Card className="p-5 space-y-4">
      <div className="flex items-center justify-between"><h2 className="text-sm font-medium">1. 观察数据（JSON 数组）</h2><Button variant="outline" onClick={preview}>预检</Button></div>
      <Textarea aria-label="观察数据（JSON 数组）" value={raw} onChange={(e) => setRaw(e.target.value)} className="w-full h-48 bg-muted border border-border rounded-md p-3 font-mono text-xs" />
      <div className="flex flex-wrap gap-2">{metrics.map((metric) => <label key={metric.metric_key} className="text-xs flex items-center gap-1.5"><input type="checkbox" className="accent-primary size-4" checked={selected.includes(metric.metric_key)} onChange={(e) => setSelected((old) => e.target.checked ? [...old, metric.metric_key] : old.filter((key) => key !== metric.metric_key))} />{metric.label}</label>)}</div>
      <Button onClick={importDataset}>保存为离线数据集</Button>
      {datasetId && <span className="text-xs text-emerald-700 dark:text-emerald-300 ml-3">已保存：{datasetId}</span>}
    </Card>
    {report && <Card className="p-5"><h2 className="text-sm font-medium mb-3">字段能力预检</h2><div className="grid md:grid-cols-5 gap-2">{report.metrics.map((item) => <div key={item.metric_key} className="bg-muted rounded-md p-3 text-xs"><div className="font-medium">{item.label}</div><div className="mt-2 text-emerald-700 dark:text-emerald-300">可评估 {item.ready_count}</div><div className="text-muted-foreground">缺失/空值 {item.missing_count}</div></div>)}</div></Card>}
    <Card className="p-5 space-y-4"><h2 className="text-sm font-medium">2. 启动评分</h2><div className="grid md:grid-cols-2 gap-3"><div><Label>LLM 预设 ID</Label><Input aria-label="LLM 预设 ID" value={llmPresetId} onChange={(e) => setLlmPresetId(e.target.value)} placeholder="如 llm-1a2b3c" /></div><div><Label>Embedding 预设 ID（按需）</Label><Input aria-label="Embedding 预设 ID（按需）" value={embeddingPresetId} onChange={(e) => setEmbeddingPresetId(e.target.value)} placeholder="如 emb-1a2b3c" /></div></div><Button onClick={score}>启动五指标评分</Button></Card>
    <TaskProgress taskId={taskId}
      onDone={(r, err) => {
        // 终态统一由 TaskProgress 的 SSE 处理（与其他页一致），这里只取结果里的评估 id；
        // 随后清空任务 ID 让进度卡片收起，导出卡片只按 evaluationId 显示
        if (err) { setError(err); } else {
          const id = r?.evaluation_id;
          if (id) setEvaluationId(String(id));
          setIsMock(Boolean(r?.is_mock));
        }
        setTaskId(null);
      }}
      onCancel={async () => { if (taskId) await post(`/api/tasks/${taskId}/cancel`).catch(() => {}); }} />
    {evaluationId && <Card className="p-5"><h2 className="text-sm font-medium mb-3">3. 结果导出</h2><div className="flex items-center gap-3 text-sm"><a className="text-primary-strong hover:underline" href={`/api/evaluations/${evaluationId}/export?format=jsonl`}>JSONL</a><a className="text-primary-strong hover:underline" href={`/api/evaluations/${evaluationId}/export?format=csv`}>CSV</a>{isMock && <Badge variant="warning" size="xs" data-testid="mock-badge">⚠ MOCK 演示评分</Badge>}<span className="text-muted-foreground text-xs">评估 ID：{evaluationId}</span></div></Card>}
    {error && <div role="alert" className="border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300 rounded-md p-3 text-sm">{error}</div>}
  </div>;
}
