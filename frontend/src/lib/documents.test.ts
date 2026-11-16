import { describe, expect, it } from "vitest";
import { ALLOWED_EXT, filterSupported } from "./documents";

function fakeFile(name: string, path = ""): File {
  const f = new File(["x"], name);
  Object.defineProperty(f, "webkitRelativePath", { value: path });
  return f;
}

describe("filterSupported", () => {
  it("只保留 md/html/docx/txt，扩展名大小写不敏感", () => {
    const files = [
      fakeFile("a.md"),
      fakeFile("b.HTML"),
      fakeFile("c.docx"),
      fakeFile("d.txt"),
      fakeFile("e.png"),
      fakeFile("f.pdf"),
    ];
    expect(filterSupported(files).map((f) => f.name)).toEqual(["a.md", "b.HTML", "c.docx", "d.txt"]);
  });

  it("按文件夹内相对路径排序，无路径时退回文件名", () => {
    const files = [
      fakeFile("z.md", "docs/z.md"),
      fakeFile("a.md", "docs/a.md"),
      fakeFile("m.md"),
    ];
    expect(filterSupported(files).map((f) => f.webkitRelativePath || f.name))
      .toEqual(["docs/a.md", "docs/z.md", "m.md"]);
  });

  it("没有合法文件时返回空数组", () => {
    expect(filterSupported([fakeFile("image.jpg")])).toEqual([]);
  });

  it("允许的扩展名清单与页面提示一致", () => {
    expect(ALLOWED_EXT).toEqual([".md", ".html", ".docx", ".txt"]);
  });
});
