import { useCallback, useEffect, useState } from "react";
import { del, get, patch, post, postForBlob, type ApiError } from "../api";
import TaskProgress from "../components/TaskProgress";
import GenerateForm, { type CostSettings, type GenerateFormState } from "../components/testset/GenerateForm";
import SampleList from "../components/testset/SampleList";
import type { PlanError } from "../components/testset/ProvenanceCard";
import { consumeSelectedKg } from "../lib/kgSelection";
import { pushError } from "../lib/errorLog";
import { normalizeDistribution, stopLabel, testsetNameError } from "../lib/testsetView";
import { useTrackedTask } from "../lib/useTrackedTask";
import type { KgMeta, Preset, RegeneratePlan, Sample, TestsetDetail } from "../types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";

type HealthInfo = { started_at?: string | null; commit?: string | null; commit_note?: string | null };

/** 测试集页：只做状态编排与组合；生成表单、样本列表、导出、补生成分别是 components/testset/ 下的组件。 */
export default function TestsetPage({ onBack }: { onBack?: () => void }) {
  const [kgs, setKgs] = useState<KgMeta[]>([]);
  const [, setPresets] = useState<Preset[]>([]);
  const [tsList, setTsList] = useState<{
    id: string; name: string; count: number; created_at: string; is_mock?: boolean; stale?: boolean;
    stale_sample_count?: number; partial?: boolean; stop_reason?: string; failed_samples?: number;
    testset_size?: number; source_unbound?: boolean; source_unbound_reason?: string | null;
    projection_missing?: boolean;
  }[]>([]);
  /** 上一次生成的收尾提示：部分结果 / 失败但已保存的测试集 */
  const [doneNotice, setDoneNotice] = useState<{ text: string; tsId?: string; level: "warn" | "error" } | null>(null);
  const [form, setForm] = useState<GenerateFormState>({ kg: "", llm: "", emb: "", size: 10, name: "" });
  const [useDefault, setUseDefault] = useState(true);
  const [dist, setDist] = useState<Record<string, number>>({ single_hop_specific: 0.5, multi_hop_abstract: 0.25, multi_hop_specific: 0.25 });
  const [splits, setSplits] = useState<Record<string, number> | null>(null);
  const [normalized, setNormalized] = useState(false);
  const [taskId, setTaskId] = useTrackedTask("testset", ["generate_testset", "generate_testset_external"]);
  const [detail, setDetail] = useState<TestsetDetail | null>(null);
  /** 来源检查与局部补生成计划：打开详情时读取（0 token），按当前的人格数 / 重试上限预估 */
  const [plan, setPlan] = useState<RegeneratePlan | null>(null);
  const [planErr, setPlanErr] = useState<PlanError | null>(null);
  const [filterType, setFilterType] = useState<string>("");
  const [keyword, setKeyword] = useState("");
  const [adoptOnly, setAdoptOnly] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [health, setHealth] = useState<Record<string, any> | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  /** 成本与容错：ragas 默认 max_retries=10（失败即重发整份 prompt），是 token 失控主因，这里默认 3 */
  const [cost, setCost] = useState<CostSettings>({ maxRetries: 3, timeout: 180, workers: 8, budget: 0, personas: 3 });
  const [estimate, setEstimate] = useState<Record<string, any> | null>(null);
  const [estimating, setEstimating] = useState(false);
  /** 出题语言。ragas 的出题 prompt 全英文且不约束输出语言，中文语料会中英摇摆，故显式锁定 */
  const [language, setLanguage] = useState("zh");
  /** 多跳抽象题按哪种边把分块串起来。质检报告会给推荐值，默认与后端默认一致 */
  const [relProp, setRelProp] = useState("summary_similarity");
  /** KG 能力判定（取代无人写入的 generation_ready，缺素材时展示原因） */
  const [caps, setCaps] = useState<Record<string, any> | null>(null);
  const [langOpts, setLangOpts] = useState<{ value: string; label: string }[]>([]);

  const loadAll = useCallback(async () => {
    const [k, p, t] = await Promise.all([
      get<KgMeta[]>("/api/kg"), get<Preset[]>("/api/presets"), get<typeof tsList>("/api/testset"),
    ]);
    setKgs(k); setPresets(p); setTsList(t);
    get<{ value: string; label: string }[]>("/api/testset/languages")
      .then(setLangOpts).catch((e) => pushError("测试集·出题语言", "读取出题语言列表失败", String(e)));
    const preselected = consumeSelectedKg();
    const preselectedKg = preselected ? k.find((kg) => kg.id === preselected) : null;
    setForm((f) => ({
      ...f,
      kg: preselected || f.kg || k[k.length - 1]?.id || "",
      // 优先出题专用的 llm_testset；用户只配了通用 llm 时回退到它，别提示「未找到默认 LLM」
      llm: f.llm
        || p.find((x) => x.kind === "llm_testset" && x.is_default)?.id || p.find((x) => x.kind === "llm_testset")?.id
        || p.find((x) => x.kind === "llm" && x.is_default)?.id || p.find((x) => x.kind === "llm")?.id || "",
      emb: f.emb || p.find((x) => x.kind === "embedding" && x.is_default)?.id || p.find((x) => x.kind === "embedding")?.id || "",
      // 自动生成测试集名称：基于关联的 KG 名称
      name: f.name || (preselectedKg ? `${preselectedKg.name}-测试集` : ""),
    }));
  }, []);
  useEffect(() => { loadAll().catch((e) => setErr(String(e))); }, [loadAll]);

  // 选定 KG / 连线依据变化时刷新能力判定：缺素材的题型直接展示原因，不再整体禁用
  useEffect(() => {
    if (!form.kg) { setCaps(null); return; }
    let alive = true;
    get<Record<string, any>>(`/api/testset/capabilities/${form.kg}?relation_property=${encodeURIComponent(relProp)}`)
      .then((r) => { if (alive) setCaps(r); })
      .catch((e) => { if (alive) setCaps(null); pushError("测试集·题型能力判定", `读取 ${form.kg} 的能力判定失败`, String(e)); });
    return () => { alive = false; };
  }, [form.kg, relProp]);

  // 预估结果只对算它时的配置有效：换 KG / 条数 / 比例 / 连线依据后清掉，避免旧数字误导
  useEffect(() => { setEstimate(null); }, [form.kg, form.size, dist, useDefault, relProp, cost.personas, cost.maxRetries]);

  // 比例 -> 条数实时换算
  useEffect(() => {
    if (useDefault) { setSplits(null); setNormalized(false); return; }
    const sum = Object.values(dist).reduce((a, b) => a + b, 0);
    setNormalized(Math.abs(sum - 1) > 1e-6);
    post<{ splits: Record<string, number> }>("/api/calc/splits", {
      testset_size: form.size, distribution: dist,
    }).then((r) => setSplits(r.splits)).catch((e) => pushError("测试集·条数分配", "读取条数分配失败", String(e)));
  }, [dist, form.size, useDefault]);

  const openTestset = async (id: string) => {
    try {
      setDetail(await get<TestsetDetail>(`/api/testset/${id}`));
      setFilterType("");
    }
    catch (e) { setErr(String(e)); }
  };

  useEffect(() => {
    if (!detail) { setPlan(null); setPlanErr(null); return; }
    let alive = true;
    setPlanErr(null);
    get<RegeneratePlan>(`/api/testset/${detail.id}/regenerate-plan?num_personas=${cost.personas}&max_retries=${cost.maxRetries}`)
      .then((r) => { if (alive) setPlan(r); })
      .catch(async (e: ApiError) => {
        // 两类 404 的处置完全不同：接口不存在 = 后端旧进程；测试集不存在 = 数据失效
        const kind: PlanError["kind"] = e?.endpointMissing ? "endpoint_missing"
          : e?.errorCode === "testset_not_found" ? "testset_not_found" : "other";
        let startedAt: string | null = null;
        let commit: string | null = null;
        let commitNote: string | null = null;
        if (kind === "endpoint_missing") {
          // 只有这一类需要时间/版本信息，其他类不必多打一次 /api/health
          try {
            const h = await get<HealthInfo>("/api/health");
            startedAt = h.started_at ?? null;
            commit = h.commit ?? null;
            commitNote = h.commit_note ?? null;
          } catch { /* health 也取不到就只显示通用提示 */ }
        }
        if (alive) { setPlan(null); setPlanErr({ kind, message: String(e?.message ?? e), startedAt, commit, commitNote }); }
      });
    return () => { alive = false; };
  }, [detail, cost.personas, cost.maxRetries]);

  /** 局部补生成：只重出内容已变 / 来源已删的题，结果另存为新测试集，原测试集不改 */
  const regenerate = async (name: string, lang: string) => {
    if (!detail) return;
    setErr(null);
    if (!form.llm || !form.emb) { setErr("未找到默认 LLM / Embedding 预设，请先到「模型配置」创建并设为默认"); return; }
    const nameIssue = testsetNameError(name);
    if (nameIssue) { setErr(nameIssue); return; }
    try {
      const r = await post<{ task_id: string }>(`/api/testset/${detail.id}/regenerate`, {
        llm_preset_id: form.llm, embedding_preset_id: form.emb,
        max_retries: cost.maxRetries, llm_timeout: cost.timeout,
        max_workers: cost.workers, token_budget: cost.budget, num_personas: cost.personas,
        name: name.trim(), language: lang || null,
      });
      setTaskId(r.task_id);
    } catch (e) { setErr(String(e)); }
  };

  const generate = async () => {
    setErr(null);
    if (!form.kg) { setErr("请选择知识图谱（若无可回 Step 1 生成）"); return; }
    if (!form.llm || !form.emb) { setErr("未找到默认 LLM / Embedding 预设，请先到「模型配置」创建并设为默认"); return; }
    const nameIssue = testsetNameError(form.name);
    if (nameIssue) { setErr(nameIssue); return; }
    try {
      const r = await post<{ task_id: string }>("/api/testset/generate", {
        kg_id: form.kg, llm_preset_id: form.llm, embedding_preset_id: form.emb,
        testset_size: form.size, query_distribution: dist,
        use_default_distribution: useDefault, name: form.name.trim(),
        max_retries: cost.maxRetries, llm_timeout: cost.timeout,
        max_workers: cost.workers, token_budget: cost.budget,
        num_personas: cost.personas, language,
        abstract_relation_property: relProp,
      });
      setTaskId(r.task_id);
    } catch (e) { setErr(String(e)); }
  };

  const checkHealth = async () => {
    if (!form.kg) { setErr("请先选择知识图谱"); return; }
    setHealthLoading(true);
    setErr(null);
    try {
      setHealth(await get<Record<string, any>>(`/api/testset/health/${form.kg}`));
    } catch (e) { setErr(String(e)); }
    finally { setHealthLoading(false); }
  };

  const estimateCost = async () => {
    setEstimating(true); setErr(null);
    try {
      setEstimate(await post<Record<string, any>>("/api/testset/estimate-cost", {
        kg_id: form.kg, testset_size: form.size, query_distribution: dist,
        use_default_distribution: useDefault, max_retries: cost.maxRetries,
        num_personas: cost.personas, abstract_relation_property: relProp,
      }));
    } catch (e) { setErr(`预估失败：${(e as Error).message}`); }
    finally { setEstimating(false); }
  };

  /** 删除测试集：先确认；只在删的正是当前打开的那份时才关闭详情 */
  const removeTestset = async (t: { id: string; name: string }) => {
    if (!window.confirm(`确定删除测试集「${t.name}」？删除后不可恢复。`)) return;
    try {
      await del(`/api/testset/${t.id}`);
      setDetail((d) => (d && d.id === t.id ? null : d));
    } catch (e) {
      setErr(`删除失败：${(e as Error).message}`);
    } finally {
      void loadAll();
    }
  };

  const toggleAdopt = async (s: Sample) => {
    try {
      await patch(`/api/testset/${detail!.id}/samples/${s.index}`, { adopted: !s.adopted });
    } catch (e) {
      setErr(`标记失败：${(e as Error).message}`);
      return;
    }
    setDetail((d) => d && {
      ...d,
      groups: Object.fromEntries(Object.entries(d.groups).map(([k, arr]) =>
        [k, arr.map((x) => (x.index === s.index ? { ...x, adopted: !x.adopted } : x))])),
    });
  };

  const doExport = async (fmt: "csv" | "jsonl", scope: "all" | "adopted") => {
    if (!detail) return;
    setExporting(true);
    try {
      // 必须走 api.ts 的写出口：导出是 POST，启动器设置令牌后后端拒绝缺 X-RAGAS-Write-Token 的请求（F1）
      const { blob, filename } = await postForBlob(`/api/testset/${detail.id}/export`,
        { format: fmt, scope, types: filterType ? [filterType] : undefined });
      if (blob.size === 0) {
        setErr("导出内容为空，可能测试集无符合条件的数据");
        return;
      }
      // 文件名以后端为准（演示数据会带「演示数据」后缀，）
      const name = filename || `${detail.name}-${scope}.${fmt}`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      setErr(`已导出 ${name}（${blob.size} bytes），请到浏览器下载目录查看`);
    } catch (e) {
      setErr(`导出失败：${(e as Error).message}`);
    } finally { setExporting(false); }
  };

  // LLM / Embedding 预设自动取默认（UI 不再暴露选择，与 KG 页一致）
  const allSamples = detail ? Object.values(detail.groups).flat() : [];
  const affectedByIndex = new Map((plan?.affected || []).map((a) => [a.index, a.status]));
  const affectedStatus = (index?: number | null) =>
    index === null || index === undefined ? undefined : affectedByIndex.get(index);
  const visible = allSamples
    .filter((s) => !filterType || s.synthesizer_name === filterType)
    .filter((s) => !keyword || s.user_input.includes(keyword))
    .filter((s) => !adoptOnly || s.adopted);

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-6">
      <header>
        {onBack && (
          <button className="text-xs text-muted-foreground hover:text-primary-strong mb-3 -ml-1 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card" onClick={onBack}>← 返回上一页</button>
        )}
        <h1 className="text-xl font-semibold">测试集生成</h1>
        <p className="text-sm text-muted-foreground mt-1">
          TestsetGenerator.generate(testset_size, query_distribution)，按 synthesizer_name 分类预览，导出 CSV / JSONL（ragas 内置格式）。
        </p>
      </header>

      <GenerateForm
        kgs={kgs} form={form} onFormChange={setForm}
        language={language} onLanguageChange={setLanguage} langOptions={langOpts}
        relProp={relProp} onRelPropChange={setRelProp}
        useDefault={useDefault} onUseDefaultChange={setUseDefault}
        dist={dist} onDistChange={setDist} splits={splits} normalized={normalized}
        onNormalize={() => setDist(normalizeDistribution(dist))}
        cost={cost} onCostChange={setCost}
        estimate={estimate} estimating={estimating} onEstimate={estimateCost}
        caps={caps}
        health={health} healthLoading={healthLoading} onCheckHealth={checkHealth}
        onCloseHealth={() => setHealth(null)}
        onAdoptRelProp={(v) => { setRelProp(v); setHealth(null); }}
        busy={!!taskId} onGenerate={generate}
      />

      {err && <div className="text-xs text-red-600 dark:text-red-400">{err}</div>}

      {doneNotice && (
        <div className={`rounded-md border px-4 py-2 text-xs flex items-center gap-3 ${
          doneNotice.level === "error" ? "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300" : "border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300"
        }`}>
          <span>{doneNotice.text}</span>
          {doneNotice.tsId && (
            <Button variant="outline" size="xs" onClick={() => openTestset(doneNotice.tsId!)}>查看 {doneNotice.tsId}</Button>
          )}
          <Button variant="outline" size="xs" className="ml-auto" onClick={() => setDoneNotice(null)}>关闭</Button>
        </div>
      )}

      <TaskProgress taskId={taskId}
        onDone={(r, error) => {
          setTaskId(null);
          void loadAll();
          if (error) {
            setErr(`测试集生成失败：${error}`);
            // 失败时已完成样本与账单仍会落盘，错误里带测试集 id：给个直达入口
            const saved = /\b(ts-[0-9a-z]+)\b/i.exec(error)?.[1];
            setDoneNotice(saved ? { text: "已完成的部分与 token 账单已保存为测试集", tsId: saved, level: "error" } : null);
            return;
          }
          if (r?.partial) {
            const label = stopLabel(r.stop_reason ? String(r.stop_reason) : null);
            const failed = Number(r.failed_samples || 0);
            setDoneNotice({
              text: `生成未完整结束（${label}）：已打开保存下来的 ${Number(r.count || 0)} 条`
                + (failed ? `，另有 ${failed} 条失败已跳过` : ""),
              tsId: r.testset_id ? String(r.testset_id) : undefined, level: "warn",
            });
          } else {
            setDoneNotice(null);
          }
          if (r?.testset_id) openTestset(String(r.testset_id));
        }}
        onCancel={async () => { if (taskId) await post(`/api/tasks/${taskId}/cancel`).catch(() => {}); }} />

      {/* 历史测试集 */}
      <Card className="divide-y divide-border">
        <CardHeader className="px-5 py-3 text-sm font-medium text-muted-foreground">已生成测试集（{tsList.length}）</CardHeader>
        {tsList.length === 0 && <div className="px-5 py-6 text-sm text-muted-foreground">暂无。</div>}
        {tsList.map((t) => (
          <div key={t.id} className="px-5 py-3 flex items-center gap-4 text-sm">
            <button className="flex-1 text-left min-w-0 -mx-2 px-2 py-1 rounded hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card" onClick={() => openTestset(t.id)}>
              <span className="font-medium">{t.name}</span>
              <span className="text-xs text-muted-foreground ml-2">{t.count} 条 · {t.created_at}</span>
              {t.is_mock && (
                <Badge variant="warning" size="xs" className="ml-2 align-middle">
                  ⚠ MOCK
                </Badge>
              )}
              {t.stale && <Badge variant="warning" size="xs" className="ml-2 align-middle">来源已变化 · {t.stale_sample_count || 0} 条</Badge>}
              {t.source_unbound && (
                <Badge variant="warning" size="xs" className="ml-2 align-middle">
                  已停止跟踪
                </Badge>
              )}
              {t.projection_missing && !t.source_unbound && (
                <Badge variant="warning" size="xs" className="ml-2 align-middle">
                  投影已删除 · 待重新同步
                </Badge>
              )}
              {/* 终态徽章：部分结果、失败跳过的条数一眼可见，不用点进详情 */}
              {t.partial && (
                <Badge variant="warning" size="xs" className="ml-2 align-middle">
                  ⚠ 部分结果{t.stop_reason ? `：${stopLabel(t.stop_reason)}` : ""}
                  {t.testset_size ? ` · ${t.count}/${t.testset_size} 条` : ""}
                </Badge>
              )}
              {!!t.failed_samples && (
                <Badge variant="danger" size="xs" className="ml-2 align-middle">
                  {t.failed_samples} 条失败已跳过
                </Badge>
              )}
            </button>
            <Button variant="danger" size="xs" onClick={() => removeTestset(t)}>删除</Button>
          </div>
        ))}
      </Card>

      {/* 分类预览 */}
      {detail && (
        <SampleList
          // 按测试集重挂载：补生成名称/语言是组件内局部状态，不重挂载会带到下一个测试集
          key={detail.id}
          detail={detail} visible={visible} busy={!!taskId}
          filterType={filterType} onFilterType={setFilterType}
          keyword={keyword} onKeyword={setKeyword}
          adoptOnly={adoptOnly} onAdoptOnly={setAdoptOnly}
          exporting={exporting} onExport={doExport} onToggleAdopt={toggleAdopt}
          affectedStatus={affectedStatus}
          plan={plan} planErr={planErr} onRegenerate={regenerate}
          langOptions={langOpts} onOpenTestset={openTestset}
        />
      )}
    </div>
  );
}
