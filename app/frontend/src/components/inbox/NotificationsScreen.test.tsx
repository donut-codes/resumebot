import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { LiveHandler } from "../../api/live";
import type { Notification, Task } from "../../api/types/inbox";
import { createInboxStore, InboxStoreProvider, type InboxApi } from "../../state/inboxStore";
import { render } from "../../test-utils";
import NotificationsScreen from "./NotificationsScreen";

// Notifications on a made-up store: the rail's parts and counts, a decision's options as one click each, answering folds
// the row out and opens the next, opening a notification reads it, and a task raised elsewhere shows without a reload.

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, title: `Merge Co ${id} into Co?`, kind: "employer_merge", state: "open", detail: `Both post to the same site (${id}).`, due: null,
    for: { kind: "owner", name: "You" }, options: [{ id: "keep", label: "Keep separate" }, { id: "merge", label: "Merge", description: "One employer." }],
    recommended: "merge", answer: null, note: null, link: null, snoozed_until: null, created_at: "2026-10-24T08:00:00Z", done_at: null, ...extra,
  };
}

function note(id: string, extra: Partial<Notification> = {}): Notification {
  return { id, kind: "packet_ready", title: `Packet ready ${id}`, body: "Written and checked.", level: "info", link: { kind: "opportunity", id: `r-${id}` }, created_at: "2026-10-24T08:00:00Z", read_at: null, ...extra };
}

function setup(data: { mine?: Task[]; notes?: Notification[]; unread?: number } = {}, overrides: Partial<InboxApi> = {}) {
  let push: LiveHandler = () => {};
  const api = {
    inbox: vi.fn(async () => ({ today: "2026-10-24", count: 0, items: [], notifications: data.notes ?? [], unread_count: data.unread ?? 0 })),
    tasks: vi.fn(async () => ({ today: "2026-10-24", for_you: data.mine ?? [], for_agents: [], done: [] })),
    inboxHistory: vi.fn(async () => ({ items: [], next_before: null })),
    readNotification: vi.fn(async () => ({ changed: 1 })),
    readAllNotifications: vi.fn(async () => ({ changed: 1 })),
    taskDone: vi.fn(),
    taskSkip: vi.fn(async (id: string) => ({ ...task(id), state: "skipped", done_at: "2026-10-24T09:00:00Z" })),
    taskAnswer: vi.fn(async (id: string, option: string) => ({ ...task(id), state: "done", answer: option, done_at: "2026-10-24T09:00:00Z" })),
    taskSnooze: vi.fn(),
    ...overrides,
  } as unknown as InboxApi;
  const store = createInboxStore({ api, openLive: (onEvent) => ((push = onEvent), { close: () => {} }) });
  function Screen() {
    const [s] = useState(store);
    return (
      <InboxStoreProvider store={s}>
        <NotificationsScreen />
      </InboxStoreProvider>
    );
  }
  return { api, store, push: ((e, d) => push(e, d)) as LiveHandler, Screen };
}

