import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LiveHandler } from "../api/live";
import type { Board, BoardCard, LaneCounts, RoleDetail } from "../api/types/pipeline";
import { createPipelineStore, FAVORITES, type PipelineApi } from "./pipelineStore";
import { makeCard } from "./rowFixtures";

// The Pipeline's data layer against a made-up server: moves and stars show at once and are taken back when the server
// refuses, the live stream's messages change the lanes, and `resync` reads everything again. Everything is made up.

function card(id: string, status: string, extra: Partial<BoardCard> = {}): BoardCard {
  return makeCard({
    id, title: `Role ${id}`, company: `Co ${id}`, status, status_label: status, fit_pct: 50, score: 50,
    work: { mode: "remote", place: null, words: "Remote" }, pay: "Not stated", age: { days: 1, words: "1 day", from: "found" },
    origin: "scout", ...extra,
  });
}

/** The server's counts: each lane, and the two groups the page draws apart, which are sums of their lanes (docs/PRD.md 24.17). */
function counts(lanes: Record<string, number>, favorites = 0): LaneCounts {
  const sum = (...keys: string[]) => keys.reduce((n, k) => n + (lanes[k] ?? 0), 0);
  const groups = { work: sum("followup_needed", "landing_zone", "cleansed_data"), ranked: sum("first_impression", "second_impression", "ready_to_launch") };
  return { lanes, groups, closed: {}, closed_total: lanes.closed ?? 0, favorites };
}

