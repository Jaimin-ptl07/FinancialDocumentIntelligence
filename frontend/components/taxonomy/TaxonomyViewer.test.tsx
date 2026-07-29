import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import { TaxonomyViewer } from "./TaxonomyViewer";

const API = "http://localhost:8000";

function renderViewer(docId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TaxonomyViewer docId={docId} />
    </QueryClientProvider>,
  );
}

const bundle = {
  summary: {
    extracted: 2, derived_ok: 0, derived_unresolved: 0, ratios_ok: 0,
    checks_total: 0, checks_failed: 0,
    bands: { high: 2, needs_review: 0, low: 0, failed: 0 },
  },
  ratios: [],
  validations: [],
  coverage: {
    totals: { expected: 2, found_extracted: 2, derived_ok: 0,
              missing_extracted: 0, completeness_pct: 100 },
  },
  statements: [
    {
      key: "module_profit_and_loss", name: "Profit and Loss", statement: "profit_and_loss",
      tree: [
        { field_name: "Revenue from Sale of Products", tag: "RFSP",
          path: "profit_and_loss.PR.0", origin: "extracted", role: "line",
          value: 10, confidence: 0.9, band: "high", page: 4, children: [] },
      ],
    },
    {
      key: "module_assets", name: "Assets", statement: "assets",
      tree: [
        { field_name: "Cash on hand", tag: "COH", path: "assets.AS.0",
          origin: "extracted", role: "line", value: 5, confidence: 0.9,
          band: "high", page: 6, children: [] },
      ],
    },
  ],
  doc_key: "annual-report-2025.pdf",
};

describe("TaxonomyViewer", () => {
  it("shows the P&L statement by default and switches tabs", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json({
          id: "doc-1", checksum: "abc", filename: "annual-report-2025.pdf",
          byte_size: 100, doc_key: "annual-report-2025.pdf", version: 1,
          content_type: "application/pdf",
        }),
      ),
      http.post(`${API}/factsheet/document`, () => HttpResponse.json(bundle)),
    );
    const user = userEvent.setup();
    renderViewer("doc-1");

    expect(await screen.findByText("Revenue from Sale of Products")).toBeInTheDocument();
    expect(screen.queryByText("Cash on hand")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Assets" }));
    expect(await screen.findByText("Cash on hand")).toBeInTheDocument();
    expect(screen.queryByText("Revenue from Sale of Products")).not.toBeInTheDocument();
  });

  it("shows the completeness stat from AuditFactSheet", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json({
          id: "doc-1", checksum: "abc", filename: "annual-report-2025.pdf",
          byte_size: 100, doc_key: "annual-report-2025.pdf", version: 1,
          content_type: "application/pdf",
        }),
      ),
      http.post(`${API}/factsheet/document`, () => HttpResponse.json(bundle)),
    );
    renderViewer("doc-1");
    expect(await screen.findByText("100%")).toBeInTheDocument();
    expect(screen.getByText("Completeness")).toBeInTheDocument();
  });

  it("shows an inline error if the factsheet call fails", async () => {
    server.use(
      http.get(`${API}/documents/:id`, () =>
        HttpResponse.json({
          id: "doc-1", checksum: "abc", filename: "annual-report-2025.pdf",
          byte_size: 100, doc_key: "annual-report-2025.pdf", version: 1,
          content_type: "application/pdf",
        }),
      ),
      http.post(`${API}/factsheet/document`, () => HttpResponse.json(
        { detail: "this document's pipeline has not finished indexing yet -- retry later" },
        { status: 409 },
      )),
    );
    renderViewer("doc-1");
    expect(await screen.findByRole("alert")).toHaveTextContent(/not finished indexing/);
  });
});
