const INDIAN_GROUPING = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** field_name is a display label the extraction schema deliberately reuses
 *  across modules (e.g. "Cash on hand" appears under both Balance Sheet and
 *  a note) -- it cannot key a React list or identify a selected row without
 *  colliding. Pairing it with its position in the fields array a given
 *  extraction response returns gives every row a unique, stable id. */
export function fieldId(index: number, fieldName: string): string {
  return `${index}:${fieldName}`;
}

/** A dual-period field (schema `periods` set) comes back from the extractor
 *  as `{current, prior}` rather than a scalar — see extractor.py's
 *  build_unit_messages prompt contract. Guarded structurally, not by field
 *  name, since dual-period-ness is a schema property or the caller's. */
export function isDualPeriod(value: unknown): value is { current: unknown; prior: unknown } {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    "current" in value &&
    "prior" in value
  );
}

/** Extracted values arrive as `unknown` — the schema's data_type says what the
 *  field SHOULD be, but the model may return anything, and a failed extraction
 *  returns null. Numbers are grouped lakh/crore style because every source
 *  document in this corpus is an Indian statutory filing. */
export function formatAmount(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isFinite(value)) {
    return INDIAN_GROUPING.format(value);
  }
  if (isDualPeriod(value)) {
    return formatAmount(value.current);
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

/** Confidence rendered as ink density: a value the model is unsure about
 *  literally looks faded on the page, the way a weak carbon copy does. Floored
 *  at 0.45 so it stays legible — a fully transparent figure would be a bug,
 *  not a signal. */
export function inkOpacity(confidence: number): number {
  const clamped = clamp(confidence);
  return Number((0.45 + 0.55 * clamped).toFixed(4));
}

export function formatConfidence(confidence: number): string {
  return clamp(confidence).toFixed(2);
}

/** Byte sizes are shown to help an analyst recognize a filing, not to audit
 *  storage, so one decimal place and MB is as precise as it needs to be. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const INR_CURRENCY = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

/** Cost figures (costing feature) are always shown in Rupees, grouped
 *  lakh/crore style like formatAmount -- Intl's "en-IN" locale does this
 *  natively for currency too. */
export function formatInr(amount: number): string {
  return INR_CURRENCY.format(amount);
}

/** Percentages appear beside counts across the analytics screen; keeping them
 *  whole-numbered stops a row of tiles turning into decimal noise. */
export function formatPercent(fraction: number): string {
  return `${Math.round(clamp(fraction) * 100)}%`;
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
}
