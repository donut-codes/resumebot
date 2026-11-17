import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Mission, MissionDetail, MissionNumber } from "../../api/types/agents";
import { json, render, stubFetch } from "../../test-utils";
import MissionsScreen from "./MissionsScreen";
import { fakeLive, pollTimers } from "../../test-live";

// The playbook is How they work's own view, tested there.
vi.mock("../agents/HowTheyWork", () => ({ default: ({ only }: { only: string }) => <div>Playbook {only}</div> }));


const RUN = {
  id: "run1", started_by: "you", started_at: "2026-10-07T02:00:00+00:00", ended_at: null, outcome: "running" as const,
  summary: "Asked for 2 packets: Projects Lead at Initech, Projects Lead at Umbrella.", search_run: null,
  instances: ["i1", "i2"], progress: { done: 1, target: 2 }, stopped: null,
};

// What the server's registry says each setting is (backend missions/numbers.py): the screen draws whatever it is told.
const num = (name: string, label: string, help: string, rest: Partial<MissionNumber> = {}): MissionNumber => ({
  name, label, help, kind: "whole", low: 0, high: 100, unit: "", optional: false, unlimited: false, value: 0, ...rest,
});

// The nightly limit on sites to learn takes -1: no limit (backend missions/numbers.py `unlimited`).
const SITES = num("site_projects_per_night", "Sites to learn a night",
  "How many of the sites you switched on the scout starts learning in a night. -1 for no limit.", { low: 1, high: 100, unlimited: true, value: 3 });
// A number that can be left blank: a blank one is sent as none.
const DAYS_BETWEEN = num("days_between_employer_applications", "Days between applications to the same employer",
  "Launch in Browser skips a role at an employer you applied to within this many days.", { low: 1, high: 3650, unit: " days", optional: true, value: 14 });
// The queue's five numbers (backend missions/numbers.py): none can be left blank, each already holds its default.
const QUEUE = [
  num("stale_days", "A posting is stale after", "How many days old a posting can be before it is stale.",
    { low: 1, high: 3650, unit: " days", value: 30 }),
  num("stale_penalty", "A stale posting loses", "The points a stale posting loses from its score in the order.",
    { low: 0, high: 100, unit: " points", value: 21 }),
  num("golden_hours", "A posting found in the last", "How many hours a posting counts as new.",
    { low: 0, high: 8760, unit: " hours", value: 72 }),
  num("golden_bonus", "A recent posting gains", "The points a new posting gains in the order.",
    { low: 0, high: 100, unit: " points", value: 20 }),
  num("research_start_multiple", "Start new research when First Impression holds", "New reads wait until it holds this many times the Second Impression size.",
    { low: 0, high: 100, unit: " × the Second Impression size", value: 3 }),
];
const EXAMPLES = num("classifier_min_examples", "Examples before it learns",
  "Once you've kept this many roles, the app learns your taste and puts the likeliest new postings first. It only changes the order: no posting is hidden or dropped.",
  { low: 1, high: 10000, value: 20 });
// No setting on a mission is a share of something any more, but the form still draws one when the server lists it.
const SHARE = num("a_share", "A share", "A made-up share, from 0 to 1.", { kind: "fraction", low: 0, high: 1, value: 0.05 });

const PEAK_NUMBERS: MissionNumber[] = [
  num("avoid_peak", "Avoid Claude's peak hours", "When this is checked, no Claude work happens in the window.",
    { kind: "flag", low: null, high: null, value: true }),
  num("peak_start", "Peak hours start", "A time of day, in the time zone below.", { kind: "time", low: null, high: null, value: "05:00" }),
  num("peak_end", "Peak hours end", "A time of day, in the time zone below.", { kind: "time", low: null, high: null, value: "11:00" }),
  num("peak_zone", "Time zone", "The zone the peak hours are read in.", { kind: "zone", low: null, high: null, value: "America/Los_Angeles" }),
];

const PEAK = { avoid: true, window: "8:00 AM to 2:00 PM", start: "05:00", end: "11:00", zone: "America/Los_Angeles",
  now: false, resumes_words: null, numbers: PEAK_NUMBERS };

const LIST: Mission[] = [
  { id: "s", key: "scout_boards", name: "Scout the boards", purpose: "Search the boards.", playbook: "jobsearch_round",
    needs_claude: true, agent: "scout", runs_elsewhere: false, done_when: "20 matches · at most 4 searches, or every site searched · by 06:00",
    schedule: { kind: "every", hours: 24 }, schedule_words: "every day", settings: { focus: "", keep_target: 20, max_sessions: 4, finish_by: "06:00" }, state: "on",
    paused_until: null, checked_at: null, next_run_at: "2026-10-08T12:00:00+00:00", last_run: null, numbers: [SITES] },
  { id: "f", key: "open_apply_tabs", name: "Open my apply tabs", purpose: "Open the day's tabs.", playbook: "jobsearch_go",
    needs_claude: false, agent: null, runs_elsewhere: false, done_when: null,
    schedule: { kind: "daily", at: "02:00" }, schedule_words: "every day at 02:00",
    settings: {},
    state: "paused", paused_until: null, checked_at: null, next_run_at: null, last_run: RUN, numbers: [DAYS_BETWEEN, ...QUEUE] },
];

const FORGE: MissionDetail = {
  ...LIST[1], runs: [RUN],
  changes: [{ at: "2026-10-06T12:00:00+00:00", by: "resumebot", what: "Made it, paused, every day at 02:00.", why: "one of the missions every install starts with" }],
};

