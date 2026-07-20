"use client";

/** THE SIGNATURE.
 *
 *  A drawn cross-reference joining an extracted figure to the region of the
 *  page it was read from. In a printed annual report the reader does this join
 *  by hand — superscript, flip to the note, find the line. Here the join is
 *  made visible in one gesture.
 *
 *  It is the only orchestrated motion in the application: it draws once over
 *  240ms and then holds. Everything else on every screen is still, so this
 *  lands. */
export function Tether({
  from,
  to,
  container,
}: {
  from: DOMRect | null;
  to: DOMRect | null;
  container: DOMRect | null;
}) {
  if (!from || !to || !container) return null;

  const startX = to.right - container.left;
  const startY = to.top + to.height / 2 - container.top;
  const endX = from.left - container.left;
  const endY = from.top + from.height / 2 - container.top;

  // A flat S-curve: the control points sit at the horizontal midpoint so the
  // line leaves the page and arrives at the row travelling horizontally, the
  // way a leader line on a technical drawing does.
  const midX = (startX + endX) / 2;
  const path = `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`;

  return (
    <svg
      aria-hidden="true"
      className="tether-layer absolute inset-0 pointer-events-none"
      width={container.width}
      height={container.height}
      style={{ zIndex: 5 }}
    >
      <path
        d={path}
        fill="none"
        stroke="var(--tether)"
        strokeWidth={1.25}
        pathLength={1}
        strokeDasharray={1}
        style={{ animation: "tether-draw 240ms ease-out both" }}
      />
      <circle cx={startX} cy={startY} r={2.5} fill="var(--tether)" />
      <circle cx={endX} cy={endY} r={2.5} fill="var(--tether)" />
      <style>{`
        @keyframes tether-draw {
          from { stroke-dashoffset: 1; }
          to { stroke-dashoffset: 0; }
        }
      `}</style>
    </svg>
  );
}
