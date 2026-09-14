"use client";

import {
  AlertTriangleIcon,
  FileSearchIcon,
  ScrollTextIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { useI18n } from "@/core/i18n/hooks";
import type {
  TenderReviewEvidence,
  TenderReviewReport,
  TenderReviewSeverity,
} from "@/core/tender-review/findings";
import { cn } from "@/lib/utils";

import { useArtifacts } from "../artifacts/context";

const SEVERITY_CLASS: Record<TenderReviewSeverity, string> = {
  blocker: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300",
  major:
    "border-orange-500/40 bg-orange-500/10 text-orange-700 dark:text-orange-300",
  minor:
    "border-yellow-500/40 bg-yellow-500/10 text-yellow-700 dark:text-yellow-300",
  info: "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-300",
};

function filenameFromPath(path: string | undefined, fallback: string): string {
  return path?.split("/").pop() ?? fallback;
}

export function TenderReviewFindings({
  report,
}: {
  report: TenderReviewReport;
}) {
  const { t } = useI18n();
  const { previewDocument } = useArtifacts();

  const openEvidence = (evidence: TenderReviewEvidence) => {
    previewDocument({
      filename: filenameFromPath(evidence.path, evidence.document_name),
      path: evidence.path,
      url: evidence.url,
      page: evidence.page,
      quote: evidence.quote,
      section: evidence.section,
      clause: evidence.clause,
    });
  };

  return (
    <section className="mt-5 space-y-3" aria-label={t.tenderReview.title}>
      <header className="flex items-center gap-2">
        <AlertTriangleIcon className="text-primary size-4" />
        <h2 className="text-sm font-semibold">{t.tenderReview.title}</h2>
        <Badge variant="secondary">
          {t.tenderReview.findingCount(report.findings.length)}
        </Badge>
      </header>

      {report.findings.map((finding) => (
        <Card key={finding.finding_id} className="gap-0 overflow-hidden py-0">
          <CardHeader className="flex-row items-start gap-3 border-b px-4 py-3">
            <Badge
              variant="outline"
              className={cn("shrink-0", SEVERITY_CLASS[finding.severity])}
            >
              {t.tenderReview.severity[finding.severity]}
            </Badge>
            <div className="min-w-0">
              <div className="text-muted-foreground mb-0.5 font-mono text-[11px]">
                {finding.finding_id} · {finding.dimension}
              </div>
              <h3 className="text-sm leading-5 font-semibold">
                {finding.title}
              </h3>
            </div>
          </CardHeader>

          <CardContent className="space-y-4 px-4 py-4 text-sm">
            <div>
              <div className="text-muted-foreground mb-1 text-xs font-medium">
                {t.tenderReview.issue}
              </div>
              <p className="leading-6">{finding.issue}</p>
            </div>

            <EvidenceList
              title={t.tenderReview.issueEvidence}
              icon="issue"
              evidence={finding.issue_evidence}
              onOpen={openEvidence}
            />

            {finding.legal_basis.length > 0 && (
              <EvidenceList
                title={t.tenderReview.legalBasis}
                icon="basis"
                evidence={finding.legal_basis}
                onOpen={openEvidence}
              />
            )}

            <div className="bg-muted/35 rounded-md p-3">
              <div className="text-muted-foreground mb-1 text-xs font-medium">
                {t.tenderReview.recommendation}
              </div>
              <p className="leading-6">{finding.recommendation}</p>
            </div>
          </CardContent>
        </Card>
      ))}
    </section>
  );
}

function EvidenceList({
  title,
  icon,
  evidence,
  onOpen,
}: {
  title: string;
  icon: "issue" | "basis";
  evidence: TenderReviewEvidence[];
  onOpen: (evidence: TenderReviewEvidence) => void;
}) {
  const { t } = useI18n();
  const Icon = icon === "issue" ? FileSearchIcon : ScrollTextIcon;
  return (
    <div>
      <div className="text-muted-foreground mb-2 text-xs font-medium">
        {title}
      </div>
      <div className="space-y-2">
        {evidence.map((item, index) => {
          const location = [
            item.page ? t.tenderReview.page(item.page) : null,
            item.section,
            item.clause,
          ]
            .filter(Boolean)
            .join(" · ");
          return (
            <Button
              key={`${item.document_name}-${item.page ?? "none"}-${index}`}
              type="button"
              variant="outline"
              className="hover:border-primary/50 h-auto w-full items-start justify-start gap-3 px-3 py-2.5 text-left whitespace-normal"
              onClick={() => onOpen(item)}
            >
              <Icon className="text-primary mt-0.5 size-4 shrink-0" />
              <span className="min-w-0 flex-1">
                <span className="block text-xs font-medium">
                  {item.document_name}
                  {location ? ` · ${location}` : ""}
                </span>
                <span className="text-muted-foreground mt-1 line-clamp-2 block text-xs leading-5">
                  {item.quote}
                </span>
              </span>
              <span className="text-primary shrink-0 text-xs">
                {t.tenderReview.viewOriginal}
              </span>
            </Button>
          );
        })}
      </div>
    </div>
  );
}