function serve() {
  const calls: { url: string; method: string; body: unknown }[] = [];
  stubFetch((input: string, init?: RequestInit) => {
    calls.push({ url: input, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (input.endsWith("/api/v1/missions/numbers")) return json({ saved: true, version: 2, peak: PEAK });
    if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
    if (input.endsWith("/api/v1/missions/f/run")) return json(RUN);
    if (input.includes("/api/v1/missions/f")) return json(FORGE);
    return json({}, 404);
  });
  return calls;
}

describe("MissionsScreen", () => {
  afterEach(() => vi.useRealTimers());

  it("lists every mission with its schedule, next run, last run and state", async () => {
    serve();
    render(<MissionsScreen />, { hash: "#/missions" });
    await screen.findByRole("list", { name: "Missions" });
    // The page index above the list is a list too: the missions are the list named for them.
    const [scout, forge] = within(screen.getByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(screen.queryByRole("region", { name: "The mission" })).toBeNull();
    expect(scout).toHaveTextContent("Scout the boards");
    expect(scout).toHaveTextContent("every day");
    expect(scout).toHaveTextContent("never ran");
    expect(within(scout).getByText("On")).toBeInTheDocument();
    expect(within(forge).getByText("Paused")).toBeInTheDocument();
    expect(within(forge).getByText("Running")).toBeInTheDocument();
    // A state is a word in its ink: on is green, paused and running are saffron; a robot's mission carries its robot.
    expect(within(scout).getByText("On")).toHaveAttribute("data-ink", "green");
    expect(within(forge).getByText("Paused")).toHaveAttribute("data-ink", "saffron");
    expect(within(forge).getByText("Running")).toHaveAttribute("data-ink", "saffron");
    expect(scout).toHaveAttribute("data-agent", "scout");
    expect(forge).not.toHaveAttribute("data-agent"); // Go is the app's own work: no robot
    expect(within(forge).getByRole("link", { name: "Open my apply tabs" })).toHaveAttribute("href", "#/missions/f");
    expect(scout).toHaveTextContent("Done when20 matches · at most 4 searches, or every site searched · by 06:00");
    expect(forge).toHaveTextContent("1 of 2");
    // The numbers each runs with, in one line, on the mission they govern.
    expect(scout).toHaveTextContent("Runs withSites to learn a night 3");
    expect(forge).toHaveTextContent(
      "Runs withDays between applications to the same employer 14 days · A posting is stale after 30 days · A stale posting loses 21 points");
  });

  it("tells a pause that runs until a time as a sentence with its ink's dot, not as a badge", async () => {
    stubFetch((input: string) => {
      const until = { ...LIST[1], paused_until: "2026-10-09T12:00:00+00:00" };
      if (input.endsWith("/api/v1/missions")) return json({ missions: [LIST[0], until], peak: PEAK });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions" });
    const [scout, forge] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    // "On" is a word: a badge. "Paused until Oct 9, 5:00 AM" is a sentence: ordinary words, a sun dot.
    expect(within(scout).getByText("On")).toHaveClass("fi-state");
    const paused = forge.querySelector(".mission-state")!;
    expect(paused.textContent).toMatch(/^Paused until /);
    expect(paused).toHaveClass("fi-state-words");
    expect(paused).not.toHaveClass("fi-state");
    expect(paused).toHaveAttribute("data-ink", "saffron");
  });

  it("writes a mission's next run on the owner's clock, not the browser's", async () => {
    // A day in the year of the run is written without its year (lib/dates.ts dateInWords), so the test names its own
    // today: only Date is replaced, the timers the page and the queries wait on stay real.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-07T12:00:00Z"));
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [LIST[0]], peak: PEAK });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions" });
    const [scout] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    // 12:00 UTC on Oct 8 is 02:00 on Oct 9 at UTC+14.
    expect(scout).toHaveTextContent("NextOctober 9, 2:00 am");
  });

  it("reads the time typed to pause until as the owner's, and names the zone beside the box", async () => {
    const calls: { url: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ url: input, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: [LIST[0]], peak: PEAK });
      if (input.includes("/api/v1/missions/s")) return json({ ...LIST[0], runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const box = await within(panel).findByLabelText(/^Pause until, .+ \(blank: until you resume it\)$/);
    fireEvent.change(box, { target: { value: "2099-01-15T10:30" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Pause until then" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/pause"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/pause"))!.body).toMatchObject({ until: "2099-01-14T20:30:00.000Z" });
  });

  it("shows no numbers on a mission that has none", async () => {
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [{ ...LIST[0], key: "sweep_stuck", needs_claude: false, agent: null, done_when: null, numbers: [] }], peak: PEAK });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions" });
    const [sweep] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(sweep).not.toHaveTextContent("Runs with");
    // The app's own work has no robot to carry.
    expect(sweep).not.toHaveAttribute("data-agent");
  });

  it("keeps Run now on in the blackout for Read the mail, whose reading and moving use no Claude, and shows its days of mail", async () => {
    const days = num("email_lookback_days", "Days of mail to look at", "How many days back each read of your mail looks.",
      { kind: "whole", low: 1, high: 60, value: 3 });
    const mail = { ...LIST[1], id: "m", key: "read_mail", name: "Read the mail", playbook: "jobsearch_read_mail",
      agent: "analyst" as const, done_when: "your mailbox, read-only · the mail since the last read",
      settings: {}, state: "on" as const, numbers: [days] };
    stubFetch((input: string) => {
      const inPeak = { ...PEAK, now: true, resumes_words: "2:00 PM" };
      if (input.endsWith("/api/v1/missions")) return json({ missions: [mail, ...LIST], peak: inPeak });
      if (input.includes("/api/v1/missions/m")) return json({ ...FORGE, ...mail, runs: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/m" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    expect(await within(panel).findByRole("button", { name: "Run now" })).toBeEnabled();
    expect(screen.getByText(/your mailbox, read-only/)).toBeInTheDocument();
    expect(within(panel).getByLabelText("Days of mail to look at")).toHaveValue(3);
    expect(within(panel).queryByRole("button", { name: "Save done conditions" })).not.toBeInTheDocument();
  });

  it("turns Run now off in the blackout and says how to run anyway", async () => {
    stubFetch((input: string) => {
      const inPeak = { ...PEAK, now: true, resumes_words: "2:00 PM" };
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: inPeak });
      if (input.includes("/api/v1/missions/s")) return json({ ...LIST[0], runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    expect(await within(panel).findByRole("button", { name: "Run now" })).toBeDisabled();
    expect(panel).toHaveTextContent("Run now is off until 2:00 PM. To run it now, edit Claude’s peak hours above and uncheck “Avoid Claude’s peak hours”.");
    expect(screen.getByText("Peak hours now: no Claude work until 2:00 PM.")).toBeInTheDocument();
  });

  it("keeps Run now on in the blackout for the read, which uses no Claude, and says what it does", async () => {
    const read = { ...LIST[1], id: "r", key: "read_boards", name: "Read the employers' boards", playbook: null,
      done_when: "every known employer's board · up to 150 descriptions read",
      settings: { details_per_night: 150 }, state: "on" as const, numbers: [EXAMPLES] };
    stubFetch((input: string) => {
      const inPeak = { ...PEAK, now: true, resumes_words: "2:00 PM" };
      if (input.endsWith("/api/v1/missions")) return json({ missions: [read, ...LIST], peak: inPeak });
      if (input.includes("/api/v1/missions/r")) return json({ ...FORGE, ...read, runs: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/r" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    expect(await within(panel).findByRole("button", { name: "Run now" })).toBeEnabled();
    expect(panel).not.toHaveTextContent("Run now is off");
    expect(screen.getByText(/every known employer's board · up to 150 descriptions read/)).toBeInTheDocument();
    // The classifier's one number runs in the board read, so it is on it. The classifier stops nothing now: there is no
    // bar it stops under and no share of the stopped it double-checks.
    const examples = within(panel).getByLabelText("Examples before it learns");
    expect(examples).toHaveValue(20);
    expect(examples).toHaveAttribute("step", "1");
    expect(examples).toHaveAttribute("max", "10000");
    expect(within(panel).queryByLabelText("Stopped under")).toBeNull();
    expect(within(panel).queryByLabelText("Trimmed ones double-checked")).toBeNull();
    expect(panel.closest("li")).toHaveTextContent("Runs withExamples before it learns 20");
  });

  it("draws a setting that is a share as a number from 0 to 1, a twentieth at a time", async () => {
    const share = { ...LIST[0], numbers: [SHARE] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [share, LIST[1]], peak: PEAK });
      if (input.includes("/api/v1/missions/s")) return json({ ...share, runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const field = await within(panel).findByLabelText("A share");
    expect(field).toHaveValue(0.05);
    expect(field).toHaveAttribute("step", "0.05");
    expect(field).toHaveAttribute("max", "1");
  });

  it("names a run Claude started through the action door as Claude's, not as on schedule", async () => {
    const claude = { ...RUN, id: "k1", started_by: "claude", outcome: "done" as const, progress: null, instances: [],
      summary: "Swept up 2 stuck runs." };
    const sweep: Mission = { id: "w", key: "sweep_stuck", name: "Sweep up stuck runs", purpose: "Close what got stuck.",
      playbook: null, schedule: { kind: "every", hours: 1 }, schedule_words: "every hour", settings: {}, state: "on",
      needs_claude: false, agent: null, runs_elsewhere: false, done_when: null,
      paused_until: null, checked_at: null, next_run_at: null, last_run: claude, numbers: [] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [sweep, ...LIST], peak: PEAK });
      if (input.includes("/api/v1/missions/w")) return json({ ...sweep, runs: [claude], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/w" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const run = await within(panel).findByText(/Claude, on demand/);
    expect(run.textContent).not.toMatch(/on schedule/);
  });

  it("shows Read in your browser as a mission the owner's computer starts: no Run now, no schedule, how it last went, and how to set it up", async () => {
    const run = { ...RUN, id: "b1", started_by: "browser", outcome: "done" as const, progress: null, instances: [],
      summary: "Handed in 87 postings, 41 new. Couldn't read Example Jobs: it asked to sign in." };
    const browser: Mission = { id: "b", key: "read_browser", name: "Read in your browser", purpose: "Read the sites a server can't.",
      playbook: null, schedule: { kind: "outside" }, schedule_words: "when your computer's scheduled session calls in",
      needs_claude: true, agent: null, runs_elsewhere: true, done_when: "every posting handed in · every new one opened for its description",
      settings: {}, state: "paused", paused_until: null, checked_at: null,
      next_run_at: null, last_run: run, numbers: [] };
    const calls: { method: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: [browser, ...LIST], peak: PEAK });
      if (input.includes("/api/v1/missions/b")) return json({ ...browser, runs: [run], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/b" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const setup = await within(panel).findByRole("region", { name: "Set up on your computer" });
    const item = panel.closest("li")!;
    expect(item).toHaveTextContent("when your computer's scheduled session calls in");
    expect(item).toHaveTextContent("every posting handed in · every new one opened for its description");
    expect(item).toHaveTextContent("Handed in 87 postings, 41 new. Couldn't read Example Jobs: it asked to sign in.");
    expect(within(item).queryByText("Next")).toBeNull();
    expect(within(panel).queryByRole("button", { name: "Run now" })).toBeNull();
    expect(panel).toHaveTextContent(/· your computer/);
    // The steps name the command, never a file, a folder or an address that may not be the engine's.
    expect(setup).toHaveTextContent("resumebot connect");
    expect(setup.textContent).not.toMatch(/browser-token|browser-reader|mcp-browser/);
    expect(setup).toHaveTextContent("Resume this mission here");
    // The server sends it no settings and its time is set on the owner's computer: nothing here to change.
    expect(within(panel).queryByRole("button", { name: "Change schedule or done conditions" })).toBeNull();
    expect(within(panel).queryByLabelText("Postings to hand in a run")).toBeNull();
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
  });

  it("shows the catch-up after an import in Analyse the finds' history, labelled so, with how far it got", async () => {
    const catchUp = { ...RUN, id: "c1", started_by: "import", instances: ["i9"], progress: { done: 12, target: 148 },
      summary: "Researched 12 of 148 roles. Paused: Claude's usage limit, until 3:00 PM." };
    const analyse: Mission = { id: "a", key: "analyse_finds", name: "Analyse the finds", purpose: "Research the best Found roles.",
      playbook: "jobsearch_analyse", schedule: { kind: "after", mission: "scout_boards" }, schedule_words: "when Scout the boards finishes",
      needs_claude: true, agent: "analyst", runs_elsewhere: false, done_when: "3 roles researched · by 06:00",
      settings: { target: 3, finish_by: "06:00" }, state: "on", paused_until: null, checked_at: null, next_run_at: null,
      last_run: catchUp, numbers: [] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [analyse, ...LIST], peak: PEAK });
      if (input.includes("/api/v1/missions/a")) return json({ ...analyse, runs: [catchUp], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/a" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    // The run is labelled as the catch-up, not "on schedule", and says how many it has researched and why it waits.
    const run = await within(panel).findByText(/the catch-up after an import/);
    expect(run.textContent).not.toMatch(/on schedule/);
    expect(panel).toHaveTextContent("Researched 12 of 148 roles. Paused: Claude's usage limit, until 3:00 PM.");
    expect(panel.closest("li")).toHaveTextContent("12 of 148");
  });

  it("has Claude's peak hours at the top as one section, with the window in the owner's time", async () => {
    const calls = serve();
    render(<MissionsScreen />, { hash: "#/missions" });
    const section = (await screen.findByRole("heading", { name: "Claude’s peak hours" })).closest("section") as HTMLElement;
    expect(section).toHaveTextContent("Applies to every mission that uses Claude. The window, in your time: 8:00 AM to 2:00 PM.");
    expect(within(section).queryByLabelText("Avoid Claude's peak hours")).toBeNull();  // folded until Edit
    fireEvent.click(within(section).getByRole("button", { name: "Edit" }));
    const box = within(section).getByLabelText("Avoid Claude's peak hours");
    expect(box).toBeChecked();
    expect(within(section).getByLabelText("Peak hours start")).toHaveValue("05:00");
    expect(within(section).getByLabelText("Peak hours end")).toHaveValue("11:00");
    expect(within(section).getByLabelText("Time zone")).toHaveValue("America/Los_Angeles");
    expect(within(section).getByText("When this is checked, no Claude work happens in the window.")).toBeInTheDocument();
    fireEvent.click(box);
    fireEvent.change(within(section).getByLabelText("Peak hours end"), { target: { value: "12:30" } });
    fireEvent.change(within(section).getByLabelText("Time zone"), { target: { value: "Europe/Paris" } });
    fireEvent.change(within(section).getByLabelText(/^Why/), { target: { value: "working a late shift" } });
    fireEvent.click(within(section).getByRole("button", { name: "Save peak hours" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")).toBeDefined());
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({
      url: expect.stringMatching(/\/api\/v1\/missions\/numbers$/),
      body: {
        values: { avoid_peak: false, peak_start: "05:00", peak_end: "12:30", peak_zone: "Europe/Paris" },
        why: "working a late shift",
      },
    });
  });

  it("each mission's numbers are on its open panel, one plain line each, and save together with why", async () => {
    const calls = serve();
    render(<MissionsScreen />, { hash: "#/missions/f" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const form = await within(panel).findByRole("form", { name: "Open my apply tabs settings" });
    expect(within(form).getByLabelText("Days between applications to the same employer")).toHaveValue(14);
    expect(within(form).getAllByText("days")).toHaveLength(2); // "Days between applications" and "A posting is stale after"
    expect(within(form).getByText("Launch in Browser skips a role at an employer you applied to within this many days.")).toBeInTheDocument();
    // The queue's numbers read as the owner's sentences: "A posting is stale after [30] days", and so on.
    expect(within(form).getByLabelText("A posting is stale after")).toHaveValue(30);
    expect(within(form).getByLabelText("A stale posting loses")).toHaveValue(21);
    expect(within(form).getByLabelText("A posting found in the last")).toHaveValue(72);
    expect(within(form).getByLabelText("A recent posting gains")).toHaveValue(20);
    expect(within(form).getByLabelText("Start new research when First Impression holds")).toHaveValue(3);
    expect(within(form).getByText("× the Second Impression size")).toBeInTheDocument();
    expect(within(form).getByText("hours")).toBeInTheDocument();
    expect(within(form).getAllByText("points")).toHaveLength(2);
    expect(within(form).queryByLabelText("Apply queue at a time")).not.toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("Days between applications to the same employer"), { target: { value: "21" } });
    fireEvent.change(within(form).getByLabelText("A posting is stale after"), { target: { value: "45" } });
    fireEvent.change(within(form).getByLabelText("Start new research when First Impression holds"), { target: { value: "2" } });
    fireEvent.change(within(form).getByLabelText(/^Why/), { target: { value: "fewer, better" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")).toBeDefined());
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({
      values: { days_between_employer_applications: 21, stale_days: 45, stale_penalty: 21, golden_hours: 72, golden_bonus: 20,
        research_start_multiple: 2 }, why: "fewer, better" });
  });


  it("sends a blank number as none, and shows what the server refuses", async () => {
    const calls: { body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      if (input.endsWith("/api/v1/missions/numbers")) {
        calls.push({ body: JSON.parse(String(init?.body)) });
        return json({ detail: "Days between applications to the same employer: 1 to 3650 days" }, 422);
      }
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
      if (input.includes("/api/v1/missions/f")) return json(FORGE);
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/f" });
    const form = await screen.findByRole("form", { name: "Open my apply tabs settings" });
    fireEvent.change(within(form).getByLabelText("Days between applications to the same employer"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText("Days between applications to the same employer: 1 to 3650 days")).toBeInTheDocument();
    expect(calls[0].body).toEqual({ values: { days_between_employer_applications: null, stale_days: 30, stale_penalty: 21, golden_hours: 72, golden_bonus: 20,
      research_start_multiple: 3 }, why: "" });
  });

  it("opens one below its line: run now, resume, change the schedule with why, its runs, playbook and history", async () => {
    const calls = serve();
    render(<MissionsScreen />, { hash: "#/missions/f" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    await within(panel).findByRole("button", { name: "Run now" });
    expect(panel.closest("li")).toHaveTextContent("Open my apply tabs");
    expect(screen.getByRole("link", { name: "Open my apply tabs" })).toHaveAttribute("href", "#/missions");
    expect(within(panel).getByText("Playbook jobsearch_go")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Run 1" })).toHaveAttribute("href", "#/agents/how?live=i1");
    expect(within(panel).getByText("Why: one of the missions every install starts with")).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/missions/f/run"))).toBe(true));
    fireEvent.click(within(panel).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/missions/f/resume"))).toBe(true));
    fireEvent.click(within(panel).getByRole("button", { name: "Change schedule or done conditions" }));
    fireEvent.change(within(panel).getByLabelText("Time of day"), { target: { value: "03:30" } });
    fireEvent.change(within(panel).getAllByLabelText(/Why/)[0], { target: { value: "later" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ schedule: { kind: "daily", at: "03:30" }, why: "later" });
    fireEvent.click(within(panel).getByRole("button", { name: "Fold up" }));
    expect(window.location.hash).toBe("#/missions");
  });

  it("shows the morning apply tabs as on, with an editable time and no done conditions", async () => {
    const tabs: MissionDetail = {
      ...LIST[1], id: "t", key: "open_apply_tabs", name: "Open my apply tabs", purpose: "Open the batch.", playbook: null,
      schedule: { kind: "daily", at: "06:30" }, schedule_words: "every day at 06:30", settings: {}, state: "on",
      next_run_at: "2026-10-09T13:30:00+00:00", last_run: null, numbers: [], runs: [], changes: [],
    };
    const calls: { url: string; method: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ url: input, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: [tabs], peak: PEAK });
      if (input.includes("/api/v1/missions/t")) return json(tabs);
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/t" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    expect(panel.closest("li")).toHaveTextContent("Open my apply tabs");
    expect(panel.closest("li")).toHaveTextContent("every day at 06:30");
    expect(await within(panel).findByRole("button", { name: "Pause" })).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Change schedule or done conditions" }));
    expect(within(panel).queryByRole("button", { name: "Save done conditions" })).toBeNull();
    expect(within(panel).getByLabelText("Time of day")).toHaveValue("06:30");
    fireEvent.change(within(panel).getByLabelText("Time of day"), { target: { value: "07:00" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ schedule: { kind: "daily", at: "07:00" }, why: "" });
  });

  it("calls the scout's target the number of matches and says what counts", async () => {
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
      if (input.includes("/api/v1/missions/s")) return json({ ...LIST[0], runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    fireEvent.click(await within(panel).findByRole("button", { name: "Change schedule or done conditions" }));
    expect(within(panel).getByLabelText("Number of matches")).toHaveValue(20);
    expect(within(panel).queryByLabelText("New roles to keep")).toBeNull();
    expect(within(panel).getByText(/roles that pass your gates and reach Ready to Launch/)).toBeInTheDocument();
  });

  it("says a scout night is done when every site is searched once its search limits are -1, and shows the limits that are none", async () => {
    const none = { ...LIST[0], settings: { focus: "", keep_target: -1, max_sessions: -1, finish_by: "06:00" },
      done_when: "every site searched · by 06:00", numbers: [{ ...SITES, value: -1 }] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [none, LIST[1]], peak: PEAK });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions" });
    const [scout] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(scout).toHaveTextContent("Done whenevery site searched · by 06:00");
    expect(scout).toHaveTextContent("Runs withSites to learn a night: no limit");
    expect(scout).not.toHaveTextContent("-1");
  });

  it("tells each other mission's night in words once its limit is -1, and a run with no limit shows no progress bar", async () => {
    const base = { ...LIST[1], state: "on" as const, last_run: null, numbers: [] };
    const missions: Mission[] = [
      { ...base, id: "a", key: "analyse_finds", name: "Analyse the finds", settings: { target: -1, finish_by: "06:00" },
        needs_claude: true, agent: "analyst" as const, done_when: "every role on deck researched · by 06:00" },
      { ...base, id: "r", key: "read_boards", name: "Read the employers' boards", playbook: null, settings: { details_per_night: -1 },
        done_when: "every known employer's board · every description read" },
      { ...base, id: "b", key: "read_browser", name: "Read in your browser", playbook: null, schedule: { kind: "outside" },
        schedule_words: "when your computer's scheduled session calls in", settings: {}, needs_claude: true,
        runs_elsewhere: true, done_when: "every posting handed in · every new one opened for its description",
        last_run: { ...RUN, outcome: "done" as const, progress: null, summary: "Handed in 87 postings." } },
    ];
    stubFetch((input: string) => (input.endsWith("/api/v1/missions") ? json({ missions, peak: PEAK }) : json({}, 404)));
    render(<MissionsScreen />, { hash: "#/missions" });
    const [analyse, read, browser] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(analyse).toHaveTextContent("Done whenevery role on deck researched · by 06:00");
    expect(read).toHaveTextContent("Done whenevery known employer's board · every description read");
    expect(browser).toHaveTextContent("Done whenevery posting handed in · every new one opened for its description");
    for (const li of [analyse, read, browser]) expect(li).not.toHaveTextContent("-1");
  });

  it("draws no done-condition field for a mission the server sent no settings for, and none for its schedule when its time is set elsewhere", async () => {
    const browser: Mission = { ...LIST[0], id: "b", key: "read_browser", name: "Read in your browser", playbook: null,
      schedule: { kind: "outside" }, schedule_words: "when your computer's scheduled session calls in", settings: {},
      needs_claude: true, agent: null, runs_elsewhere: true, state: "paused", numbers: [] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [browser], peak: PEAK });
      if (input.includes("/api/v1/missions/b")) return json({ ...browser, runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/b" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    await within(panel).findByRole("region", { name: "Set up on your computer" });
    expect(within(panel).queryByRole("button", { name: "Change schedule or done conditions" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "Save done conditions" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "Run now" })).toBeNull();
  });

  it("draws a Finish by only for a mission whose settings the server sent one for: the read has none to keep", async () => {
    const read: Mission = { ...LIST[1], id: "r", key: "read_boards", name: "Read the employers' boards", playbook: null,
      settings: { details_per_night: 150 }, state: "on", numbers: [], done_when: "every known employer's board · up to 150 descriptions read" };
    const scoutOpen = { ...LIST[0], runs: [], changes: [] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: [read, LIST[0]], peak: PEAK });
      if (input.includes("/api/v1/missions/r")) return json({ ...read, runs: [], changes: [] });
      if (input.includes("/api/v1/missions/s")) return json(scoutOpen);
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/r" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    fireEvent.click(await within(panel).findByRole("button", { name: "Change schedule or done conditions" }));
    expect(within(panel).getByLabelText("Descriptions to read a night")).toHaveValue(150);
    expect(within(panel).queryByLabelText("Finish by (blank: no limit)")).toBeNull();
  });

  it("keeps Run now on in the blackout for any mission the server says needs no Claude, whatever its key", async () => {
    const own: Mission = { ...LIST[0], id: "o", key: "sweep_stuck", name: "Sweep up stuck runs", needs_claude: false, agent: null,
      done_when: null, settings: {}, numbers: [], playbook: null };
    stubFetch((input: string) => {
      const inPeak = { ...PEAK, now: true, resumes_words: "2:00 PM" };
      if (input.endsWith("/api/v1/missions")) return json({ missions: [own, LIST[0]], peak: inPeak });
      if (input.includes("/api/v1/missions/o")) return json({ ...own, runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/o" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    expect(await within(panel).findByRole("button", { name: "Run now" })).toBeEnabled();
    expect(panel).not.toHaveTextContent("Run now is off");
  });

  it("takes the robot and the done words from what the server sent, not from the mission's key", async () => {
    const odd: Mission = { ...LIST[0], key: "sweep_stuck", agent: "analyst", done_when: "whatever the server says", numbers: [] };
    const none: Mission = { ...LIST[0], id: "n", key: "scout_boards", agent: null, done_when: null, numbers: [] };
    stubFetch((input: string) => (input.endsWith("/api/v1/missions") ? json({ missions: [odd, none], peak: PEAK }) : json({}, 404)));
    render(<MissionsScreen />, { hash: "#/missions" });
    const [first, second] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(first).toHaveAttribute("data-agent", "analyst");
    expect(first).toHaveTextContent("Done whenwhatever the server says");
    expect(second).not.toHaveAttribute("data-agent");
    expect(second).not.toHaveTextContent("Done when");
  });

  it("shows a running read's own words for its progress, and done of target when it has none", async () => {
    const base = { ...LIST[1], state: "on" as const, numbers: [] };
    const missions: Mission[] = [
      { ...base, id: "r", key: "read_boards", name: "Read the employers' boards", playbook: null,
        last_run: { ...RUN, outcome: "running" as const, progress: { done: 180, target: 282, text: "Sites 180 of 282 · job texts 404 of 1,252" } } },
      { ...base, id: "a", key: "analyse_finds", name: "Analyse the finds", needs_claude: true, agent: "analyst" as const,
        last_run: { ...RUN, outcome: "running" as const, progress: { done: 1, target: 2 } } },
    ];
    stubFetch((input: string) => (input.endsWith("/api/v1/missions") ? json({ missions, peak: PEAK }) : json({}, 404)));
    render(<MissionsScreen />, { hash: "#/missions" });
    const [read, analyse] = within(await screen.findByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(read).toHaveTextContent("Sites 180 of 282 · job texts 404 of 1,252");
    expect(analyse).toHaveTextContent("1 of 2");
  });

  it("says the employer researcher's night in words, takes -1 for its employers a night but not for its days, and runs after the read", async () => {
    const research: Mission = {
      ...LIST[1], id: "e", key: "research_employers", name: "Research employers", purpose: "Research the employers.",
      playbook: "jobsearch_research_employer", schedule: { kind: "after", mission: "read_boards" },
      schedule_words: "when Read the employers' boards finishes",
      needs_claude: true, agent: "analyst", done_when: "25 employers researched · none again within 90 days · by 06:00",
      settings: { target: 25, again_after_days: 90, finish_by: "06:00" }, state: "paused", last_run: null, numbers: [],
    };
    const calls: { method: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: [research], peak: PEAK });
      if (input.includes("/api/v1/missions/e")) return json({ ...research, runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/e" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const [card] = within(screen.getByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(card).toHaveTextContent("when Read the employers' boards finishes");
    expect(card).toHaveTextContent("Done when25 employers researched · none again within 90 days · by 06:00");
    expect(card).toHaveAttribute("data-agent", "analyst");
    fireEvent.click(await within(panel).findByRole("button", { name: "Change schedule or done conditions" }));
    const form = within(panel).getByRole("button", { name: "Save done conditions" }).closest("form") as HTMLElement;
    expect(within(form).getByLabelText("Employers to research a night")).toHaveAttribute("min", "-1");
    expect(within(form).getByLabelText("Research again after (days)")).toHaveAttribute("min", "1");
    expect(within(form).getByText(/-1 for no limit/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("Employers to research a night"), { target: { value: "-1" } });
    fireEvent.change(within(form).getByLabelText("Research again after (days)"), { target: { value: "30" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save done conditions" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({
      settings: { target: -1, again_after_days: 30, finish_by: "06:00" }, why: "" });
  });

  it("keeps the mission it follows when its schedule is saved again, and says the night with no limit and no days in words", async () => {
    const research: Mission = {
      ...LIST[1], id: "e", key: "research_employers", name: "Research employers", playbook: "jobsearch_research_employer",
      schedule: { kind: "after", mission: "read_boards" }, schedule_words: "when Read the employers' boards finishes",
      needs_claude: true, agent: "analyst", done_when: "every employer not researched lately · none again within 1 day",
      settings: { target: -1, again_after_days: 1, finish_by: "" }, state: "on", last_run: null, numbers: [],
    };
    const calls: { method: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: [research], peak: PEAK });
      if (input.includes("/api/v1/missions/e")) return json({ ...research, runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/e" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const [card] = within(screen.getByRole("list", { name: "Missions" })).getAllByRole("listitem");
    expect(card).toHaveTextContent("Done whenevery employer not researched lately · none again within 1 day");
    expect(card).not.toHaveTextContent("-1");
    fireEvent.click(await within(panel).findByRole("button", { name: "Change schedule or done conditions" }));
    fireEvent.click(within(panel).getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({
      schedule: { kind: "after", mission: "read_boards" }, why: "" });
  });

  it("takes -1 for no limit on every count limit in the done conditions, each with its line saying so, and sends it as it is", async () => {
    const calls: { method: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
      if (input.includes("/api/v1/missions/s")) return json({ ...LIST[0], runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    fireEvent.click(await within(panel).findByRole("button", { name: "Change schedule or done conditions" }));
    const form = within(panel).getByRole("button", { name: "Save done conditions" }).closest("form") as HTMLElement;
    for (const label of ["Number of matches", "Most searches a night"]) {
      expect(within(form).getByLabelText(label)).toHaveAttribute("min", "-1");
    }
    expect(within(form).getAllByText(/-1 for no limit/)).toHaveLength(2);
    fireEvent.change(within(form).getByLabelText("Number of matches"), { target: { value: "-1" } });
    fireEvent.change(within(form).getByLabelText("Most searches a night"), { target: { value: "-1" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save done conditions" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({
      settings: { focus: "", keep_target: -1, max_sessions: -1, finish_by: "06:00" }, why: "" });
  });

  it("lets the sites to learn a night be -1 too: the number takes it, and its own line says so", async () => {
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
      if (input.includes("/api/v1/missions/s")) return json({ ...LIST[0], runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const form = await within(panel).findByRole("form", { name: "Scout the boards settings" });
    expect(within(form).getByLabelText("Sites to learn a night")).toHaveAttribute("min", "-1");
    expect(within(form).getByText(/-1 for no limit/)).toBeInTheDocument();
  });

  it("keeps the lines of a run's account: the boards read, what passed, and what was stopped with its count first", async () => {
    const account = "Read 29 employer boards: 615 new postings.\n2,330 passed the filter.\nStopped:\n2,901 the place";
    const run = { ...RUN, outcome: "done" as const, ended_at: "2026-10-07T03:00:00+00:00", summary: account, progress: null, instances: [] };
    stubFetch((input: string) => {
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
      if (input.includes("/api/v1/missions/f")) return json({ ...FORGE, runs: [run] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/f" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const shown = await within(panel).findByText(/^Read 29 employer boards/);
    expect(shown).toHaveClass("mission-run-summary");
    expect(shown.textContent).toBe(account); // the line breaks are kept (the stylesheet draws them)
  });
});

describe("MissionsScreen: no limit on what is typed", () => {
  // The owner, 2026-10-14: a length limit on what the owner types is not wanted. A made-up text of `n` characters,
  // with spaces, so a cut anywhere shows.
  const words = (n: number) => {
    let out = "";
    for (let i = 1; out.length < n; i++) out += `sentence ${i} says one more thing. `;
    return out.slice(0, n - 1) + "!";
  };

  it("takes any length in the Why beside the numbers and beside the schedule and the done conditions, and sends it whole", async () => {
    const calls = serve();
    render(<MissionsScreen />, { hash: "#/missions/f" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    const form = await within(panel).findByRole("form", { name: "Open my apply tabs settings" });
    const long = words(5000);

    const numbersWhy = within(form).getByLabelText(/^Why/);
    expect(numbersWhy).not.toHaveAttribute("maxlength");
    fireEvent.change(numbersWhy, { target: { value: long } });
    expect(numbersWhy).toHaveValue(long);
    fireEvent.click(within(form).getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")).toBeDefined());
    expect((calls.find((c) => c.method === "PATCH")!.body as { why: string }).why).toBe(long);

    calls.length = 0;
    fireEvent.click(within(panel).getByRole("button", { name: "Change schedule or done conditions" }));
    const whys = within(panel).getAllByLabelText(/Why/); // the schedule's and the numbers' (Go has no done conditions)
    expect(whys).toHaveLength(2);
    for (const box of whys) expect(box).not.toHaveAttribute("maxlength");
    fireEvent.change(whys[0], { target: { value: long } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect((calls.find((c) => c.method === "PATCH")!.body as { why: string }).why).toBe(long);
  });

  it("takes any length in the scout's Focus and sends it whole", async () => {
    const calls: { method: string; body: unknown }[] = [];
    stubFetch((input: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (input.endsWith("/api/v1/missions")) return json({ missions: LIST, peak: PEAK });
      if (input.includes("/api/v1/missions/s")) return json({ ...LIST[0], runs: [], changes: [] });
      return json({}, 404);
    });
    render(<MissionsScreen />, { hash: "#/missions/s" });
    const panel = await screen.findByRole("region", { name: "The mission" });
    fireEvent.click(await within(panel).findByRole("button", { name: "Change schedule or done conditions" }));
    const focus = within(panel).getByLabelText(/^Focus/);
    expect(focus).not.toHaveAttribute("maxlength");
    const long = words(5000);
    fireEvent.change(focus, { target: { value: long } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save done conditions" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect((calls.find((c) => c.method === "PATCH")!.body as { settings: { focus: string } }).settings.focus).toBe(long);
  });
});

describe("MissionsScreen, kept fresh by the live stream", () => {
  let live: ReturnType<typeof fakeLive>;
  beforeEach(() => {
    live = fakeLive();
  });
  afterEach(() => live.restore());

  const reads = (calls: { url: string; method: string }[], url: string) => calls.filter((c) => c.method === "GET" && c.url === url).length;

  it("reads the list and the open mission again when the server says missions changed, and sets no timer", async () => {
    const timers = pollTimers();
    const calls = serve();
    render(<MissionsScreen />, { hash: "#/missions/f" });
    await screen.findByRole("region", { name: "The mission" });
    await waitFor(() => expect(reads(calls, "/api/v1/missions")).toBeGreaterThan(0));
    await waitFor(() => expect(reads(calls, "/api/v1/missions/f")).toBeGreaterThan(0));
    // Counts are taken as they stand once both have loaded, so the test names no number of reads the screen makes at open.
    const listBefore = reads(calls, "/api/v1/missions");
    const openBefore = reads(calls, "/api/v1/missions/f");
    await live.changed("agents");
    expect(reads(calls, "/api/v1/missions")).toBe(listBefore);
    expect(reads(calls, "/api/v1/missions/f")).toBe(openBefore);
    await live.changed("missions", ["f"]);
    await waitFor(() => expect(reads(calls, "/api/v1/missions")).toBeGreaterThan(listBefore));
    await waitFor(() => expect(reads(calls, "/api/v1/missions/f")).toBeGreaterThan(openBefore));
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });
});
