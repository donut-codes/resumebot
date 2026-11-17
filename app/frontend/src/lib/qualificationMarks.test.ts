import type { RequirementRow } from "../api/types/me";
import { isDutiesHeading, normalizeLine, placeDuties, placeMarks, postingBlocks, requirementPartLines } from "./qualificationMarks";

// Made up: no real posting or person.
function row(index: number, item: string, cls: "P" | "T" | "U" | "N" = "P", extra: Partial<RequirementRow> = {}): RequirementRow {
  return { index, item, class: cls, why: "", by: "analyst", owner_why: "", from: [], ...extra };
}
const POSTING = [
  "About the role",
  "We build things.",
  "Required Qualifications:",
  "- 5+ years of experience in project management",
  "- Strong written communication skills.",
  "- A pilot's licence",
  "Preferred Qualifications:",
  "- Experience with regulated industries",
].join("\n");

describe("normalizeLine", () => {
  it("sets aside case, bullets, punctuation and spacing", () => {
    expect(normalizeLine("  • Strong   Written-Communication, skills.  ")).toBe("strong written communication skills");
  });
});

describe("postingBlocks", () => {
  it("keeps the posting's blocks and lines in order, with the bullet apart from the line", () => {
    const blocks = postingBlocks("Intro\n\nRequired Qualifications:\n- One\n- Two\n\nClosing");
    expect(blocks.map((b) => b.map((l) => l.raw))).toEqual([["Intro"], ["Required Qualifications:", "- One", "- Two"], ["Closing"]]);
    expect(blocks[1][1].shown).toBe("One");
  });

  it("gives each line the kind of the section it sits in", () => {
    const kinds = postingBlocks(POSTING.replace(/\n/g, "\n\n")).flat().map((l) => l.kind);
    expect(kinds).toEqual([null, null, "required", "required", "required", "required", "preferred", "preferred"]);
  });
});

describe("placeMarks", () => {
  const lines = postingBlocks(POSTING).flat();
  const at = (m: Map<number, RequirementRow>) => [...m.entries()].map(([i, r]) => [lines[i].shown, r.index]);

  it("marks the line that matches an item, whatever its case, bullet or punctuation", () => {
    const m = placeMarks(lines, [row(0, "strong written communication skills", "P")], []);
    expect(at(m)).toEqual([["Strong written communication skills.", 0]]);
  });

  it("tolerates a small difference between the line and the item", () => {
    expect(placeMarks(lines, [row(0, "5 years experience in project management", "T")], []).size).toBe(1);
  });

  it("leaves a line unmarked when no item resembles it", () => {
    expect(placeMarks(lines, [row(0, "Knows Kubernetes deeply")], []).size).toBe(0);
  });

  it("matches an item to a line of the other kind's section when the line is a strong match, never to a weak one", () => {
    const strong = [row(0, "Experience with regulated industries")];
    expect(at(placeMarks(lines, strong, []))).toEqual([["Experience with regulated industries", 0]]);
    const weak = [row(0, "Some experience with industries that are regulated")];
    expect(placeMarks(lines, weak, []).size).toBe(0);
  });

  it("prefers the line in the item's own kind of section when two lines match equally", () => {
    const twice = postingBlocks("Preferred:\n- Strong written skills\n\nRequirements:\n- Strong written skills").flat();
    const m = placeMarks(twice, [row(0, "Strong written skills")], []);
    expect([...m.keys()]).toEqual([3]);
    const p = placeMarks(twice, [], [row(0, "Strong written skills")]);
    expect([...p.keys()]).toEqual([1]);
  });

  it("marks one line per item and one item per line", () => {
    const twice = postingBlocks("Qualifications:\n- Strong written skills\n- Strong written skills").flat();
    expect(placeMarks(twice, [row(0, "Strong written skills")], []).size).toBe(1);
  });
});

describe("placeMarks for items that answer a numbered line (docs/PRD.md 26.12)", () => {
  const text = [
    "Requirements:Education:",
    "A bachelor's degree preferred.",
    "Experience:",
    "Seven years of project work.",
    "Seven years of project work, abroad.",
    "Willingness to travel.",
    "Skills: Strong written communication.",
  ].join("\n");
  const lines = postingBlocks(text).flat();
  const shownAt = (m: Map<number, RequirementRow>) => [...m.entries()].map(([i, r]) => [lines[i].shown, r.index]);

  it("marks the line whose words the item has exactly, in any section, with no likeness test", () => {
    const rows = [row(0, "A bachelor's degree preferred.", "U", { line: 1 }), row(1, "Seven years of project work.", "P", { line: 2 })];
    expect(shownAt(placeMarks(lines, rows, []))).toEqual([["A bachelor's degree preferred.", 0], ["Seven years of project work.", 1]]);
  });

  it("does not take a near line for a line item: Seven years of project work is not the line that adds 'abroad'", () => {
    const rows = [row(0, "Seven years of project work, abroad.", "T", { line: 3 })];
    expect(shownAt(placeMarks(lines, rows, []))).toEqual([["Seven years of project work, abroad.", 0]]);
  });

  it("marks a line that carries its heading with the item that is the words after it", () => {
    const rows = [row(0, "Strong written communication.", "P", { line: 5 })];
    expect(shownAt(placeMarks(lines, rows, []))).toEqual([["Skills: Strong written communication.", 0]]);
  });

  it("marks an N item's line too, and gives two items of the same words their two lines in order", () => {
    const twice = postingBlocks("Requirements\nWilling to travel\nWilling to travel").flat();
    const rows = [row(0, "Willing to travel", "N", { line: 1 }), row(1, "Willing to travel", "N", { line: 2 })];
    expect([...placeMarks(twice, rows, []).entries()].map(([i, r]) => [i, r.index])).toEqual([[1, 0], [2, 1]]);
  });

  it("leaves a line item with no line of those words unmarked rather than guessing", () => {
    const rows = [row(0, "Seven years of project work and more", "P", { line: 2 })];
    expect(placeMarks(lines, rows, []).size).toBe(0);
  });
});

