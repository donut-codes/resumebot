import type { Ask, Group, Suggestion } from "../../api/types/profile";
import { ALL_TAB } from "./scope";
import { applySuggestion, cardOf, idsOf, isRowSuggestion, isSet, orderKey, overlapOf, type RowSuggestion, tidy, worthMoreBelow } from "./setLogic";

// Made up: no real person's asks, employers or pay.

const ask = (id: string, says: string, points: number, more: Partial<Ask> = {}): Ask => ({
  id, section: "posting", says, points, checked_by: "reader", roles: [], ...more,
});

const LADDER: Group = {
  id: "list-a", kind: "best", section: "posting", roles: [],
  members: [ask("lvl-a", "One", 4), ask("lvl-b", "Two", 3), ask("lvl-c", "Three", 2)],
};
const BOX: Group = {
  id: "box-a", kind: "any", section: "company", roles: [], points: 2,
  members: [ask("part-a", "Part one", 0, { section: "company" }), ask("part-b", "Part two", 0, { section: "company" })],
};

describe("what a row is, and the order a part keeps its rows in", () => {
  it("knows the two kinds the page has and no other", () => {
    expect(isSet(LADDER)).toBe(true);
    expect(isSet(BOX)).toBe(true);
    expect(isSet({ ...BOX, kind: "all" as Group["kind"] })).toBe(false); // an "all of" row was dropped
  });

  it("orders an ask by its points, a first match row by its best statement, and an any-of row by the row's points", () => {
    expect(orderKey(ask("a", "A", 3))).toBe(3);
    expect(orderKey(ask("a", "A", -2))).toBe(-2);
    expect(orderKey(LADDER)).toBe(4);
    expect(orderKey({ ...LADDER, members: [ask("a", "A", 2), ask("b", "B", 5), ask("c", "C", 1)] })).toBe(5); // the largest level
    expect(orderKey({ ...LADDER, members: [ask("a", "A", 1), ask("b", "B", 0), ask("c", "C", -1), ask("d", "D", -2)] })).toBe(1);
    expect(orderKey({ ...LADDER, members: [ask("a", "A", -1), ask("b", "B", -2)] })).toBe(-1); // a row of penalties sits with them
    expect(orderKey(BOX)).toBe(2);
    expect(orderKey({ ...BOX, points: undefined })).toBe(0);
  });
});

describe("the note under a first match row: a statement lower down worth more than the one above it", () => {
  const pts = (...points: number[]) => points.map((p) => ({ points: p }));

  it("is there when a statement lower down earns more than the one above it", () => {
    expect(worthMoreBelow(pts(4, 5))).toBe(true);
    expect(worthMoreBelow(pts(4, 3, 5))).toBe(true);
    expect(worthMoreBelow(pts(0, 2))).toBe(true);
  });

  it("is not there for good, better, best the right way up, equal levels, or a ladder that crosses zero", () => {
    expect(worthMoreBelow(pts(4, 3, 2))).toBe(false);
    expect(worthMoreBelow(pts(1, 1))).toBe(false);
    expect(worthMoreBelow(pts(1, 0, -1, -2))).toBe(false);
    expect(worthMoreBelow(pts(5))).toBe(false);
    expect(worthMoreBelow([])).toBe(false);
  });

  it("is not there for penalties put worst first: the first true level counts, so the worst goes at the top", () => {
    expect(worthMoreBelow(pts(-2, -1))).toBe(false);
    expect(worthMoreBelow(pts(1, -2, -1))).toBe(false);
    expect(worthMoreBelow(pts(-1, 0))).toBe(false);
  });
});

describe("every id of a document", () => {
  it("lists every id there is: the asks', the rows', and every statement's", () => {
    expect(idsOf([ask("x", "X", 1)], [LADDER, BOX]).map((i) => i.id)).toEqual(["x", "list-a", "lvl-a", "lvl-b", "lvl-c", "box-a", "part-a", "part-b"]);
  });
});

