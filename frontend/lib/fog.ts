/** Pure readers over the three Financial Object Graph artifacts.
 *
 *  No fetching and no React, matching `lib/stages.ts`: the artifact bodies have
 *  changed shape ten times across backend phases 2-6, and every change should
 *  break one focused test file rather than a component tree.
 *
 *  TOTAL, mirroring `fdi.core`'s own law: every reader takes `unknown` and
 *  degrades a missing or malformed body to an empty result. A panel that
 *  renders nothing is recoverable; a page that throws is not. */

export type Basis = string;

export type FogFact = {
  tag: string;
  value: number;
  /** How the mapping was resolved: "memory" | "lexical:*" | "llm" | "".
   *  Empty means the proof record is absent, NOT that it was guessed. */
  method: string;
  page: number | null;
  label: string;
};

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const isNumber = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);

const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** `doc.bases[basis]`, or `{}`. */
function region(doc: unknown, basis: Basis): Record<string, unknown> {
  if (!isRecord(doc)) return {};
  const bases = doc.bases;
  if (!isRecord(bases)) return {};
  const found = bases[basis];
  return isRecord(found) ? found : {};
}

/** Which basis to show. Consolidated is the product's answer (it is what banks
 *  underwrite); otherwise take whichever the filing actually carries. Returns
 *  null rather than inventing one — a number shown under the wrong basis is
 *  worse than no number. */
export function chooseBasis(reason: unknown): Basis | null {
  if (!isRecord(reason) || !isRecord(reason.bases)) return null;
  const names = Object.keys(reason.bases).sort();
  if (names.length === 0) return null;
  return names.includes("consolidated") ? "consolidated" : names[0];
}

/** Every fact, joined to the proof and the line item behind it.
 *
 *  A fact whose proof record is missing is KEPT with `method: ""`. Dropping it
 *  would hide a real number because its bookkeeping was incomplete. */
export function readFacts(
  reason: unknown,
  mapping: unknown,
  fog: unknown,
  basis: Basis,
): FogFact[] {
  const facts = region(reason, basis).facts;
  if (!isRecord(facts)) return [];

  const proofByTag = new Map<string, Record<string, unknown>>();
  for (const record of arr(region(mapping, basis).proof)) {
    if (isRecord(record) && typeof record.tag === "string") {
      if (!proofByTag.has(record.tag)) proofByTag.set(record.tag, record);
    }
  }

  const itemById = new Map<string, Record<string, unknown>>();
  for (const item of arr(region(fog, basis).line_items)) {
    if (isRecord(item) && typeof item.id === "string") itemById.set(item.id, item);
  }

  const out: FogFact[] = [];
  for (const [tag, raw] of Object.entries(facts)) {
    if (!isNumber(raw)) continue;
    const proof = proofByTag.get(tag);
    const item = proof && typeof proof.line_item === "string"
      ? itemById.get(proof.line_item)
      : undefined;
    out.push({
      tag,
      value: raw,
      method: proof && typeof proof.method === "string" ? proof.method : "",
      label: proof && typeof proof.label === "string" ? proof.label : "",
      page: item && isNumber(item.page) ? item.page : null,
    });
  }
  out.sort((a, b) => a.tag.localeCompare(b.tag));
  return out;
}
