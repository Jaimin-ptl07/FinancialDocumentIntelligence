import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import TaxonomyPickerPage from "./page";

const API = "http://localhost:8000";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TaxonomyPickerPage />
    </QueryClientProvider>,
  );
}

const doc = (over: Record<string, unknown> = {}) => ({
  id: "doc-1",
  checksum: "abc123",
  filename: "annual-report-2025.pdf",
  byte_size: 100,
  doc_key: "annual-report-2025.pdf",
  version: 1,
  content_type: "application/pdf",
  stage_counts: { done: 15 },
  ...over,
});

describe("TaxonomyPickerPage", () => {
  it("lists only filings whose pipeline has fully completed", async () => {
    server.use(
      http.get(`${API}/documents`, () =>
        HttpResponse.json({
          documents: [
            doc(),
            doc({ id: "doc-2", filename: "half-done.pdf", stage_counts: { done: 3 } }),
          ],
          total: 2, limit: 50, offset: 0,
        }),
      ),
    );
    renderPage();
    expect(await screen.findByRole("link", { name: /annual-report-2025\.pdf/ })).toHaveAttribute(
      "href",
      "/taxonomy/doc-1",
    );
    expect(screen.queryByText("half-done.pdf")).not.toBeInTheDocument();
  });

  it("shows an empty state when no filing has completed its pipeline", async () => {
    server.use(
      http.get(`${API}/documents`, () =>
        HttpResponse.json({ documents: [], total: 0, limit: 50, offset: 0 }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/No filings are ready yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Filings" })).toHaveAttribute("href", "/");
  });
});
