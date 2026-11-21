"use client";

/** Fetches the three FOG artifacts, picks a basis, and composes the panels
 *  beside the source PDF.
 *
 *  Three INDEPENDENT queries -- one per artifact -- so one failing stage does
 *  not blank the page: `lib/fog.ts`'s readers are total and degrade a
 *  missing or malformed body to an empty result on their own, so a stage that
 *  has not run yet only removes what depended on it, never the rest of the
 *  screen. A stage that failed to fetch (as opposed to one that returned
 *  something malformed) gets its own "not built yet" notice naming the stage
 *  -- a filing mid-ingest is the normal case, not an error.
 *
 *  `PdfCanvas` always gets `regions={null}`: a FOG line item carries its
 *  ADDRESS (page, table_index, source_row), not its geometry. Resolving
 *  `structure.tables[i].grid.cell_regions[row]` here would be a second
 *  implementation of `core/provenance.py` in TypeScript (spec §4) -- two
 *  representations of one rule drifting apart is the shape of the RITES
 *  note-grammar bug, where all 10 references silently resolved to nothing. */

import { useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getArtifact, getDocument, pdfUrl } from "@/lib/api";
import { filingLabel } from "@/lib/documents";
import { PageHeader } from "@/components/shell/PageHeader";
import { PdfCanvas } from "@/components/viewer/PdfCanvas";
import { FactsPanel } from "@/components/fog/FactsPanel";
import { AttentionPanel } from "@/components/fog/AttentionPanel";
import { HealthPanel } from "@/components/fog/HealthPanel";
import {
  chooseBasis,
  readContested,
  readFacts,
  readHealth,
  readUnmapped,
  type FogFact,
  type Health,
} from "@/lib/fog";

const EMPTY_HEALTH: Health = {
  footing: [],
  checks: [],
  coverage: {},
  mapping: {},
  schemaUsable: null,
};

const STAGES = ["fog_objects", "fog_mapping", "fog_reason"] as const;

export function FogViewer({ docId }: { docId: string }) {
  const [selectedTag, setSelectedTag] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState<number | null>(null);

  const { data: doc } = useQuery({
    queryKey: ["document", docId],
    queryFn: () => getDocument(docId),
  });

  // One useQuery per artifact -- NOT a single combined fetch -- so a stage
  // that has not run yet does not take the other two down with it.
  const fogQuery = useQuery({
    queryKey: ["artifact", docId, "fog_objects"],
    queryFn: () => getArtifact(docId, "fog_objects"),
  });
  const mappingQuery = useQuery({
    queryKey: ["artifact", docId, "fog_mapping"],
    queryFn: () => getArtifact(docId, "fog_mapping"),
  });
  const reasonQuery = useQuery({
    queryKey: ["artifact", docId, "fog_reason"],
    queryFn: () => getArtifact(docId, "fog_reason"),
  });

  const queriesByStage = {
    fog_objects: fogQuery,
    fog_mapping: mappingQuery,
    fog_reason: reasonQuery,
  } as const;
  const missingStages = STAGES.filter((stage) => queriesByStage[stage].isError);

  // `chooseBasis` and every reader below are TOTAL -- they take `unknown`
  // and degrade a missing or errored query's `undefined` data to an empty
  // view model rather than throwing, so a stage that failed to fetch just
  // means less to show, never a blank screen.
  const basis = chooseBasis(reasonQuery.data);
  const facts = basis
    ? readFacts(reasonQuery.data, mappingQuery.data, fogQuery.data, basis)
    : [];
  const contested = basis ? readContested(reasonQuery.data, basis) : [];
  const unmapped = basis ? readUnmapped(mappingQuery.data, basis) : [];
  const health = basis
    ? readHealth(reasonQuery.data, mappingQuery.data, fogQuery.data, basis)
    : EMPTY_HEALTH;

  // Selecting a fact is what turns the page -- a `LineItem`'s address names
  // a page, never a region, so this is the only navigation a fact can drive.
  const select = useCallback((fact: FogFact) => {
    setSelectedTag(fact.tag);
    if (fact.page !== null) setPage(fact.page);
  }, []);

  const onPageCount = useCallback((count: number) => setPageCount(count), []);

  return (
    <>
      <PageHeader
        eyebrow={filingLabel(doc)}
        title={basis ? `Basis: ${basis}` : "No basis in this filing"}
      />

      {missingStages.length > 0 ? (
        <div
          style={{
            padding: "var(--sp-3) var(--sp-6)",
            borderBottom: "1px solid var(--rule-soft)",
          }}
        >
          {missingStages.map((stage) => (
            <p
              key={stage}
              style={{ margin: 0, fontSize: "var(--text-sm)", color: "var(--ink-soft)" }}
            >
              {stage} has not run for this filing yet — not built.
            </p>
          ))}
        </div>
      ) : null}

      <div
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
            <PdfCanvas
              url={pdfUrl(docId)}
              page={page}
              regions={null}
              onPageCount={onPageCount}
            />
          </div>
        </div>

        <div
          className="viewer-aside overflow-y-auto"
          style={{ background: "var(--leaf)" }}
        >
          <FactsPanel facts={facts} selected={selectedTag} onSelect={select} />
          <AttentionPanel contested={contested} unmapped={unmapped} />
          <HealthPanel health={health} />
        </div>
      </div>
    </>
  );
}
