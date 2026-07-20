import { describe, expect, it } from "vitest";
import {
  formatAmount,
  formatBytes,
  formatConfidence,
  formatPercent,
  inkOpacity,
} from "./format";

describe("formatAmount", () => {
  it("groups an integer in the Indian numbering system", () => {
    expect(formatAmount(124850)).toBe("1,24,850.00");
  });

  it("renders a null value as an em dash", () => {
    expect(formatAmount(null)).toBe("—");
  });

  it("passes a non-numeric value through unchanged", () => {
    expect(formatAmount("Reliance Industries Limited")).toBe(
      "Reliance Industries Limited",
    );
  });
});

describe("inkOpacity", () => {
  it("renders a fully confident value at full ink", () => {
    expect(inkOpacity(1)).toBe(1);
  });

  it("renders a zero-confidence value at the floor, still legible", () => {
    expect(inkOpacity(0)).toBe(0.45);
  });

  it("clamps a confidence outside 0..1", () => {
    expect(inkOpacity(-3)).toBe(0.45);
    expect(inkOpacity(9)).toBe(1);
  });
});

describe("formatConfidence", () => {
  it("renders two decimal places", () => {
    expect(formatConfidence(0.9234)).toBe("0.92");
  });
});

describe("formatBytes", () => {
  it("reports a multi-megabyte filing in MB", () => {
    expect(formatBytes(4_200_000)).toBe("4.0 MB");
  });

  it("reports a missing size as an em dash", () => {
    expect(formatBytes(null)).toBe("—");
  });
});

describe("formatPercent", () => {
  it("rounds to a whole percent", () => {
    expect(formatPercent(0.876)).toBe("88%");
  });
});
