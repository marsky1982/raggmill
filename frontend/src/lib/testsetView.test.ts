import { describe, expect, it } from "vitest";
import {
  DIST_KEYS, MAX_TESTSET_NAME_LEN, REGEN_TYPE_LABEL, STYLE_HINT, TYPE_COLORS, TYPE_TEXT,
  matchType, normalizeDistribution, regenStatusText, statusVariant, stopLabel, testsetNameError,
} from "./testsetView";

describe("testsetNameError", () => {
  it("空名称合法（后端按图谱名自动命名）", () => {
    expect(testsetNameError("")).toBeNull();
    expect(testsetNameError("   ")).toBeNull();
  });

  it("恰好达到上限合法，超一个字给中文提示", () => {
    expect(testsetNameError("名".repeat(MAX_TESTSET_NAME_LEN))).toBeNull();
    expect(testsetNameError("名".repeat(MAX_TESTSET_NAME_LEN + 1)))
      .toBe(`测试集名称最多 ${MAX_TESTSET_NAME_LEN} 个字符`);
  });

  it("按去空白后的长度判定，首尾空白不占额度", () => {
    expect(testsetNameError(`  ${"名".repeat(MAX_TESTSET_NAME_LEN)}  `)).toBeNull();
  });
});

describe("stopLabel", () => {
  it("已知终态显示中文", () => {
    expect(stopLabel("completed")).toBe("已完成");
    expect(stopLabel("budget_exceeded")).toBe("超预算中止");
  });

  it("历史数据缺失显示「未记录」，未知枚举不把英文透出", () => {
    expect(stopLabel()).toBe("未记录");
    expect(stopLabel("weird")).toBe("未知状态");
  });
});

describe("normalizeDistribution", () => {
  it("比例和不为 1 时按总和折算并保留 4 位", () => {
    expect(normalizeDistribution({ single_hop_specific: 50, multi_hop_abstract: 25, multi_hop_specific: 25 }))
      .toEqual({ single_hop_specific: 0.5, multi_hop_abstract: 0.25, multi_hop_specific: 0.25 });
  });

  it("全为 0 时各项归 0（避免除零）", () => {
    expect(normalizeDistribution({ a: 0, b: 0 })).toEqual({ a: 0, b: 0 });
  });

  it("0.6/0.25/0.25 归一化后合计恰为 1（旧 toFixed(4) 恒定得到 1.0001）", () => {
    const out = normalizeDistribution({ single_hop_specific: 0.6, multi_hop_abstract: 0.25, multi_hop_specific: 0.25 });
    const sum = Object.values(out).reduce((a, b) => a + b, 0);
    expect(Math.abs(sum - 1)).toBeLessThan(1e-6);
    // 最大余数法：5454 + 2272 + 2272，两个余数更大的项各补 1
    expect(out).toEqual({ single_hop_specific: 0.5454, multi_hop_abstract: 0.2273, multi_hop_specific: 0.2273 });
  });

  it("三等分也按最大余数法补齐，合计恰为 1", () => {
    const out = normalizeDistribution({ a: 1, b: 1, c: 1 });
    expect(Object.values(out).reduce((x, y) => x + y, 0)).toBeCloseTo(1, 10);
  });
});

describe("matchType", () => {
  it("分布 key 与 ragas 合成器类名一一对应", () => {
    expect(matchType("SingleHopSpecificQuery", "single_hop_specific")).toBe(true);
    expect(matchType("MultiHopAbstractQuery", "multi_hop_abstract")).toBe(true);
    expect(matchType("MultiHopSpecificQuery", "multi_hop_specific")).toBe(true);
  });

  it("不匹配与未知 key 都返回 false", () => {
    expect(matchType("MultiHopAbstractQuery", "single_hop_specific")).toBe(false);
    expect(matchType("X", "unknown")).toBe(false);
  });
});

describe("regenStatusText", () => {
  it("中文映射后拼成一句", () => {
    expect(regenStatusText({ stale: 2, orphan: 1 })).toBe("内容已变 2、来源已删除 1");
  });

  it("空对象显示「无」，未知状态保留原 key", () => {
    expect(regenStatusText({})).toBe("无");
    expect(regenStatusText({ future: 3 })).toBe("future 3");
  });
});

describe("statusVariant", () => {
  it("质检状态映射到徽章变体", () => {
    expect(statusVariant("可用")).toBe("success");
    expect(statusVariant("受限")).toBe("warning");
    expect(statusVariant("缺失")).toBe("danger");
  });
});

describe("题型/风格映射表", () => {
  it("三类合成器都有色值且有深色主题字色", () => {
    for (const { key } of DIST_KEYS) {
      const synth = matchTypeName(key);
      expect(TYPE_COLORS[synth]).toMatch(/^#/);
      expect(TYPE_TEXT[synth]).toContain("dark:");
    }
  });

  it("ragas 枚举值与可读文案两种写法都能命中", () => {
    expect(STYLE_HINT.MISSPELLED.bad).toBe(true);
    expect(STYLE_HINT["Misspelled queries"].label).toBe("故意错别字");
    expect(STYLE_HINT.PERFECT_GRAMMAR.bad).toBeUndefined();
  });

  it("补生成题型都有中文名", () => {
    for (const key of ["single_hop_specific", "multi_hop_abstract", "multi_hop_specific"]) {
      expect(REGEN_TYPE_LABEL[key]).toBeTruthy();
    }
  });
});

function matchTypeName(key: string): string {
  const map: Record<string, string> = {
    single_hop_specific: "SingleHopSpecificQuery",
    multi_hop_abstract: "MultiHopAbstractQuery",
    multi_hop_specific: "MultiHopSpecificQuery",
  };
  return map[key];
}
