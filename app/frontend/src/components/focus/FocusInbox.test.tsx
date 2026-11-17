import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { LiveHandler } from "../../api/live";
import type { Board, BoardCard, RoleDetail } from "../../api/types/pipeline";
import { createPipelineStore, FAVORITES, PipelineStoreProvider, type PipelineApi } from "../../state/pipelineStore";
import { makeCard } from "../../state/rowFixtures";
import { render, settle } from "../../test-utils";
import CountRoll from "./CountRoll";
import FocusInbox from "./FocusInbox";

// The Focus Inbox drawn from a store over a made-up server: the stages rail with Favorites first, a search across every
// stage, the role beside the list, and closing a role with a reason. Everything is made up.

afterEach(settle);

function card(id: string, status: string, label: string, extra: Partial<BoardCard> = {}): BoardCard {
  return makeCard({
    id, title: `Role ${id}`, company: `Co ${id}`, status, status_label: label, fit_pct: 60, score: 60,
    work: { mode: "remote", place: null, words: "Remote" }, pay: "$120K", age: { days: 2, words: "2 days", from: "found" },
    origin: "scout", ...extra,
  });
}

const READY = card("r1", "ready_to_launch", "Ready to Launch", { new: true, apply_url: "https://jobs.example.com/apply/1", next: null });
const APPLIED = card("a1", "applied", "Applied", { favorite: true });

function board(): Board {
  return {
    columns: [
      { key: "ready_to_launch", label: "Ready to Launch", count: 1, cards: [READY] },
      { key: "applied", label: "Applied", count: 1, cards: [APPLIED] },
      { key: "closed", label: "Closed", count: 0, cards: [] },
    ],
    ended_reasons: [{ reason: "wrong_level", label: "Wrong level" }, { reason: "they_said_no", label: "They said no" }],
  };
}

function detail(c: BoardCard): RoleDetail {
  return {
    role: { ...c, notes: null, found: null, posted: null, board_found_on: null, ats: null, level: null, sector: null, description: "First part.\n\nSecond part.", description_at: null },
    employer: null,
    scoring: { asks: [{ id: "x", section: "role" as never, says: "Leads a team", points: 5, answer: "met", earned: 5, evidence: "Led four people", state: "checked" }] },
    gates: [{ id: "g", kind: "pay", says: "Lowest pay", result: "pass", fact: "Band starts high enough" }],
    timeline: [{ at: null, kind: "found", who: "app", sentence: "Found on a job board", detail: null }],
    packets: c.id === "r1" ? [PACKET] : [],
  };
}

const PACKET = {
  id: "p1", role_id: "r1", version: 2, status: "ready", feedback: "", created_at: null,
  resume: { name: "A. Person", headline: "Builds operating rhythms", summary: "Ten years of running projects." },
  letter: { paragraphs: ["Dear team, this is the letter."] },
  reasoning: {}, checks: [{ check: "Length", ok: true, blocking: true, detail: "" }],
} as unknown as RoleDetail["packets"][number];

function setup(overrides: Partial<PipelineApi> = {}, hash = "#/board") {
  let push: LiveHandler = () => {};
  const api = {
    board: vi.fn(async () => board()),
    favorites: vi.fn(async () => ({ key: FAVORITES, label: "Favorites", count: 1, cards: [APPLIED] })),
    role: vi.fn(async (id: string) => detail(id === "a1" ? APPLIED : READY)),
    search: vi.fn(),
    setFavorite: vi.fn(),
    moveRole: vi.fn(),
    moveBack: vi.fn(),
    applyGo: vi.fn(),
    applyGoProgress: vi.fn(async () => ({ phase: "idle", total: 0, built: 0, opened: [], skipped: [], notes: [], done: true })),
    askPacket: vi.fn(async () => ({})),
    savePacketFiles: vi.fn(async () => ({})),
    profile: vi.fn(async () => ({ criteria: { target_roles: [{ id: "builders", name: "Builders" }] } })),
    ...overrides,
  } as unknown as PipelineApi;
  const store = createPipelineStore({ api, openLive: (onEvent) => ((push = onEvent), { close: () => {} }) });
  render(
    <PipelineStoreProvider store={store}>
      <FocusInbox />
    </PipelineStoreProvider>,
    { hash },
  );
  return { api, store, push: ((e, d) => act(() => push(e, d))) as LiveHandler };
}

const rail = () => screen.getByRole("navigation", { name: "Stages" });

