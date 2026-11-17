import { fireEvent, render, screen, within } from "@testing-library/react";
import type { Ask } from "../../api/types/profile";
import SuggestionBand, { type BandCard, type OverlapCard } from "./SuggestionBand";

// Made up: no real person's asks, employers or pay.

const ask = (id: string, says: string, points: number, section: Ask["section"] = "posting"): Ask => ({ id, section, says, points, checked_by: "reader", roles: [] });

const LADDER: BandCard = {
  suggestion: { id: "sug-a", kind: "best", asks: ["t1", "t2", "t3"], points: 0, why: "A posting is one of these, so only one should count." },
  card: { members: [ask("t1", "A promotion", 1), ask("t2", "A lateral move", 0), ask("t3", "A step down", -2)], moves: [], section: "posting", tab: "__all__" },
  part: "Job posting",
  role: null,
};

const BOX: BandCard = {
  suggestion: { id: "sug-b", kind: "any", asks: ["c1", "c2"], points: 3, why: "One of these is enough." },
  card: { members: [ask("c1", "Owned by a nonprofit", 1, "company"), ask("c2", "Owned by a university", 2, "company")], moves: [], section: "company", tab: "__all__" },
  part: "Company",
  role: null,
};

const HEAD = "Some of your asks look like choices between each other.";
const LEAD =
  "Two asks that cannot both be true of one posting, counted one by one, let a role earn both and keep every role under 100%. Putting them in one row fixes that. Nothing changes until you save.";

function band(cards: BandCard[]) {
  const onUse = vi.fn();
  const onLater = vi.fn();
  const view = render(<SuggestionBand cards={cards} onUse={onUse} onLater={onLater} />);
  return { ...view, onUse, onLater };
}

/** A block of the band by its kind line ("First match in Job posting"). */
const block = (kind: string) => screen.getByText(kind).closest("li") as HTMLElement;
/** A row of a block as [its points, its sentence]. */
const rows = (el: HTMLElement) =>
  within(el).getAllByRole("listitem").map((li) => [li.querySelector(".fi-ss-sug-points")?.firstChild?.textContent ?? null, li.querySelector(".fi-ss-sug-says")?.textContent]);

describe("the suggestion band", () => {
  it("is not there with nothing to suggest", () => {
    const { container } = band([]);
    expect(container).toBeEmptyDOMElement();
  });

  it("says in plain words what it is, and that nothing changes until the owner saves", () => {
    band([LADDER]);
    const region = screen.getByRole("region", { name: HEAD });
    expect(within(region).getByRole("heading", { level: 2, name: HEAD })).toBeInTheDocument();
    expect(within(region).getByText(LEAD)).toBeInTheDocument();
  });

  it("shows each suggestion as a block: its kind in words and the part, their sentences in the order proposed with their points, why, and two buttons", () => {
    band([LADDER, BOX]);
    const ladder = block("First match in Job posting");
    expect(rows(ladder)).toEqual([["+1", "A promotion"], ["0", "A lateral move"], ["−2", "A step down"]]);
    expect(within(ladder).getByText("A posting is one of these, so only one should count.")).toBeInTheDocument();
    expect(within(ladder).getByRole("button", { name: "Use this row: A promotion" })).toHaveTextContent("Use this row");
    expect(within(ladder).getByRole("button", { name: "Not now: A promotion" })).toHaveTextContent("Not now");
    const box = block("Any of in Company");
    expect(within(box).getByRole("button", { name: "Use this row: Owned by a nonprofit" })).toHaveTextContent("Use this row");
    expect(within(box).getByRole("button", { name: "Not now: Owned by a nonprofit" })).toHaveTextContent("Not now");
    expect(within(box).getByText("One of these is enough.")).toBeInTheDocument();
  });

  it("lists a first match row's sentences as an ordered list and an any-of row's as a plain one, the row's points shown once", () => {
    band([LADDER, BOX]);
    expect(within(block("First match in Job posting")).getByRole("list").tagName).toBe("OL");
    const box = block("Any of in Company");
    expect(within(box).getByRole("list").tagName).toBe("UL");
    // The row's own points, once; its statements carry none.
    expect(box.querySelectorAll(".fi-ss-sug-points")).toHaveLength(1);
    expect(box.querySelector(".fi-ss-sug-points")?.firstChild?.textContent).toBe("+3");
    expect(rows(box).map(([, says]) => says)).toEqual(["Owned by a nonprofit", "Owned by a university"]);
  });

  it("names the role when the suggestion is for a role's tab, and says which asks it would move into another part", () => {
    const moved: BandCard = {
      ...BOX,
      suggestion: { ...BOX.suggestion, id: "sug-m" },
      card: {
        ...BOX.card,
        members: [ask("c1", "Owned by a nonprofit", 1, "company"), ask("t9", "Hires from universities", 1, "posting")],
        moves: [ask("t9", "Hires from universities", 1, "posting")],
      },
      role: "Builders",
    };
    band([moved]);
    expect(screen.getByText("Any of in Company, for Builders")).toBeInTheDocument();
    expect(screen.getByText("This moves Hires from universities into Company.")).toBeInTheDocument();
  });

  it("has no moves line when nothing moves", () => {
    band([LADDER]);
    expect(screen.queryByText(/This moves/)).toBeNull();
  });

  it("tells the page which suggestion was used or put off, and does nothing else", () => {
    const { onUse, onLater } = band([LADDER, BOX]);
    fireEvent.click(screen.getByRole("button", { name: "Use this row: Owned by a nonprofit" }));
    expect(onUse).toHaveBeenCalledWith("sug-b");
    fireEvent.click(screen.getByRole("button", { name: "Not now: A promotion" }));
    expect(onLater).toHaveBeenCalledWith("sug-a");
    expect(onUse).toHaveBeenCalledTimes(1);
  });

  it("never says group, member, kind, pick one, all of, ranked list, level or part; its buttons are the one button size and its heading is plain type", () => {
    const { container } = band([LADDER, BOX]);
    expect(container.textContent).not.toMatch(/\bgroups?\b|\bmembers?\b|\bkind\b|pick one|all of|ranked list|\blevels?\b|\bparts?\b/i);
    for (const b of screen.getAllByRole("button")) expect(b).toHaveClass("fi-btn");
    expect(screen.getByRole("heading", { level: 2 })).toHaveClass("fi-ss-sug-head");
  });
});

