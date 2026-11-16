import { useCallback, useEffect, useRef, useState } from "react";
import { del, fmtBytes, get, post, uploadFiles } from "../api";
import { pushError } from "../lib/errorLog";
import { ALLOWED_EXT, filterSupported } from "../lib/documents";
import type { DocItem } from "../types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";

interface Estimate {
  total_tokens: number;
  avg_tokens: number;
  bin: string;
  warning: string | null;
}

export default function DocumentsPage({ onNext, onBack }: { onNext: () => void; onBack?: () => void }) {
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [folderFiles, setFolderFiles] = useState<File[]>([]);  // 文件夹选择器选中的文件列表
  const [est, setEst] = useState<Estimate | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => get<DocItem[]>("/api/documents").then(setDocs), []);
  // 加载失败要说出来：吞掉的话后端离线时只显示「暂无文档」，像是数据丢了
  useEffect(() => { load().catch((e) => setMsg(`文档列表加载失败：${(e as Error).message}`)); }, [load]);

  const refreshEstimate = async () => {
    try { setEst(await post<Estimate>("/api/documents/estimate", {})); }
    catch (e) { pushError("文档·总量预估", "读取文档 token 预估失败", String(e)); }
  };
  useEffect(() => { refreshEstimate(); }, [docs.length]);

  /** 移除文档：先确认，失败要提示。只删工作台里的索引与上传副本，不碰用户原文件 */
  const removeDoc = async (d: DocItem) => {
    if (!window.confirm(`确定从工作台移除「${d.name}」？`)) return;
    try {
      await del(`/api/documents/${d.id}`);
    } catch (e) {
      setMsg(`移除失败：${(e as Error).message}`);
    } finally {
      load().catch(() => {});
    }
  };

  const onUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true); setMsg(null);
    try {
      await uploadFiles(files);
      await load();
      setMsg(`已上传 ${files.length} 个文件`);
    } catch (e) { setMsg(String(e)); }
    setBusy(false);
    if (fileRef.current) fileRef.current.value = "";
  };

  const onFolderSelect = (files: FileList | null) => {
    if (!files?.length) return;
    const matched = filterSupported(files);
    setFolderFiles(matched);
    setMsg(matched.length
      ? `已选中 ${matched.length} 个支持格式的文件`
      : `该文件夹中没有支持的文件格式（${ALLOWED_EXT.join(" / ")}）`);
  };

  const onFolderConfirm = async () => {
    if (!folderFiles.length) return;
    setBusy(true); setMsg(null);
    try {
      await uploadFiles(folderFiles);
      await load();
      setMsg(`已从文件夹读取 ${folderFiles.length} 个文件`);
      setFolderFiles([]);
    } catch (e) { setMsg(String(e)); }
    setBusy(false);
    if (folderRef.current) folderRef.current.value = "";
  };

  const binBadge = (b: string) =>
    b === "long" ? { text: ">500 tokens · 长文档管线", variant: "success" as const }
    : b === "mid" ? { text: "101–500 tokens · 中等管线", variant: "info" as const }
    : { text: "<100 tokens · 不满足 ragas 要求", variant: "danger" as const };

  return (
    <div className="max-w-4xl mx-auto p-8 space-y-6">
      <header>
        {onBack && (
          <button className="text-xs text-muted-foreground hover:text-primary-strong mb-3 -ml-1 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={onBack}>← 返回上一页</button>
        )}
        <h1 className="text-xl font-semibold">文档输入</h1>
        <p className="text-sm text-muted-foreground mt-1">支持 .md / .html / .docx；多文件合并为一个 Document 列表。ragas 要求平均 ≥100 tokens，上传后自动预估。</p>
      </header>

      <div className="grid grid-cols-2 gap-4">
        <Card
          className="p-8 text-center cursor-pointer hover:border-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          role="button"
          tabIndex={0}
          aria-label="点击或拖拽上传文档"
          onClick={() => fileRef.current?.click()}
          /* N3：拖拽区原先只有鼠标可点，键盘用户按 Enter / 空格也能触发文件选择 */
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileRef.current?.click(); }
          }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); onUpload(e.dataTransfer.files); }}
        >
          <input ref={fileRef} type="file" multiple hidden accept=".md,.html,.docx,.txt" onChange={(e) => onUpload(e.target.files)} />
          <div className="text-2xl mb-2">📄</div>
          <div className="text-sm font-medium">{busy ? "处理中…" : "点击或拖拽上传"}</div>
          <div className="text-xs text-muted-foreground mt-1">单文件 / 多文件</div>
        </Card>
        <Card className="p-5 space-y-3">
          <div className="text-sm font-medium">本地文件夹</div>
          <input ref={folderRef} type="file" {...({ webkitdirectory: "" } as Record<string, unknown>)} hidden onChange={(e) => onFolderSelect(e.target.files)} />
          <Button variant="outline" className="w-full" disabled={busy} onClick={() => folderRef.current?.click()}>选择文件夹…</Button>

          {folderFiles.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground flex items-center gap-2">
                <span>匹配文件（{folderFiles.length}）</span>
                <span className="ml-auto">格式：{ALLOWED_EXT.join(" / ")}</span>
              </div>
              <div className="bg-background border border-border rounded-md max-h-48 overflow-y-auto divide-y divide-border">
                {folderFiles.map((f, i) => (
                  <div key={i} className="px-3 py-1.5 flex items-center gap-2 text-xs">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
                    <span className="flex-1 min-w-0 truncate" title={f.webkitRelativePath || f.name}>
                      {f.webkitRelativePath || f.name}
                    </span>
                    <span className="text-muted-foreground shrink-0">{fmtBytes(f.size)}</span>
                  </div>
                ))}
              </div>
              <Button className="w-full" disabled={busy || !folderFiles.length} onClick={onFolderConfirm}>
                {busy ? "读取中…" : `确认读取（${folderFiles.length} 个文件）`}
              </Button>
            </div>
          )}
        </Card>
      </div>

      {msg && <div className="text-xs text-muted-foreground">{msg}</div>}

      {est && (
        <Card className="px-5 py-3 flex items-center gap-4 text-sm">
          <span>合计预估 <b className="text-primary-strong">{est.total_tokens.toLocaleString()}</b> tokens</span>
          <span className="text-muted-foreground">|</span>
          <span>平均 {est.avg_tokens} tokens/文档</span>
          <Badge variant={binBadge(est.bin).variant} className="px-2 py-0.5 rounded-full text-xs">{binBadge(est.bin).text}</Badge>
        </Card>
      )}

      <Card className="divide-y divide-border">
        <CardHeader className="px-5 py-3 text-sm font-medium text-muted-foreground">已加载文档（{docs.length}）</CardHeader>
        {docs.length === 0 && <div className="px-5 py-6 text-sm text-muted-foreground">暂无文档。</div>}
        {docs.map((d) => (
          <div key={d.id} className="px-5 py-3 flex items-center gap-4 text-sm">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
            <span className="flex-1 min-w-0 truncate">{d.name}</span>
            <span className="text-xs text-muted-foreground w-20 text-right">{fmtBytes(d.size)}</span>
            <span className="text-xs text-primary-strong w-28 text-right">≈{d.est_tokens.toLocaleString()} tokens</span>
            <Button variant="danger" size="xs" onClick={() => removeDoc(d)}>移除</Button>
          </div>
        ))}
      </Card>

      {docs.length > 0 && (
        <Card className="p-5 flex items-center justify-between">
          <div>
            <div className="text-sm font-medium">生成知识图谱</div>
            <div className="text-xs text-muted-foreground mt-0.5">基于已加载的 {docs.length} 个文档，调用 LLM 抽取实体与关系</div>
          </div>
          <Button onClick={onNext}>生成知识图谱 →</Button>
        </Card>
      )}
    </div>
  );
}
