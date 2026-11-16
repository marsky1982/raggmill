import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { statusVariant } from "../../lib/testsetView";

/** 质检报告弹层：只负责展示后端 `GET /api/testset/health/{kg_id}` 的原始结果。 */
export default function HealthReportDialog({ data, onClose, onAdoptRelProp }: {
  data: Record<string, any>;
  onClose: () => void;
  /** 采纳推荐的「多跳连线依据」，回填到生成配置 */
  onAdoptRelProp?: (value: string) => void;
}) {
  const ns = data.node_stats || {};
  const es = data.edge_stats || {};
  const cov = data.chunk_property_coverage || {};
  const synth = data.synthesizer_availability || {};
  const edgeRows: any[] = es.by_type_labeled
    || Object.entries(es.by_type || {}).map(([t, c]: [string, any]) => ({ key: t, label: t, count: c }));
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto space-y-4">
        {/* 头部 */}
        <div className="flex items-start justify-between gap-3">
          <div>
            <DialogTitle>质检报告</DialogTitle>
            <DialogDescription className="mt-0.5">{data.name} · {data.mode_label || data.mode} · {data.created_at}</DialogDescription>
            {data.source_files?.length > 0 && (
              <p className="text-xs text-muted-foreground mt-0.5">文档：{data.source_files.join("、")}</p>
            )}
          </div>
          <Button variant="outline" size="xs" onClick={onClose}>关闭</Button>
        </div>

        {/* 总评：状态文字压在同色系浅底上，浅色档用 -800 才够 4.5:1（N9） */}
        <div className={`rounded-md p-3 space-y-1 leading-relaxed ${
          data.needs_rebuild
            ? "bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-300"
            : "bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-200 dark:border-emerald-800 text-emerald-800 dark:text-emerald-300"
        }`}>
          <div className="text-sm font-semibold">
            {data.summary?.verdict || (data.needs_rebuild ? "需重建" : "可用")}
          </div>
          <div className="text-xs">{data.summary?.headline}</div>
          <div className="text-xs text-muted-foreground">{data.summary?.detail || data.rebuild_reason}</div>
        </div>

        {/* 推荐的多跳连线依据 */}
        <div className="bg-primary/10 border border-primary/30 rounded-md p-3 space-y-1">
          <div className="text-xs text-muted-foreground">推荐的多跳连线依据</div>
          <div className="text-sm font-semibold text-primary-strong">{data.recommended_label || data.recommended_relation_property}</div>
          <div className="text-xs text-muted-foreground">
            {data.relation_property_needs_change ? (
              <>
                ⚠ 当前用的是系统默认值{" "}
                <span className="font-mono">{data.default_relation_property}</span>
                ，这份图谱下需要在生成配置里手动改成{" "}
                <span className="font-mono text-amber-800 dark:text-amber-300">{data.recommended_relation_property}</span>
              </>
            ) : (
              <>
                ✓ 生成配置里的「多跳连线依据」当前就是{" "}
                <span className="font-mono">{data.recommended_relation_property}</span>
                ，无需改动
              </>
            )}
          </div>
          <div className="text-xs text-muted-foreground">{data.recommended_reason}</div>
          {data.relation_property_needs_change && onAdoptRelProp && (
            <Button variant="outline" size="xs" className="mt-1"
              onClick={() => onAdoptRelProp(data.recommended_relation_property)}>
              采纳推荐值
            </Button>
          )}
        </div>

        {/* 节点统计 */}
        <div className="border border-border rounded-md p-3 space-y-1">
          <div className="text-xs font-medium">节点统计</div>
          <div className="text-xs text-muted-foreground">{ns.explain}</div>
        </div>

        {/* 边统计 */}
        <div className="border border-border rounded-md p-3 space-y-2">
          <div className="text-xs font-medium">边统计</div>
          <div className="text-xs text-muted-foreground leading-relaxed">{es.explain}</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs pt-1">
            {edgeRows.map((e: any) => (
              <div key={e.key} className="flex justify-between gap-2">
                <span className="text-muted-foreground">{e.label}</span><span className="tabular-nums">{e.count}</span>
              </div>
            ))}
          </div>
        </div>

        {/* chunk 属性覆盖率 */}
        <div className="border border-border rounded-md p-3 space-y-2">
          <div className="text-xs font-medium">分块属性覆盖率</div>
          <div className="space-y-1.5">
            {Object.entries(cov).map(([k, v]: [string, any]) => (
              <div key={k} className="text-xs">
                <div className="flex justify-between items-baseline">
                  <span className="text-primary-strong">{v.label || k}</span>
                  <span className="tabular-nums text-muted-foreground">{v.value}</span>
                </div>
                <div className="text-muted-foreground text-xs leading-tight">{v.explain}</div>
              </div>
            ))}
            {Object.keys(cov).length === 0 && (
              <div className="text-xs text-muted-foreground">无分块节点</div>
            )}
          </div>
        </div>

        {/* 各合成器可用性 */}
        <div className="border border-border rounded-md p-3 space-y-2">
          <div className="text-xs font-medium">各合成器可用性</div>
          <div className="space-y-3">
            {Object.entries(synth).map(([k, v]: [string, any]) => (
              <div key={k} className="text-xs space-y-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{v.label || k}</span>
                  <Badge variant={statusVariant(v.status)} size="xs">{v.status}</Badge>
                </div>
                <div className="text-muted-foreground text-xs">{v.desc}</div>
                <div className="text-muted-foreground text-xs">{v.reason}</div>
                {v.relation_property && (
                  <div className="text-primary-strong text-xs">· 多跳连线依据（relation_property）：<span className="font-mono">{v.relation_property}</span></div>
                )}
              </div>
            ))}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
