/** Every resolved Fact, as a button so the viewer can jump to the page that
 *  proves it.
 *
 *  Presentational only: props in, markup out. Method weight tracks evidence
 *  tier, mirroring `core/fog_reason.py::_tier` EXACTLY: memory, a section-
 *  confirmed lexical hit, and an EXACT lexical hit (score >= 0.95) are the
 *  heaviest evidence; an LLM judgement is mid; a bare token overlap is the
 *  lightest.
 *
 *  The score is what makes that possible. `lexical_match` returns method
 *  "name" for BOTH an exact hit (1.0) and a token-overlap guess (<= 0.9), so
 *  ranking on the method string alone would file every exact match under the
 *  weakest evidence there is -- the same conflation the backend had to fix in
 *  `_tier` itself.
 *
 *  Weight and glyph carry the distinction, never a colour -- --tether stays
 *  reserved for provenance elsewhere in the app. */

import type { FogFact } from "@/lib/fog";
import { formatInr } from "@/lib/format";

const NO_PROOF = "no proof recorded";

/** Mirrors `core/fog_reason.py::_EXACT_SCORE`. */
const EXACT_SCORE = 0.95;

function methodWeight(method: string, score: number): number {
  if (method === "memory") return 700;
  if (method.startsWith("lexical:section")) return 700;
  if (method.startsWith("lexical") && score >= EXACT_SCORE) return 700;
  if (method === "llm") return 500;
  return 400;
}

export function FactsPanel({
  facts,
  selected,
  onSelect,
}: {
  facts: FogFact[];
  selected: string | null;
  onSelect: (f: FogFact) => void;
}) {
  if (facts.length === 0) {
    return (
      <p style={{ padding: "var(--sp-6)", color: "var(--ink-soft)", fontSize: "var(--text-sm)" }}>
        No facts.
      </p>
    );
  }

  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {facts.map((fact) => {
        const hasProof = fact.method !== "";
        return (
          <li key={fact.tag} style={{ borderTop: "1px solid var(--rule-soft)" }}>
            <button
              type="button"
              onClick={() => onSelect(fact)}
              aria-pressed={selected === fact.tag}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "baseline",
                justifyContent: "space-between",
                gap: "var(--sp-4)",
                padding: "var(--sp-3) var(--sp-4)",
                border: "none",
                background: selected === fact.tag ? "var(--leaf-2)" : "transparent",
                textAlign: "left",
                cursor: "pointer",
                color: "var(--ink)",
              }}
            >
              <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                <span className="data" style={{ fontWeight: 600 }}>
                  {fact.tag}
                </span>
                <span
                  style={{
                    fontSize: "var(--text-xs)",
                    fontWeight: hasProof ? methodWeight(fact.method, fact.score) : 400,
                    color: hasProof ? "var(--ink-soft)" : "var(--flag)",
                    borderBottom: hasProof ? undefined : "1.5px dotted var(--flag)",
                  }}
                >
                  {hasProof ? fact.method : NO_PROOF}
                </span>
              </span>
              <span className="data" style={{ fontVariantNumeric: "tabular-nums", flexShrink: 0 }}>
                {formatInr(fact.value)}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
