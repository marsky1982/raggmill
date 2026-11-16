import { MAX_TESTSET_NAME_LEN, REGEN_TYPE_LABEL, testsetNameError } from "../../lib/testsetView";
import type { RegeneratePlan } from "../../types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** 补生成计划读取失败的三类原因：接口不存在（后端旧进程）/ 测试集不存在 / 其他 */
export type PlanError = {
  kind: "endpoint_missing" | "testset_not_found" | "other";
  message: string;
  /** 仅接口不存在时展示：后端进程启动时间与代码版本，便于判断是否需要重启 */
  startedAt?: string | null;
  commit?: string | null;
  /** commit 拿不到时后端给的原因 */
  commitNote?: string | null;
};

/** 来源检查与局部补生成：对照数据源当前图谱判定每条题，只重出受影响的（读取 0 token，补生成才花钱） */
export default function ProvenanceCard({ plan, error, busy, onRegenerate,
  name, onNameChange, language, onLanguageChange, langOptions }: {
  plan: RegeneratePlan | null;
  error: PlanError | null;
  busy: boolean;
  onRegenerate: () => void;
  /** 补生成结果名称（留空走后端默认） */
  name: string;
  onNameChange: (v: string) => void;
  /** 补生成出题语言（空串 = 沿用原测试集） */
  language: string;
  onLanguageChange: (v: string) => void;
  langOptions: { value: string; label: string }[];
}) {
  if (error) {
    const title = error.kind === "endpoint_missing" ? "后端接口不存在"
      : error.kind === "testset_not_found" ? "测试集不存在" : "读取失败";
    const hint = error.kind === "endpoint_missing"
      ? "后端可能仍是旧进程：确认服务重启时间晚于代码更新后刷新页面。"
      : error.kind === "testset_not_found" ? "该测试集已被删除或记录失效，换一个测试集。" : null;
    return (
      <div className="rounded-md border border-border px-3 py-2 text-xs leading-relaxed space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-foreground">来源检查 · {title}</span>
          {hint && <span className="text-muted-foreground">{hint}</span>}
        </div>
        {error.kind === "endpoint_missing" && (
          <div className="text-muted-foreground">
            后端启动时间：<span className="font-mono text-foreground">
              {error.startedAt ? new Date(error.startedAt).toLocaleString("zh-CN", { hour12: false }) : "未上报"}
            </span>
            ；代码版本：<span className="font-mono text-foreground">{error.commit || `未知（${error.commitNote || "后端未提供"}）`}</span>
          </div>
        )}
        <div className="text-muted-foreground break-all">原始错误：{error.message}</div>
      </div>
    );
  }
  if (!plan) return null;
  const c = plan.counts;
  const est = plan.estimate?.estimate_tokens;
  const basis = plan.judged_against === "current_projection" ? "数据源当前图谱"
    : plan.judged_against === "own_kg" ? "测试集自身的图谱（非 RAGFlow 来源，或找不到当前图谱）" : "指定图谱";
  const warn = "text-amber-700 dark:text-amber-300";
  const defaultName = plan.testset_name ? `${plan.testset_name}·局部补生成` : "原名·局部补生成";
  // 后端的 "" 选项是「跟随语料（不指定）」；这里的空值语义是「沿用原测试集」，二者不能同时出现
  const opts = (langOptions.length ? langOptions : [{ value: "zh", label: "中文" }, { value: "en", label: "英文" }])
    .filter((o) => o.value);
  return (
    <div className="rounded-md border border-border px-3 py-2 text-xs leading-relaxed space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-medium text-foreground">来源检查</span>
        <span className="text-muted-foreground">对照{basis}</span>
      </div>
      <div className="flex gap-3 flex-wrap text-muted-foreground">
        <span>有效 <span className="font-mono text-foreground">{c.valid}</span></span>
        <span>内容已变 <span className={`font-mono ${c.stale ? warn : "text-foreground"}`}>{c.stale}</span></span>
        <span>来源已删除 <span className={`font-mono ${c.orphan ? warn : "text-foreground"}`}>{c.orphan}</span></span>
        <span>无出处（历史） <span className="font-mono text-foreground">{c.legacy}</span></span>
        {!!c.unknown && <span>无法判定 <span className="font-mono text-foreground">{c.unknown}</span></span>}
      </div>
      {plan.legacy_note && <div className="text-muted-foreground">{plan.legacy_note}</div>}
      {plan.regenerate > 0 && !plan.blocked ? (
        <>
          <div className="flex items-center gap-2 flex-wrap">
            <Label htmlFor="regen-name" className="text-muted-foreground">结果名称</Label>
            <Input id="regen-name" className="!w-64" value={name} disabled={busy}
              maxLength={MAX_TESTSET_NAME_LEN}
              placeholder={`默认：${defaultName}`} onChange={(e) => onNameChange(e.target.value)} />
            <span className={`text-xs tabular-nums ${testsetNameError(name) ? "text-destructive" : "text-muted-foreground"}`}>
              {testsetNameError(name) ?? `${name.length}/${MAX_TESTSET_NAME_LEN}`}
            </span>
            <Label htmlFor="regen-language" className="text-muted-foreground">出题语言</Label>
            <select id="regen-language" className="w-40" value={language} disabled={busy}
              onChange={(e) => onLanguageChange(e.target.value)}>
              <option value="">沿用原测试集</option>
              {opts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-foreground">
              可局部补生成 {plan.regenerate} 条（
              {Object.entries(plan.regenerate_by_type).map(([k, n]) => `${REGEN_TYPE_LABEL[k] || k} ${n}`).join("、")}
              ），原样保留 {plan.keep} 条
              {est && <>；预估约 <span className="font-mono">{est.expected.toLocaleString()}</span> token（{est.floor.toLocaleString()}～{est.worst_case.toLocaleString()}）</>}
            </span>
            <Button size="xs" disabled={busy} onClick={onRegenerate}
              title={busy ? "已有生成任务在进行" : "结果另存为新测试集，原测试集不改；模型与成本设置沿用上方表单"}>
              局部补生成
            </Button>
          </div>
        </>
      ) : (
        <div className="text-muted-foreground">{plan.blocked || "没有需要补生成的题"}</div>
      )}
    </div>
  );
}
