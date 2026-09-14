# 范本完整性检查输出契约

## Markdown 报告

按以下顺序输出：

1. 对比结论：是否发现重大差异，以及最高严重性；
2. 基线说明：待审文件、范本名称、版本、来源、适用范围及匹配置信度；
3. 重大差异：按 `blocker`、`major` 排序；
4. 功能覆盖矩阵：所有适用功能单元及其映射状态；
5. 非重大差异摘要：只统计或列附录，不混入重大问题；
6. 局限与人工复核事项。

每项重大差异包含：差异编号、功能单元、状态、严重性、范本要求、待审文件现状、具体影响、修订建议、证据和置信度。

## JSON 报告

正式 JSON 使用以下结构；不得使用省略号、注释或虚构证据：

```json
{
  "version": 1,
  "status": "completed",
  "summary": "发现 2 项重大范本差异",
  "baseline": {
    "tender_document": "待审文件名称",
    "template_document": "范本名称",
    "template_version": "版本或发布日期",
    "template_source": "用户上传或知识库",
    "applicability": "confirmed",
    "confidence": "high"
  },
  "counts": {
    "applicable_units": 12,
    "matched": 9,
    "materially_modified": 1,
    "missing": 1,
    "unlocatable": 1
  },
  "major_differences": [
    {
      "difference_id": "TC-001",
      "functional_unit": "评标办法/评分计算",
      "status": "missing",
      "severity": "blocker",
      "title": "评分计算规则缺失",
      "template_requirement": "范本要求说明评分计算与并列处理",
      "tender_state": "待审文件仅列评分项，未给出计算和并列处理规则",
      "impact": "评审人员无法按统一口径计算得分并处理并列结果",
      "recommendation": "补充计算公式、精度、四舍五入和并列处理规则",
      "template_evidence": [
        {
          "document_name": "范本.pdf",
          "page": 42,
          "section": "评标办法",
          "quote": "范本逐字原文"
        }
      ],
      "tender_evidence": [
        {
          "document_name": "待审招标文件.pdf",
          "page": 38,
          "section": "评分标准",
          "quote": "待审文件逐字原文"
        }
      ],
      "search_record": [
        "检查目录中的评标办法与评分标准",
        "检索：评分公式、得分计算、四舍五入、并列"
      ],
      "confidence": "high",
      "manual_review": false
    }
  ],
  "coverage": [
    {
      "functional_unit": "评标办法/评分计算",
      "applicability": "applicable",
      "status": "missing",
      "mapped_section": "评分标准",
      "notes": "仅有评分项"
    }
  ],
  "limitations": []
}
```

如果范本不可用：

- `status` 必须为 `baseline_missing`；
- `baseline.applicability` 为 `unconfirmed`；
- `major_differences` 必须为空数组；
- 在 `limitations` 中列出缺失的范本名称、版本、地区或采购制度信息；
- 可以输出待审文件结构预检，但不能给出“与范本一致”的结论。

## 与前端 tender-review 结构衔接

招投标聊天页要求最终答复追加 `tender-review` JSON 代码块时，将确认后的 `blocker`、`major` 差异映射为 findings：

- `dimension` 按功能单元映射到八维审核维度；
- `issue_evidence` 使用待审文件证据；
- 范本证据写入问题说明或报告附件，不伪装成法规 `legal_basis`；
- 待审文件完全缺失对应内容时，`issue_evidence` 使用最接近章节、目录或断裂引用的真实证据，同时在 issue 中说明搜索范围；
- 无法提供真实待审文件证据时不生成前端 finding，改写入 limitations。
