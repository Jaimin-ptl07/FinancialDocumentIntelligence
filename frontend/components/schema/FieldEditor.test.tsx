import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { SchemaField } from "@/lib/types";
import { FieldEditor } from "./FieldEditor";

const field: SchemaField = {
  field_name: "total_assets",
  field_path: "module_3_balance_sheet.total_assets",
  canonical_label: "Total assets",
  aliases: ["Total assets", "TOTAL ASSETS"],
  data_type: "decimal",
  description: "Total assets as stated in the Balance Sheet.",
  extraction_prompt: "Extract the total assets from the Balance Sheet.",
};

/** FieldEditor is controlled: it renders what it is handed and reports edits
 *  upward. Testing it against a spy alone would freeze the value prop and let
 *  React undo every keystroke, so the harness closes the loop the way the
 *  schema screen does — state in, patch out, spy in the middle. */
function Harness({ onChange }: { onChange: (next: SchemaField) => void }) {
  const [current, setCurrent] = useState(field);
  return (
    <FieldEditor
      field={current}
      onChange={(next) => {
        setCurrent(next);
        onChange(next);
      }}
    />
  );
}

describe("FieldEditor", () => {
  it("reports the edited prompt to its parent", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    await userEvent.type(
      screen.getByLabelText("Extraction prompt"),
      " Return null if absent.",
    );

    expect(onChange).toHaveBeenCalled();
    const last = onChange.mock.calls.at(-1)![0] as SchemaField;
    expect(last.extraction_prompt).toBe(
      "Extract the total assets from the Balance Sheet. Return null if absent.",
    );
  });

  it("edits aliases as one per line, dropping blank lines", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const aliases = screen.getByLabelText("Aliases");
    await userEvent.clear(aliases);
    await userEvent.type(aliases, "Total assets\n\nSum of assets");

    const last = onChange.mock.calls.at(-1)![0] as SchemaField;
    expect(last.aliases).toEqual(["Total assets", "Sum of assets"]);
  });

  it("shows the field's identity as data, not as prose", () => {
    render(<FieldEditor field={field} onChange={vi.fn()} />);
    expect(
      screen.getByText("module_3_balance_sheet.total_assets"),
    ).toBeInTheDocument();
  });

  it("keeps the editable knobs reachable by their labels", () => {
    render(<FieldEditor field={field} onChange={vi.fn()} />);
    expect(screen.getByLabelText("Data type")).toHaveValue("decimal");
    expect(screen.getByLabelText("Normalization")).toHaveValue("");
    expect(screen.getByLabelText("Description")).toHaveValue(
      "Total assets as stated in the Balance Sheet.",
    );
  });
});
