import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge } from "./badge";

describe("Badge size 变体（11px 徽章收口）", () => {
  it("默认尺寸仍是 text-xs（不改既有大徽章）", () => {
    render(<Badge>默认</Badge>);
    const badge = screen.getByText("默认");
    expect(badge.className).toContain("text-xs");
    expect(badge.className).not.toContain("text-[11px]");
  });

  it("size=xs 提供 11px 紧凑方角，页面不再重复写字号", () => {
    render(<Badge size="xs">MOCK</Badge>);
    const badge = screen.getByText("MOCK");
    expect(badge.className).toContain("text-[11px]");
    expect(badge.className).toContain("px-1.5");
    expect(badge.className).toContain("rounded");
  });

  it("size=pill 提供 11px 胶囊", () => {
    render(<Badge size="pill">演示</Badge>);
    const badge = screen.getByText("演示");
    expect(badge.className).toContain("text-[11px]");
    expect(badge.className).toContain("rounded-full");
  });

  it("页面传入的 className 可以覆盖尺寸（tailwind-merge 生效）", () => {
    render(<Badge size="pill" className="px-1.5">紧凑胶囊</Badge>);
    expect(screen.getByText("紧凑胶囊").className).toContain("px-1.5");
  });
});
