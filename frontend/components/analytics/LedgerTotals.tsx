/** The totals row from the foot of a statement, not a row of KPI cards. Big
 *  mono figures on a ruled strip, each one labelled beneath in the same way a
 *  balance sheet labels its subtotals.
 *
 *  --tick and --flag are both pure ink now (the monochrome system has no
 *  second hue to spend on "good" vs "needs attention"), so tone is carried by
 *  three ink-only compensations instead of colour:
 *   - a top rule on the cell — single hairline for "tick" (one clean line,
 *     like a subtotal ruled once), a doubled rule for "flag" (the ledger's own
 *     mark for a total that needs a second look, ruled twice)
 *   - a small glyph set before the figure (✓ / !), outside the tabular-mono
 *     value itself so `tabular-nums` keeps governing only the digits
 *   - a weight bump on "flag" so it also reads heavier at a glance, not only
 *     differently ruled */
export function LedgerTotals({
  items,
}: {
  items: { label: string; value: string; note?: string; tone?: "tick" | "flag" }[];
}) {
  return (
    <dl
      className="sheet"
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fit, minmax(11rem, 1fr))`,
        overflow: "hidden",
      }}
    >
      {items.map((item, index) => (
        <div
          key={item.label}
          style={{
            padding: "var(--sp-5) var(--sp-6)",
            borderLeft: index === 0 ? undefined : "1px solid var(--rule-soft)",
            borderTop:
              item.tone === "tick"
                ? "1px solid var(--ink)"
                : item.tone === "flag"
                  ? "3px double var(--ink)"
                  : "1px solid transparent",
          }}
        >
          <dd
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: "var(--sp-1)",
              fontSize: "var(--text-xl)",
              lineHeight: 1.1,
              color: "var(--ink)",
            }}
          >
            {item.tone ? (
              <span
                aria-hidden="true"
                style={{
                  fontSize: "var(--text-md)",
                  fontWeight: 700,
                }}
              >
                {item.tone === "tick" ? "✓" : "!"}
              </span>
            ) : null}
            <span
              className="data"
              style={{ fontWeight: item.tone === "flag" ? 700 : 400 }}
            >
              {item.value}
            </span>
            <span className="sr-only">
              {item.tone === "tick"
                ? " — verified"
                : item.tone === "flag"
                  ? " — needs attention"
                  : ""}
            </span>
          </dd>
          <dt className="eyebrow" style={{ marginTop: "var(--sp-2)" }}>
            {item.label}
          </dt>
          {item.note ? (
            <p
              style={{
                marginTop: "var(--sp-1)",
                fontSize: "var(--text-xs)",
                color: "var(--ink-faint)",
              }}
            >
              {item.note}
            </p>
          ) : null}
        </div>
      ))}
    </dl>
  );
}
