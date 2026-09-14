import { describe, expect, it } from "vitest";

import { getDocumentPreview, withPdfPage } from "@/core/uploads/preview";

describe("getDocumentPreview", () => {
  it("previews PDFs from the original uploaded path", () => {
    expect(
      getDocumentPreview({
        filename: "招标文件.pdf",
        path: "/mnt/user-data/uploads/招标文件.pdf",
        previewPath: "/mnt/user-data/uploads/招标文件.md",
      }),
    ).toEqual({
      kind: "pdf",
      path: "/mnt/user-data/uploads/招标文件.pdf",
    });
  });

  it("previews Office documents through their converted Markdown", () => {
    expect(
      getDocumentPreview({
        filename: "资格条件.docx",
        path: "/mnt/user-data/uploads/资格条件.docx",
        previewPath: "/mnt/user-data/uploads/资格条件.md",
      }),
    ).toEqual({
      kind: "text",
      path: "/mnt/user-data/uploads/资格条件.md",
    });
  });

  it("marks Office documents without a converted preview as unsupported", () => {
    expect(
      getDocumentPreview({
        filename: "评分表.xlsx",
        path: "/mnt/user-data/uploads/评分表.xlsx",
      }),
    ).toEqual({ kind: "unsupported", path: null });
  });

  it.each([
    ["现场照片.png", "image"],
    ["说明.txt", "text"],
    ["数据.json", "text"],
  ] as const)("classifies %s as %s", (filename, kind) => {
    const path = `/mnt/user-data/uploads/${filename}`;
    expect(getDocumentPreview({ filename, path })).toEqual({ kind, path });
  });
});

describe("withPdfPage", () => {
  it("adds a physical PDF page fragment and replaces stale fragments", () => {
    expect(withPdfPage("/api/file.pdf?token=1#page=2", 37)).toBe(
      "/api/file.pdf?token=1#page=37",
    );
  });

  it("ignores invalid page numbers", () => {
    expect(withPdfPage("/api/file.pdf", 0)).toBe("/api/file.pdf");
  });
});
