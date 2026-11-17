import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LiveHandler } from "../api/live";
import type { Employer, EmployerCounts, EmployerPage } from "../api/types/employers";
import { createEmployersStore, FIND_MS, type EmployersApi } from "./employersStore";

// Employers' data layer against a made-up server: it reads the list a page at a time in a view, narrows it on the
// server, shows a watch and a hard pass at once (and takes them back, saying so, when refused), takes the rows a merge
// or split answers with in place, and follows the live stream's employer messages. No real company.

function employer(id: string, extra: Partial<Employer> = {}): Employer {
  return {
    id, name: `Co ${id}`, aliases: [], website: null, careers_url: null, reviews_url: null, headquarters: null, offices: [],
    local: null, work_style: "unknown", employees: null, founded: null, industry: null, sectors: [], ownership: "unknown",
    ai_native: null, events: [], group: null, notes: null, sources: {},
    roles_total: 1, roles_open: 1, roles_ahead: 0, site: { id: `s-${id}`, url: `https://${id}.test`, on: false, state: "off", needs: null, status: "" },
    ...extra,
  };
}

const COUNTS: EmployerCounts = { all: 3, open: 2, watched: 0, hard_pass: 0, local: 1 };

function page(rows: Employer[], extra: Partial<EmployerPage> = {}): EmployerPage {
  return { employers: rows, next: null, total: rows.length, counts: COUNTS, industries: ["Robotics"], ...extra };
}

function setup(overrides: Partial<EmployersApi> = {}, rows: Employer[] = [employer("a"), employer("b")]) {
  let push: LiveHandler = () => {};
  const api = {
    employers: vi.fn(async () => page(rows)),
    employer: vi.fn(async (id: string) => ({ ...employer(id), roles: [] })),
    editEmployer: vi.fn(async (id: string, changes: Partial<Employer>) => ({ ...employer(id), ...changes })),
    setSite: vi.fn(async (id: string, on: boolean) => ({ id, on, state: on ? "waiting" : "off", status: on ? "Starts tonight" : "", needs: null })),
    employerMergePreview: vi.fn(),
    mergeEmployer: vi.fn(),
    employerSplitPreview: vi.fn(),
    splitEmployer: vi.fn(),
    ...overrides,
  } as unknown as EmployersApi;
  const close = vi.fn();
  const store = createEmployersStore({ api, openLive: (onEvent) => ((push = onEvent), { close }), recountMs: 0 });
  return { store, api, push: ((e, d) => push(e, d)) as LiveHandler, close };
}

async function started(overrides: Partial<EmployersApi> = {}, rows?: Employer[]) {
  const s = setup(overrides, rows);
  const stop = s.store.start();
  await vi.waitFor(() => expect(s.store.getState().status).toBe("ready"));
  return { ...s, stop };
}

