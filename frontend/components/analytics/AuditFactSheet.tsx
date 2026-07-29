/** The audit surface for one filing's Fact bundle (POST /factsheet/document).
 *
 *  Three ruled sections, in the ledger's own idiom — completeness at the head,
 *  the CARO/Schedule-III ratios, and the reconciliation checks. Tone is carried
 *  the way LedgerTotals carries it: an ink glyph (✓ / !) and rule weight, never
 *  a second hue. --tether stays reserved for provenance, so a FAILED check is a
 *  doubled rule + "!", not red.
 *
 *  Presentational only: it takes a decoded factsheet and renders it, so it is
 *  trivially testable and has no idea where the data came from. */

export type RatioFact = {
  key: string;
  label: string;
  value: number | null;
  status: string;
  caro_clause: string;
  explanation: string;
};

export type ValidationResult = {
  rule_id: string;
  kind: string;
  expected: number | string;
  actual: number | string;
  delta: number;
  severity: string;
  passed: boolean;
};

export type FactSheetBundle = {
  summary: {
    extracted: number;
    derived_ok: number;
    derived_unresolved: number;
    ratios_ok: number;
    checks_total: number;
    checks_failed: number;
    bands: { high: number; needs_review: number; low: number; failed: number };
  };
  ratios: RatioFact[];
  validations: ValidationResult[];
  coverage: {
    totals: {
      expected: number;
      found_extracted: number;
      derived_ok: number;
      missing_extracted: number;
      completeness_pct: number;
    };
  };
};

const fmt = (n: number | null): string =>
  n === null || Number.isNaN(n) ? "—" : n.toFixed(2);

export function AuditFactSheet({ data }: { data: FactSheetBundle }) {
  const { summary, ratios, validations, coverage } = data;
  const t = coverage.totals;

  return (
    <section className="sheet" aria-label="Audit factsheet">
      <header
        style={{
          padding: "var(--sp-5) var(--sp-6)",
          borderBottom: "1px solid var(--ink)",
          display: "flex",
          gap: "var(--sp-6)",
          flexWrap: "wrap",
        }}
      >
        <Stat label="Completeness" value={`${t.completeness_pct}%`} />
        <Stat label="Extracted" value={String(t.found_extracted)} />
        <Stat label="Derived" value={String(t.derived_ok)} />
        <Stat label="Missing" value={String(t.missing_extracted)} />
        <Stat
          label="Checks failed"
          value={`${summary.checks_failed}/${summary.checks_total}`}
          tone={summary.checks_failed > 0 ? "flag" : "tick"}
        />
      </header>

      <div style={{ padding: "var(--sp-4) var(--sp-6)" }}>
        <h3 style={{ margin: "var(--sp-3) 0" }}>Ratios (CARO xix / Schedule III)</h3>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <tbody>
            {ratios.map((r) => (
              <tr key={r.key} style={{ borderTop: "1px solid var(--rule-soft)" }}>
                <td style={{ padding: "var(--sp-2) 0" }}>{r.label}</td>
                <td
                  style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}
                  data-status={r.status}
                >
                  {r.status === "ok" ? fmt(r.value) : `— (${r.status})`}
                </td>
                <td style={{ textAlign: "right", opacity: 0.6, paddingLeft: "var(--sp-4)" }}>
                  {r.caro_clause}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ padding: "var(--sp-4) var(--sp-6)" }}>
        <h3 style={{ margin: "var(--sp-3) 0" }}>Reconciliation & identity checks</h3>
        {validations.length === 0 && <p style={{ opacity: 0.6 }}>No checks applicable.</p>}
        {validations.map((v) => (
          <div
            key={v.rule_id}
            role="listitem"
            data-passed={v.passed}
            style={{
              display: "flex",
              justifyContent: "space-between",
              padding: "var(--sp-2) 0",
              borderTop: v.passed ? "1px solid var(--rule-soft)" : "3px double var(--ink)",
              fontWeight: v.passed ? 400 : 600,
            }}
          >
            <span>
              {v.passed ? "✓" : "!"} {v.rule_id}
            </span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              exp {String(v.expected)} · act {String(v.actual)} · Δ {fmt(v.delta)}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "tick" | "flag";
}) {
  return (
    <div
      style={{
        borderTop:
          tone === "flag"
            ? "3px double var(--ink)"
            : tone === "tick"
              ? "1px solid var(--ink)"
              : undefined,
        paddingTop: tone ? "var(--sp-2)" : undefined,
      }}
    >
      <div style={{ fontVariantNumeric: "tabular-nums", fontSize: "1.4rem" }}>{value}</div>
      <div style={{ opacity: 0.6, fontSize: "0.8rem" }}>{label}</div>
    </div>
  );
}
