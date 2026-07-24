"use client";

import Link from "next/link";
import type { DocumentSummaryView } from "@/lib/types";
import { formatBytes } from "@/lib/format";
import { summarizeStageCounts } from "@/lib/stages";
import { filingLabel, hasSourceFile } from "@/lib/documents";

const cell = {
  padding: "var(--sp-3) var(--sp-4)",
  verticalAlign: "baseline" as const,
};

export function FilingRow({ document }: { document: DocumentSummaryView }) {
  const { done, failed, total } = summarizeStageCounts(document.stage_counts);
  const complete = failed === 0 && done >= total;
  const sourceless = !hasSourceFile(document);

  return (
    <tr
      className="transition-colors duration-200 hover:bg-leaf-2"
      style={{ borderTop: "1px solid var(--rule-soft)" }}
    >
      <td style={cell}>
        <Link
          href={`/documents/${document.id}`}
          style={{
            fontWeight: 500,
            textDecoration: "underline",
            textUnderlineOffset: "3px",
          }}
        >
          {filingLabel(document)}
        </Link>
        {sourceless ? (
          <span
            className="data"
            style={{
              marginLeft: "var(--sp-2)",
              fontSize: "var(--text-2xs)",
              color: "var(--ink-faint)",
              whiteSpace: "nowrap",
            }}
          >
            no source PDF
          </span>
        ) : null}
      </td>
      <td className="data" style={{ ...cell, fontSize: "var(--text-sm)" }}>
        v{document.version ?? "—"}
      </td>
      <td
        className="data"
        style={{
          ...cell,
          fontSize: "var(--text-sm)",
          color: "var(--ink-soft)",
          textAlign: "right",
        }}
      >
        {formatBytes(document.byte_size)}
      </td>
      <td style={cell}>
        {/* Bold is reserved app-wide for "needs attention" (LedgerTotals,
            Value, FieldList) — "done" must not also claim it, or the same
            weight would mean opposite things depending which screen a user
            is looking at. The checkmark glyph alone carries "complete" here;
            weight stays at the row's ordinary 400. */}
        <span
          className="data"
          style={{
            fontSize: "var(--text-sm)",
            color: "var(--tick)",
          }}
        >
          {complete ? "✓ " : ""}
          {done}/{total}
        </span>
        {failed > 0 ? (
          <span
            className="data"
            style={{
              marginLeft: "var(--sp-2)",
              padding: "1px var(--sp-2)",
              borderRadius: "var(--radius-sm)",
              background: "var(--ink)",
              color: "var(--leaf)",
              fontSize: "var(--text-2xs)",
            }}
          >
            ✕ {failed} failed
          </span>
        ) : null}
      </td>
    </tr>
  );
}
