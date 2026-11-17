import type { CompExample, CompMethod } from "../../api/types/profile";
import { DEFAULT_TIERS, exampleLine, problemsOf, rangeOf, readTiers, type RangedKey, writeTiers } from "./askMethodNumbers";

// Made up: the method's defaults, as the server holds them (backend profile.Settings).
const OK: CompMethod = {
  comp_basis: "base",
  comp_assumptions: "",
  comp_tiers: DEFAULT_TIERS,
  comp_transferable_weight: 0.5,
  comp_stance_clamp: 0.1,
  comp_stance_min_siblings: 3,
  comp_likely_low: 0.85,
  comp_likely_high: 1,
  comp_round_to: null,
  comp_range_width: null,
};

describe("the coverage tiers", () => {
  it("read from and written back to the stored text, the default one exactly", () => {
    expect(DEFAULT_TIERS).toBe("0.90:0.70, 0.75:0.60, 0.60:0.45, 0.45:0.30, 0:0.15");
    expect(readTiers(DEFAULT_TIERS)).toEqual([
      { least: 0.9, ask: 0.7 }, { least: 0.75, ask: 0.6 }, { least: 0.6, ask: 0.45 }, { least: 0.45, ask: 0.3 }, { least: 0, ask: 0.15 },
    ]);
    expect(writeTiers(readTiers(DEFAULT_TIERS))).toBe(DEFAULT_TIERS);
  });

  it("are written highest first, two decimals at least, the lowest as a plain 0, joined by a comma and a space", () => {
    expect(writeTiers([{ least: 0, ask: 0.1 }, { least: 0.625, ask: 0.5 }, { least: 0.9, ask: 1 }])).toBe("0.90:1.00, 0.625:0.50, 0:0.10");
    expect(writeTiers([{ least: 0.5, ask: 0.35 }])).toBe("0:0.35"); // one tier reaches 0: it is the whole range of coverage
  });

  it("are read the way the server reads them: any order, the lowest reaching 0, otherwise the default", () => {
    expect(readTiers("0:0.15, 0.9:0.7")).toEqual([{ least: 0.9, ask: 0.7 }, { least: 0, ask: 0.15 }]);
    expect(readTiers(" 0.9 : 0.7 ,0:0.1 ")).toEqual([{ least: 0.9, ask: 0.7 }, { least: 0, ask: 0.1 }]);
    expect(readTiers("0.9:0.7, 0.5:0.2")).toEqual(readTiers(DEFAULT_TIERS)); // no tier reaches 0
    expect(readTiers("high:low")).toEqual(readTiers(DEFAULT_TIERS));
    expect(readTiers("0.9")).toEqual(readTiers(DEFAULT_TIERS));
    expect(readTiers("")).toEqual(readTiers(DEFAULT_TIERS));
  });

  it("read again from what they were written as, whatever the rows were", () => {
    const rows = [{ least: 0.8, ask: 0.55 }, { least: 0.333, ask: 0.4 }, { least: 0, ask: 0.05 }];
    expect(readTiers(writeTiers(rows))).toEqual(rows);
  });
});

