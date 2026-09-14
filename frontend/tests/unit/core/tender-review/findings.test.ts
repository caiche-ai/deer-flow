import { describe, expect, it } from "vitest";

import { parseTenderReviewContent } from "@/core/tender-review/findings";

describe("parseTenderReviewContent", () => {
  it("extracts a structured review block and inherits the source document", () => {
    const content = `审核完成。

\`\`\`tender-review
{
  "version": 1,
  "summary": "发现一项高风险问题",
  "overall_risk": "high",
  "source_document": {
    "filename": "招标文件正文.pdf",
    "path": "/mnt/user-data/uploads/招标文件正文.pdf"
  },
  "findings": [{
    "finding_id": "QUAL-001",
    "dimension": "qualification",
    "severity": "major",
    "title": "资格条件存在地域限制",
    "issue": "要求投标人在深圳注册",
    "recommendation": "删除注册地限制",
    "issue_evidence": [{
      "page": 37,
      "section": "投标人资格要求",
      "quote": "投标人须在深圳市注册"
    }],
    "legal_basis": [{
      "document_name": "深圳经济特区政府采购条例",
      "clause": "第二十一条",
      "quote": "不得以不合理条件限制供应商"
    }]
  }]
}
\`\`\``;

    const parsed = parseTenderReviewContent(content);

    expect(parsed.content).toBe("审核完成。");
    expect(parsed.report?.findings).toHaveLength(1);
    expect(parsed.report?.findings[0]?.issue_evidence[0]).toMatchObject({
      document_name: "招标文件正文.pdf",
      path: "/mnt/user-data/uploads/招标文件正文.pdf",
      page: 37,
    });
  });

  it("leaves malformed or unrelated blocks visible", () => {
    const content = "```tender-review\n{not json}\n```";
    expect(parseTenderReviewContent(content)).toEqual({
      content,
      report: null,
    });
  });

  it("rejects findings without exact issue evidence", () => {
    const content = `\`\`\`tender-review
{"version":1,"findings":[{"finding_id":"F-1","title":"问题"}]}
\`\`\``;
    expect(parseTenderReviewContent(content).report).toBeNull();
  });
});
