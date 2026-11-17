import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LiveHandler } from "../api/live";
import type { Profile, ProfileSaved } from "../api/types/profile";
import { profileOf, site } from "./searchFixtures";
import { createSearchStore, type SearchApi } from "./searchStore";

// Search Settings' data layer against a made-up server: it reads the profile once, saves a part a field at a time (the
// answer carries the whole profile, so nothing is read again), puts a part back with Undo, keeps a reason said after the
// save, shows a newer version when its save is refused, ranks again on the match weight until the stream says the version
// landed, and follows the stream's profile versions and sites. Nobody's real data.

/** A made-up server that saves a part as its next version and answers the whole profile. */
function setup(overrides: Partial<SearchApi> = {}) {
  let push: LiveHandler = () => {};
  let server = profileOf();
  const api = {
    profile: vi.fn(async () => structuredClone(server)),
    saveProfile: vi.fn(async (part: keyof Profile["versions"], data: unknown): Promise<ProfileSaved> => {
      const version = server.versions[part] + 1;
      server = { ...server, [part]: structuredClone(data), versions: { ...server.versions, [part]: version } };
      return { saved: true, version, profile: structuredClone(server) };
    }),
    profileWhy: vi.fn(async (_part: string, version: number, why: string) => ({ version, saved_by: "owner", why, saved_at: null })),
    profileHistory: vi.fn(async () => ({ kind: "criteria", versions: [] })),
    gatesStopped: vi.fn(async () => ({ stopped: { travel: 4 } })),
    roleTrend: vi.fn(async () => ({ total: 6 })),
    agentReady: vi.fn(async () => ({ ready: true, why: null })),
    sites: vi.fn(async () => ({ sites: [site("a")], job_systems: [], watch_in_play: false })),
    setSite: vi.fn(async (id: string, on: boolean) => site(id, { on, state: on ? "waiting" : "off", status: "" })),
    setWatchInPlay: vi.fn(async (on: boolean) => ({ on })),
    ...overrides,
  } as unknown as SearchApi;
  const close = vi.fn();
  const store = createSearchStore({ api, openLive: (onEvent, onState) => ((push = onEvent), onState("open"), { close }), resiteMs: 0, rerankMs: 10_000 });
  return { store, api, push: ((e, d) => push(e, d)) as LiveHandler, close, server: () => server, setServer: (p: Profile) => (server = p) };
}

async function started(overrides: Partial<SearchApi> = {}) {
  const s = setup(overrides);
  const stop = s.store.start();
  await vi.waitFor(() => expect(s.store.getState().status).toBe("ready"));
  await vi.waitFor(() => expect(s.store.getState().sites).not.toBeNull());
  return { ...s, stop };
}

