"use client";

import {
  DownloadIcon,
  ExternalLinkIcon,
  FileWarningIcon,
  Loader2Icon,
  XIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { fetch as apiFetch } from "@/core/api/fetcher";
import { resolveArtifactURL } from "@/core/artifacts/utils";
import { useI18n } from "@/core/i18n/hooks";
import { getDocumentPreview, withPdfPage } from "@/core/uploads/preview";
import { cn } from "@/lib/utils";

function downloadUrl(url: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}download=true`;
}

export function DocumentPreviewPanel({
  className,
  filename,
  path,
  previewPath,
  url,
  page,
  quote,
  section,
  clause,
  threadId,
  onClose,
}: {
  className?: string;
  filename: string;
  path?: string;
  previewPath?: string;
  url?: string;
  page?: number;
  quote?: string;
  section?: string;
  clause?: string;
  threadId: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const preview = path
    ? getDocumentPreview({ filename, path, previewPath })
    : { kind: "unsupported" as const, path: null };
  const originalUrl = path ? resolveArtifactURL(path, threadId) : url;
  const rawPreviewUrl = preview.path
    ? resolveArtifactURL(preview.path, threadId)
    : null;
  const previewUrl = useMemo(() => {
    if (!rawPreviewUrl || preview.kind !== "pdf" || !page) {
      return rawPreviewUrl;
    }
    return withPdfPage(rawPreviewUrl, page);
  }, [page, preview.kind, rawPreviewUrl]);
  const [text, setText] = useState<string | null>(null);
  const [textError, setTextError] = useState(false);
  const quoteRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (preview.kind !== "text" || !previewUrl) return;

    const controller = new AbortController();
    setText(null);
    setTextError(false);
    void apiFetch(previewUrl, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Preview request failed: ${response.status}`);
        }
        return response.text();
      })
      .then(setText)
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setTextError(true);
        }
      });

    return () => controller.abort();
  }, [preview.kind, previewUrl]);

  useEffect(() => {
    quoteRef.current?.scrollIntoView({ block: "center" });
  }, [quote, text]);

  const quoteIndex = text && quote ? text.indexOf(quote) : -1;
  const location = [page ? t.tenderReview.page(page) : null, section, clause]
    .filter(Boolean)
    .join(" · ");

  return (
    <section className={cn("bg-background flex min-h-0 flex-col", className)}>
      <header className="flex h-12 shrink-0 items-center gap-3 border-b px-3">
        <h2
          className="min-w-0 flex-1 truncate text-sm font-medium"
          title={filename}
        >
          {filename}
        </h2>
        {path && originalUrl && (
          <Button asChild size="icon-sm" variant="ghost">
            <a
              aria-label={t.uploads.download}
              href={downloadUrl(originalUrl)}
              title={t.uploads.download}
            >
              <DownloadIcon />
            </a>
          </Button>
        )}
        {!path && url && (
          <Button asChild size="icon-sm" variant="ghost">
            <a
              aria-label={t.tenderReview.openSource}
              href={url}
              rel="noopener noreferrer"
              target="_blank"
              title={t.tenderReview.openSource}
            >
              <ExternalLinkIcon />
            </a>
          </Button>
        )}
        <Button
          aria-label={t.common.close}
          size="icon-sm"
          variant="ghost"
          onClick={onClose}
          title={t.common.close}
        >
          <XIcon />
        </Button>
      </header>

      {(location || quote) && (
        <div className="bg-muted/30 shrink-0 border-b px-4 py-3">
          {location && (
            <div className="text-primary mb-1 text-xs font-medium">
              {location}
            </div>
          )}
          {quote && (
            <blockquote className="text-muted-foreground line-clamp-3 border-l-2 pl-3 text-xs leading-5">
              {quote}
            </blockquote>
          )}
        </div>
      )}

      <div className="bg-muted/20 min-h-0 flex-1 overflow-hidden">
        {preview.kind === "pdf" && previewUrl && (
          <iframe
            className="size-full border-0"
            src={previewUrl}
            title={filename}
          />
        )}

        {preview.kind === "image" && previewUrl && (
          <div className="flex size-full items-center justify-center overflow-auto p-6">
            <img
              className="max-h-full max-w-full object-contain"
              src={previewUrl}
              alt={filename}
            />
          </div>
        )}

        {preview.kind === "text" && (
          <div className="bg-background size-full overflow-auto p-6">
            {!text && !textError && (
              <div className="text-muted-foreground flex h-full items-center justify-center gap-2">
                <Loader2Icon className="size-4 animate-spin" />
                {t.uploads.loadingPreview}
              </div>
            )}
            {textError && (
              <div className="text-destructive flex h-full items-center justify-center gap-2">
                <FileWarningIcon className="size-5" />
                {t.uploads.previewFailed}
              </div>
            )}
            {text !== null && quoteIndex < 0 && (
              <pre className="font-mono text-sm leading-6 break-words whitespace-pre-wrap">
                {text}
              </pre>
            )}
            {text !== null && quoteIndex >= 0 && quote && (
              <pre className="font-mono text-sm leading-6 break-words whitespace-pre-wrap">
                {text.slice(0, quoteIndex)}
                <mark
                  ref={quoteRef}
                  className="bg-yellow-300/70 text-inherit dark:bg-yellow-500/40"
                >
                  {text.slice(quoteIndex, quoteIndex + quote.length)}
                </mark>
                {text.slice(quoteIndex + quote.length)}
              </pre>
            )}
          </div>
        )}

        {preview.kind === "unsupported" && !quote && (
          <div className="text-muted-foreground flex size-full flex-col items-center justify-center gap-3 p-6 text-center">
            <FileWarningIcon className="size-8" />
            <p>{t.uploads.previewUnavailable}</p>
          </div>
        )}
        {preview.kind === "unsupported" && quote && (
          <div className="flex size-full items-start justify-center overflow-auto p-6">
            <div className="bg-background w-full max-w-2xl rounded-lg border p-5 shadow-sm">
              <div className="text-muted-foreground mb-2 text-xs">
                {t.tenderReview.sourcePreviewUnavailable}
              </div>
              <blockquote className="border-primary border-l-2 pl-4 text-sm leading-7">
                {quote}
              </blockquote>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
