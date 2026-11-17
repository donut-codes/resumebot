// Search Settings' data layer (the screens rebuild spec, screen 3): the profile, read once per visit (GET /profile),
// and the sites of Where to look (GET /sites), with every change the screen makes. A field saves itself: an edit shows at
// once and its part goes to the server as its next version (the whole part, naming the version it was made from), one
// save at a time per part; the server's answer carries the whole profile, so nothing is read again after a save. The
// Saved line offers Undo (the part as it was, saved again) and a reason, said after the save and shown in History. A
// save refused because a newer version was saved elsewhere shows the newer one and keeps what was typed in words. A save of
// Hard pass is answered only when its re-gate ends (a minute or more), and an edit made meanwhile waits for it in this page
// alone, so `pending` is true until the last of them has landed, the Saved line says Saving… until then, and the page
// asks before it is closed or reloaded (a warning only: a page left anyway still loses the edit that waits). It
// follows the live stream's `profile_version` messages (another tab, an agent, a restore: the profile is read again) and
// `site` messages (the sites are read again once they stop). A store knows nothing of how anything looks.

import { api as defaultApi, ApiError, errorMessage } from "../api/client";
import type { LiveConnection, LiveHandler, LiveTopic } from "../api/live";
import type { LiveProfileVersion } from "../api/types/pipeline";
import type { AgentReady, Profile, ProfilePart, ProfileSaved } from "../api/types/profile";
import type { JobSystem, Site, SiteList, SiteReadBy } from "../api/types/where";
import type { WherePage } from "../api/types/profile";
import { counted, createState, optimistic, storeContext, topicsOf, type OpenLive } from "./store";

export const SEARCH_TOPICS: LiveTopic[] = ["profile_version", "site"];
/** How long the sites wait after a live message before they are read again, so a burst is one read. */
export const RESITE_MS = 400;
/** How long "Re-ranking" waits for the stream's word before it gives up saying so (a stream that is down says nothing). */
export const RERANK_MS = 20_000;
/** The days "found this week" counts. */
export const WEEK_DAYS = 7;

export type SearchApi = Pick<
  typeof defaultApi,
  | "profile" | "saveProfile" | "profileWhy" | "profileHistory" | "gatesStopped" | "roleTrend" | "agentReady"
  | "sites" | "setSite" | "setJobSystem" | "setSiteReadBy" | "addSite" | "removeSite" | "setWatchInPlay"
  | "siteRoles" | "jobSystemRoles" | "employers"
  | "fitsNone" | "roleFromRole" | "dismissFitsNone" | "checkRoles" | "checkJob" | "parseExample" | "parseJob" | "compExample"
>;

/** The parts of the profile the screen saves. */
export type Part = Exclude<ProfilePart, never>;
const PARTS: Part[] = ["criteria", "gates", "scoring", "settings"];

/** The Saved line in the pane's head: the last save of a field, with Undo and a reason. */
export interface SaveLine {
  part: Part | null;
  state: "idle" | "saving" | "saved" | "failed";
  /** Why it did not go, in words. */
  error: string | null;
  /** The version the save made (null when it changed nothing). */
  version: number | null;
  /** Whether Undo can put the part back as it was before this save. */
  canUndo: boolean;
  /** The reason said for it, once said. */
  why: string;
  /** What the owner typed, said back when the save was refused, so it can be typed again. */
  typed: string | null;
}

export interface SearchState {
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  /** The profile as shown: the server's, with the edits on their way already in it. */
  profile: Profile | null;
  sites: WherePage | null;
  sitesStatus: "idle" | "loading" | "ready" | "error";
  sitesError: string | null;
  /** How many each line of Hard pass stopped, by the line's id. */
  stopped: Record<string, number>;
  /** Roles found for each target role in the last week, by the role's id. */
  foundWeek: Record<string, number>;
  /** Whether an agent can take work the owner asks for now, or why not; null until known. */
  ready: AgentReady | null;
  saved: SaveLine;
  /** A save is on its way, or an edit waits for one: what the owner changed is held only by this page until it lands, and
   * the page asks before it is closed or reloaded (`SearchLeaveGuard`). */
  pending: boolean;
  /** The match weight was saved and every role is being ranked again, until the stream says the version landed. */
  reranking: boolean;
  /** The site whose way of being read was just left to resumebot: the server is trying it. */
  trying: string | null;
  live: "connecting" | "open" | "closed";
  /** The last thing that went wrong with a site's action, in words, until dismissed. */
  notice: string | null;
  /** The one line a save said of what it changed, until it is said. */
  said: string | null;
}

