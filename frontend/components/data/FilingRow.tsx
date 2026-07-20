"use client";

import Link from "next/link";
import type { DocumentSummaryView } from "@/lib/types";
import { formatBytes } from "@/lib/format";

const cell = {
  padding: "var(--sp-3) var(--sp-4)",
  verticalAlign: "baseline" as const,
};

export function FilingRow({ document }: { document: DocumentSummaryView }) {
  const failed = document.stages_failed > 0;
  const complete =
    !failed && document.stages_done >= document.stages_total;

  return (
    <tr style={{ borderTop: "1px solid var(--rule-soft)" }}>
      <td style={cell}>
        <Link
          href={`/documents/${document.id}`}
          style={{ fontWeight: 500, textDecoration: "underline", textUnderlineOffset: "3px" }}
        >
          {document.filename ?? document.doc_key ?? document.id}
        </Link>
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
        <span
          className="data"
          style={{
            fontSize: "var(--text-sm)",
            color: complete ? "var(--tick)" : "var(--ink)",
          }}
        >
          {document.stages_done}/{document.stages_total}
        </span>
        {failed ? (
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
            {document.failed_stage ?? "a stage"} failed
          </span>
        ) : null}
      </td>
    </tr>
  );
}
