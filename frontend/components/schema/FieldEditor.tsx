"use client";

import { useState } from "react";
import type { DataType, SchemaField } from "@/lib/types";

const DATA_TYPES: DataType[] = ["decimal", "integer", "string", "date", "list"];

/** The label sits above its control; the hint sits between them, because the
 *  hint is what the analyst needs *before* typing, not after. */
function Row({
  htmlFor,
  label,
  hint,
  children,
}: {
  htmlFor: string;
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: "var(--sp-6)" }}>
      <label
        htmlFor={htmlFor}
        className="eyebrow"
        style={{ display: "block", color: "var(--ink)" }}
      >
        {label}
      </label>
      {hint ? (
        <p
          style={{
            margin: "var(--sp-1) 0 var(--sp-2)",
            fontSize: "var(--text-xs)",
            color: "var(--ink-soft)",
            maxWidth: "62ch",
          }}
        >
          {hint}
        </p>
      ) : (
        <div style={{ height: "var(--sp-2)" }} />
      )}
      {children}
    </div>
  );
}

/** The aliases box holds raw text and normalizes only on the way out. Feeding
 *  `aliases.join("\n")` straight back as the value would trim the space and
 *  swallow the newline the analyst is in the middle of typing. Keyed on the
 *  field path by its parent, so switching fields reseeds it. */
function AliasesBox({
  id,
  aliases,
  onChange,
}: {
  id: string;
  aliases: string[];
  onChange: (next: string[]) => void;
}) {
  const [text, setText] = useState(() => aliases.join("\n"));

  return (
    <textarea
      id={id}
      rows={4}
      className="input data"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(
          e.target.value
            .split("\n")
            .map((a) => a.trim())
            .filter(Boolean),
        );
      }}
    />
  );
}

export function FieldEditor({
  field,
  onChange,
}: {
  field: SchemaField;
  onChange: (next: SchemaField) => void;
}) {
  const patch = (part: Partial<SchemaField>) => onChange({ ...field, ...part });

  return (
    <div>
      <div style={{ marginBottom: "var(--sp-6)" }}>
        <div className="eyebrow">Field</div>
        <h2
          className="display"
          style={{
            fontSize: "var(--text-lg)",
            marginTop: "var(--sp-1)",
          }}
        >
          {field.canonical_label}
        </h2>
        <p
          className="data"
          style={{
            marginTop: "var(--sp-1)",
            fontSize: "var(--text-xs)",
            color: "var(--ink-soft)",
            wordBreak: "break-word",
          }}
        >
          {field.field_path}
        </p>
      </div>

      <Row htmlFor="field-data-type" label="Data type">
        <select
          id="field-data-type"
          className="input data"
          value={field.data_type}
          onChange={(e) => patch({ data_type: e.target.value as DataType })}
        >
          {DATA_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </Row>

      <Row
        htmlFor="field-description"
        label="Description"
        hint="What this figure is, in the filing's own terms. Read by whoever reviews the extraction, and by the model."
      >
        <textarea
          id="field-description"
          rows={2}
          className="input"
          value={field.description}
          onChange={(e) => patch({ description: e.target.value })}
        />
      </Row>

      <Row
        htmlFor="field-extraction-prompt"
        label="Extraction prompt"
        hint="Sent to the model with the retrieved context. Say what to return, and what to return when the figure is absent."
      >
        <textarea
          id="field-extraction-prompt"
          rows={5}
          className="input"
          value={field.extraction_prompt}
          onChange={(e) => patch({ extraction_prompt: e.target.value })}
        />
      </Row>

      <Row
        htmlFor="field-aliases"
        label="Aliases"
        hint="One label per line. These are the wordings the figure appears under in real filings; they also weight the reranker."
      >
        <AliasesBox
          key={field.field_path}
          id="field-aliases"
          aliases={field.aliases}
          onChange={(aliases) => patch({ aliases })}
        />
      </Row>

      <Row
        htmlFor="field-normalization"
        label="Normalization"
        hint="How the raw string becomes a number — scale, sign, and units. Left blank, the value is taken as written."
      >
        <input
          id="field-normalization"
          className="input"
          value={field.normalization ?? ""}
          onChange={(e) => patch({ normalization: e.target.value })}
        />
      </Row>
    </div>
  );
}