describe("what is saved: blank statements dropped, a row left with one sentence saved as an ask", () => {
  it("drops an ask with no sentence and keeps the others as they are", () => {
    const out = tidy([ask("a", "Words", 1), ask("b", "   ", 1)], []);
    expect(out.asks.map((a) => a.id)).toEqual(["a"]);
    expect(out.groups).toEqual([]);
  });

  it("drops blank statements and keeps a row with two or more sentences", () => {
    const list: Group = { ...LADDER, members: [ask("l1", "One", 3), ask("l2", "", 2), ask("l3", "Three", 1)] };
    const out = tidy([], [list]);
    expect(out.groups).toHaveLength(1);
    expect(out.groups[0].members.map((m) => m.id)).toEqual(["l1", "l3"]);
    expect(out.asks).toEqual([]);
  });

  it("saves a first match row left with one sentence as a plain ask: that statement's id, sentence and points, in the row's part and tab", () => {
    const list: Group = {
      ...LADDER, section: "qualifications", roles: ["builders"],
      members: [ask("l1", "", 3), ask("l2", "Only this", 2), ask("l3", " ", 1)],
    };
    const out = tidy([ask("a", "Kept", 1)], [list]);
    expect(out.groups).toEqual([]);
    expect(out.asks.map((a) => a.id)).toEqual(["a", "l2"]);
    expect(out.asks[1]).toMatchObject({ id: "l2", says: "Only this", points: 2, section: "qualifications", roles: ["builders"], checked_by: "reader" });
  });

  it("saves an any-of row left with one sentence as a plain ask worth the row's points", () => {
    const box: Group = { ...BOX, points: 4, members: [ask("p1", "", 0), ask("p2", "The one part", 0)] };
    const out = tidy([], [box]);
    expect(out.groups).toEqual([]);
    expect(out.asks).toEqual([expect.objectContaining({ id: "p2", says: "The one part", points: 4, section: "company", roles: [] })]);
  });

  it("drops a row with no sentence at all", () => {
    const out = tidy([], [{ ...LADDER, members: [ask("a", "", 1), ask("b", "  ", 1)] }, { ...BOX, members: [ask("c", "", 0), ask("d", "", 0)] }]);
    expect(out).toEqual({ asks: [], groups: [] });
  });

  it("sends a row that is as it came back as it came: nothing about it changes", () => {
    const out = tidy([], [LADDER, BOX]);
    expect(out.groups).toEqual([LADDER, BOX]);
  });

  it("keeps a group of a kind the page does not have exactly as it came, shown or not", () => {
    const odd = { ...BOX, id: "odd", kind: "all" } as unknown as Group;
    expect(tidy([], [odd]).groups).toEqual([odd]);
  });
});

