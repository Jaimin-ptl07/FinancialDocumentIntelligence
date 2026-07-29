export function PageHeader({
  eyebrow,
  title,
  lede,
  children,
}: {
  eyebrow?: string;
  title: string;
  lede?: string;
  children?: React.ReactNode;
}) {
  return (
    <header
      className="flex items-end justify-between"
      style={{
        gap: "var(--sp-6)",
        padding: "var(--sp-8) var(--sp-8) var(--sp-6)",
        background: "var(--leaf)",
        borderBottom: "var(--hairline)",
        boxShadow: "var(--shadow-sm)",
      }}
    >
      <div style={{ minWidth: 0 }}>
        {eyebrow ? (
          <div className="eyebrow" style={{ marginBottom: "var(--sp-2)" }}>
            {eyebrow}
          </div>
        ) : null}
        <h1 className="display" style={{ fontSize: "var(--text-xl)" }}>
          {title}
        </h1>
        {lede ? (
          <p
            style={{
              marginTop: "var(--sp-2)",
              color: "var(--ink-soft)",
              fontSize: "var(--text-sm)",
              maxWidth: "56ch",
            }}
          >
            {lede}
          </p>
        ) : null}
      </div>
      {children ? (
        <div
          className="flex items-center shrink-0"
          style={{ gap: "var(--sp-3)" }}
        >
          {children}
        </div>
      ) : null}
    </header>
  );
}
