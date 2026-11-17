import { screen } from "@testing-library/react";
import type { CostNight } from "../../api/types/agents";
import CostNights from "./CostNights";
import { json, render, stubFetch } from "../../test-utils";

function night(day: string, usd: number, over: Partial<CostNight> = {}): CostNight {
  return {
    night: day,
    total: { usd, sessions: 10 },
    kinds: {
      analyst: { sessions: 8, turns: 30, usd: usd * 0.6, unit: "role analysed", per_unit_usd: 0.126 },
      writer: { sessions: 2, turns: 8, usd: usd * 0.4, unit: "packet", per_unit_usd: 0.8 },
      scout: { sessions: 1, turns: 4, usd: 0.2 },
    },
    roles: { analysed: 50, ready: 5, usd_per_ready: usd / 5, gated_usd: 20, gated_roles: 40 },
    ...over,
  };
}

describe("CostNights", () => {
  it("shows last night's total, each kind's cost and the nights' trend", async () => {
    const nights = [night("2026-10-18", 100), night("2026-10-17", 50)];
    stubFetch(() => json(nights));
    render(<CostNights />);
    expect(await screen.findByRole("heading", { name: /Night cost/ })).toHaveTextContent("$100.00");
    expect(screen.getByText("Researcher").closest("li")).toHaveTextContent("$60.00");
    expect(screen.getByText("$0.13 per role analysed")).toBeInTheDocument();
    expect(screen.getByText("$0.80 per packet")).toBeInTheDocument();
    expect(screen.getByText("1 sessions")).toBeInTheDocument();
    expect(screen.getByText(/5 roles reached Ready to Launch: \$20\.00 each/)).toBeInTheDocument();
    expect(screen.getByText(/\$20\.00 went on 40 roles that were later gated out/)).toBeInTheDocument();
    const trend = screen.getByRole("list", { name: "The last nights' cost" });
    expect(trend.querySelectorAll("li")).toHaveLength(2);
    expect(trend).toHaveTextContent("10-17");
  });

  it("labels every kind the report sends, and never shows a raw kind id", async () => {
    const kinds = {
      parser: { sessions: 3, turns: 9, usd: 1 }, scout: { sessions: 1, turns: 4, usd: 0.2 },
      reader: { sessions: 1, turns: 2, usd: 0.1 }, pathfinder: { sessions: 1, turns: 2, usd: 0.1 },
    };
    stubFetch(() => json([night("2026-10-18", 10, { kinds })]));
    render(<CostNights />);
    expect(await screen.findByText("Reading postings and resumes")).toBeInTheDocument();
    expect(screen.getByText("Search")).toBeInTheDocument();
    expect(screen.queryByText("parser")).toBeNull();
  });

  it("says no role was ready, and draws no trend for one night", async () => {
    stubFetch(() => json([night("2026-10-18", 10, { roles: { analysed: 3, ready: 0, usd_per_ready: null, gated_usd: 0, gated_roles: 0 } })]));
    render(<CostNights />);
    expect(await screen.findByText(/No role reached Ready to Launch/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "The last nights' cost" })).toBeNull();
  });

  it("is not there until a night has been worked out", async () => {
    stubFetch(() => json([]));
    const { container } = render(<CostNights />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container.querySelector("#agents-cost")).toBeNull();
  });
});
