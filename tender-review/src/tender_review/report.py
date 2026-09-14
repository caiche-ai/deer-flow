"""Validated and auditable report persistence for tender-document review."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from tender_review.document import MineruDocument


class AuditDimension(StrEnum):
    BASIC_INFORMATION = "basic_information"
    SCHEDULE = "schedule"
    QUALIFICATION = "qualification"
    REJECTION_CLAUSES = "rejection_clauses"
    EVALUATION_METHOD = "evaluation_method"
    TECHNICAL = "technical"
    COMMERCIAL_PRICE = "commercial_price"
    CONTRACT = "contract"


class AuditSeverity(StrEnum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    BLOCKER = "blocker"


class CoverageStatus(StrEnum):
    REVIEWED = "reviewed"
    ISSUE_FOUND = "issue_found"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class AuditCoverage:
    dimension: AuditDimension
    status: CoverageStatus
    summary: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TenderAuditFinding:
    finding_id: str
    dimension: AuditDimension
    severity: AuditSeverity
    title: str
    issue: str
    recommendation: str
    evidence_ids: tuple[str, ...]
    basis_ids: tuple[str, ...] = ()
    issue_evidence: tuple[dict[str, Any], ...] = ()
    legal_basis: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class TenderAuditReport:
    version: int
    document_id: str
    source_content_list: str
    source_sha256: str
    generated_at: str
    overall_risk: str
    summary: str
    coverage: tuple[AuditCoverage, ...]
    findings: tuple[TenderAuditFinding, ...]
    limitations: tuple[str, ...]


class TenderReportStore:
    """Validate evidence and write JSON plus Markdown reports."""

    def __init__(self, document: MineruDocument, output_dir: str | Path) -> None:
        self.document = document
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.last_saved_paths: tuple[Path, Path] | None = None
        self.interaction_store: Any | None = None
        self._verified_evidence_ids: set[str] = set()
        self._verified_basis_ids: set[str] = set()
        self._verified_basis_evidence: dict[str, dict[str, Any]] = {}
        self._evidence_lock = threading.Lock()

    def record_verified_evidence(self, evidence_ids: list[str]) -> None:
        """Record evidence returned by the exact-evidence read tool in this run."""

        unknown = [
            evidence_id
            for evidence_id in evidence_ids
            if evidence_id not in self.document.evidence_ids
        ]
        if unknown:
            raise ValueError(f"unknown evidence ids: {', '.join(unknown)}")
        with self._evidence_lock:
            self._verified_evidence_ids.update(evidence_ids)

    @property
    def verified_basis_ids(self) -> frozenset[str]:
        with self._evidence_lock:
            return frozenset(self._verified_basis_ids)

    def record_verified_basis(
        self,
        basis_ids: list[str],
        *,
        evidence: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record public-knowledge evidence returned by the exact-basis read tool."""

        if not basis_ids or any(not str(basis_id).strip() for basis_id in basis_ids):
            raise ValueError("basis_ids must not be empty")
        evidence_by_id = {
            str(item.get("basis_id", "")).strip(): dict(item)
            for item in (evidence or [])
            if isinstance(item, dict) and str(item.get("basis_id", "")).strip()
        }
        unexpected = sorted(set(evidence_by_id) - {str(item).strip() for item in basis_ids})
        if unexpected:
            raise ValueError(
                f"basis evidence does not match requested ids: {', '.join(unexpected)}"
            )
        with self._evidence_lock:
            self._verified_basis_ids.update(str(basis_id).strip() for basis_id in basis_ids)
            self._verified_basis_evidence.update(evidence_by_id)

    def save(
        self,
        *,
        summary: str,
        coverage: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        limitations: list[str] | None = None,
    ) -> dict[str, Any]:
        if not summary.strip():
            raise ValueError("summary must not be empty")
        parsed_coverage = tuple(self._parse_coverage(item) for item in coverage)
        dimensions = [item.dimension for item in parsed_coverage]
        required = set(AuditDimension)
        if set(dimensions) != required or len(dimensions) != len(required):
            missing = sorted(dimension.value for dimension in required - set(dimensions))
            duplicates = sorted(
                dimension.value for dimension in set(dimensions) if dimensions.count(dimension) > 1
            )
            raise ValueError(
                "coverage must contain every audit dimension exactly once; "
                f"missing={missing}, duplicates={duplicates}"
            )

        parsed_findings = tuple(self._parse_finding(item) for item in findings)
        finding_ids = [finding.finding_id for finding in parsed_findings]
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("finding_id must be unique")

        limitations_tuple = tuple(text.strip() for text in (limitations or []) if str(text).strip())
        source_bytes = self.document.source_path.read_bytes()
        report = TenderAuditReport(
            version=1,
            document_id=self.document.document_id,
            source_content_list=str(self.document.source_path),
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            generated_at=datetime.now(UTC).isoformat(),
            overall_risk=self._overall_risk(parsed_findings, parsed_coverage),
            summary=summary.strip(),
            coverage=parsed_coverage,
            findings=parsed_findings,
            limitations=limitations_tuple,
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.-]+", "_", self.document.document_id).strip("_.")
        json_path = self.output_dir / f"{safe_name}_tender_review.json"
        markdown_path = self.output_dir / f"{safe_name}_tender_review.md"
        report_payload = asdict(report)
        json_path.write_text(
            json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        markdown_path.write_text(self._to_markdown(report), encoding="utf-8")
        self.last_saved_paths = (json_path, markdown_path)
        return {
            "status": "saved",
            "overall_risk": report.overall_risk,
            "finding_count": len(report.findings),
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "report": report_payload,
        }

    def _parse_coverage(self, item: dict[str, Any]) -> AuditCoverage:
        if not isinstance(item, dict):
            raise TypeError("each coverage item must be an object")
        evidence_ids = self._validate_evidence_ids(item.get("evidence_ids"))
        summary = str(item.get("summary", "")).strip()
        if not summary:
            raise ValueError("coverage summary must not be empty")
        try:
            dimension = AuditDimension(str(item.get("dimension", "")))
            status = CoverageStatus(str(item.get("status", "")))
        except ValueError as exc:
            raise ValueError(f"invalid coverage enum: {exc}") from exc
        return AuditCoverage(dimension, status, summary, evidence_ids)

    def _parse_finding(self, item: dict[str, Any]) -> TenderAuditFinding:
        if not isinstance(item, dict):
            raise TypeError("each finding must be an object")
        required_text = {
            field: str(item.get(field, "")).strip()
            for field in ("finding_id", "title", "issue", "recommendation")
        }
        empty = [field for field, value in required_text.items() if not value]
        if empty:
            raise ValueError(f"finding fields must not be empty: {', '.join(empty)}")
        try:
            dimension = AuditDimension(str(item.get("dimension", "")))
            severity = AuditSeverity(str(item.get("severity", "")))
        except ValueError as exc:
            raise ValueError(f"invalid finding enum: {exc}") from exc
        evidence_ids = self._validate_evidence_ids(item.get("evidence_ids"))
        basis_ids = self._validate_basis_ids(item.get("basis_ids"))
        return TenderAuditFinding(
            finding_id=required_text["finding_id"],
            dimension=dimension,
            severity=severity,
            title=required_text["title"],
            issue=required_text["issue"],
            recommendation=required_text["recommendation"],
            evidence_ids=evidence_ids,
            basis_ids=basis_ids,
            issue_evidence=self._expand_issue_evidence(evidence_ids),
            legal_basis=self._expand_legal_basis(basis_ids),
        )

    def _expand_issue_evidence(self, evidence_ids: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "evidence_id": chunk.evidence_id,
                "document_name": self.document.document_id,
                "page": chunk.page,
                "quote": chunk.text,
                "source_blocks": [chunk.block_start, chunk.block_end],
                "bboxes": list(chunk.bboxes),
            }
            for chunk in self.document.get_evidence(list(evidence_ids))
        )

    def _expand_legal_basis(self, basis_ids: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        if not basis_ids:
            return ()
        with self._evidence_lock:
            missing = [
                basis_id for basis_id in basis_ids if basis_id not in self._verified_basis_evidence
            ]
            if missing:
                raise ValueError(
                    "knowledge basis details must be retained when evidence is re-read: "
                    + ", ".join(missing)
                )
            payloads = [dict(self._verified_basis_evidence[basis_id]) for basis_id in basis_ids]
        return tuple(
            {
                "basis_id": basis_id,
                "document_name": str(payload.get("document_title", "")).strip() or basis_id,
                "page": payload.get("page_start"),
                "page_end": payload.get("page_end"),
                "section": payload.get("section_title"),
                "clause": payload.get("clause_no"),
                "quote": str(payload.get("content", "")).strip(),
                "bbox": payload.get("bbox_json") or {},
            }
            for basis_id, payload in zip(basis_ids, payloads, strict=True)
        )

    def _validate_basis_ids(self, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise TypeError("basis_ids must be a list")
        basis_ids = tuple(str(basis_id).strip() for basis_id in value)
        if any(not basis_id for basis_id in basis_ids):
            raise ValueError("basis_ids must not contain empty values")
        with self._evidence_lock:
            unverified = [
                basis_id for basis_id in basis_ids if basis_id not in self._verified_basis_ids
            ]
        if unverified:
            raise ValueError(
                "knowledge basis must be re-read with get_tender_knowledge_evidence before saving: "
                + ", ".join(unverified)
            )
        return basis_ids

    def _validate_evidence_ids(self, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list) or not value:
            raise ValueError("evidence_ids must be a non-empty list")
        evidence_ids = tuple(str(evidence_id).strip() for evidence_id in value)
        if any(not evidence_id for evidence_id in evidence_ids):
            raise ValueError("evidence_ids must not contain empty values")
        unknown = [
            evidence_id
            for evidence_id in evidence_ids
            if evidence_id not in self.document.evidence_ids
        ]
        if unknown:
            raise ValueError(f"unknown evidence ids: {', '.join(unknown)}")
        with self._evidence_lock:
            unverified = [
                evidence_id
                for evidence_id in evidence_ids
                if evidence_id not in self._verified_evidence_ids
            ]
        if unverified:
            raise ValueError(
                "evidence must be re-read with get_tender_evidence before saving: "
                + ", ".join(unverified)
            )
        return evidence_ids

    @staticmethod
    def _overall_risk(
        findings: tuple[TenderAuditFinding, ...], coverage: tuple[AuditCoverage, ...]
    ) -> str:
        severities = {finding.severity for finding in findings}
        if AuditSeverity.BLOCKER in severities or AuditSeverity.MAJOR in severities:
            return "high"
        if AuditSeverity.MINOR in severities or any(
            item.status is CoverageStatus.INSUFFICIENT_EVIDENCE for item in coverage
        ):
            return "medium"
        return "low"

    def _to_markdown(self, report: TenderAuditReport) -> str:
        risk_label = {"high": "高", "medium": "中", "low": "低"}[report.overall_risk]
        lines = [
            f"# 招标文件审核报告：{report.document_id}",
            "",
            f"- 综合风险：**{risk_label}**",
            f"- 生成时间：{report.generated_at}",
            f"- 解析产物 SHA-256：`{report.source_sha256}`",
            "- 声明：本报告仅供辅助审核，疑似违法违规、否决条款及重大风险必须人工复核。",
            "",
            "## 审核摘要",
            "",
            report.summary,
            "",
            "## 维度覆盖",
            "",
            "| 维度 | 状态 | 说明 | 证据 |",
            "|---|---|---|---|",
        ]
        for item in report.coverage:
            lines.append(
                f"| {item.dimension.value} | {item.status.value} | "
                f"{item.summary.replace('|', '｜')} | {', '.join(item.evidence_ids)} |"
            )

        lines.extend(["", "## 风险发现", ""])
        if not report.findings:
            lines.append("未发现需单列的问题；该结论仍受下述审核局限约束。")
        for finding in report.findings:
            lines.extend(
                [
                    f"### {finding.finding_id} · {finding.title}",
                    "",
                    f"- 维度：{finding.dimension.value}",
                    f"- 严重性：{finding.severity.value}",
                    f"- 问题：{finding.issue}",
                    f"- 建议：{finding.recommendation}",
                    f"- 证据：{', '.join(finding.evidence_ids)}",
                ]
            )
            for evidence in finding.issue_evidence:
                lines.extend(
                    [
                        f"- 问题位置：{evidence['document_name']} · 第 {evidence['page']} 页",
                        *(
                            f"  > {line}"
                            for line in str(evidence["quote"]).splitlines()
                            if line.strip()
                        ),
                    ]
                )
            if finding.basis_ids:
                lines.append(f"- 知识库依据：{', '.join(finding.basis_ids)}")
            for basis in finding.legal_basis:
                location = " · ".join(
                    str(value)
                    for value in (
                        basis["document_name"],
                        f"第 {basis['page']} 页" if basis.get("page") else None,
                        basis.get("section"),
                        basis.get("clause"),
                    )
                    if value
                )
                lines.extend(
                    [
                        f"- 依据原文：{location}",
                        *(
                            f"  > {line}"
                            for line in str(basis["quote"]).splitlines()
                            if line.strip()
                        ),
                    ]
                )
            lines.append("")

        lines.extend(["## 审核局限", ""])
        if report.limitations:
            lines.extend(f"- {limitation}" for limitation in report.limitations)
        else:
            lines.append("- 未声明额外局限。")
        lines.append("")
        return "\n".join(lines)
