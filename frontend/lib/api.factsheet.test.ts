import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import { factsheetDocument } from "./api";

const API = "http://localhost:8000";

describe("factsheetDocument", () => {
  it("posts doc_key and returns the parsed bundle", async () => {
    server.use(
      http.post(`${API}/factsheet/document`, async ({ request }) => {
        const body = (await request.json()) as { doc_key: string; force: boolean };
        expect(body).toEqual({ doc_key: "annual-report-2025.pdf", force: false });
        return HttpResponse.json({
          summary: {
            extracted: 1, derived_ok: 0, derived_unresolved: 0, ratios_ok: 0,
            checks_total: 0, checks_failed: 0,
            bands: { high: 1, needs_review: 0, low: 0, failed: 0 },
          },
          ratios: [],
          validations: [],
          coverage: {
            totals: { expected: 1, found_extracted: 1, derived_ok: 0,
                      missing_extracted: 0, completeness_pct: 100 },
          },
          statements: [
            { key: "module_assets", name: "Assets", statement: "assets", tree: [] },
          ],
          doc_key: "annual-report-2025.pdf",
        });
      }),
    );

    const result = await factsheetDocument("annual-report-2025.pdf");
    expect(result.doc_key).toBe("annual-report-2025.pdf");
    expect(result.statements[0].name).toBe("Assets");
  });
});