function board(): Board {
  return {
    columns: [
      { key: "second_impression", label: "Second Impression", count: 2, cards: [card("a", "second_impression", { score: 80 }), card("b", "second_impression", { score: 60 })] },
      { key: "ready_to_launch", label: "Ready to Launch", count: 1, cards: [card("c", "ready_to_launch")] },
      { key: "applied", label: "Applied", count: 0, cards: [] },
      { key: "closed", label: "Closed", count: 0, cards: [] },
    ],
    ended_reasons: [{ reason: "wrong_level", label: "Wrong level" }],
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup(overrides: Partial<PipelineApi> = {}) {
  let push: LiveHandler = () => {};
  const api = {
    board: vi.fn(async () => board()),
    favorites: vi.fn(async () => ({ key: FAVORITES, label: "Favorites", count: 0, cards: [] as BoardCard[] })),
    role: vi.fn(),
    roleRow: vi.fn(),
    search: vi.fn(),
    setFavorite: vi.fn(),
    moveRole: vi.fn(),
    moveBack: vi.fn(),
    applyGo: vi.fn(),
    applyGoProgress: vi.fn(async () => ({ phase: "idle", total: 0, built: 0, opened: [], skipped: [], notes: [], done: true })),
    ...overrides,
  } as unknown as PipelineApi;
  const close = vi.fn();
  const store = createPipelineStore({
    api,
    openLive: (onEvent) => {
      push = onEvent;
      return { close };
    },
  });
  return { store, api, push: ((e, d) => push(e, d)) as LiveHandler, close };
}

async function started(overrides: Partial<PipelineApi> = {}) {
  const s = setup(overrides);
  s.store.start();
  await vi.waitFor(() => expect(s.store.getState().status).toBe("ready"));
  return s;
}

const ids = (store: ReturnType<typeof createPipelineStore>, lane: string) => store.getState().lanes[lane].cards.map((c) => c.id);

describe("the pipeline store", () => {
  it("loads the board and the favorites into lanes when a screen opens, and closes the stream when the last one goes", async () => {
    const { store, close } = setup();
    const stop = store.start();
    await vi.waitFor(() => expect(store.getState().status).toBe("ready"));
    expect(ids(store, "second_impression")).toEqual(["a", "b"]);
    expect(store.getState().lanes[FAVORITES].cards).toEqual([]);
    expect(store.getState().endedReasons).toEqual([{ reason: "wrong_level", label: "Wrong level" }]);
    stop();
    expect(close).toHaveBeenCalled();
  });

  it("moves a role at once, and keeps the server's card and counts when it agrees", async () => {
    const answer = deferred<BoardCard & { counts: LaneCounts }>();
    const { store, push } = await started({ moveRole: vi.fn(() => answer.promise) as PipelineApi["moveRole"] });
    push("hello", { counts: counts({ second_impression: 2, ready_to_launch: 1, applied: 0, closed: 0 }) });
    const going = store.move("a", "ready_to_launch");
    // At once, before the server answers.
    expect(ids(store, "second_impression")).toEqual(["b"]);
    expect(ids(store, "ready_to_launch")).toEqual(["a", "c"]);
    expect(store.getState().counts?.lanes).toMatchObject({ second_impression: 1, ready_to_launch: 2 });
    expect(store.getState().busy.a).toBe(true);
    answer.resolve({ ...card("a", "ready_to_launch", { score: 80, next: "The packet failed" }), counts: counts({ second_impression: 1, ready_to_launch: 2, applied: 0, closed: 0 }) });
    expect(await going).toBe(true);
    expect(store.getState().lanes.ready_to_launch.cards[0].next).toBe("The packet failed");
    expect(store.getState().busy.a).toBeUndefined();
  });

  it("puts a refused move back where it was, at the same place, with its counts, and says why", async () => {
    const { store, push } = await started({
      moveRole: vi.fn(async () => {
        throw new ApiError("That role can't be moved now.", 409);
      }) as PipelineApi["moveRole"],
    });
    push("hello", { counts: counts({ second_impression: 2, ready_to_launch: 1, applied: 0, closed: 0 }) });
    expect(await store.move("a", "applied")).toBe(false);
    expect(ids(store, "second_impression")).toEqual(["a", "b"]);
    expect(ids(store, "applied")).toEqual([]);
    expect(store.getState().counts?.lanes).toMatchObject({ second_impression: 2, applied: 0 });
    expect(store.getState().lanes.second_impression.count).toBe(2);
    expect(store.getState().notice).toBe("That role can't be moved now.");
    store.dismissNotice();
    expect(store.getState().notice).toBeNull();
  });

  it("stars a role at once (Favorites first) and takes the star back when the server refuses", async () => {
    const { store, push } = await started({
      setFavorite: vi.fn(async () => {
        throw new ApiError("Not now.", 409);
      }) as PipelineApi["setFavorite"],
    });
    push("hello", { counts: counts({ second_impression: 2 }, 0) });
    const going = store.toggleFavorite("b");
    expect(ids(store, FAVORITES)).toEqual(["b"]);
    expect(store.getState().lanes.second_impression.cards[1].favorite).toBe(true);
    expect(store.getState().counts?.favorites).toBe(1);
    expect(await going).toBe(false);
    expect(ids(store, FAVORITES)).toEqual([]);
    expect(store.getState().lanes.second_impression.cards[1].favorite).toBe(false);
    expect(store.getState().counts?.favorites).toBe(0);
  });

  it("a confirmed star that fast-tracks the role moves it to Ready to Launch with the server's counts", async () => {
    const moved = card("b", "ready_to_launch", { favorite: true, score: 60 });
    const { store } = await started({
      setFavorite: vi.fn(async () => ({ favorite: true, changed: true, moved: true, research: null, packet: null, role: moved, counts: counts({ second_impression: 1, ready_to_launch: 2 }, 1) })) as PipelineApi["setFavorite"],
    });
    expect(await store.toggleFavorite("b")).toBe(true);
    expect(ids(store, "second_impression")).toEqual(["a"]);
    expect(ids(store, "ready_to_launch")).toContain("b");
    expect(store.getState().lanes[FAVORITES].cards[0].status).toBe("ready_to_launch");
    expect(store.getState().counts?.favorites).toBe(1);
  });

  it("applies the live stream: counts, a role changing lane, a star from elsewhere and a packet's progress", async () => {
    const { store, push } = await started();
    push("counts", counts({ second_impression: 7, ready_to_launch: 3, applied: 1, closed: 4 }, 2));
    expect(store.getState().counts?.lanes.second_impression).toBe(7);
    expect(store.getState().lanes.closed.count).toBe(4);
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "second_impression", to: "applied", lane_to: "applied",
      label: "Applied", closed_reason: null, reason: null, favorite: true, changed: ["status", "favorite"],
    });
    expect(ids(store, "second_impression")).toEqual(["b"]);
    expect(ids(store, "applied")).toEqual(["a"]);
    expect(store.getState().lanes.applied.cards[0].status_label).toBe("Applied");
    expect(ids(store, FAVORITES)).toEqual(["a"]);
    push("packet", { id: "p1", role_id: "c", version: 1, status: "writing", from: null, progress: { step: "checking", label: "Writing the resume", done: 1, of: 3, fraction: 0.33 } });
    expect(store.getState().lanes.ready_to_launch.cards[0].packet?.progress?.label).toBe("Writing the resume");
  });

  it("reads a role on the page again when the stream says its stage or star changed, and shows the server's words", async () => {
    const said = card("a", "applied", { score: 80, next: "Pay mostly below your floor: $60,000–$80,000", favorite: true, favorited_at: "2026-10-23T10:00:00Z" });
    const roleRow = vi.fn(async () => said);
    const { store, push, api } = await started({ roleRow: roleRow as unknown as PipelineApi["roleRow"] });
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "second_impression", to: "applied", lane_to: "applied",
      label: "Applied", closed_reason: null, reason: null, favorite: true, changed: ["status", "favorite"],
    });
    // At once: the stage and the star the event names.
    expect(ids(store, "applied")).toEqual(["a"]);
    expect(store.getState().lanes.applied.cards[0].favorite).toBe(true);
    // Then the server's row, with its sentence, in the card.
    await vi.waitFor(() => expect(store.getState().lanes.applied.cards[0].next).toBe("Pay mostly below your floor: $60,000–$80,000"));
    expect(roleRow).toHaveBeenCalledWith("a");
    expect(api.role, "a card on the page costs one row, not the role's whole page").not.toHaveBeenCalled();
    expect(store.getState().lanes[FAVORITES].cards.map((c) => c.id)).toEqual(["a"]);
    expect(store.getState().lanes[FAVORITES].cards[0].favorited_at).toBe("2026-10-23T10:00:00Z");
  });

  it("reads the whole page of a role that is open when its stage changes, and the card takes the row at its top", async () => {
    const said = card("a", "applied", { next: "Pay mostly below your floor: $60,000–$80,000" });
    const page = { role: said } as unknown as RoleDetail;
    const role = vi.fn(async () => page);
    const roleRow = vi.fn();
    const { store, push } = await started({
      role: role as unknown as PipelineApi["role"], roleRow: roleRow as unknown as PipelineApi["roleRow"],
    });
    store.openRole("a");
    await vi.waitFor(() => expect(store.getState().details.a?.status).toBe("ready"));
    role.mockClear();
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "second_impression", to: "applied", lane_to: "applied",
      label: "Applied", closed_reason: null, reason: null, favorite: false, changed: ["status"],
    });
    await vi.waitFor(() => expect(store.getState().lanes.applied.cards[0].next).toBe("Pay mostly below your floor: $60,000–$80,000"));
    expect(role).toHaveBeenCalledTimes(1);
    expect(roleRow, "its page is read, and its top is the same row").not.toHaveBeenCalled();
  });

  it("does not read a role again for a change that touched neither its stage nor its star", async () => {
    const roleRow = vi.fn();
    const { push, api } = await started({ roleRow: roleRow as unknown as PipelineApi["roleRow"] });
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "second_impression", to: "second_impression", lane_to: "second_impression",
      label: "Second Impression", closed_reason: null, reason: null, favorite: false, changed: [],
    });
    expect(roleRow).not.toHaveBeenCalled();
    expect(api.role).not.toHaveBeenCalled();
  });

  it("reads a role again when its packet changed state, and not while it only moves on a step", async () => {
    // A packet that fails is one of the few a row says anything of (docs/PRD.md 24.18): a packet written says nothing.
    const failed = card("c", "ready_to_launch", { next: "The packet failed", packet: { id: "p1", version: 1, status: "failed" } });
    const roleRow = vi.fn(async () => failed);
    const { store, push } = await started({ roleRow: roleRow as unknown as PipelineApi["roleRow"] });
    const progress = (step: "asked" | "checking" | "done", label: string) => ({ step, label, done: 1, of: 3, fraction: 0.33 });
    push("packet", { id: "p1", role_id: "c", version: 1, status: "writing", from: "writing", progress: progress("checking", "Its facts being checked") });
    expect(store.getState().lanes.ready_to_launch.cards[0].packet?.progress?.label).toBe("Its facts being checked");
    expect(roleRow).not.toHaveBeenCalled();
    push("packet", { id: "p1", role_id: "c", version: 1, status: "failed", from: "writing", progress: progress("done", "Not written") });
    await vi.waitFor(() => expect(store.getState().lanes.ready_to_launch.cards[0].next).toBe("The packet failed"));
    expect(roleRow).toHaveBeenCalledWith("c");
  });

  it("reads a role it does not hold when the stream says it arrived in a lane on screen", async () => {
    const { store, push, api } = await started({ roleRow: vi.fn(async () => card("n", "ready_to_launch", { score: 99 })) as unknown as PipelineApi["roleRow"] });
    push("role", {
      id: "n", title: "Role n", company: "Co n", from: null, to: "ready_to_launch", lane_to: "ready_to_launch",
      label: "Ready to Launch", closed_reason: null, reason: null, favorite: false, changed: ["new"],
    });
    await vi.waitFor(() => expect(ids(store, "ready_to_launch")).toEqual(["n", "c"]));
    expect(api.roleRow).toHaveBeenCalledWith("n");
  });

  it("reads the board again on resync", async () => {
    const { store, push, api } = await started();
    const changed = board();
    changed.columns[0].cards = [card("z", "second_impression")];
    changed.columns[0].count = 1;
    (api.board as ReturnType<typeof vi.fn>).mockResolvedValueOnce(changed);
    push("resync", {});
    await vi.waitFor(() => expect(ids(store, "second_impression")).toEqual(["z"]));
    expect(api.board).toHaveBeenCalledTimes(2);
    expect(api.favorites).toHaveBeenCalledTimes(2);
  });

  it("narrowed to a target role, reads the lanes for it and keeps their own counts over the server's", async () => {
    const { store, push, api } = setup({ profile: vi.fn(async () => ({ criteria: { target_roles: [{ id: "builders", name: "Builders" }] } })) as unknown as PipelineApi["profile"] });
    store.setTargetRole("builders");
    store.start();
    await vi.waitFor(() => expect(store.getState().status).toBe("ready"));
    expect(api.board).toHaveBeenCalledWith(undefined, "builders");
    await vi.waitFor(() => expect(store.getState().targetRole).toEqual({ id: "builders", name: "Builders" }));
    push("counts", counts({ second_impression: 90, ready_to_launch: 40 }, 1));
    expect(store.getState().lanes.second_impression.count).toBe(2);
    push("role", {
      id: "elsewhere", title: "x", company: "y", from: null, to: "ready_to_launch", lane_to: "ready_to_launch",
      label: "Ready to Launch", closed_reason: null, reason: null, favorite: false, changed: ["new"],
    });
    expect(api.role).not.toHaveBeenCalled();
    expect(api.roleRow).not.toHaveBeenCalled();
  });

  it("keeps only the newest search's answer", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    const search = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { store } = await started({ search: search as unknown as PipelineApi["search"] });
    const a = store.search("acme");
    const b = store.search("acme remote");
    second.resolve({ q: "acme remote", total: 1, items: [], stages: {}, page: 1, size: 100, pages: 1, roles: 1, postings: 0, closed: 0 });
    first.resolve({ q: "acme", total: 9, items: [], stages: {}, page: 1, size: 100, pages: 1, roles: 9, postings: 0, closed: 0 });
    await Promise.all([a, b]);
    expect(store.getState().search.result?.total).toBe(1);
    await store.search("");
    expect(store.getState().search.status).toBe("idle");
  });

  it("adds a job by its link: the answer comes back and the roles are read again, unless the owner already had it", async () => {
    const added = { role: null, posting: "p1", already: false, said: "It is in Landing Zone.", stage: "landing_zone", stage_label: "Landing Zone", title: "T", company: "C" };
    const addRole = vi.fn().mockResolvedValueOnce(added).mockResolvedValueOnce({ ...added, role: "c", already: true });
    const { store, api } = await started({ addRole } as unknown as Partial<PipelineApi>);
    const boardCalls = (api.board as ReturnType<typeof vi.fn>).mock.calls.length;
    expect(await store.addRole("https://jobs.example.com/1")).toEqual(added);
    expect(addRole).toHaveBeenCalledWith("https://jobs.example.com/1");
    await vi.waitFor(() => expect((api.board as ReturnType<typeof vi.fn>).mock.calls.length).toBe(boardCalls + 1));
    await store.addRole("https://jobs.example.com/2");
    expect((api.board as ReturnType<typeof vi.fn>).mock.calls.length).toBe(boardCalls + 1);
  });

  it("a refused link is thrown, in the server's words, and nothing is read again", async () => {
    const addRole = vi.fn().mockRejectedValue(new ApiError("That is not a link to a job.", 422));
    const { store, api } = await started({ addRole } as unknown as Partial<PipelineApi>);
    const boardCalls = (api.board as ReturnType<typeof vi.fn>).mock.calls.length;
    await expect(store.addRole("nope")).rejects.toThrow("That is not a link to a job.");
    expect((api.board as ReturnType<typeof vi.fn>).mock.calls.length).toBe(boardCalls);
  });
});

