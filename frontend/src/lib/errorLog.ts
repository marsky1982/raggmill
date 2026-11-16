/** 全局错误日志（轻量单例 + 订阅）。 */
export interface ErrEntry {
  id: number;
  time: string;
  source: string;
  level: "error" | "warn" | "info";
  message: string;
  detail?: string;
}

let seq = 1;
let entries: ErrEntry[] = [];
const listeners = new Set<(es: ErrEntry[]) => void>();
const MAX = 200;

function notify() {
  for (const l of listeners) l(entries);
}

export function pushError(source: string, message: string, detail?: string) {
  const e: ErrEntry = { id: seq++, time: new Date().toLocaleTimeString(), source, level: "error", message, detail };
  entries = [e, ...entries].slice(0, MAX);
  notify();
  if (typeof console !== "undefined") console.error(`[${source}]`, message, detail ?? "");
}

export function pushInfo(source: string, message: string) {
  const e: ErrEntry = { id: seq++, time: new Date().toLocaleTimeString(), source, level: "info", message };
  entries = [e, ...entries].slice(0, MAX);
  notify();
}

export function clearErrors() {
  entries = [];
  notify();
}

export function subscribeErrors(fn: (es: ErrEntry[]) => void): () => void {
  listeners.add(fn);
  fn(entries);
  return () => listeners.delete(fn);
}