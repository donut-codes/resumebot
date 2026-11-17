import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Profile, ProfileSaved } from "../../api/types/profile";
import { profileOf, site } from "../../state/searchFixtures";
import { createSearchStore, SearchStoreProvider, type SearchApi } from "../../state/searchStore";
import { render } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import { LIMIT_WARNING } from "./limitWarning";
import SearchScreen from "./SearchScreen";
import { ApiError } from "../../api/client";
import clientSource from "../../api/client.ts?raw";

// Search Settings on a made-up store: the rail's six parts, a field that saves itself when it is left (with Undo and a
// reason), a box that saves at once, the match weight saved when let go, the rule rows with what they stopped, the sites,
// and About. Nobody's real data.

function setup(overrides: Partial<SearchApi> = {}) {
  let server = profileOf();
  const api = {
    profile: vi.fn(async () => structuredClone(server)),
    saveProfile: vi.fn(async (part: keyof Profile["versions"], data: unknown): Promise<ProfileSaved> => {
      const version = server.versions[part] + 1;
      server = { ...server, [part]: structuredClone(data), versions: { ...server.versions, [part]: version } };
      return { saved: true, version, profile: structuredClone(server) };
    }),
    profileWhy: vi.fn(async (_part: string, version: number, why: string) => ({ version, saved_by: "owner", why, saved_at: null })),
    profileHistory: vi.fn(async () => ({ kind: "criteria", versions: [{ version: 3, saved_by: "owner", why: "made up", saved_at: null, changed: ["company"] }] })),
    gatesStopped: vi.fn(async () => ({ stopped: { travel: 4, location: 12 } })),
    roleTrend: vi.fn(async () => ({ total: 6 })),
    agentReady: vi.fn(async () => ({ ready: true, why: null })),
    sites: vi.fn(async () => ({ sites: [site("a"), site("b", { list: "avoid", on: false, label: "b.test" })], job_systems: [], watch_in_play: false })),
    setSite: vi.fn(async (id: string, on: boolean) => site(id, { on })),
    setWatchInPlay: vi.fn(async (on: boolean) => ({ on })),
    siteRoles: vi.fn(async () => ({ total: 0, items: [] })),
    fitsNone: vi.fn(async () => ({ pursued: { count: 0, items: [] }, this_week: { count: 0, items: [] } })),
    ...overrides,
  } as unknown as SearchApi;
  const store = createSearchStore({ api, openLive: (_e, onState) => (onState("open"), { close: () => {} }), resiteMs: 0 });
  function Screen() {
    const [s] = useState(store);
    return (
      <ToastProvider>
        <SearchStoreProvider store={s}>
          <SearchScreen />
        </SearchStoreProvider>
      </ToastProvider>
    );
  }
  return { api, store, Screen };
}