// The two groups the page draws apart (docs/PRD.md 24.17): the ranked list (First Impression, Second Impression and Ready to
// Launch as one list by the one number) and work in progress (Followup needed, Landing Zone, Cleansed Data). The server gives
// each as one column of its own; the store reads it when a screen asks, keeps it with the board, and moves its cards with
// the lanes' own.

const GROUP_LABELS = { ranked: "Ranked list", work: "Work in progress" };

function groupColumn(key: "ranked" | "work", cards: BoardCard[], count = cards.length, page = 1, pages = 1): Board {
  return { columns: [{ key, label: GROUP_LABELS[key], count, cards, page, pages }] };
}

/** The server: the whole board for no column, and a group for its key. `groups` is what each key answers, per page. */
function server(groups: Partial<Record<"ranked" | "work", (page: number) => Board>>, whole: () => Board = board) {
  return vi.fn(async (column?: string, _role?: string, _limit?: number, _reason?: string, page?: number) => {
    const group = column === "ranked" || column === "work" ? groups[column] : undefined;
    return group ? group(page ?? 1) : whole();
  });
}

const RANKED_TOP = () => [
  card("u1", "first_impression", { score: 95, status_label: "First Impression" }),
  card("a", "second_impression", { score: 80, status_label: "Second Impression" }),
  card("c", "ready_to_launch", { score: 70, status_label: "Ready to Launch" }),
];

