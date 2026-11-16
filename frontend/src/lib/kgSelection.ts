/**
 * 轻量跨页面状态：KG 页点「生成测试集」时写入，测试集页读取后清空。
 * 比 URL query 更干净（不污染地址栏），比 context 更轻（无需改 App 层级）。
 */

let _selectedKgId: string | null = null;

export function setSelectedKg(id: string | null): void {
  _selectedKgId = id;
}

export function consumeSelectedKg(): string | null {
  const id = _selectedKgId;
  _selectedKgId = null;
  return id;
}
