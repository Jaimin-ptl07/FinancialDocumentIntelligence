"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/** Destinations are named by what the analyst does there, not by the noun the
 *  system stores. "Add a filing", not "Upload". */
const DESTINATIONS = [
  { href: "/", label: "Filings", hint: "Everything read so far" },
  { href: "/ingest", label: "Add a filing", hint: "Upload and process" },
  { href: "/schema", label: "Extraction schema", hint: "What to look for" },
  { href: "/analytics", label: "Quality", hint: "How well it read" },
] as const;

export function Spine() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Sections"
      className="shrink-0 flex flex-col"
      style={{
        width: "var(--spine-w)",
        background: "var(--leaf)",
        borderRight: "var(--hairline)",
      }}
    >
      <div style={{ padding: "var(--sp-6) var(--sp-5) var(--sp-5)" }}>
        <div className="eyebrow">Financial document</div>
        <div
          className="display"
          style={{ fontSize: "var(--text-md)", marginTop: "var(--sp-1)" }}
        >
          Intelligence
        </div>
      </div>

      <ul style={{ padding: "var(--sp-2) var(--sp-3)" }}>
        {DESTINATIONS.map((d) => {
          const active =
            d.href === "/" ? pathname === "/" : pathname.startsWith(d.href);
          return (
            <li key={d.href}>
              <Link
                href={d.href}
                aria-current={active ? "page" : undefined}
                className="block"
                style={{
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

      <div
        className="mt-auto"
        style={{
          padding: "var(--sp-4) var(--sp-5)",
          borderTop: "1px solid var(--rule-soft)",
        }}
      >
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
