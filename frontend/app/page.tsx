"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "@/lib/api";
import { PageHeader } from "@/components/shell/PageHeader";
import { FilingRow } from "@/components/data/FilingRow";

/** The analyst's question on opening this app is "which filings are ready to
 *  work on". This screen answers exactly that and nothing else — it is a
 *  ledger index, not a wall of KPI tiles. The figures live on /analytics. */
export default function Dashboard() {
  const { data, error, isPending } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
  });

  // On an empty corpus the empty state owns the invitation; a second identical
  // button in the header would just be the same call to action twice.
  const empty = data?.documents.length === 0;

  return (
    <>
      <PageHeader
        eyebrow="Corpus"
        title="Filings"
        lede="Every annual report read into the system, with how far each one got through the pipeline."
      >
        {empty ? null : (
          <Link href="/ingest" className="btn btn-primary">
            Add a filing
          </Link>
        )}
      </PageHeader>

      <div style={{ padding: "var(--sp-8)" }}>
        {error ? (
          <p className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(error as Error).message}
          </p>
        ) : isPending ? (
          <p className="eyebrow">Loading filings</p>
        ) : data.documents.length === 0 ? (
          <div
            className="sheet"
            style={{
              padding: "var(--sp-16)",
              textAlign: "center",
              boxShadow: "var(--shadow-md)",
            }}
          >
            <p
              className="display"
              style={{
                fontSize: "var(--text-lg)",
                marginBottom: "var(--sp-3)",
              }}
            >
              No filings yet
            </p>
            <p
              style={{
                color: "var(--ink-soft)",
                marginBottom: "var(--sp-6)",
                maxWidth: "42ch",
                marginLeft: "auto",
                marginRight: "auto",
              }}
            >
              Upload an annual report to start reading figures out of it.
            </p>
            <Link href="/ingest" className="btn btn-primary">
              Add a filing
            </Link>
          </div>
        ) : (
          <table
            className="sheet"
            style={{
              width: "100%",
              textAlign: "left",
              borderCollapse: "collapse",
              overflow: "hidden",
              boxShadow: "var(--shadow-md)",
            }}
          >
            <caption className="sr-only">
              Filings, with pipeline progress
            </caption>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--rule)" }}>
                <th
                  className="eyebrow"
                  style={{ padding: "var(--sp-3) var(--sp-4)" }}
                >
                  Filing
                </th>
                <th
                  className="eyebrow"
                  style={{ padding: "var(--sp-3) var(--sp-4)" }}
                >
                  Version
                </th>
                <th
                  className="eyebrow"
                  style={{
                    padding: "var(--sp-3) var(--sp-4)",
                    textAlign: "right",
                  }}
                >
                  Size
                </th>
                <th
                  className="eyebrow"
                  style={{ padding: "var(--sp-3) var(--sp-4)" }}
                >
                  Pipeline
                </th>
                <th
                  className="eyebrow"
                  style={{
                    padding: "var(--sp-3) var(--sp-4)",
                    textAlign: "right",
                  }}
                >
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.documents.map((d) => (
                <FilingRow key={d.id} document={d} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
