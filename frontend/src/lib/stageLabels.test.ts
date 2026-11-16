import { describe, expect, it } from "vitest";
import { formatHeartbeatLine, stageLabel } from "./stageLabels";

describe("stageLabel", () => {
  it("同步与出题阶段翻译成中文", () => {
    expect(stageLabel("fetching")).toBe("拉取上游数据");
    expect(stageLabel("entity_resolution")).toBe("实体规范化与消歧");
    expect(stageLabel("samples")).toBe("生成样本");
    expect(stageLabel("personas")).toBe("生成人格");
  });

  it("出题、外部导入、补生成与生命周期的阶段值都有译名", () => {
    expect(stageLabel("kg_loaded")).toBe("知识图谱已加载");
    expect(stageLabel("generating")).toBe("开始生成");
    expect(stageLabel("accounting")).toBe("用量结算");
    expect(stageLabel("plan")).toBe("生成补生成计划");
    expect(stageLabel("kg_prepared")).toBe("外部图谱已准备");
    expect(stageLabel("kg_building")).toBe("构建外部图谱");
    expect(stageLabel("kg_built")).toBe("外部图谱构建完成");
    expect(stageLabel("warn")).toBe("注意事项");
    expect(stageLabel("budget_exceeded")).toBe("超预算中止");
    expect(stageLabel("warn_mock")).toBe("演示模式提示");
    expect(stageLabel("warn_mock_done")).toBe("演示模式完成");
  });

  it("评估指标的 metric: 前缀统一翻译，未知 key 也不报错", () => {
    expect(stageLabel("metric:faithfulness")).toBe("评估指标：faithfulness");
    expect(stageLabel("metric:")).toBe("评估指标");
  });

  it("未知阶段原样显示，不抛错", () => {
    expect(stageLabel("brand_new_stage")).toBe("brand_new_stage");
    expect(stageLabel("custom:thing")).toBe("custom:thing");
  });

  it("建图的后端中文阶段名原样保留（不重复翻译）", () => {
    expect(stageLabel("提取文档标题结构")).toBe("提取文档标题结构");
  });

  it("空值返回空串", () => {
    expect(stageLabel("")).toBe("");
    expect(stageLabel(null)).toBe("");
    expect(stageLabel(undefined)).toBe("");
  });
});

describe("formatHeartbeatLine", () => {
  it("用中文阶段名与耗时/完成数拼出日志行", () => {
    expect(formatHeartbeatLine("fetching", 32, 2, 5))
      .toBe("⏳ 仍在进行：拉取上游数据，已用时 32 秒，已完成 2/5");
  });

  it("未知阶段名原样显示", () => {
    expect(formatHeartbeatLine("mystery", 15))
      .toBe("⏳ 仍在进行：mystery，已用时 15 秒");
  });

  it("没有 total 时不拼完成数", () => {
    expect(formatHeartbeatLine("projection", 15.9, 3, 0))
      .toBe("⏳ 仍在进行：生成图谱投影，已用时 15 秒");
  });
});
