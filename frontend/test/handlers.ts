import { http, HttpResponse } from "msw";

const API = "http://localhost:8000";

export const handlers = [
  // No refresh cookie by default: a test that wants a session logs in (or
  // overrides this) itself. Anything that probes for one -- the PDF loader,
  // a 401 retry -- gets the anonymous answer instead of an unhandled request.
  http.post(`${API}/auth/refresh`, () =>
    HttpResponse.json({ detail: "No refresh token was presented." }, { status: 401 }),
  ),

  http.get(`${API}/documents`, () =>
    HttpResponse.json({
      documents: [
        {
          id: "doc-1",
          checksum: "abc123",
          filename: "annual-report-2025.pdf",
          byte_size: 4_200_000,
          doc_key: "annual-report-2025.pdf",
          version: 1,
          content_type: "application/pdf",
          stage_counts: { done: 15 },
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    }),
  ),

  http.get(`${API}/documents/:docId`, ({ params }) =>
    HttpResponse.json({
      id: params.docId,
      checksum: "abc123",
      filename: "annual-report-2025.pdf",
      byte_size: 4_200_000,
      doc_key: "annual-report-2025.pdf",
      version: 1,
      content_type: "application/pdf",
    }),
  ),

  http.get(`${API}/documents/:docId/stages`, () =>
    HttpResponse.json({
      stages: [
        { name: "ocr", status: "done", error: null },
        { name: "ast", status: "running", error: null },
      ],
    }),
  ),

  http.get(`${API}/analytics`, () =>
    HttpResponse.json({
      corpus: {
        document_count: 3,
        doc_key_count: 2,
        total_bytes: 12_600_000,
        latest_ingested_at: "2026-07-18T09:12:00Z",
      },
      stages: [
        {
          name: "ocr",
          done: 3,
          running: 0,
          queued: 0,
          failed: 0,
          median_seconds: 42.5,
        },
        {
          name: "index",
          done: 2,
          running: 0,
          queued: 0,
          failed: 1,
          median_seconds: 3.1,
        },
      ],
      documents_complete: 2,
      documents_failed: 1,
      chunk_count: 1840,
    }),
  ),

  http.get(`${API}/schema`, () =>
    HttpResponse.json({
      version: "3.1.0",
      source: "seed",
      module_count: 1,
      unit_count: 1,
      field_count: 1,
      schema_document: {
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
              field_count: 1,
              fields: [
                {
                  field_name: "total_assets",
                  field_path: "module_3_balance_sheet.total_assets",
                  canonical_label: "Total assets",
                  aliases: ["Total assets", "TOTAL ASSETS"],
                  data_type: "decimal",
                  description: "Total assets as stated in the Balance Sheet.",
                  extraction_prompt:
                    "Extract the total assets from the Balance Sheet.",
                },
              ],
            },
          ],
          },
        ],
      },
    }),
  ),
];
