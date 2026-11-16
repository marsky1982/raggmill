import { useEffect, useState } from "react";
import { clearErrors, subscribeErrors, type ErrEntry } from "../lib/errorLog";
import { Button } from "@/components/ui/button";

/** 侧栏底部可折叠调试抽屉（收集全站错误/警告/信息）。
 *  按钮在侧栏内联渲染（视觉整改 V4 的正式方案），不再压在列表最后一行的按钮上；
 *  展开后的日志面板才用 fixed 浮层，从侧栏右侧弹出。 */
export default function DebugDrawer() {
  const [entries, setEntries] = useState<ErrEntry[]>([]);
  const [open, setOpen] = useState(false);
  const [hasNew, setHasNew] = useState(false);

  useEffect(() => subscribeErrors((es) => {
    setEntries(es);
    if (es.some((e) => e.level === "error")) setHasNew(true);
  }), []);

  const errorCount = entries.filter((e) => e.level === "error").length;

  return (
    <>
      {open && (
        <div className="fixed bottom-4 left-[15.5rem] z-50 w-[460px] max-w-[calc(100vw-16.5rem)] max-h-[60vh] bg-background border border-border rounded-lg shadow-2xl flex flex-col overflow-hidden font-mono text-xs select-none">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-card">
            <span className="font-semibold">调试日志</span>
            <span className="text-muted-foreground">{entries.length} 条 · {errorCount} 错误</span>
            <div className="ml-auto flex items-center gap-1">
              <Button variant="outline" size="xs"
                onClick={() => navigator.clipboard.writeText(entries.map((e) => `[${e.time}] ${e.source} ${e.level}: ${e.message}${e.detail ? "\n  " + e.detail : ""}`).join("\n"))}>
                复制全部
              </Button>
              <Button variant="outline" size="xs" onClick={() => { clearErrors(); setHasNew(false); }}>清空</Button>
              <Button variant="outline" size="xs" onClick={() => setOpen(false)}>收起</Button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {entries.length === 0 && <div className="text-muted-foreground">无日志。</div>}
            {entries.map((e) => (
              <div key={e.id} className={`p-2 rounded border ${
                e.level === "error" ? "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40" :
                e.level === "warn" ? "border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40" :
                "border-border bg-card"
              }`}>
                <div className="flex items-center gap-2">
                  <span className={
                    e.level === "error" ? "text-red-700 dark:text-red-300" :
                    e.level === "warn" ? "text-amber-700 dark:text-amber-300" : "text-emerald-700 dark:text-emerald-300"
                  }>●</span>
                  <span className="text-muted-foreground">{e.time}</span>
                  <span className="text-primary-strong">{e.source}</span>
                  <span className="ml-auto text-muted-foreground text-[11px]">#{e.id}</span>
                </div>
                <div className="mt-1 text-foreground whitespace-pre-wrap break-all">{e.message}</div>
                {e.detail && <div className="mt-1 text-muted-foreground whitespace-pre-wrap break-all text-xs">{e.detail}</div>}
              </div>
            ))}
          </div>
        </div>
      )}
      <button
        onClick={() => { setOpen(!open); if (!open) setHasNew(false); }}
        className={`shrink-0 px-2 h-7 rounded-md border transition-colors whitespace-nowrap focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
          errorCount > 0 ? "bg-red-50 dark:bg-red-950/40 border-red-200 dark:border-red-800 text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-900" : "bg-card border-border text-muted-foreground hover:text-foreground"
        }`}
        title={open ? "收起调试抽屉" : "打开调试抽屉"}
      >
        {open ? "▼ 调试" : (errorCount > 0 ? `▲ 调试 (${errorCount})` : "▲ 调试")}
        {hasNew && !open && errorCount > 0 && <span className="ml-1 inline-block w-2 h-2 bg-red-500 rounded-full animate-pulse" />}
      </button>
    </>
  );
}
