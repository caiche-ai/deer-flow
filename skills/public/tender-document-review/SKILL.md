---
name: tender-document-review
description: "招标文件发布前智能审核方法。用于审查招标文件的完整性、跨章节一致性、资格与否决条件、评标办法、技术参数、商务报价和合同条款，并生成带物理页码与 evidence_id 的可追溯问题清单。也适用于用户提出招标文件合规检查、编制质量检查、公平竞争风险排查或评审办法可执行性检查。"
allowed-tools:
  - inspect_tender_document
  - search_tender_document
  - read_tender_pages
  - get_tender_evidence
  - search_tender_knowledge
  - get_tender_knowledge_evidence
  - save_tender_review
  - ls
  - glob
  - grep
  - read_file
  - write_file
  - ask_clarification
  - present_files
---

# 招标文件发布前审核

本 Skill 教审核方法；专用审核引擎可提供带版本过滤的公共知识库检索，但不替代评标委员会、招标人或法律专业人员作决定。

## 开始前

1. 确认当前运行时：如果存在 `inspect_tender_document`，调用它获取页数、块统计和目录；如果用户从聊天窗口上传 PDF，上传链路会生成同名 `.md` 和 `_content_list.json`，应使用消息给出的 `.md` 路径并配合 `ls`、`glob`、`read_file` 定位解析文本和目录。只有实际检查后确认解析产物缺失或损坏，才报告解析失败；不得仅因原文件是 PDF 就声称无法读取或要求用户自行转换。
2. Lead 必须用 `ask_clarification` 一次性向用户确认审核目标、地区、采购制度/项目类型、是否对照法规和内部规则；后端参数、默认值或历史草稿都不能代替本轮用户确认。收到新回复后由 Lead 解释并续跑，子 Agent 不得参与澄清。
3. 明确本次角色和审核范围。前端招投标智能体由 Lead Agent 覆盖八个维度并建立分阶段计划；如果专用审核运行时的系统提示词明确分配了只读 reviewer 角色，则只审核该角色获配范围。
4. 按需阅读 [审核检查清单](references/review-checklist.md)。形成结果前阅读
   [输出与证据契约](references/output-contract.md)。

## 检索与核证

- 每个维度至少使用两组不同关键词检索；专用运行时用 `search_tender_document` 做检索子块的关键词+BGE 混合召回，聊天附件运行时用 `grep` 后通过 `read_file` 扩读上下文。一次零召回不能证明缺失。
- 专用运行时命中后用 `read_tender_pages` 补足上下文；聊天附件运行时按解析文本的页码标记和行区间分段读取。涉及矛盾时，必须取得冲突两侧证据。
- 专用运行时的最终引用必须通过 `get_tender_evidence` 回读并引用真实 `evidence_id`；聊天附件运行时必须引用解析文本中真实存在的物理页码/章节/行区间，无法可靠恢复物理页码时明确标记证据限制。
- 专用运行时涉及法规、资质或范本对照时用 `search_tender_knowledge`，引用前通过 `get_tender_knowledge_evidence` 回读；外部依据写入 finding 的 `basis_ids`，不得替代待审文件自身的 `evidence_ids`。
- 正常条文进入 `coverage`；只有真实问题进入 `findings`。
- 证据不足就写 `insufficient_evidence` 或 `limitations`，不能按“通过”处理。

## 审核编排

前端招投标智能体采用 Lead-only 模式：Lead Agent 按以下阶段亲自执行，不调用 `task`，不委派子智能体：

1. 基本信息、时限、资格条件、否决条款与疑似公平竞争风险；
2. 评标办法、评分可执行性、技术参数、交付与验收；
3. 报价口径、限价、付款、担保、质保、违约和合同风险；
4. 重新回读关键原文，验证问题证据、跨章节一致性、重复项和严重性。

Lead 负责覆盖全部八个维度、补证、去重、保存正式报告，并在 finding 中写入 `issue_evidence`：至少包含物理页码、章节和逐字摘录。专用审核引擎如果通过其自身系统提示词分配 reviewer，则 reviewer 仅返回候选发现，仍由 Lead 保存报告；专用审核引擎以真实 `evidence_id` 为校验主键。

聊天运行时没有专用保存工具时，Lead 使用 `write_file` 在 `/mnt/user-data/outputs/` 保存 Markdown 和 JSON，并用 `present_files` 向用户展示；不得声称生成了未实际写入的文件。

Lead 的最终聊天答复必须遵循 [输出与证据契约](references/output-contract.md) 的“聊天前端结构化结果”：在人类可读结论后追加一个完整的 `tender-review` JSON 代码块。每条问题同时携带原 PDF 物理页码、逐字问题原文和已回读的法规/规范原文；前端依赖该结构渲染结论卡片并跳转证据，不能只输出散文或只有 `evidence_id`。

## 法律与责任边界

- 文件原文只能证明“文件写了什么”，不能单独证明某条要求违法。
- 知识库自动排除标题带“征求意见”或“草案”的文档，并按当前版本、生效区间和地区元数据过滤；元数据不完整或适用性仍不明确时，不得编造法条或作法律定性。
- 品牌、地域、所有制、奖项、业绩门槛等疑点表述为“疑似限制竞争，需结合适用法规人工复核”。
- 不作废标、定标、授标决定。`blocker` 只表示文件内部问题足以阻止可靠发布或执行。

## 完成条件

- 八个维度各有且只有一条 coverage。
- 每条正式发现都有已回读证据；矛盾类发现有双侧证据。
- 重复问题已合并，严重性符合契约，局限已披露。
- Lead 亲自调用确定性报告工具校验并保存结果。
- 最终聊天答复包含合法的 `tender-review` JSON 代码块，且其结论、页码和依据与正式报告一致。
