/** Topology transcribed from backend/src/fdi/pipeline/stages/*.py. `ping` is
 *  the walking-skeleton no-op and is deliberately excluded from the display
 *  graph — it tells an analyst nothing about their filing. */
export const STAGES = [
  { name: "ocr", deps: [] },
  { name: "normalize", deps: ["ocr"] },
  { name: "structure", deps: ["ocr"] },
  { name: "linking", deps: ["ocr"] },
  { name: "ast", deps: ["normalize", "ocr"] },
  { name: "semantic", deps: ["ast"] },
  { name: "ontology", deps: ["ast", "structure"] },
  { name: "graph", deps: ["ast", "structure"] },
  { name: "graph_load", deps: ["graph"] },
  { name: "provenance", deps: ["graph", "ocr"] },
  { name: "quality", deps: ["ast", "structure", "ontology"] },
  { name: "knowledge", deps: ["semantic", "ontology", "quality"] },
  { name: "chunks", deps: ["ast", "semantic", "provenance", "structure"] },
  { name: "embedding", deps: ["chunks"] },
  { name: "index", deps: ["embedding", "chunks"] },
  { name: "basis", deps: ["ast", "provenance"] },
  { name: "statement_index", deps: ["ocr", "structure", "basis"] },
  { name: "note_index", deps: ["ocr", "structure", "basis"] },
  { name: "fog_objects", deps: ["ocr", "statement_index", "note_index", "structure"] },
  { name: "fog_mapping", deps: ["fog_objects"] },
  { name: "fog_reason", deps: ["fog_mapping", "fog_objects"] },
] as const satisfies readonly { name: string; deps: readonly string[] }[];

/** The last stage to settle. When this reaches `done` the FINANCIAL OBJECT
 *  GRAPH is readable — facts, their proof, and the health of the run. It used
 *  to be `index`, which only meant the vector store had been written: a filing
 *  could reach it with nothing mapped at all. */
export const TERMINAL_STAGE = "fog_reason";

/** `GET /documents` reports each row's progress as a {status: count} map over
 *  the latest run per stage. Reduce it to the three numbers the filings index
 *  actually shows. `total` is the pipeline's own length, not the sum of the
 *  map: a filing that has only run OCR has one entry, and "1/1" would read as
 *  finished. */
export function summarizeStageCounts(counts: Record<string, number>): {
  done: number;
  failed: number;
  total: number;
} {
  return {
    done: counts.done ?? 0,
    failed: counts.failed ?? 0,
    total: STAGES.length,
  };
}

/** Longest-path layering: a stage sits one column right of its deepest
 *  dependency. Derived rather than hand-positioned, so adding a stage to
 *  STAGES cannot silently place it in a column that contradicts its deps. */
export function layerStages(): string[][] {
  const depth = new Map<string, number>();

  const depthOf = (name: string): number => {
    const cached = depth.get(name);
    if (cached !== undefined) return cached;
    const stage = STAGES.find((s) => s.name === name);
    const value =
      !stage || stage.deps.length === 0
        ? 0
        : 1 + Math.max(...stage.deps.map(depthOf));
    depth.set(name, value);
    return value;
  };

  const layers: string[][] = [];
  for (const stage of STAGES) {
    const d = depthOf(stage.name);
    (layers[d] ??= []).push(stage.name);
  }
  return layers;
}
