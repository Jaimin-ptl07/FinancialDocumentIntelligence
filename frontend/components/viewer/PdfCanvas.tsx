"use client";

import { useEffect, useRef, useState } from "react";
import type { BoundingRegion } from "@/lib/types";

const MIN_SCALE = 0.6;
const MAX_SCALE = 3;
const POINTS_PER_INCH = 72;

/** `pdfjs-dist` is driven directly rather than through `react-pdf` because the
 *  overlay has to share the canvas's exact coordinate space — that join is the
 *  whole point of this screen, and a wrapper that owns its own layout takes it
 *  away. */
export function PdfCanvas({
  url,
  page,
  regions,
  onPageCount,
}: {
  url: string;
  page: number;
  regions: BoundingRegion[] | null;
  onPageCount?: (count: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [scale, setScale] = useState(1);

  // Fit-to-width, not a fixed SCALE: a fixed scale leaves dead space beside a
  // narrow pane and forces both horizontal AND vertical scrolling on a wide
  // one, exactly the "user has to scroll" complaint this replaces. Re-measured
  // on resize (the aside collapsing/expanding changes the pane's width too).
  useEffect(() => {
    const el = wrapperRef.current?.parentElement;
    if (!el) return;
    const measure = () => {
      const width = el.clientWidth;
      if (width > 0) {
        setScale(Math.min(MAX_SCALE, Math.max(MIN_SCALE, width / (8.27 * POINTS_PER_INCH))));
      }
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let renderTask: { cancel: () => void } | null = null;

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();

        const doc = await pdfjs.getDocument(url).promise;
        if (cancelled) return;
        onPageCount?.(doc.numPages);

        const target = Math.min(Math.max(1, page), doc.numPages);
        const rendered = await doc.getPage(target);
        if (cancelled) return;

        const viewport = rendered.getViewport({ scale });
        const canvas = canvasRef.current;
        const context = canvas?.getContext("2d");
        if (!canvas || !context || cancelled) return;

        canvas.width = viewport.width;
        canvas.height = viewport.height;

        const task = rendered.render({ canvasContext: context, viewport });
        renderTask = task;
        await task.promise;
        if (!cancelled) {
          setReady(true);
          setError(null);
        }
      } catch (cause) {
        // A cancelled render is the expected outcome of changing page quickly;
        // it is not something to show the analyst.
        const message = (cause as Error).message ?? String(cause);
        if (cancelled || /cancel/i.test(message)) return;
        // pdfjs reports a 404 as `Missing PDF "<url>"`, which tells an analyst
        // nothing they can act on. Say what it means instead.
        setError(
          /missing pdf|unexpected server response \(404\)/i.test(message)
            ? "No source PDF is stored for this filing."
            : message,
        );
      }
    })();

    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [url, page, onPageCount, scale]);

  useEffect(() => {
    highlightRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [regions, ready]);

  const onPage = (regions ?? []).filter((r) => r.page_number === page);

  if (error) {
    return (
      <p style={{ padding: "var(--sp-6)", fontSize: "var(--text-sm)" }}>
        This page could not be rendered: {error}
      </p>
    );
  }

  return (
    <div
      ref={wrapperRef}
      className="relative inline-block"
      style={{ boxShadow: "var(--shadow-md)" }}
    >
      <canvas ref={canvasRef} className="block" />
      {onPage.map((region, index) => {
        const box = boxOf(region.polygon);
        return (
          // A double ring rather than one flat-bordered wash: now that the
          // mark is pure ink rather than a rust accent, a single 1.5px line
          // reads as just another rule on the page. Two concentric rings —
          // a tight inner line plus an offset outer line — plus the wash
          // between them reads as a deliberately placed mark (a rubber
          // stamp's double ruling, a certified-copy seal) rather than a
          // color swatch. The whole mark settles in with a brief scale/fade
          // rather than snapping straight to full size.
          <div
            key={index}
            ref={index === 0 ? highlightRef : undefined}
            aria-hidden="true"
            className="absolute pointer-events-none tether-highlight"
            style={{
              left: box.x * scale * POINTS_PER_INCH - 3,
              top: box.y * scale * POINTS_PER_INCH - 3,
              width: box.width * scale * POINTS_PER_INCH + 6,
              height: box.height * scale * POINTS_PER_INCH + 6,
              padding: 3,
              border: "1px solid var(--pdf-mark)",
              borderRadius: "var(--radius-sm)",
              boxShadow: "0 1px 4px color-mix(in srgb, var(--pdf-mark) 18%, transparent)",
            }}
          >
            <div
              style={{
                width: "100%",
                height: "100%",
                border: "2px solid var(--pdf-mark)",
                background: "var(--pdf-mark-wash)",
                borderRadius: "var(--radius-sm)",
              }}
            />
          </div>
        );
      })}
      <style>{`
        .tether-highlight {
          animation: tether-highlight-in 220ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
          transform-origin: center;
        }
        @keyframes tether-highlight-in {
          from { opacity: 0; transform: scale(0.96); }
          to { opacity: 1; transform: scale(1); }
        }
        @media (prefers-reduced-motion: reduce) {
          .tether-highlight {
            animation: none;
          }
        }
      `}</style>
    </div>
  );
}

/** Azure DI polygons are [x1,y1,x2,y2,x3,y3,x4,y4] in INCHES. The four corners
 *  reduce to an axis-aligned box: the quads are near-rectangular for text lines
 *  and table cells, and a rotated overlay would be harder to read than a
 *  slightly loose one. */
function boxOf(polygon: number[]) {
  const xs = polygon.filter((_, i) => i % 2 === 0);
  const ys = polygon.filter((_, i) => i % 2 === 1);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}
