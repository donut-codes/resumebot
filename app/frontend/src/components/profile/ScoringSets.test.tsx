import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import type { Ask, Group } from "../../api/types/profile";
import { AnyOfRow, FirstMatchRow } from "./ScoringSets";

// Made up: no real person's asks, employers or pay.

const level = (id: string, says: string, points: number, more: Partial<Ask> = {}): Ask => ({
  id, section: "posting", says, points, checked_by: "reader", roles: [], ...more,
});

const LIST: Group = {
  id: "list-a", kind: "best", section: "posting", roles: [],
  members: [level("lvl-a", "The role is mostly building", 4), level("lvl-b", "The role is partly building", 3), level("lvl-c", "The role touches building", 2)],
};

const BOX: Group = {
  id: "box-a", kind: "any", section: "company", roles: [], points: 2,
  members: [level("part-a", "Owned by a nonprofit", 0, { section: "company" }), level("part-b", "Owned by a university", 0, { section: "company" })],
};

const FIRST_MATCH = "First match (top wins)";
const HINT = "The first one that is true counts. Put the best at the top.";
const BOX_HINT = "Earns its points once, when any one of these is true.";
const NOTE = "A statement lower down is worth more than the one above it. The first true one from the top still counts.";
const LEVEL = "The statement, one sentence about a posting";
const PART = "The statement, one sentence about a posting";

interface HeldProps {
  start: Group;
  as: typeof FirstMatchRow | typeof AnyOfRow;
  onRemove?: () => void;
  seen?: (next: Group) => void;
  focus?: string | null;
}

/** Holds the group the way the page does, so an edit comes back as the next render. */
function Held({ start, as: Editor, onRemove = () => {}, seen, focus = null }: HeldProps) {
  const [group, setGroup] = useState(start);
  return (
    <Editor
      group={group}
      focus={focus}
      onChange={(next) => {
        setGroup(next);
        seen?.(next);
      }}
      onRemove={onRemove}
    />
  );
}

type Extra = Partial<Omit<HeldProps, "start" | "as">>;
const list = (extra: Extra = {}) => render(<Held start={LIST} as={FirstMatchRow} {...extra} />);
const box = (extra: Extra = {}) => render(<Held start={BOX} as={AnyOfRow} {...extra} />);
const sentences = () => screen.getAllByLabelText(LEVEL).map((e) => (e as HTMLTextAreaElement).value);
const parts = () => screen.getAllByLabelText(PART).map((e) => (e as HTMLTextAreaElement).value);
const rowOf = (says: string) => screen.getByLabelText(`Points for ${says}`).closest("li") as HTMLElement;
/** The names of the things the keyboard stops on, in the order it stops on them. */
const stops = (root: HTMLElement) =>
  Array.from(root.querySelectorAll<HTMLElement>("input, textarea, select, button")).map((e) => e.getAttribute("aria-label") ?? e.textContent ?? "");

