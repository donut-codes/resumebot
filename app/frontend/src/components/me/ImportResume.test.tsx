import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Documents } from "../../api/types/me";
import type { AgentReady, ParsedResume, ParseJob } from "../../api/types/profile";
import type { MePage } from "../../lib/route";
import { json, render, stubFetch } from "../../test-utils";
import { fakeLive, pollTimers } from "../../test-live";
import { ToastProvider } from "../toast/ToastProvider";
import MeScreen from "./MeScreen";

// Made up: no real person's resume, contact details or rules.
const DOCS: Documents = {
  resume: {
    summary: "What the master said.",
    summary_variants: [{ id: "v1", text: "A variant", note: "" }],
    achievements: [{ id: "a1", text: "An old achievement", note: "approved phrasing" }],
    philosophy: "An old philosophy.",
    competencies: [{ id: "old", area: "Old area", items: ["Old term"] }],
    roles: [
      {
        id: "old-role", employer: "Old Employer", location: "", title: "Old Title", start: "2010", end: "2012", intro: "", descriptor: "", optional: true,
        note: "A role note", bullets: [{ id: "ob", text: "Old bullet", group: "Group", note: "A bullet note" }],
        tools: ["Visio"], evidence: [{ id: "tools", title: "Tools", text: "Visio, Miro." }],
      },
    ],
    education: [{ id: "ms", degree: "Old degree", school: "Old school", note: "" }],
    projects: [{ id: "pr", text: "Old project", note: "" }],
    awards: [{ id: "aw", text: "Old award", note: "" }],
    notes: [{ id: "todo", text: "- [ ] Confirm the start month", source: "TODO" }],
  },
  about: {
    name: "Old Name", location: "Old place", metro_location: "The metro area", address: "1 Old Street", phone: "555-0100",
    email: "old@example.test", links: ["example.test/old"], work_authorization: "Yes", sponsorship: "No", current_company: "",
    notes: [],
  },
  writing: {
    voice_rules: [{ id: "voice", says: "Plain." }], ats_rules: [], interview_rules: [], resume_global: [], resume_header: [], resume_summary: [], resume_competencies: [], resume_achievements: [], resume_experience: [], resume_education: [], role_rules: {}, page_margin_inches: null, letter_global: [], letter_header: [], letter_greeting: [], letter_opening: [], letter_body: [], letter_closing: [], letter_signoff: [], letter_role_rules: {}, banned: [], legacy_patterns: [], review_steps: [],
    drift_patterns: [], review_flags: [], humanizing: [], style_off: [], credentials: [], notes: [],
  },
  versions: { resume: 3, about: 1, writing: 1 },
};

/** What reading a resume gives: the resume's own words in the master resume's parts, and the contact details. */
const READ: ParsedResume = {
  summary: "Builds projects that people use.",
  achievements: ["Opened a second office in a year", "Cut the backlog by half"],
  competencies: [{ area: "Governance", items: ["Risk frameworks", "Policy design"] }],
  roles: [
    {
      employer: "Northwind Labs", title: "Director", location: "Oakfield, ST", start: "2019", end: "Present", intro: "Sole leader of the practice.",
      descriptor: "", bullets: ["Led the risk working group.", "Built the knowledge base."],
    },
    { employer: "Fabrikam", title: "Engineer", location: "", start: "2012", end: "2015", intro: "", descriptor: "", bullets: [] },
  ],
  education: [{ degree: "MS in Leadership", school: "Example University" }],
  philosophy: "",
  awards: [],
  projects: [],
  contact: { name: "Sam Sample", location: "Riverton, ST", address: "", phone: "555-0111", email: "sam@example.test", links: ["example.test/in/sam"] },
};
const NO_CONTACT = { name: "", location: "", address: "", phone: "", email: "", links: [] };

