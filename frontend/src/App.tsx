import { useEffect, useState } from "react";
import ConfigPage from "./pages/ConfigPage";
import DocumentsPage from "./pages/DocumentsPage";
import KgPage from "./pages/KgPage";
import TestsetPage from "./pages/TestsetPage";
import EvaluationPage from "./pages/EvaluationPage";
import DebugDrawer from "./components/DebugDrawer";
import ThemeToggle from "./components/ThemeToggle";
import { get } from "./api";
import { Badge } from "@/components/ui/badge";

const STEPS = [
  { key: "config", label: "模型配置", desc: "LLM / Embedding 预设" },
  { key: "docs", label: "文档输入", desc: "上传 / 文件夹" },
  { key: "kg", label: "知识图谱", desc: "生成与预览" },
  { key: "testset", label: "测试集", desc: "预览与导出" },
  { key: "evaluation", label: "质量评估", desc: "五指标与结果导出" },
] as const;

type StepKey = (typeof STEPS)[number]["key"];

export default function App() {
  const [step, setStep] = useState<StepKey>("config");
  const [mode, setMode] = useState<string>("...");

  useEffect(() => {
    get<{ mode: string }>("/api/health").then((h) => setMode(h.mode)).catch(() => setMode("offline"));
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      {/* 侧边栏 */}
      <aside className="w-60 shrink-0 bg-card border-r border-border flex flex-col">
        <div className="px-5 py-5 border-b border-border">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-md bg-primary flex items-center justify-center text-[11px] font-bold text-primary-foreground">R</div>
            <div className="font-semibold text-sm">RAGgmill</div>
          </div>
        </div>
        <nav className="flex-1 p-3 space-y-1">
          {STEPS.map((s, i) => (
            <button
              key={s.key}
              onClick={() => setStep(s.key)}
              className={`w-full text-left px-3 py-2.5 rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                step === s.key ? "bg-primary/10 text-primary-strong border border-primary/40" : "text-muted-foreground hover:bg-muted hover:text-foreground border border-transparent"
              }`}
            >
              <div className="flex items-center gap-2 text-sm font-medium">
                <span className={`w-4 h-4 rounded-full text-[11px] flex items-center justify-center border ${
                  step === s.key ? "border-primary text-primary-strong" : "border-border text-muted-foreground"
                }`}>{i + 1}</span>
                {s.label}
              </div>
              <div className="text-xs text-muted-foreground mt-0.5 pl-6">{s.desc}</div>
            </button>
          ))}
        </nav>
        <div className="p-3 border-t border-border text-xs space-y-2">
          <ThemeToggle />
          {/* 调试入口从右下角浮钮挪到这里（视觉整改 V4 的正式方案）：和「演示模式」徽章同一行 */}
          <div className="flex items-center gap-2">
            <Badge variant={mode === "ragas" ? "success" : "warning"} size="pill" className="py-1 gap-1.5 font-normal min-w-0">
              <span className="w-1.5 h-1.5 rounded-full bg-current shrink-0" />
              <span className="truncate">
                {mode === "ragas" ? "ragas 引擎已接入" : mode === "demo" ? "演示模式（模拟数据）" : "后端未连接"}
              </span>
            </Badge>
            <DebugDrawer />
          </div>
        </div>
      </aside>

      {/* 主工作区 */}
      <main className="flex-1 overflow-y-auto pb-12">
        {step === "config" && <ConfigPage onNext={() => setStep("docs")} />}
        {step === "docs" && <DocumentsPage onNext={() => setStep("kg")} onBack={() => setStep("config")} />}
        {step === "kg" && <KgPage onNext={() => setStep("testset")} onBack={() => setStep("docs")} />}
        {step === "testset" && <TestsetPage onBack={() => setStep("kg")} />}
        {step === "evaluation" && <EvaluationPage />}
      </main>
    </div>
  );
}
