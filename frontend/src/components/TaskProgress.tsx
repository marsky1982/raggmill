import { useEffect, useMemo, useRef, useState } from "react";
import { subscribeTask, type SseHandlers } from "../api";
import { pushError, pushInfo } from "../lib/errorLog";
import { formatHeartbeatLine, stageLabel } from "../lib/stageLabels";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";

interface Props {
  taskId: string | null;
  /** stopReason 为终态细分（如建图超预算 "budget_exceeded"），供页面区分提示文案 */
  onDone: (result: Record<string, unknown> | null, error?: string, stopReason?: string) => void;
  /** 取消回调；runId 为本次生成对应的断点 run_id（供取消后清理断点用，可能为空） */
  onCancel?: (runId?: string) => void;
}

/** 通用任务进度组件：进度条 + 阶段 + 步骤计数 + 节点统计 + 断点提示 + 错误 + 日志抽屉 + 取消。 */
export default function TaskProgress({ taskId, onDone, onCancel }: Props) {
  const [status, setStatus] = useState("");
  const [progress, setProgress] = useState(0);
  const [stage, setStage] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
  const [stepIdx, setStepIdx] = useState<number | null>(null);
  const [stepTotal, setStepTotal] = useState<number | null>(null);
  const [nodeCount, setNodeCount] = useState<number | null>(null);
  const [relCount, setRelCount] = useState<number | null>(null);
  const [ckptFlash, setCkptFlash] = useState(false);
  /** Step 2 生成的 LLM 用量遥测（KG 生成任务不会带这些字段，保持为 null 即不渲染） */
  const [tele, setTele] = useState<{
    calls: number; failed: number; retries: number; fixes: number;
    tokens: number; tokensIn: number; tokensOut: number; elapsed: number;
    overBudget: boolean;
  } | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const runIdRef = useRef<string | undefined>(undefined);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;

  const handlers: SseHandlers = useMemo(() => {
    const onProgress: NonNullable<SseHandlers["onProgress"]> = (d) => {
      // 骨架心跳（PR9）：长阶段在 to_thread / ragas 工作线程里跑，期间没有日志。
      // 按普通日志行渲染，阶段名走同一张映射表；状态与进度与上一条事件一致，无需重设。
      if (d.heartbeat) {
        const line = formatHeartbeatLine(d.stage, d.stage_elapsed_s ?? 0, d.done, d.total);
        setLogs((ls) => [...ls, line].slice(-500));
        return;
      }
      if (d.error) setError(d.error);
      setStatus(d.status);
      setProgress(d.progress ?? 0);
      if (d.stage) setStage(d.stage);
      if (d.stage_index != null) setStepIdx(d.stage_index);
      if (d.stage_total != null) setStepTotal(d.stage_total);
      if (d.node_count != null) setNodeCount(d.node_count);
      if (d.relationship_count != null) setRelCount(d.relationship_count);
      if (d.run_id) runIdRef.current = d.run_id;
      if (d.llm_calls != null) {
        setTele({
          calls: d.llm_calls,
          failed: d.llm_calls_failed ?? 0,
          retries: d.llm_retries ?? 0,
          fixes: d.llm_fix_calls ?? 0,
          tokens: d.total_tokens ?? 0,
          tokensIn: d.input_tokens ?? 0,
          tokensOut: d.output_tokens ?? 0,
          elapsed: d.elapsed_s ?? 0,
          overBudget: !!d.budget_exceeded,
        });
      }
      if (d.checkpoint) {
        setCkptFlash(true);
        window.setTimeout(() => setCkptFlash(false), 1500);
      }
      if (d.log) {
        setLogs((ls) => [...ls, d.log!].slice(-500));
        pushInfo("任务·步骤", d.log!);
      }
    };
    return {
    onProgress,
    // 快照：刷新状态与用量；日志区为空时（切页回来 / 刷新后）用快照里的最近日志补回
    onSnapshot: (s) => {
      onProgress({ ...s, log: undefined, checkpoint: undefined });
      if (s.logs?.length) setLogs((ls) => (ls.length ? ls : s.logs!.slice(-500)));
    },
    onDone: (d: { status: string; result?: Record<string, unknown>; error?: string; stop_reason?: string }) => {
      setStatus(d.status);
      setProgress(1);
      const errMsg = d.error || null;
      if (errMsg) {
        setError(errMsg);
        setLogs((ls) => [...ls, `✗ ${errMsg}`]);
        pushError("任务·失败", errMsg);
      } else {
        pushInfo("任务·终态", `任务 ${taskId}: ${d.status}`);
      }
      doneRef.current(d.result ?? null, errMsg ?? undefined, d.stop_reason);
    },
    };
  }, [taskId]);

  useEffect(() => {
    if (!taskId) return;
    setLogs([]);
    setProgress(0);
    setStatus("running");
    setError(null);
    setStepIdx(null); setStepTotal(null); setNodeCount(null); setRelCount(null);
    setTele(null);
    // 来源按任务类型写，别把测试集 / 同步任务也标成「KG生成」
    pushInfo("任务·启动", `任务 ${taskId} 已开始`);
    return subscribeTask(taskId, handlers);
  }, [taskId, handlers]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [logs]);

  if (!taskId) return null;
  const color = status === "failed" ? "bg-red-500" : status === "succeeded" ? "bg-emerald-500" : "bg-primary";

  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <span className={`w-2 h-2 rounded-full ${
          status === "running" ? "bg-primary animate-pulse" : status === "succeeded" ? "bg-emerald-500" : status === "failed" ? "bg-red-500" : "bg-muted-foreground"
        }`} />
        <span className="text-sm" role="status" aria-live="polite">
          {status === "running" && "任务执行中"}
          {status === "succeeded" && "任务完成"}
          {status === "failed" && "任务失败"}
          {status === "canceled" && "已取消"}
          {status === "cancel_requested" && "取消中，等待在途调用收尾"}
          {status === "interrupted" && "任务已中断"}
          {status === "reconnecting" && "连接中断，正在恢复"}
        </span>
        {stage && <span className="text-xs text-muted-foreground">阶段: {stageLabel(stage)}</span>}
        {/* 任务 ID 直接上卡片（此前只写调试窗口），排查时少一个线索 */}
        <span className="text-xs text-muted-foreground font-mono" data-testid="task-id">{taskId}</span>
        {stepIdx != null && stepTotal != null && (
          <Badge variant={ckptFlash ? "success" : "outline"}
            className={`text-xs px-1.5 py-0.5 rounded ${ckptFlash ? "" : "text-muted-foreground"}`}>
            步骤 {stepIdx}/{stepTotal}
          </Badge>
        )}
        {nodeCount != null && (
          <span className="text-xs text-muted-foreground">
            {nodeCount} 节点{relCount != null ? ` / ${relCount} 关系` : ""}
          </span>
        )}
        {ckptFlash && <span className="text-xs text-emerald-700 dark:text-emerald-300">✓ 断点已保存</span>}
        <span className="text-xs text-muted-foreground ml-auto">{Math.round(progress * 100)}%</span>
        {status === "running" && (
          <Button variant="outline" size="xs" onClick={() => cancelRef.current?.(runIdRef.current)}>取消</Button>
        )}
        <Button variant="outline" size="xs" onClick={() => setOpen(!open)}>{open ? "收起日志" : "展开日志"}</Button>
      </div>
      <div className="h-1.5 bg-muted rounded-full overflow-hidden">
        <div className={`h-full ${color} transition-all duration-300`} style={{ width: `${progress * 100}%` }} />
      </div>

      {/* LLM 用量看板：只在 Step 2 生成时出现（后端带回遥测字段才渲染） */}
      {tele && (
        <div className={`grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs rounded-md border px-3 py-2 ${
          tele.overBudget ? "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40" : "border-border bg-muted"
        }`}>
          <div>
            <div className="text-muted-foreground">累计 token</div>
            <div className={`font-mono text-sm ${tele.overBudget ? "text-red-700 dark:text-red-300" : "text-foreground"}`}>
              {tele.tokens.toLocaleString()}
            </div>
            <div className="text-muted-foreground">入 {tele.tokensIn.toLocaleString()} / 出 {tele.tokensOut.toLocaleString()}</div>
          </div>
          <div>
            <div className="text-muted-foreground">LLM 调用</div>
            <div className="font-mono text-sm">{tele.calls}</div>
            <div className="text-muted-foreground">
              {tele.elapsed > 0 ? `已耗时 ${Math.floor(tele.elapsed / 60)}m${tele.elapsed % 60}s` : "—"}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">重试 / 修复</div>
            <div className={`font-mono text-sm ${tele.retries + tele.fixes > 0 ? "text-amber-700 dark:text-amber-300" : ""}`}>
              {tele.retries} / {tele.fixes}
            </div>
            <div className="text-muted-foreground">重发同一 prompt，纯浪费</div>
          </div>
          <div>
            <div className="text-muted-foreground">失败调用</div>
            <div className={`font-mono text-sm ${tele.failed > 0 ? "text-red-700 dark:text-red-300" : ""}`}>{tele.failed}</div>
            <div className="text-muted-foreground">
              {tele.calls > 0 ? `均摊 ${Math.round(tele.tokens / tele.calls).toLocaleString()} token/次` : "—"}
            </div>
          </div>
          {tele.overBudget && (
            <div className="col-span-2 sm:col-span-4 text-red-700 dark:text-red-300">
              ⛔ 已超 token 预算，任务已自动中止
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 rounded-md px-4 py-3 text-sm text-red-700 dark:text-red-300 flex items-start gap-2">
          <span className="shrink-0 mt-0.5">✗</span>
          <div className="min-w-0">
            <div className="font-medium">任务执行失败</div>
            <div className="text-xs text-red-700 dark:text-red-300 mt-1 break-words">{error}</div>
          </div>
        </div>
      )}

      {open && (
        <div ref={logRef} className="max-h-40 overflow-y-auto bg-background rounded-md p-3 font-mono text-xs leading-5 text-muted-foreground space-y-0.5">
          {logs.length === 0 && <div>等待任务日志…</div>}
          {logs.map((l, i) => (
            <div key={i} className={`whitespace-pre-line ${
              l.startsWith("✗") || l.startsWith("⛔") ? "text-red-700 dark:text-red-300"
              : l.startsWith("✓") ? "text-emerald-700 dark:text-emerald-300"
              : l.startsWith("▶") || l.startsWith("▶ [演示]") ? "text-primary-strong"
              : l.startsWith("↻") || l.startsWith("⚠") ? "text-amber-700 dark:text-amber-300"
              : l.startsWith("■") ? "text-foreground font-medium"
              : l.startsWith("⏳") ? "text-primary-strong"
              : l.startsWith("·") ? "text-muted-foreground"
              : l.startsWith("✅") ? "text-emerald-700 dark:text-emerald-300 font-medium"
              : ""
            }`}>{l}</div>
          ))}
        </div>
      )}
    </Card>
  );
}
