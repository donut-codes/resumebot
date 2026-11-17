import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LiveHandler } from "../api/live";
import type { Notification, Task } from "../api/types/inbox";
import { createInboxStore, needsYou, type InboxApi } from "./inboxStore";

// Notifications' data layer against a made-up server: it reads once, follows the live stream's task and notification
// messages, and shows an answer, a read or a Not now at once, taking it back (and saying so) when the server refuses.

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, title: `Task ${id}`, kind: "decision", state: "open", detail: null, due: null, for: { kind: "owner", name: "You" },
    options: [{ id: "yes", label: "Yes" }, { id: "no", label: "No" }], recommended: "yes", answer: null, note: null, link: null,
    snoozed_until: null, created_at: "2026-10-20T08:00:00Z", done_at: null, ...extra,
  };
}

function note(id: string, extra: Partial<Notification> = {}): Notification {
  return { id, kind: "note", title: `Note ${id}`, body: null, level: "info", link: null, created_at: "2026-10-20T08:00:00Z", read_at: null, ...extra };
}

function setup(overrides: Partial<InboxApi> = {}, data: { mine?: Task[]; notes?: Notification[]; unread?: number } = {}) {
  let push: LiveHandler = () => {};
  const api = {
    inbox: vi.fn(async () => ({
      today: "2026-10-24", count: 0, items: [], notifications: data.notes ?? [], unread_count: data.unread ?? 0,
    })),
    tasks: vi.fn(async () => ({ today: "2026-10-24", for_you: data.mine ?? [], for_agents: [], done: [] })),
    inboxHistory: vi.fn(async () => ({ items: [], next_before: null })),
    readNotification: vi.fn(async () => ({ changed: 1 })),
    readAllNotifications: vi.fn(async () => ({ changed: 1 })),
    taskDone: vi.fn(),
    taskSkip: vi.fn(),
    taskAnswer: vi.fn(),
    taskSnooze: vi.fn(),
    ...overrides,
  } as unknown as InboxApi;
  const close = vi.fn();
  const store = createInboxStore({ api, openLive: (onEvent) => ((push = onEvent), { close }) });
  return { store, api, push: ((e, d) => push(e, d)) as LiveHandler, close };
}

async function started(overrides: Partial<InboxApi> = {}, data: Parameters<typeof setup>[1] = {}) {
  const s = setup(overrides, data);
  const stop = s.store.start();
  await vi.waitFor(() => expect(s.store.getState().status).toBe("ready"));
  return { ...s, stop };
}

