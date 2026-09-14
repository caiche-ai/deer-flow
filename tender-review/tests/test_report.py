import json

import pytest

from tender_review.document import MineruDocument
from tender_review.report import AuditDimension, TenderReportStore


def _document(tmp_path):
    path = tmp_path / "sample_content_list.json"
    path.write_text(
        json.dumps(
            [
                {"type": "text", "text": f"第{page + 1}页审核证据", "page_idx": page}
                for page in range(8)
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return MineruDocument.load(path, document_id="sample", chunk_chars=300)


def _coverage(document):
    return [
        {
            "dimension": dimension.value,
            "status": "reviewed",
            "summary": f"已审核{dimension.value}",
            "evidence_ids": [f"sample:page={index}:chunk=1"],
        }
        for index, dimension in enumerate(AuditDimension, start=1)
    ]


def _verify_all(store, document):
    store.record_verified_evidence(sorted(document.evidence_ids))


def test_save_validated_json_and_markdown_report(tmp_path):
    document = _document(tmp_path)
    store = TenderReportStore(document, tmp_path / "reports")
    _verify_all(store, document)

    result = store.save(
        summary="发现一项日期不一致问题。",
        coverage=_coverage(document),
        findings=[
            {
                "finding_id": "F-001",
                "dimension": "schedule",
                "severity": "major",
                "title": "日期不一致",
                "issue": "公告和须知日期不同。",
                "recommendation": "发布前统一日期。",
                "evidence_ids": ["sample:page=2:chunk=1"],
            }
        ],
        limitations=["尚未接入法规库。"],
    )

    payload = json.loads(
        (tmp_path / "reports" / "sample_tender_review.json").read_text(encoding="utf-8")
    )
    markdown = (tmp_path / "reports" / "sample_tender_review.md").read_text(encoding="utf-8")
    assert result["overall_risk"] == "high"
    assert payload["version"] == 1
    assert payload["source_sha256"]
    assert payload["findings"][0]["finding_id"] == "F-001"
    assert payload["findings"][0]["issue_evidence"][0] == {
        "evidence_id": "sample:page=2:chunk=1",
        "document_name": "sample",
        "page": 2,
        "quote": "第2页审核证据",
        "source_blocks": [2, 2],
        "bboxes": [],
    }
    assert "问题位置：sample · 第 2 页" in markdown
    assert "> 第2页审核证据" in markdown
    assert "sample:page=2:chunk=1" in markdown


def test_report_requires_complete_dimension_coverage(tmp_path):
    document = _document(tmp_path)
    store = TenderReportStore(document, tmp_path / "reports")
    _verify_all(store, document)

    with pytest.raises(ValueError, match="every audit dimension"):
        store.save(summary="不完整", coverage=_coverage(document)[:-1], findings=[])


def test_report_rejects_invented_evidence(tmp_path):
    document = _document(tmp_path)
    store = TenderReportStore(document, tmp_path / "reports")
    _verify_all(store, document)
    coverage = _coverage(document)
    coverage[0]["evidence_ids"] = ["sample:page=999:chunk=1"]

    with pytest.raises(ValueError, match="unknown evidence ids"):
        store.save(summary="错误证据", coverage=coverage, findings=[])


def test_report_rejects_evidence_not_reread_by_agent(tmp_path):
    document = _document(tmp_path)
    store = TenderReportStore(document, tmp_path / "reports")

    with pytest.raises(ValueError, match="get_tender_evidence"):
        store.save(summary="未回读证据", coverage=_coverage(document), findings=[])


def test_report_requires_knowledge_basis_to_be_reread(tmp_path):
    document = _document(tmp_path)
    store = TenderReportStore(document, tmp_path / "reports")
    _verify_all(store, document)
    finding = {
        "finding_id": "F-LEGAL-001",
        "dimension": "qualification",
        "severity": "major",
        "title": "资格条件需复核",
        "issue": "资格门槛可能超出项目需要。",
        "recommendation": "结合适用法规人工复核。",
        "evidence_ids": ["sample:page=3:chunk=1"],
        "basis_ids": ["kb:tender_regulations:chunk-1"],
    }

    with pytest.raises(ValueError, match="get_tender_knowledge_evidence"):
        store.save(summary="存在待复核问题", coverage=_coverage(document), findings=[finding])

    store.record_verified_basis(
        ["kb:tender_regulations:chunk-1"],
        evidence=[
            {
                "basis_id": "kb:tender_regulations:chunk-1",
                "document_title": "政府采购条例",
                "page_start": 8,
                "page_end": 8,
                "section_title": "供应商条件",
                "clause_no": "第二十一条",
                "content": "不得以不合理条件限制供应商。",
                "bbox_json": {"blocks": []},
            }
        ],
    )
    store.save(summary="存在待复核问题", coverage=_coverage(document), findings=[finding])
    payload = json.loads(
        (tmp_path / "reports" / "sample_tender_review.json").read_text(encoding="utf-8")
    )
    markdown = (tmp_path / "reports" / "sample_tender_review.md").read_text(encoding="utf-8")

    assert payload["findings"][0]["basis_ids"] == ["kb:tender_regulations:chunk-1"]
    assert payload["findings"][0]["legal_basis"][0]["clause"] == "第二十一条"
    assert payload["findings"][0]["legal_basis"][0]["quote"] == "不得以不合理条件限制供应商。"
    assert "kb:tender_regulations:chunk-1" in markdown
