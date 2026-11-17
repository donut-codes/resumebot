import type { ParsedResume } from "../../api/types/profile";
import { SIMILAR_NOTE, likeness, planImport } from "./importMerge";
import { pruneResume, type Master } from "./meParts";

// Made up: no real person's resume or contact details.
const NO_CONTACT = { name: "", location: "", address: "", phone: "", email: "", links: [] };
const NOTHING: ParsedResume = {
  summary: "", achievements: [], competencies: [], roles: [], education: [], philosophy: "", awards: [], projects: [], contact: NO_CONTACT,
};
const HELD: Master = {
  summary: "I run projects and the teams that run them.",
  summary_variants: [{ id: "v", text: "A variant", note: "" }],
  achievements: [{ id: "a", text: "Opened a second office in a year", note: "approved phrasing" }],
  philosophy: "People first.",
  competencies: [{ id: "c", area: "Tools", items: ["Ticket tracker"] }],
  roles: [
    {
      id: "now", employer: "Northwind Labs", location: "", title: "Project Manager", start: "2019", end: "Present", intro: "", descriptor: "",
      optional: false, note: "", tools: ["Wiki"], evidence: [{ id: "ev", title: "Detail", text: "Kept." }],
      bullets: [
        { id: "b1", text: "I was the ticket tracker admin for the whole office", group: "", note: "" },
        { id: "b2", text: "I trained new staff on the wiki", group: "Training", note: "past tense" },
      ],
    },
    {
      id: "before", employer: "Fabrikam", location: "Oakfield, ST", title: "Analyst", start: "2012", end: "2015", intro: "", descriptor: "",
      optional: true, note: "A role note", tools: [], evidence: [], bullets: [{ id: "f1", text: "I wrote the weekly report", group: "", note: "" }],
    },
  ],
  education: [{ id: "e", degree: "BA in History", school: "Example College", note: "" }],
  projects: [{ id: "p", text: "A community garden", note: "" }],
  awards: [],
  notes: [{ id: "n", text: "kept", source: "" }],
  name: "Sam Sample", location: "Riverton, ST", metro_location: "", address: "", phone: "", email: "sam@example.test", links: [],
  work_authorization: "Yes", sponsorship: "No", current_company: "",
};

const read = (over: Partial<ParsedResume>): ParsedResume => ({ ...NOTHING, ...over });
const role = (over: Partial<ParsedResume["roles"][number]>): ParsedResume["roles"][number] => ({
  employer: "", title: "", location: "", start: "", end: "", intro: "", descriptor: "", bullets: [], ...over,
});

/** Every line the master held is still in `out`, with its id, words and note. */
function keepsEverything(out: Master) {
  for (const a of HELD.achievements) expect(out.achievements).toContainEqual(a);
  for (const x of HELD.roles) {
    const now = out.roles.find((o) => o.id === x.id)!;
    expect(now).toBeDefined();
    expect(now.tools).toEqual(x.tools);
    expect(now.evidence).toEqual(x.evidence);
    expect(now.note).toBe(x.note);
    expect(now.optional).toBe(x.optional);
    for (const b of x.bullets) expect(now.bullets).toContainEqual(b);
  }
  for (const e of HELD.education) expect(out.education).toContainEqual(e);
  for (const p of HELD.projects) expect(out.projects).toContainEqual(p);
  for (const c of HELD.competencies) for (const t of c.items) expect(out.competencies.find((o) => o.id === c.id)!.items).toContain(t);
  for (const v of HELD.summary_variants) expect(out.summary_variants).toContainEqual(v);
  expect(out.summary).toBe(HELD.summary);
  expect(out.notes).toEqual(HELD.notes);
}

