"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { getDocument, pdfUrl } from "@/lib/api";
import { submitExtract } from "@/lib/jobs";
import type { ExtractDocumentView } from "@/lib/types";
import { useJob } from "@/lib/useJob";
import { filingLabel, hasSourceFile } from "@/lib/documents";
import { fieldId as toFieldId } from "@/lib/format";
import { PageHeader } from "@/components/shell/PageHeader";
import { PdfCanvas } from "@/components/viewer/PdfCanvas";
import { FieldList } from "@/components/viewer/FieldList";

export function ReportViewer({ docId }: { docId: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const { data: filing } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  // Reading the figures is an API job (T5): submit, poll, then fetch the
  // body the route used to answer inline. A plain (non-forced) read is cheap
  // -- extract_document is cache-first server-side, serving a persisted record
  // with no LLM calls -- so opening the viewer starts one by itself, unless a
  // job for this filing is already remembered (left running, or settled):
  // then that job is picked back up instead of paying for another.
  const canRead = Boolean(filing && hasSourceFile(filing));
  const docKey = filing ? (filing.doc_key ?? docId) : null;
  const extract = useJob<ExtractDocumentView, [boolean]>({
    slot: canRead && docKey ? `extract:${docKey}` : null,
    submit: (force) => submitExtract(docKey!, undefined, force),
  });
  const { start, phase, restored } = extract;
  const autoStarted = useRef<string | null>(null);
  useEffect(() => {
    if (!canRead || !restored || phase !== "idle" || autoStarted.current === docKey) return;
    autoStarted.current = docKey;
    start(false);
  }, [canRead, restored, phase, docKey, start]);

  const fields = useMemo(() => extract.result?.fields ?? [], [extract.result]);
  // Indexed by position, matching FieldList's fieldId scheme: field_name
  // alone collides across modules (e.g. two "Cash on hand" rows), so it
  // cannot resolve which field is selected on its own.
  const active =
    selected !== null
      ? (fields.find((f, i) => toFieldId(i, f.field_name) === selected) ?? null)
      : null;

  // Selecting a field is what turns the page. The reference mark says p84, so
  // clicking it must actually land on 84.
  const select = useCallback(
    (fieldId: string) => {
      setSelected(fieldId);
      const target = fields.find((f, i) => toFieldId(i, f.field_name) === fieldId);
      if (target?.source.page) setPage(target.source.page);
    },
    [fields],
  );

  const onPageCount = useCallback((count: number) => setPageCount(count), []);

  return (
    <>
      <PageHeader
        eyebrow={filing ? `Version ${filing.version ?? "—"}` : "Filing"}
        title={filingLabel(filing)}
      >
        <button
          type="button"
          disabled={!canRead || extract.busy}
          onClick={() => start(false)}
          className="btn btn-primary"
        >
          {phase === "queued" ? "Queued" : extract.busy ? "Reading figures" : "Read figures"}
        </button>
        {extract.result ? (
          <button
            type="button"
            disabled={extract.busy}
            onClick={() => start(true)}
            className="btn"
            title="Discard the stored reading and extract again from scratch"
          >
            Re-extract
          </button>
        ) : null}
        <Link href={`/documents/${docId}/costing`} className="btn">
          Costing
        </Link>
      </PageHeader>

      <div
        ref={containerRef}
        className="viewer-split relative"
        style={{ height: "calc(100vh - 129px)" }}
      >
        <div
          className="flex-1 min-w-0 flex flex-col"
          style={{ borderRight: "1px solid var(--rule)", minHeight: 0 }}
        >
          <div
            className="flex items-center shrink-0"
            style={{
              gap: "var(--sp-3)",
              padding: "var(--sp-2) var(--sp-4)",
              borderBottom: "1px solid var(--rule-soft)",
            }}
          >
            <button
              type="button"
              className="btn"
              style={{ padding: "var(--sp-1) var(--sp-2)" }}
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              Previous
            </button>
            <span className="data" style={{ fontSize: "var(--text-xs)" }}>
              Page {page}
              {pageCount ? ` of ${pageCount}` : ""}
            </span>
            <button
              type="button"
              className="btn"
              style={{ padding: "var(--sp-1) var(--sp-2)" }}
              disabled={pageCount !== null && page >= pageCount}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>

          <div className="flex-1 overflow-auto" style={{ padding: "var(--sp-6)" }}>
            {filing && !hasSourceFile(filing) ? (
              <div style={{ maxWidth: "48ch" }}>
                <p
                  className="display"
                  style={{
                    fontSize: "var(--text-md)",
                    marginBottom: "var(--sp-2)",
                  }}
                >
                  No source PDF
                </p>
                <p style={{ color: "var(--ink-soft)", fontSize: "var(--text-sm)" }}>
                  This record was created by the pipeline rather than uploaded,
                  so there is no filing to read alongside. Upload the annual
                  report to see its pages here.
                </p>
                <Link
                  href="/ingest"
                  className="btn"
                  style={{ marginTop: "var(--sp-5)" }}
                >
                  Add a filing
                </Link>
              </div>
            ) : (
              <PdfCanvas
                url={pdfUrl(docId)}
                page={page}
                regions={active?.source.bounding_regions ?? null}
                onPageCount={onPageCount}
              />
            )}
          </div>
        </div>

        <div
          className="viewer-aside overflow-y-auto"
          style={{ background: "var(--leaf)" }}
        >
          {/* Above the figures, not instead of them: a failed re-read leaves
              the previous reading standing, and it is still worth showing. */}
          {extract.problem ? (
            <p style={{ padding: "var(--sp-6)", fontSize: "var(--text-sm)" }}>
              {extract.problem}
            </p>
          ) : null}
          {extract.problem && fields.length === 0 ? null : fields.length === 0 ? (
            <p
              style={{
                padding: "var(--sp-6)",
                color: "var(--ink-soft)",
                fontSize: "var(--text-sm)",
              }}
            >
              Read the figures out of this filing to see them beside the page
              they came from.
            </p>
          ) : (
            <FieldList fields={fields} selected={selected} onSelect={select} />
          )}
        </div>
      </div>
    </>
  );
}
