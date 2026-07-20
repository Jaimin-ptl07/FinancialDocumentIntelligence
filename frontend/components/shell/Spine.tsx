"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/** Destinations are named by what the analyst does there, not by the noun the
 *  system stores. "Add a filing", not "Upload". */
const DESTINATIONS = [
  { href: "/", label: "Filings" },
  { href: "/ingest", label: "Add a filing" },
  { href: "/schema", label: "Extraction schema" },
  { href: "/analytics", label: "Quality" },
] as const;

export function Spine() {
  const pathname = usePathname();

  return (
    <nav aria-label="Sections" className="spine">
      <div className="spine-brand">
        <div className="eyebrow">Financial document</div>
        <div
          className="display"
          style={{ fontSize: "var(--text-md)", marginTop: "var(--sp-1)" }}
        >
          Intelligence
        </div>
      </div>

      <ul className="spine-nav">
        {DESTINATIONS.map((d) => {
          const active =
            d.href === "/" ? pathname === "/" : pathname.startsWith(d.href);
          return (
            <li key={d.href}>
              <Link
                href={d.href}
                aria-current={active ? "page" : undefined}
                style={{
                  display: "block",
                  whiteSpace: "nowrap",
                  padding: "var(--sp-2) var(--sp-3)",
                  marginBottom: "2px",
                  borderRadius: "var(--radius-sm)",
                  background: active ? "var(--leaf-2)" : "transparent",
                  boxShadow: active ? "inset 2px 0 0 0 var(--ink)" : undefined,
                  fontSize: "var(--text-sm)",
                  fontWeight: active ? 600 : 400,
                  color: active ? "var(--ink)" : "var(--ink-soft)",
                }}
              >
                {d.label}
              </Link>
            </li>
          );
        })}
      </ul>

      <div className="spine-foot">
        <p
          style={{
            fontSize: "var(--text-xs)",
            color: "var(--ink-faint)",
            lineHeight: 1.5,
          }}
        >
          Every figure carries a reference back to the page it was read from.
        </p>
      </div>
    </nav>
  );
}
