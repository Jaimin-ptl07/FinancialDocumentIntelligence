import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "@/test/setup";
import { ReportViewer } from "./ReportViewer";

// pdfjs-dist cannot run under jsdom (no canvas, no worker), and this suite is
// about which branch the viewer chooses, not about rasterizing a page.
vi.mock("./PdfCanvas", () => ({
  PdfCanvas: () => <div data-testid="pdf-canvas" />,
}));

const API = "http://localhost:8000";

function renderViewer(docId: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReportViewer docId={docId} />
    </QueryClientProvider>,
  );
}

const record = (over: Record<string, unknown> = {}) => ({
  id: "doc-1",
  checksum: "abc123",
  filename: "annual-report-2025.pdf",
  byte_size: 4_200_000,
  doc_key: "annual-report-2025.pdf",
  version: 1,
  content_type: "application/pdf",
  ...over,
});

describe("ReportViewer", () => {
  it("renders the filing when a source PDF is stored", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () => HttpResponse.json(record())),
    );
    renderViewer("doc-1");
    expect(await screen.findByTestId("pdf-canvas")).toBeInTheDocument();
  });

  it("explains a record that has no source PDF instead of failing at it", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json(
          record({ checksum: null, filename: null, doc_key: null }),
        ),
      ),
    );
    renderViewer("doc-9");

    expect(await screen.findByText("No source PDF")).toBeInTheDocument();
    expect(
      screen.getByText(/created by the pipeline rather than uploaded/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("pdf-canvas")).not.toBeInTheDocument();
  });

  it("does not offer to read figures out of a record with no filing", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json(record({ checksum: null })),
      ),
    );
    renderViewer("doc-9");
    await screen.findByText("No source PDF");
    expect(screen.getByRole("button", { name: "Read figures" })).toBeDisabled();
  });

  it("never titles a record with a bare UUID", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json(
          record({ checksum: null, filename: null, doc_key: null }),
        ),
      ),
    );
    renderViewer("17a180c7-dd8d-4ccb-bfd3-0720d480a78a");
    expect(
      await screen.findByRole("heading", { name: "Untitled record" }),
    ).toBeInTheDocument();
  });
});
