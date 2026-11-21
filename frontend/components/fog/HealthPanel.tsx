/** The health of one FOG run: footing, validation checks, coverage and
 *  mapping diagnostics, and whether the schema itself was usable.
 *
 *  Presentational only. Footing stays THREE-VALUED -- `passed: true` draws a
 *  tick, `passed: false` draws a flag, and `passed: null` ("could not check")
 *  draws neither glyph, only the words "not checked". Collapsing that third
 *  state into a failure would punish a correct fact for an absent check. A
 *  schema that yields zero usable concepts maps nothing while looking exactly
 *  like an honest empty filing, so it is the one diagnostic that shouts
 *  (`role="alert"`) instead of sitting quietly in the definition lists. */

import { Fragment } from "react";
import type { Health } from "@/lib/fog";
import { formatAmount } from "@/lib/format";

function kvValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return formatAmount(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function KvList({ record }: { record: Record<string, unknown> }) {
  const entries = Object.entries(record);
  if (entries.length === 0) {
    return (
      <p style={{ color: "var(--ink-soft)", fontSize: "var(--text-sm)" }}>None recorded.</p>
    );
  }
  return (
    <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "var(--sp-1) var(--sp-3)" }}>
      {entries.map(([key, value]) => (
        <Fragment key={key}>
          <dt style={{ color: "var(--ink-soft)", fontSize: "var(--text-xs)" }}>{key}</dt>
          <dd
            className="data"
            style={{ margin: 0, fontSize: "var(--text-xs)", textAlign: "right" }}
          >
            {kvValue(value)}
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}

export function HealthPanel({ health }: { health: Health }) {
  const { footing, checks, coverage, mapping, schemaUsable } = health;

  return (
    <div>
      {schemaUsable === false && (
        <p
          role="alert"
          style={{
            padding: "var(--sp-4)",
            margin: 0,
            fontWeight: 600,
            borderBottom: "3px double var(--ink)",
          }}
        >
          Schema is unusable — nothing in this basis can be mapped.
        </p>
      )}

      <section style={{ padding: "var(--sp-4)" }}>
        <h3 className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
          Footing
        </h3>
        {footing.length === 0 ? (
          <p style={{ color: "var(--ink-soft)", fontSize: "var(--text-sm)" }}>
            No footing statements.
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {footing.map((f) => (
              <li
                key={f.statement}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: "var(--sp-3)",
                  padding: "var(--sp-2) 0",
                  borderTop: "1px solid var(--rule-soft)",
                  fontWeight: f.passed === false ? 600 : 400,
                }}
              >
                <span>
                  <span className="data">{f.statement}</span>
                  {f.rule ? (
                    <span style={{ color: "var(--ink-soft)", fontSize: "var(--text-xs)" }}>
                      {" "}
                      {f.rule}
                    </span>
                  ) : null}
                </span>
                <span>
                  {f.passed === true ? "✓" : f.passed === false ? "⚑" : "not checked"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section style={{ padding: "var(--sp-4)" }}>
        <h3 className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
          Checks
        </h3>
        {checks.length === 0 ? (
          <p style={{ color: "var(--ink-soft)", fontSize: "var(--text-sm)" }}>
            No validation checks ran.
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {checks.map((check, i) => (
              <li key={i} style={{ padding: "var(--sp-2) 0", borderTop: "1px solid var(--rule-soft)" }}>
                <KvList record={check} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section style={{ padding: "var(--sp-4)" }}>
        <h3 className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
          Coverage
        </h3>
        <KvList record={coverage} />
      </section>

      <section style={{ padding: "var(--sp-4)" }}>
        <h3 className="eyebrow" style={{ marginBottom: "var(--sp-3)" }}>
          Mapping
        </h3>
        <KvList record={mapping} />
      </section>
    </div>
  );
}