const FLOW = (fields: ParsedResume = READ): ParseJob<ParsedResume>[] => [
  { state: "waiting", step: "Waiting for an agent" },
  { state: "reading", step: "Reading your resume" },
  { state: "done", step: "Done", fields },
];

interface Call {
  url: string;
  method: string;
  type: string | null;
  body: unknown;
}
interface Options {
  ready?: AgentReady;
  /** What starting a reading answers: a job, or a refusal. */
  start?: Response | Promise<Response>;
  jobs?: ParseJob<ParsedResume>[];
  /** The server no longer knows the job. */
  gone?: boolean;
  /** The documents as saved, when not DOCS. */
  saved?: Documents;
}

/** A server for Me: the documents (kept as saved), whether an agent is free, and a reading that goes through its steps. */
function serve({ ready = { ready: true, why: null }, start, jobs = FLOW(), saved = DOCS, gone = false }: Options = {}) {
  let docs = structuredClone(saved);
  const calls: Call[] = [];
  const puts: { url: string; body: { data: Record<string, unknown>; why: string } }[] = [];
  let polls = 0;
  stubFetch((input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);
    if (method === "PUT") {
      const body = JSON.parse(String(init?.body));
      puts.push({ url: input, body });
      const kind = /\/profile\/(\w+)$/.exec(input)?.[1];
      if (kind) docs = { ...docs, [kind]: body.data };
      return json({ saved: true, version: 2 });
    }
    if (input.endsWith("/api/v1/agent/ready")) return json(ready);
    if (input.includes("/api/v1/profile/parse/") && method === "GET") {
      if (gone) return json({}, 404);
      const job = jobs[Math.min(polls++, jobs.length - 1)];
      return json(job);
    }
    if (input.includes("/api/v1/profile/parse/resume")) {
      const sent = init?.body;
      calls.push({ url: input, method, type: headers.get("Content-Type"), body: typeof sent === "string" ? JSON.parse(sent) : sent });
      return start ?? json({ id: "j1" });
    }
    if (input.endsWith("/api/v1/documents")) return json(docs);
    if (/\/api\/v1\/profile\/\w+\/history$/.test(input)) return json({ kind: "resume", versions: [] });
    return json({}, 404);
  });
  return { calls, puts };
}

const PAGE_TAB: Record<MePage, string> = {
  resume: "Master resume", "find-gaps": "Find Gaps", "story-library": "Story library", "resume-rules": "Resume Rules", "letter-rules": "Cover Letter Rules",
  "document-rules": "Document Creation Rules",
};

function open(part: MePage = "resume") {
  const view = render(
    <ToastProvider>
      <MeScreen />
    </ToastProvider>,
    { hash: part === "resume" ? "#/me" : `#/me/${part}` },
  );
  /** The same screen on another page of Me, by its tab: the page's own state stays. */
  const go = (next: MePage) => fireEvent.click(screen.getByRole("link", { name: PAGE_TAB[next] }));
  return { ...view, go };
}

const TITLE = "Import your resume";
const REVIEW = "Add from your resume";
const bar = () => screen.queryByRole("form", { name: "Unsaved changes" });
const dialog = () => screen.getByRole("dialog", { name: TITLE });
const review = () => screen.findByRole("dialog", { name: REVIEW });
let live: ReturnType<typeof fakeLive>;
/** The server says the job moved: what the clock's tick used to bring. `times` messages, one after the other. */
const tick = async (times = 1) => {
  for (let i = 0; i < times; i++) await live.changed("jobs");
};

