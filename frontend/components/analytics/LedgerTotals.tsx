/** The totals row from the foot of a statement, not a row of KPI cards. Big
 *  mono figures on a ruled strip, each one labelled beneath in the same way a
 *  balance sheet labels its subtotals. */
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
          }}
        >
          <dd
            className="data"
            style={{
              fontSize: "var(--text-xl)",
              lineHeight: 1.1,
              color:
                item.tone === "tick"
                  ? "var(--tick)"
                  : item.tone === "flag"
                    ? "var(--flag)"
                    : "var(--ink)",
            }}
          >
            {item.value}
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
