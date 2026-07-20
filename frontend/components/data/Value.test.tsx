import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExtractFieldView } from "@/lib/types";
import { Value } from "./Value";

const field = (over: Partial<ExtractFieldView> = {}): ExtractFieldView => ({
  field_name: "total_assets",
  value: 124850,
  source: {
    page: 84,
    snippet: "Total assets 1,24,850",
    evidence_ids: ["doc-1:t3r7"],
    bounding_regions: null,
  },
  confidence: 0.92,
  ...over,
});

describe("Value", () => {
  it("renders the figure grouped for an Indian filing", () => {
    render(<Value field={field()} selected={false} onSelect={vi.fn()} />);
    expect(screen.getByText("1,24,850.00")).toBeInTheDocument();
  });

  it("fades a low-confidence figure — confidence is ink density", () => {
    const { rerender } = render(
      <Value
        field={field({ confidence: 1 })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("1,24,850.00")).toHaveStyle({ opacity: "1" });

    rerender(
      <Value
        field={field({ confidence: 0 })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("1,24,850.00")).toHaveStyle({ opacity: "0.45" });
  });

  it("offers the page reference as the way into the source", async () => {
    const onSelect = vi.fn();
    render(<Value field={field()} selected={false} onSelect={onSelect} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Show total_assets on page 84" }),
    );
    expect(onSelect).toHaveBeenCalledWith("total_assets");
  });

  it("says plainly when a figure was not found", () => {
    render(
      <Value
        field={field({ value: null, confidence: 0 })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Not found")).toBeInTheDocument();
  });

  it("offers no way into the source when the model reported no page", () => {
    render(
      <Value
        field={field({
          source: {
            page: null,
            snippet: null,
            evidence_ids: [],
            bounding_regions: null,
          },
        })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
