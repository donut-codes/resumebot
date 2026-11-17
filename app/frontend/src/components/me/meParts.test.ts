import type { Resume, Writing } from "../../api/types/me";
import { QA_PART, linesOf, preview, pruneResume, pruneWriting, settle, withStyleSwitch, wordCount } from "./meParts";

describe("linesOf", () => {
  it("counts a line for each break and one for each 80 characters, and nothing for nothing", () => {
    expect(linesOf("")).toBe(0);
    expect(linesOf("A short line.")).toBe(1);
    expect(linesOf("x".repeat(80))).toBe(1);
    expect(linesOf("x".repeat(81))).toBe(2);
    expect(linesOf("one\ntwo\n\nfour")).toBe(4);
    expect(linesOf("a", "", "b")).toBe(2);
  });
});

describe("words for a part", () => {
  it("counts and previews", () => {
    expect(wordCount("  ")).toBe(0);
    expect(wordCount("Plain, short and optimistic.")).toBe(4);
    expect(preview(["  one  ", "", "two\nlines", "three", "four"], 3)).toBe("one · two lines · three");
  });
});

describe("settle", () => {
  it("gives a line added since the last save an id from its words, unique among its siblings", () => {
    const out = settle(
      [{ id: "rule", says: "Keep it short" }, { id: "~new-1", says: "Rule" }, { id: "~new-2", says: "Rule" }, { id: "~new-3", says: "" }],
      (r) => r.says,
    );
    expect(out.map((r) => r.id)).toEqual(["rule", "rule-2", "rule-3", "item"]);
  });

  it("keeps the id of a saved line whose words begin \"New\", so packets that cite it still find it", () => {
    const saved = [{ id: "new-product-launch", says: "New product launch, edited" }, { id: "~new-1", says: "Fresh" }];
    expect(settle(saved, (r) => r.says).map((r) => r.id)).toEqual(["new-product-launch", "fresh"]);
  });

  it("keeps the ids of lines that were saved", () => {
    const saved = [{ id: "kept-one", says: "changed words" }];
    expect(settle(saved, (r) => r.says)).toEqual(saved);
  });

  it("makes an id the backend takes: lowercase words and hyphens, from the first words only", () => {
    const [one] = settle([{ id: "~new-9", says: "Led the (very) big, \"statewide\" project for the Department of Health" }], (r) => r.says);
    expect(one.id).toMatch(/^[a-z0-9][a-z0-9-]*$/);
    expect(one.id).toBe("led-the-very-big-statewide");
  });
});

describe("pruneResume", () => {
  const resume: Resume = {
    summary: "", summary_variants: [], achievements: [{ id: "a", text: "  ", note: "never printed" }, { id: "~new-1", text: "Did it", note: "" }],
    philosophy: "", competencies: [{ id: "c", area: "", items: [] }],
    roles: [
      { id: "r", employer: "", location: "", title: "", start: "", end: "", intro: "", descriptor: "", optional: false, note: "", tools: [], bullets: [], evidence: [] },
      {
        id: "s", employer: "Acme", location: "", title: "Lead", start: "", end: "", intro: "", descriptor: "", optional: false, note: "",
        tools: ["Visio", " ", "Miro"],
        bullets: [{ id: "b", text: "", group: "g", note: "" }, { id: "b2", text: "Kept", group: "", note: "" }],
        evidence: [{ id: "e", title: "", text: "" }, { id: "e2", title: "Board deck", text: "" }, { id: "~new-9", title: "Slide set", text: "Quarterly" }],
      },
    ],
    education: [], projects: [], awards: [], notes: [{ id: "n", text: "kept", source: "" }],
  };

  it("leaves out lines that were added and never written, and keeps everything else as it was", () => {
    const out = pruneResume(resume);
    expect(out.achievements.map((a) => a.id)).toEqual(["did-it"]);
    expect(out.competencies).toEqual([]);
    expect(out.roles.map((r) => r.id)).toEqual(["s"]);
    expect(out.roles[0].bullets.map((b) => b.id)).toEqual(["b2"]);
    expect(out.roles[0].evidence.map((e) => e.id)).toEqual(["e2", "slide-set"]);
    expect(out.roles[0].tools).toEqual(["Visio", "Miro"]);
    expect(out.notes).toEqual(resume.notes);
  });
});

