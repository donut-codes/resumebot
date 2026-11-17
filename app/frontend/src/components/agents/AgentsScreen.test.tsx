import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { AgentDetail, AgentKind, AgentRunner, AgentSummary, TrailEntry } from "../../api/types/agents";
import AgentsScreen from "./AgentsScreen";
import { json, render, stubFetch } from "../../test-utils";
import { howProcessRoutes } from "./howBpmn.fixture";
import { OVERVIEW } from "./how.fixture";
import { fakeLive, pollTimers } from "../../test-live";

const MINUTES_AGO = (n: number) => new Date(Date.now() - n * 60_000).toISOString();

function summary(overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    connection_id: "a1",
    name: "The scout",
    description: "Searches the job boards for roles that fit you",
    status: "working",
    paused: false,
    session_state: "live",
    checked_in_at: MINUTES_AGO(0.2),
    current: { task_title: "Search for roles", project_title: "Job search", started_at: "2026-09-30T21:41:00+00:00" },
    last_seen_at: MINUTES_AGO(6),
    declared_at: "2026-09-30T21:00:00+00:00",
    roles: ["scout"],
    roles_words: "Scout",
    availability: "On a task",
    model_words: "A model for each kind of work",
    capacity: { free: false, busy: 1, slots: 1, words: "Busy: 1 of 1 slots", since: null },
    runners: [],
    ...overrides,
  };
}

/** The runners of an install with lanes, as the app lists them: the search and a researcher at work, the other
 * researcher and the owner's runner between sessions, and the writer gone quiet. */
function lanes(): AgentRunner[] {
  return [
    { name: "search", label: "Search", state: "working", kind: "reader", task: "Reading postings", since: MINUTES_AGO(4), seen_at: MINUTES_AGO(0.5) },
    { name: "analysis-1", label: "Analysis 1", state: "working", kind: "analyst", task: "Researching Office Lead at Fabrikam", since: MINUTES_AGO(2), seen_at: MINUTES_AGO(0.5) },
    { name: "analysis-2", label: "Analysis 2", state: "idle", kind: null, task: "Idle", since: "2026-10-13T09:40:00+00:00", seen_at: MINUTES_AGO(1) },
    { name: "owner", label: "Owner", state: "idle", kind: null, task: "Idle", since: "2026-10-13T09:40:00+00:00", seen_at: MINUTES_AGO(1) },
    { name: "writer", label: "Writer", state: "not_seen", kind: null, task: null, since: "2026-10-13T08:15:00+00:00", seen_at: "2026-10-13T08:15:00+00:00" },
  ];
}

function detail(overrides: Partial<AgentDetail> = {}): AgentDetail {
  return {
    ...summary(),
    activity: [{ at: "2026-09-30T21:42:00+00:00", text: "Reported 12 roles", detail: null }],
    may_do: {
      summary: "Can search and report roles, and nothing else.",
      rules: [{ kind: "jobs.report", title: "Report roles it found", rule: "Always allowed" }],
    },
    ...overrides,
  };
}

function entry(overrides: Partial<TrailEntry> = {}): TrailEntry {
  return {
    id: "t1", at: "2026-10-01T12:00:00+00:00", who: "The scout", actor: "agent", event: "search_done",
    sentence: "Reported 12 roles.", mark: "on", details: null, link: null, ...overrides,
  };
}

const KINDS: AgentKind[] = [
  { kind: "learner", label: "Site setup", purpose: "Works out how each site is read.", doing: [], paused: true, waiting: 1, instructions: [] },
  { kind: "researcher", label: "Researcher", purpose: "Researches each role and its employer.", doing: [{ session: "analysis-1", task: "Researching Office Lead at Fabrikam" }], paused: false, waiting: 3, instructions: [{ kind_of_file: "instructions", name: "research.md" }, { kind_of_file: "templates", name: "brief.md" }] },
  { kind: "writer", label: "Writer", purpose: "Writes the tailored resume and cover letter for a role.", doing: [], paused: false, waiting: 0, instructions: [] },
  { kind: "title_checker", label: "Title checker", purpose: "Reads place, pay and way of working from a role's header and tags it with a target role.", doing: [], paused: false, waiting: 0, instructions: [] },
  { kind: "search", label: "Search", purpose: "Searches the job boards for new roles.", doing: [], paused: false, waiting: 0, instructions: [{ kind_of_file: "instructions", name: "search.md" }] },
];