describe("the pipeline store's groups", () => {
  it("reads the ranked list as one column, in the server's order, when a screen asks for it, and not before", async () => {
    const read = server({ ranked: () => groupColumn("ranked", RANKED_TOP(), 120, 1, 3) });
    const { store } = await started({ board: read as unknown as PipelineApi["board"] });
    expect(store.getState().lanes.ranked).toBeUndefined();
    expect(read.mock.calls.some(([column]) => column === "ranked")).toBe(false);
    await store.loadGroup("ranked");
    expect(read).toHaveBeenCalledWith("ranked", undefined, undefined, undefined, 1);
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c"]);
    expect(store.getState().lanes.ranked).toMatchObject({ label: "Ranked list", count: 120, page: 1 });
    expect(store.getState().groupStatus.ranked).toBe("ready");
  });

  it("keeps a group it was asked for when the board is read again, narrowed to a target role too", async () => {
    const read = server({ work: () => groupColumn("work", [card("f1", "followup_needed", { score: null })]) });
    const { store, push } = await started({
      board: read as unknown as PipelineApi["board"],
      profile: vi.fn(async () => ({ criteria: { target_roles: [{ id: "builders", name: "Builders" }] } })) as unknown as PipelineApi["profile"],
    });
    await store.loadGroup("work");
    read.mockClear();
    push("resync", {});
    await vi.waitFor(() => expect(read).toHaveBeenCalledWith("work", undefined, undefined, undefined, 1));
    expect(ids(store, "work")).toEqual(["f1"]);
    store.setTargetRole("builders");
    await vi.waitFor(() => expect(read).toHaveBeenCalledWith("work", "builders", undefined, undefined, 1));
  });

  it("reads a group asked for while the board is still loading, as soon as the board is there", async () => {
    const read = server({ work: () => groupColumn("work", [card("f1", "followup_needed", { score: null })]) });
    const { store } = setup({ board: read as unknown as PipelineApi["board"] });
    store.start();
    void store.loadGroup("work");
    await vi.waitFor(() => expect(ids(store, "work")).toEqual(["f1"]));
    expect(store.getState().status).toBe("ready");
  });

  it("pages a group as it pages a lane: the next page of the server's column is added, nothing twice", async () => {
    const read = server({
      ranked: (page) => (page === 1 ? groupColumn("ranked", RANKED_TOP(), 4, 1, 2) : groupColumn("ranked", [RANKED_TOP()[2], card("u2", "first_impression", { score: 60 })], 4, 2, 2)),
    });
    const { store } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    await store.loadMore("ranked");
    expect(read).toHaveBeenCalledWith("ranked", undefined, undefined, undefined, 2);
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c", "u2"]);
    expect(store.getState().lanes.ranked.page).toBe(2);
  });

  it("follows the server's counts: a group's total is the group's count, not a lane's", async () => {
    const read = server({ ranked: () => groupColumn("ranked", RANKED_TOP()) });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    push("counts", counts({ first_impression: 9, second_impression: 2, ready_to_launch: 1, followup_needed: 4 }, 0));
    expect(store.getState().lanes.ranked.count).toBe(12);
    expect(store.getState().counts?.groups).toEqual({ ranked: 12, work: 4 });
  });

  it("a role moved out of the ranked list leaves it at once and takes the group's count down; refused, it comes back with the count", async () => {
    const refused = vi.fn(async () => {
      throw new ApiError("That role can't be moved now.", 409);
    });
    const read = server({ ranked: () => groupColumn("ranked", RANKED_TOP(), 3) });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"], moveRole: refused as unknown as PipelineApi["moveRole"] });
    await store.loadGroup("ranked");
    push("hello", { counts: counts({ first_impression: 1, second_impression: 1, ready_to_launch: 1, applied: 0 }) });
    const going = store.move("a", "applied");
    expect(ids(store, "ranked")).toEqual(["u1", "c"]);
    expect(store.getState().lanes.ranked.count).toBe(2);
    expect(store.getState().counts?.groups.ranked).toBe(2);
    expect(await going).toBe(false);
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c"]);
    expect(store.getState().lanes.ranked.count).toBe(3);
    expect(store.getState().counts?.groups.ranked).toBe(3);
  });

  it("a role moved from one ranked stage to another stays in the list, in its place, and says its new stage; the group's count is the same", async () => {
    const answer = deferred<BoardCard & { counts: LaneCounts }>();
    const read = server({ ranked: () => groupColumn("ranked", RANKED_TOP(), 3) });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"], moveRole: vi.fn(() => answer.promise) as PipelineApi["moveRole"] });
    await store.loadGroup("ranked");
    push("hello", { counts: counts({ first_impression: 1, second_impression: 1, ready_to_launch: 1 }) });
    const going = store.move("a", "ready_to_launch");
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c"]);
    expect(store.getState().lanes.ranked.cards[1]).toMatchObject({ status: "ready_to_launch", status_label: "Ready to Launch" });
    expect(store.getState().counts?.groups.ranked).toBe(3);
    answer.resolve({ ...card("a", "ready_to_launch", { score: 80, next: "The packet failed" }), counts: counts({ first_impression: 1, second_impression: 0, ready_to_launch: 2 }) });
    expect(await going).toBe(true);
    expect(store.getState().lanes.ranked.cards[1].next).toBe("The packet failed");
    expect(store.getState().lanes.ranked.count).toBe(3);
  });

  it("a role that comes into the ranked list from another stage joins it, in the place its number gives it, and the count follows", async () => {
    const stray = card("p", "applied", { score: 85 });
    const whole = (): Board => ({ ...board(), columns: board().columns.map((c) => (c.key === "applied" ? { ...c, count: 1, cards: [stray] } : c)) });
    const read = server({ ranked: () => groupColumn("ranked", RANKED_TOP(), 3) }, whole);
    const { store, push } = await started({
      board: read as unknown as PipelineApi["board"],
      moveRole: vi.fn(async () => ({ ...card("p", "first_impression", { score: 85 }), counts: counts({ first_impression: 2, second_impression: 1, ready_to_launch: 1, applied: 0 }) })) as unknown as PipelineApi["moveRole"],
    });
    await store.loadGroup("ranked");
    push("hello", { counts: counts({ first_impression: 1, second_impression: 1, ready_to_launch: 1, applied: 1 }) });
    expect(await store.move("p", "first_impression")).toBe(true);
    expect(ids(store, "ranked")).toEqual(["u1", "p", "a", "c"]);
    expect(store.getState().lanes.ranked.count).toBe(4);
  });

  it("the live stream moves a role in the group too: its new stage on the card, and gone when it left the list", async () => {
    const read = server({ ranked: () => groupColumn("ranked", RANKED_TOP(), 3) });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "second_impression", to: "ready_to_launch", lane_to: "ready_to_launch",
      label: "Ready to Launch", closed_reason: null, reason: null, favorite: false, changed: [],
    });
    expect(store.getState().lanes.ranked.cards[1]).toMatchObject({ id: "a", status: "ready_to_launch", status_label: "Ready to Launch" });
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "ready_to_launch", to: "applied", lane_to: "applied",
      label: "Applied", closed_reason: null, reason: null, favorite: false, changed: [],
    });
    expect(ids(store, "ranked")).toEqual(["u1", "c"]);
  });

  it("moves a role that only the group's list holds: the card in the group changes, with no lane's cards to find it in", async () => {
    const far = card("far", "first_impression", { score: 40 });
    const read = server({ ranked: () => groupColumn("ranked", [...RANKED_TOP(), far], 4) });
    const { store } = await started({
      board: read as unknown as PipelineApi["board"],
      moveRole: vi.fn(async () => ({ ...card("far", "applied"), counts: counts({ applied: 1 }) })) as unknown as PipelineApi["moveRole"],
    });
    await store.loadGroup("ranked");
    expect(await store.move("far", "applied")).toBe(true);
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c"]);
  });

  it("says in words when a group cannot be read, and reads it again when asked again", async () => {
    const read = vi.fn()
      .mockResolvedValueOnce(board())
      .mockRejectedValueOnce(new ApiError("The list is not ready.", 503))
      .mockResolvedValue(groupColumn("ranked", RANKED_TOP()));
    const { store } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    expect(store.getState().groupStatus.ranked).toBe("error");
    expect(store.getState().notice).toBe("The list is not ready.");
    expect(store.getState().lanes.ranked).toBeUndefined();
    await store.loadGroup("ranked");
    expect(store.getState().groupStatus.ranked).toBe("ready");
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c"]);
  });
});

