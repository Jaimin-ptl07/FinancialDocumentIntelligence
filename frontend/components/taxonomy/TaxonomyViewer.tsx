"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { factsheetDocument, getDocument } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { PageHeader } from "@/components/shell/PageHeader";
import { AuditFactSheet } from "@/components/analytics/AuditFactSheet";
import { StatementTable } from "./StatementTable";

export function TaxonomyViewer({ docId }: { docId: string }) {
  const { data: filing } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  const factsheet = useQuery({
    queryKey: ["factsheet", docId, filing?.doc_key],
    queryFn: () => factsheetDocument(filing!.doc_key ?? docId),
    enabled: Boolean(filing?.doc_key),
  });

  const [activeStatement, setActiveStatement] = useState<string | null>(null);
  const statements = factsheet.data?.statements ?? [];
  const active = statements.find((s) => s.statement === activeStatement) ?? statements[0] ?? null;

  return (
    <div className="flex flex-col" style={{ minHeight: "100vh" }}>
      <PageHeader
        eyebrow={filing ? `Version ${filing.version ?? "—"}` : "Filing"}
        title={filingLabel(filing)}
        lede="Every tagged line item from the taxonomy, laid out the way the source statements are, with figures read from this filing where they were found."
      />

      <div style={{ padding: "var(--sp-8)" }}>
        {factsheet.error ? (
          <p role="alert" className="sheet" style={{ padding: "var(--sp-6)" }}>
            {(factsheet.error as Error).message}
          </p>
        ) : factsheet.isPending ? (
          <p className="eyebrow">Reading the factsheet</p>
        ) : (
          <>
            <div style={{ marginBottom: "var(--sp-6)" }}>
              <AuditFactSheet data={factsheet.data} />
            </div>

            <div
              className="flex"
              style={{ gap: "var(--sp-1)", marginBottom: "var(--sp-4)" }}
            >
              {statements.map((s) => {
                const isActive = active?.statement === s.statement;
                return (
                  <button
                    key={s.statement}
                    type="button"
                    onClick={() => setActiveStatement(s.statement)}
                    aria-pressed={isActive}
                    className="data"
                    style={{
                      fontSize: "var(--text-xs)",
                      padding: "var(--sp-2) var(--sp-4)",
                      borderRadius: "var(--radius-sm)",
                      border: "1px solid",
                      borderColor: isActive ? "var(--ink)" : "var(--rule)",
                      background: isActive ? "var(--ink)" : "transparent",
                      color: isActive ? "var(--leaf)" : "var(--ink-soft)",
                      cursor: "pointer",
                    }}
                  >
                    {s.name}
                  </button>
                );
              })}
            </div>

            {active ? <StatementTable tree={active.tree} docId={docId} /> : null}
          </>
        )}
      </div>
    </div>
  );
}