interface Call { method: string; url: string; body: unknown }

function renderScreen(
  list: AgentSummary[],
  details: Record<string, AgentDetail>,
  trail: TrailEntry[] = [],
  hash = "#/agents",
  sessions: { budget?: number | null } = {},
) {
  const calls: Call[] = [];
  stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/agent-kinds/learner/run")) return json({ kind: "learner", queued: 0, held: "this kind is paused" });
    if (url.endsWith("/agent-kinds/sessions")) return json({ count: 1, max: 5, ...sessions });
    if (url.includes("/agent-kinds")) return method === "GET" ? json(KINDS) : json({});
    if (method === "PATCH") {
      const id = Object.keys(details).find((k) => url.includes(`/agents/${k}`))!;
      return json({ ...details[id], ...JSON.parse(String(init!.body)) });
    }
    if (url.includes("/trail")) return json({ items: trail, next_before: null });
    const edit = howProcessRoutes(url);
    if (edit) return edit;
    if (url.includes("/harness")) return json(OVERVIEW);
    for (const [id, agentDetail] of Object.entries(details)) {
      if (url.includes(`/agents/${id}`)) return json(agentDetail);
    }
    if (url.includes("/agents")) return json(list);
    return json({}, 404);
  });
  render(<AgentsScreen />, { hash });
  return calls;
}

/** The open agent's own head, so its Pause is not mistaken for a kind's. */
async function agentHead(): Promise<HTMLElement> {
  return (await screen.findByText(/./, { selector: ".agent-panel-name" })).closest(".agent-panel-head") as HTMLElement;
}

/** One agent's row of the roster. The kinds list above the roster has words of its own (a kind named "Writer", a kind
 * that is "Paused") and its answer lands in its own time, so what a test says of an agent it asks of that agent's row. */
async function rosterRow(name: string): Promise<HTMLElement> {
  return (await screen.findByText(name, { selector: ".agent-roster-name" })).closest(".agent-roster-item") as HTMLElement;
}