describe("the pipeline store's groups, when a read of one fails behind the page", () => {
  it("keeps the board and the group's last list when the board is read again and the group's read fails", async () => {
    let failing = false;
    const read = vi.fn(async (column?: string) => {
      if (column !== "ranked") return board();
      if (failing) throw new ApiError("Not now.", 503);
      return groupColumn("ranked", RANKED_TOP(), 3);
    });
    const { store } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    failing = true;
    await store.reload();
    expect(read.mock.calls.filter(([c]) => c === "ranked").length).toBe(2);
    expect(store.getState().status).toBe("ready");
    expect(store.getState().error).toBeNull();
    expect(ids(store, "second_impression")).toEqual(["a", "b"]);
    expect(ids(store, "ranked")).toEqual(["u1", "a", "c"]);
  });
});

describe("the pipeline store's groups, when a read of the board overlaps a read of one", () => {
  const RANKED_ROWS = () => [card("u1", "first_impression", { score: 95 }), card("a", "second_impression", { score: 80 })];

  it("is never left on loading: a group read the board's read overtook, whose own read fails, says it could not be read, and is read when asked again", async () => {
    const first = deferred<Board>();
    let asked = 0;
    const read = vi.fn(async (column?: string) => {
      if (column !== "ranked") return board();
      asked += 1;
      if (asked === 1) return first.promise;
      if (asked === 2) throw new ApiError("Not now.", 503);
      return groupColumn("ranked", RANKED_ROWS(), 2);
    });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"] });
    const loading = store.loadGroup("ranked");
    expect(store.getState().groupStatus.ranked).toBe("loading");
    push("resync", {}); // the board is read again while the first read of the list is on
    await vi.waitFor(() => expect(asked).toBe(2));
    first.resolve(groupColumn("ranked", RANKED_ROWS(), 2)); // overtaken: the board's read answers for it
    await loading;
    await vi.waitFor(() => expect(store.getState().groupStatus.ranked).toBe("error"));
    expect(store.getState().lanes.ranked).toBeUndefined();
    await store.loadGroup("ranked"); // picked again
    expect(store.getState().groupStatus.ranked).toBe("ready");
    expect(ids(store, "ranked")).toEqual(["u1", "a"]);
  });

  it("is not left on loading when the overtaking read of the board fails altogether", async () => {
    const first = deferred<Board>();
    let boards = 0;
    const read = vi.fn(async (column?: string) => {
      if (column === "ranked") return first.promise;
      boards += 1;
      if (boards === 2) throw new ApiError("The server is busy.", 503);
      return board();
    });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"] });
    void store.loadGroup("ranked");
    push("resync", {});
    await vi.waitFor(() => expect(store.getState().status).toBe("error"));
    first.resolve(groupColumn("ranked", RANKED_ROWS(), 2));
    await vi.waitFor(() => expect(store.getState().groupStatus.ranked).toBe("error"));
    expect(store.getState().lanes.ranked).toBeUndefined();
  });

  it("does not say a list could not be read when the board's read that overtook its own read brought the list", async () => {
    const first = deferred<Board>();
    let asked = 0;
    const read = vi.fn(async (column?: string) => {
      if (column !== "ranked") return board();
      asked += 1;
      return asked === 1 ? first.promise : groupColumn("ranked", RANKED_ROWS(), 2);
    });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"] });
    const loading = store.loadGroup("ranked");
    push("resync", {});
    await vi.waitFor(() => expect(ids(store, "ranked")).toEqual(["u1", "a"]));
    first.reject(new ApiError("Too late.", 503));
    await loading;
    expect(store.getState().groupStatus.ranked).toBe("ready");
    expect(store.getState().notice).toBeNull();
  });
});