const IDLE: SaveLine = { part: null, state: "idle", error: null, version: null, canUndo: false, why: "", typed: null };

function initialState(): SearchState {
  return {
    status: "idle",
    error: null,
    profile: null,
    sites: null,
    sitesStatus: "idle",
    sitesError: null,
    stopped: {},
    foundWeek: {},
    ready: null,
    saved: IDLE,
    pending: false,
    reranking: false,
    trying: null,
    live: "connecting",
    notice: null,
    said: null,
  };
}

/** Two documents as they would be saved: keys in any order, a key left empty the same as one never set. */
export function same(a: unknown, b: unknown): boolean {
  const sort = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>)
          .filter(([, x]) => x !== null && x !== undefined)
          .sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0))
          .map(([k, x]) => [k, sort(x)]),
      );
    }
    return v;
  };
  return JSON.stringify(sort(a)) === JSON.stringify(sort(b));
}

/** What a switch means once flipped, until the server answers: on waits for tonight's mission, off is nothing. */
function flipped(site: Site, on: boolean): Site {
  const read = site.read_by === "auto" ? site.read_found : site.read_by;
  return { ...site, on, state: !on ? "off" : read === "browser" ? "browser" : "waiting", status: "", needs: null };
}

export function createSearchStore(opts: { api?: SearchApi; openLive?: OpenLive; resiteMs?: number; rerankMs?: number } = {}) {
  const api = opts.api ?? defaultApi;
  const openLive = opts.openLive ?? topicsOf(SEARCH_TOPICS);
  const resiteMs = opts.resiteMs ?? RESITE_MS;
  const rerankMs = opts.rerankMs ?? RERANK_MS;
  const core = createState(initialState());
  const set = core.set;
  const current = () => core.getState();

  /** Each part as the server last saved it, and the version that is. */
  const base: Partial<Record<Part, unknown>> = {};
  const versions: Partial<Record<Part, number>> = {};
  /** A save of a part on its way, and whether another edit came while it was. */
  const inflight: Partial<Record<Part, Promise<void>>> = {};
  const again: Partial<Record<Part, { typed: string | null; undoable: boolean }>> = {};
  /** What Undo puts back: the part before the last save. */
  let undo: { part: Part; doc: unknown } | null = null;
  let profileSeq = 0;
  let siteTimer: ReturnType<typeof setTimeout> | null = null;
  let rerankTimer: ReturnType<typeof setTimeout> | null = null;
  /** The scoring version in force when the match weight was let go: a newer one finishes the re-rank. */
  let rerankFrom: number | null = null;

  function takeServer(profile: Profile, parts: Part[] = PARTS) {
    for (const part of parts) {
      base[part] = structuredClone(profile[part]);
      versions[part] = profile.versions[part] ?? 0;
    }
  }

  // --- reading ----------------------------------------------------------------------------------------------------

  /** The profile (again, when asked again). Quiet keeps what is shown while it is read; a part with a save on its way
   * keeps what is shown. */
  async function loadProfile(quiet = false): Promise<void> {
    const seq = ++profileSeq;
    if (!quiet || !current().profile) set({ status: current().profile ? "ready" : "loading", error: null });
    try {
      const fresh = await api.profile();
      if (seq !== profileSeq) return;
      show(fresh, current().profile ? free() : PARTS);
      set({ status: "ready", error: null });
      void loadFound(fresh);
    } catch (err) {
      if (seq !== profileSeq) return;
      if (!current().profile) set({ status: "error", error: errorMessage(err, "Couldn't load your search settings.") });
    }
  }

  async function loadSites(): Promise<void> {
    if (!current().sites) set({ sitesStatus: "loading", sitesError: null });
    try {
      const sites = await api.sites();
      set({ sites, sitesStatus: "ready", sitesError: null });
    } catch (err) {
      if (!current().sites) set({ sitesStatus: "error", sitesError: errorMessage(err, "Couldn't load the sites.") });
    }
  }

  async function loadStopped(): Promise<void> {
    try {
      set({ stopped: (await api.gatesStopped()).stopped });
    } catch {
      // The counts stay as they were; a row without one says nothing.
    }
  }

  /** How many roles each target role found this week: one small read each, quietly. */
  async function loadFound(profile: Profile): Promise<void> {
    const roles = profile.criteria?.target_roles ?? [];
    const found: Record<string, number> = {};
    await Promise.all(
      roles.map(async (r) => {
        try {
          found[r.id] = (await api.roleTrend(r.id, WEEK_DAYS)).total;
        } catch {
          // No count for that role.
        }
      }),
    );
    set({ foundWeek: found });
  }

  async function checkReady(): Promise<void> {
    try {
      set({ ready: await api.agentReady() });
    } catch {
      set({ ready: null });
    }
  }

  function resiteSoon() {
    if (siteTimer) clearTimeout(siteTimer);
    siteTimer = setTimeout(() => {
      siteTimer = null;
      void loadSites();
    }, resiteMs);
  }

  function stopReranking() {
    rerankFrom = null;
    if (rerankTimer) clearTimeout(rerankTimer);
    rerankTimer = null;
    if (current().reranking) set({ reranking: false });
  }

  // --- saving a field ---------------------------------------------------------------------------------------------

  const shown = <P extends Part>(part: P): Profile[P] | undefined => current().profile?.[part];

  /** The parts a fresh profile from the server may replace: every one with no save on its way, and `part` itself when
   * nothing was typed in it since `sent` went. */
  function free(part?: Part, keepPart = false): Part[] {
    return PARTS.filter((p) => (p === part ? !keepPart : !inflight[p]));
  }

  /** Shows a fresh profile from the server in the parts given; the others keep what is shown. */
  function show(fresh: Profile, parts: Part[]) {
    takeServer(fresh, parts);
    set((s) => {
      const profile: Profile = { ...fresh };
      if (s.profile) for (const p of PARTS) if (!parts.includes(p)) (profile as unknown as Record<Part, unknown>)[p] = s.profile[p];
      return { profile };
    });
  }

  /** One save of a part as it is shown now; another follows when an edit came while it was on its way. */
  async function send(part: Part, typed: string | null, undoable: boolean): Promise<void> {
    const before = structuredClone(base[part]);
    const sending = structuredClone(shown(part));
    set({ saved: { ...IDLE, part, state: "saving", typed } });
    let answer: ProfileSaved;
    try {
      answer = await api.saveProfile(part, sending, "", versions[part] ?? 0);
    } catch (err) {
      delete again[part];
      if (part === "scoring") stopReranking();
      if (err instanceof ApiError && err.status === 409) {
        // A newer version is in force: show it, and say back what was typed so it can be put in again.
        try {
          show(await api.profile(), free(part));
        } catch {
          // What is shown stays; the next visit reads it again.
        }
        set({ saved: { ...IDLE, part, state: "failed", typed, error: "Not saved: a newer version was saved elsewhere, and it is shown now." } });
        return;
      }
      set((s) => ({
        profile: s.profile ? ({ ...s.profile, [part]: structuredClone(before) } as Profile) : s.profile,
        saved: { ...IDLE, part, state: "failed", typed, error: errorMessage(err, "Couldn't save that.") },
      }));
      return;
    }
    versions[part] = answer.version ?? versions[part] ?? 0;
    base[part] = structuredClone(sending);
    if (answer.profile) {
      const editedSince = !same(shown(part), sending);
      show(answer.profile, free(part, editedSince));
      if (part === "criteria") void loadFound(answer.profile);
    }
    const made = answer.version != null;
    undo = undoable && made ? { part, doc: before } : null;
    set((s) => ({
      // An edit made while this save was on its way goes next: until it has landed the line stays on "Saving…", never
      // "Saved" with an Undo for the one before it.
      saved: again[part] ? s.saved : { ...IDLE, part, state: made ? "saved" : "idle", version: answer.version, canUndo: undoable && made },
      said: answer.said || s.said,
    }));
    if (part === "gates") void loadStopped();
    if (part === "scoring" && !made) stopReranking();
  }

  async function queue(part: Part, typed: string | null, undoable: boolean): Promise<void> {
    if (inflight[part]) {
      again[part] = { typed, undoable };
      return inflight[part];
    }
    const run = (async () => {
      let next: { typed: string | null; undoable: boolean } | undefined = { typed, undoable };
      while (next) {
        delete again[part];
        await send(part, next.typed, next.undoable);
        next = again[part];
      }
    })();
    inflight[part] = run;
    set({ pending: true });
    try {
      await run;
    } finally {
      delete inflight[part];
      set({ pending: PARTS.some((p) => inflight[p]) });
    }
  }

  /** An edit of a part: shown at once, then saved as its next version. `typed` is what the owner wrote, said back if the
   * save is refused. */
  function edit<P extends Part>(part: P, next: Profile[P], typed: string | null = null): Promise<void> {
    if (!current().profile) return Promise.resolve();
    if (same(shown(part), next)) return Promise.resolve();
    set((s) => ({ profile: s.profile ? ({ ...s.profile, [part]: next } as Profile) : s.profile }));
    return queue(part, typed, true);
  }

  // --- the live stream --------------------------------------------------------------------------------------------

  function applyVersion(ev: LiveProfileVersion): void {
    const part = ev.kind;
    if (part === "scoring" && rerankFrom !== null && ev.version > rerankFrom) stopReranking();
    if (inflight[part]) return; // the save's own answer brings it
    if (ev.version <= (versions[part] ?? 0)) return; // one this page has
    void loadProfile(true);
  }

  const onEvent: LiveHandler = (event, data) => {
    if (event === "profile_version") applyVersion(data as LiveProfileVersion);
    else if (event === "site") resiteSoon();
    else if (event === "resync") {
      void loadProfile(true);
      void loadSites();
    }
  };

  // --- the sites --------------------------------------------------------------------------------------------------

  function replaceSite(next: Site) {
    set((s) => (s.sites ? { sites: { ...s.sites, sites: s.sites.sites.map((x) => (x.id === next.id ? next : x)) } } : {}));
  }
  function replaceSystem(next: JobSystem) {
    set((s) => (s.sites ? { sites: { ...s.sites, job_systems: s.sites.job_systems.map((j) => (j.key === next.key ? next : j)) } } : {}));
  }

  return {
    getState: core.getState,
    subscribe: core.subscribe,
    /** Search Settings starts it; it reads and listens while the screen is open. */
    start: counted(() => {
      void loadProfile();
      void loadSites();
      void loadStopped();
      void checkReady();
      set({ live: "connecting" });
      const connection: LiveConnection = openLive(onEvent, (st) => {
        const back = st === "open" && current().live === "closed";
        set({ live: st });
        if (back) {
          void loadProfile(true);
          void loadSites();
        }
      });
      return () => {
        connection.close();
        if (siteTimer) clearTimeout(siteTimer);
        siteTimer = null;
        stopReranking();
      };
    }).start,
    reload: () => Promise.all([loadProfile(true), loadSites(), loadStopped()]).then(() => undefined),
    checkReady,

    edit,
    /** Puts the part back as it was before the last save (one more version; it cannot itself be undone). */
    undo: async (): Promise<void> => {
      const was = undo;
      if (!was) return;
      undo = null;
      set((s) => ({ profile: s.profile ? ({ ...s.profile, [was.part]: structuredClone(was.doc) } as Profile) : s.profile }));
      await queue(was.part, null, false);
    },
    /** Says why the last save was made; History shows it. */
    addWhy: async (why: string): Promise<boolean> => {
      const line = current().saved;
      if (!line.part || line.version == null || !why.trim()) return false;
      try {
        const row = await api.profileWhy(line.part, line.version, why.trim());
        if (current().saved.version === line.version) set({ saved: { ...current().saved, why: row.why } });
        return true;
      } catch (err) {
        set({ saved: { ...current().saved, error: errorMessage(err, "Couldn't keep the reason.") } });
        return false;
      }
    },
    /** The Saved line has done its job (another item opened). */
    quiet: () => {
      if (current().saved.state !== "saving") set({ saved: IDLE });
    },
    /** The match weight, saved when let go: every role is ranked again, and the pane says so until the stream says the
     * version landed. */
    setMatchWeight: async (weight: number): Promise<void> => {
      const scoring = shown("scoring");
      if (!scoring) return;
      rerankFrom = versions.scoring ?? 0;
      set({ reranking: true });
      if (rerankTimer) clearTimeout(rerankTimer);
      rerankTimer = setTimeout(stopReranking, rerankMs);
      await edit("scoring", { ...scoring, match_weight: weight });
      if (current().live !== "open") stopReranking();
    },
    history: (part: Part) => api.profileHistory(part),

    // --- Where to look ------------------------------------------------------------------------------------------
    /** Switch a site on or off: the row at once, back (and said) when the server refuses. */
    setSite: async (site: Site, on: boolean): Promise<boolean> =>
      optimistic(
        () => replaceSite(flipped(site, on)),
        () => api.setSite(site.id, on),
        (err) => {
          replaceSite(site);
          set({ notice: errorMessage(err, `Couldn't switch ${site.label} ${on ? "on" : "off"}.`) });
        },
        replaceSite,
      ),
    /** A whole job system, on or off; the answer is the whole page, since the sites on it change what they say. */
    setJobSystem: async (system: JobSystem, on: boolean): Promise<boolean> =>
      optimistic(
        () => replaceSystem({ ...system, on, status: "" }),
        () => api.setJobSystem(system.key, on),
        (err) => {
          replaceSystem(system);
          set({ notice: errorMessage(err, `Couldn't switch ${system.name} ${on ? "on" : "off"}.`) });
        },
        (page) => set((s) => ({ sites: { ...page, watch_in_play: s.sites?.watch_in_play, terms: s.sites?.terms } })),
      ),
    /** How a site is read. Left to resumebot, the server tries the site before it answers (up to 15 seconds). */
    setReadBy: async (site: Site, readBy: SiteReadBy): Promise<void> => {
      if (readBy === "auto") set({ trying: site.id });
      try {
        replaceSite(await api.setSiteReadBy(site.id, readBy));
      } catch (err) {
        set({ notice: errorMessage(err, `Couldn't change how ${site.label} is read.`) });
      } finally {
        set({ trying: null });
      }
    },
    /** Adds a site to a list; rejects in words when it cannot. */
    addSite: async (url: string, list: SiteList, readBy: SiteReadBy): Promise<Site> => {
      const site = await api.addSite(url, { list, readBy });
      set((s) => ({
        sites: { job_systems: s.sites?.job_systems ?? [], watch_in_play: s.sites?.watch_in_play, terms: s.sites?.terms, sites: [...(s.sites?.sites ?? []).filter((x) => x.id !== site.id), site] },
        said: site.said ?? s.said,
      }));
      return site;
    },
    /** Takes a site off the list to avoid. */
    removeSite: async (site: Site): Promise<boolean> => {
      try {
        const removed = await api.removeSite(site.id);
        set((s) => ({ sites: s.sites ? { ...s.sites, sites: s.sites.sites.filter((x) => x.id !== site.id) } : s.sites, said: removed.said ?? s.said }));
        return true;
      } catch (err) {
        set({ notice: errorMessage(err, `Couldn't take ${site.label} off the list.`) });
        return false;
      }
    },
    /** Watch the job board of every employer with a role in play: the switch at once, then the sites read again. */
    setWatchInPlay: async (on: boolean): Promise<boolean> => {
      const was = current().sites?.watch_in_play ?? false;
      const ok = await optimistic(
        () => set((s) => (s.sites ? { sites: { ...s.sites, watch_in_play: on } } : {})),
        () => api.setWatchInPlay(on),
        (err) => set((s) => ({ sites: s.sites ? { ...s.sites, watch_in_play: was } : s.sites, notice: errorMessage(err, "Couldn't change it.") })),
      );
      if (ok) void loadSites();
      return ok;
    },
    siteRoles: (id: string, offset: number, limit: number) => api.siteRoles(id, offset, limit),
    jobSystemRoles: (key: string, offset: number, limit: number) => api.jobSystemRoles(key, offset, limit),

    // --- What to look for and Hard pass: reads and agent jobs the panes start ------------------------------------
    /** The employers on the hard pass list, every page of it. */
    gatedEmployers: async () => {
      const all = [];
      let cursor: string | null = null;
      do {
        const page = await api.employers({ view: "hard_pass", cursor, limit: 500 });
        all.push(...page.employers);
        cursor = page.next;
      } while (cursor);
      return all.filter((e) => e.gated);
    },
    fitsNone: (limit: number) => api.fitsNone({ limit }),
    roleFromRole: (id: string) => api.roleFromRole(id),
    dismissFitsNone: (id: string) => api.dismissFitsNone(id),
    checkRoles: api.checkRoles,
    checkJob: api.checkJob,
    parseExample: api.parseExample,
    parseJob: api.parseJob,
    compExample: api.compExample,

    dismissNotice: () => set({ notice: null }),
    dismissSaid: () => set({ said: null }),
    /** For tests: what the stream would have said. */
    receive: onEvent,
  };
}

export type SearchStore = ReturnType<typeof createSearchStore>;

const context = storeContext<SearchStore, SearchState>("useSearchStore");

/** Hands the store to Search Settings (a test's own, or the app's). */
export const SearchStoreProvider = context.Provider;
export const useSearchStore = context.useStore;
/** Reads part of Search Settings' state; the component redraws only when that part changes. */
export const useSearch = context.useSlice;
