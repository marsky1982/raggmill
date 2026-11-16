import { describe, expect, it } from "vitest";
import { consumeSelectedKg, setSelectedKg } from "./kgSelection";

describe("kgSelection", () => {
  it("写入后读取一次即清空（避免重复预选）", () => {
    setSelectedKg("kg-1");
    expect(consumeSelectedKg()).toBe("kg-1");
    expect(consumeSelectedKg()).toBeNull();
  });

  it("可显式写入 null 覆盖上一次选择", () => {
    setSelectedKg("kg-2");
    setSelectedKg(null);
    expect(consumeSelectedKg()).toBeNull();
  });
});
