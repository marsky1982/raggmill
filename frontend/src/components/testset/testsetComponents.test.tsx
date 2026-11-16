import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { RegeneratePlan, Sample, TestsetDetail } from "../../types";

// 补生成计划的读取接口不在本文件覆盖范围内；错误日志走真实实现但只写内存
vi.mock("../../lib/errorLog", () => ({ pushError: vi.fn() }));

import ExportMenu from "./ExportMenu";
import GenerateForm, { type GenerateFormState, type CostSettings } from "./GenerateForm";
import ProvenanceCard from "./ProvenanceCard";
import SampleList from "./SampleList";

const kgs = [{
  id: "kg-1", name: "知识库", node_count: 10, relationship_count: 20,
  created_at: "2026-11-01", source_files: [], mode: "ragas",
}];
const form: GenerateFormState = { kg: "kg-1", llm: "p", emb: "e", size: 10, name: "" };
const cost: CostSettings = { maxRetries: 3, timeout: 180, workers: 8, budget: 0, personas: 3 };
const dist = { single_hop_specific: 0.5, multi_hop_abstract: 0.25, multi_hop_specific: 0.25 };

function renderForm(caps: Record<string, any> | null, over: Partial<React.ComponentProps<typeof GenerateForm>> = {}) {
  const props: React.ComponentProps<typeof GenerateForm> = {
    kgs, form, onFormChange: vi.fn(),
    language: "zh", onLanguageChange: vi.fn(), langOptions: [{ value: "zh", label: "中文" }],
    relProp: "summary_similarity", onRelPropChange: vi.fn(),
    useDefault: true, onUseDefaultChange: vi.fn(),
    dist, onDistChange: vi.fn(), splits: null, normalized: false, onNormalize: vi.fn(),
    cost, onCostChange: vi.fn(),
    estimate: null, estimating: false, onEstimate: vi.fn(),
    caps,
    health: null, healthLoading: false, onCheckHealth: vi.fn(),
    onCloseHealth: vi.fn(), onAdoptRelProp: vi.fn(),
    busy: false, onGenerate: vi.fn(),
    ...over,
  };
  render(<GenerateForm {...props} />);
  return props;
}

