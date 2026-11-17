import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Documents, GapDashboard, GapDraft, GapJobOption, GapRunView, GapTheme } from "../../api/types/me";
import type { AgentReady } from "../../api/types/profile";
import { fakeLive, pollTimers } from "../../test-live";
import { json, render, stubFetch } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import MeScreen from "./MeScreen";

// Made up: no real person's resume, employers or roles.
const DOCS: Documents = {
  resume: {
    summary: "Builds projects that people use.",
    summary_variants: [],
    achievements: [{ id: "a1", text: "Led a statewide project", note: "" }],
    philosophy: "",
    competencies: [],
    roles: [
      {
        id: "lead", employer: "Northwind Labs", location: "", title: "Senior Manager", start: "2022", end: "2026", intro: "", descriptor: "",
        optional: false, note: "", tools: ["Visio"], evidence: [{ id: "board", title: "Board deck", text: "Presented the risk register" }],
        bullets: [{ id: "b-risk", text: "Led the risk working group", group: "", note: "" }],
      },
      {
        id: "eng", employer: "Fabrikam", location: "", title: "Engineer", start: "2012", end: "2015", intro: "", descriptor: "", optional: true,
        note: "", tools: [], bullets: [], evidence: [],
      },
    ],
    education: [],
    projects: [],
    awards: [],
    notes: [],
  },
  about: {
    name: "Pat Example", location: "", metro_location: "", address: "", phone: "", email: "", links: [], work_authorization: "", sponsorship: "",
    current_company: "", notes: [],
  },
  writing: {
    voice_rules: [], ats_rules: [], interview_rules: [], resume_global: [], resume_header: [], resume_summary: [], resume_competencies: [], resume_achievements: [],
    resume_experience: [], resume_education: [], role_rules: {}, page_margin_inches: null, letter_global: [], letter_header: [], letter_greeting: [],
    letter_opening: [], letter_body: [], letter_closing: [], letter_signoff: [], letter_role_rules: {}, banned: [], legacy_patterns: [],
    review_steps: [], drift_patterns: [], review_flags: [], humanizing: [], style_off: [], credentials: [], notes: [],
  },
  versions: { resume: 3, about: 1, writing: 1 },
};

const NOW = new Date().toISOString();
const DASH: GapDashboard = {
  roles: [
    { id: "role-a", name: "Role One", researched: 10, counts: { P: 6, T: 3, U: 1 }, percent: { P: 60, T: 25, U: 15 }, last_run: { id: "r1", at: NOW } },
    { id: "role-b", name: "Role Two", researched: 6, counts: { P: 2, T: 3, U: 1 }, percent: { P: 35, T: 40, U: 25 }, last_run: null },
    { id: "role-c", name: "Role Three", researched: 0, counts: { P: 0, T: 0, U: 0 }, percent: null, last_run: null },
  ],
  active: null,
  failed: null,
  job_runs: [],
};

const POSTINGS = Array.from({ length: 10 }, (_, i) => ({
  ref: `p${i + 1}`, employer: `Employer ${i + 1}`, title: `Projects Lead ${i + 1}`, url: `https://jobs.example.test/${i + 1}`,
}));

function theme(id: string, name: string, kind: GapTheme["kind"], asked: number, grade: GapTheme["grade"], over: Partial<GapTheme> = {}): GapTheme {
  return {
    id, name, kind, asked, of: 10, grade, words: { P: "Proven", T: "Transferable", U: "Unproven" }[grade], why: "",
    lines: grade === "U" ? [] : [{ id: "b-risk", text: "Led the risk working group" }],
    postings: POSTINGS.slice(0, asked).map((p) => p.ref), answer: null, answer_id: null, ...over,
  };
}

const THEMES: GapTheme[] = [
  theme("projects-at-scale", "Projects at scale", "required", 10, "P"),
  theme("governance", "Governance and risk", "required", 7, "T", { why: "close work in risk" }),
  theme("data-catalog", "Data catalog tooling", "required", 6, "U"),
  theme("team-lead", "Leading a team", "responsibility", 5, "P"),
  theme("certification", "A certification", "preferred", 3, "U"),
];

