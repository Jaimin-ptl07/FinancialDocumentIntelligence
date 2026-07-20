import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import Analytics from "./page";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("Analytics", () => {
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
