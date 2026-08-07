/** The two shapes of "look at this": tags multiple sources disagree about,
 *  and labels nothing mapped at all.
 *
 *  Presentational only. Contested tags are ordered widest-disagreement-first
 *  by `readContested` already; this just names the winner and the losers so
 *  the spread is legible rather than a bare number. Unmapped reason codes
 *  render as prose -- `model_call_failed` (a swallowed outage) must never
 *  read the same as `model_said_unmapped` (an honest refusal); an
 *  unrecognised code renders as-is rather than being hidden. */

import type { Contested, Unmapped } from "@/lib/fog";
import { formatInr } from "@/lib/format";

const REASON_PROSE: Record<string, string> = {
  model_call_failed: "model call failed",
  model_said_unmapped: "model declined to map",
  no_client: "no model configured",
  no_candidate: "no candidate concept",
  no_candidate_left: "no candidate concept",
};

function reasonProse(reason: string): string {
  return REASON_PROSE[reason] ?? reason;
}

function money(value: number | null): string {
  return value === null ? "—" : formatInr(value);
}

export function AttentionPanel({
  contested,
  unmapped,
}: {
  contested: Contested[];
  unmapped: Unmapped[];
}) {
  if (contested.length === 0 && unmapped.length === 0) {
    return (
      <p style={{ padding: "var(--sp-6)", color: "var(--ink-soft)", fontSize: "var(--text-sm)" }}>
        Nothing contested, nothing unmapped.
      </p>
    );
  }

  return (
    <div>
      {contested.length > 0 && (
        <section style={{ padding: "var(--sp-4)" }}>
          <h3 className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
            Contested
          </h3>
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {contested.map((c) => (
              <li
                key={c.tag}
                style={{ borderTop: "1px solid var(--rule-soft)", padding: "var(--sp-3) 0" }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: "var(--sp-3)" }}>
                  <span className="data" style={{ fontWeight: 600 }}>
                    {c.tag}
                  </span>
                  <span className="data" style={{ color: "var(--ink-soft)" }}>
                    spread {money(c.spread)}
                  </span>
                </div>
                <div style={{ fontSize: "var(--text-sm)", marginTop: "var(--sp-1)" }}>
                  <span style={{ fontWeight: 600 }}>{c.winnerLabel}</span>{" "}
                  <span className="data">{money(c.winnerValue)}</span>
                </div>
                {c.losers.length > 0 && (
                  <ul style={{ margin: "var(--sp-1) 0 0", padding: 0, listStyle: "none" }}>
                    {c.losers.map((l, i) => (
                      <li
                        key={`${c.tag}-${i}`}
                        style={{
                          fontSize: "var(--text-xs)",
                          color: "var(--ink-soft)",
                          display: "flex",
                          justifyContent: "space-between",
                          gap: "var(--sp-3)",
                        }}
                      >
                        <span>{l.label}</span>
                        <span className="data">{money(l.value)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {unmapped.length > 0 && (
        <section style={{ padding: "var(--sp-4)" }}>
          <h3 className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
            Unmapped
          </h3>
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {unmapped.map((u) => (
              <li
                key={u.reason}
                style={{ borderTop: "1px solid var(--rule-soft)", padding: "var(--sp-3) 0" }}
              >
                <div style={{ fontSize: "var(--text-sm)", fontWeight: 600 }}>
                  {reasonProse(u.reason)}
                </div>
                <ul style={{ margin: "var(--sp-1) 0 0", padding: 0, listStyle: "none" }}>
                  {/* Keyed by the row's own id, never its caption: two rows
                      can print one string, and 93 of TCS's 379 unmapped
                      records do. */}
                  {u.items.map((item) => (
                    <li
                      key={item.id}
                      style={{ fontSize: "var(--text-xs)", color: "var(--ink-soft)" }}
                    >
                      {item.label}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
