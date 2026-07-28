"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getDocument, getDocumentCost } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { formatInr } from "@/lib/format";
import { PageHeader } from "@/components/shell/PageHeader";
import { LedgerTotals } from "@/components/analytics/LedgerTotals";
import { CostingEvents } from "@/components/costing/CostingEvents";

/** The dedicated per-document costing screen -- every real OCR/LLM call
 *  this filing made, and what it cost. Deliberately separate from
 *  ReportViewer (which stays focused on extracted values): costing is an
 *  audit/spend question, not a reading one, and the two pages are cross-
 *  linked rather than merged. */
export function DocumentCosting({ docId }: { docId: string }) {
  const { data: filing } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  const { data: cost, error: costError, isPending: costPending } = useQuery({
    queryKey: ["cost", docId],
    queryFn: () => getDocumentCost(docId),
  });

  return (
    <>
      <PageHeader eyebrow="Costing" title={filingLabel(filing)}>
        <Link href={`/documents/${docId}`} className="btn">
          Report
        </Link>
      </PageHeader>

      <div style={{ padding: "var(--sp-8)", display: "grid", gap: "var(--sp-8)" }}>
        {costError ? (
          <p className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(costError as Error).message}
          </p>
        ) : costPending ? (
          <p className="eyebrow">Loading figures</p>
        ) : (
          <LedgerTotals
            items={[
              {
                label: "Total cost",
                value: formatInr(cost.total_cost_inr),
                note: "Every real OCR page and LLM call, across this filing",
              },
              ...cost.by_source.map((s) => ({
                label: s.source,
                value: formatInr(s.cost_inr),
                note: `${s.call_count} call${s.call_count === 1 ? "" : "s"}`,
              })),
            ]}
          />
        )}

        <CostingEvents docId={docId} />
      </div>
    </>
  );
}
