import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import Dashboard from "./page";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("Dashboard", () => {
  it("lists each filing by the name the analyst uploaded", async () => {
    renderWithQuery(<Dashboard />);
    expect(
      await screen.findByRole("link", { name: /annual-report-2025\.pdf/ }),
    ).toHaveAttribute("href", "/documents/doc-1");
  });

  it("invites the first upload when there are no filings yet", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({ documents: [], total: 0 }),
      ),
    );
    renderWithQuery(<Dashboard />);
    expect(await screen.findByText("No filings yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a filing" })).toHaveAttribute(
      "href",
      "/ingest",
    );
  });

  it("says a stage failed rather than only showing a short count", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({
          documents: [
            {
              id: "doc-2",
              checksum: "d",
              filename: "hdfc-2024.pdf",
              byte_size: 900_000,
              doc_key: "hdfc-2024.pdf",
              version: 2,
              content_type: "application/pdf",
              stage_counts: { done: 11, failed: 1 },
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        }),
      ),
    );
    renderWithQuery(<Dashboard />);
    expect(await screen.findByText("✕ 1 failed")).toBeInTheDocument();
    expect(screen.getByText("11/15")).toBeInTheDocument();
  });

  it("explains the failure when the filings cannot be loaded", async () => {
    server.use(
      http.get("http://localhost:8000/documents", () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 503 }),
      ),
    );
    renderWithQuery(<Dashboard />);
    expect(await screen.findByText(/database unavailable/)).toBeInTheDocument();
  });
});