describe("GenerateForm", () => {
  it("当前分布要用的题型缺素材时禁用生成并给出原因", () => {
    renderForm({
      synthesizers: {
        single_hop_specific: { label: "单跳具体", reason: "缺少实体", effective_available: false },
        multi_hop_abstract: { label: "多跳抽象", reason: "ok", effective_available: true },
        multi_hop_specific: { label: "多跳具体", reason: "ok", effective_available: true },
      },
    });
    expect(screen.getByRole("button", { name: "生成测试集" })).toBeDisabled();
    expect(screen.getByText(/单跳具体 不可用：缺少实体/)).toBeInTheDocument();
  });

  it("不在当前分布里的题型缺失只提示、不阻塞生成", () => {
    renderForm({
      synthesizers: {
        single_hop_specific: { label: "单跳具体", reason: "ok", effective_available: true },
        multi_hop_abstract: { label: "多跳抽象", reason: "缺少文档摘要", effective_available: false },
        multi_hop_specific: { label: "多跳具体", reason: "ok", effective_available: true },
      },
    }, { useDefault: false, dist: { single_hop_specific: 1, multi_hop_abstract: 0, multi_hop_specific: 0 }, splits: {} });
    expect(screen.getByRole("button", { name: "生成测试集" })).toBeEnabled();
    expect(screen.getByText(/当前分布未使用，不影响生成/)).toBeInTheDocument();
  });

  it("缺素材但可自动补齐时只提示，不出现在不可用列表里", () => {
    renderForm({
      synthesizers: {
        single_hop_specific: { label: "单跳具体", reason: "将自动补齐分块摘要", effective_available: true, auto_enhanceable: true },
      },
    });
    expect(screen.getByText(/单跳具体：将自动补齐分块摘要/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成测试集" })).toBeEnabled();
  });
});

describe("ExportMenu", () => {
  it("三个导出组合各自带上格式与范围", async () => {
    const onExport = vi.fn();
    render(<ExportMenu onExport={onExport} />);
    // Radix 下拉靠 pointerdown 打开（与 TestsetPage.test.tsx 的既有写法一致）
    fireEvent.pointerDown(screen.getByRole("button", { name: "导出" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByText("CSV · 全部"));
    fireEvent.pointerDown(screen.getByRole("button", { name: "导出" }), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByText("JSONL · 仅采纳"));
    expect(onExport.mock.calls).toEqual([["csv", "all"], ["jsonl", "adopted"]]);
  });

  it("导出中禁用触发按钮", () => {
    render(<ExportMenu disabled onExport={vi.fn()} />);
    expect(screen.getByRole("button", { name: "导出" })).toBeDisabled();
  });
});

const sample: Sample = {
  index: 0, synthesizer_name: "single_hop_specific", user_input: "问题一",
  reference: "答案一", reference_contexts: ["上下文一"], response: "", adopted: false,
};
const detail = {
  id: "ts-1", name: "测试集", created_at: "2026-11-01", kg_id: "kg-1", total: 1,
  stats: { single_hop_specific: 1 }, groups: { single_hop_specific: [sample] },
  stop_reason: "completed",
} as unknown as TestsetDetail;

function sampleListProps(over: Partial<React.ComponentProps<typeof SampleList>> = {}): React.ComponentProps<typeof SampleList> {
  return {
    detail, visible: [sample], busy: false,
    filterType: "", onFilterType: vi.fn(), keyword: "", onKeyword: vi.fn(),
    adoptOnly: false, onAdoptOnly: vi.fn(),
    exporting: false, onExport: vi.fn(), onToggleAdopt: vi.fn(),
    affectedStatus: () => undefined,
    plan: null, planErr: null, onRegenerate: vi.fn(),
    langOptions: [], onOpenTestset: vi.fn(),
    ...over,
  };
}

function renderList(over: Partial<React.ComponentProps<typeof SampleList>> = {}) {
  const props = sampleListProps(over);
  render(<SampleList {...props} />);
  return props;
}

describe("SampleList", () => {
  it("画出样本并支持采纳标记", () => {
    const props = renderList();
    expect(screen.getByText("问题一")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "丢弃" }));
    expect(props.onToggleAdopt).toHaveBeenCalledWith(sample);
  });

  it("过滤后为空时给出空态", () => {
    renderList({ visible: [] });
    expect(screen.getByText("无匹配样本。")).toBeInTheDocument();
  });

  it("按当前投影判定为 orphan 的样本显示「来源已删除」徽章", () => {
    renderList({ affectedStatus: () => "orphan" });
    expect(screen.getByText("来源已删除")).toBeInTheDocument();
  });

  it("切换测试集（页面按 detail.id 重新挂载）后不保留上一次的补生成名称/语言", () => {
    const plan = {
      counts: { valid: 1, stale: 1, orphan: 0, legacy: 0, unknown: 0 }, regenerate: 1, keep: 1,
      regenerate_by_type: { stale: 1 }, judged_against: "current_projection", testset_name: "旧集",
    } as unknown as RegeneratePlan;
    const props = sampleListProps({ plan });
    const { rerender } = render(<SampleList key={detail.id} {...props} />);
    fireEvent.change(screen.getByLabelText("结果名称"), { target: { value: "临时名称" } });
    fireEvent.change(screen.getByLabelText("出题语言"), { target: { value: "zh" } });
    expect(screen.getByLabelText("结果名称")).toHaveValue("临时名称");

    rerender(<SampleList key="ts-2" {...props} />);

    expect(screen.getByLabelText("结果名称")).toHaveValue("");
    expect(screen.getByLabelText("出题语言")).toHaveValue("");
  });
});

describe("ProvenanceCard", () => {
  it("接口不存在时展示后端启动时间与代码版本", () => {
    render(<ProvenanceCard
      plan={null}
      error={{ kind: "endpoint_missing", message: "404 Not Found", startedAt: "2026-11-14T02:00:00Z", commit: "abc1234" }}
      busy={false} onRegenerate={vi.fn()}
      name="" onNameChange={vi.fn()} language="" onLanguageChange={vi.fn()} langOptions={[]} />);
    expect(screen.getByText(/后端接口不存在/)).toBeInTheDocument();
    expect(screen.getByText("abc1234")).toBeInTheDocument();
  });

  it("有可补生成条目时展示名称/语言并回调", () => {
    const onRegenerate = vi.fn();
    render(<ProvenanceCard
      plan={{
        counts: { valid: 1, stale: 1, orphan: 0, legacy: 0, unknown: 0 }, regenerate: 1, keep: 1,
        regenerate_by_type: { stale: 1 }, judged_against: "current_projection", testset_name: "旧集",
      } as unknown as RegeneratePlan}
      error={null} busy={false} onRegenerate={onRegenerate}
      name="" onNameChange={vi.fn()} language="" onLanguageChange={vi.fn()}
      langOptions={[{ value: "zh", label: "中文" }]} />);
    expect(screen.getByText(/对照数据源当前图谱/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/默认：旧集·局部补生成/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "局部补生成" }));
    expect(onRegenerate).toHaveBeenCalled();
  });
});
