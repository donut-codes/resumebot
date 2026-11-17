import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { LiveHandler } from "../../api/live";
import type { Employer, EmployerCounts, EmployerMergePreview, EmployerPage, EmployerQuery, EmployerSplitPreview } from "../../api/types/employers";
import { dateInWords, ownerToday } from "../../lib/dates";
import { createEmployersStore, EmployersStoreProvider, type EmployersApi } from "../../state/employersStore";
import { render } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import { mergeWords, splitWords } from "./EmployerMove";
import EmployersScreen from "./EmployersScreen";

// Employers on a made-up store: the rail's views and counts, a row's chip, meta and watch switch, the pane's head, actions,
// facts, roles and history, and every action kept from the old page: watch, hard pass with a reason, edit facts, merge
// and split with what they move. No real company.

function employer(id: string, extra: Partial<Employer> = {}): Employer {
  return {
    id, name: `Co ${id}`, aliases: [], website: null, careers_url: null, reviews_url: null, headquarters: null, offices: [],
    local: null, work_style: "unknown", employees: null, founded: null, industry: null, sectors: [], ownership: "unknown",
    ai_native: null, events: [], group: null, notes: null, sources: {},
    roles_total: 1, roles_open: 1, roles_ahead: 0, site: { id: `s-${id}`, url: `https://${id}.test`, on: false, state: "off", needs: null, status: "" },
    ...extra,
  };
}

const COUNTS: EmployerCounts = { all: 14, open: 8, watched: 7, hard_pass: 2, local: 6 };

const ROWS = [
  employer("a", { name: "Northwind Labs", industry: "Robotics", employees: 2400, ats: "example-system", ats_label: "Example System", roles_open: 3, roles_ahead: 1, fetched_at: "2026-10-24T03:00:00Z" }),
  employer("b", { name: "Initech", industry: "Banking", roles_open: 2 }),
  employer("c", { name: "Umbrella Works", roles_open: 0, site: null }),
];

function detail(id: string, extra: Partial<Employer> = {}): Employer {
  const row = ROWS.find((r) => r.id === id) ?? employer(id);
  return {
    ...row, headquarters: "Maplewood, ST", local: true, offices: ["Maplewood, ST", "Oakfield, ST"], website: "https://northwind.test",
    careers_url: "https://northwind.test/careers", fetched_at: "2026-10-24T03:00:00Z", fetch_ok: true, open_roles: 3,
    sources: {
      local: { source: "you", at: "2026-10-01T12:00:00Z", by: "owner" },
      offices: { source: "company site", at: "2026-10-23T12:00:00Z", by: "scout" },
    },
    roles: [
      { id: "r1", title: "Operations Manager", status: "ready_to_launch", fit: 84, found: "2026-10-20" },
      { id: "r2", title: "Projects Lead", status: "first_impression", fit: 71, found: "2026-10-19" },
    ],
    history: [{ at: "2026-10-18T12:00:00Z", sentence: "Researched Northwind Labs: size and industry filled in." }],
    ...extra,
  };
}

function setup(overrides: Partial<EmployersApi> = {}, rows: Employer[] = ROWS) {
  let push: LiveHandler = () => {};
  const api = {
    employers: vi.fn(async (): Promise<EmployerPage> => ({ employers: rows, next: null, total: rows.length, counts: COUNTS, industries: ["Banking", "Robotics"] })),
    employer: vi.fn(async (id: string) => detail(id)),
    editEmployer: vi.fn(async (id: string, changes: Partial<Employer>) => ({ ...detail(id), ...changes })),
    setSite: vi.fn(async (id: string, on: boolean) => ({ id, on, state: on ? "waiting" : "off", status: on ? "Starts tonight" : "", needs: null })),
    employerMergePreview: vi.fn(),
    mergeEmployer: vi.fn(),
    employerSplitPreview: vi.fn(),
    splitEmployer: vi.fn(),
    ...overrides,
  } as unknown as EmployersApi;
  const store = createEmployersStore({ api, openLive: (onEvent) => ((push = onEvent), { close: () => {} }), recountMs: 0 });
  function Screen() {
    const [s] = useState(store);
    return (
      <ToastProvider>
        <EmployersStoreProvider store={s}>
          <EmployersScreen />
        </EmployersStoreProvider>
      </ToastProvider>
    );
  }
  return { api, store, push: ((e, d) => push(e, d)) as LiveHandler, Screen };
}

