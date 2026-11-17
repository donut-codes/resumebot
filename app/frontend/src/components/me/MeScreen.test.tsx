import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Documents } from "../../api/types/me";
import { confirmLeave, LEAVE_QUESTION } from "../../lib/unsavedGuard";
import type { MePage } from "../../lib/route";
import { json, render, stubFetch } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import MeScreen from "./MeScreen";
import { BANNED_LINE, CHECKED_LINE, FOLD_OVER_LINES, QA_LINE, REVIEW_LINE } from "./meParts";

// Nothing in this file waits: every test draws the whole master resume (about a hundred fields) and queries it by role,
// which is CPU. A test takes 0.2 to 0.3 s alone, 0.6 to 0.9 s in a full run and 2 s or more with several runs at once, which
// can pass the default 5 s. So this file, and only this file, has a limit sized for that load.
vi.setConfig({ testTimeout: 15_000 });

// Made up: no real person's resume, contact details or rules.
const DOCS: Documents = {
  resume: {
    summary: "Builds projects that people use.",
    summary_variants: [],
    achievements: [{ id: "a1", text: "Led a statewide project", note: "approved phrasing" }],
    philosophy: "People first.",
    competencies: [{ id: "gov", area: "Governance", items: ["Risk Frameworks", "Policy Design"] }],
    roles: [
      {
        id: "lead", employer: "Northwind Labs", location: "Oakfield, ST", title: "Senior Manager", start: "2022", end: "2026",
        intro: "Sole leader of the practice.", descriptor: "", optional: false, note: "Use Senior Manager in cuts.",
        tools: ["Visio", "Miro"], evidence: [{ id: "board", title: "Board deck", text: "Presented the risk register each quarter" }],
        bullets: [
          { id: "b1", text: "Led the risk working group", group: "Governance", note: "Never an absolute." },
          { id: "b2", text: "Built the knowledge base", group: "Governance", note: "" },
        ],
      },
      {
        id: "eng", employer: "Fabrikam", location: "", title: "Engineer", start: "2012", end: "2015", intro: "", descriptor: "",
        optional: true, note: "", tools: [], bullets: [], evidence: [],
      },
    ],
    education: [{ id: "ms", degree: "MS in Leadership", school: "Example University", note: "" }],
    projects: [],
    awards: [{ id: "aw", text: "Rated highly", note: "" }],
    notes: [{ id: "todo", text: "- [ ] Confirm the start month", source: "TODO" }],
  },
  about: {
    name: "Pat Example", location: "Oakfield, ST", metro_location: "Springdale, ST", address: "", phone: "555-010-0199",
    email: "pat@example.test", links: ["profiles.example.test/in/example"], work_authorization: "Yes", sponsorship: "No",
    current_company: "",
    notes: [{ id: "old", text: "Something kept from before", source: "an old file" }],
  },
  writing: {
    voice_rules: [
      { id: "voice", says: "Plain, short and optimistic." },
      { id: "rule-1", says: "Clients are never named. Use sector and scale." },
    ],
    ats_rules: [{ id: "keywords", says: "Use the posting's own words for skills." }], interview_rules: [],
    resume_global: [
      { id: "format-0", says: "Name the problem first." },
      { id: "one-page", says: "A resume is one page unless a posting asks for more." },
      { id: "pdf", says: "Send a PDF, never a Word file." },
    ],
    resume_header: [],
    resume_summary: [],
    resume_competencies: [],
    resume_achievements: [],
    resume_experience: [],
    resume_education: [],
    role_rules: {},
    page_margin_inches: null,
    letter_global: [{ id: "three", says: "A letter is three paragraphs at most." }],
    letter_header: [],
    letter_greeting: [],
    letter_opening: [],
    letter_body: [],
    letter_closing: [],
    letter_signoff: [],
    letter_role_rules: {},
    banned: [{ id: "b", phrase: "spearheaded", why: "puffery" }],
    legacy_patterns: [{ id: "yrs", pattern: "\\d+ years", regex: true, why: "dates the owner" }],
    review_steps: [{ id: "step-1", says: "Read the sources yourself." }],
    drift_patterns: [{ id: "scope", name: "Scope creep by one word", looks_like: "led AI for the firm", fix: "Use the master's noun" }],
    review_flags: [{ id: "flag-1", says: "Hard gates in the posting." }],
    humanizing: [],
    style_off: [],
    credentials: [],
    notes: [{ id: "oldw", text: "A writing note kept from before", source: "an old file" }],
  },
  versions: { resume: 1, about: 1, writing: 1 },
};

/** Two target roles of the Search Settings. */
const TARGET_ROLES = [
  { id: "ops", name: "Operations Lead" },
  { id: "dev", name: "Developer" },
];

const BUILT_IN = {
  checks: [
    { id: "years-of-experience", name: "A count of years of experience", rule: "Show what was done, not how long.", backs: "Nothing you ban appears" },
    { id: "headcount-flex", name: "A headcount used as a flex", rule: "Describe scope by what was led.", backs: "Nothing you ban appears" },
  ],
  style: [
    { id: "no-em-dash", name: "No em dash", rule: "Use a comma, a period or a new sentence. A dash between two dates is fine." },
    { id: "no-inline-markup", name: "No markup", rule: "Bold, italics and other markup print as typed in the Word file. Write plain text; the app sets its own labels." },
    { id: "no-year-by-degree", name: "No year beside a degree", rule: "A year next to a degree tells a reader your age. Name the degree and the school." },
    { id: "no-pre-conceding", name: "No phrase that concedes a gap first", rule: "Say nothing about a gap, or say one confident sentence. Never open with what you lack." },
    { id: "no-weak-words", name: "No weak or inflated words", rule: "Words like 'responsible for', 'various' and 'leveraged' say little and sound padded." },
  ],
};
interface Put {
  url: string;
  body: { data?: Record<string, unknown>; why: string; base_version?: number };
}

/** The address of every request the page made, since the last `serve`. */
const requested: string[] = [];

/** A server that keeps what is saved, so the page loaded again shows it. */
function serve(start: Documents = DOCS) {
  let docs = structuredClone(start);
  const puts: Put[] = [];
  requested.length = 0;
  stubFetch((input: string, init?: RequestInit) => {
    requested.push(input);
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      puts.push({ url: input, body });
      const kind = /\/profile\/(\w+)$/.exec(input)?.[1];
      if (kind) docs = { ...docs, [kind]: body.data };
      return json({ saved: true, version: 2 });
    }
    if (input.endsWith("/api/v1/documents")) return json(docs);
    if (input.endsWith("/api/v1/documents/built-in-checks")) return json(BUILT_IN);
    if (input.endsWith("/api/v1/profile")) return json({ criteria: { target_roles: TARGET_ROLES } });
    const history =/\/api\/v1\/profile\/(\w+)\/history$/.exec(input);
    if (history) {
      return json({ kind: history[1], versions: [{ version: 3, saved_by: "owner", why: `kept as ${history[1]}`, saved_at: null, changed: [`changed ${history[1]} things`] }] });
    }
    return json({}, 404);
  });
  return puts;
}

function open(part: MePage) {
  return render(
    <ToastProvider>
      <MeScreen />
    </ToastProvider>,
    { hash: part === "resume" ? "#/me" : `#/me/${part}` },
  );
}

/** The button in the heading of a part (a second-level heading) or of a role (a third-level one). */
const head = (name: string | RegExp, level: 2 | 3 = 2) => within(screen.getByRole("heading", { level, name })).getByRole("button");
/** A part of the page (the section its heading names): a query inside it looks at its own fields, not at one for every line of the page. */
const part = (name: string) => screen.getByRole("region", { name });
const bar = () => screen.queryByRole("form", { name: "Unsaved changes" });
/** The names of the parts of the page, top to bottom: the second-level headings that fold (the sections beside them do not). */
const partNames = () => screen.getAllByRole("heading", { level: 2 }).filter((h) => h.querySelector("button")).map((h) => h.textContent);
const type = (name: string, value: string) => fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } });

