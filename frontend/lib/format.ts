const INDIAN_GROUPING = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Extracted values arrive as `unknown` — the schema's data_type says what the
 *  field SHOULD be, but the model may return anything, and a failed extraction
 *  returns null. Numbers are grouped lakh/crore style because every source
 *  document in this corpus is an Indian statutory filing. */
export function formatAmount(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isFinite(value)) {
    return INDIAN_GROUPING.format(value);
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

/** Percentages appear beside counts across the analytics screen; keeping them
 *  whole-numbered stops a row of tiles turning into decimal noise. */
export function formatPercent(fraction: number): string {
  return `${Math.round(clamp(fraction) * 100)}%`;
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
}
