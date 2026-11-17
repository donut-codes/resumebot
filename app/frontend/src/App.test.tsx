import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "./App";
import { getOwnerZone } from "./lib/dates";
// Agent Settings and its Missions tab are chunks that App loads when first opened (it keeps the process view's xyflow and
// dagre out of the first bundle). Opened for the first time inside a test, that is a cold import of the whole screen:
// about a second when the machine is quiet, several times that when the run's workers share it, charged to whichever
// test opened #/agents first against its 5 seconds. Imported here with the file, the chunks are already loaded when App
// asks for them, and no test pays for the import.
import "./components/agents/AgentsScreen";
import "./components/missions/MissionsScreen";
import { json, ME, settle, stubFetch } from "./test-utils";

/** One of the owner's open tasks, made up. */
function ownerTask(n: number) {
  return {
    id: `t${n}`, title: `Task ${n}`, kind: "decision", state: "open", detail: null, due: null, for: { kind: "owner", name: "You" },
    options: null, recommended: null, answer: null, note: null, link: null, snoozed_until: null, created_at: "2026-10-07T08:00:00Z", done_at: null,
  };
}

/** A fake server: who is here, the board, the owner's open tasks and an empty roster. `count` is how many tasks wait on
 * the owner. */
function stubServer(opts: { count?: number; unread?: number; me?: unknown; meStatus?: number; profile?: boolean; board?: unknown } = {}) {
  const urls: string[] = [];
  stubFetch((input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    if (url.endsWith("/api/v1/me")) return json(opts.me ?? ME, opts.meStatus ?? 200);
    if (opts.profile) {
      // A made-up profile with one role: enough for What to look for to open.
      if (url.endsWith("/api/v1/agent/ready")) return json({ ready: true, why: null });
      if (url.includes("/found")) return json({ total: 0, by_stage: [], items: [] });
      if (url.includes("/history")) return json({ kind: "criteria", versions: [] });
      if (url.split("?")[0].endsWith("/api/v1/employers")) return json({ employers: [], next: null, total: 0, counts: { all: 0, open: 0, watched: 0, hard_pass: 0, local: 0 }, industries: [] });
      if (url.endsWith("/api/v1/profile")) {
        const noWay = { on: false, locations: [] };
        return json({
          criteria: {
            company: "A mid-sized nonprofit.",
            target_roles: [{ id: "builders", name: "Builders", titles: [], description: "", responsibilities: "", qualifications: "", preferred: "", compensation: "" }],
          },
          gates: {
            sections_off: [], location: { remote: noWay, hybrid: noWay, in_person: noWay },
            compensation: { amount: null, against: "ask", wide_band_ratio: 1.5 },
            other: [], by_role: {}, employer_limit: true, employer_names: [], notes: [],
          },
          scoring: { sections_off: [], asks: [], by_role: {}, notes: [] },
          settings: {},
          says: { gates: { location: "", compensation: "", other: {} }, asks: {} },
          versions: { criteria: 1, gates: 1, scoring: 1, settings: 1 },
        });
      }
    }
    if (url.endsWith("/api/v1/favorites")) return json({ key: "favorites", label: "Favorites", count: 0, cards: [] });
    if (url.endsWith("/api/v1/apply/go")) return json({ phase: "idle", total: 0, built: 0, opened: [], skipped: [], notes: [], done: true });
    if (url.includes("/api/v1/board")) return json(opts.board ?? { columns: [] });
    if (url.includes("/api/v1/inbox/history")) return json({ items: [], next_before: null });
    if (url.endsWith("/api/v1/inbox")) {
      return json({
        today: "2026-10-07", count: 0, items: [], notifications: [], unread_count: opts.unread ?? 0,
      });
    }
    if (url.endsWith("/api/v1/missions")) {
      return json({
        missions: [],
        peak: { avoid: false, window: "8:00 AM to 2:00 PM", start: "05:00", end: "11:00", zone: "America/Los_Angeles", now: false,
          resumes_words: null, numbers: [] },
      });
    }
    if (url.endsWith("/api/v1/tasks")) {
      return json({ today: "2026-10-07", for_you: Array.from({ length: opts.count ?? 0 }, (_, i) => ownerTask(i + 1)), for_agents: [], done: [] });
    }
    if (url.includes("/trail")) return json({ items: [], next_before: null });
    if (url.includes("/agents")) return json([]);
    return json({}, 404);
  });
  return urls;
}

