"use client";

/** The cross-reference mark, and the one motif the whole product is built on.
 *  In a printed annual report a figure carries a superscript pointing to the
 *  note that explains it; here it points to the page a machine read it from.
 *  Same gesture, made clickable. This is one of the few places --tether is
 *  allowed, because it is literally asserting "this came from there". */
export function RefMark({
  page,
  label,
  active,
  onClick,
}: {
  page: number;
  label: string;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={active}
      className="data"
      style={{
        alignSelf: "flex-start",
        fontSize: "var(--text-2xs)",
        lineHeight: 1,
        color: active ? "var(--leaf)" : "var(--tether)",
        background: active ? "var(--tether)" : "transparent",
        padding: "2px 3px",
        borderRadius: "var(--radius-sm)",
        textDecoration: active ? "none" : "underline",
        textUnderlineOffset: "2px",
        cursor: "pointer",
      }}
    >
      p{page}
    </button>
  );
}
