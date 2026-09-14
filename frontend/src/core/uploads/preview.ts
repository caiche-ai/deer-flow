export type DocumentPreviewKind = "pdf" | "image" | "text" | "unsupported";

export interface DocumentPreview {
  kind: DocumentPreviewKind;
  path: string | null;
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp"]);
const TEXT_EXTENSIONS = new Set([
  "txt",
  "md",
  "markdown",
  "csv",
  "json",
  "yaml",
  "yml",
  "xml",
  "log",
  "py",
  "js",
  "ts",
  "tsx",
  "css",
  "html",
]);
const OFFICE_EXTENSIONS = new Set([
  "doc",
  "docx",
  "xls",
  "xlsx",
  "ppt",
  "pptx",
]);

function extensionOf(filename: string): string {
  return filename.split(".").pop()?.toLowerCase() ?? "";
}

export function getDocumentPreview({
  filename,
  path,
  previewPath,
}: {
  filename: string;
  path?: string;
  previewPath?: string;
}): DocumentPreview {
  const extension = extensionOf(filename);

  if (extension === "pdf" && path) {
    return { kind: "pdf", path };
  }
  if (IMAGE_EXTENSIONS.has(extension) && path) {
    return { kind: "image", path };
  }
  if (TEXT_EXTENSIONS.has(extension) && path) {
    return { kind: "text", path };
  }
  if (OFFICE_EXTENSIONS.has(extension) && previewPath) {
    return { kind: "text", path: previewPath };
  }
  return { kind: "unsupported", path: null };
}

export function withPdfPage(url: string, page?: number): string {
  if (!page || !Number.isInteger(page) || page < 1) return url;
  return `${url.split("#", 1)[0]}#page=${page}`;
}
