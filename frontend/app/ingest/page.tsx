"use client";

import Link from "next/link";
import { useState } from "react";
import type { DragEvent } from "react";
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
  const [dragActive, setDragActive] = useState(false);

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

  // The dropzone is additive over the native input, not a replacement for
  // it: drag-and-drop only ever ends by calling the same setFile the input's
  // onChange calls, so there is exactly one path a chosen file can take.
  const acceptDrop = (dropped: File | undefined) => {
    if (dropped && dropped.type === "application/pdf") setFile(dropped);
  };

  const onDragOver = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragActive(true);
  };

  const onDragLeave = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragActive(false);
  };

  const onDrop = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragActive(false);
    acceptDrop(e.dataTransfer.files?.[0]);
  };

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
          <div
            className="eyebrow"
            style={{ marginBottom: "var(--sp-3)" }}
          >
            Annual report PDF
          </div>

          {/* The dropzone is a <label> so the entire panel — not just the
              native input — is a click target; the input keeps its own
              "Annual report PDF" accessible name via aria-label (rather than
              inheriting the label's visible text, which is the drop
              instructions) so a screen reader and a test driving the input
              directly both see the same name either way. Drag-and-drop is
              layered on top of onChange, not a replacement for it — both
              paths end at the same setFile. */}
          <label
            htmlFor="pdf"
            onDragOver={onDragOver}
            onDragEnter={onDragOver}
            onDragLeave={onDragLeave}
            onDrop={onDrop}
            className="sheet-flat"
            style={{
              display: "block",
              position: "relative",
              padding: "var(--sp-6)",
              marginBottom: "var(--sp-3)",
              textAlign: "center",
              cursor: "pointer",
              borderStyle: "dashed",
              borderColor: dragActive ? "var(--ink)" : "var(--rule)",
              background: dragActive ? "var(--leaf-2)" : "var(--leaf)",
              transition: "background-color 120ms ease, border-color 120ms ease",
            }}
          >
            <div
              className="data"
              aria-hidden="true"
              style={{ fontSize: "var(--text-lg)", marginBottom: "var(--sp-2)" }}
            >
              {dragActive ? "↓" : "⇪"}
            </div>
            <div style={{ fontSize: "var(--text-sm)", marginBottom: "var(--sp-1)" }}>
              {file ? (
                <span className="data">
                  {file.name} · {formatBytes(file.size)}
                </span>
              ) : (
                <>Drop a PDF here, or click to browse</>
              )}
            </div>
            <div style={{ fontSize: "var(--text-xs)", color: "var(--ink-faint)" }}>
              One annual report at a time, PDF only
            </div>
            <input
              id="pdf"
              type="file"
              accept="application/pdf"
              aria-label="Annual report PDF"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              style={{
                position: "absolute",
                width: 1,
                height: 1,
                padding: 0,
                margin: -1,
                overflow: "hidden",
                clip: "rect(0, 0, 0, 0)",
                whiteSpace: "nowrap",
                border: 0,
              }}
            />
          </label>

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