// A posting that titles its requirements in words the page does not recognise (docs/PRD.md 20.8). Made up.
const UNUSUAL = [
  "About the role",
  "We run projects.",
  "“I am the person Acme Corp is looking for.”",
  "· You have 10+ years of project management experience.",
  "· Have 4 years managing people and their growth.",
  "· You are a clear communicator who can explain trade-offs.",
  "· A fourth line nobody graded.",
  "“I can apply in less than 4 minutes.”",
  "Apply now.",
].join("\n");

describe("marks under a heading the page does not recognise", () => {
  const lines = postingBlocks(UNUSUAL).flat();
  const shown = (m: Map<number, RequirementRow>) => [...m.entries()].map(([i, r]) => [lines[i].shown, r.index]);
  const rows = [
    row(0, "10+ years of project management experience", "P"),
    row(1, "4 years managing people and their growth", "T"),
    row(2, "A clear communicator who can explain trade-offs", "U"),
  ];

  it("marks a graded item on the line it strongly matches wherever the posting puts it", () => {
    expect(shown(placeMarks(lines, rows, []))).toEqual([
      ["You have 10+ years of project management experience.", 0],
      ["Have 4 years managing people and their growth.", 1],
      ["You are a clear communicator who can explain trade-offs.", 2],
    ]);
  });

  it("boxes the bullets beside a strong match that no item matched, and nothing else of the posting", () => {
    const m = placeMarks(lines, rows, []);
    const part = [...requirementPartLines(lines, m)].sort((a, b) => a - b).map((i) => lines[i].shown);
    expect(part).toEqual([
      "You have 10+ years of project management experience.", "Have 4 years managing people and their growth.",
      "You are a clear communicator who can explain trade-offs.", "A fourth line nobody graded.",
    ]);
  });

  it("takes no part from a posting with nothing strongly matched", () => {
    expect(requirementPartLines(lines, placeMarks(lines, [row(0, "Knows Kubernetes deeply")], [])).size).toBe(0);
  });

  it("keeps the lines of a recognised requirement section in the part with or without a match", () => {
    const known = postingBlocks("Required Qualifications:\n- One\n- Two\nBenefits:\n- Free lunch").flat();
    expect([...requirementPartLines(known, new Map())].sort()).toEqual([0, 1, 2]);
  });
});

// The posting's duties carry no line numbers: each is placed by its text (docs/PRD.md 20.8). Made up.
const WITH_DUTIES = [
  "Key Responsibilities:",
  "- Lead the project office",
  "- Report progress to the board each quarter",
  "- Keep the lights on",
  "Required Qualifications:",
  "- Five years of project management",
  "Benefits:",
  "- Free lunch",
].join("\n");

describe("duties in the posting", () => {
  const lines = postingBlocks(WITH_DUTIES).flat();
  const shown = (m: Map<number, RequirementRow>) => [...m.entries()].map(([i, r]) => [lines[i].shown, r.index]);

  it("knows a section of duties by its heading, and a qualification part is never one", () => {
    expect(["Responsibilities", "Key responsibilities", "Duties", "What you'll do", "What you will do"].map(isDutiesHeading))
      .toEqual([true, true, true, true, true]);
    expect(["Required Qualifications", "Preferred", "Key Responsibilities and Required Skills", "Benefits", ""].map(isDutiesHeading))
      .toEqual([false, false, false, false, false]);
    expect(lines.map((l) => [l.kind, l.duty])).toEqual([
      [null, true], [null, true], [null, true], [null, true],
      ["required", false], ["required", false], [null, false], [null, false],
    ]);
  });

  it("places each duty on the line of its words, whatever its case, bullet or punctuation", () => {
    const duties = [row(0, "lead the project office.", "P"), row(1, "Report progress to the board every quarter", "T")];
    expect(shown(placeDuties(lines, duties, new Map()))).toEqual([["Lead the project office", 0], ["Report progress to the board each quarter", 1]]);
  });

  it("leaves a duty line unmarked when no duty resembles it", () => {
    const placed = placeDuties(lines, [row(0, "Lead the project office")], new Map());
    expect([...placed.keys()]).toEqual([1]);
  });

  it("marks one line per duty and one duty per line", () => {
    const twice = postingBlocks("Responsibilities:\n- Lead the project office\n- Lead the project office").flat();
    expect(placeDuties(twice, [row(0, "Lead the project office")], new Map()).size).toBe(1);
    expect(placeDuties(twice, [row(0, "Lead the project office"), row(1, "Lead the project office"), row(2, "Lead the project office")], new Map()).size).toBe(2);
  });

  it("takes a strong match under a heading the page does not know, never a weak one", () => {
    const odd = postingBlocks("A day in the life:\n- Lead the project office\n- Report to the board").flat();
    const placed = placeDuties(odd, [row(0, "Lead the project office"), row(1, "Report progress to the board each quarter")], new Map());
    expect([...placed.entries()].map(([i, r]) => [odd[i].shown, r.index])).toEqual([["Lead the project office", 0]]);
  });

  it("does not take a line a qualification already holds, and returns only the duties' lines", () => {
    const held = placeMarks(lines, [row(7, "Lead the project office", "U")], []);
    expect([...held.keys()]).toEqual([1]);
    const placed = placeDuties(lines, [row(0, "Lead the project office", "P"), row(1, "Keep the lights on", "P")], held);
    expect(shown(placed)).toEqual([["Keep the lights on", 1]]);
  });
});