describe("Me: the master resume", () => {
  it("is a list of parts: the short ones open, one over about 30 lines folded with what is in it", async () => {
    // It is the first test of the file, so it also pays the cold start (the first render of the whole master resume, the
    // first role queries), and is kept light: one line over the fold and no more, each query inside its part (a role query
    // names every match it looks at, so a page-wide one names every field on the page).
    const awards = Array.from({ length: FOLD_OVER_LINES + 1 }, (_, i) => ({ id: `aw${i}`, text: `Award number ${i}`, note: "" }));
    serve({ ...DOCS, resume: { ...DOCS.resume, awards } });
    open("resume");
    expect(await screen.findByRole("heading", { level: 1, name: "My Resume" })).toBeInTheDocument();
    expect(head("Summary")).toHaveAttribute("aria-expanded", "true");
    expect(within(part("Summary")).getByRole("textbox", { name: "Summary" })).toHaveValue("Builds projects that people use.");
    const folded = head("Awards and recognition");
    expect(folded).toHaveAttribute("aria-expanded", "false");
    // Folded, it says how many and what is in it, and draws none of its fields.
    expect(screen.getByText(`${awards.length} awards`)).toBeInTheDocument();
    expect(screen.getByText(/Award number 0 · Award number 1 · Award number 2/)).toBeInTheDocument();
    expect(within(part("Awards and recognition")).queryByRole("textbox", { name: "Award 1" })).toBeNull();
    fireEvent.click(folded);
    expect(folded).toHaveAttribute("aria-expanded", "true");
    expect(within(part("Awards and recognition")).getByRole("textbox", { name: `Award ${awards.length}` })).toHaveValue(`Award number ${awards.length - 1}`);
    expect(screen.queryByText(/Award number 0 · /)).toBeNull();
    fireEvent.click(folded);
    expect(folded).toHaveAttribute("aria-expanded", "false");
  });

  it("lists its six parts in the rail, each a link, and has no page index of its own", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const rail = screen.getByRole("navigation", { name: "My Resume" });
    expect(rail).toHaveClass("fi-rail");
    expect(within(rail).getAllByRole("link").map((l) => l.textContent)).toEqual([
      "Master resume", "Find Gaps", "Story library", "Resume Rules", "Cover Letter Rules", "Document Creation Rules",
    ]);
    expect(within(rail).getByRole("link", { name: "Master resume" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("navigation", { name: "Jump to a part" })).toBeNull();
    expect(screen.queryByText("On this page")).toBeNull();
  });

  it("shows no 'kept as written' sections, no 'also in the master' card and no Edit button", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(screen.queryByText(/Kept as written|not placed yet|From your old/)).toBeNull();
    expect(screen.queryByText(/Confirm the start month/)).toBeNull();
    expect(screen.queryByText("Also in the master")).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(head("Leadership philosophy")).toBeInTheDocument();
  });

  it("puts the history on the line under the tabs, beside what the page is", async () => {
    serve();
    open("resume");
    const history = await screen.findByRole("button", { name: "History: 1 version" });
    expect(history.closest(".fi-me-head")).toContainElement(screen.getByText(/^The single source every tailored resume/));
    fireEvent.click(history);
    expect(await screen.findByText("changed resume things")).toBeInTheDocument();
    // The history is a section of its own, with its name over its sheet.
    expect(screen.getByRole("heading", { level: 2, name: "History" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide history" })).toBeInTheDocument();
  });

  describe("exports the saved master as a JSON Resume file (docs/PRD.md 8.4)", () => {
    const EXPORT = "Export the saved master (JSON Resume)";
    const NOT_IN_IT = "Your unsaved changes are not in it.";
    const BLANK: Documents["resume"]["roles"][number] = {
      id: "blank", employer: "", location: "", title: "", start: "", end: "", intro: "", descriptor: "", optional: false, note: "",
      tools: [], bullets: [], evidence: [],
    };

    it("links the version in force as a download of resume.json, in the tools line above the parts", async () => {
      serve();
      open("resume");
      const link = await screen.findByRole("link", { name: EXPORT });
      expect(link).toHaveAttribute("href", "/api/v1/documents/resume/export.json");
      expect(link).toHaveAttribute("download", "resume.json");
      expect(link.closest(".fi-me-tools")).toContainElement(screen.getByRole("checkbox", { name: "Show notes and groups" }));
      expect(screen.queryByText(NOT_IN_IT)).toBeNull();
    });

    it.each([
      ["never saved", { ...DOCS, resume: { ...DOCS.resume, summary: "", roles: [], achievements: [] }, versions: { ...DOCS.versions, resume: 0 } }],
      // The page has something (an achievement) but the file would hold nothing: achievements are not exported.
      ["saved with no summary and no role", { ...DOCS, resume: { ...DOCS.resume, summary: "  ", roles: [] } }],
      // A role with nothing in it is no role: the server leaves it out of the file, so the file would hold nothing.
      ["saved with no summary and only a role with nothing in it", { ...DOCS, resume: { ...DOCS.resume, summary: "", roles: [BLANK] } }],
    ])("has no link for a master that was %s", async (_name, docs) => {
      serve(docs);
      open("resume");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      await screen.findByRole("textbox", { name: "Summary" });
      expect(screen.queryByRole("link", { name: EXPORT })).toBeNull();
      expect(screen.queryByText(NOT_IN_IT)).toBeNull();
    });

    it("has a link for a master with no summary whose only role has just a bullet in it", async () => {
      serve({ ...DOCS, resume: { ...DOCS.resume, summary: "", roles: [{ ...BLANK, bullets: [{ ...DOCS.resume.roles[0].bullets[0], text: "Led the group" }] }] } });
      open("resume");
      expect(await screen.findByRole("link", { name: EXPORT })).toBeInTheDocument();
    });

    it("says the unsaved changes are not in the file only while there are some", async () => {
      serve();
      open("resume");
      await screen.findByRole("link", { name: EXPORT });
      expect(screen.queryByText(NOT_IN_IT)).toBeNull();
      type("Summary", "Builds projects and the teams that run them.");
      expect(screen.getByText(NOT_IN_IT)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: EXPORT })).toHaveAttribute("href", "/api/v1/documents/resume/export.json");
      fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(bar()).toBeNull());
      expect(screen.queryByText(NOT_IN_IT)).toBeNull();
      expect(screen.getByRole("link", { name: EXPORT })).toBeInTheDocument();
    });

    it("puts an Export link on each version in the history of the master resume, to that version's own file", async () => {
      serve();
      open("resume");
      fireEvent.click(await screen.findByRole("button", { name: "History: 1 version" }));
      await screen.findByText("changed resume things");
      const link = screen.getByRole("link", { name: "Export version 3 (JSON Resume)" });
      expect(link).toHaveTextContent("Export");
      expect(link).toHaveAttribute("href", "/api/v1/documents/resume/versions/3/export.json");
      expect(link).toHaveAttribute("download", "resume.json");
      expect(link.closest("li")).toHaveTextContent("Version 3");
      // No restore and no view: the row has the one link.
      expect(within(link.closest("li")!).queryAllByRole("button")).toHaveLength(0);
    });

    it.each(["resume-rules", "letter-rules", "document-rules"] as const)("puts no Export link in the history of the writing rules on %s", async (page) => {
      serve();
      open(page);
      fireEvent.click(await screen.findByRole("button", { name: "History: 1 version" }));
      await screen.findByText("changed writing things");
      expect(screen.queryByRole("link", { name: /Export/ })).toBeNull();
    });
  });

  it("edits in place: the save bar names the part, Save sends the whole resume with why, and says Saved", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(bar()).toBeNull();
    type("Summary", "Builds projects and the teams that run them.");
    const form = bar()!;
    expect(form).toHaveTextContent("Unsaved changes to Summary");
    expect(head(/^Summary/)).toHaveAccessibleName("Summary, unsaved changes");
    fireEvent.change(within(form).getByLabelText("Why (optional, one line)"), { target: { value: "sharper opener" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved")).toHaveClass("toast");
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts).toHaveLength(1);
    expect(puts[0].url).toMatch(/\/api\/v1\/profile\/resume$/);
    expect(puts[0].body.why).toBe("sharper opener");
    expect(puts[0].body.data).toMatchObject({ summary: "Builds projects and the teams that run them.", philosophy: "People first." });
    // What the page does not show is kept as it was.
    expect(puts[0].body.data?.notes).toEqual(DOCS.resume.notes);
    expect(puts[0].body.data?.summary_variants).toEqual([]);
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Builds projects and the teams that run them.");
  });

  it("a line's only tool is Remove, the oxblood text button: no Up or Down, since the order changes nothing that comes out", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    // The first line's tools (a table's heading row has an empty tools cell).
    const tools = Array.from(document.querySelectorAll(".fi-me-item-tools")).find((t) => t.querySelector("button")) as HTMLElement;
    const buttons = Array.from(tools.querySelectorAll("button"));
    expect(buttons).toHaveLength(1);
    const [remove] = buttons;
    expect(remove).toHaveTextContent("Remove");
    expect(remove).toHaveClass("fi-link", "fi-me-remove");
    expect(remove).not.toHaveClass("fi-btn");
    // Nowhere on the page, in any line or any role, is there a control that moves a line.
    expect(screen.queryAllByRole("button", { name: /^Move / })).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: /^(Up|Down)$/ })).toHaveLength(0);
    // One tool in every line: each line (the achievement, the bullets) has just its Remove.
    const columns = Array.from(document.querySelectorAll(".fi-me-items .fi-me-item-tools"));
    expect(columns.length).toBeGreaterThan(3);
    for (const column of columns) {
      expect(Array.from(column.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Remove"]);
    }
  });

  it("removing a line keeps the others in the order they are stored in", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.click(screen.getByRole("button", { name: "Remove Led the risk working group" }));
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    const roles = puts[0].body.data?.roles as { id: string; bullets: { id: string }[] }[];
    expect(roles.map((r) => r.id)).toEqual(["lead", "eng"]);
    expect(roles[0].bullets.map((b) => b.id)).toEqual(["b2"]);
  });

  it("puts every edit back with Undo, a removed line too", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Summary", "Something else.");
    fireEvent.click(screen.getByRole("button", { name: "Remove Led a statewide project" }));
    expect(screen.queryByRole("textbox", { name: "Achievement 1" })).toBeNull();
    expect(bar()).toHaveTextContent("Unsaved changes to Summary and Selected achievements");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(bar()).toBeNull();
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Builds projects that people use.");
    expect(screen.getByRole("textbox", { name: "Achievement 1" })).toHaveValue("Led a statewide project");
  });

  it("saves with Ctrl or Cmd and S, and keeps the browser's own dialog off", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    // (The listener is set once the page's effects have run, a moment after it shows.)
    await waitFor(() => expect(fireEvent.keyDown(document.body, { key: "s", ctrlKey: true })).toBe(false));
    expect(puts).toHaveLength(0);
    type("Summary", "By keyboard.");
    fireEvent.keyDown(document.body, { key: "S", metaKey: true });
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
  });

  it("asks before the page is left with changes that are not saved", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const ask = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      expect(confirmLeave()).toBe(true);
      expect(ask).not.toHaveBeenCalled();
      type("Summary", "Not saved yet.");
      expect(confirmLeave()).toBe(false);
      expect(ask).toHaveBeenCalledWith(LEAVE_QUESTION);
      fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
      ask.mockClear();
      expect(confirmLeave()).toBe(true);
      expect(ask).not.toHaveBeenCalled();
    } finally {
      ask.mockRestore();
    }
  });

  it("has no Tidy up on any of the four pages: the line under the tabs holds History", async () => {
    for (const part of ["resume", "resume-rules", "letter-rules", "document-rules"] as const) {
      serve();
      const view = open(part);
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      expect(screen.getByRole("button", { name: /^History/ })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Tidy up" })).toBeNull();
      type({ resume: "Summary", "resume-rules": "Global rule 1", "letter-rules": "Global rule 1", "document-rules": "Voice rule 1" }[part], "Changed.");
      expect(screen.queryByText(/tidy/i)).toBeNull();
      view.unmount();
    }
  });

  it("adds a line that takes focus, leaves out one never written, and gives a written one an id from its words", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.click(screen.getByRole("button", { name: "Add an achievement" }));
    expect(screen.getByRole("textbox", { name: "Achievement 2" })).toHaveFocus();
    // A blank line is not a change: there is nothing to save.
    expect(bar()).toBeNull();
    type("Achievement 2", "Opened a second office in a year");
    expect(bar()).toHaveTextContent("Unsaved changes to Selected achievements");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    const saved = puts[0].body.data?.achievements as { id: string; text: string }[];
    expect(saved.map((a) => a.id)).toEqual(["a1", "opened-a-second-office-in"]);
    expect(saved[1].text).toBe("Opened a second office in a year");
  });

  it("shows a role's bullets, and its notes and groups only when asked", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(head("Senior Manager, Northwind Labs", 3)).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Optional")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Bullet 1 of Senior Manager, Northwind Labs" })).toHaveValue("Led the risk working group");
    expect(screen.queryByDisplayValue("Never an absolute.")).toBeNull();
    expect(screen.queryByDisplayValue("approved phrasing")).toBeNull();
    fireEvent.click(screen.getByLabelText("Show notes and groups"));
    expect(screen.getByDisplayValue("Never an absolute.")).toBeInTheDocument();
    expect(screen.getByDisplayValue("approved phrasing")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Use Senior Manager in cuts.")).toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue("Never an absolute."), { target: { value: "Never an absolute; say about." } });
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    const roles = puts[0].body.data?.roles as { bullets: { note: string }[] }[];
    expect(roles[0].bullets[0].note).toBe("Never an absolute; say about.");
  });

  it("starts the roles folded when Experience is long, each saying how many bullets it has and its first two", async () => {
    const bullets = Array.from({ length: 31 }, (_, i) => ({ id: `x${i}`, text: `Did thing ${i}`, group: "", note: "" }));
    serve({ ...DOCS, resume: { ...DOCS.resume, roles: [{ ...DOCS.resume.roles[0], bullets }, DOCS.resume.roles[1]] } });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    // Experience holds more than 30 lines, so it starts folded; its summary names the roles.
    expect(head("Experience")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("2 roles")).toBeInTheDocument();
    fireEvent.click(head("Experience"));
    expect(head("Senior Manager, Northwind Labs", 3)).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("31 bullets: Did thing 0 · Did thing 1")).toBeInTheDocument();
    expect(head("Engineer, Fabrikam", 3)).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("No bullets yet")).toBeInTheDocument();
    fireEvent.click(head("Engineer, Fabrikam", 3));
    expect(screen.getByRole("textbox", { name: "Employer" })).toHaveValue("Fabrikam");
    type("Employer", "Fabrikam Two");
    expect(bar()).toHaveTextContent("Unsaved changes to Experience");
  });

  it("keeps a role's tools and evidence in the role, always shown; a new entry is saved and a blank one is left out", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(screen.queryByRole("heading", { name: "Evidence library" })).toBeNull();
    const role = "Senior Manager, Northwind Labs";
    expect(screen.getAllByRole("textbox", { name: "Tools used" })[0]).toHaveValue("Visio; Miro");
    expect(screen.getByRole("textbox", { name: `Evidence title in ${role}` })).toHaveValue("Board deck");
    expect(screen.getByRole("textbox", { name: `Evidence detail in ${role}` })).toHaveValue("Presented the risk register each quarter");
    expect(screen.getAllByText("Never printed: detail from this job the analyst cites and the writer draws on.")).toHaveLength(2);
    fireEvent.change(screen.getAllByRole("textbox", { name: "Tools used" })[0], { target: { value: "Visio; Miro; Tableau;" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Add evidence" })[0]);
    fireEvent.change(screen.getAllByRole("textbox", { name: `Evidence title in ${role}` })[1], { target: { value: "Slide set" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Add evidence" })[0]);
    expect(bar()).toHaveTextContent("Unsaved changes to Experience");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    const roles = puts[0].body.data?.roles as { tools: string[]; evidence: { id: string; title: string }[] }[];
    expect(roles[0].tools).toEqual(["Visio", "Miro", "Tableau"]);
    expect(roles[0].evidence.map((e) => [e.id, e.title])).toEqual([["board", "Board deck"], ["slide-set", "Slide set"]]);
    expect(puts[0].body.data).not.toHaveProperty("evidence");
  });

  it("says plainly when there is no resume yet, and the tabs move between the pages", async () => {
    serve({ ...DOCS, resume: { ...DOCS.resume, summary: "", roles: [], achievements: [] } });
    open("resume");
    const empty = await screen.findByText(/No master resume yet/);
    expect(empty).toHaveTextContent("No master resume yet. Write below, or use Import your resume above.");
    expect(empty).not.toHaveTextContent(/plugin/i);
    expect(screen.getByRole("button", { name: "Add a role" })).toBeInTheDocument();
    // The tabs, in order: the master resume (with the facts), Find Gaps, then the three pages of writing rules. No Application info.
    const tabs = within(screen.getByRole("navigation", { name: "My Resume" }));
    expect(tabs.getAllByRole("link").map((t) => t.textContent)).toEqual(["Master resume", "Find Gaps", "Story library", "Resume Rules", "Cover Letter Rules", "Document Creation Rules"]);
    expect(tabs.getByRole("link", { name: "Master resume" })).toHaveAttribute("aria-current", "page");
    for (const [name, page] of [["Find Gaps", "find-gaps"], ["Resume Rules", "resume-rules"], ["Cover Letter Rules", "letter-rules"], ["Document Creation Rules", "document-rules"]]) {
      fireEvent.click(tabs.getByRole("link", { name }));
      expect(window.location.hash).toBe(`#/me/${page}`);
    }
    // The line "Your name and contact details are on Application info" is gone: they are on this page.
    expect(screen.queryByText(/Application info/)).toBeNull();
  });
});

describe("Me: the facts forms ask for, the first part of the master resume", () => {
  it("lists the facts as names and values, first in the parts and in the index, with no standing answers and no Application info tab", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(head("Facts forms ask for")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Pat Example");
    expect(screen.getByRole("textbox", { name: /^Where you live/ })).toHaveValue("Oakfield, ST");
    expect(screen.getByRole("textbox", { name: /^For remote or out-of-town roles/ })).toHaveValue("Springdale, ST");
    expect(screen.getByRole("textbox", { name: /^Links/ })).toHaveValue("profiles.example.test/in/example");
    expect(screen.getByRole("textbox", { name: /^Street address/ })).toHaveValue("");
    // A resume opens with a name and contact details, so the facts come before the summary.
    const parts = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(parts[0]).toMatch(/^Facts forms ask for/);
    expect(parts[1]).toMatch(/^Summary/);
    // Standing answers are dropped; the notes no field holds are not shown.
    expect(screen.queryByText(/Standing answers/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Add an answer" })).toBeNull();
    expect(screen.queryByText(/Kept as written|not placed yet|From your old|Something kept from before/)).toBeNull();
    expect(screen.queryByText(/Application info/)).toBeNull();
    expect(screen.queryByRole("link", { name: "Application info" })).toBeNull();
  });

  it("edits the facts in place and saves them to the about document with one bar and one why, leaving the resume alone", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.change(screen.getByRole("textbox", { name: /^Street address/ }), { target: { value: "1 Example Way" } });
    fireEvent.change(screen.getByRole("textbox", { name: /^Links/ }), { target: { value: "profiles.example.test/in/example; example.test/work" } });
    expect(bar()).toHaveTextContent("Unsaved changes to Facts forms ask for");
    expect(head(/^Facts forms ask for/)).toHaveAccessibleName("Facts forms ask for, unsaved changes");
    fireEvent.change(within(bar()!).getByLabelText("Why (optional, one line)"), { target: { value: "added the address" } });
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].url).toMatch(/\/api\/v1\/profile\/about$/);
    expect(puts[0].body).toMatchObject({
      why: "added the address",
      data: { address: "1 Example Way", phone: "555-010-0199", links: ["profiles.example.test/in/example", "example.test/work"] },
    });
    // The old notes are not shown, and are kept; the resume's fields are not in the about document.
    expect(puts[0].body.data?.notes).toEqual(DOCS.about.notes);
    expect(puts[0].body.data).not.toHaveProperty("answers");
    expect(puts[0].body.data).not.toHaveProperty("summary");
  });

  it("saves a change to the facts and one to the resume together, with one Save and the same why, each to its own document", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Summary", "Builds projects and the teams that run them.");
    type("Name", "Pat Q. Example");
    expect(bar()).toHaveTextContent("Unsaved changes to Facts forms ask for and Summary");
    expect(screen.getAllByRole("form", { name: "Unsaved changes" })).toHaveLength(1);
    fireEvent.change(within(bar()!).getByLabelText("Why (optional, one line)"), { target: { value: "new name and opener" } });
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    await waitFor(() => expect(bar()).toBeNull());
    const resume = puts.find((p) => p.url.endsWith("/profile/resume"))!;
    const about = puts.find((p) => p.url.endsWith("/profile/about"))!;
    expect(resume.body.why).toBe("new name and opener");
    expect(about.body.why).toBe("new name and opener");
    expect(resume.body.data).toMatchObject({ summary: "Builds projects and the teams that run them.", philosophy: "People first." });
    expect(resume.body.data).not.toHaveProperty("name");
    expect(resume.body.data?.notes).toEqual(DOCS.resume.notes);
    expect(about.body.data).toMatchObject({ name: "Pat Q. Example", email: "pat@example.test" });
    expect(about.body.data?.notes).toEqual(DOCS.about.notes);
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Pat Q. Example");
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Builds projects and the teams that run them.");
  });

  it("sends only the document that changed, and puts the facts back with Undo along with the resume", async () => {
    const puts = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Summary", "Only the resume.");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].url).toMatch(/\/profile\/resume$/);
    type("Name", "Someone Else");
    type("Summary", "Changed again.");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(bar()).toBeNull();
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Pat Example");
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Only the resume.");
  });

  it("asks before the page is left with unsaved facts", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const ask = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      type("Phone", "555-010-0100");
      expect(confirmLeave()).toBe(false);
      expect(ask).toHaveBeenCalledWith(LEAVE_QUESTION);
    } finally {
      ask.mockRestore();
    }
  });

  it("does not save the resume twice when the facts were refused, and keeps the edits", async () => {
    let docs = structuredClone(DOCS);
    const puts: string[] = [];
    let refuseAbout = true;
    stubFetch((input: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        if (input.endsWith("/profile/about") && refuseAbout) return json({ detail: "That didn't fit." }, 422);
        puts.push(input.replace(/^.*\/profile\//, ""));
        const body = JSON.parse(String(init.body));
        docs = { ...docs, [/\/profile\/(\w+)$/.exec(input)![1]]: body.data };
        return json({ saved: true, version: 2 });
      }
      if (input.endsWith("/api/v1/documents")) return json(docs);
      return json({}, 404);
    });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Summary", "Saved first.");
    type("Name", "Refused Name");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    expect(await within(bar()!).findByText(/didn't fit/)).toBeInTheDocument();
    expect(puts).toEqual(["resume"]);
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Refused Name");
    refuseAbout = false;
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts).toEqual(["resume", "about"]);
  });
});

const DOCUMENT_SECTIONS = ["Rules for your voice", "Rules for ATS screening", "Rules for interviews", "Settings for QA of documentation"];
/** A section of the writing rules, by its name. */
const section = (title: string) => within(screen.getByRole("heading", { level: 2, name: new RegExp(`^${title}`) }).closest("section") as HTMLElement);
const sections = () => screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);

