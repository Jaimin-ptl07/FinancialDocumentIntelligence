"use client";

/** The cross-reference mark, and the one motif the whole product is built on.
 *  In a printed annual report a figure carries a superscript pointing to the
 *  note that explains it; here it points to the page a machine read it from.
 *  Same gesture, made clickable. This is one of the few places --tether is
 *  allowed, because it is literally asserting "this came from there".
 *
 *  Inactive vs active used to be carried entirely by --tether color (ink text
 *  on transparent vs ink fill); now that the whole app is monochrome, that
 *  color-in/color-out toggle is really an ink-in/ink-out toggle, which reads
 *  fine, but on its own it is a subtle one at 11px. Two things reinforce it:
 *  the inactive state gets an actual outline (not just underlined text) so it
 *  reads as a chip rather than a link, and both states carry a small caret
 *  that points into the chip — a leader stub, echoing the tether line this
 *  button draws when clicked. */
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
      aria-current={active ? "true" : undefined}
      className="data"
      style={{
        alignSelf: "flex-start",
        display: "inline-flex",
        alignItems: "center",
        gap: "1px",
        fontSize: "var(--text-2xs)",
        fontWeight: active ? 600 : 500,
        lineHeight: 1,
        color: active ? "var(--leaf)" : "var(--tether)",
        background: active ? "var(--tether)" : "transparent",
        border: active ? "1px solid var(--tether)" : "1px solid var(--rule)",
        padding: "2px 4px",
        borderRadius: "var(--radius-sm)",
        textDecoration: active ? "none" : "underline",
        textUnderlineOffset: "2px",
        cursor: "pointer",
        transition: "background-color 120ms ease, border-color 120ms ease",
      }}
    >
      <span aria-hidden="true" style={{ fontSize: "0.85em", opacity: active ? 1 : 0.6 }}>
        &raquo;
      </span>
      p{page}
    </button>
  );
}