/** The Import button, once an agent is known to be free. */
async function importButton() {
  const button = await screen.findByRole("button", { name: TITLE });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

/** Opens the dialog, pastes a resume and presses Read it. */
async function readPasted(text = "Sam Sample. Builds projects that people use. Director at Northwind Labs, 2019 to now.") {
  fireEvent.click(await importButton());
  fireEvent.change(within(dialog()).getByLabelText("Or paste its text"), { target: { value: text } });
  fireEvent.click(within(dialog()).getByRole("button", { name: "Read it" }));
}

/** The first answer about a reading just started ("Waiting for an agent"): the reading is asked after at once, and its
 * next ask is set only once that answer is in, a message before it has no reading to answer. */
const firstAnswer = () => within(dialog()).findByText("Waiting for an agent", { selector: "[role=status]" });

/** Reads a pasted resume through its three answers (waiting, reading, done), one tick for each of the last two, and waits
 * for the review of what it would add. A message before the first answer has no reading to answer. */
async function readToReview() {
  await readPasted();
  await firstAnswer();
  await tick();
  await tick();
  return review();
}

/** Reads a pasted resume, confirms the review, and waits for the draft it adds to. */
async function readThrough() {
  const region = await readToReview();
  fireEvent.click(within(region).getByRole("button", { name: "Add to my resume" }));
  await waitFor(() => expect(bar()).not.toBeNull());
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  live = fakeLive();
});
afterEach(() => {
  vi.useRealTimers();
  live.restore();
});

describe("Me: Import your resume, the button", () => {
  it("is on the line under the tabs of the master resume, beside History, and on no other page: the place Tidy up was", async () => {
    serve();
    const view = open("resume");
    const button = await importButton();
    expect(button).toHaveClass("fi-btn");
    expect(button).not.toHaveClass("fi-btn-go"); // the page's one sun button is Save
    const history = screen.getByRole("button", { name: /^History/ });
    expect(button.closest(".fi-me-actions")).toBe(history.closest(".fi-me-actions"));
    expect(screen.queryByRole("button", { name: "Tidy up" })).toBeNull();
    view.go("resume-rules");
    await screen.findByRole("heading", { level: 2, name: "Global" });
    expect(screen.queryByRole("button", { name: TITLE })).toBeNull();
    view.go("document-rules");
    await screen.findByRole("heading", { level: 2, name: /Rules for your voice/ });
    expect(screen.queryByRole("button", { name: TITLE })).toBeNull();
  });

  it("is off, with the reason beside it, while no agent is free: no agent checked in, or Claude's peak hours", async () => {
    const why = "Claude's peak hours: no Claude work until 5:00 PM.";
    serve({ ready: { ready: false, why } });
    open("resume");
    const button = await screen.findByRole("button", { name: TITLE });
    expect(await screen.findByText(why)).toHaveClass("fi-me-why");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", why);
    expect(button).toHaveAccessibleDescription(why);
    fireEvent.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says plainly that no agent is connected when the app gives no other reason", async () => {
    serve({ ready: { ready: false, why: null } });
    open("resume");
    expect(await screen.findByText("No agent is free to read a resume right now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: TITLE })).toBeDisabled();
  });

  it("opens a popup with a file chooser and a text area, Read it off until there is something, and Cancel puts focus back on the button", async () => {
    serve();
    open("resume");
    const button = await importButton();
    fireEvent.click(button);
    const region = dialog();
    const file = within(region).getByLabelText("Your resume file (.docx, .pdf or .txt)") as HTMLInputElement;
    expect(file.type).toBe("file");
    expect(file.accept).toBe(".docx,.pdf,.txt");
    const box = within(region).getByLabelText("Or paste its text");
    expect(box.tagName).toBe("TEXTAREA");
    expect(within(region).getByRole("button", { name: "Read it" })).toBeDisabled();
    fireEvent.change(box, { target: { value: "My resume." } });
    expect(within(region).getByRole("button", { name: "Read it" })).toBeEnabled();
    expect(button).toBeDisabled(); // the dialog is open
    fireEvent.click(within(region).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: TITLE })).toBeNull();
    expect(screen.getByRole("button", { name: TITLE })).toHaveFocus();
  });
});

