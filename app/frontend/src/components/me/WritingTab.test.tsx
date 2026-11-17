import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { StyleRule, Writing } from "../../api/types/me";
import { json, render, stubFetch } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import WritingTab from "./WritingTab";
import { CHECKED_LINE } from "./meParts";

// Made up: no real person's rules or credentials.
const NONE: { id: string; says: string }[] = [];
const WRITING: Writing = {
  voice_rules: [{ id: "voice", says: "Plain and short." }],
  ats_rules: [{ id: "one-column", says: "Use one column." }], interview_rules: [],
  resume_global: NONE, resume_header: NONE, resume_summary: NONE, resume_competencies: NONE, resume_achievements: NONE,
  resume_experience: NONE, resume_education: NONE, role_rules: {}, page_margin_inches: null,
  letter_global: NONE, letter_header: NONE, letter_greeting: NONE, letter_opening: NONE, letter_body: NONE, letter_closing: NONE,
  letter_signoff: NONE, letter_role_rules: {},
  banned: [{ id: "puff", phrase: "spearheaded", why: "puffery" }],
  legacy_patterns: [],
  review_steps: [{ id: "read", says: "Read the sources yourself." }],
  drift_patterns: [],
  review_flags: [{ id: "gates", says: "Hard gates in the posting." }],
  humanizing: NONE,
  style_off: [],
  credentials: [{ id: "cea", name: "Certified Example Analyst" }],
  notes: [],
};

/** The five style rules the app ships, as the server describes them (the page draws these; the writing document says which are off). */
const STYLE: StyleRule[] = [
  { id: "no-em-dash", name: "No em dash", rule: "Use a comma, a period or a new sentence. A dash between two dates is fine." },
  { id: "no-inline-markup", name: "No markup", rule: "Bold, italics and other markup print as typed in the Word file. Write plain text; the app sets its own labels." },
  { id: "no-year-by-degree", name: "No year beside a degree", rule: "A year next to a degree tells a reader your age. Name the degree and the school." },
  { id: "no-pre-conceding", name: "No phrase that concedes a gap first", rule: "Say nothing about a gap, or say one confident sentence. Never open with what you lack." },
  { id: "no-weak-words", name: "No weak or inflated words", rule: "Words like 'responsible for', 'various' and 'leveraged' say little and sound padded." },
];
const CHECKS = [
  { id: "years-of-experience", name: "A count of years of experience", rule: "Show what was done, not how long.", backs: "Nothing you ban appears" },
  { id: "headcount-flex", name: "A headcount used as a flex", rule: "Describe scope by what was led.", backs: "Nothing you ban appears" },
];
const STYLE_HINT = "These start on. Switch one off and the packet check stops looking for it.";
const ADD = "Add a certification or licence";

interface Put {
  url: string;
  body: { data: Writing; why: string; base_version: number };
}

/** The checks the app runs and the style rules it ships, and a PUT that is kept so a test can read what was saved. */
function serve(answer: unknown = { checks: CHECKS, style: STYLE }) {
  const puts: Put[] = [];
  stubFetch((input: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      puts.push({ url: input, body: JSON.parse(String(init.body)) });
      return json({ saved: true, version: 4 });
    }
    if (input.endsWith("/api/v1/documents/built-in-checks")) return json(answer);
    return json({}, 404);
  });
  return puts;
}

function open(saved: Writing = WRITING) {
  return render(
    <ToastProvider>
      <WritingTab saved={saved} version={3} onSaved={() => {}} page="document-rules" />
    </ToastProvider>,
  );
}

const bar = () => screen.queryByRole("form", { name: "Unsaved changes" });
const qaHeading = () => screen.getByRole("heading", { level: 2, name: /^Settings for QA of documentation/ });
const qa = () => within(qaHeading().closest("section") as HTMLElement);
/** The rows of a list under a list name (a third-level heading): what follows its one plain line. */
const listAfter = (name: string) => {
  const heading = screen.getByRole("heading", { level: 3, name });
  const list = heading.nextElementSibling?.matches("p") ? heading.nextElementSibling.nextElementSibling : heading.nextElementSibling;
  return within(list as HTMLElement);
};
const styleSwitch = (name: string) => screen.getByRole("checkbox", { name });
const save = () => fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));

describe("WritingTab: To review", () => {
  const OLD = { id: "yrs", pattern: "\\d+ years", regex: true, why: "dates the owner" };

  it("drops an entry with Remove, marks the part changed, saves the list without it, and Undo brings it back", async () => {
    const puts = serve();
    open({ ...WRITING, legacy_patterns: [OLD] });
    await qa().findByText("A count of years of experience");
    fireEvent.click(screen.getByRole("button", { name: "Remove \\d+ years" }));
    expect(screen.queryByText("dates the owner")).toBeNull();
    expect(bar()).toHaveTextContent("Unsaved changes to Settings for QA of documentation");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(screen.getByText("dates the owner")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove \\d+ years" }));
    save();
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data).toEqual({ ...WRITING, legacy_patterns: [] });
  });
});