describe("a first match row", () => {
  it("is a named block headed First match (top wins), with its one plain line and nothing else on it but its rows and two buttons", () => {
    list();
    const block = screen.getByRole("group", { name: FIRST_MATCH });
    expect(within(block).getByRole("heading", { level: 3, name: FIRST_MATCH })).toBeInTheDocument();
    expect(within(block).getByText(HINT)).toBeInTheDocument();
    expect(block).toHaveAccessibleDescription(HINT);
    // The rows are an ordered list: the order is the meaning.
    expect(within(block).getByRole("list").tagName).toBe("OL");
    expect(within(block).getAllByRole("listitem")).toHaveLength(3);
    expect(within(block).getByRole("button", { name: "Add a statement to the row: The role is mostly building" })).toHaveTextContent("Add a statement");
    expect(within(block).getByRole("button", { name: "Add a statement to the row: The role is mostly building" })).toHaveClass("fi-link");
    expect(within(block).getByRole("button", { name: "Remove this row: The role is mostly building" })).toHaveTextContent("Remove this row");
    expect(within(block).getByRole("button", { name: "Remove this row: The role is mostly building" })).toHaveClass("fi-link", "is-danger");
  });

  it("has a points box and a sentence for each statement, named for it, and sentences that are never cut off", () => {
    list();
    expect(sentences()).toEqual(["The role is mostly building", "The role is partly building", "The role touches building"]);
    expect(screen.getAllByRole("spinbutton").map((e) => [e.getAttribute("aria-label"), (e as HTMLInputElement).value])).toEqual([
      ["Points for The role is mostly building", "4"],
      ["Points for The role is partly building", "3"],
      ["Points for The role touches building", "2"],
    ]);
    for (const field of screen.getAllByLabelText(LEVEL)) expect(field).not.toHaveAttribute("maxlength"); // no limit on a sentence
    expect(screen.getByLabelText("Points for The role is mostly building")).toHaveAttribute("data-sign", "plus");
  });

  it("edits a statement's sentence and points in place, a number kept as a whole one, and tells the page", () => {
    const seen = vi.fn();
    list({ seen });
    fireEvent.change(screen.getAllByLabelText(LEVEL)[1], { target: { value: "The role is half building" } });
    expect((seen.mock.lastCall![0] as Group).members[1]).toMatchObject({ id: "lvl-b", says: "The role is half building", points: 3 });
    // Edited 2026-10-27 (PRD 30.10): typing -2 saved -2 (and showed a minus); Prioritization only adds points, so it is 0.
    fireEvent.change(screen.getByLabelText("Points for The role is half building"), { target: { value: "-2" } });
    expect((seen.mock.lastCall![0] as Group).members[1]).toMatchObject({ id: "lvl-b", points: 0 });
    expect(screen.getByLabelText("Points for The role is half building")).toHaveAttribute("min", "0");
    fireEvent.change(screen.getByLabelText("Points for The role is half building"), { target: { value: "2.7" } });
    expect((seen.mock.lastCall![0] as Group).members[1]).toMatchObject({ points: 2 });
  });

  it("says a statement saved with minus points earns nothing, and a plus one says nothing of the kind (PRD 30.10)", () => {
    const owners: Group = { ...LIST, members: [LIST.members[0], { ...LIST.members[1], points: -2 }, LIST.members[2]] };
    render(<Held start={owners} as={FirstMatchRow} />);
    const notes = screen.getAllByRole("note").filter((n) => /^Earns nothing/.test(n.textContent ?? ""));
    expect(notes).toHaveLength(1);
    expect(notes[0]).toHaveTextContent("Earns nothing: Prioritization only adds points; use Hard pass to exclude.");
    expect(rowOf("The role is partly building")).toContainElement(notes[0]);
  });

  it("no Verify box on a statement with no Verify text", () => {
    list();
    expect(screen.queryByLabelText(/^Verify:/)).toBeNull();
    box();
    expect(screen.queryByLabelText(/^Verify:/)).toBeNull();
  });

  it("has a Verify box under a statement that holds Verify text, edited in place, kept whole with no limit, and told to the page", () => {
    const seen = vi.fn();
    const [a, b, c] = LIST.members;
    render(<Held start={{ ...LIST, members: [a, { ...b, verify: "The posting names the work" }, c] }} as={FirstMatchRow} seen={seen} />);
    const field = screen.getByLabelText("Verify: The role is partly building") as HTMLTextAreaElement;
    expect(field.value).toBe("The posting names the work");
    expect(field).not.toHaveAttribute("maxlength");
    expect(within(rowOf("The role is partly building")).getByText("Verify:")).toBeInTheDocument();
    const long = "The posting names the work, in these words or others. ".repeat(2000);
    fireEvent.change(field, { target: { value: long } });
    const last = (seen.mock.lastCall![0] as Group).members;
    expect(last[1]).toMatchObject({ id: "lvl-b", says: "The role is partly building", points: 3, verify: long });
    expect(last[0].verify).toBeUndefined(); // no other statement changed
    expect((screen.getByLabelText("Verify: The role is partly building") as HTMLTextAreaElement).value).toBe(long);
  });

  it("shows a Verify line a statement already has, and none on a statement the app decides", () => {
    render(<Held start={{ ...LIST, members: [level("s1", "Small", 3, { verify: "The posting lists under 100 people" }), level("s2", "Medium", 2, { checked_by: "app", fact: "employees", op: "<", value: "1000" })] }} as={FirstMatchRow} />);
    expect(screen.getByLabelText("Verify: Small")).toHaveValue("The posting lists under 100 people");
    expect(screen.queryByLabelText("Verify: Medium")).toBeNull(); // the app decides it: nobody reads a posting for it
  });

  it("has Move up, Move down and Remove under each statement, named with its sentence, Move up absent on the top one and Move down on the last", () => {
    list();
    const top = within(rowOf("The role is mostly building"));
    expect(top.queryByRole("button", { name: /^Move up/ })).toBeNull();
    expect(top.getByRole("button", { name: "Move down: The role is mostly building" })).toHaveTextContent("Move down");
    expect(top.getByRole("button", { name: "Remove The role is mostly building" })).toHaveTextContent("Remove");
    const middle = within(rowOf("The role is partly building"));
    expect(middle.getByRole("button", { name: "Move up: The role is partly building" })).toHaveTextContent("Move up");
    expect(middle.getByRole("button", { name: "Move down: The role is partly building" })).toBeInTheDocument();
    const last = within(rowOf("The role touches building"));
    expect(last.getByRole("button", { name: "Move up: The role touches building" })).toBeInTheDocument();
    expect(last.queryByRole("button", { name: /^Move down/ })).toBeNull();
    // Every one is a quiet text button, and Remove is the oxblood one.
    for (const b of screen.getAllByRole("button", { name: /^(Move up|Move down|Remove) / })) expect(b).toHaveClass("fi-link");
    expect(top.getByRole("button", { name: /^Remove / })).toHaveClass("is-danger");
    expect(top.getByRole("button", { name: /^Move down/ })).not.toHaveClass("is-danger");
  });

  it("is reached by the keyboard in reading order: a statement's points, its sentence, then its buttons, statement by statement, then the row's two", () => {
    list();
    expect(stops(rowOf("The role is partly building"))).toEqual([
      "Points for The role is partly building",
      LEVEL,
      "Answer from the company record instead: The role is partly building",
      "Move up: The role is partly building",
      "Move down: The role is partly building",
      "Remove The role is partly building",
    ]);
    expect(stops(screen.getByRole("group"))).toEqual([
      "Points for The role is mostly building", LEVEL, "Answer from the company record instead: The role is mostly building", "Move down: The role is mostly building", "Remove The role is mostly building",
      "Points for The role is partly building", LEVEL, "Answer from the company record instead: The role is partly building", "Move up: The role is partly building", "Move down: The role is partly building", "Remove The role is partly building",
      "Points for The role touches building", LEVEL, "Answer from the company record instead: The role touches building", "Move up: The role touches building", "Remove The role touches building",
      "Add a statement to the row: The role is mostly building", "Remove this row: The role is mostly building",
    ]);
  });

  it("moves a statement down and up, the others keep their places, and the keyboard stays on the button it pressed", () => {
    const seen = vi.fn();
    list({ seen });
    fireEvent.click(screen.getByRole("button", { name: "Move down: The role is mostly building" }));
    expect(sentences()).toEqual(["The role is partly building", "The role is mostly building", "The role touches building"]);
    expect((seen.mock.lastCall![0] as Group).members.map((m) => m.id)).toEqual(["lvl-b", "lvl-a", "lvl-c"]);
    expect(screen.getByRole("button", { name: "Move down: The role is mostly building" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Move up: The role is mostly building" }));
    expect(sentences()).toEqual(["The role is mostly building", "The role is partly building", "The role touches building"]);
    expect(screen.getByRole("button", { name: "Move down: The role is mostly building" })).toHaveFocus(); // on top now: it has no Move up
  });

  it("keeps the keyboard on the other button when a move takes a statement to the end, and says where the statement is now", () => {
    list();
    fireEvent.click(screen.getByRole("button", { name: "Move down: The role is partly building" }));
    expect(sentences()).toEqual(["The role is mostly building", "The role touches building", "The role is partly building"]);
    expect(screen.getByRole("button", { name: "Move up: The role is partly building" })).toHaveFocus(); // last now: no Move down
    expect(screen.getByRole("status")).toHaveTextContent("Moved down: The role is partly building. Now statement 3 of 3.");
    fireEvent.click(screen.getByRole("button", { name: "Move up: The role is partly building" }));
    expect(screen.getByRole("status")).toHaveTextContent("Moved up: The role is partly building. Now statement 2 of 3.");
  });

  it("adds a statement at the end, worth a point, with the cursor in its sentence, and a statement can be removed", () => {
    const seen = vi.fn();
    list({ seen });
    fireEvent.click(screen.getByRole("button", { name: /^Add a statement/ }));
    const next = seen.mock.lastCall![0] as Group;
    expect(next.members).toHaveLength(4);
    expect(next.members[3]).toMatchObject({ says: "", points: 1, checked_by: "reader", section: "posting", roles: [] });
    expect(new Set(next.members.map((m) => m.id)).size).toBe(4);
    expect(screen.getAllByLabelText(LEVEL)[3]).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Remove The role is partly building" }));
    expect(sentences()).toEqual(["The role is mostly building", "The role touches building", ""]);
  });

  it("removes the whole row with Remove this row", () => {
    const onRemove = vi.fn();
    list({ onRemove });
    fireEvent.click(screen.getByRole("button", { name: /^Remove this row/ }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("puts the cursor in a statement when the page asks for it", () => {
    list({ focus: "lvl-b" });
    expect(screen.getAllByLabelText(LEVEL)[1]).toHaveFocus();
  });

  it("says in one quiet line, not as an error, when a statement lower down is worth more than the one above it, and not otherwise", () => {
    list();
    expect(screen.queryByText(NOTE)).toBeNull();
    fireEvent.change(screen.getByLabelText("Points for The role touches building"), { target: { value: "6" } });
    expect(screen.getByText(NOTE)).toHaveAttribute("role", "note");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.change(screen.getByLabelText("Points for The role touches building"), { target: { value: "1" } });
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it("does not call a row of penalties put worst first a mistake", () => {
    render(<Held start={{ ...LIST, members: [level("a", "Two", -2), level("b", "One", -1)] }} as={FirstMatchRow} />);
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it("shows the one value a statement the app decides is decided from, in the statement's own line, as an ask does", () => {
    const decided = level("lvl-s", "Under a thousand people", 2, { checked_by: "app", fact: "employees", op: "<", value: "1000" });
    const next = level("lvl-t", "Under ten thousand people", 1, { checked_by: "app", fact: "employees", op: "<", value: "10000" });
    render(<Held start={{ ...LIST, members: [decided, next] }} as={FirstMatchRow} />);
    const row = within(rowOf("Under a thousand people"));
    expect(row.getByLabelText("Employer fact")).toHaveValue("employees");
    expect(row.getByLabelText("Must be")).toHaveValue("<");
    expect(row.getByLabelText("This value")).toHaveValue("1000");
    expect(row.getByText("automatic")).toHaveClass("fi-badge");
    fireEvent.change(row.getByLabelText("This value"), { target: { value: "2000" } });
    expect(row.getByLabelText("This value")).toHaveValue("2000");
  });

  it("never says group, member, kind, pick one, ranked list, all of, AND, OR, level or part", () => {
    const { container } = list();
    expect(container.textContent).not.toMatch(/\bgroups?\b|\bmembers?\b|\bkind\b|pick one|ranked list|all of|\blevels?\b|\bparts?\b|\bbox\b/i);
    expect(container.textContent).not.toMatch(/\bAND\b|\bOR\b/); // the capitals: the words "and" and "or" are plain words
  });
});

describe("an any-of row", () => {
  it("is a named block headed Any of these, with its one plain line, its points at its head and a sentence for each statement", () => {
    box();
    const block = screen.getByRole("group", { name: "Any of these" });
    expect(within(block).getByRole("heading", { level: 3, name: "Any of these" })).toBeInTheDocument();
    expect(within(block).getByText(BOX_HINT)).toBeInTheDocument();
    expect(block).toHaveAccessibleDescription(BOX_HINT);
    // One points box, on the row: a statement of an "any of" row has none of its own.
    expect(screen.getAllByRole("spinbutton")).toHaveLength(1);
    expect(screen.getByLabelText("Points for the row: Owned by a nonprofit")).toHaveValue(2);
    expect(parts()).toEqual(["Owned by a nonprofit", "Owned by a university"]);
    expect(within(block).getByRole("button", { name: "Add a statement to the row: Owned by a nonprofit" })).toHaveClass("fi-link");
    expect(within(block).getByRole("button", { name: "Remove this row: Owned by a nonprofit" })).toHaveClass("fi-link", "is-danger");
  });

  it("changes the row's points and a statement's sentence, and tells the page", () => {
    const seen = vi.fn();
    box({ seen });
    fireEvent.change(screen.getByLabelText("Points for the row: Owned by a nonprofit"), { target: { value: "3" } });
    expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ points: 3 }));
    fireEvent.change(screen.getAllByLabelText(PART)[1], { target: { value: "Owned by a college" } });
    expect((seen.mock.lastCall![0] as Group).members[1]).toMatchObject({ id: "part-b", says: "Owned by a college", points: 0 });
  });

  it("has a Verify box under an any-of statement that holds Verify text, and the statement keeps it", () => {
    const seen = vi.fn();
    const [a, b] = BOX.members;
    render(<Held start={{ ...BOX, members: [a, { ...b, verify: "The posting names a university" }] }} as={AnyOfRow} seen={seen} />);
    fireEvent.change(screen.getByLabelText("Verify: Owned by a university"), { target: { value: "The posting names a university as owner" } });
    const members = (seen.mock.lastCall![0] as Group).members;
    expect(members[1]).toMatchObject({ id: "part-b", says: "Owned by a university", points: 0, verify: "The posting names a university as owner" });
    expect(members[0].verify).toBeUndefined();
  });

  it("has only Remove under a statement: the order of statements means nothing, so there is no Move up or Move down", () => {
    box();
    expect(screen.queryByRole("button", { name: /^Move / })).toBeNull();
    const row = within(screen.getAllByLabelText(PART)[0].closest("li") as HTMLElement);
    expect(row.getAllByRole("button").map((b) => b.textContent)).toEqual(["Answer from the company record instead", "Remove"]); // and no Move up or Move down
    expect(row.getByRole("button", { name: "Remove Owned by a nonprofit" })).toHaveClass("fi-link", "is-danger");
  });

  it("adds a statement, with the cursor in its sentence and no points, and removes a statement or the whole row", () => {
    const seen = vi.fn();
    const onRemove = vi.fn();
    box({ seen, onRemove });
    fireEvent.click(screen.getByRole("button", { name: /^Add a statement/ }));
    const next = seen.mock.lastCall![0] as Group;
    expect(next.members).toHaveLength(3);
    expect(next.members[2]).toMatchObject({ says: "", points: 0, section: "company" });
    expect(screen.getAllByLabelText(PART)[2]).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Remove Owned by a nonprofit" }));
    expect(parts()).toEqual(["Owned by a university", ""]);
    fireEvent.click(screen.getByRole("button", { name: /^Remove this row/ }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("is reached by the keyboard in reading order: the row's points, then each statement's sentence and Remove, then the row's two buttons", () => {
    box();
    expect(stops(screen.getByRole("group"))).toEqual([
      "Points for the row: Owned by a nonprofit",
      PART, "Answer from the company record instead: Owned by a nonprofit", "Remove Owned by a nonprofit",
      PART, "Answer from the company record instead: Owned by a university", "Remove Owned by a university",
      "Add a statement to the row: Owned by a nonprofit", "Remove this row: Owned by a nonprofit",
    ]);
  });

  it("puts the cursor in a statement when the page asks for it", () => {
    box({ focus: "part-b" });
    expect(screen.getAllByLabelText(PART)[1]).toHaveFocus();
  });

  it("never says group, member, kind, pick one, ranked list, all of, level, part, box, AND or OR, and has no note about the order of statements", () => {
    const { container } = box();
    expect(container.textContent).not.toMatch(/\bgroups?\b|\bmembers?\b|\bkind\b|pick one|ranked list|all of|\blevels?\b|\bparts?\b|\bbox\b/i);
    expect(container.textContent).not.toMatch(/\bAND\b|\bOR\b/);
    expect(screen.queryByRole("note")).toBeNull();
  });
});

describe("who decides, in a row", () => {
  const sized = (id: string, says: string, points: number, value: string): Ask =>
    level(id, says, points, { checked_by: "app", fact: "employees", op: "<", value });

  it("lets the app decide one statement of a first match row from the employer's record, and the row then offers the same for the whole row", () => {
    const seen = vi.fn();
    list({ seen });
    expect(screen.queryByRole("button", { name: /^Answer the whole row from the company record/ })).toBeNull(); // all Claude's: nothing to offer
    fireEvent.click(screen.getByRole("button", { name: "Answer from the company record instead: The role is partly building" }));
    expect((seen.mock.lastCall![0] as Group).members[1]).toMatchObject({ id: "lvl-b", checked_by: "app", fact: "employees", op: ">=", value: "" });
    const row = within(rowOf("The role is partly building"));
    expect(row.getByText(/^Answered by: the company record/)).toBeInTheDocument();
    expect(row.getByLabelText("Employer fact")).toHaveValue("employees"); // the three boxes, in the statement's own line
    expect(row.getByLabelText("Must be")).toHaveValue(">=");
    expect(row.getByLabelText("This value")).toHaveValue("");
    expect(row.getByText("automatic")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Answer the whole row from the company record: The role is mostly building" })).toHaveClass("fi-link");
  });

  it("answers the whole row from the company record: every statement still Claude's takes the same fact and comparison with its number blank, and the numbers already typed stay", () => {
    const seen = vi.fn();
    render(<Held start={{ ...LIST, members: [sized("s1", "Small", 3, "100"), level("s2", "Medium", 2), level("s3", "Large", 1)] }} as={FirstMatchRow} seen={seen} />);
    fireEvent.click(screen.getByRole("button", { name: "Answer the whole row from the company record: Small" }));
    const members = (seen.mock.lastCall![0] as Group).members;
    expect(members.map((m) => [m.id, m.checked_by, m.fact, m.op, m.value])).toEqual([
      ["s1", "app", "employees", "<", "100"],
      ["s2", "app", "employees", "<", ""],
      ["s3", "app", "employees", "<", ""],
    ]);
    expect(screen.queryByRole("button", { name: /^Answer the whole row from the company record/ })).toBeNull(); // it is all the app's now
    expect(members.map((m) => m.points)).toEqual([3, 2, 1]);
  });

  it("offers no whole row once every statement is the app's", () => {
    render(<Held start={{ ...LIST, members: [sized("s1", "Small", 3, "100"), sized("s2", "Medium", 2, "1000")] }} as={FirstMatchRow} />);
    expect(screen.queryByRole("button", { name: /^Answer the whole row from the company record/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Answer from the job posting instead/ })).toBeNull();
  });

  it("has no whole row in an any-of row, whose statements may be some Claude's and some the app's", () => {
    box();
    fireEvent.click(screen.getByRole("button", { name: "Answer from the company record instead: Owned by a nonprofit" }));
    expect(screen.queryByRole("button", { name: /whole row/ })).toBeNull();
    expect(within(screen.getAllByLabelText(PART)[0].closest("li") as HTMLElement).getByLabelText("This value")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Answer from the company record instead: Owned by a university" })).toBeInTheDocument();
  });
});
