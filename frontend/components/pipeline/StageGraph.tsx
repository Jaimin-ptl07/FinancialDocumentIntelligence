import type { StageStatus, StageView } from "@/lib/types";
import { layerStages } from "@/lib/stages";

type Mark = StageStatus | "pending";

/** Failure is signalled by inversion — a struck chip — rather than by a hue.
 *  Red is spent entirely on provenance in this product, so an error cannot
 *  borrow it, and a stamped-through row is more legible at a glance than a
 *  third colour would be anyway. */
const MARKS: Record<Mark, { glyph: string; color: string; label: string }> = {
  done: { glyph: "✓", color: "var(--tick)", label: "done" },
  running: { glyph: "◐", color: "var(--flag)", label: "running" },
  queued: { glyph: "·", color: "var(--ink-faint)", label: "queued" },
  failed: { glyph: "✕", color: "var(--leaf)", label: "failed" },
  pending: { glyph: "·", color: "var(--ink-faint)", label: "not started" },
};

export function StageGraph({ stages }: { stages: StageView[] }) {
  const byName = new Map(stages.map((s) => [s.name, s]));
  const layers = layerStages();

  return (
    <ol
      className="flex overflow-x-auto"
      style={{ gap: "var(--sp-6)", paddingBottom: "var(--sp-2)" }}
    >
      {layers.map((layer, index) => (
        <li
          key={index}
          className="shrink-0"
          style={{
            paddingLeft: index === 0 ? 0 : "var(--sp-6)",
            borderLeft: index === 0 ? undefined : "1px solid var(--rule-soft)",
          }}
        >
          {/* Ordinals appear here and nowhere else in the app: the pipeline
              genuinely is an ordered dependency sequence, so the number
              carries information rather than decorating a list. */}
          <div className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
            {String(index + 1).padStart(2, "0")}
          </div>
          <ul style={{ display: "grid", gap: "var(--sp-1)" }}>
            {layer.map((name) => {
              const run = byName.get(name);
              const status: Mark = run?.status ?? "pending";
              const mark = MARKS[status];
              const struck = status === "failed";
              return (
                <li
                  key={name}
                  className="flex items-center"
                  style={{
                    gap: "var(--sp-2)",
                    padding: "var(--sp-1) var(--sp-2)",
                    borderRadius: "var(--radius-sm)",
                    background: struck ? "var(--ink)" : "transparent",
                  }}
                  title={run?.error ?? undefined}
                >
                  <span
                    className="data"
                    aria-hidden="true"
                    style={{ color: mark.color, fontSize: "var(--text-sm)" }}
                  >
                    {mark.glyph}
                  </span>
                  <span
                    className="data"
                    style={{
                      fontSize: "var(--text-xs)",
                      color: struck
                        ? "var(--leaf)"
                        : run
                          ? "var(--ink)"
                          : "var(--ink-faint)",
                    }}
                  >
                    {name}
                  </span>
                  <span className="sr-only">{mark.label}</span>
                </li>
              );
            })}
          </ul>
        </li>
      ))}
    </ol>
  );
}