async function openPane(hash = "#/employers/a", overrides: Partial<EmployersApi> = {}) {
  const s = setup(overrides);
  render(<s.Screen />, { hash });
  const pane = await screen.findByRole("region", { name: "Northwind Labs" });
  return { ...s, pane };
}

describe("Employers", () => {
  it("opens on With open roles: the rail's views with their counts, a page of rows from one call, the first one open", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/employers" });
    const rail = await screen.findByRole("navigation", { name: "Employer views" });
    await waitFor(() => expect(within(rail).getByRole("button", { name: /With open roles/ })).toHaveTextContent("8"));
    expect(within(rail).getAllByRole("button").map((b) => b.querySelector(".fi-stage-name")?.textContent)).toEqual(["With open roles", "Watched", "Has a local office", "Hard pass", "All employers"]);
    expect(within(rail).getByRole("button", { name: /All employers/ })).toHaveTextContent("14");
    expect(screen.getByRole("heading", { level: 1, name: "With open roles" })).toBeInTheDocument();
    expect(screen.getByText("3 employers")).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Northwind Labs" })).toBeInTheDocument();
    expect(api.employers).toHaveBeenCalledTimes(1);
  });

  it("shows on a row its open roles (washed when one is ahead, faint at none), its meta and its watch switch", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/employers" });
    expect(await screen.findByRole("img", { name: "3 open roles" })).toHaveClass("fi-fit", "is-high");
    expect(screen.getByRole("img", { name: "2 open roles" })).not.toHaveClass("is-high");
    expect(screen.getByRole("img", { name: "0 open roles" })).toHaveClass("is-none");
    const row = screen.getByRole("button", { name: "Northwind Labs" }).closest("li")!;
    expect(row).toHaveTextContent("Robotics");
    expect(row).toHaveTextContent("2,400 people");
    expect(row).toHaveTextContent("Example System");
    expect(row).toHaveTextContent(/Board read Oct 2[34]/);
    expect(within(row).getByRole("switch", { name: "Watch Northwind Labs's job board" })).toHaveAttribute("aria-checked", "false");
    // A company with no site has nothing to watch.
    expect(within(screen.getByRole("button", { name: "Umbrella Works" }).closest("li")!).queryByRole("switch")).toBeNull();
  });

  it("asks the server for a view, a name and an industry without a reload", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/employers" });
    const rail = await screen.findByRole("navigation", { name: "Employer views" });
    fireEvent.click(within(rail).getByRole("button", { name: /Hard pass/ }));
    await waitFor(() => expect(api.employers).toHaveBeenLastCalledWith(expect.objectContaining({ view: "hard_pass" })));
    fireEvent.change(screen.getByRole("searchbox", { name: "Find an employer" }), { target: { value: "north" } });
    await waitFor(() => expect(api.employers).toHaveBeenLastCalledWith(expect.objectContaining({ view: "hard_pass", q: "north" })));
    fireEvent.change(screen.getByRole("combobox", { name: "Industry" }), { target: { value: "Robotics" } });
    await waitFor(() => expect(api.employers).toHaveBeenLastCalledWith(expect.objectContaining({ industry: "Robotics" })));
    expect(screen.getByRole("heading", { level: 1, name: "Hard pass" })).toBeInTheDocument();
  });

  it("watches from the row: the switch and the rail's count move at once", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/employers" });
    const toggle = await screen.findByRole("switch", { name: "Watch Initech's job board" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(within(screen.getByRole("navigation", { name: "Employer views" })).getByRole("button", { name: /Watched/ })).toHaveTextContent("8");
    await waitFor(() => expect(api.setSite).toHaveBeenCalledWith("s-b", true));
  });

  it("heads the pane with its name, links and the board's last read, then its actions in one row", async () => {
    const { pane } = await openPane();
    expect(within(pane).getByRole("heading", { level: 2, name: "Northwind Labs" })).toBeInTheDocument();
    expect(within(pane).getByRole("link", { name: "Website" })).toHaveAttribute("href", "https://northwind.test");
    expect(within(pane).getByRole("link", { name: "Careers" })).toHaveAttribute("href", "https://northwind.test/careers");
    expect(within(pane).getByText(/Board read .*: 3 open/)).toBeInTheDocument();
    const actions = within(pane).getByRole("group", { name: "What to do with it" });
    expect(within(actions).getByRole("switch", { name: "Watch its job board" })).toBeInTheDocument();
    expect(within(actions).getByRole("button", { name: "Hard pass" })).toBeInTheDocument();
    expect(within(actions).getByRole("button", { name: "More" })).toBeInTheDocument();
  });

  it("shows no reviews link, whether research recorded the employer's page there or not", async () => {
    const without = await openPane();
    expect(within(without.pane).queryByRole("link", { name: /reviews/i })).not.toBeInTheDocument();
    expect(without.pane.querySelector('a[href*="reviews.example"]')).toBeNull();
    cleanup();
    const recorded = await openPane("#/employers/a", { employer: vi.fn(async (id: string) => detail(id, { reviews_url: "https://reviews.example.test/overview/x.htm" })) });
    expect(within(recorded.pane).getByRole("link", { name: "Website" })).toBeInTheDocument();
    expect(within(recorded.pane).queryByRole("link", { name: /reviews/i })).not.toBeInTheDocument();
    expect(recorded.pane.querySelector('a[href*="reviews.example"]')).toBeNull();
  });

  it("shows the facts strip, a fact's source only when the owner set it or it is older than the board's read, and the rest", async () => {
    const { pane } = await openPane();
    const strip = within(pane).getByText("Headquarters").closest("dl")!;
    expect(strip).toHaveTextContent("Maplewood, ST");
    expect(strip).toHaveTextContent("Local office");
    expect(within(strip).getByText(/set by you/)).toBeInTheDocument();
    const facts = within(pane).getByRole("heading", { name: "Facts" }).closest("section")!;
    expect(facts).toHaveTextContent("Maplewood, ST; Oakfield, ST");
    expect(within(facts).getByText(/company site, Oct 24/)).toBeInTheDocument();
  });

  it("says when its board was first counted as a date in words, on the owner's calendar", async () => {
    const since = (days: number) => {
      const d = new Date(`${ownerToday()}T12:00:00Z`);
      d.setUTCDate(d.getUTCDate() - days);
      return d.toISOString().slice(0, 10);
    };
    const hiring = { open: 3, opened: 2, closed: 1, days: 30 };
    const { pane } = await openPane("#/employers/a", { employer: vi.fn(async (id: string) => detail(id, { hiring: { ...hiring, since: since(4) } })) });
    expect(pane).toHaveTextContent(`counting since ${dateInWords(since(4))}, 1 closed so far`);
    expect(pane).not.toHaveTextContent(since(4));
  });

  it("lists its roles with their stage, each a link to the role, and its history", async () => {
    const { pane } = await openPane();
    const roles = within(pane).getByRole("heading", { name: /Roles/ }).closest("section")!;
    expect(within(roles).getByRole("link", { name: "Operations Manager" })).toHaveAttribute("href", "#/board/r1");
    expect(within(roles).getByText("Ready to Launch")).toHaveClass("is-yours");
    expect(roles).toHaveTextContent("Fit 84");
    expect(within(pane).getByText("Researched Northwind Labs: size and industry filled in.")).toBeInTheDocument();
  });

  it("puts a hard pass on with a reason from the menu, says what it changed, and offers to take it back", async () => {
    const editEmployer = vi.fn(async (id: string) => ({ ...detail(id), gated: true, gated_reason: "too far", said: "1 role went to Gated out." }));
    const employer = vi.fn(async (id: string) => detail(id, editEmployer.mock.calls.length ? { gated: true, gated_reason: "too far" } : {}));
    const { pane } = await openPane("#/employers/a", { editEmployer, employer });
    fireEvent.click(within(pane).getByRole("button", { name: "Hard pass" }));
    const menu = within(pane).getByRole("dialog", { name: "Hard pass on its roles" });
    fireEvent.change(within(menu).getByRole("textbox"), { target: { value: "too far" } });
    fireEvent.click(within(menu).getByRole("button", { name: "Hard pass" }));
    await waitFor(() => expect(editEmployer).toHaveBeenCalledWith("a", { gated: true, gated_reason: "too far" }));
    expect(await screen.findByText("1 role went to Gated out.")).toBeInTheDocument();
    expect(within(pane).getByText("Hard pass: too far")).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Take it back" })).toBeInTheDocument();
  });

  it("says what a watch is doing, in Where to look's words, and why the app watches it", async () => {
    const watched = detail("a", { site: { id: "s-a", url: "https://a.test", on: true, state: "waiting", needs: null, status: "Waits its turn: starts in 2 nights", watched_for: "in_play" } });
    const { pane } = await openPane("#/employers/a", { employer: vi.fn(async () => watched) });
    expect(within(pane).getByText("Waits its turn: starts in 2 nights. Watched because it has a role in play")).toBeInTheDocument();
  });

  it("says a watched employer's job system is switched off, though its watch is off", async () => {
    const off = detail("a", { site: { id: "s-a", url: "https://a.test", on: false, state: "off", needs: null, status: "Off: Example System is switched off" } });
    const { pane } = await openPane("#/employers/a", { employer: vi.fn(async () => off) });
    expect(within(pane).getByText("Off: Example System is switched off")).toBeInTheDocument();
  });

  it("edits facts from More, and sends only what changed, so a city and state are never cut in two", async () => {
    const { pane, api } = await openPane();
    fireEvent.click(within(pane).getByRole("button", { name: "More" }));
    fireEvent.click(within(pane).getByRole("menuitem", { name: "Edit facts" }));
    const form = within(pane).getByRole("form", { name: "Edit its facts" });
    fireEvent.change(within(form).getByLabelText("People"), { target: { value: "300" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.editEmployer).toHaveBeenCalledWith("a", { employees: 300 }));
    await waitFor(() => expect(within(pane).queryByRole("form", { name: "Edit its facts" })).toBeNull());
  });

  it("merges from More: finds the one to keep by name through the list's call, shows what moves, and the row leaves", async () => {
    const preview: EmployerMergePreview = {
      keep: { id: "b", name: "Initech", roles: 2, postings: 2, sites: 0 }, away: { id: "a", name: "Northwind Labs", roles: 3, postings: 3, sites: 1 },
      name: "Initech", names: ["Northwind Labs"], roles: 3, postings: 3, folded: 0, sites: 1, hard_pass: false, to_gated_out: 0,
    };
    const kept = detail("b", { name: "Initech" });
    const employers = vi.fn(async (q?: EmployerQuery): Promise<EmployerPage> =>
      q?.q ? { employers: [ROWS[1]], next: null, total: 1, counts: COUNTS, industries: [] } : { employers: ROWS, next: null, total: 3, counts: COUNTS, industries: [] });
    const { pane, store } = await openPane("#/employers/a", {
      employers,
      employerMergePreview: vi.fn(async () => preview),
      mergeEmployer: vi.fn(async () => ({ merged: true, employer: kept, rows: [{ ...ROWS[1], roles_total: 5 }], removed: ["a"], counts: { ...COUNTS, all: 13 } })),
    });
    fireEvent.click(within(pane).getByRole("button", { name: "More" }));
    fireEvent.click(within(pane).getByRole("menuitem", { name: "Merge into another employer" }));
    fireEvent.change(within(pane).getByRole("searchbox"), { target: { value: "init" } });
    await waitFor(() => expect(employers).toHaveBeenLastCalledWith({ view: "all", q: "init", limit: 9 }));
    fireEvent.click(await within(pane).findByRole("radio", { name: /Initech/ }));
    expect(await within(pane).findByText(mergeWords(preview))).toBeInTheDocument();
    fireEvent.click(within(pane).getByRole("button", { name: "Merge" }));
    expect(await screen.findByRole("region", { name: "Initech" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Northwind Labs" })).toBeNull();
    expect(store.getState().counts.all).toBe(13);
  });

  it("asks for the merge search once for a word typed in a burst, not for each letter", async () => {
    const employers = vi.fn(async (q?: EmployerQuery): Promise<EmployerPage> =>
      q?.q ? { employers: [ROWS[1]], next: null, total: 1, counts: COUNTS, industries: [] } : { employers: ROWS, next: null, total: 3, counts: COUNTS, industries: [] });
    const { pane } = await openPane("#/employers/a", { employers });
    fireEvent.click(within(pane).getByRole("button", { name: "More" }));
    fireEvent.click(within(pane).getByRole("menuitem", { name: "Merge into another employer" }));
    const box = within(pane).getByRole("searchbox");
    for (const value of ["i", "in", "ini"]) fireEvent.change(box, { target: { value } });
    await waitFor(() => expect(employers).toHaveBeenLastCalledWith({ view: "all", q: "ini", limit: 9 }));
    expect(employers.mock.calls.filter(([q]) => q?.q).map(([q]) => q?.q)).toEqual(["ini"]);
  });

  it("says why a merge could not be done and leaves the page as it was", async () => {
    const { pane } = await openPane("#/employers/a", {
      employers: vi.fn(async () => ({ employers: ROWS, next: null, total: 3, counts: COUNTS, industries: [] })),
      employerMergePreview: vi.fn(async () => Promise.reject(new ApiError("they are the same employer", 422))),
    });
    fireEvent.click(within(pane).getByRole("button", { name: "More" }));
    fireEvent.click(within(pane).getByRole("menuitem", { name: "Merge into another employer" }));
    fireEvent.change(within(pane).getByRole("searchbox"), { target: { value: "init" } });
    fireEvent.click(await within(pane).findByRole("radio", { name: /Initech/ }));
    expect(await within(pane).findByRole("alert")).toHaveTextContent("they are the same employer");
    expect(within(pane).getByRole("button", { name: "Merge" })).toBeDisabled();
  });

  it("splits roles out from More: chosen roles and a name, what moves, then the new employer opens", async () => {
    const preview: EmployerSplitPreview = { from: { id: "a", name: "Northwind Labs" }, to: { id: null, name: "Fabrikam", exists: false }, roles: 1, postings: 1, sites: 0, names: [], still_gated_out: 0 };
    const made = detail("f", { name: "Fabrikam", roles: [] });
    const splitEmployer = vi.fn(async () => ({ roles: 1, postings: 1, names: [], still_gated_out: 0, employer: made, rows: [ROWS[0], made], counts: COUNTS }));
    const { pane } = await openPane("#/employers/a", { employerSplitPreview: vi.fn(async () => preview), splitEmployer });
    fireEvent.click(within(pane).getByRole("button", { name: "More" }));
    fireEvent.click(within(pane).getByRole("menuitem", { name: "Split roles out" }));
    fireEvent.click(within(pane).getByRole("checkbox", { name: "Projects Lead" }));
    fireEvent.change(within(pane).getByLabelText("Move them to the employer called"), { target: { value: "Fabrikam" } });
    fireEvent.click(within(pane).getByRole("button", { name: "See what moves" }));
    expect(await within(pane).findByText(splitWords(preview))).toBeInTheDocument();
    fireEvent.click(within(pane).getByRole("button", { name: "Split them out" }));
    await waitFor(() => expect(splitEmployer).toHaveBeenCalledWith("a", ["r2"], "Fabrikam"));
    expect(await screen.findByRole("region", { name: "Fabrikam" })).toBeInTheDocument();
  });

  it("changes a row in place when the researcher changes it at night", async () => {
    const { Screen, push } = setup();
    render(<Screen />, { hash: "#/employers" });
    const row = (await screen.findByRole("button", { name: "Initech" })).closest("li")!;
    push("employer", { id: "b", industry: "Insurance" });
    await waitFor(() => expect(row).toHaveTextContent("Insurance"));
  });
});

describe("the words of a merge and of a split", () => {
  it("say what moves, what it is called, what is counted once and what the hard pass does", () => {
    const words = mergeWords({
      keep: { id: "k", name: "Harbor Mutual", roles: 2, postings: 3, sites: 1 }, away: { id: "a", name: "Harbor Mutual Insurance", roles: 1, postings: 2, sites: 1 },
      name: "Harbor Mutual Insurance", names: ["Harbor Mutual"], roles: 1, postings: 2, folded: 1, sites: 1, hard_pass: true, to_gated_out: 2,
    });
    expect(words).toBe(
      "Harbor Mutual Insurance goes into Harbor Mutual: 1 role, 2 postings and 1 site move over. It is called Harbor Mutual Insurance, and also Harbor Mutual. 1 posting read under both is counted once. A hard pass holds on the one employer left: 2 roles not yet applied to will go to Gated out.",
    );
  });

  it("say where the roles go, the names that go with them and the roles that stay in Gated out", () => {
    const words = splitWords({ from: { id: "n", name: "Northwind" }, to: { id: null, name: "Fabrikam", exists: false }, roles: 2, postings: 2, sites: 0, names: ["Fabrikam"], still_gated_out: 1 });
    expect(words).toBe(
      "2 roles and 2 postings move from Northwind to a new employer called Fabrikam. The name Fabrikam goes with them. 1 of the roles is in Gated out for the hard pass on Northwind and stays there until the gates are checked again, which returns it if nothing else stops it.",
    );
  });
});