describe("Notifications", () => {
  it("opens on Needs you: every task listed, the first open, its options one click each with the suggested one first", async () => {
    const { Screen } = setup({ mine: [task("a"), task("b"), task("c"), task("d")], unread: 2 });
    render(<Screen />, { hash: "#/inbox" });
    const rail = await screen.findByRole("navigation", { name: "Notifications" });
    await waitFor(() => expect(within(rail).getByRole("button", { name: /Needs you/ })).toHaveTextContent("4"));
    expect(within(rail).getByRole("button", { name: /^Notifications/ })).toHaveTextContent("2");
    expect(screen.getByRole("heading", { level: 1, name: "Needs you" })).toBeInTheDocument();
    // All four, not three.
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    const pane = screen.getByRole("region", { name: "Merge Co a into Co?" });
    const answers = within(pane).getByRole("group", { name: "Your answer" });
    expect(within(answers).getAllByRole("button").map((b) => b.textContent)).toEqual(["Merge", "Keep separate", "Not now", "Skip"]);
    expect(within(answers).getByRole("button", { name: "Merge" })).toHaveClass("fi-btn-go");
    expect(within(pane).getByText("Both post to the same site (a).")).toBeInTheDocument();
  });

  it("draws a decision's facts side by side when the code that raised it gave some, and none when it gave none", async () => {
    const facts = [{ label: "Co a", value: "1 role" }, { label: "Co", value: "3 roles" }, { label: "Shared careers site", value: "co.test" }];
    const { Screen } = setup({ mine: [task("a", { facts }), task("b")] });
    render(<Screen />, { hash: "#/inbox" });
    const pane = await screen.findByRole("region", { name: "Merge Co a into Co?" });
    const strip = within(pane).getByText("Shared careers site").closest("dl")!;
    expect(strip).toHaveTextContent("co.test");
    expect(strip).toHaveTextContent("3 roles");
    fireEvent.click(screen.getByRole("button", { name: "Merge Co b into Co?" }));
    const other = await screen.findByRole("region", { name: "Merge Co b into Co?" });
    expect(within(other).queryByRole("definition")).toBeNull();
  });

  it("answering folds the row out, opens the next one, and moves the count, with no list read again", async () => {
    const { Screen, api } = setup({ mine: [task("a"), task("b")] });
    render(<Screen />, { hash: "#/inbox" });
    const pane = await screen.findByRole("region", { name: "Merge Co a into Co?" });
    fireEvent.click(within(pane).getByRole("button", { name: "Merge" }));
    expect(await screen.findByRole("region", { name: "Merge Co b into Co?" })).toBeInTheDocument();
    expect(api.taskAnswer).toHaveBeenCalledWith("a", "merge", undefined);
    const rail = screen.getByRole("navigation", { name: "Notifications" });
    await waitFor(() => expect(within(rail).getByRole("button", { name: /Needs you/ })).toHaveTextContent("1"));
    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(1));
    expect(api.tasks).toHaveBeenCalledTimes(1);
  });

  it("an option that wants the owner's words opens a box for them, and the answer carries them", async () => {
    const asks = task("a", { options: [{ id: "other", label: "Something else", note: "What should it be?" }], recommended: null });
    const { Screen, api } = setup({ mine: [asks] });
    render(<Screen />, { hash: "#/inbox" });
    fireEvent.click(await screen.findByRole("button", { name: "Something else" }));
    fireEvent.change(screen.getByRole("textbox", { name: "What should it be?" }), { target: { value: "A new role" } });
    fireEvent.click(screen.getByRole("button", { name: "Something else" }));
    await waitFor(() => expect(api.taskAnswer).toHaveBeenCalledWith("a", "other", "A new role"));
  });

  it("a task raised elsewhere shows at once, from the live stream", async () => {
    const { Screen, push } = setup({ mine: [task("a")] });
    render(<Screen />, { hash: "#/inbox" });
    await screen.findByRole("region", { name: "Merge Co a into Co?" });
    act(() => push("task", task("z", { title: "A new question" })));
    expect(await screen.findByRole("button", { name: "A new question" })).toBeInTheDocument();
  });

  it("Notifications lists them newest first; opening one reads it, and Mark all read reads the rest", async () => {
    const notes = [note("old", { created_at: "2026-10-20T08:00:00Z" }), note("new"), note("seen", { read_at: "2026-10-23T08:00:00Z", created_at: "2026-10-22T08:00:00Z" })];
    const { Screen, api } = setup({ notes, unread: 2 });
    render(<Screen />, { hash: "#/inbox/notifications" });
    expect(await screen.findByRole("heading", { level: 1, name: "Notifications" })).toBeInTheDocument();
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map((r) => within(r).getByRole("button").textContent)).toEqual(["Packet ready new", "Packet ready seen", "Packet ready old"]);
    expect(rows[1]).toHaveClass("is-quiet");
    fireEvent.click(within(rows[2]).getByRole("button", { name: "Packet ready old" }));
    expect(api.readNotification).toHaveBeenCalledWith("old");
    const pane = await screen.findByRole("region", { name: "Packet ready old" });
    expect(within(pane).getByRole("link", { name: "See the role" })).toHaveAttribute("href", "#/board/r-old");
    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }));
    expect(api.readAllNotifications).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Mark all read" })).toBeNull());
  });

  it("Past decisions reads its first page when opened and the earlier ones on Show earlier", async () => {
    const history = vi
      .fn()
      .mockResolvedValueOnce({ items: [{ at: "2026-10-22T08:00:00Z", title: "Merged A", decision: "Merge", decided_by: "You", why: "Same site.", link: null }], next_before: "x" })
      .mockResolvedValueOnce({ items: [{ at: "2026-10-01T08:00:00Z", title: "Kept B apart", decision: "Keep separate", decided_by: "You", why: null, link: null }], next_before: null });
    const { Screen } = setup({}, { inboxHistory: history });
    render(<Screen />, { hash: "#/inbox/decisions" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Merged A" })).toBeInTheDocument());
    expect(history).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Same site.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show earlier" }));
    expect(await screen.findByRole("button", { name: "Kept B apart" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show earlier" })).toBeNull();
  });

  it("says when nothing needs you", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/inbox" });
    expect(await screen.findByText("Nothing needs you right now.")).toBeInTheDocument();
  });
});
