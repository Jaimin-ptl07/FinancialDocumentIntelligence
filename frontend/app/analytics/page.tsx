"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { extractDocument, getAnalytics, listDocuments } from "@/lib/api";
import { PageHeader } from "@/components/shell/PageHeader";
import { LedgerTotals } from "@/components/analytics/LedgerTotals";
import { StageThroughput } from "@/components/analytics/StageThroughput";
import { ConfidenceHistogram } from "@/components/analytics/ConfidenceHistogram";
import { formatBytes, formatPercent } from "@/lib/format";

export default function Analytics() {
  const [docKey, setDocKey] = useState("");

  const { data, error, isPending } = useQuery({
    queryKey: ["analytics"],
    queryFn: getAnalytics,
  });

  const { data: filings } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
  });

  // Extraction is a synchronous on-demand call and nothing persists its
  // results, so quality figures cannot be served from the corpus — they have
  // to be read now, for one filing at a time. The screen says so rather than
  // implying a number it does not have.
  const read = useMutation({
    mutationFn: (key: string) => extractDocument(key),
  });

  const fields = read.data?.fields ?? [];
  const found = fields.filter((f) => f.value !== null && f.value !== undefined);
  const soft = found.filter((f) => f.confidence < 0.5);

  return (
    <>
      <PageHeader
        eyebrow="Assurance"
        title="Quality"
        lede="How much of the corpus the pipeline got through, and how sure the model was about what it read."
      />

      <div
        style={{ padding: "var(--sp-8)", display: "grid", gap: "var(--sp-8)" }}
      >
        {error ? (
          <p className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(error as Error).message}
          </p>
        ) : isPending ? (
          <p className="eyebrow">Loading figures</p>
        ) : (
          <>
            <LedgerTotals
              items={[
                {
                  label: "Filings",
                  value: String(data.corpus.document_count),
                  note: `${data.corpus.doc_key_count} distinct reports · ${formatBytes(data.corpus.total_bytes)}`,
                },
                {
                  label: "Read end to end",
                  value: String(data.documents_complete),
                  tone: "tick",
                  note:
                    data.corpus.document_count > 0
                      ? formatPercent(
                          data.documents_complete / data.corpus.document_count,
                        ) + " of the corpus"
                      : undefined,
                },
                {
                  label: "Stopped on a failure",
                  value: String(data.documents_failed),
                  tone: data.documents_failed > 0 ? "flag" : undefined,
                  note:
                    data.documents_failed > 0
                      ? "Reprocess from the failed stage"
                      : "Nothing is stuck",
                },
                {
                  label: "Chunks indexed",
                  value: data.chunk_count.toLocaleString("en-IN"),
                  note: "Retrievable passages",
                },
              ]}
            />

            <section className="sheet" style={{ padding: "var(--sp-6)" }}>
              <h2
                className="display"
                style={{
                  fontSize: "var(--text-md)",
                  marginBottom: "var(--sp-4)",
                }}
              >
                Pipeline throughput
              </h2>
              <StageThroughput stages={data.stages} />
            </section>
          </>
        )}

        <section className="sheet" style={{ padding: "var(--sp-6)" }}>
          <h2
            className="display"
            style={{ fontSize: "var(--text-md)", marginBottom: "var(--sp-2)" }}
          >
            Extraction quality
          </h2>
          <p
            style={{
              color: "var(--ink-soft)",
              fontSize: "var(--text-sm)",
              maxWidth: "64ch",
              marginBottom: "var(--sp-5)",
            }}
          >
            Extraction runs on demand and its results are not stored, so these
            figures are read fresh for one filing at a time.
          </p>

          <div
            className="flex items-center"
            style={{ gap: "var(--sp-3)", marginBottom: "var(--sp-6)" }}
          >
            <label htmlFor="filing" className="sr-only">
              Filing to read
            </label>
            <select
              id="filing"
              className="input"
              style={{ maxWidth: "24rem" }}
              value={docKey}
              onChange={(e) => setDocKey(e.target.value)}
            >
              <option value="">Choose a filing</option>
              {(filings?.documents ?? []).map((d) => (
                <option key={d.id} value={d.doc_key ?? d.id}>
                  {d.filename ?? d.doc_key ?? d.id}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!docKey || read.isPending}
              onClick={() => read.mutate(docKey)}
            >
              {read.isPending ? "Reading" : "Read figures"}
            </button>
          </div>

          {read.error ? (
            <p style={{ fontSize: "var(--text-sm)" }}>
              {(read.error as Error).message}
            </p>
          ) : fields.length === 0 ? (
            <p style={{ color: "var(--ink-faint)", fontSize: "var(--text-sm)" }}>
              Pick a filing to see how much of the schema it answered.
            </p>
          ) : (
            <div
              style={{
                display: "grid",
                gap: "var(--sp-8)",
                gridTemplateColumns: "minmax(0, 1fr) minmax(0, 22rem)",
                alignItems: "start",
              }}
            >
              <ConfidenceHistogram fields={fields} />
              <LedgerTotals
                items={[
                  {
                    label: "Fields answered",
                    value: `${found.length}/${fields.length}`,
                    note: formatPercent(found.length / Math.max(1, fields.length)),
                  },
                  {
                    label: "Need a check",
                    value: String(soft.length),
                    tone: soft.length > 0 ? "flag" : "tick",
                    note: "Confidence below 0.50",
                  },
                ]}
              />
            </div>
          )}
        </section>
      </div>
    </>
  );
}
