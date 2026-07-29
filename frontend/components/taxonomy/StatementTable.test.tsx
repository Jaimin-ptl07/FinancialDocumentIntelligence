import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TaxonomyTreeNode } from "@/lib/types";
import { StatementTable } from "./StatementTable";

const tree: TaxonomyTreeNode[] = [
  {
    field_name: "Raw materials",
    tag: "RM",
    path: "assets.AS.0",
    origin: "derived",
    role: "line",
    value: 7,
    confidence: 0.8,
    band: "high",
    page: null,
    children: [
      {
        field_name: "Raw materials - Imported",
        tag: "IMP",
        path: "assets.AS.0.0",
        origin: "extracted",
        role: "line",
        value: { current: 3, prior: 2 },
        confidence: 0.9,
        band: "high",
        page: 12,
        children: [],
      },
      {
        field_name: "Raw materials - Indigenous",
        tag: "IND",
        path: "assets.AS.0.1",
        origin: "extracted",
        role: "line",
        value: { current: 4, prior: 5 },
        confidence: 0.4,
        band: "needs_review",
        page: 12,
        children: [],
      },
    ],
  },
  {
    field_name: "Not extracted this run",
    tag: "MISSING",
    path: "assets.AS.1",
    origin: "extracted",
    role: "line",
    value: null,
    confidence: null,
    band: null,
    page: null,
    children: [],
  },
];

describe("StatementTable", () => {
  it("shows a parent row and its depth-1/2 children by default", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    expect(screen.getByText("Raw materials")).toBeInTheDocument();
    // Depth-1 children of a depth-0 root stay visible under the default
    // expand-to-depth-2 rule (see plan Task 6, Step 3).
    expect(screen.getByText("Raw materials - Imported")).toBeInTheDocument();
    expect(screen.getByText("Raw materials - Indigenous")).toBeInTheDocument();
  });

  it("collapses and re-expands a subtree on toggle", async () => {
    const user = userEvent.setup();
    render(<StatementTable tree={tree} docId="doc-1" />);
    const toggle = screen.getByRole("button", { name: "Collapse Raw materials" });
    await user.click(toggle);
    expect(screen.queryByText("Raw materials - Imported")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand Raw materials" }));
    expect(screen.getByText("Raw materials - Imported")).toBeInTheDocument();
  });

  it("renders current/prior/delta for a dual-period value", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    const row = screen.getByText("Raw materials - Imported").closest("tr")!;
    expect(within(row).getByText("3.00")).toBeInTheDocument();
    expect(within(row).getByText("2.00")).toBeInTheDocument();
    expect(within(row).getByText("50.0%")).toBeInTheDocument(); // (3-2)/2
  });

  it("shows an em-dash for a tag with no value", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    const row = screen.getByText("Not extracted this run").closest("tr")!;
    // A missing, non-dual-period tagged value legitimately renders "—" in
    // more than one cell (Current, and Prior/Δ% which are always "—" for a
    // non-dual-period field per StatementRow) -- assert presence, not count.
    expect(within(row).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("filters rows by particulars or tag, pruning empty branches", async () => {
    const user = userEvent.setup();
    render(<StatementTable tree={tree} docId="doc-1" />);
    await user.type(screen.getByPlaceholderText("Find a line item or tag"), "IND");
    expect(screen.getByText("Raw materials - Indigenous")).toBeInTheDocument();
    expect(screen.queryByText("Raw materials - Imported")).not.toBeInTheDocument();
  });

  it("opens the source document in a new tab, keyed by tag, from a provenance link", async () => {
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<StatementTable tree={tree} docId="doc-1" />);
    await user.click(
      screen.getByRole("button", {
        name: "Open evidence for Raw materials - Imported on page 12",
      }),
    );
    expect(openSpy).toHaveBeenCalledWith("/documents/doc-1?tag=IMP", "_blank");
    openSpy.mockRestore();
  });

  it("gives a derived node no provenance link", () => {
    render(<StatementTable tree={tree} docId="doc-1" />);
    const row = screen.getByText("Raw materials").closest("tr")!;
    expect(within(row).queryByRole("button", { name: /Open evidence/ })).not.toBeInTheDocument();
  });
});
