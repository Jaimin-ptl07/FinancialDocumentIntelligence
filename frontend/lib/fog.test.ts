import { describe, expect, it } from "vitest";
import { chooseBasis, readContested, readFacts, readHealth, readUnmapped } from "./fog";

// Shapes copied from out/fog-run-llm-p6b/TCS/*.json, trimmed.
const reason = {
  schema_version: 4,
  doc_id: "doc-1",
  bases: {
    consolidated: {
      facts: { PBTR: 65487, CT: 16388 },
      contested: [], checks: [], coverage: {}, composed: {}, summary: {},
    },
    standalone: { facts: { PBTR: 62806 }, contested: [], checks: [] },
  },
};
const mapping = {
  bases: {
    consolidated: {
      edges: [
        { type: "MAPS_TO", source: "li:a", target: "PBTR" },
        { type: "MAPS_TO", source: "li:b", target: "CT" },
      ],
      proof: [
        { line_item: "li:a", tag: "PBTR", label: "PROFIT BEFORE TAX",
          method: "lexical:section:name", score: 0.9, unit: "st" },
        { line_item: "li:b", tag: "CT", label: "Current tax",
          method: "llm", score: 0, unit: "nt" },
      ],
      unmapped: [],
    },
  },
};
const fog = {
  bases: {
    consolidated: {
      line_items: [
        { id: "li:a", label: "PROFIT BEFORE TAX", current: 65487, page: 167 },
        { id: "li:b", label: "Current tax", current: 16388, page: 167 },
      ],
      statements: [], notes: [], edges: [],
    },
  },
};

describe("chooseBasis", () => {
  it("prefers consolidated — it is what banks underwrite", () => {
    expect(chooseBasis(reason)).toBe("consolidated");
  });

  it("falls back to whatever the filing has", () => {
    expect(chooseBasis({ bases: { unknown: { facts: {} } } })).toBe("unknown");
  });

  it("returns null rather than inventing a basis", () => {
    expect(chooseBasis({ bases: {} })).toBeNull();
    expect(chooseBasis(null)).toBeNull();
    expect(chooseBasis("nonsense")).toBeNull();
  });
});

describe("readFacts", () => {
  it("joins a fact to the method that produced it", () => {
    const facts = readFacts(reason, mapping, fog, "consolidated");
    const pbtr = facts.find((f) => f.tag === "PBTR");
    expect(pbtr?.value).toBe(65487);
    expect(pbtr?.method).toBe("lexical:section:name");
    expect(pbtr?.label).toBe("PROFIT BEFORE TAX");
    expect(pbtr?.page).toBe(167);
  });

  it("shows the proof that WON arbitration, not the first claimant", () => {
    // Real TCS shape: five rows claim BB, `proof` is ordered by unit so the
    // balance-sheet winner is LAST, and the fact's value (6491) is the
    // winner's. Taking the first record labelled 6491 "Earmarked balances
    // with banks" on page 185 -- another row's caption and another row's
    // page, attached to a number belonging to neither.
    const contestedReason = {
      bases: {
        consolidated: {
          facts: { BB: 6491 },
          contested: [
            { tag: "BB", winner: "li:st:r19", winner_label: "Other balances with banks",
              winner_value: 6491, spread: 6194, losers: [] },
          ],
        },
      },
    };
    const contestedMapping = {
      bases: {
        consolidated: {
          proof: [
            { line_item: "li:nt:8d:r0", tag: "BB", label: "Earmarked balances with banks",
              method: "lexical:name", score: 0.9, unit: "nt:8(d)" },
            { line_item: "li:st:r19", tag: "BB", label: "Other balances with banks",
              method: "lexical:name", score: 0.9, unit: "st:balance_sheet" },
          ],
        },
      },
    };
    const contestedFog = {
      bases: {
        consolidated: {
          line_items: [
            { id: "li:nt:8d:r0", label: "Earmarked balances with banks", page: 185 },
            { id: "li:st:r19", label: "Other balances with banks", page: 166 },
          ],
        },
      },
    };
    const [bb] = readFacts(contestedReason, contestedMapping, contestedFog, "consolidated");
    expect(bb.label).toBe("Other balances with banks");
    expect(bb.page).toBe(166);
  });

  it("keeps the only claimant when a tag is uncontested", () => {
    const facts = readFacts(reason, mapping, fog, "consolidated");
    expect(facts.find((f) => f.tag === "CT")?.label).toBe("Current tax");
  });

  it("is sorted by tag so the list does not reshuffle between runs", () => {
    const tags = readFacts(reason, mapping, fog, "consolidated").map((f) => f.tag);
    expect(tags).toEqual([...tags].sort());
  });

  it("keeps a fact whose proof is missing rather than dropping the number", () => {
    const facts = readFacts(reason, { bases: { consolidated: {} } }, fog, "consolidated");
    expect(facts).toHaveLength(2);
    expect(facts[0].method).toBe("");
  });

  it("reads the basis it was asked for, not the first one", () => {
    const facts = readFacts(reason, mapping, fog, "standalone");
    expect(facts.map((f) => f.tag)).toEqual(["PBTR"]);
    expect(facts[0].value).toBe(62806);
  });

  it("never throws on a malformed artifact", () => {
    expect(readFacts(null, null, null, "consolidated")).toEqual([]);
    expect(readFacts({ bases: "no" }, {}, {}, "consolidated")).toEqual([]);
    expect(readFacts(reason, mapping, fog, "absent")).toEqual([]);
  });
});

