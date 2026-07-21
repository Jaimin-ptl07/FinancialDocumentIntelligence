"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { extractDocument, getDocument, pdfUrl } from "@/lib/api";
import { filingLabel, hasSourceFile } from "@/lib/documents";
import { PageHeader } from "@/components/shell/PageHeader";
import { PdfCanvas } from "@/components/viewer/PdfCanvas";
import { FieldList } from "@/components/viewer/FieldList";
import { Tether } from "@/components/viewer/Tether";

export function ReportViewer({ docId }: { docId: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [highlightRect, setHighlightRect] = useState<DOMRect | null>(null);
  const [fieldRect, setFieldRect] = useState<DOMRect | null>(null);
  const [containerRect, setContainerRect] = useState<DOMRect | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const { data: filing } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  const extract = useMutation({
    mutationFn: () => extractDocument(filing!.doc_key ?? docId),
  });

  // Memoized because the tether re-measures whenever `fields` changes identity;
  // a fresh array on every render would make that effect run on every paint.
  const fields = useMemo(() => extract.data?.fields ?? [], [extract.data]);
  const active = fields.find((f) => f.field_name === selected) ?? null;

  // Selecting a field is what turns the page. The reference mark says p84, so
  // clicking it must actually land on 84.
  const select = useCallback(
    (fieldName: string) => {
      setSelected(fieldName);
      const target = fields.find((f) => f.field_name === fieldName);
      if (target?.source.page) setPage(target.source.page);
    },
    [fields],
  );

  // Both endpoints of the tether move whenever the panes scroll or the window
  // resizes, so the geometry is re-measured on those events rather than only
  // when the selection changes.
  useEffect(() => {
    const measure = () => {
      setContainerRect(containerRef.current?.getBoundingClientRect() ?? null);
      if (!selected) {
        setFieldRect(null);
        return;
      }
      const row = containerRef.current?.querySelector(
        `[data-field="${CSS.escape(selected)}"]`,
      );
      setFieldRect(row?.getBoundingClientRect() ?? null);
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [selected, fields, page]);

  const onPageCount = useCallback((count: number) => setPageCount(count), []);
  const onHighlightRect = useCallback(
    (rect: DOMRect | null) => setHighlightRect(rect),
    [],
  );

  return (
    <>
      <PageHeader
        eyebrow={filing ? `Version ${filing.version ?? "—"}` : "Filing"}
        title={filingLabel(filing)}
      >
        <button
          type="button"
          disabled={!filing || !hasSourceFile(filing) || extract.isPending}
          onClick={() => extract.mutate()}
          className="btn btn-primary"
        >
          {extract.isPending ? "Reading figures" : "Read figures"}
        </button>
      </PageHeader>

      <div
        ref={containerRef}
        className="viewer-split relative"
        style={{ minHeight: "calc(100vh - 129px)" }}
      >
        <div
          className="flex-1 min-w-0 flex flex-col"
          style={{ borderRight: "1px solid var(--rule)" }}
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
                onHighlightRect={onHighlightRect}
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

        <Tether from={fieldRect} to={highlightRect} container={containerRect} />
      </div>
    </>
  );
}
