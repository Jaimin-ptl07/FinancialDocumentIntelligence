import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/setup";
import Ingest from "./page";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const pdf = () =>
  new File([new Uint8Array([37, 80, 68, 70])], "report.pdf", {
    type: "application/pdf",
  });

const uploaded = {
  id: "doc-9",
  checksum: "c",
  filename: "report.pdf",
  byte_size: 4,
  doc_key: "report.pdf",
  version: 1,
  content_type: "application/pdf",
};

describe("Ingest", () => {
  it("starts the pipeline once the filing is uploaded", async () => {
    server.use(
      http.post("http://localhost:8000/documents", () =>
        HttpResponse.json(uploaded, { status: 201 }),
      ),
      http.post("http://localhost:8000/documents/doc-9/ocr", () =>
        HttpResponse.json({ doc_id: "doc-9", stage: "ocr" }, { status: 202 }),
      ),
    );

    renderWithQuery(<Ingest />);
    await userEvent.upload(screen.getByLabelText("Annual report PDF"), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Start reading" }));

    expect(
      await screen.findByRole("link", { name: "Open the filing" }),
    ).toHaveAttribute("href", "/documents/doc-9");
  });

  it("reports the stage that failed and what it said", async () => {
    server.use(
      http.post("http://localhost:8000/documents", () =>
        HttpResponse.json(uploaded, { status: 201 }),
      ),
      http.post("http://localhost:8000/documents/doc-9/ocr", () =>
        HttpResponse.json({ doc_id: "doc-9", stage: "ocr" }, { status: 202 }),
      ),
      http.get("http://localhost:8000/documents/doc-9/stages", () =>
        HttpResponse.json({
          stages: [
            { name: "ocr", status: "failed", error: "Azure returned 429" },
          ],
        }),
      ),
    );

    renderWithQuery(<Ingest />);
    await userEvent.upload(screen.getByLabelText("Annual report PDF"), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Start reading" }));

    expect(await screen.findByText(/Azure returned 429/)).toBeInTheDocument();
  });

  it("says what went wrong when the upload is rejected", async () => {
    server.use(
      http.post("http://localhost:8000/documents", () =>
        HttpResponse.json({ detail: "file exceeds 50 MB" }, { status: 413 }),
      ),
    );

    renderWithQuery(<Ingest />);
    await userEvent.upload(screen.getByLabelText("Annual report PDF"), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Start reading" }));

    expect(await screen.findByText(/file exceeds 50 MB/)).toBeInTheDocument();
  });

  it("cannot start reading before a file is chosen", () => {
    renderWithQuery(<Ingest />);
    expect(screen.getByRole("button", { name: "Start reading" })).toBeDisabled();
  });
});
