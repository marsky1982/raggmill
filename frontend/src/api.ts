/** FastAPI 的 detail 可能是字符串，也可能是 422 校验错误数组（[{loc, msg}]）——数组直接塞进
 *  Error 会显示成 [object Object]，这里拼成「字段: 原因」。 */
export function formatDetail(detail: unknown): string {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      if (d && typeof d === "object") {
        const item = d as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(item.loc) ? item.loc.filter((x) => x !== "body").join(".") : "";
        return field ? `${field}：${item.msg ?? ""}` : String(item.msg ?? JSON.stringify(d));
      }
      return String(d);
    }).join("；");
  }
  return JSON.stringify(detail);
}

/** 后端错误统一从这里抛：error_code 来自业务错误体（如 RAGFlow / 测试集不存在），
 *  endpointMissing 专指「接口本身不存在」——FastAPI 路由未注册时只回裸 {"detail":"Not Found"}，
 *  与带 error_code 的业务 404 是两回事（常见于后端仍是旧进程）。 */
export type ApiError = Error & {
  errorCode?: string;
  upstreamStatus?: number | null;
  endpointMissing?: boolean;
  /** 业务 409 的可选明细（如 KG 删除被数据源投影引用时的 sources） */
  sources?: {
    source_id?: string | null;
    dataset_id?: string | null;
    connection_id?: string | null;
    connection_name?: string | null;
  }[];
};

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = res.statusText;
    let errorCode: string | undefined;
    let upstreamStatus: number | null | undefined;
    let sources: ApiError["sources"];
    try {
      const body = await res.json();
      msg = formatDetail(body.detail) || body.error?.message || JSON.stringify(body);
      errorCode = body.error_code;
      // 附加信息统一在 extra 里（上游状态码 / 被引用的数据源），不再平铺到顶层
      upstreamStatus = body.extra?.upstream_status;
      sources = body.extra?.sources;
    } catch { /* ignore */ }
    // RAGFlow 六类错误文案要按 error_code 映射，故把错误码挂在 Error 上
    const err = new Error(msg) as ApiError;
    err.errorCode = errorCode;
    err.upstreamStatus = upstreamStatus;
    err.sources = sources;
    // detail 就是 FastAPI 的 "Not Found" → 接口不存在，而非业务对象不存在（常见于后端仍是旧进程）
    err.endpointMissing = res.status === 404 && msg.trim() === "Not Found";
    throw err;
  }
  return res.json() as Promise<T>;
}