describe("Search Settings", () => {
  it("has the six parts in its rail, the one the address names pressed", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/criteria" });
    const rail = await screen.findByRole("navigation", { name: "Search Settings" });
    expect(within(rail).getAllByRole("button").map((b) => b.textContent)).toEqual(["Where to look", "What to look for", "Hard pass", "Prioritization", "Also sent to agents", "About"]);
    expect(within(rail).getByRole("button", { name: "What to look for" })).toHaveAttribute("aria-pressed", "true");
  });

  it("saves a field when it is left, says Saved with Undo, takes a reason, and Undo puts it back", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/criteria" });
    fireEvent.click(await screen.findByRole("button", { name: "Company" }));
    const box = await screen.findByLabelText("The kind of employer you want");
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "A different made-up kind." } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.blur(box);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveProfile).mock.calls[0][1]).toMatchObject({ company: "A different made-up kind." });
    const pane = screen.getByRole("region", { name: "Company" });
    await waitFor(() => expect(within(pane).getByRole("status")).toHaveTextContent("Saved"));
    const line = within(pane).getByRole("status");

    fireEvent.click(within(line).getByRole("button", { name: "Add a reason" }));
    fireEvent.change(screen.getByLabelText("Why you changed it"), { target: { value: "made up" } });
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    await waitFor(() => expect(api.profileWhy).toHaveBeenCalledWith("criteria", 4, "made up"));

    fireEvent.click(within(within(pane).getByRole("status")).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.saveProfile).mock.calls[1][1]).toMatchObject({ company: "A made-up kind of employer." });
    await waitFor(() => expect(screen.getByLabelText("The kind of employer you want")).toHaveValue("A made-up kind of employer."));
  });

  it("opens a target role with its roles found this week, its fields, and the posting and check actions", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/criteria" });
    const pane = await screen.findByRole("region", { name: "Builders" });
    await waitFor(() => expect(within(pane).getByText("6 found this week")).toBeInTheDocument());
    expect(within(pane).getByLabelText("Example titles, separated by semicolons")).toHaveValue("Builder");
    for (const name of ["Paste a job posting", "Check my roles", "Delete role"]) expect(within(pane).getByRole("button", { name })).toBeInTheDocument();
    expect(within(pane).getByRole("heading", { name: /History/ })).toBeInTheDocument();
  });

  it("says the count of example titles beside the box, and refuses an eleventh before anything is sent", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/criteria" });
    const pane = await screen.findByRole("region", { name: "Builders" });
    expect(within(pane).getByText("Up to 10 example titles (1 now).")).toBeInTheDocument();
    const box = within(pane).getByLabelText("Example titles, separated by semicolons");
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: Array.from({ length: 11 }, (_, i) => `Title ${i}`).join("; ") } });
    fireEvent.blur(box);
    expect(await within(pane).findByRole("alert")).toHaveTextContent("A role holds up to 10 example titles, and you wrote 11. Remove some, then leave the box again.");
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "A; B" } });
    fireEvent.blur(box);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
  });

  it("says the count of roles, and offers no Add a role once there are twenty", async () => {
    const roles = Array.from({ length: 20 }, (_, i) => ({ id: `r${i}`, name: `Role ${i}`, titles: [], description: "", responsibilities: "", qualifications: "", preferred: "", compensation: "" }));
    const base = profileOf();
    const { Screen } = setup({ profile: vi.fn(async () => ({ ...base, criteria: { ...base.criteria, target_roles: roles } })) as unknown as SearchApi["profile"] });
    render(<Screen />, { hash: "#/profile/criteria" });
    expect(await screen.findByText("Target roles (up to 20)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a role" })).toBeDisabled();
  });

  it("offers Add a role while there is room", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/criteria" });
    expect(await screen.findByRole("button", { name: "Add a role" })).toBeEnabled();
  });

  it("turns the server's refusal of too many titles into one plain sentence on the Saved line", async () => {
    const refusal = "the criteria don't fit: target_roles.0.titles: Value error, a target role holds at most 10 example titles; it has 11";
    const { Screen } = setup({ saveProfile: vi.fn(async () => { throw new ApiError(refusal, 422); }) as unknown as SearchApi["saveProfile"] });
    render(<Screen />, { hash: "#/profile/criteria" });
    const pane = await screen.findByRole("region", { name: "Builders" });
    const box = within(pane).getByLabelText("Role name");
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "Other" } });
    fireEvent.blur(box);
    await waitFor(() => expect(within(pane).getByRole("status")).toHaveTextContent("A role holds up to 10 example titles, and this one has 11. Remove some, then save again."));
    expect(within(pane).getByRole("status")).not.toHaveTextContent("Value error");
  });

  it("lists each hard pass with what it stopped, and a box in work location saves at once", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/gates" });
    const travel = (await screen.findByRole("button", { name: "Travel is under a quarter of the time." })).closest("li")!;
    expect(travel).toHaveTextContent("Stopped 4");
    const pane = await screen.findByRole("region", { name: "Work location" });
    fireEvent.click(within(pane).getByRole("checkbox", { name: "Remote" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveProfile).mock.calls[0][0]).toBe("gates");
    expect(vi.mocked(api.saveProfile).mock.calls[0][1]).toMatchObject({ location: { remote: { on: true } } });
  });

  it("limits an employer to two open applications, on, and shows the owner's warning before it is turned off", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/gates" });
    fireEvent.click(await screen.findByRole("button", { name: /At most two open applications at one employer/ }));
    const pane = await screen.findByRole("region", { name: "Open applications" });
    const toggle = within(pane).getByRole("switch", { name: "At most two open applications at one employer" });
    expect(toggle).toHaveAttribute("aria-checked", "true");

    fireEvent.click(toggle);
    const warning = await screen.findByRole("group", { name: "Before you turn this off" });
    expect(warning).toHaveTextContent(LIMIT_WARNING.intro);
    for (const [lead] of LIMIT_WARNING.points) expect(warning).toHaveTextContent(lead);
    expect(api.saveProfile).not.toHaveBeenCalled();

    fireEvent.click(within(warning).getByRole("button", { name: "Keep it on" }));
    expect(screen.queryByRole("group", { name: "Before you turn this off" })).not.toBeInTheDocument();
    expect(api.saveProfile).not.toHaveBeenCalled();

    fireEvent.click(toggle);
    fireEvent.click(within(await screen.findByRole("group", { name: "Before you turn this off" })).getByRole("button", { name: "Turn it off" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveProfile).mock.calls[0][0]).toBe("gates");
    expect(vi.mocked(api.saveProfile).mock.calls[0][1]).toMatchObject({ employer_limit: false });
  });

  it("turns the limit back on with no warning", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/gates" });
    fireEvent.click(await screen.findByRole("button", { name: /At most two open applications at one employer/ }));
    const pane = await screen.findByRole("region", { name: "Open applications" });
    fireEvent.click(within(pane).getByRole("switch", { name: "At most two open applications at one employer" }));
    fireEvent.click(within(await screen.findByRole("group", { name: "Before you turn this off" })).getByRole("button", { name: "Turn it off" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const again = await within(await screen.findByRole("region", { name: "Open applications" })).findByRole("switch", { name: "At most two open applications at one employer" });
    await waitFor(() => expect(again).toHaveAttribute("aria-checked", "false"));
    fireEvent.click(again);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("group", { name: "Before you turn this off" })).not.toBeInTheDocument();
    expect(vi.mocked(api.saveProfile).mock.calls[1][1]).toMatchObject({ employer_limit: true });
  });

  it("puts the match weight first in Prioritization, saves it when let go and says it is re-ranking", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/scoring" });
    const slider = await screen.findByRole("slider");
    const rows = screen.getByRole("region", { name: "Prioritization" });
    expect(within(rows).getAllByRole("listitem")[0]).toHaveTextContent("Match weight");
    fireEvent.change(slider, { target: { value: "70" } });
    expect(screen.getByText("Can I do it 70%")).toBeInTheDocument();
    expect(api.saveProfile).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.pointerUp(slider);
    });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveProfile).mock.calls[0][1]).toMatchObject({ match_weight: 0.7 });
    expect(screen.getByText("Re-ranking every role…")).toBeInTheDocument();
  });

  it("shows the sites under their heads with a switch, and watching every employer in play first", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile" });
    const list = await screen.findByRole("region", { name: "Where to look" });
    await waitFor(() => expect(within(list).getByText("Sites to search")).toBeInTheDocument());
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Watch the job board of every employer with a role in play");
    expect(within(list).getByText("Sites to avoid")).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("switch", { name: "Look here: Board a" }));
    await waitFor(() => expect(api.setSite).toHaveBeenCalledWith("a", false));
  });

  it("offers no robots-exempt switch on Where to look, and the page has no call to its route", async () => {
    // docs/PRD.md 48.6 as amended and 55.6 (the owner, 2026-11-10): the setting an owner turned on by hand is still in the server's answer, and no screen offers it.
    const { Screen } = setup({ sites: vi.fn(async () => ({ sites: [site("a")], job_systems: [], watch_in_play: false, read_robots_exempt: true })) });
    render(<Screen />, { hash: "#/profile" });
    const list = await screen.findByRole("region", { name: "Where to look" });
    await waitFor(() => expect(within(list).getByText("Sites to search")).toBeInTheDocument());
    expect(within(list).queryByRole("switch", { name: /robots|exempt/i })).toBeNull();
    expect(within(list).queryByText(/robots|exempt/i)).toBeNull();
    expect(clientSource).not.toContain("/sites/robots-exempt");
  });

  it("explains how the search works in About, step by step, and where each setting takes effect", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/about" });
    expect(await screen.findByRole("heading", { level: 2, name: "How the search works" })).toBeInTheDocument();
    for (const step of ["Where roles come from", "The hard passes", "Your target roles", "First look and rank", "Line-by-line read", "The queue and Ready to Launch", "Favorites", "Where each setting takes effect"]) {
      expect(screen.getByRole("heading", { level: 3, name: new RegExp(step) })).toBeInTheDocument();
    }
    expect(screen.queryByRole("region", { name: "What to look for" })).toBeNull();
  });

  it("About names no model and no sentence rule", async () => {
    const { Screen } = setup();
    const { container } = render(<Screen />, { hash: "#/profile/about" });
    await screen.findByRole("heading", { level: 2, name: "How the search works" });
    expect(container.textContent).not.toMatch(/Haiku|Sonnet|sentence|Claude/);
  });

  it("a switched-off site shows one plain line, never its raw note", async () => {
    const off = site("a", { on: false, state: "off", off_by: "app", off_note: "HTTP 403 from a made-up vendor", off_at: "2026-11-01T00:00:00Z", status: "" });
    const { Screen } = setup({ sites: vi.fn(async () => ({ sites: [off], job_systems: [], watch_in_play: false })) as unknown as SearchApi["sites"] });
    const { container } = render(<Screen />, { hash: "#/profile/where" });
    fireEvent.click(await screen.findByRole("button", { name: /Board a/ }));
    expect(await screen.findByText(/resumebot turned this off, because it couldn't be read/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/403|vendor/);
  });

  it("names the two stage sizes in About and points at where they are set, not at one daily number", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/about" });
    await screen.findByRole("heading", { level: 2, name: "How the search works" });
    for (const name of ["Roles in Second Impression", "Roles in Ready to Launch"]) {
      expect(screen.getByRole("link", { name })).toHaveAttribute("href", "#/missions");
    }
    expect(screen.queryByText(/aim to send a day/i)).toBeNull();
  });

  it("names no lowest score for Ready to Launch in About, and links to no such setting: it is gone (docs/PRD.md 24.12, 24.15)", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/about" });
    await screen.findByRole("heading", { level: 2, name: "How the search works" });
    expect(screen.queryByRole("link", { name: /lowest score/i })).toBeNull();
    expect(screen.queryByText(/lowest score/i)).toBeNull();
    expect(screen.queryByText(/ready minimum/i)).toBeNull();
    expect(screen.queryByText(/under it waits in Second Impression/)).toBeNull();
  });

  it("says in About that the three stages are one list with one number per role, and what the number is at each point", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/about" });
    await screen.findByRole("heading", { level: 2, name: "How the search works" });
    expect(screen.getByText(/are one list, and every role in it has one number/)).toBeInTheDocument();
    expect(screen.getByText(/its number is that rank as written/)).toBeInTheDocument();
    expect(screen.getByText(/its number is that read.s score/)).toBeInTheDocument();
    expect(screen.getByText(/the number on a card already has both in it/)).toBeInTheDocument();
    expect(screen.getByText(/Ready to Launch is the top read roles in that list/)).toBeInTheDocument();
    expect(screen.queryByText(/has an estimate, worked out from its first look rank/)).toBeNull();
  });

  it("says in About that a role is checked when it comes into Ready to Launch, and that work in progress is kept apart", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/about" });
    await screen.findByRole("heading", { level: 2, name: "How the search works" });
    expect(screen.getByText(/checks at once that its posting is still open/)).toBeInTheDocument();
    expect(screen.getByText(/keeps them apart, as Work in progress, from the ranked list/)).toBeInTheDocument();
  });

  it("says in About that no unread role sits in Ready to Launch and that a role moved there is read first", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/about" });
    await screen.findByRole("heading", { level: 2, name: "How the search works" });
    expect(screen.getByText(/No role sits in Ready to Launch until its Second Impression has read and graded its posting/)).toBeInTheDocument();
    expect(screen.getByText(/If you move a role there before it is read, it is read first, as a star is/)).toBeInTheDocument();
  });
});
