import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { FogViewer } from "./FogViewer";

vi.mock("@/lib/api", () => ({
  getDocument: vi.fn(async () => ({
    id: "doc-1", filename: "tcs.pdf", doc_key: "tcs", version: 1,
    checksum: "a", byte_size: 1, content_type: "application/pdf",
  })),
  pdfUrl: () => "/x.pdf",
  getArtifact: vi.fn(),
}));
vi.mock("@/components/viewer/PdfCanvas", () => ({
  PdfCanvas: ({ page }: { page: number }) => <div data-testid="pdf">page {page}</div>,
}));

const REASON = {
  bases: { consolidated: { facts: { PBTR: 65487 }, contested: [], checks: [], coverage: {} } },
};
const MAPPING = {
  schema: { usable: true },
  diagnostics: { consolidated: { llm_calls: 35 } },
  bases: { consolidated: { proof: [{ line_item: "li:a", tag: "PBTR",
    label: "PROFIT BEFORE TAX", method: "lexical:name", score: 1 }], unmapped: [] } },
};
const FOG = {
  bases: { consolidated: { statements: [], notes: [], edges: [],
    line_items: [{ id: "li:a", label: "PROFIT BEFORE TAX", current: 65487, page: 167 }] } },
};

// Same setup as ReportViewer.test.tsx: `retry: false` is load-bearing --
// without it an "artifact missing" assertion HANGS instead of failing.
function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

async function mount(over: Record<string, unknown> = {}) {
  const { getArtifact } = await import("@/lib/api");
  (getArtifact as ReturnType<typeof vi.fn>).mockImplementation(
    async (_id: string, stage: string) => {
      if (stage in over) {
        const v = over[stage];
        if (v instanceof Error) throw v;
        return v;
      }
      return ({ fog_objects: FOG, fog_mapping: MAPPING, fog_reason: REASON } as
        Record<string, unknown>)[stage];
    },
  );
  render(<FogViewer docId="doc-1" />, { wrapper: makeWrapper() });
}

describe("FogViewer", () => {
  it("names the basis it is showing", async () => {
    await mount();
    expect(await screen.findByText(/consolidated/i)).toBeInTheDocument();
  });

  it("names the stage that has not run when an artifact is missing", async () => {
    await mount({ fog_reason: new Error("404") });
    expect(await screen.findByText(/fog_reason/)).toBeInTheDocument();
    expect(screen.getByText(/not built/i)).toBeInTheDocument();
  });

  it("still renders the facts when a DIFFERENT artifact is malformed", async () => {
    await mount({ fog_mapping: "nonsense" });
    expect(await screen.findByText("PBTR")).toBeInTheDocument();
  });

  it("navigates the PDF to a fact page when the fact is clicked", async () => {
    await mount();
    await userEvent.click(await screen.findByRole("button", { name: /PBTR/ }));
    expect(screen.getByTestId("pdf")).toHaveTextContent("page 167");
  });

  it("refuses to guess a basis when the filing carries none", async () => {
    await mount({ fog_reason: { bases: {} } });
    expect(await screen.findByText(/no basis/i)).toBeInTheDocument();
  });
});
