"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSyncExternalStore } from "react";

/** Destinations are named by what the analyst does there, not by the noun the
 *  system stores. "Add a filing", not "Upload". */
const DESTINATIONS = [
  { href: "/", label: "Filings" },
  { href: "/ingest", label: "Add a filing" },
  { href: "/schema", label: "Extraction schema" },
  { href: "/analytics", label: "Quality" },
  { href: "/costing", label: "Costing" },
] as const;

const THEME_KEY = "fdi-theme";
const THEME_CHANGE_EVENT = "fdi-theme-change";
type Theme = "light" | "dark";

function readTheme(): Theme {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  if (typeof window.matchMedia !== "function") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/** useSyncExternalStore, not useState+useEffect: its getServerSnapshot slot
 *  is the one place React lets a component say "the server legitimately
 *  cannot know this value; render `null` there" without an effect-driven
 *  setState-after-mount — which this codebase's lint config rejects
 *  (react-hooks/set-state-in-effect), and which would in any case cost a
 *  hydration-mismatch warning on every visitor whose real theme differs
 *  from the server's guess (the server always guesses "light": it has no
 *  access to localStorage). `data-theme` only ever changes through this
 *  hook's own setTheme() below — never from another tab, another
 *  component, or an outside script — so the subscribe/dispatch pair exists
 *  purely to give useSyncExternalStore a real signal to re-run getSnapshot
 *  on, which is what actually re-renders the button after a click. */
function useTheme(): [Theme | null, (next: Theme) => void] {
  const theme = useSyncExternalStore(
    (onChange) => {
      window.addEventListener(THEME_CHANGE_EVENT, onChange);
      return () => window.removeEventListener(THEME_CHANGE_EVENT, onChange);
    },
    readTheme,
    () => null,
  );

  const setTheme = (next: Theme) => {
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // Private-browsing/quota errors leave the choice live for this tab via
      // the attribute above; it just won't survive a reload. Not worth
      // surfacing to the analyst over a cosmetic preference.
    }
    window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
  };

  return [theme, setTheme];
}

export function Spine() {
  const pathname = usePathname();
  const [theme, setTheme] = useTheme();

  return (
    <nav aria-label="Sections" className="spine">
      <div
        className="spine-brand flex items-start justify-between"
        style={{ gap: "var(--sp-3)" }}
      >
        <div style={{ minWidth: 0 }}>
          <div className="eyebrow">Financial document</div>
          <div
            className="display"
            style={{ fontSize: "var(--text-md)", marginTop: "var(--sp-1)" }}
          >
            Intelligence
          </div>
        </div>

        <button
          type="button"
          onClick={() => theme && setTheme(theme === "dark" ? "light" : "dark")}
          disabled={theme === null}
          aria-label={
            // Neutral label pre-mount: the server cannot know the visitor's
            // saved preference or OS setting, so it cannot know which
            // direction this button will switch to yet either. Resolves to
            // the real label as soon as useSyncExternalStore re-checks its
            // snapshot against the client's actual DOM, just after hydration.
            theme === null
              ? "Toggle theme"
              : theme === "dark"
                ? "Switch to light mode"
                : "Switch to dark mode"
          }
          title={
            theme === null
              ? undefined
              : theme === "dark"
                ? "Switch to light mode"
                : "Switch to dark mode"
          }
          className="btn shrink-0"
          style={{
            padding: "var(--sp-1) var(--sp-2)",
            fontSize: "var(--text-sm)",
            lineHeight: 1,
          }}
        >
          {/* Text glyphs, not an icon font — consistent with the rest of the
              app, which draws every mark (✓, ✕, ◐, ●) from plain characters
              rather than an icon library. A neutral dot pre-mount, same
              reasoning as the label above: ☾/☀ would assert a direction the
              server-rendered pass cannot actually know yet. */}
          <span aria-hidden="true">
            {theme === null ? "◐" : theme === "dark" ? "☀" : "☾"}
          </span>
        </button>
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
                className={
                  active
                    ? "block whitespace-nowrap text-ink"
                    : "block whitespace-nowrap text-ink-soft transition-colors duration-150 hover:bg-leaf-2 hover:text-ink"
                }
                style={{
                  padding: "var(--sp-2) var(--sp-3)",
                  marginBottom: "2px",
                  borderRadius: "var(--radius-sm)",
                  background: active ? "var(--leaf-2)" : undefined,
                  boxShadow: active ? "inset 2px 0 0 0 var(--ink)" : undefined,
                  fontSize: "var(--text-sm)",
                  fontWeight: active ? 600 : 400,
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