describe("the search store", () => {
  it("reads the profile, the sites, the stops and the roles found this week once", async () => {
    const { store, api } = await started();
    expect(api.profile).toHaveBeenCalledTimes(1);
    expect(api.sites).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(store.getState().stopped).toEqual({ travel: 4 }));
    await vi.waitFor(() => expect(store.getState().foundWeek).toEqual({ builders: 6 }));
    expect(api.roleTrend).toHaveBeenCalledWith("builders", 7);
  });

  it("saves one field's part as its next version, naming the version it was made from, and reads nothing again", async () => {
    const { store, api } = await started();
    const criteria = store.getState().profile!.criteria;
    const saving = store.edit("criteria", { ...criteria, company: "Another made-up kind." }, "Another made-up kind.");
    expect(store.getState().profile!.criteria.company).toBe("Another made-up kind.");
    expect(store.getState().saved.state).toBe("saving");
    await saving;
    expect(api.saveProfile).toHaveBeenCalledWith("criteria", { ...criteria, company: "Another made-up kind." }, "", 3);
    expect(store.getState().saved).toMatchObject({ part: "criteria", state: "saved", version: 4, canUndo: true });
    expect(store.getState().profile!.versions.criteria).toBe(4);
    expect(api.profile).toHaveBeenCalledTimes(1);
  });

  it("an edit that changes nothing sends nothing", async () => {
    const { store, api } = await started();
    await store.edit("criteria", structuredClone(store.getState().profile!.criteria));
    expect(api.saveProfile).not.toHaveBeenCalled();
  });

  it("saves edits made while a save is on its way after it, one at a time, from the newer version", async () => {
    let release: () => void = () => {};
    const s = await started();
    const real = vi.mocked(s.api.saveProfile).getMockImplementation()!;
    vi.mocked(s.api.saveProfile).mockImplementationOnce(async (...args) => {
      await new Promise<void>((r) => (release = r));
      return real(...args);
    });
    const c = s.store.getState().profile!.criteria;
    const first = s.store.edit("criteria", { ...c, company: "One" });
    const second = s.store.edit("criteria", { ...s.store.getState().profile!.criteria, company: "Two" });
    expect(s.api.saveProfile).toHaveBeenCalledTimes(1);
    release();
    await Promise.all([first, second]);
    expect(s.api.saveProfile).toHaveBeenCalledTimes(2);
    expect(vi.mocked(s.api.saveProfile).mock.calls[1][3]).toBe(4);
    expect(s.store.getState().profile!.criteria.company).toBe("Two");
  });

  // A save of Hard pass is answered only when its re-gate ends, and an edit made meanwhile
  // waited in the page alone; a reload then lost it without a word.
  it("is pending from the first edit until the last edit waiting behind it has landed, and the second goes with the latest state", async () => {
    const releases: (() => void)[] = [];
    const s = await started();
    const real = vi.mocked(s.api.saveProfile).getMockImplementation()!;
    for (let i = 0; i < 2; i++) {
      vi.mocked(s.api.saveProfile).mockImplementationOnce(async (...args) => {
        await new Promise<void>((r) => releases.push(r));
        return real(...args);
      });
    }
    expect(s.store.getState().pending).toBe(false);
    const gates = s.store.getState().profile!.gates;
    const first = s.store.edit("gates", { ...gates, employer_limit: false });
    const second = s.store.edit("gates", { ...s.store.getState().profile!.gates, employer_names: ["Kept"] });
    expect(s.store.getState().pending).toBe(true);
    releases[0]();
    await vi.waitFor(() => expect(s.api.saveProfile).toHaveBeenCalledTimes(2));
    expect(s.store.getState().pending).toBe(true); // the second save is on its way, not done
    releases[1]();
    await Promise.all([first, second]);
    expect(s.store.getState().pending).toBe(false);
    expect(s.api.saveProfile).toHaveBeenCalledTimes(2);
    expect(vi.mocked(s.api.saveProfile).mock.calls[1][1]).toMatchObject({ employer_limit: false, employer_names: ["Kept"] });
    expect(s.server().gates).toMatchObject({ employer_limit: false, employer_names: ["Kept"] });
  });

  it("does not say Saved when the save that landed has an edit still waiting behind it", async () => {
    const releases: (() => void)[] = [];
    const s = await started();
    const real = vi.mocked(s.api.saveProfile).getMockImplementation()!;
    for (let i = 0; i < 2; i++) {
      vi.mocked(s.api.saveProfile).mockImplementationOnce(async (...args) => {
        await new Promise<void>((r) => releases.push(r));
        return real(...args);
      });
    }
    const states: string[] = [];
    s.store.subscribe(() => states.push(s.store.getState().saved.state));
    const gates = s.store.getState().profile!.gates;
    const first = s.store.edit("gates", { ...gates, employer_limit: false });
    const second = s.store.edit("gates", { ...s.store.getState().profile!.gates, employer_names: ["Kept"] });
    releases[0]();
    await vi.waitFor(() => expect(s.api.saveProfile).toHaveBeenCalledTimes(2));
    expect(states).not.toContain("saved"); // the first has landed; the second has not, and the line says so
    releases[1]();
    await Promise.all([first, second]);
    expect(s.store.getState().saved.state).toBe("saved");
  });

  it("is no longer pending when the save was refused", async () => {
    const s = await started();
    vi.mocked(s.api.saveProfile).mockRejectedValueOnce(new ApiError("That doesn't fit.", 422));
    await s.store.edit("criteria", { ...s.store.getState().profile!.criteria, company: "Bad" });
    expect(s.store.getState().pending).toBe(false);
  });

  it("Undo saves the part as it was before, once", async () => {
    const { store, api } = await started();
    const before = structuredClone(store.getState().profile!.criteria);
    await store.edit("criteria", { ...before, company: "Changed" });
    await store.undo();
    expect(store.getState().profile!.criteria.company).toBe(before.company);
    expect(vi.mocked(api.saveProfile).mock.calls[1][1]).toEqual(before);
    expect(store.getState().saved.canUndo).toBe(false);
  });

  it("keeps a reason said after the save, for that version", async () => {
    const { store, api } = await started();
    await store.edit("criteria", { ...store.getState().profile!.criteria, company: "Changed" });
    expect(await store.addWhy("  a made-up reason ")).toBe(true);
    expect(api.profileWhy).toHaveBeenCalledWith("criteria", 4, "a made-up reason");
    expect(store.getState().saved.why).toBe("a made-up reason");
  });

  it("a save refused because a newer version is in force shows the newer one and says back what was typed", async () => {
    const s = await started();
    s.setServer({ ...s.server(), criteria: { ...s.server().criteria, company: "Saved elsewhere" }, versions: { ...s.server().versions, criteria: 9 } });
    vi.mocked(s.api.saveProfile).mockRejectedValueOnce(new ApiError("A newer version was saved.", 409));
    await s.store.edit("criteria", { ...s.store.getState().profile!.criteria, company: "Mine" }, "Mine");
    expect(s.store.getState().profile!.criteria.company).toBe("Saved elsewhere");
    expect(s.store.getState().saved).toMatchObject({ state: "failed", typed: "Mine" });
    expect(s.store.getState().saved.error).toMatch(/newer version/);
  });

  it("a save refused for another reason puts the part back and says why", async () => {
    const s = await started();
    vi.mocked(s.api.saveProfile).mockRejectedValueOnce(new ApiError("That doesn't fit.", 422));
    const before = s.store.getState().profile!.criteria.company;
    await s.store.edit("criteria", { ...s.store.getState().profile!.criteria, company: "Bad" });
    expect(s.store.getState().profile!.criteria.company).toBe(before);
    expect(s.store.getState().saved).toMatchObject({ state: "failed", error: "That doesn't fit." });
  });

  it("the match weight saves once, and says Re-ranking until the stream says a newer scoring version landed", async () => {
    const s = await started();
    await s.store.setMatchWeight(0.7);
    expect(s.api.saveProfile).toHaveBeenCalledTimes(1);
    expect(vi.mocked(s.api.saveProfile).mock.calls[0][1]).toMatchObject({ match_weight: 0.7 });
    expect(s.store.getState().reranking).toBe(true);
    s.push("profile_version", { kind: "scoring", version: 6, saved_by: "owner", why: "", saved_at: null });
    expect(s.store.getState().reranking).toBe(false);
  });

  it("reads the profile again for a version it has not seen, and not for its own", async () => {
    const s = await started();
    await s.store.edit("criteria", { ...s.store.getState().profile!.criteria, company: "Mine" });
    s.push("profile_version", { kind: "criteria", version: 4, saved_by: "owner", why: "", saved_at: null });
    expect(s.api.profile).toHaveBeenCalledTimes(1);
    s.setServer({ ...s.server(), gates: { ...s.server().gates, other: [] }, versions: { ...s.server().versions, gates: 3 } });
    s.push("profile_version", { kind: "gates", version: 3, saved_by: "researcher", why: "", saved_at: null });
    await vi.waitFor(() => expect(s.store.getState().profile!.gates.other).toEqual([]));
    expect(s.api.profile).toHaveBeenCalledTimes(2);
  });

  it("reads the sites again after a site message, and switches a site at once, back when refused", async () => {
    const s = await started();
    s.push("site", { id: "a", on: false });
    await vi.waitFor(() => expect(s.api.sites).toHaveBeenCalledTimes(2));
    vi.mocked(s.api.setSite).mockRejectedValueOnce(new ApiError("No.", 409));
    const a = s.store.getState().sites!.sites[0];
    const done = s.store.setSite(a, false);
    expect(s.store.getState().sites!.sites[0].on).toBe(false);
    expect(await done).toBe(false);
    expect(s.store.getState().sites!.sites[0].on).toBe(true);
    expect(s.store.getState().notice).toBe("No.");
  });

  it("stops listening when the last screen stops it", async () => {
    const { stop, close } = await started();
    stop();
    expect(close).toHaveBeenCalled();
  });
});
