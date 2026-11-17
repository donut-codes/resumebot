import type { GapCard, GapRunView, Resume } from "../../api/types/me";
import { addToResume, barWords, cardsByTheme, employersIn, jobName, placeWords, runLine } from "./gaps";

const RESUME: Resume = {
  summary: "Builds projects.",
  summary_variants: [{ id: "variant", text: "A variant", note: "" }],
  achievements: [{ id: "data-catalog", text: "Led a project", note: "" }],
  philosophy: "",
  competencies: [{ id: "gov", area: "Governance", items: [] }],
  roles: [
    {
      id: "lead", employer: "Northwind Labs", location: "", title: "Senior Manager", start: "", end: "", intro: "", descriptor: "", optional: false,
      note: "", tools: [], bullets: [{ id: "b1", text: "Led the group", group: "G", note: "" }], evidence: [{ id: "e1", title: "Board deck", text: "Presented" }],
    },
    {
      id: "eng", employer: "Fabrikam", location: "", title: "Engineer", start: "", end: "", intro: "", descriptor: "", optional: false, note: "",
      tools: [], bullets: [], evidence: [],
    },
  ],
  education: [{ id: "ms", degree: "MS", school: "Example University", note: "" }],
  projects: [],
  awards: [],
  notes: [],
};

describe("Find Gaps' plain rules", () => {
  it("says a grade bar in words, and names a job the way the master does", () => {
    expect(barWords({ P: 60, T: 25, U: 15 })).toBe("60% Proven, 25% Transferable, 15% Unproven");
    expect(jobName({ title: " Senior Manager ", employer: "Northwind Labs" })).toBe("Senior Manager, Northwind Labs");
    expect(jobName({ title: "", employer: "Fabrikam" })).toBe("Fabrikam");
    expect(jobName({ title: "", employer: "" })).toBe("A job with no name");
  });

  it("counts the employers a run read, each once, however they are written", () => {
    expect(employersIn([{ employer: "Alpha" }, { employer: " alpha " }, { employer: "Beta" }])).toBe(2);
    expect(employersIn([])).toBe(0);
  });

  it("says what a run read: how many postings from how many employers, when, and which instructions", () => {
    const run = {
      postings: [{ employer: "A" }, { employer: "B" }, { employer: "B" }], checked_at: null, instructions_version: 2,
    } as unknown as GapRunView;
    expect(runLine(run)).toBe("3 postings from 2 employers · instructions version 2");
    expect(runLine({ ...run, postings: [{ employer: "A" }] as GapRunView["postings"], instructions_version: null })).toBe("1 posting from 1 employer");
  });
});

describe("addToResume", () => {
  it("adds Evidence to the chosen job, titled with the theme, with an id no line in the resume has", () => {
    const next = addToResume(RESUME, "eng", "evidence", "Data catalog", "  Rolled out a catalog.  ");
    expect(next?.roles[1].evidence).toEqual([{ id: "data-catalog-2", title: "Data catalog", text: "Rolled out a catalog." }]);
    expect(next?.roles[1].bullets).toEqual([]);
    expect(next?.roles[0]).toBe(RESUME.roles[0]);
  });

  it("adds a Bullet that prints: no note, no group, an id from its words", () => {
    const next = addToResume(RESUME, "lead", "bullet", "Anything", "Rolled out a data catalog.");
    expect(next?.roles[0].bullets).toEqual([
      { id: "b1", text: "Led the group", group: "G", note: "" },
      { id: "rolled-out-a-data-catalog", text: "Rolled out a data catalog.", note: "", group: "" },
    ]);
    expect(next?.roles[0].evidence).toEqual(RESUME.roles[0].evidence);
  });

  it("changes nothing else in the resume, and is null for a job that is not in it", () => {
    const next = addToResume(RESUME, "lead", "evidence", "Theme", "Text.");
    expect({ ...next, roles: [] }).toEqual({ ...RESUME, roles: [] });
    expect(next).not.toBe(RESUME);
    expect(RESUME.roles[0].evidence).toHaveLength(1);
    expect(addToResume(RESUME, "gone", "bullet", "Theme", "Text.")).toBeNull();
  });
});

describe("Working through the gaps' plain rules", () => {
  const card = (id: string, theme: string, grade: GapCard["grade"]): GapCard => ({
    id, theme, grade, question: "?", state: "open", answer: "", can_change: true,
  });

  it("groups the cards by the theme they ask about, in the order they come", () => {
    const groups = cardsByTheme([card("1", "Governance", "T"), card("2", "Governance", "T"), card("3", "Catalog", "U")]);
    expect(groups.map((g) => [g.theme, g.grade, g.cards.map((c) => c.id)])).toEqual([
      ["Governance", "T", ["1", "2"]],
      ["Catalog", "U", ["3"]],
    ]);
    expect(cardsByTheme([])).toEqual([]);
  });

  it("says where a line goes in the master's words", () => {
    expect(placeWords({ kind: "new_line", job: "Senior Manager, Northwind Labs", line: null })).toBe("A new line under Senior Manager, Northwind Labs");
    expect(placeWords({ kind: "evidence", job: "Senior Manager, Northwind Labs", line: "Led the group" })).toBe(
      "Evidence for “Led the group” (Senior Manager, Northwind Labs)",
    );
    expect(placeWords({ kind: "evidence", job: "Fabrikam", line: null })).toBe("Evidence under Fabrikam");
  });
});