function run(over: Partial<GapRunView> = {}): GapRunView {
  return {
    id: "r1", role_id: "role-a", title: "Role One", kind: "role", state: "done", step: "Done", read: 10, total: 10, note: null,
    checked_at: NOW, instructions_version: 1, can_repeat: true, postings: POSTINGS, themes: THEMES, hidden: [], stale: false, job: null,
    clarify: null, ...over,
  };
}

const LINE = "Picked and rolled out a data catalog for finance and about 40 reports.";

interface Options {
  ready?: AgentReady;
  dash?: GapDashboard;
  /** What each run answers, by id: a list of answers, one for each time it is asked (the last one repeats). */
  runs?: Record<string, GapRunView[]>;
  /** What starting a run answers. */
  start?: () => Promise<Response>;
  /** What a drafted line goes through. */
  drafts?: GapDraft[];
  /** The server no longer knows the draft. */
  draftGone?: boolean;
  /** The picker's list of roles, read when it is first opened. */
  jobs?: GapJobOption[];
  /** What saving the resume answers. */
  saveResume?: () => Promise<Response>;
}

/** A server for Find Gaps: the dashboard, the runs it keeps, the drafts, and the resume's own save. Everything sent is kept. */
function serve({ ready = { ready: true, why: null }, dash = DASH, runs = { r1: [run()] }, start, drafts, jobs = [], saveResume, draftGone = false }: Options = {}) {
  const sent: { method: string; url: string; body: unknown }[] = [];
  const seen: Record<string, number> = {};
  let draftPolls = 0;
  let jobReads = 0;
  const current = { dash: structuredClone(dash), runs: structuredClone(runs) };
  stubFetch((input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    if (method !== "GET") sent.push({ method, url: input.replace("/api/v1", ""), body });
    if (input.endsWith("/api/v1/agent/ready")) return json(ready);
    if (input.endsWith("/api/v1/documents")) return json(DOCS);
    if (input.endsWith("/api/v1/find-gaps")) return json(current.dash);
    if (input.endsWith("/api/v1/find-gaps/jobs")) {
      jobReads += 1;
      return json(jobs);
    }
    if (method === "POST" && input.endsWith("/api/v1/find-gaps/runs")) {
      if (start) return start();
      const title = body.role_id ? (current.dash.roles.find((r) => r.id === body.role_id)?.name ?? "") : body.title;
      current.dash.active = {
        id: "r2", role_id: body.role_id ?? null, title, kind: body.role_id ? "role" : "new", state: "waiting", step: "Waiting for an agent", read: 0, total: 10,
      };
      return json({ id: "r2" });
    }
    const one = /\/api\/v1\/find-gaps\/runs\/(\w+)$/.exec(input);
    if (one && method === "GET") {
      const answers = current.runs[one[1]];
      if (!answers) return json({ detail: "That run is gone. Run Find gaps again." }, 404);
      const n = seen[one[1]] ?? 0;
      seen[one[1]] = n + 1;
      return json(answers[Math.min(n, answers.length - 1)]);
    }
    if (/\/themes\/[\w-]+\/answer$/.test(input) && method === "PUT") {
      const [, runId, themeId] = /runs\/(\w+)\/themes\/([\w-]+)\/answer/.exec(input) ?? [];
      const answers = current.runs[runId];
      const view = answers[answers.length - 1];
      const target = view.themes.find((t) => t.id === themeId);
      if (target && body.answer === "not_done") Object.assign(target, { answer: "not_done", answer_id: "ans1" });
      if (target && body.answer === "not_relevant") {
        view.themes = view.themes.filter((t) => t.id !== themeId);
        view.hidden = [{ id: "ans2", name: target.name, kind: target.kind }];
      }
      return json({ id: body.answer === "not_done" ? "ans1" : "ans2", answer: body.answer });
    }
    if (method === "DELETE" && /\/find-gaps\/answers\/ans\d$/.test(input)) {
      const view = current.runs.r1[current.runs.r1.length - 1];
      if (input.endsWith("ans2")) {
        view.themes.push(theme("data-catalog", "Data catalog tooling", "required", 6, "U"));
        view.hidden = [];
      } else {
        const t = view.themes.find((x) => x.answer_id === "ans1");
        if (t) Object.assign(t, { answer: null, answer_id: null });
      }
      return json({ cleared: true });
    }
    if (method === "POST" && /\/themes\/[\w-]+\/draft$/.test(input)) return json({ id: "d1" });
    if (input.endsWith("/api/v1/find-gaps/drafts/d1")) {
      if (draftGone) return json({}, 404);
      const list: GapDraft[] = drafts ?? [
        { id: "d1", state: "waiting", step: "Waiting for an agent" },
        { id: "d1", state: "working", step: "Drafting the line" },
        { id: "d1", state: "done", step: "Done", draft: LINE },
      ];
      return json(list[Math.min(draftPolls++, list.length - 1)]);
    }
    if (method === "PUT" && input.endsWith("/api/v1/profile/resume")) return saveResume ? saveResume() : json({ saved: true, version: 4 });
    if (method === "GET" && input.endsWith("/api/v1/profile")) {
      return json({ criteria: { company: "A short company line.", target_roles: [{ id: "role-a", name: "Role One" }] }, versions: { criteria: 7 } });
    }
    if (method === "PUT" && input.endsWith("/api/v1/profile/criteria")) return json({ saved: true, version: 8 });
    if (method === "POST" && /\/runs\/\w+\/(check-again|new-sample)$/.test(input)) return json({ id: "r3" });
    if (/\/api\/v1\/profile\/\w+\/history$/.test(input)) return json({ kind: "resume", versions: [] });
    return json({}, 404);
  });
  return { sent, current, jobReads: () => jobReads };
}

