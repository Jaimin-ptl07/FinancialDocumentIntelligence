import { describe, expect, it } from "vitest";
import type { DocumentView } from "./types";
import { filingLabel, hasSourceFile } from "./documents";

const doc = (over: Partial<DocumentView> = {}): DocumentView => ({
  id: "doc-1",
  checksum: "abc123",
  filename: "annual-report-2025.pdf",
  byte_size: 4_200_000,
  doc_key: "annual-report-2025.pdf",
  version: 1,
  content_type: "application/pdf",
  ...over,
});

describe("hasSourceFile", () => {
  it("is true for an uploaded filing", () => {
    expect(hasSourceFile(doc())).toBe(true);
  });

  it("is false for a ping-job row, which has no stored PDF", () => {
    expect(hasSourceFile(doc({ checksum: null }))).toBe(false);
  });

  it("is false before the filing has loaded", () => {
    expect(hasSourceFile(null)).toBe(false);
    expect(hasSourceFile(undefined)).toBe(false);
  });
});

describe("filingLabel", () => {
  it("prefers the name the analyst uploaded", () => {
    expect(filingLabel(doc())).toBe("annual-report-2025.pdf");
  });

  it("falls back to the doc key when there is no filename", () => {
    expect(filingLabel(doc({ filename: null }))).toBe(
      "annual-report-2025.pdf",
    );
  });

  it("never presents a bare UUID as a document title", () => {
    expect(filingLabel(doc({ filename: null, doc_key: null }))).toBe(
      "Untitled record",
    );
  });
});
