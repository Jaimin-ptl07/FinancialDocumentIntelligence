import { describe, expect, it } from "vitest";
import { HttpResponse, http } from "msw";
import { server } from "@/test/setup";
import { getStages, listDocuments, pdfUrl } from "./api";

describe("listDocuments", () => {
  it("returns the filings the backend reports", async () => {
    const result = await listDocuments();
    expect(result.total).toBe(1);
    expect(result.documents[0].doc_key).toBe("annual-report-2025.pdf");
  });

  it("reports the status and the body when the backend rejects the call", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );
    await expect(listDocuments()).rejects.toThrow(
      /GET \/documents.* failed \(503\): database unavailable/,
    );
  });
});

describe("getStages", () => {
  it("returns one entry per stage that has run", async () => {
    const result = await getStages("doc-1");
    expect(result.stages.map((s) => s.name)).toEqual(["ocr", "ast"]);
  });
});

describe("pdfUrl", () => {
  it("points at the API's own file endpoint, not at object storage", () => {
    expect(pdfUrl("doc-1")).toBe("http://localhost:8000/documents/doc-1/file");
  });
});