describe("the suggestion band, the second block: asks the graded requirements count again", () => {
  const OVERLAP: OverlapCard = {
    suggestion: { id: "sug-r", kind: "remove", asks: ["u1", "p1"], points: 0, why: "Grading takes points off each unproven requirement already." },
    asks: [
      { id: "u1", section: "qualifications", says: "Each required item not yet proven", points: -1, per_item: true, limit: -10, checked_by: "reader", roles: [] },
      { id: "p1", section: "preferred", says: "Preferred items are proven", points: 2, checked_by: "reader", roles: [] },
    ],
  };
  const OVERLAP_HEAD = "Grading counts the same things as some of your asks.";

  function overlapBand(extra: { cards?: BandCard[] } = {}) {
    const onUse = vi.fn();
    const onLater = vi.fn();
    const onDrop = vi.fn();
    const view = render(<SuggestionBand cards={extra.cards ?? []} overlaps={[OVERLAP]} onUse={onUse} onLater={onLater} onDrop={onDrop} />);
    return { ...view, onUse, onLater, onDrop };
  }

  it("has its own heading and sentence, and is the only block when there is nothing to put in a row", () => {
    overlapBand();
    const region = screen.getByRole("region", { name: OVERLAP_HEAD });
    expect(within(region).getByRole("heading", { level: 2, name: OVERLAP_HEAD })).toBeInTheDocument();
    expect(within(region).getByText("Keeping both counts a requirement twice. Nothing changes until you save.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: HEAD })).toBeNull();
  });

  it("shows their own sentences with their points as they have them, why, and Remove these asks and Not now", () => {
    overlapBand();
    const card = screen.getByText("Each required item not yet proven").closest("li")!.closest("ul")!.closest("li") as HTMLElement;
    expect(within(card).getAllByRole("listitem").map((li) => [li.querySelector(".fi-ss-sug-points")?.firstChild?.textContent, li.querySelector(".fi-ss-sug-says")?.textContent])).toEqual([
      ["−1 each, down to −10", "Each required item not yet proven"],
      ["+2", "Preferred items are proven"],
    ]);
    expect(within(card).getByText("Grading takes points off each unproven requirement already.")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Remove these asks: Each required item not yet proven" })).toHaveTextContent("Remove these asks");
    expect(within(card).getByRole("button", { name: "Not now: Each required item not yet proven" })).toHaveTextContent("Not now");
    expect(within(card).queryByText(/This moves/)).toBeNull();
  });

  it("comes after the block of asks that look like choices between each other when there is one, and each button tells the page which suggestion", () => {
    const { onLater, onDrop } = overlapBand({ cards: [LADDER] });
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual([HEAD, OVERLAP_HEAD]);
    fireEvent.click(screen.getByRole("button", { name: "Remove these asks: Each required item not yet proven" }));
    expect(onDrop).toHaveBeenCalledWith("sug-r");
    fireEvent.click(screen.getByRole("button", { name: "Not now: Each required item not yet proven" }));
    expect(onLater).toHaveBeenCalledWith("sug-r");
  });
});
