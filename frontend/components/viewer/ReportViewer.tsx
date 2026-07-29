"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { extractDocument, getDocument, pdfUrl } from "@/lib/api";
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

  // A plain (non-forced) read is a query, not a mutation: extract_document is
  // now cache-first server-side (a persisted record from a prior run is
  // served with no LLM calls), so loading the viewer should show whatever's
  // already on disk without waiting for a click -- the click is for the
  // FIRST read of a filing, or for forcing a fresh one via `extract.refetch`
  // after `force` is flipped true below.
  const canRead = Boolean(filing && hasSourceFile(filing));
  // A ref, not state: `refetch()` can run in the same tick as the click that
  // sets this, and queryFn must see the flag that click intended, not a
  // batched-and-not-yet-applied state update.
  const forceRef = useRef(false);
  const extract = useQuery({
    queryKey: ["extract", docId, filing?.doc_key],
    queryFn: () => {
      const force = forceRef.current;
      forceRef.current = false;
      return extractDocument(filing!.doc_key ?? docId, undefined, force);
    },
    enabled: canRead,
  });

  const fields = useMemo(() => extract.data?.fields ?? [], [extract.data]);
  // Indexed by position, matching FieldList's fieldId scheme: field_name
  // alone collides across modules (e.g. two "Cash on hand" rows), so it
  // cannot resolve which field is selected on its own.
  const active =
    selected !== null
      ? (fields.find((f, i) => toFieldId(i, f.field_name) === selected) ?? null)
      : null;

  const searchParams = useSearchParams();
  // A deep link from the taxonomy viewer (StatementTable's provenance links)
  // arrives as ?tag=<TAG>. Applied once, on the render where `fields` first
  // becomes non-empty -- re-applying on every render would fight a user's
  // own subsequent click.
  const appliedDeepLinkRef = useRef(false);
  useEffect(() => {
    if (appliedDeepLinkRef.current || fields.length === 0) return;
    const wantedTag = searchParams.get("tag");
    if (!wantedTag) return;
    appliedDeepLinkRef.current = true;
    const index = fields.findIndex((f) => f.tag === wantedTag);
    if (index === -1) return;
    const target = fields[index];
    setSelected(toFieldId(index, target.field_name));
    if (target.source.page) setPage(target.source.page);
  }, [fields, searchParams]);

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
          disabled={!canRead || extract.isFetching}
          onClick={() => extract.refetch()}
          className="btn btn-primary"
        >
          {extract.isFetching ? "Reading figures" : "Read figures"}
        </button>
        {extract.data ? (
          <button
            type="button"
            disabled={extract.isFetching}
            onClick={() => {
              forceRef.current = true;
              extract.refetch();
            }}
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
          {extract.error ? (
            <p style={{ padding: "var(--sp-6)", fontSize: "var(--text-sm)" }}>
              {(extract.error as Error).message}
            </p>
          ) : fields.length === 0 ? (
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