describe("planImport: an import adds to the master and never takes away", () => {
  it("keeps every line, role, example and fact the master held, and changes nothing in what it was given", () => {
    const before = structuredClone(HELD);
    const plan = planImport(HELD, read({
      summary: "Something else entirely about building things.",
      achievements: ["Cut the backlog by half"],
      roles: [role({ employer: "Northwind Labs", title: "Project Manager", start: "2018", end: "2023", intro: "Ran the office.", bullets: ["I set up workflows"] })],
      education: [{ degree: "MS in Leadership", school: "Example University" }],
      philosophy: "Different words.",
      contact: { ...NO_CONTACT, name: "Another Name", phone: "555-0111" },
    }));
    keepsEverything(plan.merged);
    expect(HELD).toEqual(before);
    expect(plan.merged.name).toBe("Sam Sample"); // a fact the owner has is never replaced
    expect(plan.merged.phone).toBe("555-0111"); // an empty one is filled
    expect(plan.merged.roles[0]).toMatchObject({ start: "2019", end: "Present" }); // dates the owner has are kept
    expect(plan.merged.philosophy).toBe("People first.\n\nDifferent words."); // added after, never in place
    expect(plan.merged.summary_variants.map((v) => v.text)).toEqual(["A variant", "Something else entirely about building things."]);
  });

  it("changes nothing for a resume that gave nothing", () => {
    const plan = planImport(HELD, NOTHING);
    expect(plan.merged).toEqual(HELD);
    expect(plan.groups).toEqual([]);
    expect(plan.counts).toEqual({ new: 0, similar: 0, duplicate: 0 });
  });

  it("skips a line the master already holds, whatever its case and punctuation", () => {
    const plan = planImport(HELD, read({
      achievements: ["opened a second office in a year."],
      roles: [role({ employer: "Northwind Labs", title: "Project Manager", bullets: ["I trained new staff on the wiki", "I set up projects"] })],
      projects: ["A community garden"],
      competencies: [{ area: "Tools", items: ["ticket tracker", "Wiki"] }],
    }));
    expect(plan.merged.achievements).toEqual(HELD.achievements);
    expect(plan.merged.projects).toEqual(HELD.projects);
    expect(plan.merged.roles[0].bullets.map((b) => b.text)).toEqual([...HELD.roles[0].bullets.map((b) => b.text), "I set up projects"]);
    expect(plan.merged.competencies[0].items).toEqual(["Ticket tracker", "Wiki"]);
    expect(plan.counts).toEqual({ new: 2, similar: 0, duplicate: 4 });
    const roleGroup = plan.groups.find((g) => g.key === "role-now")!;
    expect(roleGroup.lines).toEqual([
      { text: "I trained new staff on the wiki", fate: "duplicate", like: "I trained new staff on the wiki" },
      { text: "I set up projects", fate: "new" },
    ]);
  });

  it("keeps a near repeat, right under the line it resembles, marked as similar", () => {
    const plan = planImport(HELD, read({
      roles: [role({ employer: "Northwind Labs", title: "Project Manager", bullets: ["I was the ticket tracker admin for the office", "I set up workflows"] })],
    }));
    const bullets = plan.merged.roles[0].bullets;
    expect(bullets.map((b) => [b.text, b.note])).toEqual([
      ["I was the ticket tracker admin for the whole office", ""],
      ["I was the ticket tracker admin for the office", SIMILAR_NOTE],
      ["I trained new staff on the wiki", "past tense"],
      ["I set up workflows", ""],
    ]);
    expect(plan.groups.find((g) => g.key === "role-now")!.lines[0]).toEqual({
      text: "I was the ticket tracker admin for the office", fate: "similar", like: "I was the ticket tracker admin for the whole office",
    });
    expect(likeness("I set up workflows", "I trained new staff on the wiki")).toBeLessThan(0.6);
    // Short plain lines that share only a verb are different facts, not near repeats.
    expect(likeness("set up projects", "set up workflows")).toBe(0);
  });

  it("files each line under the role it matches by employer and title, or employer and start year, and makes a new role for the rest", () => {
    const plan = planImport(HELD, read({
      roles: [
        role({ employer: "Contoso", title: "Director", start: "2023", end: "Present", bullets: ["I lead the project office"] }),
        role({ employer: "northwind labs", title: "PROJECT MANAGER", bullets: ["I set up workflows"] }),
        role({ employer: "Fabrikam", title: "Senior Analyst", start: "Mar 2012", end: "2015", bullets: ["I trained people"] }),
        role({ employer: "Fabrikam", title: "Intern", start: "2010", end: "2011", bullets: ["I filed things"] }),
      ],
    }));
    const roles = plan.merged.roles;
    // The new role goes where this resume put it: before the next role it lists that the master holds; the rest go last.
    expect(roles.map((r) => [r.title, r.employer])).toEqual([
      ["Director", "Contoso"], ["Project Manager", "Northwind Labs"], ["Analyst", "Fabrikam"], ["Intern", "Fabrikam"],
    ]);
    expect(roles[1].bullets.at(-1)!.text).toBe("I set up workflows");
    expect(roles[2].bullets.at(-1)!.text).toBe("I trained people"); // the same employer and start year: the same job
    expect(roles[2].title).toBe("Analyst"); // its title is the owner's
    expect(roles[0]).toMatchObject({ start: "2023", end: "Present", optional: false, tools: [], evidence: [] });
    expect(roles[0].bullets.map((b) => b.text)).toEqual(["I lead the project office"]);
    expect(plan.groups.map((g) => [g.title, g.where])).toEqual([
      ["Director, Contoso", "New role (2023 – Present)"],
      ["Project Manager, Northwind Labs", "Already in your resume (2019 – Present)"],
      ["Analyst, Fabrikam", "Already in your resume (2012 – 2015)"],
      ["Intern, Fabrikam", "New role (2010 – 2011)"],
    ]);
    expect(plan.groups[2].notes).toEqual(["Dates differ: yours say 2012 – 2015, this resume says Mar 2012 – 2015. Yours are kept."]);
  });

  it("fills in a role's missing dates and details, and says which it kept", () => {
    const held: Master = { ...HELD, roles: [{ ...HELD.roles[1], start: "", end: "" }] };
    const plan = planImport(held, read({ roles: [role({ employer: "Fabrikam", title: "Analyst", start: "2012", end: "2015", location: "Elmwood, ZZ", descriptor: "a retailer" })] }));
    expect(plan.merged.roles[0]).toMatchObject({ start: "2012", end: "2015", location: "Oakfield, ST", descriptor: "a retailer" });
    const g = plan.groups[0];
    expect(g.notes).toEqual(["Start date filled in: 2012.", "End date filled in: 2015.", 'Place: yours is kept ("Oakfield, ST"); this resume says "Elmwood, ZZ".']);
    expect(g.lines).toEqual([{ text: "a retailer", fate: "new", label: "What the employer is" }]);
  });

  it("files a skill under its area, or a new area, and skips one held under any area", () => {
    const plan = planImport(HELD, read({ competencies: [{ area: "tools", items: ["Spreadsheets"] }, { area: "Training", items: ["Onboarding", "Ticket Tracker"] }] }));
    expect(plan.merged.competencies.map((c) => [c.area, c.items])).toEqual([["Tools", ["Ticket tracker", "Spreadsheets"]], ["Training", ["Onboarding"]]]);
  });

  it("gives every added line a fresh id that is settled from its words on save, and keeps the ids it had", () => {
    const plan = planImport(HELD, read({ achievements: ["Cut the backlog by half"], roles: [role({ employer: "Contoso", title: "Director", bullets: ["I lead the project office"] })] }));
    const saved = pruneResume(plan.merged);
    expect(saved.achievements.map((a) => a.id)).toEqual(["a", "cut-the-backlog-by-half"]);
    expect(saved.roles.map((r) => r.id)).toEqual(["now", "before", "director-contoso"]);
  });
});
