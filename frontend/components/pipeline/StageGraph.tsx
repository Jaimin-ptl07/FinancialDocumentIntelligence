import type { StageStatus, StageView } from "@/lib/types";
import { layerStages } from "@/lib/stages";

type Mark = StageStatus | "pending";

/** Failure is signalled by inversion — a struck chip — rather than by a hue.
 *  Red is spent entirely on provenance in this product, so an error cannot
 *  borrow it, and a stamped-through row is more legible at a glance than a
 *  third colour would be anyway.
 *
 *  done / running / queued used to differ ONLY by hue (tick green, flag
 *  ochre, ink-faint grey) and now all resolve to the same ink. The
 *  compensating axis is glyph shape, not colour or weight:
 *    - done    a filled disc   — settled
 *    - running a half disc     — the animated wash below is what actually
 *                                 says "in progress"
 *    - queued  a small centred dot — faint, hasn't started
 *  Bold weight is deliberately NOT used for "done": it is reserved app-wide
 *  (LedgerTotals, Value, FieldList) to mean "needs attention," and reusing it
 *  here for the opposite state — settled, no action needed — would make the
 *  same visual cue mean opposite things depending which screen a user reads
 *  it on. Glyph + the running-row wash below carry the whole distinction.
 *  A faint dot and a faint half-disc would otherwise both read as merely
 *  "quiet," so running additionally gets a slow ink-wash pulse on the row
 *  background — motion, not a new hue or a repurposed weight — and it
 *  respects prefers-reduced-motion via the global override in globals.css. */
const MARKS: Record<Mark, { glyph: string; label: string }> = {
  done: { glyph: "●", label: "done" },
  running: { glyph: "◐", label: "running" },
  queued: { glyph: "·", label: "queued" },
  failed: { glyph: "✕", label: "failed" },
  pending: { glyph: "·", label: "not started" },
};

export function StageGraph({ stages }: { stages: StageView[] }) {
  const byName = new Map(stages.map((s) => [s.name, s]));
  const layers = layerStages();

  return (
    <>
      {/* Scoped rather than in globals.css: this animation belongs to one
          component's "in progress" row, not the design system. The global
          prefers-reduced-motion override (globals.css) forces
          animation-duration to 0.01ms, so reduced-motion users get a static
          faint wash instead of a pulse — no local media query needed. */}
      <style>{`
        @keyframes stage-running-wash {
          0%, 100% { background-color: color-mix(in srgb, var(--ink) 4%, transparent); }
          50% { background-color: color-mix(in srgb, var(--ink) 9%, transparent); }
        }
        .stage-running {
          animation: stage-running-wash 2200ms ease-in-out infinite;
        }
        .stage-glyph {
          display: inline-block;
          transition: transform 200ms ease, opacity 200ms ease;
        }
        .stage-done .stage-glyph {
          transform: scale(1.08);
        }
      `}</style>
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
                const running = status === "running";
                return (
                  <li
                    key={name}
                    className={`flex items-center${running ? " stage-running" : ""}${
                      status === "done" ? " stage-done" : ""
                    }`}
                    style={{
                      gap: "var(--sp-2)",
                      padding: "var(--sp-1) var(--sp-2)",
                      borderRadius: "var(--radius-sm)",
                      background: struck ? "var(--ink)" : undefined,
                    }}
                    title={run?.error ?? undefined}
                  >
                    <span
                      className="data stage-glyph"
                      aria-hidden="true"
                      style={{
                        color: struck ? "var(--leaf)" : "var(--ink)",
                        fontSize: "var(--text-sm)",
                        opacity: status === "queued" || status === "pending" ? 0.55 : 1,
                      }}
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
    </>
  );
}