describe("WritingTab: Document Creation Rules, Settings for QA of documentation", () => {
  it("lists the five style rules after Never write and before Checked automatically, each one a switch that is on", async () => {
    serve();
    open();
    await qa().findByText("A count of years of experience");
    expect(qa().getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "The review every packet gets 1", "Drift reviewers catch 0", "Flagged for you, not fixed 1", "Never write 1",
      "Style rules", "Certifications and licences", "Checked automatically 2", "Humanizing documents 0",
    ]);
    expect(qa().getByText(STYLE_HINT)).toBeInTheDocument();
    // The page has no other switch: the five rows are every checkbox on it, in the app's order, each named by the rule's name alone.
    expect(screen.getAllByRole("checkbox").map((c) => c.closest("label")?.textContent)).toEqual(STYLE.map((s) => s.name));
    for (const rule of STYLE) {
      expect(styleSwitch(rule.name)).toBeChecked();
      // Its sentence is in its own row, after the name.
      expect(within(styleSwitch(rule.name).closest("li") as HTMLElement).getByText(rule.rule)).toBeInTheDocument();
    }
    // Nothing to save until a switch is moved.
    expect(bar()).toBeNull();
  });

  it("shows a rule that is switched off in the saved writing as off, and the others on", async () => {
    serve();
    open({ ...WRITING, style_off: ["no-weak-words", "no-em-dash"] });
    await qa().findByText("No weak or inflated words");
    expect(styleSwitch("No weak or inflated words")).not.toBeChecked();
    expect(styleSwitch("No em dash")).not.toBeChecked();
    expect(styleSwitch("No markup")).toBeChecked();
    expect(styleSwitch("No year beside a degree")).toBeChecked();
    expect(styleSwitch("No phrase that concedes a gap first")).toBeChecked();
  });

  it("marks the part changed when a switch is moved, and saves style_off, whole, with the rest of the writing as it was", async () => {
    const puts = serve();
    open();
    await qa().findByText("No em dash");
    fireEvent.click(styleSwitch("No em dash"));
    expect(styleSwitch("No em dash")).not.toBeChecked();
    expect(bar()).toHaveTextContent("Unsaved changes to Settings for QA of documentation");
    expect(qaHeading()).toHaveAccessibleName(/unsaved changes/);
    save();
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].url).toMatch(/\/api\/v1\/profile\/writing$/);
    expect(puts[0].body.base_version).toBe(3);
    expect(puts[0].body.data).toEqual({ ...WRITING, style_off: ["no-em-dash"] });
  });

  it("puts a switch back on to nothing to save, and saves the ids off in the order the app lists its rules", async () => {
    const puts = serve();
    open({ ...WRITING, style_off: ["no-weak-words"] });
    await qa().findByText("No em dash");
    fireEvent.click(styleSwitch("No em dash"));
    expect(bar()).not.toBeNull();
    fireEvent.click(styleSwitch("No em dash"));
    expect(bar()).toBeNull();
    // Two more off, switched in the opposite order to the app's list: saved in the app's order.
    fireEvent.click(styleSwitch("No year beside a degree"));
    fireEvent.click(styleSwitch("No em dash"));
    save();
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data.style_off).toEqual(["no-em-dash", "no-year-by-degree", "no-weak-words"]);
  });

  it("puts a switch back with Undo", async () => {
    serve();
    open();
    await qa().findByText("No markup");
    fireEvent.click(styleSwitch("No markup"));
    expect(styleSwitch("No markup")).not.toBeChecked();
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(bar()).toBeNull();
    expect(styleSwitch("No markup")).toBeChecked();
  });

  it("keeps an id it does not know in style_off and ignores it on the page", async () => {
    const puts = serve();
    open({ ...WRITING, style_off: ["a-rule-from-a-newer-app", "no-inline-markup"] });
    await qa().findByText("No markup");
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
    expect(styleSwitch("No markup")).not.toBeChecked();
    fireEvent.click(styleSwitch("No em dash"));
    save();
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data.style_off).toEqual(["no-em-dash", "no-inline-markup", "a-rule-from-a-newer-app"]);
  });

  it("has no Always checked list: the integrity checks are not a setting", async () => {
    serve();
    open();
    await qa().findByText("A count of years of experience");
    expect(screen.queryByRole("heading", { level: 3, name: /Always checked/i })).toBeNull();
    expect(screen.queryByText(/Always checked/i)).toBeNull();
    // Checked automatically stays as it was, read-only, with its line.
    expect(qa().getByText(CHECKED_LINE)).toBeInTheDocument();
    expect(qa().queryByDisplayValue("A headcount used as a flex")).toBeNull();
    expect(qa().getByText("A headcount used as a flex")).toBeInTheDocument();
  });

  it("shows no count on the two new lists, and the part's own count does not move when a certification is added", async () => {
    serve();
    open();
    await qa().findByText("No em dash");
    for (const name of ["Style rules", "Certifications and licences"]) {
      const head = screen.getByRole("heading", { level: 3, name });
      expect(head.querySelector(".fi-me-count")).toBeNull();
      expect(head.textContent).toBe(name);
    }
    // The review step, the flag and the phrase: the part's count is of those.
    expect(screen.getByText("3 items")).toBeInTheDocument();
    fireEvent.click(qa().getByRole("button", { name: ADD }));
    fireEvent.change(screen.getAllByRole("textbox", { name: "Name" })[1], { target: { value: "Certified Example Auditor" } });
    expect(screen.getByText("3 items")).toBeInTheDocument();
  });

  it("lists the owner's certifications and licences in one column named Name, with a line on what the app already knows", async () => {
    serve();
    open();
    await qa().findByText("No em dash");
    const list = listAfter("Certifications and licences");
    expect(list.getByRole("textbox", { name: "Name" })).toHaveValue("Certified Example Analyst");
    expect(list.getByRole("button", { name: "Remove Certified Example Analyst" })).toBeInTheDocument();
    expect(list.getByRole("button", { name: ADD })).toBeInTheDocument();
    expect(
      qa().getByText("The app already knows common ones. Add the ones your field uses. A packet that names one your master doesn't hold is refused."),
    ).toBeInTheDocument();
  });

  it("adds a name, takes one away, and accepts a very long name whole", async () => {
    const puts = serve();
    open();
    await qa().findByText("No em dash");
    const long = `Certified ${"Example ".repeat(700)}Practitioner`;
    fireEvent.click(qa().getByRole("button", { name: ADD }));
    const added = screen.getAllByRole("textbox", { name: "Name" })[1];
    expect(added).toHaveFocus();
    fireEvent.change(added, { target: { value: long } });
    expect(bar()).toHaveTextContent("Unsaved changes to Settings for QA of documentation");
    fireEvent.click(qa().getByRole("button", { name: "Remove Certified Example Analyst" }));
    save();
    await waitFor(() => expect(puts).toHaveLength(1));
    const saved = puts[0].body.data.credentials;
    expect(saved).toHaveLength(1);
    expect(saved[0].name).toBe(long);
    expect(saved[0].id).toMatch(/^certified-example/);
  });

  it("keeps a name on one line: a break typed or pasted into it becomes a space", async () => {
    serve();
    open();
    await qa().findByText("No em dash");
    fireEvent.click(qa().getByRole("button", { name: ADD }));
    const added = screen.getAllByRole("textbox", { name: "Name" })[1];
    fireEvent.change(added, { target: { value: "Certified Example\nAuditor" } });
    expect(added).toHaveValue("Certified Example Auditor");
  });

  it("leaves out a name that was added and never written, so adding one and leaving it is no change", async () => {
    serve();
    open();
    await qa().findByText("No em dash");
    fireEvent.click(qa().getByRole("button", { name: ADD }));
    expect(screen.getAllByRole("textbox", { name: "Name" })).toHaveLength(2);
    expect(bar()).toBeNull();
  });

  it("still lists the certifications when the app's checks can't be loaded, and draws no style rules it knows nothing of", async () => {
    let asked = false;
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/documents/built-in-checks")) {
        asked = true;
        return json({ detail: "Not now." }, 500);
      }
      return json({}, 404);
    });
    open();
    expect(await screen.findByRole("heading", { level: 3, name: "Certifications and licences" })).toBeInTheDocument();
    await waitFor(() => expect(asked).toBe(true));
    expect(screen.queryByRole("heading", { level: 3, name: "Style rules" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("draws no style rules when the app sends none, and the checks it does send are still listed", async () => {
    serve({ checks: CHECKS });
    open();
    await qa().findByText("A count of years of experience");
    expect(screen.queryByRole("heading", { level: 3, name: "Style rules" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByRole("heading", { level: 3, name: "Certifications and licences" })).toBeInTheDocument();
  });
});

describe("WritingTab: Document Creation Rules, Rules for interviews", () => {
  const interviewPart = () => within(screen.getByRole("heading", { level: 2, name: /^Rules for interviews/ }).closest("section") as HTMLElement);

  it("is a part between the ATS rules and the QA settings, with its hint", async () => {
    serve();
    open({ ...WRITING, interview_rules: [{ id: "stories", says: "Answer with a story." }] });
    const heads = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent ?? "");
    const at = (name: string) => heads.findIndex((h) => h.startsWith(name));
    expect(at("Rules for ATS screening")).toBeLessThan(at("Rules for interviews"));
    expect(at("Rules for interviews")).toBeLessThan(at("Settings for QA of documentation"));
    expect(interviewPart().getByText(/Only interview prep reads these/)).toBeInTheDocument();
    expect(interviewPart().getByDisplayValue("Answer with a story.")).toBeInTheDocument();
  });

  it("adds a rule and saves interview_rules with the rest of the writing as it was", async () => {
    const puts = serve();
    open();
    fireEvent.click(interviewPart().getByRole("button", { name: /Add/ }));
    const box = interviewPart().getByRole("textbox", { name: "Interview rule 1" });
    fireEvent.change(box, { target: { value: "Never say 'passionate'." } });
    expect(bar()).toHaveTextContent("Unsaved changes to Rules for interviews");
    save();
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].body.data.interview_rules.map((r) => r.says)).toEqual(["Never say 'passionate'."]);
    expect(puts[0].body.data.voice_rules).toEqual(WRITING.voice_rules);
  });
});