/** Notifications is on screen: its rail of parts. */
function notifications() {
  return screen.findByRole("navigation", { name: "Notifications" });
}

/** The Pipeline is on screen: its stages rail (the page has no title of its own). */
function pipeline() {
  return screen.findByRole("navigation", { name: "Stages" });
}

function go(hash: string) {
  window.history.replaceState(null, "", hash || "#");
}

beforeEach(() => go(""));
afterEach(() => go(""));

describe("App", () => {
  it("opens on the Pipeline, whatever the address was, with its tabs and the product's own name", async () => {
    stubServer();
    render(<App />);
    expect(await pipeline()).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Pages" });
    expect(within(nav).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "My Resume", "Search Settings", "Pipeline", "Employers", "Notifications", "Agent Settings",
    ]);
    expect(within(nav).getByRole("button", { name: "Pipeline" })).toHaveAttribute("aria-current", "page");
    // Neither Missions nor Tasks has a place in the bar, and the old names are gone.
    for (const gone of ["Missions", "Tasks", "Me", "Profile", "Inbox", "Agents"]) expect(within(nav).queryByRole("button", { name: gone })).toBeNull();
    expect(screen.getByRole("button", { name: "resumebot home" })).toBeInTheDocument();
  });

  it("takes the owner's time zone from /me before it draws, so every time is written on the owner's clock", async () => {
    stubServer({ me: { version: "1.0.0", timezone: "Pacific/Pago_Pago" } });
    render(<App />);
    await pipeline();
    expect(getOwnerZone()).toBe("Pacific/Pago_Pago");
  });

  it("sets the six pages as tabs on one strip, in one list, after the robot and the name", async () => {
    stubServer();
    render(<App />);
    await pipeline();
    const bar = screen.getByRole("banner");
    const nav = within(bar).getByRole("navigation", { name: "Pages" });
    const lists = nav.querySelectorAll("ul");
    expect(lists).toHaveLength(1);
    expect(Array.from(lists[0].querySelectorAll("li")).map((li) => li.textContent)).toEqual(
      ["My Resume", "Search Settings", "Pipeline", "Employers", "Notifications", "Agent Settings"],
    );
    // The original robot, then the name.
    const home = within(bar).getByRole("button", { name: "resumebot home" });
    expect(home.querySelector("svg")).not.toBeNull();
    expect(home).toHaveTextContent("resumebot");
  });

  it("starts from the address: an empty or unknown one is the Pipeline too, and #/agents opens Agent Settings", async () => {
    stubServer();
    for (const hash of ["", "#/", "#/somewhere/else"]) {
      go(hash);
      const { unmount } = render(<App />);
      expect(await pipeline()).toBeInTheDocument();
      unmount();
    }
    go("#/agents");
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Agent Settings" })).toBeInTheDocument();
  });

  it("keeps the old addresses #/me, #/profile and #/inbox working under their new names", async () => {
    stubServer({ profile: true });
    for (const [hash, title] of [["#/me", "My Resume"], ["#/profile", "Search Settings"], ["#/inbox", "Notifications"]] as const) {
      go(hash);
      const { unmount } = render(<App />);
      // The tab marks the page whatever the page itself is doing (My Resume has nothing to load from this fake server).
      const current = await within(await screen.findByRole("navigation", { name: "Pages" })).findByRole("button", { name: new RegExp(`^${title}`) });
      expect(current).toHaveAttribute("aria-current", "page");
      // The window title names the page too, so browser tabs and screen readers can tell pages apart. App sets it in
      // an effect, which React runs after the commit that put the tab on screen: finding the tab does not wait for it, so
      // wait for the title itself (an earlier iteration's title, or none, is what it reads until then).
      await waitFor(() => expect(document.title).toBe(`${title} · resumebot`));
      unmount();
    }
  });

  it("Missions is a tab of Agent Settings, after Activity, Settings and How they work, and #/missions opens it", async () => {
    stubServer();
    go("#/missions");
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Agent Settings" })).toBeInTheDocument();
    const tabs = screen.getByRole("navigation", { name: "Agent Settings" });
    expect(within(tabs).getAllByRole("link").map((t) => t.textContent)).toEqual(["Activity", "Settings", "How they work", "Missions"]);
    expect(within(tabs).getByRole("link", { name: "Missions" })).toHaveAttribute("aria-current", "page");
    // The Missions page is inside, with its content and no title of its own.
    expect(await screen.findByRole("heading", { level: 2, name: /Claude.s peak hours/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: /The missions/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Missions" })).toBeNull();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    // The bar marks Agent Settings, and the address stays #/missions.
    expect(within(screen.getByRole("navigation", { name: "Pages" })).getByRole("button", { name: "Agent Settings" })).toHaveAttribute("aria-current", "page");
    expect(window.location.hash).toBe("#/missions");
    // The other tabs are one click away, and Missions comes back.
    fireEvent.click(within(tabs).getByRole("link", { name: "Activity" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents"));
    fireEvent.click(await screen.findByRole("link", { name: "Missions" }));
    await waitFor(() => expect(window.location.hash).toBe("#/missions"));
    expect(await screen.findByRole("heading", { level: 2, name: /The missions/ })).toBeInTheDocument();
  });

  it("the tabs switch screens and write the address", async () => {
    stubServer();
    render(<App />);
    await pipeline();
    const nav = screen.getByRole("navigation", { name: "Pages" });
    fireEvent.click(within(nav).getByRole("button", { name: "Notifications" }));
    const rail = await notifications();
    expect(screen.getByRole("heading", { level: 1, name: "Needs you" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/inbox");
    // its parts are in the address too
    fireEvent.click(within(rail).getByRole("button", { name: "Past decisions" }));
    expect(window.location.hash).toBe("#/inbox/decisions");
    expect(await screen.findByText("Nothing decided yet.")).toBeInTheDocument();
    fireEvent.click(within(rail).getByRole("button", { name: "What agents did" }));
    expect(window.location.hash).toBe("#/inbox/activity");
    expect(await screen.findByText("The agents have nothing open or finished lately.")).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("button", { name: "Agent Settings" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Agent Settings" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/agents");
    expect(within(nav).getByRole("button", { name: "Agent Settings" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(nav).getByRole("button", { name: "My Resume" }));
    expect(window.location.hash).toBe("#/me");
    expect(within(nav).getByRole("button", { name: "My Resume" })).toHaveAttribute("aria-current", "page");
    // the brand goes home
    fireEvent.click(screen.getByRole("button", { name: "resumebot home" }));
    expect(await pipeline()).toBeInTheDocument();
  });

  it("Search Settings opens on Where to look, and has the parts in order: Where to look, What to look for, Hard pass, Prioritization, Also sent to agents, About", async () => {
    stubServer({ profile: true });
    render(<App />);
    await pipeline();
    fireEvent.click(within(screen.getByRole("navigation", { name: "Pages" })).getByRole("button", { name: "Search Settings" }));
    const rail = await screen.findByRole("navigation", { name: "Search Settings" });
    expect(within(rail).getAllByRole("button").map((t) => t.textContent)).toEqual(
      ["Where to look", "What to look for", "Hard pass", "Prioritization", "Also sent to agents", "About"],
    );
    expect(within(rail).getByRole("button", { name: "Where to look" })).toHaveAttribute("aria-pressed", "true");
    expect(window.location.hash).toBe("#/profile");
    // Each of the others has its own address (the ids stay: criteria, gates, scoring).
    fireEvent.click(within(rail).getByRole("button", { name: "Hard pass" }));
    expect(window.location.hash).toBe("#/profile/gates");
    fireEvent.click(within(rail).getByRole("button", { name: "Prioritization" }));
    expect(window.location.hash).toBe("#/profile/scoring");
    fireEvent.click(within(rail).getByRole("button", { name: "What to look for" }));
    expect(window.location.hash).toBe("#/profile/criteria");
    expect(within(rail).getByRole("button", { name: "What to look for" })).toHaveAttribute("aria-pressed", "true");
    await settle();
  });

  it("follows Back and pasted addresses", async () => {
    stubServer();
    render(<App />);
    await pipeline();
    act(() => {
      window.location.hash = "#/inbox";
    });
    expect(await notifications()).toBeInTheDocument();
    act(() => {
      window.location.hash = "#/board";
    });
    // the board's address opens the Pipeline, with the Pipeline tab marked
    expect(await pipeline()).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Pages" })).getByRole("button", { name: "Pipeline" })).toHaveAttribute("aria-current", "page");
  });

  it("a role opens beside the list from the board's address, and the stages rail takes it off", async () => {
    const card = {
      id: "r1", title: "Role one", company: "Co one", status: "ready_to_launch", status_label: "Ready to Launch", fit: 9, fit_pct: 90,
      comp: null, where: null, next: null, reason: null,
      url: null, apply_url: null, origin: "scout", packet: null,
    };
    stubServer({ board: { columns: [{ key: "ready_to_launch", label: "Ready to Launch", count: 1, cards: [card] }] } });
    go("#/board");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Role one" }));
    await waitFor(() => expect(window.location.hash).toBe("#/board/r1"));
    expect(await screen.findByRole("heading", { level: 2, name: "Role one" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Pages" })).getByRole("button", { name: "Pipeline" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(screen.getByRole("navigation", { name: "Stages" })).getByRole("button", { name: /^Applied/ }));
    await waitFor(() => expect(window.location.hash).toBe("#/board"));
  });

  it("shows how many things wait on the Notifications tab, and none when nothing does", async () => {
    stubServer({ count: 3 });
    const { unmount } = render(<App />);
    const tab = await screen.findByRole("button", { name: /^Notifications/ });
    await waitFor(() => expect(within(tab).getByText("3")).toBeInTheDocument());
    unmount();
    stubServer({ count: 0 });
    render(<App />);
    await pipeline();
    expect(within(screen.getByRole("button", { name: "Notifications" })).queryByText("0")).toBeNull();
  });

  it("counts every task that waits on you, not only three, as the one sun pill; what you have not read stays in the rail", async () => {
    stubServer({ count: 5, unread: 4 });
    render(<App />);
    const tab = await screen.findByRole("button", { name: /^Notifications/ });
    await waitFor(() => expect(within(tab).getByText("5")).toHaveClass("fi-pill"));
    expect(within(tab).queryByText("4")).toBeNull();
    expect(tab.querySelectorAll(".fi-pill")).toHaveLength(1);
  });

  it("moves the tab's count when the live stream says a task was raised or answered, without asking the server again", async () => {
    const urls = stubServer({ count: 1 });
    const sources: FakeSource[] = [];
    class FakeSource {
      url: string;
      handlers: Record<string, ((e: MessageEvent<string>) => void)[]> = {};
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(url: string) {
        this.url = url;
        sources.push(this);
      }
      addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
        (this.handlers[name] ??= []).push(fn);
      }
      close() {}
      send(name: string, data: unknown) {
        for (const fn of this.handlers[name] ?? []) fn(new MessageEvent(name, { data: JSON.stringify(data) }));
      }
    }
    const real = globalThis.EventSource;
    globalThis.EventSource = FakeSource as unknown as typeof EventSource;
    try {
      render(<App />);
      const tab = await screen.findByRole("button", { name: /^Notifications/ });
      await waitFor(() => expect(within(tab).getByText("1")).toBeInTheDocument());
      // One stream for the page, carrying the Pipeline's topics and Notifications'.
      await waitFor(() => expect(sources.length).toBeGreaterThan(0));
      const stream = sources[sources.length - 1];
      expect(stream.url).toContain("task");
      expect(stream.url).toContain("role");
      const asked = () => urls.filter((u) => u.endsWith("/api/v1/tasks") || u.endsWith("/api/v1/inbox")).length;
      const before = asked();
      act(() => stream.send("task", ownerTask(2)));
      await waitFor(() => expect(within(tab).getByText("2")).toBeInTheDocument());
      act(() => stream.send("task", { ...ownerTask(1), state: "done", done_at: "2026-10-07T09:00:00Z" }));
      await waitFor(() => expect(within(tab).getByText("1")).toBeInTheDocument());
      expect(asked()).toBe(before);
    } finally {
      globalThis.EventSource = real;
    }
  });

  it("asks for who is here once, with no sign-in, and uses the answer for the screens", async () => {
    const urls = stubServer();
    go("#/agents");
    render(<App />);
    await screen.findByRole("heading", { level: 1, name: "Agent Settings" });
    expect(urls.filter((u) => u.endsWith("/api/v1/me"))).toHaveLength(1);
    // The roster is asked for by the screen's own effect, which can run a moment after the heading shows under load.
    await waitFor(() => expect(urls.some((u) => u.endsWith("/api/v1/agents"))).toBe(true));
  });

  it("says so when the server can't be reached", async () => {
    stubServer({ me: { detail: "Down for a moment." }, meStatus: 503 });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Down for a moment.");
  });

});
