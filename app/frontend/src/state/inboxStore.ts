// Notifications' data layer (the screens rebuild spec, screen 1), and the shell's needs-you count: the owner's open
// tasks (every one, not three), the agents' open and finished work, the notifications, and the past decisions read a page
// at a time. It reads once when the app opens and then follows the live stream's `task` and `notification` messages;
// nothing polls. An answer, a Done, a Skip, a Not now and a read show at once, take the server's answer (the task it
// returns) in place, and are put back, and said, when the server refuses.

import { api as defaultApi, ApiError, errorMessage } from "../api/client";
import type { LiveConnection, LiveHandler, LiveTopic } from "../api/live";
import type { DecisionRecord, Notification, Task, TaskOption } from "../api/types/inbox";
import type { LiveEvents } from "../api/types/pipeline";
import { ownerToday, snoozeTomorrow } from "../lib/dates";
import { counted, createState, optimistic, storeContext, topicsOf, type OpenLive } from "./store";

export const INBOX_TOPICS: LiveTopic[] = ["task", "notification"];
/** Past decisions are read this many at a time. */
export const PAST_PAGE = 20;

export type InboxApi = Pick<
  typeof defaultApi,
  "inbox" | "tasks" | "inboxHistory" | "readNotification" | "readAllNotifications" | "taskDone" | "taskSkip" | "taskAnswer" | "taskSnooze"
>;

export interface PastState {
  status: "idle" | "loading" | "ready" | "error";
  items: DecisionRecord[];
  /** Where the next, older page starts; null when there is no more. */
  next: string | null;
  loadingMore: boolean;
}

export interface InboxState {
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  /** The server's today, "2026-10-24". */
  today: string | null;
  /** The owner's open tasks, in the server's order (decisions first, then the soonest due, then the oldest). */
  mine: Task[];
  /** The agents' open tasks. */
  theirs: Task[];
  /** What was finished lately, newest first. */
  done: Task[];
  /** The newest notifications. */
  notifications: Notification[];
  /** How many notifications are unread, of every one (not only those held). */
  unread: number;
  past: PastState;
  live: "connecting" | "open" | "closed";
  /** Tasks and notifications with a change on its way to the server. */
  busy: Record<string, true>;
  /** The last thing that went wrong with an action, in words, until it is dismissed. */
  notice: string | null;
  /** An answer the server refused as too large to send an agent (413): its words stay beside the box they are about. */
  refused: { taskId: string; words: string } | null;
}

function initialState(): InboxState {
  return {
    status: "idle",
    error: null,
    today: null,
    mine: [],
    theirs: [],
    done: [],
    notifications: [],
    unread: 0,
    past: { status: "idle", items: [], next: null, loadingMore: false },
    live: "connecting",
    busy: {},
    notice: null,
    refused: null,
  };
}

/** Whether a task waits on the owner today: open, theirs, and not put off past today. */
export function asksToday(task: Task, today: string | null): boolean {
  return task.state === "open" && task.for.kind === "owner" && !(task.snoozed_until && today && task.snoozed_until > today);
}

/** The needs-you count: the owner's open tasks not put off (what the shell's pill and the rail say). */
export function needsYou(s: Pick<InboxState, "mine" | "today">): number {
  return s.mine.filter((t) => asksToday(t, s.today)).length;
}

/** A finished task as Past decisions lists it (the server's own record of it, `inbox/history`). */
export function recordOf(task: Task): DecisionRecord {
  const chosen = task.options?.find((o) => o.id === task.answer)?.label;
  return {
    at: task.done_at ?? task.created_at,
    title: task.title,
    decision: chosen ?? (task.state === "done" ? "Done" : "Set aside"),
    decided_by: "You",
    why: task.note,
    link: task.link,
  };
}

const without = <T extends { id: string }>(rows: T[], id: string) => rows.filter((r) => r.id !== id);
/** Puts a row where it was (when it was there) or first, keeping a name the stream does not carry. */
function upsert(rows: Task[], task: Task): Task[] {
  const at = rows.findIndex((r) => r.id === task.id);
  if (at < 0) return [task, ...rows];
  const had = rows[at];
  const kept = task.for.kind === "agent" && had.for.kind === "agent" ? { ...task, for: had.for } : task;
  return [...rows.slice(0, at), kept, ...rows.slice(at + 1)];
}

