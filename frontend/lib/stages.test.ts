import { describe, expect, it } from "vitest";
import { STAGES, TERMINAL_STAGE, layerStages } from "./stages";

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
