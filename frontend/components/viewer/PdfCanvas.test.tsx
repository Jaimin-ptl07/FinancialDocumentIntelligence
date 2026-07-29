import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({
    promise: Promise.resolve({
      numPages: 1,
      getPage: () =>
        Promise.resolve({
          getViewport: () => ({ width: 100, height: 100 }),
          render: () => ({ promise: Promise.resolve(), cancel: () => {} }),
        }),
    }),
  }),
}));

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
});
