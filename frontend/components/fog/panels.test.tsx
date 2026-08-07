import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FogFact } from "@/lib/fog";
import { FactsPanel } from "./FactsPanel";
import { AttentionPanel } from "./AttentionPanel";
import { HealthPanel } from "./HealthPanel";

describe("FactsPanel", () => {
  const facts = [
    { tag: "PBTR", value: 65487, method: "lexical:section:name", page: 167, label: "PROFIT BEFORE TAX" },
    { tag: "CT", value: 16388, method: "llm", page: 167, label: "Current tax" },
  ];

  it("shows the tag, the value and HOW it was resolved", () => {
    render(<FactsPanel facts={facts} selected={null} onSelect={() => {}} />);
    expect(screen.getByText("PBTR")).toBeInTheDocument();
    expect(screen.getByText(/65,487/)).toBeInTheDocument();
    expect(screen.getByText(/section/)).toBeInTheDocument();
  });

  it("reports a fact whose proof is missing rather than hiding it", () => {
    render(<FactsPanel facts={[{ ...facts[0], method: "" }]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/no proof recorded/i)).toBeInTheDocument();
  });

  it("calls back with the fact so the viewer can navigate to its page", async () => {
    const onSelect = vi.fn();
    render(<FactsPanel facts={facts} selected={null} onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /PBTR/ }));
    expect(onSelect).toHaveBeenCalledWith(facts[0]);
  });

  it("says so when there is nothing rather than rendering an empty box", () => {
    render(<FactsPanel facts={[]} selected={null} onSelect={() => {}} />);
    expect(screen.getByText(/no facts/i)).toBeInTheDocument();
  });
});

describe("AttentionPanel", () => {
  it("names the winner and the losers of a contested tag", () => {
    render(
      <AttentionPanel
        contested={[{ tag: "RGHTUA", spread: 11136, winnerLabel: "Right-of-use assets",
          winnerValue: 11027, losers: [{ label: "Payment for right-of-use assets", value: -109 }] }]}
        unmapped={[]}
      />,
    );
    expect(screen.getByText("RGHTUA")).toBeInTheDocument();
    expect(screen.getByText(/Right-of-use assets/)).toBeInTheDocument();
    expect(screen.getByText(/Payment for right-of-use assets/)).toBeInTheDocument();
  });

  it("distinguishes an outage from an honest refusal", () => {
    render(
      <AttentionPanel
        contested={[]}
        unmapped={[
          { reason: "model_call_failed", labels: ["Travel"] },
          { reason: "model_said_unmapped", labels: ["Facility expenses"] },
        ]}
      />,
    );
    expect(screen.getByText(/call failed/i)).toBeInTheDocument();
    expect(screen.getByText(/declined to map/i)).toBeInTheDocument();
  });

  it("says nothing needs attention when nothing does", () => {
    render(<AttentionPanel contested={[]} unmapped={[]} />);
    expect(screen.getByText(/nothing contested/i)).toBeInTheDocument();
  });
});

describe("HealthPanel", () => {
  const health = {
    footing: [
      { statement: "profit_and_loss", passed: true, rule: "income - expenses = pbt" },
      { statement: "balance_sheet", passed: false, rule: "equity + liabilities = total" },
      { statement: "cash_flow", passed: null, rule: "" },
    ],
    checks: [], coverage: {}, mapping: { llm_calls: 35 }, schemaUsable: true,
  };

  it("marks a passing face and flags a failing one", () => {
    render(<HealthPanel health={health} />);
    expect(screen.getByText("profit_and_loss").closest("li")).toHaveTextContent("✓");
    expect(screen.getByText("balance_sheet").closest("li")).toHaveTextContent("⚑");
  });

  it("renders NO verdict for a face it could not check", () => {
    render(<HealthPanel health={health} />);
    const cf = screen.getByText("cash_flow").closest("li");
    expect(cf).not.toHaveTextContent("✓");
    expect(cf).not.toHaveTextContent("⚑");
    expect(cf).toHaveTextContent(/not checked/i);
  });

  it("shouts when the schema is unusable — it maps nothing, silently", () => {
    render(<HealthPanel health={{ ...health, schemaUsable: false }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/schema/i);
  });

  it("shows no alert when the schema is fine", () => {
    render(<HealthPanel health={health} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("FactsPanel evidence tiers mirror the backend", () => {
  const fact = (over: Partial<FogFact>): FogFact => ({
    tag: "T", value: 1, method: "lexical:name", page: 1, label: "L", score: 1, ...over,
  });

  const weightOf = (f: FogFact) => {
    const { unmount } = render(
      <FactsPanel facts={[f]} selected={null} onSelect={() => {}} />,
    );
    const el = screen.getByText(f.method) as HTMLElement;
    const weight = Number(el.style.fontWeight);
    unmount();
    return weight;
  };

  it("ranks an EXACT lexical hit as strong, not as a guess", () => {
    // `lexical_match` returns "name" for both 1.0 and <= 0.9; only the score
    // separates them, and ranking on the string alone filed every exact match
    // under the weakest evidence there is.
    expect(weightOf(fact({ method: "lexical:name", score: 1 })))
      .toBeGreaterThan(weightOf(fact({ method: "lexical:name", score: 0.71 })));
  });

  it("ranks a section-confirmed hit as strong even at a fuzzy score", () => {
    expect(weightOf(fact({ method: "lexical:section:name", score: 0.9 })))
      .toBeGreaterThan(weightOf(fact({ method: "lexical:name", score: 0.9 })));
  });

  it("ranks the model above a bare token overlap", () => {
    expect(weightOf(fact({ method: "llm", score: 0 })))
      .toBeGreaterThan(weightOf(fact({ method: "lexical:name", score: 0.7 })));
  });
});