describe("the inbox store", () => {
  it("reads every open task (not three) and counts those not put off as needing you", async () => {
    const mine = [task("a"), task("b"), task("c"), task("d"), task("e", { snoozed_until: "2026-10-30" })];
    const { store } = await started({}, { mine });
    expect(store.getState().mine).toHaveLength(5);
    expect(needsYou(store.getState())).toBe(4);
  });

  it("answers at once and takes the server's task in its place, with no list read again", async () => {
    const answerTask = vi.fn(async () => task("a", { state: "done", answer: "no", done_at: "2026-10-24T09:00:00Z", note: "server" }));
    const { store, api } = await started({ taskAnswer: answerTask }, { mine: [task("a"), task("b")] });
    const pending = store.answer(task("a"), { id: "no", label: "No" });
    expect(store.getState().mine.map((t) => t.id)).toEqual(["b"]);
    expect(await pending).toBe(true);
    expect(store.getState().done[0]).toMatchObject({ id: "a", answer: "no", note: "server" });
    expect(api.tasks).toHaveBeenCalledTimes(1);
  });

  it("puts a refused answer back where it was and says why", async () => {
    const { store } = await started({ taskAnswer: vi.fn(async () => Promise.reject(new ApiError("that decision is already made", 409))) }, { mine: [task("a"), task("b")] });
    expect(await store.answer(task("a"), { id: "yes", label: "Yes" })).toBe(false);
    expect(store.getState().mine.map((t) => t.id)).toEqual(["a", "b"]);
    expect(store.getState().done).toEqual([]);
    expect(store.getState().notice).toBe("that decision is already made");
  });

  it("keeps a too-long answer's refusal beside its task instead of a notice", async () => {
    const { store } = await started({ taskAnswer: vi.fn(async () => Promise.reject(new ApiError("Too long to send the agent.", 413))) }, { mine: [task("a")] });
    await store.answer(task("a"), { id: "yes", label: "Yes", note: "Why?" }, "a lot of words");
    expect(store.getState().refused).toEqual({ taskId: "a", words: "Too long to send the agent." });
    expect(store.getState().notice).toBeNull();
    expect(store.getState().mine.map((t) => t.id)).toEqual(["a"]);
  });

  it("puts a task off until tomorrow by the server's today", async () => {
    const snooze = vi.fn(async (id: string, until: string | null) => task(id, { snoozed_until: until }));
    const { store } = await started({ taskSnooze: snooze as unknown as InboxApi["taskSnooze"] }, { mine: [task("a")] });
    await store.notNow(task("a"));
    expect(snooze).toHaveBeenCalledWith("a", "2026-10-25");
    expect(needsYou(store.getState())).toBe(0);
  });

  it("reads again before putting off a task when the page was left open past the owner's midnight", async () => {
    let today = "2026-10-24";
    const tasks = vi.fn(async () => ({ today, for_you: [task("a")], for_agents: [], done: [] }));
    const snooze = vi.fn(async (id: string, until: string | null) => task(id, { snoozed_until: until }));
    const { store } = await started({ tasks, taskSnooze: snooze as unknown as InboxApi["taskSnooze"] });
    today = "2026-10-25"; // the server's day moved on; the page has not heard
    await store.notNow(task("a"));
    expect(snooze).toHaveBeenCalledWith("a", "2026-10-26");
  });

  it("follows the stream: a task raised, answered elsewhere or removed changes the lists and the count", async () => {
    const { store, push } = await started({}, { mine: [task("a")] });
    push("task", task("b"));
    expect(needsYou(store.getState())).toBe(2);
    push("task", task("a", { state: "done", done_at: "2026-10-24T10:00:00Z" }));
    expect(store.getState().mine.map((t) => t.id)).toEqual(["b"]);
    expect(store.getState().done.map((t) => t.id)).toEqual(["a"]);
    push("task", { id: "b", deleted: true });
    expect(needsYou(store.getState())).toBe(0);
    // An agent's task goes to the agents' list, keeping the name the stream does not carry.
    push("task", task("g", { for: { kind: "agent", name: "scout" } }));
    expect(store.getState().theirs.map((t) => t.id)).toEqual(["g"]);
  });

  it("counts unread once, whether the read was shown here first or only told by the stream", async () => {
    const { store, push } = await started({}, { notes: [note("n1"), note("n2")], unread: 5 });
    await store.read("n1");
    expect(store.getState().unread).toBe(4);
    push("notification", { ...note("n1", { read_at: "2026-10-24T10:00:00Z" }), was_read: false });
    expect(store.getState().unread).toBe(4);
    // One this page never held: the stream's word on what it was.
    push("notification", { ...note("n9", { read_at: "2026-10-24T10:00:00Z" }), was_read: false });
    expect(store.getState().unread).toBe(3);
    push("notification", { ...note("n10"), was_read: null });
    expect(store.getState().unread).toBe(4);
    expect(store.getState().notifications[0].id).toBe("n10");
  });

  it("takes a read back when the server refuses it", async () => {
    const { store } = await started({ readNotification: vi.fn(async () => Promise.reject(new Error("down"))) }, { notes: [note("n1")], unread: 1 });
    expect(await store.read("n1")).toBe(false);
    expect(store.getState().unread).toBe(1);
    expect(store.getState().notifications[0].read_at).toBeNull();
    expect(store.getState().notice).toBe("Couldn't mark it read.");
  });

  it("reads past decisions a page at a time, and puts an answer on them once", async () => {
    const history = vi
      .fn()
      .mockResolvedValueOnce({ items: [{ at: "2026-10-22T08:00:00Z", title: "Old", decision: "Yes", decided_by: "You", why: null, link: null }], next_before: "2026-10-22T08:00:00Z" })
      .mockResolvedValueOnce({ items: [{ at: "2026-10-01T08:00:00Z", title: "Older", decision: "No", decided_by: "You", why: null, link: null }], next_before: null });
    const answered = task("a", { state: "done", answer: "yes", done_at: "2026-10-24T09:00:00Z" });
    const { store, push } = await started({ inboxHistory: history, taskAnswer: vi.fn(async () => answered) }, { mine: [task("a")] });
    await store.loadPast();
    await store.loadEarlier();
    expect(store.getState().past.items.map((r) => r.title)).toEqual(["Old", "Older"]);
    expect(store.getState().past.next).toBeNull();
    await store.answer(task("a"), { id: "yes", label: "Yes" });
    push("task", answered);
    expect(store.getState().past.items.map((r) => r.title)).toEqual(["Task a", "Old", "Older"]);
    expect(store.getState().past.items[0].decision).toBe("Yes");
  });

  it("reads everything again when the stream fell behind, and closes it when the last screen stops", async () => {
    const { store, push, api, close, stop } = await started();
    push("resync", {});
    await vi.waitFor(() => expect(api.inbox).toHaveBeenCalledTimes(2));
    stop();
    expect(close).toHaveBeenCalled();
    expect(store.getState().status).toBe("ready");
  });
});