describe("the Focus Inbox", () => {
  it("lists Favorites first in the rail with a live count, then the stages, and opens on Ready to Launch", async () => {
    const { push } = setup();
    expect(await screen.findByRole("heading", { level: 1, name: "Ready to Launch" })).toBeInTheDocument();
    const names = within(rail()).getAllByRole("button").map((b) => b.querySelector(".fi-stage-name")?.textContent);
    expect(names.slice(0, 4)).toEqual(["Favorites", "Ready to Launch", "Applied", "Active Discussion"]);
    push("counts", { lanes: { ready_to_launch: 1, applied: 1 }, groups: { ranked: 1, work: 0 }, closed: {}, closed_total: 0, favorites: 3 });
    expect(within(rail()).getByRole("button", { name: /^Favorites/ })).toHaveTextContent("3");
    // The row says it is new; the page has no title or lead-in of its own.
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Pipeline" })).toBeNull();
  });

  it("Favorites shows every starred role whatever its stage, each naming its stage", async () => {
    setup();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Favorites/ }));
    const list = screen.getByRole("list", { name: "Roles" });
    expect(within(list).getByRole("button", { name: "Role a1" })).toBeInTheDocument();
    expect(within(list).getByText("Applied")).toHaveClass("fi-badge-stage");
    expect(within(list).queryByRole("button", { name: "Role r1" })).toBeNull();
  });

  it("stars a role open in the pane that is in no loaded lane", async () => {
    const stray = card("z9", "applied", "Applied");
    const answer = { favorite: true, changed: true, moved: false, research: null, packet: null, role: { ...stray, favorite: true }, counts: { lanes: { ready_to_launch: 1, applied: 1 }, groups: { ranked: 1, work: 0 }, closed: {}, closed_total: 0, favorites: 2 } };
    let starred = false;
    const setFavorite = vi.fn(async () => ((starred = true), answer));
    setup({ role: vi.fn(async () => detail({ ...stray, favorite: starred })) as unknown as PipelineApi["role"], setFavorite: setFavorite as unknown as PipelineApi["setFavorite"] }, "#/board/z9");
    const pane = await screen.findByRole("region", { name: "Role" });
    fireEvent.click(await within(pane).findByRole("button", { name: "Star Role z9" }));
    await waitFor(() => expect(setFavorite).toHaveBeenCalledWith("z9", true));
    expect(await within(pane).findByRole("button", { name: "Unstar Role z9" })).toBeInTheDocument();
  });

  it("moves a role open in the pane that is in no loaded lane", async () => {
    const stray = card("z9", "applied", "Applied");
    const moveRole = vi.fn(async () => ({ ...card("z9", "active_discussion", "Active Discussion"), counts: { lanes: { ready_to_launch: 1, applied: 1 }, groups: { ranked: 1, work: 0 }, closed: {}, closed_total: 0, favorites: 1 } }));
    setup({ role: vi.fn(async () => detail(stray)) as unknown as PipelineApi["role"], moveRole: moveRole as unknown as PipelineApi["moveRole"] }, "#/board/z9");
    const pane = await screen.findByRole("region", { name: "Role" });
    fireEvent.click(await within(pane).findByRole("button", { name: "Discussion" }));
    await waitFor(() => expect(moveRole).toHaveBeenCalledWith("z9", "active_discussion", undefined, undefined));
  });

  it("moves a closed role back when it is open in the pane and in no loaded lane", async () => {
    const stray = card("z9", "rejected", "Rejected", { ended_words: "They said no", next: "They said no" });
    const moveBack = vi.fn(async () => ({ ...card("z9", "applied", "Applied"), counts: { lanes: { ready_to_launch: 1, applied: 1 }, groups: { ranked: 1, work: 0 }, closed: {}, closed_total: 0, favorites: 1 } }));
    setup({ role: vi.fn(async () => detail(stray)) as unknown as PipelineApi["role"], moveBack: moveBack as unknown as PipelineApi["moveBack"] }, "#/board/z9");
    const pane = await screen.findByRole("region", { name: "Role" });
    fireEvent.click(await within(pane).findByRole("button", { name: "Move back" }));
    await waitFor(() => expect(moveBack).toHaveBeenCalledWith("z9"));
  });

  it("opens the first role beside the list, with its pay, why it fits, its hard passes, the posting and its history", async () => {
    setup();
    expect(await screen.findByRole("heading", { level: 2, name: "Role r1" })).toBeInTheDocument();
    const pane = screen.getByRole("region", { name: "Role" });
    expect(within(pane).getByText("$120K")).toBeInTheDocument();
    expect(await within(pane).findByText("Leads a team")).toBeInTheDocument();
    expect(within(pane).getByText("Lowest pay")).toBeInTheDocument();
    expect(within(pane).getByText("Second part.")).toBeInTheDocument();
    expect(within(pane).getByText("Found on a job board")).toBeInTheDocument();
    // Its own way in, not Launch in Browser (which opens every Ready to Launch role).
    expect(within(pane).queryByRole("button", { name: "Launch in Browser" })).toBeNull();
    expect(within(pane).getByRole("link", { name: "Open the application" })).toHaveAttribute("href", "https://jobs.example.com/apply/1");
    expect(within(pane).getByRole("group", { name: "Stage" })).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Ready" })).toHaveAttribute("aria-pressed", "true");
  });

  it.each([
    "Launch in Browser skips it: the posting asks for a login",
    "Being re-checked: it is offered again once its gates are confirmed",
    "The packet failed · Pay mostly below your floor: $70,000–$150,000",
  ])("says what the server says of a Ready to Launch role, in its row and in the Ready box: %s", async (next) => {
    const said = { ...READY, next };
    setup({
      board: vi.fn(async () => ({ ...board(), columns: [{ key: "ready_to_launch", label: "Ready to Launch", count: 1, cards: [said] }, ...board().columns.slice(1)] })) as unknown as PipelineApi["board"],
      role: vi.fn(async () => detail(said)) as unknown as PipelineApi["role"],
    });
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByText(next)).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Roles" })).getByText(next)).toBeInTheDocument();
  });

  it("says nothing of a Ready to Launch role that is the normal run: no sentence on its row, no title in its Ready box, only its way in", async () => {
    // Old: "Launch builds its packet" on the row, and "Ready when you are" over the same words in the box (24.18).
    setup();
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByRole("link", { name: "Open the application" })).toHaveAttribute("href", "https://jobs.example.com/apply/1");
    expect(within(pane).queryByText("Ready when you are")).toBeNull();
    expect(pane.querySelector(".fi-launch-text")).toBeNull();
    expect(document.querySelector(".fi-row-next, .fi-row-check")).toBeNull();
  });

  it("says in the Ready box that Launch opened a role, and leaves the sentence to the row", async () => {
    const opened = { ...READY, opened_at: "2026-10-21T09:10:00+00:00", next: "Opened. Mark Applied when sent" };
    setup({
      board: vi.fn(async () => ({ ...board(), columns: [{ key: "ready_to_launch", label: "Ready to Launch", count: 1, cards: [opened] }, ...board().columns.slice(1)] })) as unknown as PipelineApi["board"],
      role: vi.fn(async () => detail(opened)) as unknown as PipelineApi["role"],
    });
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByText("Opened in your browser")).toBeInTheDocument();
    expect(within(pane).getByText("Mark it Applied when you have sent it")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Roles" })).getByText("Opened. Mark Applied when sent")).toBeInTheDocument();
  });

  it("shows the analyst's summary between the facts and the packet, and none when the role has none", async () => {
    const summary = { company: "Co r1 makes billing tools.", role: "Runs delivery projects.", why: "The project work you look for, remote." };
    setup({ role: vi.fn(async (id: string) => ({ ...detail(id === "a1" ? APPLIED : READY), summary: id === "r1" ? summary : null })) as unknown as PipelineApi["role"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    const shown = await within(pane).findByRole("region", { name: "Summary" });
    expect(within(shown).getByText("The company")).toBeInTheDocument();
    expect(within(shown).getByText("Runs delivery projects.")).toBeInTheDocument();
    expect(within(shown).getByText("The project work you look for, remote.")).toBeInTheDocument();
    // Below the fit, pay, ask and company; above the packet.
    const packet = within(pane).getByRole("region", { name: "Packet" });
    expect(pane.querySelector(".fi-facts")!.compareDocumentPosition(shown) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(shown.compareDocumentPosition(packet) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(screen.getByRole("list", { name: "Roles" })).getByRole("button", { name: "Role r1" }));
    fireEvent.click(within(rail()).getByRole("button", { name: /^Favorites/ }));
    fireEvent.click(within(screen.getByRole("list", { name: "Roles" })).getByRole("button", { name: "Role a1" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Role a1" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("region", { name: "Summary" })).toBeNull());
  });

  it("a researched role opened without a summary asks for one once, and the live stream's word reads it again", async () => {
    const askSummary = vi.fn(async () => ({ asked: true }));
    let summary: RoleDetail["summary"] = null;
    const role = vi.fn(async (id: string) => ({ ...detail(id === "a1" ? APPLIED : READY), summary, summary_askable: !summary }));
    const { push } = setup({ role: role as unknown as PipelineApi["role"], askSummary: askSummary as unknown as PipelineApi["askSummary"] });
    await screen.findByRole("heading", { level: 2, name: "Role r1" });
    await waitFor(() => expect(askSummary).toHaveBeenCalledWith("r1"));
    summary = { company: "Co r1 makes billing tools.", role: "Runs delivery projects.", why: "Remote project work." };
    push("summary", { id: "r1" });
    expect(await screen.findByText("Remote project work.")).toBeInTheDocument();
    expect(askSummary).toHaveBeenCalledTimes(1);
  });

  it("shows the role's packet: the resume and the letter to read, the files to download, and asking for changes", async () => {
    const { api } = setup();
    const packet = await screen.findByRole("region", { name: "Packet" });
    expect(within(packet).getByText("Version 2: Ready")).toBeInTheDocument();
    expect(within(packet).getByText("Ten years of running projects.")).toBeInTheDocument();
    expect(within(packet).getByRole("link", { name: "Resume (.pdf)" })).toHaveAttribute("href", "/api/v1/packets/p1/resume.pdf");
    expect(within(packet).getByRole("link", { name: "Letter (.docx)" })).toHaveAttribute("href", "/api/v1/packets/p1/letter.docx");
    fireEvent.click(within(packet).getByRole("button", { name: "Cover letter" }));
    expect(within(packet).getByText("Dear team, this is the letter.")).toBeInTheDocument();
    fireEvent.change(within(packet).getByLabelText(/Ask for changes/), { target: { value: "Shorter summary" } });
    fireEvent.click(within(packet).getByRole("button", { name: "Ask for a new version" }));
    await waitFor(() => expect(api.askPacket).toHaveBeenCalledWith("r1", "Shorter summary"));
  });

  it("a packet whose files failed to save says why, and Save files again asks for the save and clears the line", async () => {
    let problem: string | null = "The folder is missing";
    const role = vi.fn(async (id: string) => {
      const d = detail(id === "a1" ? APPLIED : READY);
      return { ...d, packets: d.packets.map((p) => ({ ...p, files_problem: problem })) };
    });
    const { api } = setup({ role });
    const packet = await screen.findByRole("region", { name: "Packet" });
    expect(within(packet).getByText(/couldn.t be saved to your packet folder: The folder is missing/)).toBeInTheDocument();
    problem = null;
    fireEvent.click(within(packet).getByRole("button", { name: "Save files again" }));
    await waitFor(() => expect(api.savePacketFiles).toHaveBeenCalledWith("p1"));
    await waitFor(() => expect(within(packet).queryByRole("button", { name: "Save files again" })).toBeNull());
  });

  it("a packet with no problem shows no Save files again button", async () => {
    setup();
    const packet = await screen.findByRole("region", { name: "Packet" });
    expect(within(packet).queryByRole("button", { name: "Save files again" })).toBeNull();
  });

  it("a board address with a target role reads the lanes narrowed to it, names it, and the filter comes off", async () => {
    const { api } = setup({}, "#/board?role=builders");
    expect(await screen.findByText("Role: Builders")).toBeInTheDocument();
    expect(api.board).toHaveBeenCalledWith(undefined, "builders");
    expect(api.board).not.toHaveBeenCalledWith(undefined, undefined);
    fireEvent.click(screen.getByRole("button", { name: "Remove the role filter" }));
    await waitFor(() => expect(window.location.hash).toBe("#/board"));
    await waitFor(() => expect(api.board).toHaveBeenCalledWith(undefined, undefined));
    expect(screen.queryByText(/^Role:/)).toBeNull();
  });

  it("closing asks why, then moves the role to Closed with that reason", async () => {
    const moveRole = vi.fn(async () => ({ ...card("r1", "withdrew", "Withdrew", { ended_words: "Wrong level", next: "Wrong level" }), counts: { lanes: { ready_to_launch: 0 }, groups: { ranked: 0, work: 0 }, closed: {}, closed_total: 1, favorites: 1 } }));
    setup({ moveRole: moveRole as unknown as PipelineApi["moveRole"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    fireEvent.click(await within(pane).findByRole("button", { name: "Close" }));
    fireEvent.click(within(screen.getByRole("menu", { name: "Why close it?" })).getByRole("menuitem", { name: "Wrong level" }));
    await waitFor(() => expect(moveRole).toHaveBeenCalledWith("r1", "withdrew", undefined, "wrong_level"));
    await waitFor(() => expect(window.location.hash).toBe("#/board/r1"));
  });

  it("searches every stage and marks how many match in each stage on the rail", async () => {
    const search = vi.fn(async () => ({
      q: "co", page: 1, size: 100, pages: 1, total: 2, roles: 2, postings: 0, closed: 1, stages: { applied: 1, rejected: 1 },
      items: [
        { kind: "role", id: "a1", title: "Role a1", company: "Co a1", where: null, status: "applied", label: "Applied", lane: "applied", closed_reason: null, closed_label: null, reason: null, favorite: true, fit_pct: 60, found_at: null, url: null },
        { kind: "role", id: "x9", title: "Role x9", company: "Co x9", where: null, status: "rejected", label: "Rejected", lane: "closed", closed_reason: "rejected", closed_label: "Rejected", reason: "They said no", favorite: false, fit_pct: 40, found_at: null, url: null },
      ],
    }));
    setup({ search: search as unknown as PipelineApi["search"] });
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search every role" }), { target: { value: "co" } });
    expect(await screen.findByRole("heading", { level: 1, name: "2 matches" })).toBeInTheDocument();
    expect(search).toHaveBeenCalledWith("co", 1, 100);
    expect(within(rail()).getByRole("button", { name: /^Closed/ })).toHaveTextContent("· 1");
    expect(within(rail()).getByRole("button", { name: /^Ready to Launch/ })).toHaveClass("is-dim");
    expect(screen.getByText("Rejected: They said no")).toBeInTheDocument();
  });

  it("Add a job: paste a link, see the app's answer, and the role opens in the pane", async () => {
    const addRole = vi.fn(async () => ({ role: "a1", posting: null, already: true, said: "That job is already on your pipeline, in Applied.", stage: "applied", stage_label: "Applied", title: "Role a1", company: "Co a1" }));
    const { api } = setup({ addRole } as unknown as Partial<PipelineApi>);
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(screen.getByRole("button", { name: "Add a job" }));
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Job link"), { target: { value: " https://jobs.example.com/1 " } });
    fireEvent.click(add);
    expect(await screen.findByText("That job is already on your pipeline, in Applied.")).toBeInTheDocument();
    expect(addRole).toHaveBeenCalledWith("https://jobs.example.com/1");
    await waitFor(() => expect(api.role).toHaveBeenCalledWith("a1"));
    expect(window.location.hash).toContain("a1");
    expect(screen.queryByRole("form", { name: "Add a job" })).toBeNull();
  });

  it("Add a job: a refused link is said in the box and the box stays", async () => {
    const addRole = vi.fn(async () => {
      throw new ApiError("That does not look like a job link.", 422);
    });
    setup({ addRole } as unknown as Partial<PipelineApi>);
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(screen.getByRole("button", { name: "Add a job" }));
    fireEvent.change(screen.getByLabelText("Job link"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That does not look like a job link.");
    expect(screen.getByRole("form", { name: "Add a job" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form", { name: "Add a job" })).toBeNull();
  });

  it("a count rolls from its old figure to the new one", () => {
    const { rerender } = render(<CountRoll value={4} />);
    expect(document.querySelector(".fi-roll-old")).toBeNull();
    rerender(<CountRoll value={5} />);
    expect(document.querySelector(".fi-roll-old.fi-roll-up")).toHaveTextContent("4");
    expect(document.querySelector(".fi-roll-now.fi-roll-up")).toHaveTextContent("5");
  });
});

// Qualification marks folded into the posting in the pane (docs/PRD.md 20.8).
const POSTING = "About the role: We run projects.\n\nQualifications:\n- Ran a project office for six years\n- Owns budgets end to end\n\nPreferred:\n- Public speaking experience";
const row = (index: number, item: string, cls: "P" | "T" | "U", extra: Record<string, unknown> = {}) =>
  ({ index, item, class: cls, why: `why ${index}`, by: "analyst", owner_why: "", from: cls === "P" ? [{ id: "m1", text: "Ran the office" }] : [], ...extra });

function gradedDetail(): RoleDetail {
  const d = detail(READY);
  return {
    ...d,
    role: { ...d.role, description: POSTING },
    requirements: [row(1, "Ran a project office for six years", "P"), row(2, "Owns budgets end to end", "T"), row(3, "Fluent in a second language", "U")],
    preferred: [row(4, "Public speaking experience", "P")],
  } as RoleDetail;
}

describe("whose copy the posting is", () => {
  it("says the text is the job board's copy while the employer's own posting could not be read", async () => {
    const d = detail(READY);
    setup({ role: vi.fn(async () => ({ ...d, role: { ...d.role, description: POSTING, description_from: "board" } })) as unknown as PipelineApi["role"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByText(/job board.s copy of the posting/)).toBeInTheDocument();
    expect(within(pane).getByText(/employer.s own posting could not be read/)).toBeInTheDocument();
  });

  it("says nothing of it for the employer's own text", async () => {
    setup({ role: vi.fn(async () => gradedDetail()) as unknown as PipelineApi["role"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    await within(pane).findByText(/About the role/);
    expect(within(pane).queryByText(/job board.s copy/)).toBeNull();
  });
});

describe("qualification marks in the pane's posting", () => {
  const lineOf = async (pane: HTMLElement, re: RegExp) => (await within(pane).findByText(re)).closest(".qmark-line")!.parentElement as HTMLElement;

  it("marks the matching lines in place, shows no unmatched item, no count and no added heading", async () => {
    setup({ role: vi.fn(async () => gradedDetail()) as unknown as PipelineApi["role"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(within(await lineOf(pane, /Ran a project office/)).getByRole("button", { name: /Proven/ })).toBeInTheDocument();
    expect(within(await lineOf(pane, /Owns budgets/)).getByRole("button", { name: /Transferable/ })).toBeInTheDocument();
    expect(within(pane).queryByText("Fluent in a second language")).toBeNull();
    expect(pane.querySelectorAll(".fi-desc h4, .fi-desc .qmark-count, .fi-desc ul")).toHaveLength(0);
  });

  it("opens the reason and master lines in place under the line when its mark is clicked", async () => {
    setup({ role: vi.fn(async () => gradedDetail()) as unknown as PipelineApi["role"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    const line = await lineOf(pane, /Ran a project office/);
    fireEvent.click(within(line).getByRole("button", { name: /Proven/ }));
    expect(within(line).getByText("why 1")).toBeInTheDocument();
    expect(within(line).getByText(/Ran the office/)).toBeInTheDocument();
    expect(within(line).getByRole("button", { name: "Change the grade" })).toBeInTheDocument();
  });

  it("changes a required grade through the route and reads the role again", async () => {
    const correctRequirement = vi.fn(async () => ({}));
    const role = vi.fn(async () => gradedDetail());
    setup({ role: role as unknown as PipelineApi["role"], correctRequirement: correctRequirement as unknown as PipelineApi["correctRequirement"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    const line = await lineOf(pane, /Owns budgets/);
    fireEvent.click(within(line).getByRole("button", { name: /Transferable/ }));
    fireEvent.click(within(line).getByRole("button", { name: "Change the grade" }));
    fireEvent.change(within(line).getByLabelText("Class"), { target: { value: "P" } });
    fireEvent.change(within(line).getByLabelText("Why"), { target: { value: "did it" } });
    const calls = role.mock.calls.length;
    fireEvent.click(within(line).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(correctRequirement).toHaveBeenCalledWith("r1", 2, "P", "did it"));
    await waitFor(() => expect(role.mock.calls.length).toBeGreaterThan(calls));
  });

  it("marks the posting's duties too, each with its class and a read-only reason, and leaves an ungraded duty alone", async () => {
    const dutyPosting = "Responsibilities:\n- Run the weekly planning meeting\n- Own the roadmap\n- Keep the lights on\n\nQualifications:\n- Ran a project office for six years";
    const withDuties = () => {
      const d = gradedDetail();
      return {
        ...d, role: { ...d.role, description: dutyPosting }, preferred: [],
        responsibilities: [row(1, "Run the weekly planning meeting", "P"), row(2, "Own the roadmap", "U")],
      } as RoleDetail;
    };
    setup({ role: vi.fn(async () => withDuties()) as unknown as PipelineApi["role"] });
    const pane = await screen.findByRole("region", { name: "Role" });
    const run = await lineOf(pane, /Run the weekly planning meeting/);
    expect(within(run).getByRole("button", { name: /Proven/ })).toBeInTheDocument();
    expect(within(await lineOf(pane, /Own the roadmap/)).getByRole("button", { name: /Unproven/ })).toBeInTheDocument();
    expect(within(await lineOf(pane, /Ran a project office/)).getByRole("button", { name: /Proven/ })).toBeInTheDocument();
    fireEvent.click(within(run).getByRole("button", { name: /Proven/ }));
    expect(within(run).getByText("why 1")).toBeInTheDocument();
    expect(within(run).queryByRole("button", { name: "Change the grade" })).toBeNull();
    expect(within(pane).getByText("- Keep the lights on").className).toBe("posting-line");
  });

  it("keeps the plain paragraphs when the role has no graded items", async () => {
    setup();
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByText("First part.")).toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: /Proven|Transferable|Unproven/ })).toBeNull();
  });
});

// The ranked list and work in progress, drawn apart (docs/PRD.md 24.17), the one number on a new role's row (24.15) and the
// result of the check made when a role came into Ready to Launch (24.16). Everything is made up.

describe("the ranked list and work in progress, apart", () => {
  const RANKED = [
    card("u1", "first_impression", "First Impression", { score: 95, new: true, first_hours_bonus: 25, next: null }),
    card("s1", "second_impression", "Second Impression", { score: 80, next: null }),
    { ...READY, score: 70 },
  ];
  const WORK = [
    card("f1", "followup_needed", "Followup needed", { score: null, fit_pct: null, next: null }),
    card("z1", "cleansed_data", "Cleansed Data", { score: null, fit_pct: null, next: null }),
  ];
  const everyone = [...RANKED, ...WORK, READY, APPLIED];
  const column = (key: string, label: string, cards: BoardCard[]): Board => ({ columns: [{ key, label, count: cards.length, cards, page: 1, pages: 1 }] });
  const server = () =>
    vi.fn(async (key?: string) => (key === "ranked" ? column("ranked", "Ranked list", RANKED) : key === "work" ? column("work", "Work in progress", WORK) : board()));
  const roleOf = vi.fn(async (id: string) => detail(everyone.find((c) => c.id === id) ?? READY));
  const open = (extra: Partial<PipelineApi> = {}) => setup({ board: server() as unknown as PipelineApi["board"], role: roleOf as unknown as PipelineApi["role"], ...extra });
  const rows = () => Array.from(screen.getByRole("list", { name: "Roles" }).querySelectorAll(".fi-row"));
  const titles = () => rows().map((r) => r.querySelector(".fi-row-title")?.textContent ?? null);
  const stages = () => rows().map((r) => r.querySelector(".fi-badge-stage")?.textContent ?? null);

  it("sets them apart in the rail, each with its stages under it, and counts each from the server's groups", async () => {
    const { push } = open();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    const names = within(rail()).getAllByRole("button").map((b) => b.querySelector(".fi-stage-name")?.textContent);
    expect(names).toEqual([
      "Favorites", "Ready to Launch", "Applied", "Active Discussion",
      "Ranked list", "First Impression", "Second Impression",
      "Work in progress", "Added links", "Followup needed", "Landing Zone", "Cleansed Data",
      "Closed",
    ]);
    push("counts", { lanes: { ready_to_launch: 1, applied: 1 }, groups: { ranked: 12, work: 5 }, closed: {}, closed_total: 0, favorites: 0 });
    expect(within(rail()).getByRole("button", { name: /^Ranked list/ })).toHaveTextContent("12");
    expect(within(rail()).getByRole("button", { name: /^Work in progress/ })).toHaveTextContent("5");
    for (const name of [/^First Impression/, /^Second Impression/, /^Added links/, /^Followup needed/, /^Landing Zone/, /^Cleansed Data/]) {
      expect(within(rail()).getByRole("button", { name })).toHaveClass("is-nested");
    }
    for (const name of [/^Ranked list/, /^Work in progress/, /^Ready to Launch/, /^Closed/]) {
      expect(within(rail()).getByRole("button", { name })).not.toHaveClass("is-nested");
    }
  });

  it("opens the ranked list as one list from the server's ranked column, in its order, each row saying its stage", async () => {
    const { api } = open();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Ranked list" })).toBeInTheDocument();
    expect(api.board).toHaveBeenCalledWith("ranked", undefined, undefined, undefined, 1);
    await waitFor(() => expect(titles()).toEqual(["Role u1", "Role s1", "Role r1"]));
    expect(stages()).toEqual(["First Impression", "Second Impression", "Ready to Launch"]);
    expect(rows().map((r) => r.querySelector(".fi-fit")?.textContent)).toEqual(["95", "80", "70"]);
    expect(rows()[2].querySelector(".fi-badge-stage")).toHaveClass("is-yours");
    // The list is not Ready to Launch's own: Launch lives there.
    expect(screen.queryByRole("button", { name: "Launch in Browser" })).toBeNull();
  });

  it("opens work in progress apart from it: its own column, its roles not yet numbered, each saying its stage", async () => {
    const { api } = open();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Work in progress/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Work in progress" })).toBeInTheDocument();
    expect(api.board).toHaveBeenCalledWith("work", undefined, undefined, undefined, 1);
    await waitFor(() => expect(titles()).toEqual(["Role f1", "Role z1"]));
    expect(stages()).toEqual(["Followup needed", "Cleansed Data"]);
    expect(rows().map((r) => r.querySelector(".fi-fit")?.textContent)).toEqual(["–", "–"]);
    expect(screen.queryByText("Role u1")).toBeNull();
  });

  it("keeps each stage as a list of its own, a stage's roles without a stage badge on every row", async () => {
    open();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Applied/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Applied" })).toBeInTheDocument();
    expect(stages()).toEqual([null]);
  });

  it("says it is loading while a list is read, not that nothing is there", async () => {
    let answer!: (b: Board) => void;
    const slow = new Promise<Board>((resolve) => (answer = resolve));
    const read = vi.fn(async (key?: string) => (key === "ranked" ? slow : board()));
    open({ board: read as unknown as PipelineApi["board"] });
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    expect(await screen.findByText("Loading the pipeline…")).toBeInTheDocument();
    expect(screen.queryByText("Nothing here right now.")).toBeNull();
    await act(async () => answer(column("ranked", "Ranked list", RANKED)));
    await waitFor(() => expect(titles()).toEqual(["Role u1", "Role s1", "Role r1"]));
  });

  it("says a list could not be read, and reads it again when it is picked again", async () => {
    const read = vi.fn()
      .mockResolvedValueOnce(board())
      .mockRejectedValueOnce(new ApiError("The list is not ready.", 503))
      .mockResolvedValue(column("ranked", "Ranked list", RANKED));
    open({ board: read as unknown as PipelineApi["board"] });
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The list is not ready.");
    expect(screen.getByText("This list could not be read.")).toBeInTheDocument();
    fireEvent.click(within(rail()).getByRole("button", { name: /^Applied/ }));
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    await waitFor(() => expect(titles()).toEqual(["Role u1", "Role s1", "Role r1"]));
  });

  it("stops reading a list when another entry is picked, and reads it again when it is picked again", async () => {
    const { api, push } = open();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    await waitFor(() => expect(titles()).toEqual(["Role u1", "Role s1", "Role r1"]));
    fireEvent.click(within(rail()).getByRole("button", { name: /^Applied/ }));
    expect(await screen.findByRole("heading", { level: 1, name: "Applied" })).toBeInTheDocument();
    const read = api.board as ReturnType<typeof vi.fn>;
    read.mockClear();
    push("resync", {});
    await waitFor(() => expect(read).toHaveBeenCalledWith(undefined, undefined));
    expect(read.mock.calls.some(([key]) => key === "ranked")).toBe(false);
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    await waitFor(() => expect(read).toHaveBeenCalledWith("ranked", undefined, undefined, undefined, 1));
    await waitFor(() => expect(titles()).toEqual(["Role u1", "Role s1", "Role r1"]));
  });

  it("counts a search's matches in the group too, and dims a group with none", async () => {
    const search = vi.fn(async () => ({
      q: "co", page: 1, size: 100, pages: 1, total: 1, roles: 1, postings: 0, closed: 0, stages: { second_impression: 1 },
      items: [{ kind: "role", id: "s1", title: "Role s1", company: "Co s1", where: null, status: "second_impression", label: "Second Impression", lane: "second_impression", closed_reason: null, closed_label: null, reason: null, favorite: false, fit_pct: 80, found_at: null, url: null }],
    }));
    open({ search: search as unknown as PipelineApi["search"] });
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search every role" }), { target: { value: "co" } });
    expect(await screen.findByRole("heading", { level: 1, name: "1 match" })).toBeInTheDocument();
    expect(within(rail()).getByRole("button", { name: /^Ranked list/ })).toHaveTextContent("· 1");
    expect(within(rail()).getByRole("button", { name: /^Ranked list/ })).not.toHaveClass("is-dim");
    expect(within(rail()).getByRole("button", { name: /^Work in progress/ })).toHaveClass("is-dim");
  });
});

describe("the one number on a role", () => {
  const fresh = card("u1", "first_impression", "First Impression", { score: 90, new: true, first_hours_bonus: 20, next: null });
  const older = card("s1", "second_impression", "Second Impression", { score: 80, new: false });
  const read = () =>
    vi.fn(async (key?: string) =>
      key === "ranked" ? { columns: [{ key: "ranked", label: "Ranked list", count: 2, cards: [fresh, older], page: 1, pages: 1 }] } : board());
  const open = () =>
    setup({ board: read() as unknown as PipelineApi["board"], role: vi.fn(async (id: string) => detail(id === "u1" ? fresh : id === "s1" ? older : READY)) as unknown as PipelineApi["role"] });
  const openRanked = async () => {
    const { push } = open();
    await screen.findByRole("heading", { level: 1, name: "Ready to Launch" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Ranked list/ }));
    await within(await screen.findByRole("list", { name: "Roles" })).findByRole("button", { name: "Role u1" });
    return push;
  };

  it("prints the number the card has, and tags a new role New (+20) with the points its first hours are in it", async () => {
    await openRanked();
    const list = screen.getByRole("list", { name: "Roles" });
    const chips = list.querySelectorAll(".fi-fit");
    // Old: a bare New, a tooltip on the number and "Estimate 70: waits to be read · Its first hours add 20" under the
    // title. New (docs/PRD.md 24.18): the tag says it, in words nobody has to hover for, and no sentence repeats it.
    expect(chips[0]).toHaveTextContent("90");
    expect(chips[0]).toHaveAttribute("aria-label", "Fit 90");
    expect(chips[0]).not.toHaveAttribute("title");
    expect(within(list).getByText("New (+20)")).toHaveClass("fi-badge-new");
    expect(within(list).queryByText("New")).toBeNull();
    expect(list.querySelector(".fi-row-next")).toBeNull();
    expect(chips[1]).toHaveTextContent("80");
    expect(chips[1]).toHaveAttribute("aria-label", "Fit 80");
    expect(chips[1]).not.toHaveAttribute("title");
  });

  it("says it on the role's page too, under the Fit, for a new role only", async () => {
    await openRanked();
    const pane = screen.getByRole("region", { name: "Role" });
    expect(await within(pane).findByText("90%")).toBeInTheDocument();
    expect(within(pane).getByText("Its first hours count in this number")).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("list", { name: "Roles" })).getByRole("button", { name: "Role s1" }));
    expect(await within(pane).findByText("80%")).toBeInTheDocument();
    expect(within(pane).queryByText("Its first hours count in this number")).toBeNull();
  });
});

describe("the check made when a role came into Ready to Launch", () => {
  const checked = (next: string | null, extra: Partial<BoardCard> = {}) => ({ ...READY, next, ...extra });
  const open = (said: BoardCard) =>
    setup({
      board: vi.fn(async () => ({ ...board(), columns: [{ key: "ready_to_launch", label: "Ready to Launch", count: 1, cards: [said] }, ...board().columns.slice(1)] })) as unknown as PipelineApi["board"],
      role: vi.fn(async () => detail(said)) as unknown as PipelineApi["role"],
    });

  it.each([
    "Posting not confirmed when checked today",
    "The browser could not check the posting",
  ])("shows what is wrong with the check on the role's row and on its page, apart from what waits on the owner: %s", async (check) => {
    open(checked(`The packet failed · ${check}`, { packet: { id: "p1", version: 1, status: "failed" } }));
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByText(check)).toHaveClass("fi-launch-check");
    const list = screen.getByRole("list", { name: "Roles" });
    // The row: the packet's words keep the colour of what the owner must see; the check is its own quiet line.
    expect(within(list).getByText("The packet failed")).toHaveClass("fi-row-next", "is-closed");
    expect(within(list).getByText(check)).toHaveClass("fi-row-check");
    expect(within(list).getByText(check)).not.toHaveClass("is-closed");
    // The page: in the Ready box, under the packet's words, with the way in beside them.
    expect(within(pane).getByText("The packet failed")).toBeInTheDocument();
    expect(within(pane).getByRole("link", { name: "Open the application" })).toBeInTheDocument();
  });

  it("shows a posting the page did not confirm beside the building bar while the packet is written", async () => {
    const progress = { step: "checking" as const, label: "Its facts being checked", done: 1, of: 3, fraction: 0.33 };
    open(checked("Its facts being checked · Posting not confirmed when checked today", { packet: { id: "p1", version: 1, status: "writing", progress } }));
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByText("Posting not confirmed when checked today")).toHaveClass("fi-launch-check");
    expect(within(screen.getByRole("list", { name: "Roles" })).getByText("Posting not confirmed when checked today")).toHaveClass("fi-row-check");
  });

  it("says nothing of a check a role has no result of, or one that found the posting open", async () => {
    open(checked(null));
    const pane = await screen.findByRole("region", { name: "Role" });
    expect(await within(pane).findByRole("link", { name: "Open the application" })).toBeInTheDocument();
    expect(document.querySelector(".fi-row-check, .fi-launch-check")).toBeNull();
  });
});
