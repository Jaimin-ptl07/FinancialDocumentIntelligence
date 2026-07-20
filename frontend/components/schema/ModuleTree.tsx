"use client";

import type { SchemaField, SchemaModule } from "@/lib/types";

export interface FieldPath {
  moduleKey: string;
  unitId: string;
  fieldName: string;
}

/** A field answers to its machine name, the label an analyst reads, and every
 *  wording it has been seen under in a filing. Searching only the label would
 *  hide 198 fields behind vocabulary the analyst does not know yet. */
function matches(field: SchemaField, needle: string): boolean {
  if (!needle) return true;
  return (
    field.field_name.toLowerCase().includes(needle) ||
    field.canonical_label.toLowerCase().includes(needle) ||
    field.aliases.some((a) => a.toLowerCase().includes(needle))
  );
}

/** Modules and units with nothing left in them fall away, so the rail shows
 *  the shape of the answer rather than a column of empty headings. */
export function filterModules(
  modules: SchemaModule[],
  query: string,
): SchemaModule[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return modules;
  return modules
    .map((module) => ({
      ...module,
      retrieval_units: module.retrieval_units
        .map((unit) => ({
          ...unit,
          fields: unit.fields.filter((f) => matches(f, needle)),
        }))
        .filter((unit) => unit.fields.length > 0),
    }))
    .filter((module) => module.retrieval_units.length > 0);
}

export function countFields(modules: SchemaModule[]): number {
  return modules.reduce(
    (total, module) =>
      total +
      module.retrieval_units.reduce((n, unit) => n + unit.fields.length, 0),
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
            }}
          >
            <span>{module.name}</span>
            <span className="data" style={{ color: "var(--ink-faint)" }}>
              {countFields([module])}
            </span>
          </h2>

          {module.retrieval_units.map((unit) => (
            <div key={unit.unit_id} style={{ paddingBottom: "var(--sp-2)" }}>
              <div
                className="data"
                style={{
                  padding: "var(--sp-1) var(--sp-4)",
                  fontSize: "var(--text-xs)",
                  color: "var(--ink-soft)",
                }}
              >
                {unit.unit_id} · {unit.title}
              </div>
              <ul>
                {unit.fields.map((field) => {
                  const active =
                    selected?.moduleKey === module.key &&
                    selected.unitId === unit.unit_id &&
                    selected.fieldName === field.field_name;
                  return (
                    <li key={field.field_path}>
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
                        className="w-full text-left hover:bg-leaf-2"
                        style={{
                          padding: "var(--sp-1) var(--sp-4)",
                          fontSize: "var(--text-sm)",
                          fontWeight: active ? 600 : 400,
                          color: active ? "var(--ink)" : "var(--ink-soft)",
                          background: active ? "var(--leaf-2)" : "transparent",
                          /* The active mark is ink, not --tether: --tether is
                             spent entirely on provenance. Matches the Spine. */
                          boxShadow: active
                            ? "inset 2px 0 0 0 var(--ink)"
                            : undefined,
                        }}
                      >
                        {field.canonical_label}
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