describe("Me: Import your resume, reading it", () => {
  it("sends the pasted text, shows each step as it goes, then fills the master resume as unsaved changes", async () => {
    const server = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readPasted();
    await waitFor(() => expect(server.calls).toHaveLength(1));
    expect(server.calls[0]).toMatchObject({
      url: "/api/v1/profile/parse/resume", method: "POST", type: "application/json",
      body: { text: "Sam Sample. Builds projects that people use. Director at Northwind Labs, 2019 to now." },
    });
    const region = dialog();
    expect(await within(region).findByText("Waiting for an agent", { selector: "[role=status]" })).toBeInTheDocument();
    expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    const steps = within(within(region).getByRole("list", { name: "Steps" })).getAllByRole("listitem");
    expect(steps.map((s) => s.textContent)).toEqual(["Waiting for an agent", "Reading your resume", "Done"]);
    expect(within(region).getByLabelText("Or paste its text")).not.toBeVisible(); // the form gives way to the progress
    await tick();
    expect(await within(region).findByText("Reading your resume", { selector: "[role=status]" })).toBeInTheDocument();
    expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "65");
    expect(bar()).toBeNull(); // nothing is changed yet
    await tick();

    // First the review: what would be added, and where. The page is untouched until the owner confirms.
    const shown = await review();
    expect(screen.queryByRole("dialog", { name: TITLE })).toBeNull();
    expect(bar()).toBeNull();
    expect(within(shown).getByRole("region", { name: "Director, Northwind Labs" })).toHaveTextContent("New role (2019 – Present)");
    fireEvent.click(within(shown).getByRole("button", { name: "Add to my resume" }));
    await waitFor(() => expect(bar()).not.toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();

    // The parts hold what they held, with what the resume said added, unsaved; nothing was saved.
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("What the master said.");
    expect(screen.getByRole("textbox", { name: "Achievement 1" })).toHaveValue("An old achievement");
    expect(screen.getByRole("textbox", { name: "Achievement 2" })).toHaveValue("Opened a second office in a year");
    expect(screen.getByRole("textbox", { name: "Achievement 3" })).toHaveValue("Cut the backlog by half");
    expect(screen.getAllByRole("textbox", { name: "Area" }).map((a) => (a as HTMLInputElement).value)).toEqual(["Old area", "Governance"]);
    expect(screen.getAllByRole("textbox", { name: "Degree" }).map((a) => (a as HTMLInputElement).value)).toEqual(["Old degree", "MS in Leadership"]);
    expect(screen.getByText("Added from your resume: look it over, then Save.", { exact: false })).toBeInTheDocument();
    expect(bar()).toHaveTextContent("Unsaved changes to Facts forms ask for, Core competencies");
    expect(server.puts).toHaveLength(0);
    expect(screen.getByRole("button", { name: TITLE })).toBeEnabled();
  });

  it("adds to what a part held and never replaces or removes it; a part the resume does not have stays as it was", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readThrough();
    expect(screen.getByDisplayValue("An old achievement")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Old area")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Old Employer")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Northwind Labs")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Leadership philosophy" })).toHaveValue("An old philosophy.");
    expect(screen.getByRole("textbox", { name: "Award 1" })).toHaveValue("Old award");
    expect(screen.getByRole("textbox", { name: "Project 1" })).toHaveValue("Old project");
  });

  it("Undo puts back everything the import replaced, and the page is as it was", async () => {
    const server = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readThrough();
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(bar()).toBeNull();
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("What the master said.");
    expect(screen.getByRole("textbox", { name: "Achievement 1" })).toHaveValue("An old achievement");
    expect(screen.getByRole("textbox", { name: "Area" })).toHaveValue("Old area");
    expect(screen.getByRole("textbox", { name: "Degree" })).toHaveValue("Old degree");
    expect(screen.queryByText(/Added from your resume/)).toBeNull();
    expect(server.puts).toHaveLength(0);
  });

  it("is saved like any other change: one new version of the master resume, each new line given an id from its words", async () => {
    const server = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readThrough();
    fireEvent.change(within(bar()!).getByLabelText("Why (optional, one line)"), { target: { value: "imported" } });
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    // The resume it read has contact details too, saved as the facts: the resume first, then the about document.
    await waitFor(() => expect(server.puts).toHaveLength(2));
    expect(server.puts[0].url).toMatch(/\/api\/v1\/profile\/resume$/);
    expect(server.puts[0].body.why).toBe("imported");
    expect(server.puts[1].url).toMatch(/\/api\/v1\/profile\/about$/);
    expect(server.puts[1].body.why).toBe("imported");
    const saved = server.puts[0].body.data as {
      summary: string; achievements: { id: string; text: string; note: string }[]; roles: { id: string; employer: string; bullets: { id: string; text: string }[] }[];
      notes: unknown[]; summary_variants: unknown[]; philosophy: string;
    };
    // What the master held is all there, as it was; the summary read is kept as another version of the summary.
    expect(saved.summary).toBe("What the master said.");
    expect(saved.achievements.map((a) => [a.id, a.text, a.note])).toEqual([
      ["a1", "An old achievement", "approved phrasing"],
      ["opened-a-second-office-in", "Opened a second office in a year", ""],
      ["cut-the-backlog-by-half", "Cut the backlog by half", ""],
    ]);
    expect(saved.roles.map((r) => [r.id, r.employer])).toEqual([
      ["old-role", "Old Employer"], ["director-northwind-labs", "Northwind Labs"], ["engineer-fabrikam", "Fabrikam"],
    ]);
    expect(saved.roles[0]).toEqual(DOCS.resume.roles[0]);
    expect(saved.roles[1].bullets.map((b) => b.text)).toEqual(["Led the risk working group.", "Built the knowledge base."]);
    expect(saved.philosophy).toBe("An old philosophy.");
    expect(saved.notes).toEqual(DOCS.resume.notes);
    expect((saved.summary_variants as { text: string }[]).map((v) => v.text)).toEqual(["A variant", "Builds projects that people use."]);
    await waitFor(() => expect(bar()).toBeNull());
    await waitFor(() => expect(screen.queryByText(/Added from your resume/)).toBeNull());
  });

  it("sends a chosen file as its own bytes with its name, and the pasted text and the file are one or the other, the last one touched", async () => {
    const server = serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.click(await importButton());
    const region = dialog();
    const input = within(region).getByLabelText("Your resume file (.docx, .pdf or .txt)") as HTMLInputElement;
    const box = within(region).getByLabelText("Or paste its text");
    fireEvent.change(box, { target: { value: "Some pasted words." } });
    const file = new File(["resume bytes"], "My Resume.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(box).toHaveValue(""); // the file is what is read now
    expect(within(region).getByRole("button", { name: "Read it" })).toBeEnabled();
    fireEvent.change(box, { target: { value: "Typed after." } }); // and now the text
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(within(region).getByRole("button", { name: "Read it" }));
    await waitFor(() => expect(server.calls).toHaveLength(1));
    expect(server.calls[0]).toMatchObject({
      url: "/api/v1/profile/parse/resume/file?name=My%20Resume.docx", method: "POST", type: "application/octet-stream",
    });
    expect(server.calls[0].body).toBe(file);
    await firstAnswer();
    await tick();
    await tick();
    fireEvent.click(within(await review()).getByRole("button", { name: "Add to my resume" }));
    await waitFor(() => expect(bar()).not.toBeNull());
    expect(screen.getByRole("textbox", { name: "Achievement 3" })).toHaveValue("Cut the backlog by half");
  });

  it("shows a file that cannot be read in plain words and stays open to try another: a scan, a wrong kind", async () => {
    const scan = "This file has no text to read; paste the text instead.";
    const server = serve({ start: json({ detail: scan }, 422) });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    fireEvent.click(await importButton());
    const region = dialog();
    const file = new File(["%PDF"], "scan.pdf", { type: "application/pdf" });
    fireEvent.change(within(region).getByLabelText("Your resume file (.docx, .pdf or .txt)"), { target: { files: [file] } });
    fireEvent.click(within(region).getByRole("button", { name: "Read it" }));
    expect(await within(region).findByText(scan)).toBeInTheDocument();
    expect(server.calls).toHaveLength(1);
    expect(screen.getByRole("dialog", { name: TITLE })).toBeInTheDocument();
    // The person pastes the text instead.
    fireEvent.change(within(region).getByLabelText("Or paste its text"), { target: { value: "The text of the resume." } });
    expect(within(region).getByRole("button", { name: "Read it" })).toBeEnabled();
  });

  it("says in words when no agent can take it after all: the sentence the server gave", async () => {
    const why = "No agent has checked in for 3 hours.";
    serve({ start: json({ detail: why, why }, 409) });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readPasted();
    expect(await within(dialog()).findByText(why)).toBeInTheDocument();
    expect(within(dialog()).getByRole("button", { name: "Read it" })).toBeEnabled();
    expect(bar()).toBeNull();
  });

  it("takes a long pasted resume whole, and when an agent could not be sent it, keeps it in the box and says why in the banner", async () => {
    const said = "Your resume text is 214,000 characters, and the answer shape sent with it is 1,706. An agent can be sent at most 160,000 of the two at once, so it was not sent. Nothing was cut and nothing was saved. Check that it is your resume.";
    const server = serve({ start: json({ detail: said }, 413) });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const resume = "A line of the resume, as long as it is. ".repeat(130).trim(); // about 5,000 characters
    await readPasted(resume);
    const region = dialog();
    const banner = await within(region).findByRole("alert");
    expect(banner).toHaveTextContent(said);
    const box = within(region).getByLabelText("Or paste its text");
    expect(box).not.toHaveAttribute("maxlength");
    expect(box).toHaveValue(resume); // the words stay where they were typed
    expect(server.calls).toHaveLength(1);
    expect(server.calls[0].body).toEqual({ text: resume }); // sent whole
    expect(screen.getByRole("dialog", { name: TITLE })).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Read it" })).toBeEnabled();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(bar()).toBeNull();
    expect(document.querySelector(".toast")).toBeNull(); // no toast: the sentence is beside the words it is about
  });

  it("says why a reading failed, with Try again, and changes nothing", async () => {
    const error = "The agent found nothing it could use in that text. Check that it is your resume.";
    const server = serve({ jobs: [{ state: "failed", step: "Stopped", error }] });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readPasted();
    expect(await within(dialog()).findByText(error)).toBeInTheDocument();
    expect(within(dialog()).getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(within(dialog()).getByLabelText("Or paste its text")).toBeVisible();
    expect(bar()).toBeNull();
    expect(server.puts).toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("What the master said.");
  });

  it("says only that it didn't finish when the reading failed with no reason, was lost or came back empty, and offers Try again", async () => {
    for (const options of [
      { jobs: [{ state: "failed" as const, step: "Stopped" }] },
      { gone: true },
      { jobs: [{ state: "done" as const, step: "Done" }] },
    ]) {
      serve(options);
      const view = open("resume");
      await screen.findByRole("heading", { level: 1, name: "My Resume" });
      await readPasted();
      expect(await within(dialog()).findByText("That didn't finish.")).toBeInTheDocument();
      expect(within(dialog()).getByRole("button", { name: "Try again" })).toBeEnabled();
      view.unmount();
    }
  });

  it("can be cancelled while it works: nothing is filled in, even when the answer comes after", async () => {
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readPasted();
    await within(dialog()).findByRole("progressbar");
    fireEvent.click(within(dialog()).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await tick(3);
    expect(bar()).toBeNull();
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("What the master said.");
  });
});


describe("Me: Import your resume, kept fresh by the live stream", () => {
  it("asks after a reading only when the server says a job moved, and sets no timer", async () => {
    const timers = pollTimers();
    serve();
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readPasted();
    await firstAnswer();
    await live.changed("agents");
    expect(within(dialog()).getByText("Waiting for an agent", { selector: "[role=status]" })).toBeInTheDocument();
    await tick();
    expect(await within(dialog()).findByText("Reading your resume", { selector: "[role=status]" })).toBeInTheDocument();
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });
});

describe("Me: Import your resume, the review", () => {
  /** A resume read that repeats one line the master holds, comes close to another, and adds a new one, under the master's own role. */
  const AGAIN: ParsedResume = {
    ...READ,
    summary: "",
    achievements: ["An old achievement."],
    competencies: [],
    roles: [{ employer: "Old Employer", title: "Old Title", location: "", start: "2010", end: "2012", intro: "", descriptor: "", bullets: ["Old bullet", "Led the weekly risk review", "I set up workflows"] }],
    education: [],
    contact: NO_CONTACT,
  };
  /** The master with a second, longer bullet under its role: one a line read can come close to. */
  const LONGER: Documents = {
    ...DOCS,
    resume: {
      ...DOCS.resume,
      roles: [{ ...DOCS.resume.roles[0], bullets: [...DOCS.resume.roles[0].bullets, { id: "risk", text: "Led the weekly risk review for the board", group: "", note: "" }] }],
    },
  };

  it("shows per role what is new, what is similar to a line there, and what is skipped as already there", async () => {
    serve({ jobs: FLOW(AGAIN), saved: LONGER });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const shown = await readToReview();
    expect(shown).toHaveTextContent("1 new line, 1 similar to a line you have (placed beside it), 2 already there (skipped). Nothing you wrote is removed or reworded.");
    const role = within(shown).getByRole("region", { name: "Old Title, Old Employer" });
    expect(role).toHaveTextContent("Already in your resume (2010 – 2012)");
    const lines = within(role).getAllByRole("listitem").map((l) => l.textContent);
    expect(lines).toEqual(["Already thereOld bullet", "SimilarLed the weekly risk reviewBeside: Led the weekly risk review for the board", "NewI set up workflows"]);
    expect(within(shown).getByRole("region", { name: "Selected achievements" })).toHaveTextContent("Already thereAn old achievement.");
  });

  it("saves nothing and changes nothing until the owner confirms; Cancel leaves the page as it was", async () => {
    const server = serve({ jobs: FLOW(AGAIN) });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const shown = await readToReview();
    await tick(3);
    expect(bar()).toBeNull();
    expect(server.puts).toHaveLength(0);
    fireEvent.click(within(shown).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: REVIEW })).toBeNull();
    expect(bar()).toBeNull();
    expect(server.puts).toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "Achievement 1" })).toHaveValue("An old achievement");
    expect(screen.queryByRole("textbox", { name: "Achievement 2" })).toBeNull();
  });

  it("offers nothing to add when everything is already there", async () => {
    serve({ jobs: FLOW({ ...AGAIN, roles: [{ ...AGAIN.roles[0], bullets: ["Old bullet"] }] }) });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const shown = await readToReview();
    expect(shown).toHaveTextContent("Everything this resume holds is already in your master resume.");
    expect(within(shown).getByRole("button", { name: "Add to my resume" })).toBeDisabled();
  });
});

