// Employers' data layer (the screens rebuild spec, screen 2): the list a page at a time (GET /employers, 50 at a
// time, more as it scrolls), in a view, narrowed to a name and an industry, with each view's count for the rail; the
// employer open in the pane; and every change the screen makes: watch, hard pass, facts, merge and split. A watch and a
// hard pass show at once (the row and the rail's count) and are put back, and said, when the server refuses. Merge and
// split take the rows the server says they changed in place; nothing reads the whole list again after a click. It
// follows the live stream's `employer` messages (the researcher changes facts at night): the row changes in place, an
// open employer is read again, and the rail's counts are asked for again once the messages stop.

import { api as defaultApi, errorMessage } from "../api/client";
import type { LiveConnection, LiveHandler, LiveTopic } from "../api/live";
import type {
  Employer,
  EmployerCounts,
  EmployerMergePreview,
  EmployerQuery,
  EmployerSplitPreview,
  EmployerView,
  LiveEmployer,
} from "../api/types/employers";
import { counted, createState, optimistic, storeContext, topicsOf, type OpenLive } from "./store";

export const EMPLOYER_TOPICS: LiveTopic[] = ["employer"];
/** How many employers a page of the list holds. */
export const PAGE = 50;
/** How many employers the merge's search lists. */
export const MATCHES = 8;
/** How long the rail's counts wait after a live message before they are asked for again, so a night's burst is one ask. */
export const RECOUNT_MS = 400;
/** How long the find box waits after a keystroke before the list is asked for, so typing a word is one ask (the merge's
 * search waits the same). */
export const FIND_MS = 250;

export type EmployersApi = Pick<
  typeof defaultApi,
  "employers" | "employer" | "editEmployer" | "setSite" | "employerMergePreview" | "mergeEmployer" | "employerSplitPreview" | "splitEmployer"
>;

const NO_COUNTS: EmployerCounts = { all: 0, open: 0, watched: 0, hard_pass: 0, local: 0 };

export interface OpenState {
  id: string | null;
  status: "idle" | "loading" | "ready" | "error";
  employer: Employer | null;
  error: string | null;
}

export interface EmployersState {
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  view: EmployerView;
  q: string;
  industry: string;
  /** The rows shown, in the server's order (by name). */
  rows: Employer[];
  /** Where the next page starts; null when there is no more. */
  next: string | null;
  /** How many the view holds, so narrowed. */
  total: number;
  loadingMore: boolean;
  counts: EmployerCounts;
  industries: string[];
  open: OpenState;
  live: "connecting" | "open" | "closed";
  /** Employers with a change on its way to the server. */
  busy: Record<string, true>;
  /** The last thing that went wrong with an action, in words, until it is dismissed. */
  notice: string | null;
  /** The one line a hard pass said of what it changed, until it is dismissed. */
  said: string | null;
}

function initialState(): EmployersState {
  return {
    status: "idle",
    error: null,
    view: "open",
    q: "",
    industry: "",
    rows: [],
    next: null,
    total: 0,
    loadingMore: false,
    counts: NO_COUNTS,
    industries: [],
    open: { id: null, status: "idle", employer: null, error: null },
    live: "connecting",
    busy: {},
    notice: null,
    said: null,
  };
}

/** Whether a row belongs in a view (the same rule as the server's, for a row it changed). */
export function inView(e: Employer, view: EmployerView): boolean {
  if (view === "open") return (e.roles_open ?? 0) > 0;
  if (view === "watched") return !!e.site?.on;
  if (view === "hard_pass") return !!e.gated;
  if (view === "local") return e.local === true;
  return true;
}

/** What a merge or split answered over what the list held: a row's counts and site come from the answer. */
const merged = (had: Employer, fresh: Partial<Employer>): Employer => ({ ...had, ...fresh });

