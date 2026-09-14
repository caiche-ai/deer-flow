# 输出与证据契约

## 第一波子 Agent

只输出 JSON 对象：

```json
{
  "reviewer": "compliance-reviewer",
  "coverage": [
    {
      "dimension": "qualification",
      "status": "reviewed",
      "summary": "已核对资格条件及对应证明材料",
      "evidence_ids": ["document:page=12:chunk=1"]
    }
  ],
  "findings": [],
  "limitations": []
}
```

`coverage.status` 仅允许 `reviewed`、`issue_found`、`insufficient_evidence`。

## 正式发现

每项必须包含：

```json
{
  "finding_id": "COMP-001",
  "dimension": "schedule",
  "severity": "major",
  "title": "投标截止时间不一致",
  "issue": "公告与投标人须知给出的截止时间不同",
  "recommendation": "发布前统一截止时间并同步关联条款",
  "evidence_ids": [
    "document:page=3:chunk=1",
    "document:page=18:chunk=1"
  ]
}
```

## 第二波复核

第二波输出 `validated_findings`、`rejected_findings`、`cross_checks` 和 `limitations`。它必须重新回读候选证据，不能仅凭第一波描述放行。

## 严重性

- `blocker`：文件内部关键矛盾或缺失会阻止可靠发布、投标响应或评审执行。
- `major`：重大歧义、明显跨章节不一致或高影响风险。
- `minor`：局部表述、格式或低影响问题。
- `info`：不构成问题的人工复核提示。

疑似违法或限制竞争不能仅因措辞敏感就定为 blocker。没有适用法规原文、版本和适用性证据时，只标记人工法律复核。

## 证据要求

- `evidence_id` 必须来自当前文档，且在输出前经 `get_tender_evidence` 回读。
- 矛盾类发现至少引用冲突两侧证据。
- “缺失”类发现必须说明检查过的目录、替代关键词或相关章节，并优先使用 `insufficient_evidence`。
- 摘录不得脱离上下文改变原意；表格内容需核对表头、单位和对应行列。

## 聊天前端结构化结果

Lead 在原生聊天中给出最终审核答复时，必须在可读结论之后追加且只追加一个 `tender-review` 代码块。该代码块供前端生成可点击的审核卡片，不得用省略号、Markdown 或注释破坏 JSON：

````markdown
```tender-review
{
  "version": 1,
  "summary": "发现一项重大风险",
  "overall_risk": "high",
  "source_document": {
    "filename": "招标文件正文.pdf",
    "path": "/mnt/user-data/uploads/招标文件正文.pdf"
  },
  "findings": [
    {
      "finding_id": "QUAL-001",
      "dimension": "qualification",
      "severity": "major",
      "title": "资格条件存在地域限制",
      "issue": "资格要求将注册地作为准入条件",
      "recommendation": "删除注册地限制，改为与履约能力直接相关的条件",
      "issue_evidence": [
        {
          "document_name": "招标文件正文.pdf",
          "page": 37,
          "section": "投标人资格要求",
          "quote": "投标人须在深圳市注册"
        }
      ],
      "legal_basis": [
        {
          "document_name": "深圳经济特区政府采购条例",
          "page": 8,
          "clause": "第二十一条",
          "quote": "核验工具回读的法规原文"
        }
      ]
    }
  ],
  "limitations": []
}
```
````

约束：

- `source_document.path` 必须是上传消息中 `Original PDF` 对应的真实 `/mnt/user-data/uploads/...` 路径；不得填写解析 Markdown 路径。
- `issue_evidence` 至少一项，每项必须包含原 PDF 的一个物理页码和逐字原文 `quote`；章节、条款和 bbox 有真实数据时一并提供。
- `legal_basis` 必须来自已回读依据，包含依据文件名、条款号、页码和逐字原文；只有工具返回真实 HTTP(S) 来源时才填写 `url`，不得猜测链接。
- 文件内部一致性、缺失或纯编制质量问题如果没有外部法规依据，`legal_basis` 可以为空数组，但 `issue_evidence` 仍不得为空。
- 无问题时仍输出合法结构，`findings` 使用空数组；报告 Markdown/JSON 文件中的结构应与该代码块一致。
