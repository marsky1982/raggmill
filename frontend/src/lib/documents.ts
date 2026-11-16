/** 文档上传的本地筛选规则（纯逻辑，供页面与测试共用）。 */

export const ALLOWED_EXT = [".md", ".html", ".docx", ".txt"];

/** 从 webkitdirectory 返回的 FileList 中过滤出支持的文件，按路径排序 */
export function filterSupported(files: FileList | File[]): File[] {
  const arr = Array.from(files);
  return arr
    .filter((f) => {
      const ext = "." + f.name.split(".").pop()?.toLowerCase();
      return ALLOWED_EXT.includes(ext);
    })
    .sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name));
}