function open() {
  return render(
    <ToastProvider>
      <MeScreen />
    </ToastProvider>,
    { hash: "#/me/find-gaps" },
  );
}

let live: ReturnType<typeof fakeLive>;
/** The server says the run moved (Find Gaps, a run, a draft): what the clock's tick used to bring. */
const tick = () => live.changed("gaps");
const theBox = (name: string) => screen.getByRole("textbox", { name });

/** A button that is on once an agent is known to be free. */
async function on(name: string | RegExp) {
  const button = await screen.findByRole("button", { name });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

/** The theme named, opened. */
async function openTheme(name: string) {
  fireEvent.click(await screen.findByRole("button", { name }));
}

/** Types the owner's words and asks for the line; waits for the draft box. The draft is read at once when it is sent; each tick after is a message from the server that it moved. */
async function draftLine(words: string) {
  fireEvent.change(theBox("What you did, in your own words"), { target: { value: words } });
  fireEvent.click(await on("Draft the line"));
  await screen.findByText("Waiting for an agent", { selector: "[role=status]" });
  await tick();
  await tick();
  return screen.findByRole("textbox", { name: "The draft line, to edit as you like" });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  live = fakeLive();
});
afterEach(() => {
  vi.useRealTimers();
  live.restore();
});

describe("Find Gaps: the dashboard", () => {
  it("is a row for each target role with its name, researched roles, grade bar, last check and a button, and a field for a new title", async () => {
    serve();
    open();
    await on("Find gaps for Role One");
    expect(screen.getAllByRole("button", { name: /^Find gaps for (?!a new)/ }).map((b) => b.getAttribute("aria-label"))).toEqual([
      "Find gaps for Role One", "Find gaps for Role Two", "Find gaps for Role Three",
    ]);
    expect(screen.getByRole("heading", { level: 2, name: "Target roles" })).toBeInTheDocument();
    expect(screen.getByText("from 10 roles")).toBeInTheDocument();
    expect(screen.getByText("from 6 roles")).toBeInTheDocument();
    expect(screen.getByText("No researched roles yet")).toBeInTheDocument();
    const bars = screen.getAllByRole("img", { name: /Proven|No bar/ });
    expect(bars.map((b) => b.getAttribute("aria-label"))).toEqual([
      "60% Proven, 25% Transferable, 15% Unproven", "35% Proven, 40% Transferable, 25% Unproven", "No bar: no researched roles yet",
    ]);
    expect(screen.getAllByText("60%")).toHaveLength(1);
    expect(screen.getAllByText(/^Checked /)).toHaveLength(1);
    expect(screen.getAllByText("Not checked")).toHaveLength(2);
    expect(screen.getByLabelText("Try a new role")).toHaveAttribute("placeholder", "Role title");
    expect(screen.getByRole("button", { name: "Find gaps for a new role" })).toBeDisabled();
  });

  it("is the second tab of My Resume and says what it is for", async () => {
    serve();
    open();
    const tabs = within(await screen.findByRole("navigation", { name: "My Resume" }));
    expect(tabs.getAllByRole("link").map((t) => t.textContent)).toEqual(["Master resume", "Find Gaps", "Story library", "Resume Rules", "Cover Letter Rules", "Document Creation Rules"]);
    expect(tabs.getByRole("link", { name: "Find Gaps" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText(/What postings for your target roles ask for that your master resume does not yet show/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^History/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Import your resume" })).toBeNull();
  });

  it("says plainly when there is no target role yet", async () => {
    serve({ dash: { ...DASH, roles: [] } });
    open();
    expect(await screen.findByText(/You have no target role yet/)).toBeInTheDocument();
    expect(screen.getByLabelText("Try a new role")).toBeInTheDocument();
  });

  it("has every Find gaps button off, with the one-line why, in Claude's peak hours, and Check again and New sample too", async () => {
    const why = "Claude's peak hours: no Claude work until 5:00 PM.";
    serve({ ready: { ready: false, why } });
    open();
    const buttons = await screen.findAllByRole("button", { name: /^Find gaps for/ });
    expect(buttons).toHaveLength(4);
    for (const b of buttons) expect(b).toBeDisabled();
    expect(await screen.findByText(why)).toHaveClass("fi-me-why");
    expect(buttons[0]).toHaveAttribute("title", why);
    expect(buttons[0]).toHaveAccessibleDescription(why);
    expect(await screen.findByRole("button", { name: "Check again" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "New sample" })).toBeDisabled();
  });
});

describe("Find Gaps: a run", () => {
  it("opens on the latest check: what it read and when, the themes most postings ask first, each with its kind, count and grade", async () => {
    serve();
    open();
    expect(await screen.findByRole("heading", { level: 2, name: "Role One" })).toBeInTheDocument();
    expect(screen.getByText(/^10 postings from 10 employers · checked /)).toBeInTheDocument();
    expect(screen.getByText(/instructions version 1/)).toBeInTheDocument();
    const names = screen.getAllByRole("button", { name: /^(Projects at scale|Governance and risk|Data catalog tooling|Leading a team|A certification)$/ });
    expect(names.map((b) => b.textContent)).toEqual(["Projects at scale", "Governance and risk", "Data catalog tooling", "Leading a team", "A certification"]);
    expect(screen.getAllByText(/^\d+ of 10$/).map((e) => e.textContent)).toEqual(["10 of 10", "7 of 10", "6 of 10", "5 of 10", "3 of 10"]);
    expect(screen.getAllByText("Required")).toHaveLength(3);
    expect(screen.getByText("Responsibility")).toBeInTheDocument();
    expect(screen.getByText("Preferred")).toBeInTheDocument();
    const grades = Array.from(document.querySelectorAll(".fi-me-gap-themes [data-grade]")).map((e) => e.textContent);
    expect(grades).toEqual(["Proven", "Transferable", "Unproven", "Proven", "Unproven"]);
    // The master line that backs a grade is linked to the master; an Unproven theme has none.
    expect(screen.getAllByRole("link", { name: /Led the risk working group/ })[0]).toHaveAttribute("href", "#/me");
    expect(screen.getAllByRole("link", { name: /Led the risk working group/ })).toHaveLength(3);
    expect(screen.queryByText("Your resume changed since this check")).toBeNull();
  });

  it("lists, when a theme is clicked, exactly the postings that asked it, with a link to each", async () => {
    serve();
    open();
    await openTheme("A certification");
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell").map((c) => c.textContent))).toEqual([
      ["Employer 1", "Projects Lead 1"], ["Employer 2", "Projects Lead 2"], ["Employer 3", "Projects Lead 3"],
    ]);
    expect(within(table).getByRole("link", { name: "Projects Lead 2" })).toHaveAttribute("href", "https://jobs.example.test/2");
    expect(screen.getByRole("button", { name: "A certification" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "A certification" }));
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("says the resume changed since the check, says how many employers it read, and Check again asks for the same postings again", async () => {
    const server = serve({ runs: { r1: [run({ stale: true, note: "Only 7 employers have a posting for this role; this reads 7." })] } });
    open();
    expect(await screen.findByText("Your resume changed since this check")).toBeInTheDocument();
    expect(screen.getByText("Only 7 employers have a posting for this role; this reads 7.")).toBeInTheDocument();
    fireEvent.click(await on("Check again"));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0]).toEqual({ method: "POST", url: "/find-gaps/runs/r1/check-again", body: undefined });
  });

  it("starts a new sample only when New sample is clicked", async () => {
    const server = serve();
    open();
    const button = await on("New sample");
    expect(server.sent).toEqual([]);
    fireEvent.click(button);
    await waitFor(() => expect(server.sent.map((s) => s.url)).toEqual(["/find-gaps/runs/r1/new-sample"]));
  });

  it("shows a run that works: waiting, then how many postings it has read, and then its themes", async () => {
    const waiting = run({ id: "r2", state: "waiting", step: "Waiting for an agent", read: 0, themes: [], checked_at: null });
    const reading = (n: number) => run({ id: "r2", state: "working", step: `${n} of 10 read`, read: n, themes: [], checked_at: null });
    const server = serve({ runs: { r1: [run()], r2: [waiting, reading(3), reading(7), run({ id: "r2", themes: [THEMES[0]] })] } });
    open();
    fireEvent.click(await on("Find gaps for Role One"));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0]).toEqual({ method: "POST", url: "/find-gaps/runs", body: { role_id: "role-a" } });
    expect(await screen.findByText("Waiting for an agent")).toBeInTheDocument();
    await tick();
    expect(await screen.findByText("3 of 10 read")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Postings read" })).toHaveAttribute("aria-valuenow", "3");
    for (const b of screen.getAllByRole("button", { name: /^Find gaps for/ })) expect(b).toBeDisabled();
    await tick();
    expect(await screen.findByText("7 of 10 read")).toBeInTheDocument();
    server.current.dash.active = null;
    server.current.dash.roles[0].last_run = { id: "r2", at: NOW };
    await tick();
    expect(await screen.findByRole("button", { name: "Projects at scale" })).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("reads the dashboard and the run again on each changed message of gaps, and sets no timer", async () => {
    const timers = pollTimers();
    const waiting = run({ id: "r2", state: "waiting", step: "Waiting for an agent", read: 0, themes: [], checked_at: null });
    const reading = run({ id: "r2", state: "working", step: "3 of 10 read", read: 3, themes: [], checked_at: null });
    const server = serve({ runs: { r1: [run()], r2: [waiting, reading] } });
    open();
    fireEvent.click(await on("Find gaps for Role One"));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(await screen.findByText("Waiting for an agent")).toBeInTheDocument();
    await live.changed("agents");
    expect(screen.getByText("Waiting for an agent")).toBeInTheDocument();
    await tick();
    expect(await screen.findByText("3 of 10 read")).toBeInTheDocument();
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });

  it("starts a run while the agent is busy (the server says it is ready) and shows its queued line until the run starts", async () => {
    const queued = "Queued: the agent is busy researching a role, which takes about 3 minutes. This starts as soon as it has finished.";
    const waiting = run({ id: "r2", state: "waiting", step: queued, read: 0, themes: [], checked_at: null });
    const working = run({ id: "r2", state: "working", step: "Starting", read: 0, themes: [], checked_at: null });
    const server = serve({ runs: { r1: [run()], r2: [waiting, working] } });
    open();
    const button = await on("Find gaps for Role One");
    expect(screen.queryByText(/The agent is busy/)).toBeNull();
    fireEvent.click(button);
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0]).toEqual({ method: "POST", url: "/find-gaps/runs", body: { role_id: "role-a" } });
    expect(await screen.findByText(queued)).toHaveAttribute("role", "status");
    for (const b of screen.getAllByRole("button", { name: /^Find gaps for/ })) expect(b).toBeDisabled(); // one run at a time
    await tick();
    expect(await screen.findByText("Starting")).toBeInTheDocument();
    expect(screen.queryByText(queued)).toBeNull();
  });

  it("says why a run could not start, in the server's words, and shows no new run", async () => {
    const why = "Only 4 employers have a posting stored for Role Two; Find Gaps needs at least 5, each from a different employer.";
    serve({ start: () => json({ detail: why }, 422) });
    open();
    fireEvent.click(await on("Find gaps for Role Two"));
    expect(await screen.findByText(why)).toBeInTheDocument();
  });

  it("starts a run for a typed title, and its results offer Add as a target role, which makes a Search Settings tab with that title", async () => {
    const found = run({ id: "r2", role_id: null, title: "Head of Projects", kind: "new", can_repeat: false });
    const server = serve({ runs: { r1: [run()], r2: [found] } });
    open();
    fireEvent.change(await screen.findByLabelText("Try a new role"), { target: { value: "  Head of Projects " } });
    fireEvent.click(await on("Find gaps for a new role"));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0].body).toEqual({ title: "Head of Projects" });
    expect(await screen.findByRole("heading", { level: 2, name: "Head of Projects" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New sample" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add as a target role" }));
    await waitFor(() => expect(server.sent).toHaveLength(2));
    const put = server.sent[1];
    expect(put.url).toBe("/profile/criteria");
    const body = put.body as { data: { company: string; target_roles: { id: string; name: string; titles: string[]; description: string }[] }; why: string; base_version: number };
    expect(body.base_version).toBe(7);
    expect(body.why).toBe("Added from Find Gaps");
    expect(body.data.target_roles).toHaveLength(2);
    expect(body.data.target_roles[0]).toEqual({ id: "role-a", name: "Role One" });
    expect(body.data.target_roles[1]).toMatchObject({ id: "head-of-projects", name: "Head of Projects", titles: [], description: "" });
    expect(body.data.company).toBe("A short company line.");
    expect(await screen.findByRole("button", { name: "Added as a target role" })).toBeDisabled();
  });
});

