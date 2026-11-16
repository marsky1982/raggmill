import { describe, expect, it } from "vitest";
import { formatPropValue, PROP_LABELS } from "./kgFormat";

describe("formatPropValue", () => {
  it("数值数组按向量格式只展示前三维", () => {
    expect(formatPropValue([0.1234, 0.5678, 0.9, 1])).toBe("向量[4] 示例: 0.123, 0.568, 0.900");
  });

  it("字符串数组用顿号连接", () => {
    expect(formatPropValue(["实体A", "实体B"])).toBe("实体A、实体B");
  });

  it("对象序列化为 JSON", () => {
    expect(formatPropValue({ filename: "a.md" })).toBe('{"filename":"a.md"}');
  });

  it("原始值转字符串", () => {
    expect(formatPropValue(42)).toBe("42");
    expect(formatPropValue("正文")).toBe("正文");
  });
});

describe("PROP_LABELS", () => {
  it("覆盖 KG 详情实际出现的属性键", () => {
    for (const key of ["page_content", "themes", "entities", "document_metadata", "embedding"]) {
      expect(PROP_LABELS[key]).toBeTruthy();
    }
  });
});
