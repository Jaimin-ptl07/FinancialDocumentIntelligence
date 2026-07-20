"use client";

import { inkOpacity } from "@/lib/format";
import type { ExtractFieldView } from "@/lib/types";

const BANDS = [
  { lo: 0.0, hi: 0.2 },
  { lo: 0.2, hi: 0.4 },
  { lo: 0.4, hi: 0.6 },
  { lo: 0.6, hi: 0.8 },
  { lo: 0.8, hi: 1.01 },
];

/** A sequential single-hue distribution: every bar is ink, and each is drawn at
 *  the ink density of its own confidence band. The chart therefore says the
 *  same thing the field rows say — a low-confidence figure is a faint one — so
 *  an analyst reads the shape and the meaning in one glance instead of
 *  learning a separate legend. */
export function ConfidenceHistogram({ fields }: { fields: ExtractFieldView[] }) {
  const found = fields.filter((f) => f.value !== null && f.value !== undefined);
  const counts = BANDS.map(
    (band) =>
      found.filter((f) => f.confidence >= band.lo && f.confidence < band.hi)
        .length,
  );
  const peak = Math.max(1, ...counts);

  return (
    <figure style={{ margin: 0 }}>
      <figcaption className="eyebrow" style={{ marginBottom: "var(--sp-4)" }}>
        Confidence of the figures that were found
      </figcaption>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${BANDS.length}, 1fr)`,
          gap: "var(--sp-2)",
          alignItems: "end",
          height: "8rem",
        }}
      >
        {BANDS.map((band, index) => {
          const count = counts[index];
          const label = `${band.lo.toFixed(1)} to ${Math.min(band.hi, 1).toFixed(1)}: ${count} fields`;
          return (
            <div
              key={band.lo}
              title={label}
              aria-label={label}
              style={{ display: "flex", flexDirection: "column", justifyContent: "flex-end", height: "100%" }}
            >
              <span
                className="data"
                style={{
                  fontSize: "var(--text-xs)",
                  color: "var(--ink-soft)",
                  marginBottom: "var(--sp-1)",
                  textAlign: "center",
                }}
              >
                {count}
              </span>
              <div
                style={{
                  height: `${(count / peak) * 100}%`,
                  minHeight: count > 0 ? "3px" : "1px",
                  background: count > 0 ? "var(--ink)" : "var(--rule)",
                  opacity: count > 0 ? inkOpacity((band.lo + band.hi) / 2) : 1,
                  borderRadius: "4px 4px 0 0",
                }}
              />
            </div>
          );
        })}
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${BANDS.length}, 1fr)`,
          gap: "var(--sp-2)",
          borderTop: "1px solid var(--rule)",
          paddingTop: "var(--sp-2)",
          marginTop: "var(--sp-1)",
        }}
      >
        {BANDS.map((band) => (
          <span
            key={band.lo}
            className="data"
            style={{
              fontSize: "var(--text-2xs)",
              color: "var(--ink-faint)",
              textAlign: "center",
            }}
          >
            {band.lo.toFixed(1)}
          </span>
        ))}
      </div>
    </figure>
  );
}
