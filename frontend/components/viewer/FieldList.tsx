"use client";

import { useMemo, useState } from "react";
import type { ExtractFieldView } from "@/lib/types";
import { fieldId as toFieldId } from "@/lib/format";
import { Value } from "@/components/data/Value";

type Lens = "all" | "soft" | "missing";

const LENSES: { key: Lens; label: string }[] = [
  { key: "all", label: "All" },
  { key: "soft", label: "Needs a check" },
  { key: "missing", label: "Not found" },
];

const LOW_CONFIDENCE = 0.5;

// `field_name` is a display label the schema deliberately reuses across
// modules (e.g. "Cash on hand" appears under both Balance Sheet and a note),
// so it cannot key a React list or identify a selected row on its own --
// pairing it with its position in `fields` gives every row a unique id while
// keeping the label itself untouched everywhere it's displayed.
type IdentifiedField = { field: ExtractFieldView; fieldId: string };

export function FieldList({
  fields,
  selected,
  onSelect,
}: {
  fields: ExtractFieldView[];
  selected: string | null;
  onSelect: (fieldId: string) => void;
}) {
  const [lens, setLens] = useState<Lens>("all");

  const identified = useMemo<IdentifiedField[]>(
    () => fields.map((field, index) => ({ field, fieldId: toFieldId(index, field.field_name) })),
    [fields],
  );

  const { found, soft, missing } = useMemo(() => {
    const found = identified.filter(
      (f) => f.field.value !== null && f.field.value !== undefined,
    );
    return {
      found,
      soft: found.filter((f) => f.field.confidence < LOW_CONFIDENCE),
      missing: identified.filter(
        (f) => f.field.value === null || f.field.value === undefined,
      ),
    };
  }, [identified]);

  const shown =
    lens === "soft" ? soft : lens === "missing" ? missing : identified;

  return (
    <div>
      <div
        style={{
          padding: "var(--sp-4)",
          borderBottom: "var(--hairline)",
          position: "sticky",
          top: 0,
          background: "var(--leaf)",
          zIndex: 2,
        }}
      >
        <div
          className="flex items-baseline"
          style={{ gap: "var(--sp-4)", marginBottom: "var(--sp-3)" }}
        >
          <span className="eyebrow">Figures read</span>
          <span className="data" style={{ fontSize: "var(--text-xs)" }}>
            {found.length}/{fields.length}
          </span>
          {soft.length > 0 ? (
            // Used to be carried by --flag color alone, which now equals
            // plain ink — indistinguishable from the count beside it. A
            // dotted underline (matching the soft-figure treatment in
            // Value.tsx) plus a weight bump gives it a shape of its own.
            <span
              className="data"
              style={{
                fontSize: "var(--text-xs)",
                fontWeight: 600,
                color: "var(--flag)",
                borderBottom: "1.5px dotted var(--flag)",
                paddingBottom: 1,
              }}
            >
              {soft.length} soft
            </span>
          ) : null}
        </div>

        <div className="flex" style={{ gap: "var(--sp-1)" }}>
          {LENSES.map((l) => {
            const count =
              l.key === "soft"
                ? soft.length
                : l.key === "missing"
                  ? missing.length
                  : fields.length;
            const active = lens === l.key;
            return (
              <button
                key={l.key}
                type="button"
                onClick={() => setLens(l.key)}
                aria-pressed={active}
                className="data"
                style={{
                  fontSize: "var(--text-2xs)",
                  padding: "var(--sp-1) var(--sp-2)",
                  borderRadius: "var(--radius-sm)",
                  border: "1px solid",
                  borderColor: active ? "var(--ink)" : "var(--rule)",
                  background: active ? "var(--ink)" : "transparent",
                  color: active ? "var(--leaf)" : "var(--ink-soft)",
                  cursor: "pointer",
                }}
              >
                {l.label} {count}
              </button>
            );
          })}
        </div>
      </div>

      {shown.length === 0 ? (
        <p
          style={{
            padding: "var(--sp-6)",
            color: "var(--ink-soft)",
            fontSize: "var(--text-sm)",
          }}
        >
          {lens === "soft"
            ? "Nothing here needs a second look."
            : "Every field in this schema was found."}
        </p>
      ) : (
        shown.map(({ field, fieldId }) => (
          <Value
            key={fieldId}
            field={field}
            fieldId={fieldId}
            selected={selected === fieldId}
            onSelect={onSelect}
          />
        ))
      )}
    </div>
  );
}
