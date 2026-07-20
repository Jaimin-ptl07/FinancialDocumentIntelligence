"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { getStages, startStage, uploadDocument } from "@/lib/api";
import type { DocumentView } from "@/lib/types";
import { PageHeader } from "@/components/shell/PageHeader";
import { StageGraph } from "@/components/pipeline/StageGraph";
import { STAGES, TERMINAL_STAGE } from "@/lib/stages";
import { formatBytes } from "@/lib/format";

export default function Ingest() {
  const [file, setFile] = useState<File | null>(null);
  const [document, setDocument] = useState<DocumentView | null>(null);

  const start = useMutation({
    mutationFn: async (chosen: File) => {
      const uploaded = await uploadDocument(chosen);
      await startStage(uploaded.id, "ocr");
      return uploaded;
    },
    onSuccess: setDocument,
  });

  const { data } = useQuery({
    queryKey: ["stages", document?.id],
    queryFn: () => getStages(document!.id),
    enabled: document !== null,
    // Stops on its own once the terminal stage settles, or once anything
    // fails. The SP-18 screen polled 90 times on a setTimeout and then gave up
    // silently, which looked identical to success.
    refetchInterval: (query) => {
      const stages = query.state.data?.stages ?? [];
      const terminal = stages.find((s) => s.name === TERMINAL_STAGE);
      const failed = stages.some((s) => s.status === "failed");
      if (failed || terminal?.status === "done") return false;
      return 1_000;
    },
  });

  const stages = data?.stages ?? [];
  const done = stages.filter((s) => s.status === "done").length;
  const failed = stages.find((s) => s.status === "failed");
  const finished = stages.find((s) => s.name === TERMINAL_STAGE)?.status === "done";

  return (
    <>
      <PageHeader
        eyebrow="Ingestion"
        title="Add a filing"
        lede="Upload an annual report as a PDF. Reading it runs fifteen stages, from OCR through to a searchable index."
      />

      <div style={{ padding: "var(--sp-8)", display: "grid", gap: "var(--sp-8)" }}>
        <section
          className="sheet"
          style={{ padding: "var(--sp-6)", maxWidth: "40rem" }}
        >
          <label
            htmlFor="pdf"
            className="eyebrow"
            style={{ display: "block", color: "var(--ink)", marginBottom: "var(--sp-2)" }}
          >
            Annual report PDF
          </label>
          <input
            id="pdf"
            type="file"
            accept="application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="input"
            style={{ marginBottom: "var(--sp-3)" }}
          />
          {file ? (
            <p
              className="data"
              style={{
                fontSize: "var(--text-xs)",
                color: "var(--ink-soft)",
                marginBottom: "var(--sp-4)",
              }}
            >
              {file.name} · {formatBytes(file.size)}
            </p>
          ) : null}
          <button
            type="button"
            disabled={!file || start.isPending}
            onClick={() => file && start.mutate(file)}
            className="btn btn-primary"
          >
            {start.isPending ? "Uploading" : "Start reading"}
          </button>
          {start.error ? (
            <p style={{ marginTop: "var(--sp-4)", fontSize: "var(--text-sm)" }}>
              {(start.error as Error).message}
            </p>
          ) : null}
        </section>

        {document ? (
          <section className="sheet" style={{ padding: "var(--sp-6)" }}>
            <div
              className="flex items-baseline justify-between"
              style={{ marginBottom: "var(--sp-6)", gap: "var(--sp-4)" }}
            >
              <div>
                <div className="eyebrow">
                  {finished ? "Read" : failed ? "Stopped" : "Reading"}
                </div>
                <div style={{ fontWeight: 500 }}>{document.filename}</div>
              </div>
              <div className="data" style={{ fontSize: "var(--text-sm)" }}>
                {done}/{STAGES.length} stages
              </div>
            </div>

            <StageGraph stages={stages} />

            {failed ? (
              <p
                style={{
                  marginTop: "var(--sp-6)",
                  fontSize: "var(--text-sm)",
                  maxWidth: "60ch",
                }}
              >
                <span className="data">{failed.name}</span> failed:{" "}
                {failed.error ?? "the stage reported no detail"}. Fix the cause,
                then reprocess from this stage.
              </p>
            ) : null}

            <Link
              href={`/documents/${document.id}`}
              className="btn"
              style={{ marginTop: "var(--sp-6)" }}
            >
              Open the filing
            </Link>
          </section>
        ) : null}
      </div>
    </>
  );
}