describe("the pipeline store's groups, when the screen lets one go", () => {
  const WORK_ROWS = () => [card("f1", "followup_needed", { score: null })];
  const RANKED_ROWS = () => [card("u1", "first_impression", { score: 95 })];

  it("reads again, with the board, only the groups that are still shown: a list let go is not sorted on the server for nothing", async () => {
    const read = server({ ranked: () => groupColumn("ranked", RANKED_ROWS()), work: () => groupColumn("work", WORK_ROWS()) });
    const { store } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    await store.loadGroup("work");
    store.releaseGroup("ranked");
    expect(store.getState().groupStatus.ranked).toBeUndefined();
    read.mockClear();
    await store.reload();
    const asked = read.mock.calls.map(([column]) => column).filter(Boolean);
    expect(asked).toEqual(["work"]);
    expect(store.getState().lanes.ranked).toBeUndefined();
    expect(ids(store, "work")).toEqual(["f1"]);
    expect(store.getState().groupStatus).toEqual({ work: "ready" });
  });

  it("reads a group again when it is picked again, and reads it with the board from then on", async () => {
    const read = server({ ranked: () => groupColumn("ranked", RANKED_ROWS()) });
    const { store } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    store.releaseGroup("ranked");
    await store.reload();
    read.mockClear();
    await store.loadGroup("ranked");
    expect(read).toHaveBeenCalledWith("ranked", undefined, undefined, undefined, 1);
    expect(ids(store, "ranked")).toEqual(["u1"]);
    read.mockClear();
    await store.reload();
    expect(read).toHaveBeenCalledWith("ranked", undefined, undefined, undefined, 1);
  });

  it("drops a read of a group that comes back after it was let go, and leaves it on nothing", async () => {
    const slow = deferred<Board>();
    const { store } = await started({ board: vi.fn(async (column?: string) => (column === "ranked" ? slow.promise : board())) as unknown as PipelineApi["board"] });
    const loading = store.loadGroup("ranked");
    store.releaseGroup("ranked");
    slow.resolve(groupColumn("ranked", RANKED_ROWS()));
    await loading;
    expect(store.getState().lanes.ranked).toBeUndefined();
    expect(store.getState().groupStatus.ranked).toBeUndefined();
  });

  it("keeps the list it let go as it was, moving with the roles, until the board is read next", async () => {
    const read = server({ ranked: () => groupColumn("ranked", [card("u1", "first_impression", { score: 95 }), card("a", "second_impression", { score: 80 })], 2) });
    const { store, push } = await started({ board: read as unknown as PipelineApi["board"] });
    await store.loadGroup("ranked");
    store.releaseGroup("ranked");
    push("role", {
      id: "a", title: "Role a", company: "Co a", from: "second_impression", to: "applied", lane_to: "applied",
      label: "Applied", closed_reason: null, reason: null, favorite: false, changed: [],
    });
    expect(ids(store, "ranked")).toEqual(["u1"]);
  });
});