describe("Me: Import your resume, the contact details", () => {
  /** The documents with no name, phone or links yet: the facts an import may fill. */
  const SPARSE: Documents = { ...DOCS, about: { ...DOCS.about, name: "", phone: "", links: [] } };

  it("fills only the facts left empty, keeps every one already there, and saves them with the resume", async () => {
    const server = serve({ saved: SPARSE });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    const shown = await readToReview();
    const facts = within(shown).getByRole("region", { name: "Facts forms ask for" });
    expect(facts).toHaveTextContent('Where you live: yours is kept ("Old place"); this resume says "Riverton, ST".');
    fireEvent.click(within(shown).getByRole("button", { name: "Add to my resume" }));
    await waitFor(() => expect(bar()).not.toBeNull());
    expect(screen.getByText(/Contact details you had not filled in were added too\./)).toBeInTheDocument();
    expect(bar()).toHaveTextContent("Unsaved changes to Facts forms ask for");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Sam Sample");
    expect(screen.getByRole("textbox", { name: /^Phone/ })).toHaveValue("555-0111");
    expect(screen.getByRole("textbox", { name: /^Links/ })).toHaveValue("example.test/in/sam");
    expect(screen.getByRole("textbox", { name: /^Where you live/ })).toHaveValue("Old place");
    expect(screen.getByRole("textbox", { name: /^Email/ })).toHaveValue("old@example.test");
    expect(screen.getByRole("textbox", { name: /^Street address/ })).toHaveValue("1 Old Street");
    expect(server.puts).toHaveLength(0);

    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(server.puts).toHaveLength(2));
    await waitFor(() => expect(bar()).toBeNull());
    const about = server.puts.find((p) => p.url.endsWith("/profile/about"))!;
    expect(about.body.data).toMatchObject({
      name: "Sam Sample", location: "Old place", phone: "555-0111", email: "old@example.test", links: ["example.test/in/sam"],
      address: "1 Old Street", metro_location: "The metro area", work_authorization: "Yes",
    });
  });

  it("Undo puts the old facts back along with the old resume", async () => {
    const server = serve({ saved: SPARSE });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readThrough();
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Sam Sample");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Achievement 1" })).toHaveValue("An old achievement");
    expect(bar()).toBeNull();
    expect(server.puts).toHaveLength(0);
  });

  it("says nothing of contact details, and leaves the facts alone, when the resume gave none", async () => {
    const server = serve({ jobs: FLOW({ ...READ, contact: NO_CONTACT }) });
    open("resume");
    await screen.findByRole("heading", { level: 1, name: "My Resume" });
    await readThrough();
    expect(screen.getByText(/Added from your resume/)).toBeInTheDocument();
    expect(screen.queryByText(/Contact details/)).toBeNull();
    expect(bar()).not.toHaveTextContent("Facts forms ask for");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Old Name");
    fireEvent.click(within(bar()!).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(server.puts).toHaveLength(1));
    await waitFor(() => expect(bar()).toBeNull());
    expect(server.puts[0].url).toMatch(/\/profile\/resume$/);
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Old Name");
  });
});