export const get = <T,>(url: string) => fetch(url).then((r) => j<T>(r));
const writeToken = import.meta.env.VITE_STUDIO_WRITE_TOKEN as string | undefined;
const writeHeaders = (json = false): HeadersInit => ({
  ...(json ? { "Content-Type": "application/json" } : {}),
  ...(writeToken ? { "X-RAGAS-Write-Token": writeToken } : {}),
});
export const post = <T,>(url: string, body?: unknown) =>
  fetch(url, {
    method: "POST",
    headers: writeHeaders(true),
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => j<T>(r));
export const patch = <T,>(url: string, body: unknown) =>
  fetch(url, { method: "PATCH", headers: writeHeaders(true), body: JSON.stringify(body) }).then((r) => j<T>(r));
export const put = <T,>(url: string, body: unknown) =>
  fetch(url, { method: "PUT", headers: writeHeaders(true), body: JSON.stringify(body) }).then((r) => j<T>(r));
export const del = <T,>(url: string) => fetch(url, { method: "DELETE", headers: writeHeaders() }).then((r) => j<T>(r));
/** 写请求但要拿文件流（导出）：同样带写令牌；失败按 j() 的口径抛错。
 *  所有 POST/PUT/PATCH/DELETE 必须经本文件的出口发出（backend/tests/test_frontend_write_exit.py 守护）。 */
export async function postForBlob(url: string, body?: unknown): Promise<{ blob: Blob; filename: string | null }> {
  const res = await fetch(url, {
    method: "POST",
    headers: writeHeaders(true),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await j<never>(res);
  return { blob: await res.blob(), filename: dispositionFilename(res.headers.get("Content-Disposition")) };
}

/** 读 Content-Disposition 里的文件名（优先 RFC 5987 的 filename*=UTF-8''…），
 *  让后端决定的文件名（如演示数据后缀）原样落到用户磁盘上。 */
export function dispositionFilename(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star) {
    try { return decodeURIComponent(star[1].trim()); } catch { /* 落到 ASCII 名 */ }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

export function uploadFiles<T>(files: FileList | File[]): Promise<T> {
  const fd = new FormData();
  // 带上文件夹内相对路径：后端只取最后一段作文档名，但重名报错时能指出是哪几个文件
  Array.from(files).forEach((f) => fd.append("files", f, f.webkitRelativePath || f.name));
  return fetch("/api/documents/upload", { method: "POST", headers: writeHeaders(), body: fd }).then((r) => j<T>(r));
}

/** 任务进度事件载荷（后端 manager.log 的 extra 会原样并入）。 */
export interface TaskProgressEvent {
  status: string; progress: number; stage: string; log?: string;
  stage_index?: number; stage_total?: number; node_count?: number;
  relationship_count?: number; checkpoint?: boolean; resumed?: boolean;
  run_id?: string;
  error?: string; seq?: number;
  /** 终态细分（目前只有建图超预算为 "budget_exceeded"）；status 仍是标准四态 */
  stop_reason?: string;
  /** ---- 骨架心跳（PR9）：长阶段无日志/进度时补发的结构化事件 ---- */
  heartbeat?: boolean;
  stage_elapsed_s?: number;
  done?: number;
  total?: number;
  /** ---- Step 2 生成的 LLM 级遥测（来自 ragas_adapter/telemetry.py）---- */
  llm_calls?: number;
  llm_calls_ok?: number;
  llm_calls_failed?: number;
  llm_retries?: number;
  llm_fix_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  elapsed_s?: number;
  budget_exceeded?: boolean;
}

/** 任务快照（GET /api/tasks/{id} 与 SSE 首连发的 task.view()）：带最近日志，用量在嵌套的 usage 里。 */
export interface TaskSnapshotEvent extends TaskProgressEvent {
  task_id: string;
  logs?: string[];
}

export interface SseHandlers {
  onProgress?: (data: TaskProgressEvent) => void;
  /** 快照：只用来刷新状态、补回日志；不参与逐条日志的去重序号。 */
  onSnapshot?: (data: TaskSnapshotEvent) => void;
  onDone?: (data: { status: string; result?: Record<string, unknown>; error?: string; stop_reason?: string }) => void;
}

/** 快照的用量是嵌套的 usage，逐条事件是扁平字段；统一摊平，遥测面板两种来源都能读（F2）。 */
function flattenSnapshot(data: Record<string, unknown>): TaskSnapshotEvent {
  const usage = (data.usage && typeof data.usage === "object") ? data.usage as Record<string, unknown> : {};
  return { ...usage, ...data } as unknown as TaskSnapshotEvent;
}

/** 订阅任务 SSE 流；返回关闭函数。 */
export function subscribeTask(taskId: string, h: SseHandlers): () => void {
  const es = new EventSource(`/api/tasks/${taskId}/stream`);
  let closed = false;
  let lastSeq = -1;
  let polling = false;
  const finish = (data: { status: string; result?: Record<string, unknown>; error?: string; stop_reason?: string }) => {
    if (closed) return;
    closed = true;
    es.close();
    window.clearInterval(timer);
    h.onDone?.(data);
  };
  const receive = (data: TaskProgressEvent & { result?: Record<string, unknown> }) => {
    if (closed) return;
    if (["succeeded", "failed", "canceled", "interrupted"].includes(data.status)) {
      finish(data); return;
    }
    // 快照（带 task_id）的 seq 是「当前最新」，不能拿来推进 lastSeq：否则断线重连时
    // 服务端按 Last-Event-ID 回放的日志会全部被当成已收到而丢弃
    if ("task_id" in data) {
      const snap = flattenSnapshot(data as unknown as Record<string, unknown>);
      if (h.onSnapshot) h.onSnapshot(snap);
      else h.onProgress?.({ ...snap, log: undefined });
      return;
    }
    if ((data.seq ?? 0) <= lastSeq) return;
    lastSeq = data.seq ?? 0;
    h.onProgress?.(data);
  };
  const poll = async () => {
    if (closed || polling) return;
    polling = true;
    try {
      const res = await fetch(`/api/tasks/${taskId}`);
      if (closed) return;
      if (res.status === 404) {
        finish({ status: "interrupted", error: "任务记录不存在，服务可能已重启，请核对已有结果后重试" });
      } else if (res.ok) receive(await res.json());
    } catch {
      if (!closed) h.onProgress?.({status: "reconnecting", progress: 0, stage: "连接中断，正在恢复任务状态"});
    } finally { polling = false; }
  };
  const timer = window.setInterval(() => { void poll(); }, 3000);
  es.addEventListener("progress", (e) => receive(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => finish(JSON.parse((e as MessageEvent).data)));
  es.onerror = () => { void poll(); }; // EventSource自动重连并发送Last-Event-ID
  void poll();
  return () => {
    closed = true;
    es.close();
    window.clearInterval(timer);
  };
}

export const fmtBytes = (n: number) =>
  n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`;