describe("the pipeline store, Launch in Browser's progress", () => {
  const go = (phase: string, built: number, done: boolean) => ({ phase, total: 3, built, opened: [], skipped: [], notes: [], done });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads it at start, on changed go, on resync, and sets no timer", async () => {
    vi.useFakeTimers();
    const applyGoProgress = vi.fn(async () => go("building", 0, false));
    const { store, push } = setup({ applyGoProgress: applyGoProgress as unknown as PipelineApi["applyGoProgress"] });
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(applyGoProgress).toHaveBeenCalledTimes(1);
    expect(store.getState().launch?.built).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(applyGoProgress).toHaveBeenCalledTimes(1);

    applyGoProgress.mockImplementation(async () => go("building", 2, false));
    push("changed", { area: "go", ids: [] });
    await vi.advanceTimersByTimeAsync(0);
    expect(applyGoProgress).toHaveBeenCalledTimes(2);
    expect(store.getState().launch?.built).toBe(2);

    push("changed", { area: "jobs", ids: [] });
    await vi.advanceTimersByTimeAsync(0);
    expect(applyGoProgress).toHaveBeenCalledTimes(2);

    applyGoProgress.mockImplementation(async () => go("done", 3, true));
    push("resync", {});
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getState().launch?.done).toBe(true);
  });

  it("after the button is clicked it shows the answer and sets no timer", async () => {
    vi.useFakeTimers();
    const applyGo = vi.fn(async () => go("checking", 0, false));
    const { store } = setup({ applyGo: applyGo as unknown as PipelineApi["applyGo"] });
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getState().launch).toBeNull();
    await store.launch();
    expect(store.getState().launch?.phase).toBe("checking");
    expect(vi.getTimerCount()).toBe(0);
  });
});
