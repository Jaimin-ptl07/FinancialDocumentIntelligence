"use client";

import type { StageThroughputView } from "@/lib/types";

/** One row per stage. The bar is a single magnitude — how much of the corpus
 *  cleared this stage, nothing more — so an ink fill against the ruled track
 *  is already the correct read: it isn't distinguishing two states, only
 *  showing how far one value has got, and ink-on-rule is close to maximum
 *  contrast in both themes. Failures are the one place this row does need to
 *  distinguish a state, so that stays a reserved word-badge, never colour
 *  alone. */
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
                    role="img"
                    aria-label={`${stage.done} of ${total} documents cleared ${stage.name}`}
                    title={`${stage.done} of ${total} documents cleared ${stage.name}`}
                    style={{
                      flex: 1,
                      height: "6px",
                      background: "var(--rule)",
                      borderRadius: "var(--radius-sm)",
                      boxShadow: "inset 0 0 0 1px var(--rule-soft)",
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
                        transition: "width 200ms ease",
                      }}
                    />
                  </div>
                  <span
                    className="data"
                    style={{ fontSize: "var(--text-xs)", width: "5ch", color: "var(--ink-soft)" }}
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
