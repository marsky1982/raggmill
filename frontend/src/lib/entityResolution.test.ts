import { describe, expect, it } from "vitest";
import {
  ENTITY_SWITCH_OFF,
  ENTITY_SWITCH_ON,
  entityResolutionLabel,
  entityStatsText,
  entitySwitchOn,
  entitySwitchState,
  pendingText,
} from "./entityResolution";

const ready = { hasDataset: true, legacy: false, busy: false, hasPreset: true };

describe("entitySwitchState", () => {
  it("条件齐备时可用", () => {
    expect(entitySwitchState(ready)).toEqual({ disabled: false, reason: null });
  });

  it("未选库 / legacy / 同步探测中 / 无预设各自给出禁用原因", () => {
    expect(entitySwitchState({ ...ready, hasDataset: false }).reason).toContain("先选一个知识库");
    expect(entitySwitchState({ ...ready, legacy: true }).reason).toContain("只读");
    expect(entitySwitchState({ ...ready, busy: true }).reason).toContain("同步或检查进行中");
    expect(entitySwitchState({ ...ready, hasPreset: false }).reason).toContain("LLM 预设");
    expect(entitySwitchState({ ...ready, hasPreset: false }).disabled).toBe(true);
  });
});

describe("pendingText", () => {
  it("没有待生效变更时不显示", () => {
    expect(pendingText(false)).toBeNull();
  });

  it("已有投影时提示会重建，没有投影时提示下次同步生效", () => {
    expect(pendingText(true, true)).toBe("下次同步会重建投影");
    expect(pendingText(true, false)).toBe("下次同步生效");
  });
});

describe("entityResolutionLabel", () => {
  it("四种模式有中文名，缺失显示未记录", () => {
    expect(entityResolutionLabel("disabled")).toBe("关闭");
    expect(entityResolutionLabel("rules")).toBe("规则");
    expect(entityResolutionLabel("llm")).toBe("仅 LLM");
    expect(entityResolutionLabel("hybrid")).toBe("LLM 混合");
    expect(entityResolutionLabel(null)).toBe("未记录");
    expect(entityResolutionLabel(undefined)).toBe("未记录");
  });
});

describe("entitySwitchOn", () => {
  it("disabled 视为关；rules/llm/hybrid 视为开", () => {
    expect(entitySwitchOn(ENTITY_SWITCH_OFF)).toBe(false);
    expect(entitySwitchOn(null)).toBe(false);
    expect(entitySwitchOn(ENTITY_SWITCH_ON)).toBe(true);
    expect(entitySwitchOn("rules")).toBe(true);
  });
});

describe("entityStatsText", () => {
  it("关闭态不显示统计行", () => {
    expect(entityStatsText({ mode: "disabled" })).toBeNull();
    expect(entityStatsText(null)).toBeNull();
  });

  it("开启态给出候选、规则、LLM、缓存与 token", () => {
    const text = entityStatsText({
      mode: "hybrid", pairs_observed: 12, pairs_sent: 12, rule_hits: 5, llm_calls: 3, cache_hits: 4,
      tokens: { input: 1800, output: 240 },
    });
    expect(text).toContain("LLM 混合");
    expect(text).toContain("候选 12 对");
    expect(text).toContain("规则命中 5");
    expect(text).toContain("LLM 3 次");
    expect(text).toContain("缓存命中 4");
    expect(text).toContain("token");
  });

  it("发生截断时同时给出真实候选、被截断数与送 LLM 对数", () => {
    const text = entityStatsText({
      mode: "hybrid", pairs_observed: 413, pairs_truncated: 213, pairs_sent: 200,
      rule_hits: 7, llm_calls: 200, cache_hits: 0,
    });
    expect(text).toContain("候选 413 对（截断 213，送 LLM 200）");
  });
});