describe("Find Gaps: what the owner says of a theme", () => {
  it("offers three answers on a Transferable or Unproven theme and none on a Proven one", async () => {
    serve();
    open();
    await openTheme("Projects at scale");
    expect(screen.queryByRole("radio")).toBeNull();
    await openTheme("Data catalog tooling");
    expect(screen.getAllByRole("radio").map((r) => r.closest("label")?.textContent?.trim())).toEqual(["I've done this", "I haven't", "Not relevant"]);
    expect(screen.getByRole("radio", { name: "I've done this" })).toBeChecked();
    expect(screen.getByText("Have you done this?")).toBeInTheDocument();
  });

  it("takes the owner's words, drafts a line in a box they can rewrite, and adds it to the chosen job as Evidence by default", async () => {
    const server = serve();
    open();
    await openTheme("Data catalog tooling");
    expect(screen.getByRole("button", { name: "Draft the line" })).toBeDisabled();
    const words = "At my last job I picked and rolled out a data catalog for finance and about 40 reports.";
    fireEvent.change(theBox("What you did, in your own words"), { target: { value: words } });
    fireEvent.click(await on("Draft the line"));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0]).toEqual({ method: "POST", url: "/find-gaps/runs/r1/themes/data-catalog/draft", body: { words } });
    expect(await screen.findByText("Waiting for an agent")).toBeInTheDocument();
    await tick();
    await tick();
    const box = await screen.findByRole("textbox", { name: "The draft line, to edit as you like" });
    expect(box).toHaveValue(LINE);
    fireEvent.change(box, { target: { value: "Rolled out a data catalog for finance." } });
    expect(screen.getByRole("combobox", { name: "Which job it belongs to" })).toHaveValue("lead");
    expect(within(screen.getByRole("combobox", { name: "Which job it belongs to" })).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Senior Manager, Northwind Labs", "Engineer, Fabrikam",
    ]);
    expect(screen.getByRole("radio", { name: /^Evidence/ })).toBeChecked();
    fireEvent.change(screen.getByRole("combobox", { name: "Which job it belongs to" }), { target: { value: "eng" } });
    fireEvent.click(screen.getByRole("button", { name: "Add to resume" }));
    await waitFor(() => expect(server.sent).toHaveLength(2));
    const put = server.sent[1];
    expect(put.url).toBe("/profile/resume");
    const saved = put.body as { data: Documents["resume"]; why: string; base_version: number };
    expect(saved.why).toBe("Added from Find Gaps");
    expect(saved.base_version).toBe(3);
    expect(saved.data.roles[1].evidence).toEqual([{ id: "data-catalog-tooling", title: "Data catalog tooling", text: "Rolled out a data catalog for finance." }]);
    expect(saved.data.roles[1].bullets).toEqual([]);
    expect(saved.data.roles[0]).toEqual(DOCS.resume.roles[0]);
    expect({ ...saved.data, roles: [] }).toEqual({ ...DOCS.resume, roles: [] });
    expect(await screen.findByText("Added to your resume")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "The draft line, to edit as you like" })).toBeNull();
  });

  it("adds a Bullet, to the job chosen, when Bullet is chosen", async () => {
    const server = serve();
    open();
    await openTheme("Governance and risk");
    await draftLine("I led the audit remediation for three entities.");
    fireEvent.click(screen.getByRole("radio", { name: /^Bullet/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add to resume" }));
    await waitFor(() => expect(server.sent.filter((s) => s.method === "PUT")).toHaveLength(1));
    const saved = server.sent.find((s) => s.method === "PUT")?.body as { data: Documents["resume"] };
    expect(saved.data.roles[0].bullets).toHaveLength(2);
    expect(saved.data.roles[0].bullets[1]).toMatchObject({ text: LINE, note: "", group: "" });
    expect(saved.data.roles[0].evidence).toHaveLength(1);
  });

  it("says in the server's words when the resume was changed elsewhere, and keeps the draft", async () => {
    const stale = "This page is out of date: it was changed elsewhere since you opened it. Reload to see the latest, then make your change again.";
    serve({ saveResume: () => json({ detail: stale }, 409) });
    open();
    await openTheme("Data catalog tooling");
    await draftLine("I picked and rolled out a catalog.");
    fireEvent.click(screen.getByRole("button", { name: "Add to resume" }));
    expect(await screen.findByText(stale)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "The draft line, to edit as you like" })).toHaveValue(LINE);
  });

  it("says why a line could not be drafted and keeps the owner's words", async () => {
    serve({ drafts: [{ id: "d1", state: "failed", step: "Stopped", error: "No agent picked this up within 5 minutes. Try again in a moment, or write the line yourself." }] });
    open();
    await openTheme("Data catalog tooling");
    const words = "I picked and rolled out a catalog.";
    fireEvent.change(theBox("What you did, in your own words"), { target: { value: words } });
    fireEvent.click(await on("Draft the line"));
    expect(await screen.findByText(/No agent picked this up within 5 minutes/)).toBeInTheDocument();
    expect(theBox("What you did, in your own words")).toHaveValue(words);
    expect(screen.queryByRole("textbox", { name: "The draft line, to edit as you like" })).toBeNull();
  });

  it("says only that it didn't finish when a draft failed with no reason, was lost or came back empty, and lets the owner ask again", async () => {
    for (const options of [
      { drafts: [{ id: "d1", state: "failed" as const, step: "Stopped" }] },
      { draftGone: true },
      { drafts: [{ id: "d1", state: "done" as const, step: "Done" }] },
    ]) {
      serve(options);
      const view = open();
      await openTheme("Data catalog tooling");
      fireEvent.change(theBox("What you did, in your own words"), { target: { value: "I picked and rolled out a catalog." } });
      fireEvent.click(await on("Draft the line"));
      expect(await screen.findByText("That didn't finish.")).toBeInTheDocument();
      expect(await on("Draft the line")).toBeEnabled();
      view.unmount();
    }
  });

  it("keeps the draft button off while no agent is free, with why", async () => {
    const why = "Claude's peak hours: no Claude work until 5:00 PM.";
    serve({ ready: { ready: false, why } });
    open();
    await openTheme("Data catalog tooling");
    fireEvent.change(theBox("What you did, in your own words"), { target: { value: "I picked and rolled out a catalog." } });
    const draft = screen.getByRole("button", { name: "Draft the line" });
    expect(draft).toBeDisabled();
    expect(draft).toHaveAttribute("title", why);
  });

  it("marks the theme a real gap with I haven't, writes nothing to the resume, and takes the mark off again", async () => {
    const server = serve();
    open();
    await openTheme("Data catalog tooling");
    fireEvent.click(screen.getByRole("radio", { name: "I haven't" }));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0]).toEqual({ method: "PUT", url: "/find-gaps/runs/r1/themes/data-catalog/answer", body: { answer: "not_done" } });
    expect(await screen.findByText("A real gap: you said you haven’t done this")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "I haven't" })).toBeChecked();
    expect(screen.queryByRole("textbox", { name: "What you did, in your own words" })).toBeNull();
    expect(server.sent.some((s) => s.url.startsWith("/profile"))).toBe(false);
    fireEvent.click(screen.getByRole("radio", { name: "I've done this" }));
    await waitFor(() => expect(server.sent).toHaveLength(2));
    expect(server.sent[1]).toEqual({ method: "DELETE", url: "/find-gaps/answers/ans1", body: undefined });
    await waitFor(() => expect(screen.queryByText(/A real gap/)).toBeNull());
  });

  it("hides a theme that is not relevant, says how many are hidden, and brings one back", async () => {
    const server = serve();
    open();
    await openTheme("Data catalog tooling");
    expect(screen.queryByRole("button", { name: /hidden$/ })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Not relevant" }));
    await waitFor(() => expect(server.sent).toHaveLength(1));
    expect(server.sent[0]).toEqual({ method: "PUT", url: "/find-gaps/runs/r1/themes/data-catalog/answer", body: { answer: "not_relevant" } });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Data catalog tooling" })).toBeNull());
    expect(server.sent.some((s) => s.url.startsWith("/profile"))).toBe(false);
    fireEvent.click(await screen.findByRole("button", { name: "1 hidden" }));
    expect(screen.getByText("Data catalog tooling")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Bring back" }));
    await waitFor(() => expect(server.sent).toHaveLength(2));
    expect(server.sent[1]).toEqual({ method: "DELETE", url: "/find-gaps/answers/ans2", body: undefined });
    expect(await screen.findByRole("button", { name: "Data catalog tooling" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /hidden$/ })).toBeNull();
  });
});

