"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getCosting } from "@/lib/api";
import { PageHeader } from "@/components/shell/PageHeader";
import { LedgerTotals } from "@/components/analytics/LedgerTotals";
import { formatInr } from "@/lib/format";

/** What to call a filing in the by-document table. Mirrors lib/documents.ts's
 *  filingLabel, but that helper takes a full DocumentView -- this listing's
 *  rows only ever carry filename/doc_key, so the same fallback logic is
 *  inlined here rather than widening that helper's contract for one caller. */
function rowLabel(row: { filename: string | null; doc_key: string | null }): string {
  return row.filename ?? row.doc_key ?? "Untitled record";
}

export default function Costing() {
  const { data, error, isPending } = useQuery({
    queryKey: ["costing"],
    queryFn: getCosting,
  });

  return (
    <>
      <PageHeader
        eyebrow="Spend"
        title="Costing"
        lede="What this corpus has actually cost to read — OCR pages and LLM tokens, priced in Rupees."
      />

      <div style={{ padding: "var(--sp-8)", display: "grid", gap: "var(--sp-8)" }}>
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
                  label: "Total cost",
                  value: formatInr(data.total_cost_inr),
                  note: "Every real OCR page and LLM call, across the whole corpus",
                },
                ...data.by_source.map((s) => ({
                  label: s.source,
                  value: formatInr(s.cost_inr),
                  note: `${s.call_count} call${s.call_count === 1 ? "" : "s"}`,
                })),
              ]}
            />

            <section className="sheet" style={{ padding: "var(--sp-6)" }}>
              <h2
                className="display"
                style={{ fontSize: "var(--text-md)", marginBottom: "var(--sp-4)" }}
              >
                Cost by source
              </h2>
              {data.by_source.length === 0 ? (
                <p style={{ color: "var(--ink-faint)", fontSize: "var(--text-sm)" }}>
                  No billable OCR or LLM calls recorded yet.
                </p>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <caption className="sr-only">
                      Corpus-wide cost, grouped by usage source
                    </caption>
                    <thead>
                      <tr>
                        <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) 0" }}>
                          Source
                        </th>
                        <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-4)" }}>
                          Calls
                        </th>
                        <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-4)" }}>
                          Pages
                        </th>
                        <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-4)" }}>
                          Tokens (in/out)
                        </th>
                        <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) 0", width: "8rem" }}>
                          Cost
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_source.map((s) => (
                        <tr key={s.source} style={{ borderTop: "1px solid var(--rule-soft)" }}>
                          <td className="data" style={{ padding: "var(--sp-3) 0", fontSize: "var(--text-xs)" }}>
                            {s.source}
                          </td>
                          <td className="data" style={{ padding: "var(--sp-3) var(--sp-4)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                            {s.call_count.toLocaleString("en-IN")}
                          </td>
                          <td className="data" style={{ padding: "var(--sp-3) var(--sp-4)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                            {s.page_count > 0 ? s.page_count.toLocaleString("en-IN") : "—"}
                          </td>
                          <td className="data" style={{ padding: "var(--sp-3) var(--sp-4)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                            {s.input_tokens > 0 || s.output_tokens > 0
                              ? `${s.input_tokens.toLocaleString("en-IN")} / ${s.output_tokens.toLocaleString("en-IN")}`
                              : "—"}
                          </td>
                          <td className="data" style={{ padding: "var(--sp-3) 0", fontSize: "var(--text-xs)", textAlign: "right" }}>
                            {formatInr(s.cost_inr)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="sheet" style={{ padding: "var(--sp-6)" }}>
              <h2
                className="display"
                style={{ fontSize: "var(--text-md)", marginBottom: "var(--sp-2)" }}
              >
                Cost by filing
              </h2>
              <p
                style={{
                  color: "var(--ink-soft)",
                  fontSize: "var(--text-sm)",
                  maxWidth: "64ch",
                  marginBottom: "var(--sp-5)",
                }}
              >
                Every filing with at least one billable call, most expensive
                first. Corpus-wide questions (like /ask) aren&rsquo;t tied to
                one filing, so their cost only appears in the total above.
              </p>
              {data.by_document.length === 0 ? (
                <p style={{ color: "var(--ink-faint)", fontSize: "var(--text-sm)" }}>
                  No filing has any billable usage yet.
                </p>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <caption className="sr-only">
                      Filings ranked by total cost, most expensive first
                    </caption>
                    <thead>
                      <tr>
                        <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) 0" }}>
                          Filing
                        </th>
                        <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) 0", width: "8rem" }}>
                          Cost
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.by_document.map((d) => (
                        <tr key={d.doc_id} style={{ borderTop: "1px solid var(--rule-soft)" }}>
                          <td className="data" style={{ padding: "var(--sp-3) 0", fontSize: "var(--text-xs)" }}>
                            <Link href={`/documents/${d.doc_id}/costing`} className="text-ink">
                              {rowLabel(d)}
                            </Link>
                          </td>
                          <td className="data" style={{ padding: "var(--sp-3) 0", fontSize: "var(--text-xs)", textAlign: "right" }}>
                            {formatInr(d.total_cost_inr)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </>
  );
}
