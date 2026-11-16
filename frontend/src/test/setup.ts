// 测试启动前置：扩展 jest-dom 断言（toBeInTheDocument / toHaveClass 等）
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest 未开 globals，Testing Library 的自动清理不会生效：每个用例后手动卸载，
// 否则多个用例渲染的 DOM 会叠加（getByRole 会命中多个元素）。
afterEach(() => cleanup());

// jsdom 未实现指针捕获：GraphView 在 pointerdown 里调用 setPointerCapture，
// 缺这个 API 会直接抛 TypeError，节点选中/拖拽用例跑不起来。只补最小 no-op。
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => {};
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => {};
}

// jsdom 未实现滚动：配置页点「编辑」时会 scrollIntoView 回到表单顶部，缺它会抛 TypeError。
if (!HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => {};
}
// 任务日志区自动滚到底部用的是 Element.scrollTo，jsdom 同样没实现。
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => {};
}

// jsdom 未实现 SVG 坐标换算：GraphView 用它把屏幕坐标转成画布坐标。
// 返回 null 会让组件走「换算不可用」分支（坐标为 0,0），选中/拖拽判定仍按位移阈值工作。
// （getScreenCTM 在类型上属于 SVGGraphicsElement，这里按元素原型补，避免依赖具体子类）
const svgProto = SVGElement.prototype as SVGElement & { getScreenCTM?: () => DOMMatrix | null };
if (!svgProto.getScreenCTM) {
  svgProto.getScreenCTM = () => null;
}
