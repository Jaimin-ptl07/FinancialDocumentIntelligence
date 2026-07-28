"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { getDocument } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { PageHeader } from "@/components/shell/PageHeader";
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

  return (
    <>
      <PageHeader eyebrow="Costing" title={filingLabel(filing)}>
        <Link href={`/documents/${docId}`} className="btn">
          Report
        </Link>
      </PageHeader>

      <div style={{ padding: "var(--sp-8)" }}>
        <CostingEvents docId={docId} />
      </div>
    </>
  );
}
