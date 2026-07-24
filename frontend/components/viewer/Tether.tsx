"use client";

/** THE SIGNATURE.
 *
 *  A drawn cross-reference joining an extracted figure to the region of the
 *  page it was read from. In a printed annual report the reader does this join
 *  by hand — superscript, flip to the note, find the line. Here the join is
 *  made visible in one gesture.
 *
 *  It is the only orchestrated motion in the application: it draws once over
 *  ~320ms and then holds. Everything else on every screen is still, so this
 *  lands.
 *
 *  Monochrome note: this used to be the one place a rust-red accent lived —
 *  the line read as "provenance" partly because it was the only colored mark
 *  on the page. --tether is now pure ink, so hue can no longer carry that
 *  signal on its own. Weight and form carry it instead: a heavier stroke than
 *  any rule elsewhere on the page, a soft ink shadow (an SVG filter, not a new
 *  color — it floods with var(--ink) at low opacity) that lifts the line off
 *  the page the way a raised seal lifts off paper, and a ringed terminus at
 *  each end rather than a flat dot, echoing the ringed highlight box in
 *  PdfCanvas. The destination ring gets one soft settle-pulse once the line
 *  lands, so the eye is told exactly where to look after the draw completes —
 *  still one gesture, just with a beginning, a middle, and a landing. */
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
      style={{ zIndex: 5, overflow: "visible" }}
    >
      <defs>
        {/* A soft ink shadow, not a glow of any hue — it reads as the line
            sitting slightly above the page rather than printed flat on it.
            Flooded with var(--ink) so it stays honestly monochrome rather
            than smuggling in a second color under a different name. */}
        <filter id="tether-lift" x="-60%" y="-60%" width="220%" height="220%">
          <feDropShadow
            dx="0"
            dy="0.5"
            stdDeviation="1.1"
            floodColor="var(--ink)"
            floodOpacity="0.35"
          />
        </filter>
      </defs>

      <g filter="url(#tether-lift)">
        <path
          d={path}
          fill="none"
          stroke="var(--tether)"
          strokeWidth={2}
          strokeLinecap="round"
          pathLength={1}
          strokeDasharray={1}
          style={{ animation: "tether-draw 320ms cubic-bezier(0.2, 0.7, 0.2, 1) both" }}
        />

        {/* Source terminus: a ringed mark over the highlight box, matching
            the ring-not-wash treatment now used on the box itself. */}
        <circle
          cx={startX}
          cy={startY}
          r={4}
          fill="none"
          stroke="var(--tether)"
          strokeWidth={1.5}
          style={{
            transformOrigin: `${startX}px ${startY}px`,
            animation: "tether-ring-in 260ms cubic-bezier(0.2, 0.7, 0.2, 1) both",
          }}
        />
        <circle cx={startX} cy={startY} r={1.5} fill="var(--tether)" />

        {/* Destination terminus: same ring, plus one settle pulse once the
            draw finishes, so the eye lands on the field row after following
            the line there. */}
        <circle
          cx={endX}
          cy={endY}
          r={4}
          fill="none"
          stroke="var(--tether)"
          strokeWidth={1.5}
          style={{
            transformOrigin: `${endX}px ${endY}px`,
            animation:
              "tether-ring-in 260ms cubic-bezier(0.2, 0.7, 0.2, 1) both, tether-pulse 640ms ease-out 320ms both",
          }}
        />
        <circle cx={endX} cy={endY} r={1.5} fill="var(--tether)" />
      </g>

      <style>{`
        @keyframes tether-draw {
          from { stroke-dashoffset: 1; }
          to { stroke-dashoffset: 0; }
        }
        @keyframes tether-ring-in {
          from { opacity: 0; transform: scale(0.3); }
          to { opacity: 1; transform: scale(1); }
        }
        @keyframes tether-pulse {
          0% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.35; transform: scale(2.1); }
          100% { opacity: 0; transform: scale(2.6); }
        }
      `}</style>
    </svg>
  );
}
