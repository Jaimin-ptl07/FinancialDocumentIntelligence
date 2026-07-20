import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { ExtractionSchema } from "@/lib/types";
import { server } from "@/test/setup";
import SchemaScreen from "./page";

const API = "http://localhost:8000";

/** Two modules, three retrieval units, four fields — small enough to assert
 *  on, shaped like the real 16/18/198 so filtering has something to hide. */
const schema: ExtractionSchema = {
  version: "3.1.0",
  title: "Field extraction schema",
  description: "Retrieve-once, extract-many.",
  modules: [
    {
      id: 3,
      key: "module_3_balance_sheet",
      name: "Balance Sheet",
      retrieval_unit_count: 1,
      retrieval_units: [
        {
          unit_id: "ru_bs_01",
          title: "Balance Sheet",
          scope: "primary_statement",
          statement_type: "BS",
          locator_keywords: ["balance sheet"],
          retrieval_query: "consolidated balance sheet total assets",
          field_count: 3,
          fields: [
            {
              field_name: "total_assets",
              field_path: "module_3_balance_sheet.total_assets",
              canonical_label: "Total assets",
              aliases: ["TOTAL ASSETS"],
              data_type: "decimal",
              description: "Total assets as stated in the Balance Sheet.",
              extraction_prompt:
                "Extract the total assets from the Balance Sheet.",
            },
            {
              field_name: "total_equity",
              field_path: "module_3_balance_sheet.total_equity",
              canonical_label: "Total equity",
              aliases: ["Shareholders' funds"],
              data_type: "decimal",
              description: "Total equity attributable to owners.",
              extraction_prompt: "Extract total equity.",
            },
            {
              field_name: "cash_and_equivalents",
              field_path: "module_3_balance_sheet.cash_and_equivalents",
              canonical_label: "Cash and cash equivalents",
              aliases: ["Cash at bank"],
              data_type: "decimal",
              description: "Cash and cash equivalents.",
              extraction_prompt: "Extract cash and cash equivalents.",
            },
          ],
        },
      ],
    },
    {
      id: 4,
      key: "module_4_profit_and_loss",
      name: "Statement of Profit and Loss",
      retrieval_unit_count: 1,
      retrieval_units: [
        {
          unit_id: "ru_pl_01",
          title: "Profit and Loss",
          scope: "primary_statement",
          statement_type: "PL",
          locator_keywords: ["profit and loss"],
          retrieval_query: "statement of profit and loss revenue",
          field_count: 1,
          fields: [
            {
              field_name: "revenue",
              field_path: "module_4_profit_and_loss.revenue",
              canonical_label: "Revenue from operations",
              aliases: ["Total income"],
              data_type: "decimal",
              description: "Revenue from operations.",
              extraction_prompt: "Extract revenue from operations.",
            },
          ],
        },
      ],
    },
  ],
};

function renderScreen() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SchemaScreen />
    </QueryClientProvider>,
  );
}

/** `GET|PUT /schema` return the document nested in an envelope carrying its
 *  counts and provenance, not the bare document. */
function view(doc: ExtractionSchema, source: "seed" | "store" = "seed") {
  const units = doc.modules.flatMap((m) => m.retrieval_units);
  return {
    version: doc.version,
    source,
    module_count: doc.modules.length,
    unit_count: units.length,
    field_count: units.reduce((n, u) => n + u.fields.length, 0),
    schema_document: doc,
  };
}

describe("SchemaScreen", () => {
  it("lists the schema's modules and fields against the loaded version", async () => {
    server.use(http.get(`${API}/schema`, () => HttpResponse.json(view(schema))));
    renderScreen();

    expect(
      await screen.findByRole("button", { name: "Total assets" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Balance Sheet/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Statement of Profit and Loss/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/v3\.1\.0/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("4 of 4 fields");
  });

  it("finds a field by an alias it never shows, and says how much is hidden", async () => {
    server.use(http.get(`${API}/schema`, () => HttpResponse.json(view(schema))));
    renderScreen();
    await screen.findByRole("button", { name: "Total assets" });

    await userEvent.type(screen.getByLabelText("Find a field"), "income");

    expect(
      screen.getByRole("button", { name: "Revenue from operations" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Total assets" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /Balance Sheet/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 of 4 fields");
  });

  it("saves an edited prompt against the version it loaded", async () => {
    let sent: { base_version: string; schema_document: ExtractionSchema } | null =
      null;
    server.use(
      http.get(`${API}/schema`, () => HttpResponse.json(view(schema))),
      http.put(`${API}/schema`, async ({ request }) => {
        sent = (await request.json()) as typeof sent;
        return HttpResponse.json(
          view({ ...sent!.schema_document, version: "3.1.1" }, "store"),
        );
      }),
    );
    renderScreen();

    const save = await screen.findByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();

    await userEvent.click(
      await screen.findByRole("button", { name: "Total assets" }),
    );
    await userEvent.type(
      screen.getByLabelText("Extraction prompt"),
      " Return null if absent.",
    );

    expect(save).toBeEnabled();
    await userEvent.click(save);

    expect(await screen.findByText(/v3\.1\.1/)).toBeInTheDocument();
    expect(sent!.base_version).toBe("3.1.0");
    expect(
      sent!.schema_document.modules[0].retrieval_units[0].fields[0].extraction_prompt,
    ).toBe(
      "Extract the total assets from the Balance Sheet. Return null if absent.",
    );
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("surfaces a rejected save without discarding the edit", async () => {
    server.use(
      http.get(`${API}/schema`, () => HttpResponse.json(view(schema))),
      http.put(`${API}/schema`, () =>
        HttpResponse.json(
          { detail: "schema changed under you (base_version 3.1.0)" },
          { status: 409 },
        ),
      ),
    );
    renderScreen();

    await userEvent.click(
      await screen.findByRole("button", { name: "Total assets" }),
    );
    await userEvent.type(screen.getByLabelText("Normalization"), "lakhs");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText(/schema changed under you/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Normalization")).toHaveValue("lakhs");
  });

  it("reports a schema it could not load", async () => {
    server.use(
      http.get(`${API}/schema`, () =>
        HttpResponse.json({ detail: "schema file is missing" }, { status: 500 }),
      ),
    );
    renderScreen();

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/schema file is missing/)).toBeInTheDocument();
  });
});