describe("a suggestion: the asks the owner already has, offered as one row", () => {
  const ASKS = [
    ask("t1", "Promotion", 1),
    ask("t2", "Lateral", 0),
    ask("t3", "Step down", -1),
    ask("c1", "Under a thousand people", 2, { section: "company" }),
    ask("c2", "Owned by a nonprofit", 1, { section: "company" }),
  ];
  const LIST: RowSuggestion = { id: "sug-a", kind: "best", asks: ["t1", "t2", "t3"], points: 0, why: "A posting is one of these." };
  const BOXED: RowSuggestion = { id: "sug-b", kind: "any", asks: ["c1", "c2"], points: 3, why: "One is enough." };
  const none = new Set<string>();

  it("is usable when every ask is there, with a sentence, on one tab, and one a reader judges", () => {
    expect(cardOf(LIST, ASKS, none)?.members.map((m) => m.id)).toEqual(["t1", "t2", "t3"]);
    expect(cardOf(BOXED, ASKS, none)?.members.map((m) => m.id)).toEqual(["c1", "c2"]);
  });

  it("keeps the order proposed, not the order the asks are in", () => {
    const flipped: Suggestion = { ...LIST, asks: ["t3", "t1", "t2"] };
    expect(cardOf(flipped, ASKS, none)?.members.map((m) => m.id)).toEqual(["t3", "t1", "t2"]);
  });

  it("is not usable when an ask is gone, has no sentence, or the same ask is named twice", () => {
    expect(cardOf(LIST, ASKS.filter((a) => a.id !== "t2"), none)).toBeNull();
    expect(cardOf(LIST, ASKS.map((a) => (a.id === "t2" ? { ...a, says: "  " } : a)), none)).toBeNull();
    expect(cardOf({ ...LIST, asks: ["t1", "t1"] }, ASKS, none)).toBeNull();
  });

  it("is not usable with fewer than two asks, or of a kind the page does not have", () => {
    expect(cardOf({ ...LIST, asks: ["t1"] }, ASKS, none)).toBeNull();
    expect(cardOf({ ...LIST, asks: [] }, ASKS, none)).toBeNull();
    expect(cardOf({ ...LIST, kind: "all" as Suggestion["kind"] }, ASKS, none)).toBeNull();
  });

  it("is not usable when its asks are on different tabs, or on a tab for a role that is gone", () => {
    const roles = new Set(["builders"]);
    const split = ASKS.map((a) => (a.id === "t2" ? { ...a, roles: ["builders"] } : a));
    expect(cardOf(LIST, split, roles)).toBeNull();
    const together = ASKS.map((a) => (a.id.startsWith("t") ? { ...a, roles: ["builders"] } : a));
    expect(cardOf(LIST, together, roles)?.tab).toBe("builders");
    expect(cardOf(LIST, together, none)).toBeNull(); // the role the asks name is gone
    expect(cardOf(LIST, ASKS, none)?.tab).toBe(ALL_TAB);
  });

  it("is not usable when an ask cannot join: counted for each item, the built-in one, or one the app decides", () => {
    for (const bad of [
      { per_item: true, limit: -3 },
      { derived: "responsibilities" as const },
      { checked_by: "app" as const, fact: "employees" },
      { fact: "employees" }, // a reader's ask the app decides once the record holds the fact
      { days: 30 },
      { amount: 100000 },
      { from_classes: "U" as const },
    ]) {
      expect(cardOf(LIST, ASKS.map((a) => (a.id === "t2" ? { ...a, ...bad } : a)), none), JSON.stringify(bad)).toBeNull();
    }
  });

  it("says which asks would move into the first ask's part, and which part that is", () => {
    const mixed: Suggestion = { ...BOXED, asks: ["c1", "t1"] };
    const card = cardOf(mixed, ASKS, none)!;
    expect(card.section).toBe("company");
    expect(card.moves.map((m) => m.id)).toEqual(["t1"]);
    expect(cardOf(BOXED, ASKS, none)!.moves).toEqual([]);
  });

  it("puts the asks into a first match row: each keeps its id, sentence and points, in the order proposed, and the asks leave the flat list", () => {
    const out = applySuggestion(LIST, ASKS, []);
    expect(out.asks.map((a) => a.id)).toEqual(["c1", "c2"]);
    expect(out.groups).toHaveLength(1);
    const group = out.group;
    expect(group).toBe(out.groups[0]);
    expect(group).toMatchObject({ kind: "best", section: "posting", roles: [], points: 0 });
    expect(group.members.map((m) => [m.id, m.says, m.points])).toEqual([["t1", "Promotion", 1], ["t2", "Lateral", 0], ["t3", "Step down", -1]]);
    expect(idsOf(out.asks, out.groups).map((i) => i.id)).toContain(group.id);
    expect(new Set(idsOf(out.asks, out.groups).map((i) => i.id)).size).toBe(idsOf(out.asks, out.groups).length);
  });

  it("puts the asks into an any-of row: the row takes the suggestion's points and the statements have none", () => {
    const out = applySuggestion(BOXED, ASKS, []);
    expect(out.group).toMatchObject({ kind: "any", section: "company", points: 3 });
    expect(out.group.members.map((m) => [m.id, m.points])).toEqual([["c1", 0], ["c2", 0]]);
    expect(out.asks.map((a) => a.id)).toEqual(["t1", "t2", "t3"]);
  });

  it("takes the part and the tab of the first ask, and moves an ask from another part into it", () => {
    const together = ASKS.map((a) => (a.id === "c1" || a.id === "t1" ? { ...a, roles: ["builders"] } : a));
    const out = applySuggestion({ ...BOXED, asks: ["c1", "t1"] }, together, []);
    expect(out.group).toMatchObject({ section: "company", roles: ["builders"] });
    expect(out.group.members.map((m) => [m.id, m.section, m.roles])).toEqual([["c1", "company", ["builders"]], ["t1", "company", ["builders"]]]);
  });

  it("never changes the asks it was handed", () => {
    const before = structuredClone(ASKS);
    applySuggestion(LIST, ASKS, []);
    expect(ASKS).toEqual(before);
  });
});

describe("a suggestion to remove asks that the graded requirements count again", () => {
  const ASKS = [
    ask("u1", "Each required item unproven", -1, { section: "qualifications", per_item: true, limit: -10 }),
    ask("p1", "Preferred items proven", 2, { section: "preferred" }),
    ask("x", "Something else", 1),
  ];
  const REMOVE: Suggestion = { id: "sug-r", kind: "remove", asks: ["p1", "gone", "u1"], points: 0, why: "Grading counts these already." };

  it("is told apart from one that puts asks in a row, and is never offered as a row", () => {
    expect(isRowSuggestion(REMOVE)).toBe(false);
    expect(isRowSuggestion({ ...REMOVE, kind: "best" })).toBe(true);
    expect(isRowSuggestion({ ...REMOVE, kind: "any" })).toBe(true);
    expect(cardOf(REMOVE, ASKS, new Set())).toBeNull();
  });

  it("names the asks of it the page still has, in the order named, and none once they are all gone", () => {
    expect(overlapOf(REMOVE, ASKS).map((a) => a.id)).toEqual(["p1", "u1"]);
    expect(overlapOf(REMOVE, ASKS.filter((a) => a.id === "x"))).toEqual([]);
    expect(overlapOf(REMOVE, ASKS.map((a) => (a.id === "p1" ? { ...a, says: " " } : a))).map((a) => a.id)).toEqual(["u1"]);
  });
});