describe("Me: writing rules, in three pages", () => {
  it("Resume Rules holds the resume rules by section and the page margins, and nothing else", async () => {
    serve();
    open("resume-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(screen.getByRole("region", { name: "Resume Rules" })).toBeInTheDocument();
    expect(sections()).toEqual([
      "How a resume works", "Global", "Header", "Summary", "Core Competencies", "Selected Achievements", "Experience", "Education",
      "The resume as built",
    ]);
    expect(section("Global").getAllByRole("textbox").map((t) => (t as HTMLTextAreaElement).value)).toEqual([
      "Name the problem first.", "A resume is one page unless a posting asks for more.", "Send a PDF, never a Word file.",
    ]);
    expect(screen.queryByRole("textbox", { name: "Voice rule 1" })).toBeNull();
    expect(screen.queryByDisplayValue("A letter is three paragraphs at most.")).toBeNull();
    expect(screen.getByText(/rules for the whole resume, for each of its sections and for each target role/)).toBeInTheDocument();
  });

  it("Cover Letter Rules holds the cover letter rules by part, and nothing else", async () => {
    serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(screen.getByRole("region", { name: "Cover Letter Rules" })).toBeInTheDocument();
    expect(sections()).toEqual([
      "How a cover letter works", "Global", "Header", "Greeting", "Opening", "Body", "Closing", "Sign-off",
      "The cover letter as built",
    ]);
    expect(section("Global").getAllByRole("textbox").map((t) => (t as HTMLTextAreaElement).value)).toEqual([
      "A letter is three paragraphs at most.",
    ]);
    expect(screen.queryByRole("textbox", { name: "Voice rule 1" })).toBeNull();
    expect(screen.queryByDisplayValue("Name the problem first.")).toBeNull();
    // The margin is set once, on Resume Rules: this page has no field for it.
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("Document Creation Rules holds voice, ATS, interviews and QA in the owner's order, each a band, with its rules one to a line", async () => {
    serve();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(sections()).toEqual(DOCUMENT_SECTIONS);
    expect(screen.getByRole("textbox", { name: "Voice rule 1" })).toHaveValue("Plain, short and optimistic.");
    expect(screen.getByRole("textbox", { name: "Voice rule 2" })).toHaveValue("Clients are never named. Use sector and scale.");
    expect(section("Rules for ATS screening").getByRole("textbox", { name: "ATS rule 1" })).toHaveValue("Use the posting's own words for skills.");
    expect(screen.queryByRole("textbox", { name: "Format rule 1" })).toBeNull();
    expect(screen.queryByDisplayValue("A letter is three paragraphs at most.")).toBeNull();
    // Each says how many it holds.
    expect(screen.getByText("2 rules")).toBeInTheDocument();
    // The rules for documents are not a part of their own, and no section is the voice paragraph and content rules of before.
    expect(screen.queryByRole("heading", { level: 2, name: /^Documents$|Your voice|Resume format|Never write|How drafts are checked/ })).toBeNull();
    expect(screen.queryByText(/Kept as written|not placed yet|A writing note kept from before/)).toBeNull();
  });

  it("the old Writing rules tab is gone, and each of the three shows the writing document's history", async () => {
    for (const page of ["resume-rules", "letter-rules", "document-rules"] as const) {
      serve();
      const view = open(page);
      expect(await screen.findByRole("button", { name: "History: 1 version" })).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Writing rules" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "History: 1 version" }));
      expect(await screen.findByText("changed writing things")).toBeInTheDocument();
      view.unmount();
    }
  });

  it("each page saves the one writing document whole, with only its own sections changed", async () => {
    const puts = serve();
    const view = open("resume-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Global rule 3", "Send a PDF.");
    expect(bar()).toHaveTextContent("Unsaved changes to Global");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].url).toMatch(/\/api\/v1\/profile\/writing$/);
    expect(puts[0].body.data).toEqual({
      ...DOCS.writing,
      resume_global: DOCS.writing.resume_global.map((r) => (r.id === "pdf" ? { ...r, says: "Send a PDF." } : r)),
    });
    view.unmount();
    // A change on the page for letters sends the formatting as it now is saved.
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Global rule 1", "Two paragraphs.");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect((puts[1].body.data?.letter_global as { says: string }[])[0].says).toBe("Two paragraphs.");
    expect((puts[1].body.data?.resume_global as { says: string }[])[2].says).toBe("Send a PDF.");
  });

  it("holds QA of documentation in its existing forms and a new, empty Humanizing documents", async () => {
    serve();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const qa = section("Settings for QA of documentation");
    await qa.findByText("A count of years of experience");
    expect(qa.getByText(QA_LINE)).toBeInTheDocument();
    expect(qa.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "The review every packet gets 1", "Drift reviewers catch 1", "Flagged for you, not fixed 1", "Never write 1", "Style rules", "Certifications and licences",
      "Checked automatically 2", "To review 1", "Humanizing documents 0",
    ]);
    expect(qa.getByDisplayValue("Read the sources yourself.")).toBeInTheDocument();
    expect(qa.getByDisplayValue("Scope creep by one word")).toBeInTheDocument();
    expect(qa.getByDisplayValue("Hard gates in the posting.")).toBeInTheDocument();
    expect(qa.getByText(BANNED_LINE)).toBeInTheDocument();
    expect(qa.getByDisplayValue("spearheaded")).toBeInTheDocument();
    expect(qa.getByDisplayValue("puffery")).toBeInTheDocument();
    expect(qa.queryByLabelText(/pattern \(regular expression\)/)).toBeNull();
    // The checks that ship with the app are listed, read-only, by name; the older pattern waits under To review.
    expect(qa.getByText(CHECKED_LINE)).toBeInTheDocument();
    expect(qa.getByText("A headcount used as a flex")).toBeInTheDocument();
    expect(qa.queryByDisplayValue("A headcount used as a flex")).toBeNull();
    expect(qa.getByText(REVIEW_LINE)).toBeInTheDocument();
    expect(qa.getByText("\\d+ years")).toBeInTheDocument();
    expect(qa.queryByDisplayValue("\\d+ years")).toBeNull();
    // Humanizing starts with no rules, and Add a rule is its way in.
    expect(qa.queryByRole("textbox", { name: /^Humanizing rule/ })).toBeNull();
    expect(qa.getAllByRole("button", { name: "Add a rule" })).toHaveLength(1);
  });

  it("lists the sections in order, and a long one starts folded with a line of what is in it", async () => {
    const voice_rules = Array.from({ length: 35 }, (_, i) => ({ id: `v${i}`, says: `Voice line ${i}` }));
    serve({ ...DOCS, writing: { ...DOCS.writing, voice_rules } });
    open("document-rules");
    await screen.findByRole("heading", { level: 2, name: /Rules for your voice/ });
    expect(partNames()).toEqual(DOCUMENT_SECTIONS);
    const voice = head("Rules for your voice");
    expect(voice).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("35 rules")).toBeInTheDocument();
    expect(screen.getByText(/Voice line 0 · Voice line 1 · Voice line 2/)).toBeInTheDocument();
    fireEvent.click(voice);
    expect(voice).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Voice rule 35" })).toHaveValue("Voice line 34");
  });

  it("on the pages of sections, a long Global starts folded and the short sections open", async () => {
    const long = (prefix: string) => Array.from({ length: 35 }, (_, i) => ({ id: `${prefix}${i}`, says: `${prefix} line ${i}` }));
    const writing = { ...DOCS.writing, resume_global: long("f"), letter_global: long("l"), voice_rules: long("v") };
    serve({ ...DOCS, writing });
    const view = open("resume-rules");
    expect(await screen.findByRole("heading", { level: 2, name: /^Global/ })).toBeInTheDocument();
    expect(head("Global")).toHaveAttribute("aria-expanded", "false");
    expect(head("Header")).toHaveAttribute("aria-expanded", "true");
    view.unmount();
    open("letter-rules");
    expect(await screen.findByRole("heading", { level: 2, name: /^Global/ })).toBeInTheDocument();
    expect(head("Global")).toHaveAttribute("aria-expanded", "false");
    expect(head("Sign-off")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/l line 0 · l line 1 · l line 2/)).toBeInTheDocument();
  });

  it.each([
    ["document-rules", "Rules for your voice", "voice_rules", "Voice rule"],
    ["document-rules", "Rules for ATS screening", "ats_rules", "ATS rule"],
    ["resume-rules", "Global", "resume_global", "Global rule"],
    ["letter-rules", "Global", "letter_global", "Global rule"],
  ] as const)("on %s, adds a rule to %s, then takes it out again", async (page, title, field, label) => {
    const puts = serve();
    open(page);
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const before = (DOCS.writing as unknown as Record<string, { id: string; says: string }[]>)[field];
    fireEvent.click(section(title).getByRole("button", { name: "Add a rule" }));
    const added = screen.getByRole("textbox", { name: `${label} ${before.length + 1}` });
    expect(added).toHaveFocus();
    // A blank line is not a change: there is nothing to save.
    expect(bar()).toBeNull();
    fireEvent.change(added, { target: { value: "Always one thing at a time." } });
    expect(bar()).toHaveTextContent(`Unsaved changes to ${title}`);
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].url).toMatch(/\/api\/v1\/profile\/writing$/);
    const saved = puts[0].body.data?.[field] as { id: string; says: string }[];
    expect(saved).toEqual([...before, { id: "always-one-thing-at-a", says: "Always one thing at a time." }]);
    // The other sections went with it, as they were.
    expect(puts[0].body.data?.banned).toEqual(DOCS.writing.banned);
    expect(puts[0].body.data?.notes).toEqual(DOCS.writing.notes);
    fireEvent.click(section(title).getByRole("button", { name: "Remove Always one thing at a time." }));
    expect(bar()).toHaveTextContent(`Unsaved changes to ${title}`);
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1].body.data?.[field]).toEqual(before);
  });

  it("adds a rule to humanizing documents, and takes it out again", async () => {
    const puts = serve();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.click(section("Settings for QA of documentation").getByRole("button", { name: "Add a rule" }));
    const added = screen.getByRole("textbox", { name: "Humanizing rule 1" });
    expect(added).toHaveFocus();
    fireEvent.change(added, { target: { value: "No lists of three adjectives." } });
    expect(bar()).toHaveTextContent("Unsaved changes to Settings for QA of documentation");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].body.data?.humanizing).toEqual([{ id: "no-lists-of-three-adjectives", says: "No lists of three adjectives." }]);
    fireEvent.click(screen.getByRole("button", { name: "Remove No lists of three adjectives." }));
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1].body.data?.humanizing).toEqual([]);
  });

  it("adds and removes a review step, a flag, a phrase and a drift pattern in their own forms", async () => {
    const puts = serve();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const qa = section("Settings for QA of documentation");
    fireEvent.click(qa.getByRole("button", { name: "Add a step" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Review step 2" }), { target: { value: "Check every number." } });
    fireEvent.click(qa.getByRole("button", { name: "Add a flag" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Flag 2" }), { target: { value: "A required licence." } });
    fireEvent.click(qa.getByRole("button", { name: "Add a phrase" }));
    fireEvent.change(screen.getAllByRole("textbox", { name: "Phrase" })[1], { target: { value: "synergy" } });
    fireEvent.click(qa.getByRole("button", { name: "Add a pattern" }));
    fireEvent.change(screen.getAllByRole("textbox", { name: "Pattern" })[1], { target: { value: "Passive voice" } });
    fireEvent.click(qa.getByRole("button", { name: "Remove Hard gates in the posting." }));
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    const data = puts[0].body.data as Record<string, { says?: string; phrase?: string; name?: string }[]>;
    expect(data.review_steps.map((r) => r.says)).toEqual(["Read the sources yourself.", "Check every number."]);
    expect(data.review_flags.map((r) => r.says)).toEqual(["A required licence."]);
    expect(data.banned.map((b) => b.phrase)).toEqual(["spearheaded", "synergy"]);
    expect(data.legacy_patterns).toEqual(DOCS.writing.legacy_patterns);
    expect(data.drift_patterns.map((d) => d.name)).toEqual(["Scope creep by one word", "Passive voice"]);
  });

  it("saves the writing in one save with why, whole, and sends no voice paragraph or rules for documents", async () => {
    const puts = serve();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Voice rule 2", "Clients are never named.");
    type("ATS rule 1", "Use the posting's words.");
    expect(bar()).toHaveTextContent("Unsaved changes to Rules for your voice and Rules for ATS screening");
    fireEvent.change(within(bar()!).getByLabelText("Why (optional, one line)"), { target: { value: "tightened" } });
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].url).toMatch(/\/api\/v1\/profile\/writing$/);
    expect(puts[0].body.why).toBe("tightened");
    const data = puts[0].body.data as Record<string, unknown>;
    expect(Object.keys(data).sort()).toEqual(Object.keys(DOCS.writing).sort());
    expect(data).not.toHaveProperty("voice");
    expect(data).not.toHaveProperty("rules");
    expect(data).not.toHaveProperty("document_rules");
    expect((data.voice_rules as { says: string }[])[1].says).toBe("Clients are never named.");
    expect((data.ats_rules as { says: string }[])[0].says).toBe("Use the posting's words.");
    expect(data.notes).toEqual(DOCS.writing.notes);
  });

  it("edits a phrase never to write, as plain words, and the drift patterns it is checked against", async () => {
    const puts = serve();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Phrase", "spearhead");
    type("Fix", "Use the noun the master uses.");
    expect(bar()).toHaveTextContent("Unsaved changes to Settings for QA of documentation");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data?.banned).toEqual([{ id: "b", phrase: "spearhead", why: "puffery" }]);
    expect((puts[0].body.data?.drift_patterns as { fix: string }[])[0].fix).toBe("Use the noun the master uses.");
  });

  it("puts every edit back with Undo, a removed rule too", async () => {
    serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.click(section("Global").getByRole("button", { name: "Remove A letter is three paragraphs at most." }));
    expect(screen.queryByRole("textbox", { name: "Global rule 1" })).toBeNull();
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(bar()).toBeNull();
    expect(screen.getByRole("textbox", { name: "Global rule 1" })).toHaveValue("A letter is three paragraphs at most.");
  });

  describe("page margins", () => {
    const field = () => screen.getByRole("spinbutton", { name: "Page margins (inches)" });

    it("is one small field at the top of Global, above its rules, empty while none is set", async () => {
      serve();
      open("resume-rules");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      const format = section("Global");
      expect(format.getByRole("spinbutton", { name: "Page margins (inches)" })).toHaveValue(null);
      expect(format.getByText(/Leave empty for the default margins/)).toBeInTheDocument();
      expect(field().compareDocumentPosition(format.getByRole("textbox", { name: "Global rule 1" }))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
      // Nothing else of the writing rules has one, and a margin is no rule: the section still counts its three.
      expect(screen.getAllByRole("spinbutton")).toHaveLength(1);
      expect(screen.getByText("3 rules")).toBeInTheDocument();
      expect(bar()).toBeNull();
    });

    it("shows the margin that is saved", async () => {
      serve({ ...DOCS, writing: { ...DOCS.writing, page_margin_inches: 0.5 } });
      open("resume-rules");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      expect(field()).toHaveValue(0.5);
    });

    it("saves a margin with the page's one save, as a number, and the rest of the writing as it was", async () => {
      const puts = serve();
      open("resume-rules");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      fireEvent.change(field(), { target: { value: "0.5" } });
      expect(bar()).toHaveTextContent("Unsaved changes to Global");
      fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(puts).toHaveLength(1));
      await waitFor(() => expect(bar()).toBeNull());
      expect(puts[0].url).toMatch(/\/api\/v1\/profile\/writing$/);
      expect(puts[0].body.data?.page_margin_inches).toBe(0.5);
      expect(puts[0].body.data?.resume_global).toEqual(DOCS.writing.resume_global);
      expect(puts[0].body.data?.banned).toEqual(DOCS.writing.banned);
      expect(field()).toHaveValue(0.5);
    });

    it("saves an emptied box as no margin, so the export keeps its own", async () => {
      const puts = serve({ ...DOCS, writing: { ...DOCS.writing, page_margin_inches: 0.5 } });
      open("resume-rules");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      fireEvent.change(field(), { target: { value: "" } });
      expect(bar()).toHaveTextContent("Unsaved changes to Global");
      fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(puts).toHaveLength(1));
      expect(puts[0].body.data?.page_margin_inches).toBeNull();
    });

    it("puts a margin back with Undo", async () => {
      serve({ ...DOCS, writing: { ...DOCS.writing, page_margin_inches: 0.5 } });
      open("resume-rules");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      fireEvent.change(field(), { target: { value: "1.25" } });
      expect(field()).toHaveValue(1.25);
      fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
      expect(bar()).toBeNull();
      expect(field()).toHaveValue(0.5);
    });

    it("says why in the save bar when the margin is out of range, and keeps what was typed", async () => {
      const refusal = "the writing don't fit: page_margin_inches: page margins must be between 0.25 and 1.5 inches";
      stubFetch((input: string, init?: RequestInit) => {
        if (init?.method === "PUT") return json({ detail: refusal }, 422);
        if (input.endsWith("/api/v1/documents")) return json(DOCS);
        return json({}, 404);
      });
      open("resume-rules");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      fireEvent.change(field(), { target: { value: "3" } });
      fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
      expect(await within(bar()!).findByText(/page margins must be between 0.25 and 1.5 inches/)).toBeInTheDocument();
      expect(field()).toHaveValue(3);
    });
  });

  it("shows the writing's history under the one History button, and no second history for rules kept with the settings", async () => {
    serve();
    open("document-rules");
    fireEvent.click(await screen.findByRole("button", { name: "History: 1 version" }));
    expect(await screen.findByText("changed writing things")).toBeInTheDocument();
    expect(screen.queryByText("Rules for documents")).toBeNull();
    expect(screen.queryByText("changed settings things")).toBeNull();
  });

  it("holds the save bar's error in the bar and keeps the edits when a save is refused", async () => {
    let refuse = true;
    stubFetch((input: string, init?: RequestInit) => {
      if (init?.method === "PUT") return refuse ? json({ detail: "That didn't fit." }, 422) : json({ saved: true, version: 2 });
      if (input.endsWith("/api/v1/documents")) return json(DOCS);
      return json({}, 404);
    });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.change(screen.getByRole("textbox", { name: /^Street address/ }), { target: { value: "1 Example Way" } });
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    expect(await within(bar()!).findByText(/didn't fit/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /^Street address/ })).toHaveValue("1 Example Way");
    refuse = false;
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(bar()).toBeNull());
  });
});

describe("Me: Resume Rules, by section and by role", () => {
  it("opens with the fixed crib: what a resume is for, who reads it, and seven things that help", async () => {
    serve();
    open("resume-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const crib = section("How a resume works");
    expect(await crib.findByText(/get you an interview/)).toBeInTheDocument();
    expect(crib.getByText(/Screening software reads it first/)).toBeInTheDocument();
    expect(crib.getAllByRole("listitem")).toHaveLength(7);
    expect(crib.getByText("One to two pages.")).toBeInTheDocument();
    // Fixed words: no field in it to change.
    expect(crib.queryByRole("textbox")).toBeNull();
  });

  it("has the parts in the owner's order, each with its best-practice note under its heading", async () => {
    serve();
    open("resume-rules");
    await screen.findByRole("heading", { level: 2, name: /^Global/ });
    expect(partNames()).toEqual([
      "Global", "Header", "Summary", "Core Competencies", "Selected Achievements", "Experience", "Education",
    ]);
    expect(section("Summary").getByText("Three or four lines written for this posting, with no \"I\".")).toBeInTheDocument();
    expect(section("Global").getByText(/One to two pages, one common font/)).toBeInTheDocument();
    expect(section("Header").getByText(/not the page header/)).toBeInTheDocument();
  });

  it.each([
    ["Header", "resume_header"],
    ["Summary", "resume_summary"],
    ["Core Competencies", "resume_competencies"],
    ["Selected Achievements", "resume_achievements"],
    ["Experience", "resume_experience"],
    ["Education", "resume_education"],
  ] as const)("adds a rule to %s and saves it in its own list, then takes it out again", async (title, field) => {
    const puts = serve();
    open("resume-rules");
    await screen.findByRole("heading", { level: 2, name: title });
    fireEvent.click(section(title).getByRole("button", { name: "Add a rule" }));
    const added = screen.getByRole("textbox", { name: `${title} rule 1` });
    expect(added).toHaveFocus();
    fireEvent.change(added, { target: { value: "Keep it short." } });
    expect(bar()).toHaveTextContent(`Unsaved changes to ${title}`);
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].body.data?.[field]).toEqual([{ id: "keep-it-short", says: "Keep it short." }]);
    expect(puts[0].body.data?.resume_global).toEqual(DOCS.writing.resume_global);
    fireEvent.click(section(title).getByRole("button", { name: "Remove Keep it short." }));
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1].body.data?.[field]).toEqual([]);
  });

  it("has a fold under Global for each target role, named for it, holding rules that are saved for that role only", async () => {
    const puts = serve();
    open("resume-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const global = section("Global");
    await global.findByRole("heading", { level: 3, name: "For Operations Lead roles" });
    expect(global.getByRole("heading", { level: 3, name: "For Developer roles" })).toBeInTheDocument();
    const fold = head("For Operations Lead roles", 3);
    expect(fold).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "true");
    const mine = within(fold.closest(".fi-me-part-sub") as HTMLElement);
    fireEvent.click(mine.getByRole("button", { name: "Add a rule" }));
    fireEvent.change(screen.getByRole("textbox", { name: "For Operations Lead roles rule 1" }), { target: { value: "Lead with team size." } });
    expect(bar()).toHaveTextContent("Unsaved changes to Global");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    // Saved under the role's id, and for no other role; the rules above are as they were.
    expect(puts[0].body.data?.role_rules).toEqual({ ops: [{ id: "lead-with-team-size", says: "Lead with team size." }] });
    expect(puts[0].body.data?.resume_global).toEqual(DOCS.writing.resume_global);
  });

  it("keeps the rules of a role deleted in Search Settings, says so, and saves them untouched", async () => {
    const writing = { ...DOCS.writing, role_rules: { ops: [{ id: "teams", says: "Lead with team size." }], gone: [{ id: "old", says: "From a role since deleted." }] } };
    const puts = serve({ ...DOCS, writing });
    open("resume-rules");
    await screen.findByRole("heading", { level: 3, name: "For Operations Lead roles" });
    // A role with rules starts open; the deleted one is shown by its id as words, with a plain note.
    expect(screen.getByDisplayValue("Lead with team size.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "For gone roles" })).toBeInTheDocument();
    expect(screen.getByText(/no longer in Search Settings/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("From a role since deleted.")).toBeInTheDocument();
    type("Global rule 1", "Changed.");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data?.role_rules).toEqual(writing.role_rules);
  });

  it("shows the resume as built beside the rules: the sections in print order with their rule counts and the margin", async () => {
    serve({ ...DOCS, writing: { ...DOCS.writing, page_margin_inches: 0.75, resume_summary: [{ id: "s", says: "Short." }] } });
    open("resume-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const built = section("The resume as built");
    expect(await built.findByText(/3 Global rules\. Page margins: 0\.75 inches/)).toBeInTheDocument();
    const rows = built.getAllByRole("listitem");
    expect(rows.map((r) => within(r).getByText(/rules?$/).textContent)).toEqual([
      "0 rules", "1 rule", "0 rules", "0 rules", "0 rules", "0 rules",
    ]);
    expect(rows.map((r) => r.querySelector(".fi-me-skeleton-name")?.textContent)).toEqual([
      "Name, contact line and headline", "Professional Summary", "Core Competencies", "Selected Achievements",
      "Professional Experience", "Education",
    ]);
    // It follows the rules as they are being edited.
    fireEvent.click(section("Education").getByRole("button", { name: "Add a rule" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Education rule 1" }), { target: { value: "Degrees only." } });
    expect(built.getAllByRole("listitem")[5]).toHaveTextContent("1 rule");
  });

  it("ends the side column with one link to How they work and shows no instruction text", async () => {
    serve();
    open("resume-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(sections()).not.toContain("What the writer is told");
    expect(screen.queryByRole("heading", { name: "Its instructions" })).toBeNull();
    expect(screen.getByRole("link", { name: "How they work" })).toHaveAttribute("href", "#/agents/how/jobsearch_packet");
    expect(screen.queryByRole("link", { name: "Edit on How they work" })).toBeNull();
    expect(requested.some((u) => /\/documents\/(resume|letter)-instructions/.test(u))).toBe(false);
  });

});

describe("Me: Cover Letter Rules, by part and by role", () => {
  it("opens with the fixed crib: what a letter is for, when one is worth writing, and seven things that help", async () => {
    serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const crib = section("How a cover letter works");
    expect(await crib.findByText(/one job: to give a person a reason to read your resume closely/)).toBeInTheDocument();
    expect(crib.getByText(/Many screeners skip it/)).toBeInTheDocument();
    expect(crib.getAllByRole("listitem")).toHaveLength(7);
    expect(crib.getByText("Lead with the employer's problem, not your history.")).toBeInTheDocument();
    expect(crib.queryByRole("textbox")).toBeNull();
  });

  it("has the parts in the owner's order, each with its one-line note under its heading", async () => {
    serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 2, name: /^Global/ });
    expect(partNames()).toEqual([
      "Global", "Header", "Greeting", "Opening", "Body", "Closing", "Sign-off",
    ]);
    expect(section("Global").getByText(/when a letter is written at all/)).toBeInTheDocument();
    expect(section("Opening").getByText(/the problem the employer is hiring to solve/)).toBeInTheDocument();
    expect(section("Body").getByText(/One or two things you have done/)).toBeInTheDocument();
  });

  it.each([
    ["Header", "letter_header"],
    ["Greeting", "letter_greeting"],
    ["Opening", "letter_opening"],
    ["Body", "letter_body"],
    ["Closing", "letter_closing"],
    ["Sign-off", "letter_signoff"],
  ] as const)("adds a rule to %s and saves it in its own list, then takes it out again", async (title, field) => {
    const puts = serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 2, name: title });
    fireEvent.click(section(title).getByRole("button", { name: "Add a rule" }));
    const added = screen.getByRole("textbox", { name: `${title} rule 1` });
    expect(added).toHaveFocus();
    fireEvent.change(added, { target: { value: "Keep it short." } });
    expect(bar()).toHaveTextContent(`Unsaved changes to ${title}`);
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].body.data?.[field]).toEqual([{ id: "keep-it-short", says: "Keep it short." }]);
    expect(puts[0].body.data?.letter_global).toEqual(DOCS.writing.letter_global);
    expect(puts[0].body.data?.resume_header).toEqual([]);
    fireEvent.click(section(title).getByRole("button", { name: "Remove Keep it short." }));
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1].body.data?.[field]).toEqual([]);
  });

  it("has a fold under Global for each target role, saved in the letter's own map and not the resume's", async () => {
    const puts = serve({ ...DOCS, writing: { ...DOCS.writing, role_rules: { ops: [{ id: "r", says: "A resume rule for ops." }] } } });
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const global = section("Global");
    await global.findByRole("heading", { level: 3, name: "For Operations Lead roles" });
    expect(global.getByRole("heading", { level: 3, name: "For Developer roles" })).toBeInTheDocument();
    // The resume's rules for this role are not the letter's: this fold starts folded and empty.
    const fold = head("For Operations Lead roles", 3);
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByDisplayValue("A resume rule for ops.")).toBeNull();
    fireEvent.click(fold);
    const mine = within(fold.closest(".fi-me-part-sub") as HTMLElement);
    fireEvent.click(mine.getByRole("button", { name: "Add a rule" }));
    fireEvent.change(screen.getByRole("textbox", { name: "For Operations Lead roles rule 1" }), { target: { value: "Name what was run." } });
    expect(bar()).toHaveTextContent("Unsaved changes to Global");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(puts[0].body.data?.letter_role_rules).toEqual({ ops: [{ id: "name-what-was-run", says: "Name what was run." }] });
    expect(puts[0].body.data?.role_rules).toEqual({ ops: [{ id: "r", says: "A resume rule for ops." }] });
    expect(puts[0].body.data?.letter_global).toEqual(DOCS.writing.letter_global);
  });

  it("keeps the letter rules of a role deleted in Search Settings, says so, and saves them untouched", async () => {
    const letter_role_rules = { ops: [{ id: "teams", says: "Name the team." }], gone: [{ id: "old", says: "From a role since deleted." }] };
    const puts = serve({ ...DOCS, writing: { ...DOCS.writing, letter_role_rules } });
    open("letter-rules");
    await screen.findByRole("heading", { level: 3, name: "For Operations Lead roles" });
    expect(screen.getByDisplayValue("Name the team.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "For gone roles" })).toBeInTheDocument();
    expect(screen.getByText(/go to no letter/)).toBeInTheDocument();
    type("Global rule 1", "Changed.");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data?.letter_role_rules).toEqual(letter_role_rules);
  });

  it("shows the letter as built beside the rules: the parts in order with their rule counts and the Global margin", async () => {
    serve({ ...DOCS, writing: { ...DOCS.writing, page_margin_inches: 0.75, letter_opening: [{ id: "o", says: "Problem first." }] } });
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const built = section("The cover letter as built");
    expect(await built.findByText(/Whole letter: 1 Global rule\. Page margins: 0\.75 inches/)).toBeInTheDocument();
    const rows = built.getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector(".fi-me-skeleton-name")?.textContent)).toEqual([
      "Header", "Greeting", "Opening", "Body", "Closing", "Sign-off",
    ]);
    expect(rows.map((r) => within(r).getByText(/rules?$/).textContent)).toEqual([
      "0 rules", "0 rules", "1 rule", "0 rules", "0 rules", "0 rules",
    ]);
    fireEvent.click(section("Closing").getByRole("button", { name: "Add a rule" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Closing rule 1" }), { target: { value: "End on the ask." } });
    expect(built.getAllByRole("listitem")[4]).toHaveTextContent("1 rule");
  });

  it("says the default margin when none is set", async () => {
    serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(await section("The cover letter as built").findByText(/Page margins: the default/)).toBeInTheDocument();
  });

  it("ends the side column with one link to How they work and shows no instruction text", async () => {
    serve();
    open("letter-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    expect(sections()).not.toContain("What the writer is told");
    expect(screen.queryByRole("heading", { name: "Its instructions" })).toBeNull();
    expect(screen.getByRole("link", { name: "How they work" })).toHaveAttribute("href", "#/agents/how/jobsearch_packet");
    expect(screen.queryByRole("link", { name: "Edit on How they work" })).toBeNull();
    expect(requested.some((u) => /\/documents\/(resume|letter)-instructions/.test(u))).toBe(false);
  });

});

describe("Me: a save made from an out-of-date page", () => {
  const STALE = "This page is out of date: it was changed elsewhere since you opened it. Reload to see the latest, then make your change again.";

  /** A server that numbers its versions as the real one does: a save that names an older version than the one in force is
   * refused (409) and stores nothing. `savedElsewhere` is another path saving a document since the page loaded it. */
  function serveVersioned() {
    let docs: Documents = { ...structuredClone(DOCS), versions: { resume: 11, about: 4, writing: 11 } };
    const puts: Put[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        puts.push({ url: input, body });
        const kind = /\/profile\/(\w+)$/.exec(input)![1] as keyof Documents["versions"];
        const now = docs.versions[kind];
        if (body.base_version !== undefined && body.base_version < now) return json({ detail: STALE }, 409);
        docs = { ...docs, [kind]: body.data, versions: { ...docs.versions, [kind]: now + 1 } };
        return json({ saved: true, version: now + 1 });
      }
      if (input.endsWith("/api/v1/documents")) return json(docs);
      if (input.endsWith("/api/v1/profile")) return json({ criteria: { target_roles: TARGET_ROLES } });
      return json({}, 404);
    });
    return {
      puts,
      savedElsewhere: (kind: keyof Documents["versions"]) => {
        docs = { ...docs, versions: { ...docs.versions, [kind]: docs.versions[kind] + 5 } };
      },
      stored: () => docs,
    };
  }

  it("refuses the writing rules, says so in the bar with Reload, keeps what was typed, and stores nothing", async () => {
    const server = serveVersioned();
    open("document-rules");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Voice rule 1", "Typed on an old copy.");
    server.savedElsewhere("writing");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    expect(await within(bar()!).findByRole("alert")).toHaveTextContent(STALE);
    expect(within(bar()!).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Voice rule 1" })).toHaveValue("Typed on an old copy.");
    expect(server.puts).toHaveLength(1);
    expect(server.puts[0].body.base_version).toBe(11); // the version the page loaded
    expect(server.stored().writing.voice_rules[0].says).not.toBe("Typed on an old copy.");
  });

  it("names the version the page loaded on every rules page, and the next save names the one the last save made", async () => {
    for (const [page, label] of [["resume-rules", "Global rule 1"], ["letter-rules", "Global rule 1"], ["document-rules", "Voice rule 1"]] as const) {
      const server = serveVersioned();
      const { unmount } = open(page);
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      type(label, "First change.");
      fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(bar()).toBeNull());
      type(label, "Second change.");
      fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(server.puts).toHaveLength(2));
      await waitFor(() => expect(bar()).toBeNull());
      expect(server.puts.map((p) => p.body.base_version)).toEqual([11, 12]);
      unmount();
    }
  });

  it("refuses the master resume when the resume was saved elsewhere, and sends nothing for the facts after it", async () => {
    const server = serveVersioned();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Summary", "Typed on an old copy.");
    fireEvent.change(screen.getByRole("textbox", { name: /^Street address/ }), { target: { value: "1 Example Way" } });
    server.savedElsewhere("resume");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    expect(await within(bar()!).findByRole("alert")).toHaveTextContent(STALE);
    expect(within(bar()!).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Typed on an old copy.");
    expect(screen.getByRole("textbox", { name: /^Street address/ })).toHaveValue("1 Example Way");
    expect(server.puts.map((p) => [p.url.split("/").pop(), p.body.base_version])).toEqual([["resume", 11]]);
  });

  it("refuses the facts when the about document was saved elsewhere, though the resume went through", async () => {
    const server = serveVersioned();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    type("Summary", "Changed.");
    fireEvent.change(screen.getByRole("textbox", { name: /^Street address/ }), { target: { value: "1 Example Way" } });
    server.savedElsewhere("about");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    expect(await within(bar()!).findByRole("alert")).toHaveTextContent(STALE);
    expect(screen.getByRole("textbox", { name: /^Street address/ })).toHaveValue("1 Example Way");
    // Each document names its own version: the resume 11 and the facts 4.
    expect(server.puts.map((p) => [p.url.split("/").pop(), p.body.base_version])).toEqual([["resume", 11], ["about", 4]]);
  });
});