describe("problemsOf", () => {
  it("is nothing for the defaults, and for numbers at the edges of their ranges", () => {
    expect(problemsOf(OK)).toEqual({});
    expect(problemsOf({ ...OK, comp_transferable_weight: 0, comp_stance_clamp: 0.5, comp_stance_min_siblings: 20, comp_likely_low: 0.5, comp_likely_high: 1.2 })).toEqual({});
    expect(problemsOf({ ...OK, comp_transferable_weight: 1, comp_stance_min_siblings: 1, comp_round_to: 50000 })).toEqual({});
    expect(problemsOf({ ...OK, comp_round_to: 1 })).toEqual({});
  });

  it("says each number outside the range the server holds it to, in percent and points, with its step", () => {
    const bad = problemsOf({
      ...OK, comp_transferable_weight: 1.01, comp_stance_clamp: 0.51, comp_stance_min_siblings: 21, comp_likely_low: 0.49, comp_likely_high: 1.21, comp_round_to: 0,
    });
    expect(bad).toEqual({
      comp_transferable_weight: "Step 2: a Transferable requirement counts as 0 to 100% of a Proven one.",
      comp_stance_min_siblings: "Step 4: the employer's pay needs 1 to 20 other posted ranges to be judged.",
      comp_stance_clamp: "Step 4: the ask moves by 0 to 50 points at most.",
      comp_likely_low: "Step 5: the likely offer's low figure is 50 to 120% of the middle of the range.",
      comp_likely_high: "Step 5: the likely offer's top figure is 50 to 120% of the middle of the range.",
      comp_round_to: "Step 6: round to a whole number of dollars from 1 to 50,000, or leave it blank.",
    });
  });

  it("holds whole numbers whole, and a blank step is fine but a blank required number is not", () => {
    expect(Object.keys(problemsOf({ ...OK, comp_stance_min_siblings: 2.5 }))).toEqual(["comp_stance_min_siblings"]);
    expect(Object.keys(problemsOf({ ...OK, comp_round_to: 1000.5 }))).toEqual(["comp_round_to"]);
    expect(problemsOf({ ...OK, comp_round_to: null })).toEqual({});
    expect(Object.keys(problemsOf({ ...OK, comp_likely_low: null as unknown as number }))).toEqual(["comp_likely_low"]);
    expect(Object.keys(problemsOf({ ...OK, comp_likely_low: NaN }))).toEqual(["comp_likely_low"]);
  });

  it("judges a share on the percent shown, so the edges are not lost to a float (0.07 x 100 is 7.000000000000001)", () => {
    expect(problemsOf({ ...OK, comp_likely_low: 0.5, comp_likely_high: 1.2, comp_stance_clamp: 0.5 })).toEqual({});
    expect(Object.keys(problemsOf({ ...OK, comp_likely_high: 1.2000001 }))).toEqual(["comp_likely_high"]);
  });
});

describe("rangeOf", () => {
  it("gives the boxes their range in what is shown, and says which are percents", () => {
    const keys: RangedKey[] = [
      "comp_transferable_weight", "comp_stance_min_siblings", "comp_stance_clamp", "comp_likely_low", "comp_likely_high", "comp_round_to",
    ];
    expect(keys.map((k) => [k, rangeOf(k).percent, rangeOf(k).min, rangeOf(k).max, rangeOf(k).blank])).toEqual([
      ["comp_transferable_weight", true, 0, 100, false],
      ["comp_stance_min_siblings", false, 1, 20, false],
      ["comp_stance_clamp", true, 0, 50, false],
      ["comp_likely_low", true, 50, 120, false],
      ["comp_likely_high", true, 50, 120, false],
      ["comp_round_to", false, 1, 50000, true],
    ]);
  });
});

describe("exampleLine", () => {
  const worked: CompExample = { band: [150_000, 200_000], coverage: 0.8, ask: 165_000, by_method: 180_000, likely_offer: 169_750, capped_at_likely_offer: true };

  it("puts what the server gave into one plain sentence, and names the likely-offer limit when it held the ask down", () => {
    expect(exampleLine(worked)).toBe(
      "Example: a $150,000 to $200,000 range where you can show 80% of the job gives an ask of $165,000. The likely-offer limit (step 5) decided it.",
    );
  });

  it("names the tiers when nothing held the ask down", () => {
    expect(exampleLine({ ...worked, ask: 180_000, capped_at_likely_offer: false })).toBe(
      "Example: a $150,000 to $200,000 range where you can show 80% of the job gives an ask of $180,000. The share of the range the tiers give (step 3) decided it.",
    );
  });

  it("writes the share as the server gave it, with no drift", () => {
    expect(exampleLine({ ...worked, coverage: 0.57 })).toContain("show 57% of the job");
    expect(exampleLine({ ...worked, coverage: 0.625 })).toContain("show 62.5% of the job");
  });
});