const attention = {
  bases: {
    consolidated: {
      facts: {},
      contested: [
        { tag: "SMALL", claimants: 2, spread: 0,
          winner: "li:1", winner_label: "Agrees", winner_value: 11027,
          losers: [{ label: "Also agrees", value: 11027 }] },
        { tag: "BIG", claimants: 3, spread: 11136,
          winner: "li:2", winner_label: "Right-of-use assets", winner_value: 11027,
          losers: [{ label: "Payment for acquiring right-of-use assets", value: -109 }] },
      ],
      checks: [{ rule_id: "identity_balance_sheet", severity: "warn", passed: false, delta: 10 }],
      coverage: { totals: { expected: 4, found_extracted: 3 } },
    },
  },
};

describe("readContested", () => {
  it("puts the widest disagreement first — that is where a human should look", () => {
    expect(readContested(attention, "consolidated").map((c) => c.tag)).toEqual(["BIG", "SMALL"]);
  });

  it("carries the losers, because a winner alone cannot be judged", () => {
    const big = readContested(attention, "consolidated")[0];
    expect(big.winnerLabel).toBe("Right-of-use assets");
    expect(big.losers[0].value).toBe(-109);
  });

  it("never throws", () => {
    expect(readContested(null, "consolidated")).toEqual([]);
    expect(readContested({ bases: { consolidated: { contested: "no" } } }, "consolidated")).toEqual([]);
  });
});

describe("readUnmapped", () => {
  const mapping = {
    bases: {
      consolidated: {
        unmapped: [
          { label: "Facility expenses", reason: "model_said_unmapped" },
          { label: "Travel", reason: "model_call_failed" },
          { label: "Other", reason: "model_said_unmapped" },
        ],
      },
    },
  };

  it("groups by reason so an outage never reads as an honest refusal", () => {
    const groups = readUnmapped(mapping, "consolidated");
    const failed = groups.find((g) => g.reason === "model_call_failed");
    const refused = groups.find((g) => g.reason === "model_said_unmapped");
    expect(failed?.labels).toEqual(["Travel"]);
    expect(refused?.labels).toEqual(["Facility expenses", "Other"]);
  });

  it("never throws", () => {
    expect(readUnmapped(null, "consolidated")).toEqual([]);
  });
});

describe("readHealth", () => {
  const fog = {
    bases: {
      consolidated: {
        statements: [
          { statement: "profit_and_loss",
            footing: { passed: true, checked: true, rule: "income - expenses = pbt" } },
          { statement: "cash_flow", footing: { passed: null, checked: false, rule: "" } },
        ],
        line_items: [], notes: [], edges: [],
      },
    },
  };
  const mapping = {
    schema: { usable: true, face_concepts: 577 },
    bases: { consolidated: { unmapped: [] } },
    diagnostics: { consolidated: { by_lexical: 375, by_llm: 84, llm_calls: 35 } },
  };

  it("keeps could-not-check distinct from failed", () => {
    const footing = readHealth(attention, mapping, fog, "consolidated").footing;
    expect(footing.find((f) => f.statement === "profit_and_loss")?.passed).toBe(true);
    expect(footing.find((f) => f.statement === "cash_flow")?.passed).toBeNull();
  });

  it("surfaces schema usability — an unusable schema maps nothing, silently", () => {
    expect(readHealth(attention, mapping, fog, "consolidated").schemaUsable).toBe(true);
    expect(readHealth(attention, { schema: { usable: false } }, fog, "consolidated").schemaUsable)
      .toBe(false);
  });

  it("carries the mapping diagnostics for the basis shown", () => {
    expect(readHealth(attention, mapping, fog, "consolidated").mapping.llm_calls).toBe(35);
  });

  it("never throws", () => {
    const empty = readHealth(null, null, null, "consolidated");
    expect(empty.footing).toEqual([]);
    expect(empty.schemaUsable).toBeNull();
  });
});

describe("FogFact.score", () => {
  it("carries the score, because method alone cannot rank the evidence", () => {
    // `lexical_match` returns "name" for BOTH an exact hit (1.0) and a token
    // overlap (<= 0.9); only the score separates them.
    const facts = readFacts(reason, mapping, fog, "consolidated");
    expect(facts.find((f) => f.tag === "PBTR")?.score).toBe(0.9);
    expect(facts.find((f) => f.tag === "CT")?.score).toBe(0);
  });

  it("is 0 when no proof record exists, not undefined", () => {
    const facts = readFacts(reason, { bases: { consolidated: {} } }, fog, "consolidated");
    expect(facts[0].score).toBe(0);
  });
});
