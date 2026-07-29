"use client";

import { useQuery } from "@tanstack/react-query";
import { getDocumentCostEvents } from "@/lib/api";
import { formatInr } from "@/lib/format";

/** Every individual OCR/LLM call for one document -- the raw per-call
 *  detail behind the source-level totals shown elsewhere. Kept as its
 *  own section (not folded into the split viewer above) since it's a
 *  variable-length audit table, not part of the read/extract flow. */
export function CostingEvents({ docId }: { docId: string }) {
  const { data, error, isPending } = useQuery({
    queryKey: ["cost-events", docId],
    queryFn: () => getDocumentCostEvents(docId),
  });

  const events = data?.events ?? [];

  return (
    <section className="sheet" style={{ padding: "var(--sp-6)" }}>
      <h2
        className="display"
        style={{ fontSize: "var(--text-md)", marginBottom: "var(--sp-4)" }}
      >
        Every call
      </h2>

      {error ? (
        <p style={{ fontSize: "var(--text-sm)" }}>{(error as Error).message}</p>
      ) : isPending ? (
        <p className="eyebrow">Loading</p>
      ) : events.length === 0 ? (
        <p style={{ color: "var(--ink-faint)", fontSize: "var(--text-sm)" }}>
          No billable OCR or LLM calls recorded for this document yet.
        </p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <caption className="sr-only">
              Every real OCR/LLM call this document made, with cost in Rupees
            </caption>
            <thead>
              <tr>
                <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) var(--sp-3) var(--sp-2) 0" }}>
                  When
                </th>
                <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) var(--sp-3)" }}>
                  Source
                </th>
                <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) var(--sp-3)" }}>
                  Model
                </th>
                <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-3)" }}>
                  Pages
                </th>
                <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-3)" }}>
                  In tok
                </th>
                <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-3)" }}>
                  Out tok
                </th>
                <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) var(--sp-3)" }}>
                  Duration
                </th>
                <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) 0 var(--sp-2) var(--sp-3)" }}>
                  Cost
                </th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id} style={{ borderTop: "1px solid var(--rule-soft)" }}>
                  <td
                    className="data"
                    style={{ padding: "var(--sp-2) var(--sp-3) var(--sp-2) 0", fontSize: "var(--text-xs)", color: "var(--ink-soft)", whiteSpace: "nowrap" }}
                    title={e.correlation_id ? `correlation ${e.correlation_id}` : undefined}
                  >
                    {new Date(e.created_at).toLocaleString()}
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)" }}>
                    {e.source}
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)", color: "var(--ink-soft)" }}>
                    {e.model || "—"}
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                    {e.kind === "ocr" ? e.page_count : "—"}
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                    {e.kind === "llm" ? e.input_tokens.toLocaleString("en-IN") : "—"}
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                    {e.kind === "llm" ? e.output_tokens.toLocaleString("en-IN") : "—"}
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)", textAlign: "right", color: "var(--ink-soft)" }}>
                    {(e.duration_ms / 1000).toFixed(2)}s
                  </td>
                  <td className="data" style={{ padding: "var(--sp-2) 0 var(--sp-2) var(--sp-3)", fontSize: "var(--text-xs)", textAlign: "right" }}>
                    {formatInr(e.cost_inr)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