describe("pruneWriting: the rules for the resume and the cover letter", () => {
  const empty = {
    voice_rules: [], ats_rules: [], interview_rules: [], resume_global: [], resume_header: [], resume_summary: [], resume_competencies: [],
    resume_achievements: [], resume_experience: [], resume_education: [], role_rules: {}, page_margin_inches: null,
    letter_global: [], letter_header: [], letter_greeting: [], letter_opening: [], letter_body: [], letter_closing: [], letter_signoff: [],
    letter_role_rules: {}, banned: [], legacy_patterns: [], review_steps: [], drift_patterns: [], review_flags: [], humanizing: [],
    style_off: [], credentials: [], notes: [],
  } as Writing;

  it("leaves out a rule never written in any section and in any role's list, and a role with none written", () => {
    const out = pruneWriting({
      ...empty,
      resume_summary: [{ id: "s", says: "Keep it short." }, { id: "~new-1", says: "  " }],
      resume_education: [{ id: "~new-2", says: "Degrees only." }],
      role_rules: { ops: [{ id: "~new-3", says: "" }], dev: [{ id: "a", says: "Lead with code." }, { id: "~new-4", says: "Name the stack." }] },
    });
    expect(out.resume_summary).toEqual([{ id: "s", says: "Keep it short." }]);
    expect(out.resume_education).toEqual([{ id: "degrees-only", says: "Degrees only." }]);
    expect(out.role_rules).toEqual({ dev: [{ id: "a", says: "Lead with code." }, { id: "name-the-stack", says: "Name the stack." }] });
  });

  it("keeps the rules of a role that is no longer in Search Settings, word for word", () => {
    const kept = { gone: [{ id: "old", says: "From a role since deleted." }] };
    expect(pruneWriting({ ...empty, role_rules: kept }).role_rules).toEqual(kept);
    expect(pruneWriting({ ...empty, letter_role_rules: kept }).letter_role_rules).toEqual(kept);
  });

  it("does the same for the cover letter's Global, each of its parts and its rules for each role, apart from the resume's", () => {
    const out = pruneWriting({
      ...empty,
      letter_global: [{ id: "g", says: "One page." }, { id: "~new-1", says: " " }],
      letter_opening: [{ id: "~new-2", says: "Open with the problem." }],
      letter_signoff: [{ id: "~new-3", says: "" }],
      letter_role_rules: { ops: [{ id: "~new-4", says: "" }], dev: [{ id: "~new-5", says: "Name the stack." }] },
      role_rules: { ops: [{ id: "r", says: "A resume rule." }] },
    });
    expect(out.letter_global).toEqual([{ id: "g", says: "One page." }]);
    expect(out.letter_opening).toEqual([{ id: "open-with-the-problem", says: "Open with the problem." }]);
    expect(out.letter_signoff).toEqual([]);
    expect(out.letter_role_rules).toEqual({ dev: [{ id: "name-the-stack", says: "Name the stack." }] });
    expect(out.role_rules).toEqual({ ops: [{ id: "r", says: "A resume rule." }] });
  });

  it("leaves out a certification never written, gives one just added the id of its words, and cuts no name short", () => {
    const long = `Certified ${"Example ".repeat(500)}Practitioner`;
    const out = pruneWriting({
      ...empty,
      credentials: [{ id: "cea", name: "Certified Example Analyst" }, { id: "~new-1", name: "  " }, { id: "~new-2", name: long }],
    });
    expect(out.credentials.map((c) => c.name)).toEqual(["Certified Example Analyst", long]);
    expect(out.credentials[0].id).toBe("cea");
    expect(out.credentials[1].id).toMatch(/^certified-example-example/);
  });

  it("keeps the style rules switched off as they are, an id it does not know too", () => {
    expect(pruneWriting({ ...empty, style_off: ["no-em-dash", "from-a-newer-app"] }).style_off).toEqual(["no-em-dash", "from-a-newer-app"]);
    expect(pruneWriting({ ...empty, style_off: [] }).style_off).toEqual([]);
  });
});

describe("the style rules' switches", () => {
  const RULES = [{ id: "a" }, { id: "b" }, { id: "c" }];

  it("switches a rule off and on, listing the ids off in the app's order", () => {
    expect(withStyleSwitch([], RULES, "b", false)).toEqual(["b"]);
    expect(withStyleSwitch(["c"], RULES, "a", false)).toEqual(["a", "c"]);
    expect(withStyleSwitch(["a", "c"], RULES, "a", true)).toEqual(["c"]);
    expect(withStyleSwitch(["a", "c"], RULES, "c", true)).toEqual(["a"]);
    expect(withStyleSwitch(["c"], RULES, "c", true)).toEqual([]);
  });

  it("puts a switch back to exactly what was saved, so there is nothing to save", () => {
    const saved = ["a", "c"];
    expect(withStyleSwitch(withStyleSwitch(saved, RULES, "a", true), RULES, "a", false)).toEqual(saved);
    expect(withStyleSwitch(withStyleSwitch(saved, RULES, "b", false), RULES, "b", true)).toEqual(saved);
  });

  it("keeps an id it does not know, after the ones it does, and does not add one twice", () => {
    expect(withStyleSwitch(["old", "b"], RULES, "a", false)).toEqual(["a", "b", "old"]);
    expect(withStyleSwitch(["old", "b"], RULES, "b", true)).toEqual(["old"]);
    expect(withStyleSwitch(["a"], RULES, "a", false)).toEqual(["a"]);
  });

  it("does not change the list it is given", () => {
    const off = ["c"];
    withStyleSwitch(off, RULES, "a", false);
    expect(off).toEqual(["c"]);
  });
});

describe("the QA part's fields", () => {
  it("holds the style switches and the certifications, so the part shows as changed when either changes", () => {
    expect(QA_PART.keys).toEqual(expect.arrayContaining(["review_steps", "drift_patterns", "review_flags", "banned", "humanizing", "style_off", "credentials"]));
  });
});
