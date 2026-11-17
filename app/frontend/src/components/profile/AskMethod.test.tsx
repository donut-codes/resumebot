import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { api } from "../../api/client";
import type { CompMethod } from "../../api/types/profile";
import { json, render, settle, stubFetch } from "../../test-utils";
import AskMethod from "./AskMethod";

// Made up: the method's defaults, as the server holds them (backend profile.Settings), and no one's pay.
const DEFAULTS: CompMethod = {
  comp_basis: "base",
  comp_assumptions: "",
  comp_tiers: "0.90:0.70, 0.75:0.60, 0.60:0.45, 0.45:0.30, 0:0.15",
  comp_transferable_weight: 0.5,
  comp_stance_clamp: 0.1,
  comp_stance_min_siblings: 3,
  comp_likely_low: 0.85,
  comp_likely_high: 1,
  comp_round_to: null,
  comp_range_width: null,
};

/** The server's answer, stood in for: it echoes what the page sent, so a test sees the example follow the numbers on the
 * page. The real figure is the server's `compute` (backend tests/jobsearch/test_comp_example.py); nothing here works it out. */
function answer(query: URLSearchParams) {
  const high = Number(query.get("comp_likely_high"));
  return {
    band: [150_000, 200_000],
    coverage: 0.8,
    ask: Math.round(high * 100_000),
    by_method: 180_000,
    likely_offer: 169_750,
    capped_at_likely_offer: high <= 1,
  };
}

/** Every address the page asked the example for. */
function serve(): URLSearchParams[] {
  const asked: URLSearchParams[] = [];
  stubFetch((input: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") throw new Error(`the example must write nothing: ${init.method} ${input}`);
    if (!input.includes("/profile/comp-example")) return json({}, 404);
    const query = new URL(input, "http://localhost").searchParams;
    asked.push(query);
    return json(answer(query));
  });
  return asked;
}

/** The method with its value held, and every change reported whole. */
function Harness({ start, seen }: { start: CompMethod; seen: CompMethod[] }) {
  const [value, setValue] = useState(start);
  return (
    <AskMethod
      example={api.compExample}
      value={value}
      onChange={(next) => {
        seen.push(next);
        setValue(next);
      }}
    />
  );
}

function open(start: CompMethod = DEFAULTS) {
  const seen: CompMethod[] = [];
  const view = render(<Harness start={start} seen={seen} />);
  return { seen, last: () => seen[seen.length - 1], ...view };
}

const TRANSFERABLE = /^A Transferable requirement counts as this share/;
const SIBLINGS = /^Other posted ranges needed/;
const CLAMP = /^The most the ask moves/;
const LIKELY_LOW = /^Likely offer when you can show none/;
const LIKELY_HIGH = /^Likely offer when you can show all/;
const ROUND = /^Round to the nearest/;
const box = (label: RegExp) => screen.getByLabelText(label) as HTMLInputElement;
const type = (label: RegExp, text: string) => fireEvent.change(box(label), { target: { value: text } });
const example = () => screen.getByRole("status");

afterEach(settle);

