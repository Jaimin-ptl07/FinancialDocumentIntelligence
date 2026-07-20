"use client";

import type { StageThroughputView } from "@/lib/types";

/** One row per stage. The bar is a single magnitude — how much of the corpus
 *  cleared this stage — so it needs no legend; failures are called out as a
 *  reserved status beside it, with a word, never colour alone. */
export function StageThroughput({ stages }: { stages: StageThroughputView[] }) {
  return (
    <table style={{ width: "100%", borderCollapse: "collapse" }}>
      <caption className="sr-only">
        Pipeline stages, with how many documents cleared each one
      </caption>
      <thead>
        <tr>
          <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) 0" }}>
            Stage
          </th>
          <th className="eyebrow" style={{ textAlign: "left", padding: "var(--sp-2) var(--sp-4)" }}>
            Cleared
          </th>
          <th className="eyebrow" style={{ textAlign: "right", padding: "var(--sp-2) 0", width: "8rem" }}>
            Median
          </th>
        </tr>
      </thead>
      <tbody>
        {stages.map((stage) => {
          const total =
            stage.done + stage.running + stage.queued + stage.failed;
          const fraction = total === 0 ? 0 : stage.done / total;
          return (
            <tr key={stage.name} style={{ borderTop: "1px solid var(--rule-soft)" }}>
              <td
                className="data"
                style={{
                  padding: "var(--sp-3) 0",
                  fontSize: "var(--text-xs)",
                  width: "9rem",
                }}
              >
                {stage.name}
              </td>
              <td style={{ padding: "var(--sp-3) var(--sp-4)" }}>
                <div
                  className="flex items-center"
                  style={{ gap: "var(--sp-3)" }}
                >
                  <div
                    title={`${stage.done} of ${total} documents cleared ${stage.name}`}
                    style={{
                      flex: 1,
                      height: "6px",
                      background: "var(--rule)",
                      borderRadius: "3px",
                      overflow: "hidden",
                      minWidth: "4rem",
                    }}
                  >
                    <div
                      style={{
                        width: `${fraction * 100}%`,
                        height: "100%",
                        background: "var(--tick)",
                        borderRadius: "3px",
                      }}
                    />
                  </div>
                  <span
                    className="data"
                    style={{ fontSize: "var(--text-xs)", width: "5ch" }}
                  >
                    {stage.done}/{total}
                  </span>
                  {stage.failed > 0 ? (
                    <span
                      className="data"
                      style={{
                        fontSize: "var(--text-2xs)",
                        padding: "1px var(--sp-2)",
                        borderRadius: "var(--radius-sm)",
                        background: "var(--ink)",
                        color: "var(--leaf)",
                        whiteSpace: "nowrap",
                      }}
                    >
                      ✕ {stage.failed} failed
                    </span>
                  ) : null}
                </div>
              </td>
              <td
                className="data"
                style={{
                  padding: "var(--sp-3) 0",
                  fontSize: "var(--text-xs)",
                  color: "var(--ink-soft)",
                  textAlign: "right",
                }}
              >
                {stage.median_seconds === null
                  ? "—"
                  : `${stage.median_seconds.toFixed(1)}s`}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
