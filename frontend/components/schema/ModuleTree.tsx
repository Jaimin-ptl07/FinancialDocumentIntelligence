"use client";

import type { SchemaField, SchemaModule } from "@/lib/types";

export interface FieldPath {
  moduleKey: string;
  unitId: string;
  fieldName: string;
}

/** A field answers to its machine name, the label an analyst reads, and every
 *  wording it has been seen under in a filing. Searching only the label would
 *  hide 198 fields behind vocabulary the analyst does not know yet.
 *
 *  `canonical_label`/`aliases` are undefined on a v4.1-shaped field (that
 *  schema has no such properties) -- guarded here rather than assumed, since
 *  this tree is not yet updated to really understand the v4.1 hierarchy. */
function matches(field: SchemaField, needle: string): boolean {
  if (!needle) return true;
  return (
    field.field_name.toLowerCase().includes(needle) ||
    (field.canonical_label ?? "").toLowerCase().includes(needle) ||
    (field.aliases ?? []).some((a) => a.toLowerCase().includes(needle))
  );
}

/** Modules and units with nothing left in them fall away, so the rail shows
 *  the shape of the answer rather than a column of empty headings. */
export function filterModules(
  modules: SchemaModule[],
  query: string,
): SchemaModule[] {
  const safeModules = modules ?? [];
  const needle = query.trim().toLowerCase();
  if (!needle) return safeModules;
  return safeModules
    .map((module) => ({
      ...module,
      retrieval_units: (module.retrieval_units ?? [])
        .map((unit) => ({
          ...unit,
          fields: (unit.fields ?? []).filter((f) => matches(f, needle)),
        }))
        .filter((unit) => unit.fields.length > 0),
    }))
    .filter((module) => module.retrieval_units.length > 0);
}

/** Guarded against a v4.1-shaped document (nested fields, units/modules
 *  missing the v3.1 fields this counts): counts only top-level fields per
 *  unit, not the full nested tree, so this stays a crash-free approximation
 *  rather than a real v4.1 field count. */
export function countFields(modules: SchemaModule[]): number {
  return (modules ?? []).reduce(
    (total, module) =>
      total +
      (module.retrieval_units ?? []).reduce(
        (n, unit) => n + (unit.fields ?? []).length,
        0,
      ),
    0,
  );
}

export function ModuleTree({
  modules,
  query,
  selected,
  onSelect,
}: {
  modules: SchemaModule[];
  query: string;
  selected: FieldPath | null;
  onSelect: (path: FieldPath) => void;
}) {
  const visible = filterModules(modules, query);

  if (visible.length === 0) {
    return (
      <p
        style={{
          padding: "var(--sp-6) var(--sp-4)",
          fontSize: "var(--text-sm)",
          color: "var(--ink-soft)",
        }}
      >
        No field answers to that name or alias.
      </p>
    );
  }

  return (
    <div style={{ paddingBottom: "var(--sp-4)" }}>
      {visible.map((module) => (
        <section
          key={module.key}
          style={{ borderBottom: "1px solid var(--rule-soft)" }}
        >
          <h2
            className="eyebrow"
            style={{
              display: "flex",
              alignItems: "baseline",
              justifyContent: "space-between",
              gap: "var(--sp-2)",
              padding: "var(--sp-3) var(--sp-4) var(--sp-2)",
              color: "var(--ink)",
              /* Top of the hierarchy: the widest tracking, so the eye reads
                 "section" before it reads "row". Units and fields below step
                 down in tracking/size/weight rather than in color. */
              letterSpacing: "0.08em",
            }}
          >
            <span>{module.name}</span>
            <span className="data" style={{ color: "var(--ink-faint)" }}>
              {countFields([module])}
            </span>
          </h2>

          {(module.retrieval_units ?? []).map((unit) => (
            <div key={unit.unit_id} style={{ paddingBottom: "var(--sp-2)" }}>
              <div
                className="data"
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  gap: "var(--sp-2)",
                  /* One step in from the module heading: the indent is the
                     hierarchy cue, not a rule or a tint. */
                  padding: "var(--sp-1) var(--sp-4) var(--sp-1) var(--sp-5)",
                  fontSize: "var(--text-xs)",
                  fontWeight: 500,
                  color: "var(--ink-soft)",
                }}
              >
                <span aria-hidden="true" style={{ color: "var(--ink-faint)" }}>
                  &rsaquo;
                </span>
                {/* `title` is undefined on a v4.1-shaped unit (no such
                    property there) -- falls back to the retrieval query,
                    then blank, rather than literally rendering "undefined". */}
                <span>{unit.unit_id} · {unit.title ?? unit.retrieval_query ?? ""}</span>
              </div>
              <ul>
                {(unit.fields ?? []).map((field) => {
                  const active =
                    selected?.moduleKey === module.key &&
                    selected.unitId === unit.unit_id &&
                    selected.fieldName === field.field_name;
                  return (
                    // `field_path` is undefined on a v4.1 group/parent node
                    // (only leaves carry it there) -- fall back to the field
                    // name so this never renders a duplicate-`undefined` key.
                    <li key={field.field_path ?? field.field_name}>
                      <button
                        type="button"
                        aria-current={active ? "true" : undefined}
                        onClick={() =>
                          onSelect({
                            moduleKey: module.key,
                            unitId: unit.unit_id,
                            fieldName: field.field_name,
                          })
                        }
                        className="group w-full text-left hover:bg-leaf-2 focus-visible:bg-leaf-2"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "var(--sp-2)",
                          /* Two steps in from the module heading: fields sit
                             under their unit the way a sub-item sits under a
                             heading in a table of contents. */
                          padding: "var(--sp-1) var(--sp-4) var(--sp-1) var(--sp-6)",
                          fontSize: "var(--text-sm)",
                          fontWeight: active ? 600 : 400,
                          color: active ? "var(--ink)" : "var(--ink-soft)",
                          background: active ? "var(--leaf-2)" : "transparent",
                          transition: "background-color 120ms ease",
                          /* The active mark is ink, not --tether: --tether is
                             spent entirely on provenance. Matches the Spine. */
                          boxShadow: active
                            ? "inset 2px 0 0 0 var(--ink)"
                            : "inset 2px 0 0 0 transparent",
                        }}
                      >
                        {/* A hover-only tick, so a row reads as reachable
                            before the pointer commits to a click — the same
                            affordance the active state gives for free, shown
                            in weight rather than a new color. Reserved at a
                            fixed width (not conditionally rendered) so its
                            fade-in never nudges the label beside it. */}
                        <span
                          aria-hidden="true"
                          className={
                            active ? "opacity-0" : "opacity-0 group-hover:opacity-100"
                          }
                          style={{
                            width: "0.6em",
                            flexShrink: 0,
                            color: "var(--ink-faint)",
                            fontSize: "var(--text-xs)",
                            transition: "opacity 120ms ease",
                          }}
                        >
                          ›
                        </span>
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                          {/* `canonical_label` is undefined on a v4.1-shaped
                              field -- falls back to the raw field name. */}
                          {field.canonical_label ?? field.field_name}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
