import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearSession, login } from "@/lib/auth";
import { server } from "@/test/setup";
import { PdfCanvas } from "./PdfCanvas";

// jsdom has no ResizeObserver -- PdfCanvas's fit-to-width effect needs one to
// measure its container, unrelated to what this suite is testing (box
// rendering/tagging), so a no-op stub is enough.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

// jsdom also has no scrollIntoView implementation -- PdfCanvas calls it on
// the current box to bring it into view, also unrelated to box tagging.
Element.prototype.scrollIntoView = vi.fn();

const getDocument = vi.hoisted(() => vi.fn());
vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument,
}));
getDocument.mockImplementation(() => ({
  promise: Promise.resolve({
    numPages: 1,
    getPage: () =>
      Promise.resolve({
        getViewport: () => ({ width: 100, height: 100 }),
        render: () => ({ promise: Promise.resolve(), cancel: () => {} }),
      }),
  }),
}));

afterEach(() => clearSession());

const region = (x: number) => ({ page_number: 1, polygon: [x, 0, x + 1, 0, x + 1, 1, x, 1] });

describe("PdfCanvas", () => {
  it("tags a prior-period box distinctly from the current one", async () => {
    render(
      <PdfCanvas
        url="http://example.com/doc.pdf"
        page={1}
        regions={{ current: [region(0)], prior: [region(5)] }}
      />,
    );
    expect(await screen.findByText("New")).toBeInTheDocument();
    expect(screen.getByText("Prior")).toBeInTheDocument();
  });

  it("shows only a current box, no tag, when there is no prior region", async () => {
    render(
      <PdfCanvas
        url="http://example.com/doc.pdf"
        page={1}
        regions={{ current: [region(0)], prior: [] }}
      />,
    );
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("New")).not.toBeInTheDocument();
    expect(screen.queryByText("Prior")).not.toBeInTheDocument();
  });

  it("loads the gated PDF with the session's bearer token", async () => {
    server.use(
      http.post("http://localhost:8000/auth/login", () =>
        HttpResponse.json({ access_token: "tok", token_type: "bearer", expires_in: 900 }),
      ),
    );
    await login("ana@example.com", "pw");

    render(<PdfCanvas url="http://example.com/doc.pdf" page={1} regions={null} />);

    await vi.waitFor(() =>
      expect(getDocument).toHaveBeenCalledWith({
        url: "http://example.com/doc.pdf",
        httpHeaders: { Authorization: "Bearer tok" },
      }),
    );
  });
});