export function createInboxStore(opts: { api?: InboxApi; openLive?: OpenLive } = {}) {
  const api = opts.api ?? defaultApi;
  const openLive = opts.openLive ?? topicsOf(INBOX_TOPICS);
  const core = createState(initialState());
  const set = core.set;
  const current = () => core.getState();
  let loadSeq = 0;
  /** The tasks already on the Past decisions list this visit, so an answer and its stream message add it once. */
  const recorded = new Set<string>();

  // --- loading ----------------------------------------------------------------------------------------------------

  /** Reads the tasks and notifications again, and the past decisions too when they were read already. */
  async function reload(): Promise<void> {
    if (current().past.status === "ready") void loadPast();
    return load();
  }

  async function load(): Promise<void> {
    const seq = ++loadSeq;
    if (current().status !== "ready") set({ status: "loading", error: null });
    try {
      const [inbox, tasks] = await Promise.all([api.inbox(), api.tasks()]);
      if (seq !== loadSeq) return;
      set({
        status: "ready",
        error: null,
        today: tasks.today || inbox.today,
        mine: tasks.for_you,
        theirs: tasks.for_agents,
        done: tasks.done,
        notifications: inbox.notifications,
        unread: inbox.unread_count,
      });
    } catch (err) {
      if (seq !== loadSeq) return;
      set({ status: "error", error: errorMessage(err, "Couldn't load your notifications.") });
    }
  }

  /** The first page of past decisions (again, when asked again). */
  async function loadPast(): Promise<void> {
    set((s) => ({ past: { ...s.past, status: s.past.status === "ready" ? "ready" : "loading" } }));
    try {
      const page = await api.inboxHistory({ limit: PAST_PAGE });
      recorded.clear();
      set({ past: { status: "ready", items: page.items, next: page.next_before, loadingMore: false } });
    } catch (err) {
      set((s) => ({ past: { ...s.past, status: "error" }, notice: errorMessage(err, "Couldn't load your past decisions.") }));
    }
  }

  /** The next, older page of past decisions, under the ones shown. */
  async function loadEarlier(): Promise<void> {
    const past = current().past;
    if (!past.next || past.loadingMore) return;
    set((s) => ({ past: { ...s.past, loadingMore: true } }));
    try {
      const page = await api.inboxHistory({ before: past.next, limit: PAST_PAGE });
      set((s) => ({ past: { ...s.past, items: [...s.past.items, ...page.items], next: page.next_before, loadingMore: false } }));
    } catch (err) {
      set((s) => ({ past: { ...s.past, loadingMore: false }, notice: errorMessage(err, "Couldn't load the earlier ones.") }));
    }
  }

  // --- placing a task -------------------------------------------------------------------------------------------------

  /** Puts a task where its state and owner say: an open one in its list, a finished one in Done lately (and, the
   * owner's, on Past decisions when that is read). */
  function place(task: Task): void {
    set((s) => {
      const mineNow = without(s.mine, task.id);
      const theirsNow = without(s.theirs, task.id);
      const open = task.state === "open";
      const patch: Partial<InboxState> = {
        mine: open && task.for.kind === "owner" ? upsert(s.mine, task) : mineNow,
        theirs: open && task.for.kind === "agent" ? upsert(s.theirs, task) : theirsNow,
        done: open ? without(s.done, task.id) : upsert(without(s.done, task.id), task),
      };
      if (!open && task.for.kind === "owner" && s.past.status === "ready" && !recorded.has(task.id)) {
        recorded.add(task.id);
        patch.past = { ...s.past, items: [recordOf(task), ...s.past.items] };
      }
      return patch;
    });
  }

  function forget(id: string): void {
    set((s) => ({ mine: without(s.mine, id), theirs: without(s.theirs, id), done: without(s.done, id) }));
  }

  function setBusy(id: string, on: boolean) {
    set((s) => {
      const busy = { ...s.busy };
      if (on) busy[id] = true;
      else delete busy[id];
      return { busy };
    });
  }

  /** A change to one of the owner's tasks: shown at once, the server's task taken in its place, put back when refused. */
  async function change(task: Task, shown: Task, call: () => Promise<Task>, failed: string): Promise<boolean> {
    const { mine: before, past: pastBefore } = current();
    const wasRecorded = recorded.has(task.id);
    try {
      return await optimistic(
        () => {
          set({ refused: null });
          setBusy(task.id, true);
          place(shown);
        },
        call,
        (err) => {
          if (!wasRecorded) recorded.delete(task.id);
          set({ mine: before, done: without(current().done, task.id), past: pastBefore });
          if (err instanceof ApiError && err.status === 413) set({ refused: { taskId: task.id, words: err.message } });
          else set({ notice: errorMessage(err, failed) });
        },
        (answer) => place(answer),
      );
    } finally {
      setBusy(task.id, false);
    }
  }

  const now = () => new Date().toISOString();

  // --- the live stream --------------------------------------------------------------------------------------------

  function applyNotification(ev: LiveEvents["notification"]): void {
    if ("deleted" in ev) {
      set((s) => {
        const had = s.notifications.find((n) => n.id === ev.id);
        return { notifications: without(s.notifications, ev.id), unread: Math.max(0, s.unread - (had && !had.read_at ? 1 : 0)) };
      });
      return;
    }
    const { was_read: wasRead, ...note } = ev;
    set((s) => {
      const had = s.notifications.find((n) => n.id === note.id);
      // What this changes in the count: against the row held when there is one (a read shown already counts once),
      // else against what the server says it was.
      const before = had ? !!had.read_at : wasRead;
      const after = !!note.read_at;
      const delta = before === null ? (after ? 0 : 1) : before === after ? 0 : after ? -1 : 1;
      const notifications = had ? s.notifications.map((n) => (n.id === note.id ? note : n)) : [note, ...s.notifications];
      return { notifications, unread: Math.max(0, s.unread + delta) };
    });
  }

  const onEvent: LiveHandler = (event, data) => {
    if (event === "task") {
      const ev = data as LiveEvents["task"];
      if ("deleted" in ev) forget(ev.id);
      else place(ev);
    } else if (event === "notification") applyNotification(data as LiveEvents["notification"]);
    else if (event === "resync") void reload();
  };

  // --- actions ----------------------------------------------------------------------------------------------------

  const finished = (task: Task, state: Task["state"], extra: Partial<Task> = {}): Task => ({ ...task, state, done_at: now(), ...extra });

  return {
    getState: core.getState,
    subscribe: core.subscribe,
    /** The shell and Notifications start it; it reads and listens while either is open. */
    start: counted(() => {
      void load();
      set({ live: "connecting" });
      const connection: LiveConnection = openLive(onEvent, (s) => {
        const back = s === "open" && current().live === "closed";
        set({ live: s });
        if (back) void reload();
      });
      return () => connection.close();
    }).start,
    reload,
    loadPast,
    loadEarlier,
    /** Answers a decision with one of its options (and the owner's words, when the option asks for them). */
    answer: (task: Task, option: TaskOption, note?: string) =>
      change(task, finished(task, "done", { answer: option.id, note: note ?? null }), () => api.taskAnswer(task.id, option.id, note), "Couldn't send that answer."),
    done: (task: Task) => change(task, finished(task, "done"), () => api.taskDone(task.id), "Couldn't mark it done."),
    skip: (task: Task) => change(task, finished(task, "skipped"), () => api.taskSkip(task.id), "Couldn't set it aside."),
    /** Puts it off until tomorrow, by the server's today. A page left open past the owner's midnight reads again first, so
     * "tomorrow" is never a day that has already begun. */
    notNow: async (task: Task) => {
      const held = current().today;
      if (held && held < ownerToday()) await load();
      const until = snoozeTomorrow(current().today ?? undefined);
      return change(task, { ...task, snoozed_until: until }, () => api.taskSnooze(task.id, until), "Couldn't put it off.");
    },
    /** Marks one notification read: at once, and back when the server refuses. */
    read: async (id: string): Promise<boolean> => {
      const had = current().notifications.find((n) => n.id === id);
      if (!had || had.read_at) return false;
      return optimistic(
        () => set((s) => ({ notifications: s.notifications.map((n) => (n.id === id ? { ...n, read_at: now() } : n)), unread: Math.max(0, s.unread - 1) })),
        () => api.readNotification(id),
        (err) =>
          set((s) => ({
            notifications: s.notifications.map((n) => (n.id === id ? { ...n, read_at: null } : n)),
            unread: s.unread + 1,
            notice: errorMessage(err, "Couldn't mark it read."),
          })),
      );
    },
    readAll: async (): Promise<boolean> => {
      const { notifications, unread } = current();
      if (unread === 0) return false;
      const at = now();
      return optimistic(
        () => set((s) => ({ notifications: s.notifications.map((n) => ({ ...n, read_at: n.read_at ?? at })), unread: 0 })),
        () => api.readAllNotifications(),
        (err) => set({ notifications, unread, notice: errorMessage(err, "Couldn't mark them all read.") }),
      );
    },
    clearRefused: () => set({ refused: null }),
    dismissNotice: () => set({ notice: null }),
    /** For tests: what the stream would have said. */
    receive: onEvent,
  };
}

export type InboxStore = ReturnType<typeof createInboxStore>;

const context = storeContext<InboxStore, InboxState>("useInboxStore");

/** Hands the store to the shell and Notifications (a test's own, or the app's). */
export const InboxStoreProvider = context.Provider;
export const useInboxStore = context.useStore;
/** Reads part of Notifications' state; the component redraws only when that part changes. */
export const useInbox = context.useSlice;
