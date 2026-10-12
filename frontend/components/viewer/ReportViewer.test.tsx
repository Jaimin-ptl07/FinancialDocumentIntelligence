import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

const extraction = {
  doc_key: "annual-report-2025.pdf",
  doc_id: "doc-1",
  module: null,
  field_count: 1,
  fields: [
    {
      field_name: "Total assets",
      value: 1250,
      confidence: 0.92,
      source: {
        page: 84,
        snippet: "Total assets 1,250",
        evidence_ids: [],
        bounding_regions: { current: [], prior: [] },
      },
    },
  ],
};

/** POST /extract/document answers 202; the job then walks `statuses`, one
 *  step per poll, staying on the last. */
function serveExtractJob(statuses: Array<{ status: string; error?: string }>) {
  const submits: Array<{ force: boolean }> = [];
  let polls = 0;
  server.use(
    http.get(`${API}/documents/:id`, () => HttpResponse.json(record())),
    http.post(`${API}/extract/document`, async ({ request }) => {
      submits.push((await request.json()) as { force: boolean });
      return HttpResponse.json(
        { job_id: "job-7", correlation_id: "corr-7", status: "queued" },
        { status: 202 },
      );
    }),
    http.get(`${API}/jobs/job-7`, () => {
      const step = statuses[Math.min(polls, statuses.length - 1)];
      polls += 1;
      return HttpResponse.json({
        job_id: "job-7",
        kind: "extract",
        correlation_id: "corr-7",
        status: step.status,
        error: step.error ?? null,
      });
    }),
    http.get(`${API}/jobs/job-7/result`, () => HttpResponse.json(extraction)),
  );
  return submits;
}

beforeEach(() => window.localStorage.clear());

describe("ReportViewer", () => {
  it("reads the figures as a job, showing it queued before they arrive", async () => {
    const submits = serveExtractJob([{ status: "queued" }, { status: "running" }, { status: "succeeded" }]);
    renderViewer("doc-1");

    expect(await screen.findByText(/Queued/)).toBeInTheDocument();
    expect(await screen.findByText("Total assets", {}, { timeout: 4_000 })).toBeInTheDocument();
    expect(submits).toEqual([expect.objectContaining({ force: false })]);
  });

  it("shows a failed reading with the correlation id to report it by", async () => {
    serveExtractJob([{ status: "failed", error: "Azure OpenAI timed out" }]);
    renderViewer("doc-1");

    expect(
      await screen.findByText(/Azure OpenAI timed out \(correlation id corr-7\)/, {}, { timeout: 4_000 }),
    ).toBeInTheDocument();
  });

  it("resumes the reading it left running instead of starting another", async () => {
    window.localStorage.setItem("fdi.job.extract:annual-report-2025.pdf", "job-7");
    const submits = serveExtractJob([{ status: "running" }]);
    renderViewer("doc-1");

    expect(await screen.findByRole("button", { name: /Reading figures/ })).toBeDisabled();
    expect(submits).toEqual([]);
  });

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