export function createEmployersStore(opts: { api?: EmployersApi; openLive?: OpenLive; recountMs?: number; findMs?: number } = {}) {
  const api = opts.api ?? defaultApi;
  const openLive = opts.openLive ?? topicsOf(EMPLOYER_TOPICS);
  const recountMs = opts.recountMs ?? RECOUNT_MS;
  const findMs = opts.findMs ?? FIND_MS;
  const core = createState(initialState());
  const set = core.set;
  const current = () => core.getState();
  let listSeq = 0;
  let openSeq = 0;
  let recountTimer: ReturnType<typeof setTimeout> | null = null;
  let findTimer: ReturnType<typeof setTimeout> | null = null;
  let findDone: (() => void) | null = null;

  const query = (s: EmployersState, extra: Partial<EmployerQuery> = {}): EmployerQuery => ({
    view: s.view,
    q: s.q.trim() || undefined,
    industry: s.industry || undefined,
    limit: PAGE,
    ...extra,
  });

  // --- the list -----------------------------------------------------------------------------------------------------

  /** The first page of the view as it stands (again, when asked again). */
  async function load(): Promise<void> {
    const seq = ++listSeq;
    if (current().status !== "ready") set({ status: "loading", error: null });
    try {
      const page = await api.employers(query(current()));
      if (seq !== listSeq) return;
      set({
        status: "ready",
        error: null,
        rows: page.employers,
        next: page.next,
        total: page.total,
        counts: page.counts,
        industries: page.industries,
        loadingMore: false,
      });
    } catch (err) {
      if (seq !== listSeq) return;
      set({ status: "error", error: errorMessage(err, "Couldn't load the employers.") });
    }
  }

  /** The next page, under the rows shown. */
  async function loadMore(): Promise<void> {
    const s = current();
    if (!s.next || s.loadingMore) return;
    const seq = listSeq;
    set({ loadingMore: true });
    try {
      const page = await api.employers(query(s, { cursor: s.next }));
      if (seq !== listSeq) return;
      set((now) => {
        const have = new Set(now.rows.map((r) => r.id));
        return {
          rows: [...now.rows, ...page.employers.filter((e) => !have.has(e.id))],
          next: page.next,
          total: page.total,
          counts: page.counts,
          loadingMore: false,
        };
      });
    } catch (err) {
      if (seq !== listSeq) return;
      set({ loadingMore: false, notice: errorMessage(err, "Couldn't load more employers.") });
    }
  }

  /** A view, a name or an industry chosen: the list answers for it. */
  function narrow(patch: Partial<Pick<EmployersState, "view" | "q" | "industry">>): Promise<void> {
    cancelFind();
    set({ ...patch, next: null });
    return load();
  }

  /** Drops a find that is still waiting for the typing to stop (the list asks for what is typed when it next asks). */
  function cancelFind(): void {
    if (findTimer) clearTimeout(findTimer);
    findTimer = null;
    findDone?.();
    findDone = null;
  }

  /** What is typed in the find box: shown at once, asked of the server when typing has paused for `findMs`. */
  function find(q: string): Promise<void> {
    cancelFind();
    set({ q, next: null });
    return new Promise<void>((done) => {
      findDone = done;
      findTimer = setTimeout(() => {
        findTimer = null;
        findDone = null;
        void load().then(done);
      }, findMs);
    });
  }

  /** Asks the rail's counts again (after a live message: what a fact changed may move an employer between views). */
  async function recount(): Promise<void> {
    try {
      const page = await api.employers(query(current(), { limit: 1, cursor: null }));
      set({ counts: page.counts, industries: page.industries });
    } catch {
      // The counts stay as they were; the next message or reload asks again.
    }
  }

  function recountSoon() {
    if (recountTimer) clearTimeout(recountTimer);
    recountTimer = setTimeout(() => {
      recountTimer = null;
      void recount();
    }, recountMs);
  }

  // --- the open employer ------------------------------------------------------------------------------------------

  /** Reads one employer whole for the pane (null closes it). Quiet keeps what is shown while it is read again. */
  async function openEmployer(id: string | null, quiet = false): Promise<void> {
    const seq = ++openSeq;
    if (!id) {
      set({ open: { id: null, status: "idle", employer: null, error: null } });
      return;
    }
    const had = current().open;
    if (!(quiet && had.id === id && had.employer)) set({ open: { id, status: "loading", employer: had.id === id ? had.employer : null, error: null } });
    try {
      const employer = await api.employer(id);
      if (seq !== openSeq) return;
      set({ open: { id, status: "ready", employer, error: null } });
    } catch (err) {
      if (seq !== openSeq) return;
      set({ open: { id, status: "error", employer: null, error: errorMessage(err, "Couldn't load this employer.") } });
    }
  }

  // --- changing a row in place ------------------------------------------------------------------------------------

  /** Changes one employer wherever it is shown: its row and the open one. */
  function patchEmployer(id: string, change: (e: Employer) => Employer): void {
    set((s) => ({
      rows: s.rows.map((r) => (r.id === id ? change(r) : r)),
      open: s.open.employer?.id === id ? { ...s.open, employer: change(s.open.employer) } : s.open,
    }));
  }

  /** Takes rows the server answered with: replaced where shown; a new one is added by name when the view holds it. */
  function takeRows(fresh: Employer[], removed: string[] = []): void {
    set((s) => {
      const gone = new Set(removed);
      let rows = s.rows.filter((r) => !gone.has(r.id)).map((r) => {
        const answer = fresh.find((f) => f.id === r.id);
        return answer ? merged(r, answer) : r;
      });
      let total = s.total - (s.rows.length - rows.length);
      const plain = !s.q.trim() && !s.industry;
      for (const f of fresh) {
        if (rows.some((r) => r.id === f.id) || !plain || !inView(f, s.view)) continue;
        rows = [...rows, f].sort((a, b) => a.name.localeCompare(b.name));
        total += 1;
      }
      const open = s.open.id && gone.has(s.open.id) ? { id: null, status: "idle" as const, employer: null, error: null } : s.open;
      return { rows, open, total: Math.max(0, total) };
    });
  }

  function setBusy(id: string, on: boolean) {
    set((s) => {
      const busy = { ...s.busy };
      if (on) busy[id] = true;
      else delete busy[id];
      return { busy };
    });
  }

  const bump = (counts: EmployerCounts, key: EmployerView, by: number): EmployerCounts => ({ ...counts, [key]: Math.max(0, counts[key] + by) });

  // --- the live stream --------------------------------------------------------------------------------------------

  function applyEmployer(ev: LiveEmployer): void {
    if ("deleted" in ev) {
      set((s) => ({
        rows: s.rows.filter((r) => r.id !== ev.id),
        total: s.rows.some((r) => r.id === ev.id) ? Math.max(0, s.total - 1) : s.total,
        open: s.open.id === ev.id ? { id: null, status: "idle", employer: null, error: null } : s.open,
      }));
    } else {
      patchEmployer(ev.id, (e) => ({ ...e, ...ev }));
      if (current().open.id === ev.id && !current().busy[ev.id]) void openEmployer(ev.id, true);
    }
    recountSoon();
  }

  const onEvent: LiveHandler = (event, data) => {
    if (event === "employer") applyEmployer(data as LiveEmployer);
    else if (event === "resync") {
      void load();
      const id = current().open.id;
      if (id) void openEmployer(id, true);
    }
  };

  return {
    getState: core.getState,
    subscribe: core.subscribe,
    /** Employers starts it; it reads and listens while the screen is open. */
    start: counted(() => {
      void load();
      set({ live: "connecting" });
      const connection: LiveConnection = openLive(onEvent, (st) => {
        const back = st === "open" && current().live === "closed";
        set({ live: st });
        if (back) void load();
      });
      return () => {
        connection.close();
        if (recountTimer) clearTimeout(recountTimer);
        recountTimer = null;
        cancelFind();
      };
    }).start,
    reload: load,
    loadMore,
    /** Picks a view of the rail. */
    setView: (view: EmployerView) => narrow({ view }),
    /** What is typed in the find field. */
    find,
    setIndustry: (industry: string) => narrow({ industry }),
    openEmployer,

    /** Watch its job board, or stop: the row, the pane and the rail's count at once, back when the server refuses. */
    watch: async (employer: Employer, on: boolean): Promise<boolean> => {
      const site = employer.site;
      if (!site || site.on === on) return false;
      const was = current().counts;
      const flip = (to: boolean) => patchEmployer(employer.id, (e) => (e.site ? { ...e, site: { ...e.site, on: to } } : e));
      setBusy(employer.id, true);
      try {
        return await optimistic(
          () => {
            flip(on);
            set((s) => ({ counts: bump(s.counts, "watched", on ? 1 : -1) }));
          },
          () => api.setSite(site.id, on),
          (err) => {
            flip(!on);
            set({ counts: was, notice: errorMessage(err, "Couldn't change the watch.") });
          },
          (answer) =>
            patchEmployer(employer.id, (e) =>
              e.site ? { ...e, site: { ...e.site, on: answer.on, state: answer.state, status: answer.status, needs: answer.needs } } : e,
            ),
        );
      } finally {
        setBusy(employer.id, false);
      }
    },

    /** A hard pass on its roles (with a reason), or taking it back: the row and the rail's count at once, back when the
     * server refuses. The server's answer says in a line what it changed (`said`), and the open employer is read again,
     * since its roles moved. */
    hardPass: async (employer: Employer, on: boolean, reason = ""): Promise<boolean> => {
      if (!!employer.gated === on) return false;
      const was = { gated: employer.gated, gated_reason: employer.gated_reason ?? null };
      const counts = current().counts;
      setBusy(employer.id, true);
      try {
        const ok = await optimistic(
          () => {
            patchEmployer(employer.id, (e) => ({ ...e, gated: on, gated_reason: on ? reason || null : e.gated_reason }));
            set((s) => ({ counts: bump(s.counts, "hard_pass", on ? 1 : -1), said: null }));
          },
          () => api.editEmployer(employer.id, { gated: on, gated_reason: on ? reason : (employer.gated_reason ?? "") }),
          (err) => {
            patchEmployer(employer.id, (e) => ({ ...e, ...was }));
            set({ counts, notice: errorMessage(err, "Couldn't change the hard pass.") });
          },
          (answer) => {
            patchEmployer(employer.id, (e) => ({ ...e, gated: answer.gated, gated_reason: answer.gated_reason, gated_at: answer.gated_at }));
            if (answer.said) set({ said: answer.said });
          },
        );
        if (ok && current().open.id === employer.id) void openEmployer(employer.id, true);
        return ok;
      } finally {
        setBusy(employer.id, false);
      }
    },

    /** Saves facts set by hand (each becomes the owner's); true when the server took them. */
    editFacts: async (id: string, changes: Partial<Employer>): Promise<boolean> => {
      if (Object.keys(changes).length === 0) return true;
      setBusy(id, true);
      try {
        const answer = await api.editEmployer(id, changes);
        const fresh: Partial<Employer> = { ...answer };
        delete fresh.said;
        patchEmployer(id, (e) => ({ ...e, ...fresh, roles: e.roles, site: e.site, hiring: e.hiring, history: e.history }));
        if (current().open.id === id) void openEmployer(id, true);
        return true;
      } catch (err) {
        set({ notice: errorMessage(err, "Couldn't save the employer.") });
        return false;
      } finally {
        setBusy(id, false);
      }
    },

    /** The employers, other than this one, whose name (or another name they go by) holds what was typed. */
    search: async (q: string, except: string): Promise<Employer[]> => {
      if (!q.trim()) return [];
      const page = await api.employers({ view: "all", q: q.trim(), limit: MATCHES + 1 });
      return page.employers.filter((e) => e.id !== except).slice(0, MATCHES);
    },
    mergePreview: (id: string, into: string): Promise<EmployerMergePreview> => api.employerMergePreview(id, into),
    /** Merges this employer into another: the row that went leaves, the one left changes in place, the counts follow.
     * Answers the one left. */
    merge: async (id: string, into: string): Promise<Employer> => {
      const done = await api.mergeEmployer(id, into);
      takeRows(done.rows, done.removed);
      set({ counts: done.counts, open: { id: done.employer.id, status: "ready", employer: done.employer, error: null } });
      return done.employer;
    },
    splitPreview: (id: string, roles: string[], name: string): Promise<EmployerSplitPreview> => api.employerSplitPreview(id, roles, name),
    /** Moves roles out to the employer of that name: both rows change in place (the new one joins the list when the view
     * holds it). Answers the one they went to. */
    split: async (id: string, roles: string[], name: string): Promise<Employer> => {
      const done = await api.splitEmployer(id, roles, name);
      takeRows(done.rows);
      set({ counts: done.counts, open: { id: done.employer.id, status: "ready", employer: done.employer, error: null } });
      return done.employer;
    },

    dismissNotice: () => set({ notice: null }),
    dismissSaid: () => set({ said: null }),
    /** For tests: what the stream would have said. */
    receive: onEvent,
  };
}

export type EmployersStore = ReturnType<typeof createEmployersStore>;

const context = storeContext<EmployersStore, EmployersState>("useEmployersStore");

/** Hands the store to Employers (a test's own, or the app's). */
export const EmployersStoreProvider = context.Provider;
export const useEmployersStore = context.useStore;
/** Reads part of Employers' state; the component redraws only when that part changes. */
export const useEmployers = context.useSlice;