describe("AgentsScreen", () => {
  it("lists the agents with their role in words, and opens the one working", async () => {
    renderScreen([summary(), summary({ connection_id: "a2", name: "The writer", status: "idle", roles: ["writer"], roles_words: "Writer", availability: "Free", current: null })], {
      a1: detail(),
    });
    expect(await screen.findByText("The scout", { selector: ".agent-roster-name" })).toBeInTheDocument();
    expect(screen.getByText("The writer", { selector: ".agent-roster-name" })).toBeInTheDocument();
    // A role nobody coded for shows as the server says it, in its agent's row: no frontend change for a Writer.
    expect(within(await rosterRow("The writer")).getByText("Writer")).toBeInTheDocument();
    expect(within(await rosterRow("The scout")).getByText("Scout")).toBeInTheDocument();
    expect(screen.getAllByText("A model for each kind of work", { selector: ".agent-roster-model" }).length).toBeGreaterThan(0);
    // The working agent is open, with what it is doing.
    expect(await screen.findByText("Searches the job boards for roles that fit you")).toBeInTheDocument();
    expect(screen.getByText("Can search and report roles, and nothing else.")).toBeInTheDocument();
    expect(screen.getByText("Busy: 1 of 1 slots")).toBeInTheDocument();
  });

  it("shows its current work with the word Job search and a link to the pipeline, not to a project", async () => {
    renderScreen([summary()], { a1: detail() });
    const now = (await screen.findByText("Search for roles", { selector: ".agent-now-title" })).closest(".agent-now") as HTMLElement;
    expect(within(now).getByText(/Job search/)).toBeInTheDocument();
    expect(within(now).getByRole("link", { name: "Open the board" })).toHaveAttribute("href", "#/board");
    expect(screen.queryByRole("button", { name: "Open project" })).toBeNull();
  });

  it("says when a held task has gone quiet", async () => {
    const stuck = summary({
      current: { task_title: "Search for roles", project_title: "Job search", started_at: "2026-09-30T21:41:00+00:00", stuck_since: new Date().toISOString() },
    });
    renderScreen([stuck], { a1: detail({ current: stuck.current }) });
    expect(await screen.findByText(/Stuck since/)).toBeInTheDocument();
  });

  it("says plainly when it hasn't checked in", async () => {
    const off = summary({ status: "offline", availability: "Offline", session_state: null, current: null, last_seen_at: null });
    renderScreen([off], { a1: detail({ ...off }) });
    expect(await screen.findByText("Hasn't checked in yet")).toBeInTheDocument();
  });

  it("has an Activity tab and a Settings tab, and no Skills tab", async () => {
    renderScreen([summary()], { a1: detail() }, [entry()]);
    // The rail names the parts: the tabs are found in the screen's rail.
    const tabs = within(await screen.findByRole("navigation", { name: "Agent Settings" }));
    expect(tabs.getByRole("link", { name: "Activity", current: "page" })).toBeInTheDocument();
    expect(tabs.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "#/agents/a1/settings");
    expect(screen.queryByRole("button", { name: "Skills" })).toBeNull();
    expect(await screen.findByText("Reported 12 roles.")).toBeInTheDocument();
    fireEvent.click(tabs.getByRole("link", { name: "Settings" }));
    expect(await screen.findByText("What it may do")).toBeInTheDocument();
    expect(screen.getByText("Report roles it found")).toBeInTheDocument();
    expect(within(document.querySelector(".agent-manage") as HTMLElement).getByText("A model for each kind of work")).toBeInTheDocument();
    // Nothing to edit there: no fields, no token, no wake address, no passcode, no Connect an agent.
    expect(document.querySelector("input")).toBeNull();
    expect(screen.queryByText(/connect an agent|passcode|token|wake/i)).toBeNull();
  });

  it("Pause stops it being handed work, and Resume gives that back", async () => {
    const calls = renderScreen([summary()], { a1: detail() });
    fireEvent.click(within(await agentHead()).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toBe("/api/v1/agents/a1");
    expect(patch.body).toEqual({ paused: true });
  });

  it("a paused agent says so and offers Resume", async () => {
    renderScreen([summary({ paused: true })], { a1: detail({ paused: true }) });
    expect(within(await agentHead()).getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByText(/it won't be handed any work/)).toBeInTheDocument();
    expect(within(await rosterRow("The scout")).getByText("Paused")).toBeInTheDocument();
  });

  it("an activity line for a search offers 'What was sent', which opens How they work on it", async () => {
    renderScreen([summary()], { a1: detail() }, [entry({ sent_task_id: "r9", link: { kind: "search_run", id: "r9" } })]);
    fireEvent.click(await screen.findByRole("button", { name: "What was sent" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents/how?sent=r9"));
  });

  it("an activity line about a search leads to the pipeline", async () => {
    renderScreen([summary()], { a1: detail() }, [entry({ sent_task_id: "r9", link: { kind: "search_run", id: "r9" } })]);
    fireEvent.click(await screen.findByRole("button", { name: "See the search" }));
    expect(window.location.hash).toBe("#/board");
  });

  it("follows the address: picking an agent or a tab is the new address", async () => {
    renderScreen(
      [summary(), summary({ connection_id: "a2", name: "The writer", status: "idle", roles_words: "Writer", current: null })],
      { a1: detail(), a2: detail({ connection_id: "a2", name: "The writer", status: "idle", current: null }) },
      [],
      "#/agents/a2/settings",
    );
    expect(await screen.findByText("What it may do")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings", current: "page" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Activity" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents/a2/activity"));
    fireEvent.click(screen.getByText("The scout", { selector: ".agent-roster-name" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents/a1"));
  });

  it("offers Activity, Settings, How they work and Missions, and How they work opens the process", async () => {
    renderScreen([summary()], { a1: detail() });
    const strip = within(await screen.findByRole("navigation", { name: "Agent Settings" }));
    expect(strip.getByRole("link", { name: "Activity", current: "page" })).toBeInTheDocument();
    expect(strip.getByRole("link", { name: "Missions" })).toHaveAttribute("href", "#/missions");
    fireEvent.click(strip.getByRole("link", { name: "How they work" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents/how"));
    expect(await screen.findByRole("heading", { level: 2, name: "Steps" })).toBeInTheDocument();
  });

  it("with How they work open, it draws the process and not the roster", async () => {
    renderScreen([summary()], { a1: detail() }, [], "#/agents/how");
    expect(await screen.findByRole("heading", { level: 2, name: "Steps" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "How they work", current: "page" })).toBeInTheDocument();
    expect(document.querySelector(".agent-roster")).toBeNull();
  });

  it("says there are no agents yet, and keeps the page's words about the job search", async () => {
    renderScreen([], {});
    expect(await screen.findByText("No agents yet.")).toBeInTheDocument();
    expect(screen.getByText(/working on your job search/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/goal|project/i);
  });

  it("shows the message when the roster can't load", async () => {
    stubFetch(() => json({ detail: "The server is down." }, 500));
    render(<AgentsScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is down.");
  });
});

describe("AgentsScreen, in the Focus Inbox design", () => {
  it("lists each agent as a row marked with its robot's role, with where it stands and when it was last seen", async () => {
    renderScreen(
      [
        summary(),
        summary({ connection_id: "a2", name: "The analyst", status: "idle", roles: ["analyst"], roles_words: "Analyst", availability: "Free", current: null, last_seen_at: null }),
        summary({ connection_id: "a3", name: "The writer", status: "offline", roles: ["writer"], roles_words: "Writer", availability: "Offline", current: null, last_seen_at: null }),
      ],
      { a1: detail() },
    );
    await screen.findByText("The scout", { selector: ".agent-roster-name" });
    const rows = Array.from(document.querySelectorAll<HTMLElement>(".agent-roster-item"));
    expect(rows.map((r) => r.dataset.role)).toEqual(["scout", "analyst", "writer"]);
    // A badge says it in words, with the ink as well: working green, asleep plain, gone quiet oxblood.
    expect(within(rows[0]).getByText("On a task")).toHaveAttribute("data-ink", "green");
    expect(within(rows[1]).getByText("Free")).not.toHaveAttribute("data-ink");
    expect(within(rows[2]).getByText("Offline")).toHaveAttribute("data-ink", "oxblood");
    expect(within(rows[0]).getByText("1 of 1")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Never")).toBeInTheDocument();
    expect(within(rows[0]).getByText("6 minutes ago")).toBeInTheDocument();
    // The agent open is marked, and its panel carries the same role.
    expect(within(rows[0]).getByRole("button")).toHaveAttribute("aria-current", "true");
    expect((await screen.findByRole("heading", { level: 2, name: "The scout" })).closest(".agent-panel")).toHaveAttribute("data-role", "scout");
  });

  it("opens the agent that checked in last, comparing instants and not their text across offsets", async () => {
    // As text "...T01:00:00+00:00" sorts after "...T23:00:00-05:00"; as instants the second is three hours later.
    renderScreen(
      [
        summary({ connection_id: "a1", name: "The scout", status: "idle", current: null, last_seen_at: "2026-10-02T01:00:00+00:00" }),
        summary({ connection_id: "a2", name: "The analyst", status: "idle", roles: ["analyst"], roles_words: "Analyst", current: null, last_seen_at: "2026-10-01T23:00:00-05:00" }),
      ],
      { a1: detail(), a2: detail() },
    );
    const rows = await screen.findAllByRole("button", { name: /The (scout|analyst)/ });
    expect(rows.find((r) => r.getAttribute("aria-current") === "true")).toHaveTextContent("The analyst");
  });

  it("opens with the roster and the panel's blocks: Doing now with a teal dot while it works", async () => {
    renderScreen([summary()], { a1: detail() });
    const now = await screen.findByRole("heading", { level: 3, name: "Doing now" });
    expect(now.closest(".fi-block")).toHaveAttribute("data-ink", "green");
    expect(screen.getByRole("heading", { level: 2, name: /^Roster/ })).toBeInTheDocument();
    // The rail names the part: it has no heading of its own (one heading layer, not a rail entry and a heading that say the same).
    expect(screen.queryByRole("heading", { level: 3, name: "Activity" })).toBeNull();
    expect(screen.getByRole("region", { name: "Activity" })).toHaveAttribute("id", "agent-activity");
    fireEvent.click(within(screen.getByRole("navigation", { name: "Agent Settings" })).getByRole("link", { name: "Settings" }));
    expect(await screen.findByRole("region", { name: "Settings" })).toHaveAttribute("id", "agent-settings");
    expect(screen.queryByRole("heading", { level: 3, name: "Settings" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Activity" })).toBeNull();
  });

  it("an agent with several kinds of work tells each once under its own label, and what none of them does once", async () => {
    const NEVER = "Never applies, fills a form or sends anything.";
    const may_do = {
      summary: NEVER,
      rules: [
        { kind: "scout", title: "Scout", rule: "Learns how to read each site." },
        { kind: "analyst", title: "Analyst", rule: "Researches a found role." },
        { kind: "writer", title: "Writer", rule: "Writes a role's application packet." },
      ],
    };
    renderScreen([summary()], { a1: detail({ may_do }) });
    const kinds = await screen.findByLabelText("Its kinds of work");
    expect(Array.from(kinds.querySelectorAll("dt")).map((d) => d.textContent)).toEqual(["Scout", "Analyst", "Writer"]);
    expect(Array.from(kinds.querySelectorAll("dd")).map((d) => d.textContent)).toEqual([
      "Learns how to read each site.", "Researches a found role.", "Writes a role's application packet.",
    ]);
    expect(screen.getAllByText(NEVER)).toHaveLength(1);
    expect(document.querySelector(".agent-panel-head")!.textContent!.match(/Never/g)).toHaveLength(1);
  });

  it("an agent with one kind of work shows no labelled list", async () => {
    renderScreen([summary()], { a1: detail() });
    await screen.findByText("Can search and report roles, and nothing else.");
    expect(document.querySelector(".agent-kinds")).toBeNull();
  });

  it("tells a paused agent in the sun's ink", async () => {
    renderScreen([summary({ paused: true })], { a1: detail({ paused: true }) });
    expect(within(await rosterRow("The scout")).getByText("Paused", { selector: ".fi-state" })).toHaveAttribute("data-ink", "saffron");
    expect((await screen.findByRole("heading", { level: 3, name: "Doing now" })).closest(".fi-block")).toHaveAttribute("data-ink", "saffron");
  });

  it("a stuck task marks its block red", async () => {
    const stuck = summary({
      current: { task_title: "Search for roles", project_title: "Job search", started_at: "2026-09-30T21:41:00+00:00", stuck_since: new Date().toISOString() },
    });
    renderScreen([stuck], { a1: detail({ current: stuck.current }) });
    expect((await screen.findByRole("heading", { level: 3, name: "Doing now" })).closest(".fi-block")).toHaveAttribute("data-ink", "oxblood");
  });

  it("an agent with nothing to do says so, under a plain grey-dot block", async () => {
    const idle = summary({ status: "idle", availability: "Free", current: null, capacity: { free: true, busy: 0, slots: 1, words: "Free: 0 of 1 slots", since: null } });
    renderScreen([idle], { a1: detail({ ...idle }) });
    expect(await screen.findByText("Nothing right now.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Doing now" }).closest(".fi-block")).toHaveAttribute("data-ink", "graphite");
  });

  describe("an agent with several runners (an install with lanes)", () => {
    const capacity = { free: true, busy: 2, slots: 4, words: "2 of 4 at work", since: null };

    it("tells the sessions as one line under its roster row and in Doing now", async () => {
      renderScreen([summary({ runners: lanes(), capacity })], { a1: detail({ runners: lanes(), capacity }) });
      expect(await screen.findByText("2 of 5 sessions working", { selector: ".agent-roster-sessions" })).toBeInTheDocument();
      expect(await screen.findByText("2 of 5 sessions working", { selector: ".agent-now-title" })).toBeInTheDocument();
      expect(screen.queryByRole("list", { name: "Runner sessions" })).toBeNull();
      const row = screen.getByText("The scout", { selector: ".agent-roster-name" }).closest(".agent-roster-item") as HTMLElement;
      expect(within(row).getByText("2 of 4")).toBeInTheDocument();
      expect(screen.getByText("2 of 4 at work")).toBeInTheDocument();
      expect(screen.getAllByRole("link", { name: "Open the board" })).toHaveLength(1);
    });

    it("still tells a task held with no sign of life, above the runners", async () => {
      const stuck = summary({
        runners: lanes(), capacity,
        current: { task_title: "Searching for roles", project_title: "Job search", started_at: "2026-09-30T21:41:00+00:00", stuck_since: new Date().toISOString() },
      });
      renderScreen([stuck], { a1: detail({ ...stuck }) });
      expect(await screen.findByText(/Stuck since/)).toBeInTheDocument();
      expect(await screen.findByText("2 of 5 sessions working", { selector: ".agent-now-title" })).toBeInTheDocument();
      expect(screen.getAllByRole("link", { name: "Open the board" })).toHaveLength(1);
    });

    it("an agent with one runner is told as it always was: no runner lists", async () => {
      renderScreen([summary()], { a1: detail() });
      await screen.findByText("Search for roles", { selector: ".agent-now-title" });
      expect(screen.queryByRole("list", { name: "Runner sessions" })).toBeNull();
      expect(screen.queryByText(/sessions working/)).toBeNull();
    });
  });
});

describe("the agents by kind", () => {
  it("has a row per kind in order, with what it is doing, what waits, and its instruction links", async () => {
    renderScreen([summary()], { a1: detail() });
    const rows = within(await screen.findByRole("list", { name: "Agents by kind" })).getAllByRole("listitem", { hidden: false }).filter((li) => li.classList.contains("agent-kind-row"));
    expect(rows.map((r) => r.querySelector(".agent-kind-name")!.textContent)).toEqual([
      "Site setup", "Researcher", "Writer", "Title checker", "Search",
    ]);
    const researcher = rows[1];
    expect(within(researcher).getByText("Researches each role and its employer.")).toBeInTheDocument();
    expect(within(researcher).getByText("Researching Office Lead at Fabrikam")).toBeInTheDocument();
    expect(within(researcher).getByText("3 waiting")).toBeInTheDocument();
    // One link to How they work, never a file name (the files are edited there).
    expect(within(researcher).getByRole("link", { name: "How the agents work" })).toHaveAttribute("href", "#/agents/how");
    expect(within(researcher).queryByText("research.md")).toBeNull();
    expect(within(researcher).queryByText("brief.md")).toBeNull();
    expect(within(rows[4]).getByText("Idle")).toBeInTheDocument();
    expect(within(rows[4]).getByText("Searches the job boards for new roles.")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Paused")).toBeInTheDocument();
  });

  it("lists the code checker with what it is doing, and with no Run now or Pause", async () => {
    const code: AgentKind = { kind: "code_checker", label: "Code checker", purpose: "The app's own code reads postings.", doing: [{ session: "code-ab12cd34", task: "finding the original posting of a role on a job board" }], paused: false, waiting: 0, instructions: [] };
    KINDS.push(code);
    try {
      renderScreen([summary()], { a1: detail() });
      const list = await screen.findByRole("list", { name: "Agents by kind" });
      const row = within(list).getByText("Code checker", { selector: ".agent-kind-name" }).closest("li") as HTMLElement;
      expect(within(row).getByText("finding the original posting of a role on a job board")).toBeInTheDocument();
      expect(within(row).queryByRole("button")).toBeNull();
    } finally {
      KINDS.pop();
    }
  });

  it("Run now, Pause and Resume call the kind's endpoints", async () => {
    const calls = renderScreen([summary()], { a1: detail() });
    const list = await screen.findByRole("list", { name: "Agents by kind" });
    const row = (name: string) => within(list).getByText(name, { selector: ".agent-kind-name" }).closest("li") as HTMLElement;
    fireEvent.click(within(row("Researcher")).getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/agent-kinds/researcher/run"))).toBe(true));
    fireEvent.click(within(row("Researcher")).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/agent-kinds/researcher/pause"))).toBe(true));
    fireEvent.click(within(row("Site setup")).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/agent-kinds/learner/resume"))).toBe(true));
  });

  it("shows why Run now queued nothing", async () => {
    renderScreen([summary()], { a1: detail() });
    const list = await screen.findByRole("list", { name: "Agents by kind" });
    const row = within(list).getByText("Site setup", { selector: ".agent-kind-name" }).closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Run now" }));
    expect(await within(row).findByText("Held: this kind is paused")).toBeInTheDocument();
  });

  it("Run all and Pause all call the all endpoints", async () => {
    const calls = renderScreen([summary()], { a1: detail() });
    fireEvent.click(await screen.findByRole("button", { name: "Run all" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/agent-kinds/run-all"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Pause all" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/agent-kinds/pause-all"))).toBe(true));
  });

  it("sets how many sessions may work at once", async () => {
    const calls = renderScreen([summary()], { a1: detail() });
    const select = await screen.findByRole("combobox", { name: /^Sessions at once \(daytime\)/ });
    expect(screen.getByText(/Your off hours use all \d\./)).toBeInTheDocument();
    expect(select).toHaveValue("1");
    fireEvent.change(select, { target: { value: "3" } });
    await waitFor(() =>
      expect(calls.some((c) => c.method === "PUT" && c.url.endsWith("/agent-kinds/sessions") && JSON.stringify(c.body) === '{"count":3}')).toBe(true),
    );
  });

  it("shows the nightly budget blank for no limit, and saves a number and a blank again", async () => {
    const calls = renderScreen([summary()], { a1: detail() }, [], "#/agents", { budget: null });
    const field = await screen.findByRole("spinbutton", { name: "Nightly budget" });
    expect(field).toHaveValue(null);
    fireEvent.change(field, { target: { value: "25" } });
    fireEvent.blur(field);
    const put = (body: string) =>
      calls.some((c) => c.method === "PUT" && c.url.endsWith("/agent-kinds/sessions") && JSON.stringify(c.body) === body);
    await waitFor(() => expect(put('{"budget":25}')).toBe(true));
  });

  it("saves null when the nightly budget is cleared", async () => {
    const calls = renderScreen([summary()], { a1: detail() }, [], "#/agents", { budget: 25 });
    const field = await screen.findByRole("spinbutton", { name: "Nightly budget" });
    expect(field).toHaveValue(25);
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.blur(field);
    await waitFor(() =>
      expect(calls.some((c) => c.method === "PUT" && c.url.endsWith("/agent-kinds/sessions") && JSON.stringify(c.body) === '{"budget":null}')).toBe(true),
    );
  });
});

describe("AgentsScreen, kept fresh by the live stream", () => {
  let live: ReturnType<typeof fakeLive>;
  beforeEach(() => {
    live = fakeLive();
  });
  afterEach(() => live.restore());

  const reads = (calls: Call[], tail: RegExp) => calls.filter((c) => c.method === "GET" && tail.test(c.url)).length;

  it("reads the roster and the kinds again when the server says agents changed, and sets no timer", async () => {
    const timers = pollTimers();
    const list = [summary()];
    const calls = renderScreen(list, { a1: detail() });
    expect(await screen.findByText("The scout", { selector: ".agent-roster-name" })).toBeInTheDocument();
    await waitFor(() => expect(reads(calls, /\/agent-kinds$/)).toBeGreaterThan(0));
    // Counts are taken as they stand once both have loaded, so the test names no number of reads the screen makes at open.
    const rosterBefore = reads(calls, /\/agents$/);
    const kindsBefore = reads(calls, /\/agent-kinds$/);
    list[0] = summary({ name: "The renamed scout" });
    await live.changed("missions");
    expect(reads(calls, /\/agents$/)).toBe(rosterBefore);
    expect(reads(calls, /\/agent-kinds$/)).toBe(kindsBefore);
    await live.changed("agents", ["a1"]);
    expect(await screen.findByText("The renamed scout", { selector: ".agent-roster-name" })).toBeInTheDocument();
    await waitFor(() => expect(reads(calls, /\/agents$/)).toBeGreaterThan(rosterBefore));
    await waitFor(() => expect(reads(calls, /\/agent-kinds$/)).toBeGreaterThan(kindsBefore));
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });
});