describe("AskMethod", () => {
  it("is six numbered steps in the order the math runs, each number inside its sentence, then what the number means", async () => {
    serve();
    const { container } = open();
    const steps = Array.from(container.querySelectorAll(".fi-ss-steps > li"));
    expect(steps.map((li) => li.querySelector(".fi-ss-lead")?.textContent)).toEqual([
      "Start from the pay range.",
      "Count how much of the job you can show.",
      "Place your ask in the range by that share.",
      "Adjust for how the employer pays.",
      "Stay near the likely offer.",
      "Round to the nearest $.",
    ]);
    expect(steps[0]).toHaveTextContent("The posted range, or when none is posted, the market range for that title, level and place.");
    expect(steps[0].querySelector("input")).toBeNull(); // step 1 has no control
    // A number is a box inside its sentence (its value is in the box, not in the text).
    expect(steps[1]).toHaveTextContent("A Proven requirement counts fully; a Transferable one counts as % of one.");
    expect(box(TRANSFERABLE).closest("li")).toBe(steps[1]);
    expect(steps[3]).toHaveTextContent("If its other posted ranges run above or below market (needs at least of them), move the ask up or down by at most points.");
    expect([box(SIBLINGS), box(CLAMP)].map((b) => b.closest("li"))).toEqual([steps[3], steps[3]]);
    expect(steps[4]).toHaveTextContent("New hires usually land between % and % of the middle of the range; the more you can show, the nearer the top figure. Your ask never goes above that.");
    expect([box(LIKELY_LOW), box(LIKELY_HIGH)].map((b) => b.closest("li"))).toEqual([steps[4], steps[4]]);
    expect(box(ROUND).closest("li")).toBe(steps[5]);
    expect(steps[5]).toHaveTextContent("Leave it blank to round in proportion to the pay.");
    // The tiers are step 3's table, and the old text box and labels are gone.
    expect(within(steps[2] as HTMLElement).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "You can show", "Ask this far up the range", "Remove a row",
    ]);
    expect(screen.queryByLabelText(/coverage tiers/i)).toBeNull();
    expect(container.textContent).not.toMatch(/Rule of thumb|least coverage:position|One number, the same every time/);
    expect(screen.getByLabelText("What the number means:")).toHaveValue("base");
    expect(screen.getByLabelText("What the number assumes (benefits, bonus)")).toHaveValue("");
    await screen.findByText(/^Example:/);
  });

  it("shows each stored fraction as the percent it is, and a blank step as blank", async () => {
    serve();
    open({ ...DEFAULTS, comp_transferable_weight: 0.57, comp_stance_clamp: 0.07, comp_likely_low: 0.585, comp_likely_high: 1.1 });
    expect(box(TRANSFERABLE)).toHaveValue(57);
    expect(box(CLAMP)).toHaveValue(7);
    expect(box(LIKELY_LOW)).toHaveValue(58.5);
    expect(box(LIKELY_HIGH)).toHaveValue(110);
    expect(box(SIBLINGS)).toHaveValue(3);
    expect(box(ROUND)).toHaveValue(null);
    await settle();
  });

  it("each number is passed up as the setting it has always been, exactly: a percent as its fraction, whole numbers whole", async () => {
    serve();
    const { last } = open();
    // 0.57, 0.07, 0.29 and 0.58 are the ones a multiplication drifts on (0.57 x 100 is 56.99999999999999).
    type(TRANSFERABLE, "57");
    expect(last().comp_transferable_weight).toBe(0.57);
    type(CLAMP, "7");
    expect(last().comp_stance_clamp).toBe(0.07);
    type(CLAMP, "29");
    expect(last().comp_stance_clamp).toBe(0.29);
    type(LIKELY_LOW, "58");
    expect(last().comp_likely_low).toBe(0.58);
    type(LIKELY_HIGH, "110");
    expect(last().comp_likely_high).toBe(1.1);
    type(LIKELY_HIGH, "62.5");
    expect(last().comp_likely_high).toBe(0.625);
    type(SIBLINGS, "5");
    expect(last().comp_stance_min_siblings).toBe(5);
    type(ROUND, "1000");
    expect(last().comp_round_to).toBe(1000);
    // The rest is left as it was.
    expect(last()).toMatchObject({ comp_tiers: DEFAULTS.comp_tiers, comp_basis: "base", comp_range_width: null });
    await settle();
  });

  it("a blank rounding step is no value (rounded in proportion to the pay), and a number that is required is never made nothing", async () => {
    serve();
    const { seen, last } = open({ ...DEFAULTS, comp_round_to: 5000 });
    type(ROUND, "");
    expect(last().comp_round_to).toBeNull();
    const before = seen.length;
    type(TRANSFERABLE, "");
    expect(seen).toHaveLength(before); // an emptied box says nothing: it keeps the number it had
    fireEvent.blur(box(TRANSFERABLE));
    expect(box(TRANSFERABLE)).toHaveValue(50);
    await settle();
  });

  it("the tier table is the tiers, highest first, written back in the stored text's format", async () => {
    serve();
    const { last } = open();
    const rows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows()).toHaveLength(5);
    const asks = () => rows().map((r) => within(r).getAllByRole("spinbutton").map((b) => (b as HTMLInputElement).value).join(" -> "));
    expect(asks()).toEqual(["90 -> 70", "75 -> 60", "60 -> 45", "45 -> 30", "15"]);
    expect(rows()[4]).toHaveTextContent("anything less");
    fireEvent.change(screen.getByLabelText("Row 2, ask this far up the range (percent)"), { target: { value: "55" } });
    expect(last().comp_tiers).toBe("0.90:0.70, 0.75:0.55, 0.60:0.45, 0.45:0.30, 0:0.15");
    fireEvent.change(screen.getByLabelText("Row 1, you can show at least (percent)"), { target: { value: "92.5" } });
    expect(last().comp_tiers).toBe("0.925:0.70, 0.75:0.55, 0.60:0.45, 0.45:0.30, 0:0.15");
    fireEvent.change(screen.getByLabelText("Row 5, ask this far up the range (percent)"), { target: { value: "10" } });
    expect(last().comp_tiers).toBe("0.925:0.70, 0.75:0.55, 0.60:0.45, 0.45:0.30, 0:0.10");
    await settle();
  });

  it("a row can be removed, and added: the bottom row, everything less, stays", async () => {
    serve();
    const { last } = open({ ...DEFAULTS, comp_tiers: "0.90:0.70, 0:0.15" });
    fireEvent.click(screen.getByRole("button", { name: "Add a row" }));
    expect(last().comp_tiers).toBe("0.90:0.70, 0.45:0.43, 0:0.15");
    fireEvent.click(screen.getByRole("button", { name: "Remove row 1" }));
    expect(last().comp_tiers).toBe("0.45:0.43, 0:0.15");
    fireEvent.click(screen.getByRole("button", { name: "Remove row 1" }));
    expect(last().comp_tiers).toBe("0:0.15");
    expect(screen.queryByRole("button", { name: /^Remove row/ })).toBeNull();
    await settle();
  });

  it("what the number means, and what it assumes in words of any length", async () => {
    serve();
    const { last } = open();
    fireEvent.change(screen.getByLabelText("What the number means:"), { target: { value: "total" } });
    expect(last().comp_basis).toBe("total");
    const long = "standard benefits and a bonus target, ".repeat(60);
    const assumes = screen.getByLabelText("What the number assumes (benefits, bonus)");
    fireEvent.change(assumes, { target: { value: long } });
    expect(last().comp_assumptions).toBe(long);
    expect(assumes).not.toHaveAttribute("maxlength");
    await settle();
  });

  it("works the example on the server with the numbers as they stand, and follows an edit before anything is saved", async () => {
    const asked = serve();
    open();
    const first = await screen.findByText(/^Example:/);
    expect(first).toHaveTextContent(
      "Example: a $150,000 to $200,000 range where you can show 80% of the job gives an ask of $100,000. The likely-offer limit (step 5) decided it.",
    );
    expect(Object.fromEntries(asked[0])).toEqual({
      comp_tiers: DEFAULTS.comp_tiers, comp_transferable_weight: "0.5", comp_stance_clamp: "0.1", comp_stance_min_siblings: "3",
      comp_likely_low: "0.85", comp_likely_high: "1", // a blank step is left out
    });
    type(LIKELY_HIGH, "105");
    await waitFor(() => expect(example()).toHaveTextContent("gives an ask of $105,000. The share of the range the tiers give (step 3) decided it."));
    expect(asked[asked.length - 1].get("comp_likely_high")).toBe("1.05"); // the fraction, exactly
    type(ROUND, "2500");
    await waitFor(() => expect(asked[asked.length - 1].get("comp_round_to")).toBe("2500"));
  });

  it("asks once for a run of keystrokes, not for each", async () => {
    const asked = serve();
    open();
    await screen.findByText(/^Example:/);
    const before = asked.length;
    for (const text of ["1", "10", "105"]) type(LIKELY_HIGH, text);
    await waitFor(() => expect(example()).toHaveTextContent("$105,000"));
    expect(asked.length - before).toBe(1);
  });

  it("a number outside its range is judged when its box is left, and never worked out: the example is replaced by what is wrong, in percent", async () => {
    const asked = serve();
    open();
    await screen.findByText(/^Example:/);
    const before = asked.length;
    type(LIKELY_LOW, "150");
    fireEvent.blur(box(LIKELY_LOW));
    expect(example()).toHaveTextContent("Step 5: the likely offer's low figure is 50 to 120% of the middle of the range.");
    expect(box(LIKELY_LOW)).toHaveAttribute("aria-invalid", "true");
    expect(box(LIKELY_HIGH)).not.toHaveAttribute("aria-invalid");
    type(CLAMP, "60");
    fireEvent.blur(box(CLAMP));
    expect(example()).toHaveTextContent("Step 4: the ask moves by 0 to 50 points at most.");
    type(LIKELY_LOW, "85");
    type(CLAMP, "10");
    await screen.findByText(/^Example:/);
    expect(box(LIKELY_LOW)).not.toHaveAttribute("aria-invalid");
    expect(box(CLAMP)).not.toHaveAttribute("aria-invalid");
    expect(asked.slice(before).filter((q) => Number(q.get("comp_likely_low")) > 1)).toEqual([]); // the bad one never went
  });

  it("a number is not judged while it is being typed: 85 passes through 8, 100 through 1 and 10, and nothing flashes", async () => {
    const asked = serve();
    open();
    await screen.findByText(/^Example:/);
    const before = asked.length;
    const runs = [
      { field: LIKELY_HIGH, param: "comp_likely_high", steps: ["8", "85"], sent: "0.85" },
      { field: LIKELY_LOW, param: "comp_likely_low", steps: ["5", "50", "99"], sent: "0.99" },
      { field: LIKELY_HIGH, param: "comp_likely_high", steps: ["1", "10", "100"], sent: "1" },
    ];
    for (const { field, param, steps, sent } of runs) {
      for (const text of steps) {
        type(field, text);
        // Typed in, not left: no mark on any box, and the example line is the example, never a complaint.
        expect(screen.queryAllByRole("spinbutton").filter((b) => b.hasAttribute("aria-invalid"))).toEqual([]);
        expect(example()).not.toHaveClass("is-problem");
        expect(example()).not.toHaveTextContent(/Step \d/);
        expect(example()).toHaveTextContent(/^Example:/); // the last good example stays while a half-typed number is out of range
      }
      fireEvent.blur(box(field));
      expect(box(field)).not.toHaveAttribute("aria-invalid");
      await waitFor(() => expect(asked[asked.length - 1].get(param)).toBe(sent)); // the number it ended as, and only that
    }
    // The half-typed numbers never went to the server: it was asked only for numbers that were in range.
    const all = asked.slice(before);
    expect(all.length).toBeGreaterThan(0);
    expect(all.filter((q) => Number(q.get("comp_likely_high")) < 0.5 || Number(q.get("comp_likely_low")) < 0.5)).toEqual([]);
  });

  it("while a number is out of range and being typed in, the last example stays and is marked out of date; leaving it tells what is wrong, typing again takes that off", async () => {
    serve();
    open();
    await screen.findByText(/^Example:/);
    type(LIKELY_HIGH, "8");
    expect(example()).toHaveTextContent(/^Example:/);
    expect(example()).toHaveAttribute("data-stale", "true");
    fireEvent.blur(box(LIKELY_HIGH));
    expect(box(LIKELY_HIGH)).toHaveAttribute("aria-invalid", "true");
    expect(example()).toHaveClass("is-problem");
    expect(example()).toHaveTextContent("Step 5: the likely offer's top figure is 50 to 120% of the middle of the range.");
    type(LIKELY_HIGH, "9"); // typed in again: not told of until it is left again
    expect(box(LIKELY_HIGH)).not.toHaveAttribute("aria-invalid");
    expect(example()).not.toHaveClass("is-problem");
    expect(example()).toHaveTextContent(/^Example:/);
    type(LIKELY_HIGH, "95");
    await waitFor(() => expect(example()).not.toHaveAttribute("data-stale"));
    expect(example()).toHaveTextContent(/^Example:/);
  });

  it("shows no example, and nothing else wrong, when the server cannot be reached", async () => {
    stubFetch(() => json({ detail: "nope" }, 500));
    open();
    await settle();
    expect(example()).toBeEmptyDOMElement();
    expect(box(TRANSFERABLE)).toHaveValue(50);
  });
});
