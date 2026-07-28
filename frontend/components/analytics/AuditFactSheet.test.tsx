import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AuditFactSheet, type FactSheetBundle } from "./AuditFactSheet";

const bundle = (over: Partial<FactSheetBundle> = {}): FactSheetBundle => ({
  summary: {
    extracted: 400,
    derived_ok: 6,
    derived_unresolved: 73,
    ratios_ok: 1,
    checks_total: 2,
    checks_failed: 1,
    bands: { high: 300, needs_review: 80, low: 20, failed: 1 },
  },
  ratios: [
    {
      key: "current_ratio",
      label: "Current Ratio",
      value: 1.8,
      status: "ok",
      caro_clause: "xix / Schedule III",
      explanation: "TOTCAR / TOTCLR",
    },
    {
      key: "debt_equity",
      label: "Debt-Equity Ratio",
      value: null,
      status: "missing_inputs",
      caro_clause: "xix / Schedule III",
      explanation: "TOTBOR / TOTEQNR",
    },
  ],
  validations: [
    {
      rule_id: "recon_current_assets",
      kind: "reconciliation",
      expected: 100,
      actual: 100,
      delta: 0,
      severity: "error",
      passed: true,
    },
    {
      rule_id: "identity_balance_sheet",
      kind: "identity",
      expected: 300,
      actual: 288,
      delta: 12,
      severity: "warn",
      passed: false,
    },
  ],
  coverage: {
    totals: {
      expected: 508,
      found_extracted: 400,
      derived_ok: 6,
      missing_extracted: 102,
      completeness_pct: 79.9,
    },
  },
  ...over,
});

describe("AuditFactSheet", () => {
  it("shows completeness and coverage stats", () => {
    render(<AuditFactSheet data={bundle()} />);
    expect(screen.getByText("79.9%")).toBeInTheDocument();
    expect(screen.getByText("Completeness")).toBeInTheDocument();
  });

  it("renders a computed ratio and marks a missing one", () => {
    render(<AuditFactSheet data={bundle()} />);
    expect(screen.getByText("1.80")).toBeInTheDocument();
    expect(screen.getByText("— (missing_inputs)")).toBeInTheDocument();
  });

  it("flags a failed check with the ! glyph, ticks a passing one", () => {
    render(<AuditFactSheet data={bundle()} />);
    expect(screen.getByText(/! identity_balance_sheet/)).toBeInTheDocument();
    expect(screen.getByText(/✓ recon_current_assets/)).toBeInTheDocument();
  });

  it("renders nothing-to-check gracefully", () => {
    render(<AuditFactSheet data={bundle({ validations: [] })} />);
    expect(screen.getByText("No checks applicable.")).toBeInTheDocument();
  });
});
