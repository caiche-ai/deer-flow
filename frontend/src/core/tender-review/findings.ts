export type TenderReviewSeverity = "info" | "minor" | "major" | "blocker";

export interface TenderReviewEvidence {
  document_name: string;
  path?: string;
  url?: string;
  page?: number;
  section?: string;
  clause?: string;
  quote: string;
  bbox?: [number, number, number, number];
}

export interface TenderReviewFinding {
  finding_id: string;
  dimension: string;
  severity: TenderReviewSeverity;
  title: string;
  issue: string;
  recommendation: string;
  issue_evidence: TenderReviewEvidence[];
  legal_basis: TenderReviewEvidence[];
}

export interface TenderReviewReport {
  version: 1;
  summary: string;
  overall_risk: "low" | "medium" | "high";
  source_document?: {
    filename: string;
    path: string;
  };
  findings: TenderReviewFinding[];
  limitations: string[];
}

type JsonRecord = Record<string, unknown>;

const REVIEW_BLOCK_RE = /```tender-review\s*\n([\s\S]*?)\n```/i;
const SEVERITIES = new Set<TenderReviewSeverity>([
  "info",
  "minor",
  "major",
  "blocker",
]);
const RISKS = new Set<TenderReviewReport["overall_risk"]>([
  "low",
  "medium",
  "high",
]);

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized || null;
}

function safeArtifactPath(value: unknown): string | undefined {
  const path = text(value);
  return path?.startsWith("/mnt/user-data/") ? path : undefined;
}

function safeSourceUrl(value: unknown): string | undefined {
  const url = text(value);
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : undefined;
  } catch {
    return undefined;
  }
}

function positivePage(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : undefined;
}

function bbox(value: unknown): [number, number, number, number] | undefined {
  if (
    !Array.isArray(value) ||
    value.length !== 4 ||
    value.some((coordinate) => typeof coordinate !== "number")
  ) {
    return undefined;
  }
  return value as [number, number, number, number];
}

function parseEvidence(
  value: unknown,
  sourceDocument?: TenderReviewReport["source_document"],
): TenderReviewEvidence | null {
  const item = record(value);
  if (!item) return null;
  const quote = text(item.quote);
  if (!quote) return null;
  const path = safeArtifactPath(item.path) ?? sourceDocument?.path;
  return {
    document_name:
      text(item.document_name) ?? sourceDocument?.filename ?? "审核依据",
    ...(path ? { path } : {}),
    ...(safeSourceUrl(item.url) ? { url: safeSourceUrl(item.url) } : {}),
    ...(positivePage(item.page) ? { page: positivePage(item.page) } : {}),
    ...(text(item.section) ? { section: text(item.section)! } : {}),
    ...(text(item.clause) ? { clause: text(item.clause)! } : {}),
    ...(bbox(item.bbox) ? { bbox: bbox(item.bbox) } : {}),
    quote,
  };
}

function parseEvidenceList(
  value: unknown,
  sourceDocument?: TenderReviewReport["source_document"],
): TenderReviewEvidence[] | null {
  if (!Array.isArray(value)) return null;
  const evidence = value.map((item) => parseEvidence(item, sourceDocument));
  return evidence.every((item): item is TenderReviewEvidence => item !== null)
    ? evidence
    : null;
}

function parseSourceDocument(
  value: unknown,
): TenderReviewReport["source_document"] | undefined {
  const source = record(value);
  if (!source) return undefined;
  const filename = text(source.filename);
  const path = safeArtifactPath(source.path);
  return filename && path ? { filename, path } : undefined;
}

function parseFinding(
  value: unknown,
  sourceDocument?: TenderReviewReport["source_document"],
): TenderReviewFinding | null {
  const item = record(value);
  if (!item) return null;
  const severity = text(item.severity) as TenderReviewSeverity | null;
  const issueEvidence = parseEvidenceList(item.issue_evidence, sourceDocument);
  const legalBasis = parseEvidenceList(item.legal_basis) ?? [];
  const required = {
    finding_id: text(item.finding_id),
    dimension: text(item.dimension),
    title: text(item.title),
    issue: text(item.issue),
    recommendation: text(item.recommendation),
  };
  if (
    !severity ||
    !SEVERITIES.has(severity) ||
    Object.values(required).some((field) => !field) ||
    !issueEvidence?.length
  ) {
    return null;
  }
  return {
    finding_id: required.finding_id!,
    dimension: required.dimension!,
    severity,
    title: required.title!,
    issue: required.issue!,
    recommendation: required.recommendation!,
    issue_evidence: issueEvidence,
    legal_basis: legalBasis,
  };
}

function parseReport(value: unknown): TenderReviewReport | null {
  const payload = record(value);
  if (payload?.version !== 1 || !Array.isArray(payload.findings)) {
    return null;
  }
  const sourceDocument = parseSourceDocument(payload.source_document);
  const findings = payload.findings.map((item) =>
    parseFinding(item, sourceDocument),
  );
  if (
    findings.some((finding) => finding === null) ||
    (payload.findings.length > 0 && findings.length === 0)
  ) {
    return null;
  }
  const risk = text(payload.overall_risk);
  const limitations = Array.isArray(payload.limitations)
    ? payload.limitations
        .map((item) => text(item))
        .filter((item): item is string => item !== null)
    : [];
  return {
    version: 1,
    summary: text(payload.summary) ?? "",
    overall_risk: RISKS.has(risk as TenderReviewReport["overall_risk"])
      ? (risk as TenderReviewReport["overall_risk"])
      : "medium",
    ...(sourceDocument ? { source_document: sourceDocument } : {}),
    findings: findings as TenderReviewFinding[],
    limitations,
  };
}

export function parseTenderReviewContent(content: string): {
  content: string;
  report: TenderReviewReport | null;
} {
  const match = REVIEW_BLOCK_RE.exec(content);
  if (!match?.[1]) return { content, report: null };
  try {
    const report = parseReport(JSON.parse(match[1]));
    if (!report) return { content, report: null };
    return {
      content: content.replace(match[0], "").trim(),
      report,
    };
  } catch {
    return { content, report: null };
  }
}
