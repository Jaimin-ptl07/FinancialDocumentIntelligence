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
  /** The match score behind `method`. Carried because METHOD ALONE CANNOT
   *  RANK the evidence: `lexical_match` returns "name" for both an exact hit
   *  (1.0) and a token-overlap guess (<= 0.9), and only the score separates
   *  them. Without it a UI mirroring `core/fog_reason.py::_tier` has to treat
   *  every bare lexical match as the weakest evidence, which mis-ranks every
   *  exact one. 0 when no proof record exists. */
  score: number;
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
 *  would hide a real number because its bookkeeping was incomplete.
 *
 *  The proof shown is the one `fog_reason` ARBITRATED IN, not the first record
 *  carrying the tag. More than one row claims a tag for 80 of TCS's 153
 *  consolidated facts, and `fog_mapping.proof` is ordered by unit, so the first
 *  record is routinely a LOSER: BB's value is the balance-sheet row "Other
 *  balances with banks" (6,491) while the first claimant is note 8(d)'s
 *  "Earmarked balances with banks". Taking the first labelled the number with a
 *  caption that is not its own and sent the page jump to the wrong note --
 *  showing the value's provenance as some other value's. */
export function readFacts(
  reason: unknown,
  mapping: unknown,
  fog: unknown,
  basis: Basis,
): FogFact[] {
  const facts = region(reason, basis).facts;
  if (!isRecord(facts)) return [];

  const winnerByTag = new Map<string, string>();
  for (const raw of arr(region(reason, basis).contested)) {
    if (isRecord(raw) && typeof raw.tag === "string" && typeof raw.winner === "string") {
      winnerByTag.set(raw.tag, raw.winner);
    }
  }

  const proofByTag = new Map<string, Record<string, unknown>>();
  for (const record of arr(region(mapping, basis).proof)) {
    if (!isRecord(record) || typeof record.tag !== "string") continue;
    const winner = winnerByTag.get(record.tag);
    // Keep the first record UNLESS a later one is the arbitration winner. An
    // uncontested tag has no winner entry and keeps its only claimant.
    if (!proofByTag.has(record.tag) || (winner && record.line_item === winner)) {
      proofByTag.set(record.tag, record);
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
      score: proof && isNumber(proof.score) ? proof.score : 0,
      label: proof && typeof proof.label === "string" ? proof.label : "",
      page: item && isNumber(item.page) ? item.page : null,
    });
  }
  out.sort((a, b) => a.tag.localeCompare(b.tag));
  return out;
}

export type Contested = {
  tag: string;
  /** Highest claim minus lowest. 0 means the sources corroborate; large means
   *  one of them is wrong. Null when no claimant carried a value. */
  spread: number | null;
  winnerLabel: string;
  winnerValue: number | null;
  losers: { label: string; value: number | null }[];
};

export type Unmapped = { reason: string; labels: string[] };

export type Health = {
  footing: { statement: string; passed: boolean | null; rule: string }[];
  checks: Record<string, unknown>[];
  coverage: Record<string, unknown>;
  mapping: Record<string, unknown>;
  schemaUsable: boolean | null;
};

const num = (v: unknown): number | null => (isNumber(v) ? v : null);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** Contested tags, WIDEST DISAGREEMENT FIRST.
 *
 *  Corroboration and mis-mapping are indistinguishable from a fact map alone:
 *  a face row and its note agreeing on 11,027 looks exactly like a face row
 *  losing 11,027 to an unrelated -109. The spread is what separates them, so it
 *  is what the list is ordered by. */
export function readContested(reason: unknown, basis: Basis): Contested[] {
  const out: Contested[] = [];
  for (const raw of arr(region(reason, basis).contested)) {
    if (!isRecord(raw) || typeof raw.tag !== "string") continue;
    out.push({
      tag: raw.tag,
      spread: num(raw.spread),
      winnerLabel: str(raw.winner_label),
      winnerValue: num(raw.winner_value),
      losers: arr(raw.losers)
        .filter(isRecord)
        .map((l) => ({ label: str(l.label), value: num(l.value) })),
    });
  }
  out.sort((a, b) => (b.spread ?? -1) - (a.spread ?? -1) || a.tag.localeCompare(b.tag));
  return out;
}

/** Unmapped labels GROUPED BY REASON.
 *
 *  The grouping is the point. "model_call_failed" is a swallowed outage and
 *  "model_said_unmapped" is the model honestly declining to guess; before the
 *  backend recorded a reason these produced byte-identical output, and a dead
 *  endpoint read exactly like a clean refusal. */
export function readUnmapped(mapping: unknown, basis: Basis): Unmapped[] {
  const groups = new Map<string, string[]>();
  for (const raw of arr(region(mapping, basis).unmapped)) {
    if (!isRecord(raw)) continue;
    const reason = str(raw.reason) || "unknown";
    const label = str(raw.label);
    if (!label) continue;
    const bucket = groups.get(reason);
    if (bucket) bucket.push(label);
    else groups.set(reason, [label]);
  }
  return [...groups.entries()]
    .map(([reason, labels]) => ({ reason, labels: labels.sort() }))
    .sort((a, b) => a.reason.localeCompare(b.reason));
}

/** Footing verdicts, validation checks, coverage, mapping diagnostics and
 *  schema usability for one basis.
 *
 *  `passed` stays THREE-VALUED. `checked: false` means a needed line was absent
 *  or the statement has no rule, which is not a failure and must not render as
 *  one -- discarding a correct face because it labels one line unusually is
 *  worse than not checking it. */
export function readHealth(
  reason: unknown,
  mapping: unknown,
  fog: unknown,
  basis: Basis,
): Health {
  const footing: Health["footing"] = [];
  for (const raw of arr(region(fog, basis).statements)) {
    if (!isRecord(raw)) continue;
    const f = isRecord(raw.footing) ? raw.footing : {};
    footing.push({
      statement: str(raw.statement),
      passed: f.checked === true && typeof f.passed === "boolean" ? f.passed : null,
      rule: str(f.rule),
    });
  }
  footing.sort((a, b) => a.statement.localeCompare(b.statement));

  const schema = isRecord(mapping) && isRecord(mapping.schema) ? mapping.schema : {};
  const diagnostics =
    isRecord(mapping) && isRecord(mapping.diagnostics) ? mapping.diagnostics : {};
  const forBasis = isRecord(diagnostics[basis]) ? diagnostics[basis] : {};
  const coverage = region(reason, basis).coverage;

  return {
    footing,
    checks: arr(region(reason, basis).checks).filter(isRecord),
    coverage: isRecord(coverage) ? coverage : {},
    mapping: forBasis as Record<string, unknown>,
    schemaUsable: typeof schema.usable === "boolean" ? schema.usable : null,
  };
}
