import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import Analytics from "./page";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const API = "http://localhost:8000";

function serveQualityRead(statuses: string[]) {
  let polls = 0;
  server.use(
    http.post(`${API}/extract/document`, () =>
      HttpResponse.json(
        { job_id: "job-q", correlation_id: "corr-q", status: "queued" },
        { status: 202 },
      ),
    ),
    http.get(`${API}/jobs/job-q`, () => {
      const status = statuses[Math.min(polls, statuses.length - 1)];
      polls += 1;
      return HttpResponse.json({
        job_id: "job-q",
        kind: "extract",
        correlation_id: "corr-q",
        status,
        error: status === "failed" ? "no index for this filing" : null,
      });
    }),
    http.get(`${API}/jobs/job-q/result`, () =>
      HttpResponse.json({
        doc_key: "annual-report-2025.pdf",
        doc_id: "doc-1",
        module: null,
        field_count: 2,
        fields: [
          { field_name: "Total assets", value: 10, confidence: 0.9, source: {} },
          { field_name: "Revenue", value: null, confidence: 0, source: {} },
        ],
      }),
    ),
  );
}

async function readFirstFiling() {
  const user = userEvent.setup();
  await screen.findByRole("option", { name: "annual-report-2025.pdf" });
  await user.selectOptions(screen.getByLabelText("Filing to read"), "annual-report-2025.pdf");
  await user.click(screen.getByRole("button", { name: "Read figures" }));
}

beforeEach(() => window.localStorage.clear());

describe("Analytics", () => {
  it("reads a filing's quality as a job and reports how much it answered", async () => {
    serveQualityRead(["running", "succeeded"]);
    renderWithQuery(<Analytics />);
    await readFirstFiling();

    expect(await screen.findByRole("button", { name: /Queued|Reading/ })).toBeDisabled();
    expect(await screen.findByText("1/2", {}, { timeout: 4_000 })).toBeInTheDocument();
    expect(screen.getByText("Read from annual-report-2025.pdf")).toBeInTheDocument();
  });

  it("reports a failed quality read with its correlation id", async () => {
    serveQualityRead(["failed"]);
    renderWithQuery(<Analytics />);
    await readFirstFiling();

    expect(
      await screen.findByText(/no index for this filing \(correlation id corr-q\)/, {}, { timeout: 4_000 }),
    ).toBeInTheDocument();
  });

  it("reports how much of the corpus was read end to end", async () => {
    renderWithQuery(<Analytics />);
    expect(await screen.findByText("Read end to end")).toBeInTheDocument();
    expect(screen.getByText("67% of the corpus")).toBeInTheDocument();
  });

  it("names the stages that failed rather than only counting them", async () => {
    renderWithQuery(<Analytics />);
    expect(await screen.findByText("✕ 1 failed")).toBeInTheDocument();
  });

  it("says nothing is stuck when no filing failed", async () => {
    server.use(
      http.get("http://localhost:8000/analytics", () =>
        HttpResponse.json({
          corpus: {
            document_count: 1,
            doc_key_count: 1,
            total_bytes: 1000,
            latest_ingested_at: null,
          },
          stages: [],
          documents_complete: 1,
          documents_failed: 0,
          chunk_count: 10,
        }),
      ),
    );
    renderWithQuery(<Analytics />);
    expect(await screen.findByText("Nothing is stuck")).toBeInTheDocument();
  });

  it("explains that extraction quality has to be read fresh", async () => {
    renderWithQuery(<Analytics />);
    expect(
      await screen.findByText(/results are not stored/),
    ).toBeInTheDocument();
  });

  it("explains the failure when the figures cannot be loaded", async () => {
    server.use(
      http.get("http://localhost:8000/analytics", () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );
    renderWithQuery(<Analytics />);
    expect(await screen.findByText(/database unavailable/)).toBeInTheDocument();
  });
});