describe("Me: Import your resume, a reading the owner's Claude started", () => {
  it("opens the dialog on the job in the address, shows its steps, then the review, and starts no reading of its own", async () => {
    const server = serve();
    render(
      <ToastProvider>
        <MeScreen />
      </ToastProvider>,
      { hash: "#/me?import=j9" },
    );
    const region = await screen.findByRole("dialog", { name: TITLE });
    expect(await within(region).findByText("Waiting for an agent", { selector: "[role=status]" })).toBeInTheDocument();
    expect(within(region).getByLabelText("Or paste its text")).not.toBeVisible(); // the form gives way to the progress
    await tick();
    await tick();
    const added = await review();
    expect(within(added).getByRole("button", { name: "Add to my resume" })).toBeInTheDocument();
    expect(server.calls).toEqual([]); // the page started nothing: the reading was Claude's
    expect(window.location.hash).toBe("#/me"); // the address no longer names the job
  });

  it("puts the address back as it was when the owner closes the dialog without waiting for it", async () => {
    serve();
    render(
      <ToastProvider>
        <MeScreen />
      </ToastProvider>,
      { hash: "#/me?import=j9" },
    );
    const region = await screen.findByRole("dialog", { name: TITLE });
    fireEvent.click(await within(region).findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: TITLE })).toBeNull();
    expect(window.location.hash).toBe("#/me");
  });
});