describe("Find Gaps: starting from a job", () => {
  const JOB_DASH: GapDashboard = {
    ...DASH,
    job_runs: [{ id: "r9", company: "Anchor Co", title: "Role One", at: NOW }],
  };

  const JOBS = [{ id: "j1", company: "Anchor Co", title: "Sample Title A" }, { id: "j2", company: "Beta Co", title: "Sample Title B" }];

  it("reads the picker's roles the first time it is opened, not with the dashboard, and only once", async () => {
    const { jobReads } = serve({ dash: JOB_DASH, jobs: JOBS });
    open();
    const picker = await screen.findByLabelText("Start from a job");
    await waitFor(() => expect(screen.getByRole("button", { name: "Find gaps for Role One" })).toBeEnabled());
    expect(jobReads()).toBe(0);
    fireEvent.focus(picker);
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(3));
    fireEvent.mouseDown(picker);
    fireEvent.focus(picker);
    expect(jobReads()).toBe(1);
  });

  it("lists the roles in a picker with a Find gaps button that is off until one is picked, and starts a run for the job", async () => {
    const { sent } = serve({ dash: JOB_DASH, jobs: JOBS });
    open();
    const picker = await screen.findByLabelText("Start from a job");
    fireEvent.focus(picker);
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(3));
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Pick one of your roles", "Anchor Co, Sample Title A", "Beta Co, Sample Title B",
    ]);
    const button = screen.getByRole("button", { name: "Find gaps from a job" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Find gaps for Role One" })).toBeEnabled());
    expect(button).toBeDisabled();
    fireEvent.change(picker, { target: { value: "j2" } });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(sent.filter((s) => s.method === "POST")).toEqual([{ method: "POST", url: "/find-gaps/runs", body: { opportunity_id: "j2" } }]));
  });

  it("lists the finished runs started from a job, each to open again", async () => {
    serve({ dash: JOB_DASH, runs: { r9: [run({ id: "r9", role_id: null, title: "Role One", job: { id: "j1", company: "Anchor Co", title: "Role One" }, can_repeat: false })] } });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Anchor Co, Role One" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Role One" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add as a target role" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  });
});
