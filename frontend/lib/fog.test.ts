import { describe, expect, it } from "vitest";
import { chooseBasis, readFacts } from "./fog";

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
