"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { listDocuments } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { summarizeStageCounts } from "@/lib/stages";
import { PageHeader } from "@/components/shell/PageHeader";

export default function TaxonomyPickerPage() {
  const { data, error, isPending } = useQuery({
    queryKey: ["documents"],
    queryFn: () => listDocuments(),
  });

  const ready = (data?.documents ?? []).filter((d) => {
    const { done, failed, total } = summarizeStageCounts(d.stage_counts);
    return failed === 0 && done >= total;
  });

  return (
    <>
      <PageHeader
        eyebrow="Taxonomy"
        title="Statement viewer"
        lede="Pick a filing whose pipeline has finished to see its figures laid out against the full taxonomy hierarchy."
      />

      <div style={{ padding: "var(--sp-8)" }}>
        {error ? (
          <p className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(error as Error).message}
          </p>
        ) : isPending ? (
          <p className="eyebrow">Loading filings</p>
        ) : ready.length === 0 ? (
          <div
            className="sheet"
            style={{ padding: "var(--sp-16)", textAlign: "center", boxShadow: "var(--shadow-md)" }}
          >
            <p className="display" style={{ fontSize: "var(--text-lg)", marginBottom: "var(--sp-3)" }}>
              No filings are ready yet
            </p>
            <p style={{ color: "var(--ink-soft)", marginBottom: "var(--sp-6)" }}>
              A filing needs to finish its whole pipeline before its figures can be read against the taxonomy.
            </p>
            <Link href="/" className="btn btn-primary">
              Filings
            </Link>
          </div>
        ) : (
          <table
            className="sheet"
            style={{
              width: "100%", textAlign: "left", borderCollapse: "collapse",
              overflow: "hidden", boxShadow: "var(--shadow-md)",
            }}
          >
            <caption className="sr-only">Filings ready for the taxonomy viewer</caption>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--rule)" }}>
                <th className="eyebrow" style={{ padding: "var(--sp-3) var(--sp-4)" }}>Filing</th>
                <th className="eyebrow" style={{ padding: "var(--sp-3) var(--sp-4)" }}>Version</th>
              </tr>
            </thead>
            <tbody>
              {ready.map((d) => (
                <tr key={d.id} style={{ borderTop: "1px solid var(--rule-soft)" }}>
                  <td style={{ padding: "var(--sp-3) var(--sp-4)" }}>
                    <Link
                      href={`/taxonomy/${d.id}`}
                      style={{ fontWeight: 500, textDecoration: "underline", textUnderlineOffset: "3px" }}
                    >
                      {filingLabel(d)}
                    </Link>
                  </td>
                  <td className="data" style={{ padding: "var(--sp-3) var(--sp-4)", fontSize: "var(--text-sm)" }}>
                    v{d.version ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
