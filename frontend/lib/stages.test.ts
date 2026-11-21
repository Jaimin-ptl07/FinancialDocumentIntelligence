import { describe, expect, it } from "vitest";
import { STAGES, TERMINAL_STAGE, layerStages, summarizeStageCounts } from "./stages";

describe("layerStages", () => {
  it("puts ocr alone in the first layer — everything depends on it", () => {
    expect(layerStages()[0]).toEqual(["ocr"]);
  });

  it("places every stage after all of its dependencies", () => {
    const layers = layerStages();
    const layerOf = new Map<string, number>();
    layers.forEach((layer, i) => layer.forEach((name) => layerOf.set(name, i)));
    for (const stage of STAGES) {
      for (const dep of stage.deps) {
        expect(layerOf.get(stage.name)!).toBeGreaterThan(layerOf.get(dep)!);
      }
    }
  });

  it("lays out every stage exactly once", () => {
    const flat = layerStages().flat();
    expect(flat).toHaveLength(STAGES.length);
    expect(new Set(flat).size).toBe(STAGES.length);
  });
});

describe("TERMINAL_STAGE", () => {
  it("is a stage nothing else depends on", () => {
    const depended = new Set(STAGES.flatMap((s) => s.deps as readonly string[]));
    expect(depended.has(TERMINAL_STAGE)).toBe(false);
    expect(STAGES.some((s) => s.name === TERMINAL_STAGE)).toBe(true);
  });
});

const names = () => STAGES.map((s) => s.name);

describe("the FOG stages are on the graph", () => {
  it("includes every stage the backend runs", () => {
    for (const name of [
      "basis", "statement_index", "note_index",
      "fog_objects", "fog_mapping", "fog_reason",
    ]) {
      expect(names()).toContain(name);
    }
  });

  it("declares deps that all exist as stages", () => {
    // An unknown dep is treated as depth 0 by layerStages, which silently
    // draws a stage in the wrong column rather than failing.
    for (const stage of STAGES) {
      for (const dep of stage.deps) expect(names()).toContain(dep);
    }
  });

  it("waits for the graph, not the vector index", () => {
    expect(TERMINAL_STAGE).toBe("fog_reason");
  });

  it("puts fog_reason last in the layering", () => {
    const layers = layerStages();
    const depthOf = (n: string) => layers.findIndex((l) => l.includes(n));
    expect(depthOf("fog_reason")).toBeGreaterThan(depthOf("fog_mapping"));
    expect(depthOf("fog_mapping")).toBeGreaterThan(depthOf("fog_objects"));
    expect(depthOf("fog_objects")).toBeGreaterThan(depthOf("statement_index"));
    expect(depthOf("statement_index")).toBeGreaterThan(depthOf("basis"));
  });

  it("counts the new stages in the filings-index total", () => {
    expect(summarizeStageCounts({ done: 3 }).total).toBe(STAGES.length);
  });

  it("has no stage listed twice", () => {
    expect(new Set(names()).size).toBe(names().length);
  });
});