describe("the employers store", () => {
  it("reads the first page of the view with open roles, and the rail's counts, in one call", async () => {
    const { store, api } = await started();
    expect(api.employers).toHaveBeenCalledTimes(1);
    expect(api.employers).toHaveBeenCalledWith({ view: "open", q: undefined, industry: undefined, limit: 50 });
    expect(store.getState().rows.map((r) => r.id)).toEqual(["a", "b"]);
    expect(store.getState().counts).toEqual(COUNTS);
  });

  it("asks the server again for a view, a name or an industry, and keeps only the latest answer", async () => {
    let answer: (p: EmployerPage) => void = () => {};
    const { store, api } = await started();
    vi.mocked(api.employers).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    vi.useFakeTimers();
    try {
      void store.find("co");
      await vi.advanceTimersByTimeAsync(FIND_MS);
    } finally {
      vi.useRealTimers();
    }
    vi.mocked(api.employers).mockResolvedValueOnce(page([employer("z")]));
    await store.setView("all");
    answer(page([employer("old")]));
    await Promise.resolve();
    expect(api.employers).toHaveBeenLastCalledWith({ view: "all", q: "co", industry: undefined, limit: 50 });
    expect(store.getState().rows.map((r) => r.id)).toEqual(["z"]);
  });

  it("waits for the typing to pause: a word typed letter by letter is one ask, with the letters shown at once", async () => {
    const { store, api } = await started();
    vi.mocked(api.employers).mockClear();
    vi.useFakeTimers();
    try {
      for (const q of ["n", "no", "nor"]) {
        void store.find(q);
        expect(store.getState().q).toBe(q);
        await vi.advanceTimersByTimeAsync(FIND_MS - 50);
      }
      expect(api.employers).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(50);
      expect(api.employers).toHaveBeenCalledTimes(1);
      expect(api.employers).toHaveBeenLastCalledWith({ view: "open", q: "nor", industry: undefined, limit: 50 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops a find still waiting when a view is picked, and asks once for both", async () => {
    const { store, api } = await started();
    vi.mocked(api.employers).mockClear();
    vi.useFakeTimers();
    try {
      const waiting = store.find("co");
      const picked = store.setView("all");
      await vi.advanceTimersByTimeAsync(FIND_MS * 2);
      await Promise.all([waiting, picked]);
      expect(api.employers).toHaveBeenCalledTimes(1);
      expect(api.employers).toHaveBeenLastCalledWith({ view: "all", q: "co", industry: undefined, limit: 50 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("loads the next page under the rows shown, from the cursor the first page gave", async () => {
    const { store, api } = await started({ employers: vi.fn(async () => page([employer("a")], { next: "50", total: 2 })) });
    vi.mocked(api.employers).mockResolvedValueOnce(page([employer("b")], { next: null, total: 2 }));
    await store.loadMore();
    expect(api.employers).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "50" }));
    expect(store.getState().rows.map((r) => r.id)).toEqual(["a", "b"]);
    expect(store.getState().next).toBeNull();
  });

  it("watches at once, the row and the rail's count, and takes the site's words from the answer", async () => {
    const { store, api } = await started();
    const done = store.watch(store.getState().rows[0], true);
    expect(store.getState().rows[0].site?.on).toBe(true);
    expect(store.getState().counts.watched).toBe(1);
    await done;
    expect(api.setSite).toHaveBeenCalledWith("s-a", true);
    expect(store.getState().rows[0].site?.status).toBe("Starts tonight");
    expect(api.employers).toHaveBeenCalledTimes(1);
  });

  it("puts a watch back and says so when the server refuses", async () => {
    const { store } = await started({ setSite: vi.fn(async () => Promise.reject(new ApiError("it is on the list to avoid", 409))) });
    expect(await store.watch(store.getState().rows[0], true)).toBe(false);
    expect(store.getState().rows[0].site?.on).toBe(false);
    expect(store.getState().counts.watched).toBe(0);
    expect(store.getState().notice).toBe("it is on the list to avoid");
  });

  it("puts a hard pass on with its reason at once, and keeps the line the server said of what it changed", async () => {
    const editEmployer = vi.fn(async (id: string) => ({ ...employer(id), gated: true, gated_reason: "a reason", said: "2 roles went to Gated out." }));
    const { store } = await started({ editEmployer });
    const done = store.hardPass(store.getState().rows[0], true, "a reason");
    expect(store.getState().rows[0].gated).toBe(true);
    expect(store.getState().counts.hard_pass).toBe(1);
    await done;
    expect(editEmployer).toHaveBeenCalledWith("a", { gated: true, gated_reason: "a reason" });
    expect(store.getState().said).toBe("2 roles went to Gated out.");
  });

  it("takes a hard pass back off the row and the count when the server refuses it", async () => {
    const { store } = await started({ editEmployer: vi.fn(async () => Promise.reject(new Error("no"))) });
    await store.hardPass(store.getState().rows[1], true);
    expect(store.getState().rows[1].gated).toBeFalsy();
    expect(store.getState().counts.hard_pass).toBe(0);
    expect(store.getState().notice).toBe("Couldn't change the hard pass.");
  });

  it("takes a merge's rows in place: the one that went leaves, the one left changes, the counts follow", async () => {
    const counts = { ...COUNTS, all: 2 };
    const mergeEmployer = vi.fn(async () => ({
      merged: true, employer: { ...employer("a"), roles_total: 2, roles: [] }, rows: [employer("a", { roles_total: 2 })], removed: ["b"], counts,
    }));
    const { store, api } = await started({ mergeEmployer });
    const kept = await store.merge("b", "a");
    expect(kept.id).toBe("a");
    expect(store.getState().rows.map((r) => [r.id, r.roles_total])).toEqual([["a", 2]]);
    expect(store.getState().counts).toEqual(counts);
    expect(store.getState().open.id).toBe("a");
    expect(api.employers).toHaveBeenCalledTimes(1);
  });

  it("takes a split's rows in place, and adds the new employer when the view holds it", async () => {
    const splitEmployer = vi.fn(async () => ({
      roles: 1, postings: 1, names: [], still_gated_out: 0, employer: employer("c"),
      rows: [employer("a", { roles_total: 0, roles_open: 0 }), employer("c", { name: "Co c" })], counts: COUNTS,
    }));
    const { store } = await started({ splitEmployer }, [employer("a", { roles_total: 2 }), employer("b")]);
    await store.split("a", ["r1"], "Co c");
    expect(store.getState().rows.map((r) => [r.id, r.roles_total])).toEqual([["a", 0], ["b", 1], ["c", 1]]);
  });

  it("changes a row in place on a live message, reads the open employer again, and asks the counts again", async () => {
    const { store, api, push } = await started();
    await store.openEmployer("a");
    push("employer", { id: "a", industry: "Robotics" });
    expect(store.getState().rows[0].industry).toBe("Robotics");
    await vi.waitFor(() => expect(api.employer).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(api.employers).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 1 })));
  });

  it("takes a live message that is the whole row the way it takes a partial one: its facts change, the counts and the site stay", async () => {
    const { store, push, api } = await started();
    const whole = {
      id: "a", name: "Co a", aliases: ["A Co"], local: true, industry: "Robotics", employees: 12, gated: false,
      gated_reason: null, ats: null, ats_label: "", board_url: null, ats_confirmed: null,
      fetched_at: "2030-01-01T00:00:00+00:00",
    };
    push("employer", whole);
    const rows = store.getState().rows;
    expect(rows[0]).toMatchObject({ id: "a", industry: "Robotics", employees: 12, local: true, aliases: ["A Co"], fetched_at: "2030-01-01T00:00:00+00:00" });
    expect(rows[0].roles_total).toBe(1);
    expect(rows[0].roles_open).toBe(1);
    expect(rows[0].site?.id).toBe("s-a");
    expect(rows[1].id).toBe("b");
    expect(rows[1].industry).toBeNull();
    await vi.waitFor(() => expect(api.employers).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 1 })));
  });

  it("drops a row merged away elsewhere and closes it when it was open", async () => {
    const { store, push } = await started();
    await store.openEmployer("b");
    push("employer", { id: "b", deleted: true });
    expect(store.getState().rows.map((r) => r.id)).toEqual(["a"]);
    expect(store.getState().open.id).toBeNull();
    expect(store.getState().total).toBe(1);
  });

  it("listens only while a screen shows it", async () => {
    const { stop, close } = await started();
    stop();
    expect(close).toHaveBeenCalledTimes(1);
  });
});
