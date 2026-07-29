import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Spine } from "./Spine";

describe("Spine", () => {
  it("names each destination by what the analyst does there", () => {
    render(<Spine />);
    expect(screen.getByRole("link", { name: "Filings" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.getByRole("link", { name: "Add a filing" })).toHaveAttribute(
      "href",
      "/ingest",
    );
    expect(
      screen.getByRole("link", { name: "Extraction schema" }),
    ).toHaveAttribute("href", "/schema");
    expect(screen.getByRole("link", { name: "Quality" })).toHaveAttribute(
      "href",
      "/analytics",
    );
    expect(screen.getByRole("link", { name: "Costing" })).toHaveAttribute(
      "href",
      "/costing",
    );
  });

  it("exposes the rail as a navigation landmark", () => {
    render(<Spine />);
    expect(screen.getByRole("navigation")).toBeInTheDocument();
  });

  it("marks the destination the analyst is currently on", () => {
    render(<Spine />);
    expect(screen.getByRole("link", { name: "Filings" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      screen.getByRole("link", { name: "Add a filing" }),
    ).not.toHaveAttribute("aria-current");
  });
});
