"use client";

import type { ExtractFieldView } from "@/lib/types";
import { formatAmount, formatConfidence, inkOpacity } from "@/lib/format";
import { RefMark } from "./RefMark";

const LOW_CONFIDENCE = 0.5;

/** One extracted figure. Two ideas carry the whole row:
 *
 *  Confidence is ink density — a value the model is unsure about is literally
 *  faded, the way a weak carbon copy is, so an analyst can spot the soft
 *  numbers by squinting before reading a single score.
 *
 *  The reference mark is the way into the source. Clicking it is what selects
 *  the field, scrolls the page, and draws the tether. */
export function Value({
  field,
  selected,
  onSelect,
}: {
  field: ExtractFieldView;
  selected: boolean;
  onSelect: (fieldName: string) => void;
}) {
  const missing = field.value === null || field.value === undefined;
  const soft = field.confidence < LOW_CONFIDENCE;

  return (
    <div
      data-field={field.field_name}
      className="flex items-baseline justify-between"
      style={{
        gap: "var(--sp-4)",
        padding: "var(--sp-2) var(--sp-4)",
        borderBottom: "1px solid var(--rule-soft)",
        background: selected ? "var(--leaf-2)" : undefined,
        boxShadow: selected ? "inset 2px 0 0 0 var(--ink)" : undefined,
      }}
    >
      <span className="data" style={{ fontSize: "var(--text-xs)", minWidth: 0 }}>
        {field.field_name}
      </span>

      <span
        className="flex items-baseline shrink-0"
        style={{ gap: "var(--sp-2)" }}
      >
        {missing ? (
          <span
            style={{ fontSize: "var(--text-sm)", color: "var(--ink-faint)" }}
          >
            Not found
          </span>
        ) : (
          <span
            className="data"
            style={{
              fontSize: "var(--text-sm)",
              opacity: inkOpacity(field.confidence),
              borderBottom: soft ? "1px solid var(--flag)" : undefined,
            }}
          >
            {formatAmount(field.value)}
          </span>
        )}

        {field.source.page !== null ? (
          <RefMark
            page={field.source.page}
            active={selected}
            label={`Show ${field.field_name} on page ${field.source.page}`}
            onClick={() => onSelect(field.field_name)}
          />
        ) : null}

        <span
          className="data"
          style={{
            fontSize: "var(--text-2xs)",
            color: soft ? "var(--flag)" : "var(--ink-faint)",
            width: "3ch",
            textAlign: "right",
          }}
        >
          {formatConfidence(field.confidence)}
        </span>
      </span>
    </div>
  );
}
