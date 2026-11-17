import { describe, expect, it, vi } from "vitest";
import type { LiveHandler, LiveTopic } from "../api/live";
import { liveUrl } from "../api/live";
import { createLiveHub } from "./live";

// The page's one live stream: every store asks for its topics and hears only those (and hello and resync); the page
// holds one connection, opened again only when a store wants a topic it does not carry.

function fakeOpener() {
  const opened: { topics: LiveTopic[]; push: LiveHandler; state: (s: "open" | "closed") => void; close: ReturnType<typeof vi.fn> }[] = [];
  const open = (topics: LiveTopic[], push: LiveHandler, state: (s: "open" | "closed") => void) => {
    const close = vi.fn();
    opened.push({ topics: [...topics].sort(), push, state, close });
    return { close };
  };
  return { opened, open };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("the live hub", () => {
  it("opens one stream for the stores that start together, with every topic they want, and hands each only its own", async () => {
    const { opened, open } = fakeOpener();
    const hub = createLiveHub(open);
    const pipeline = vi.fn();
    const inbox = vi.fn();
    hub.subscribe(["role", "counts"], pipeline);
    hub.subscribe(["task", "notification"], inbox);
    await tick();
    expect(opened).toHaveLength(1);
    expect(opened[0].topics).toEqual(["counts", "notification", "role", "task"]);
    opened[0].push("task", { id: "t1", deleted: true });
    opened[0].push("role", {} as never);
    opened[0].push("hello", {});
    expect(inbox.mock.calls.map((c) => c[0])).toEqual(["task", "hello"]);
    expect(pipeline.mock.calls.map((c) => c[0])).toEqual(["role", "hello"]);
  });

  it("opens the stream again for a new topic, and tells the stores already there to read again once it says hello", async () => {
    const { opened, open } = fakeOpener();
    const hub = createLiveHub(open);
    const pipeline = vi.fn();
    hub.subscribe(["role"], pipeline);
    await tick();
    const inbox = vi.fn();
    hub.subscribe(["task"], inbox);
    await tick();
    expect(opened).toHaveLength(2);
    expect(opened[0].close).toHaveBeenCalled();
    expect(opened[1].topics).toEqual(["role", "task"]);
    opened[1].push("hello", {});
    expect(pipeline.mock.calls.map((c) => c[0])).toEqual(["resync", "hello"]);
    expect(inbox.mock.calls.map((c) => c[0])).toEqual(["hello"]);
  });

  it("keeps the stream as it is when a store leaves, and closes it when the last one does", async () => {
    const { opened, open } = fakeOpener();
    const hub = createLiveHub(open);
    const a = hub.subscribe(["role"], vi.fn());
    const b = hub.subscribe(["task"], vi.fn());
    await tick();
    a.close();
    await tick();
    expect(opened).toHaveLength(1);
    expect(opened[0].close).not.toHaveBeenCalled();
    // A store coming back to a topic the stream still carries changes nothing.
    hub.subscribe(["role"], vi.fn());
    await tick();
    expect(opened).toHaveLength(1);
    b.close();
    await tick();
    expect(opened[0].close).not.toHaveBeenCalled();
  });

  it("closes when nobody listens, and tells a listener the stream's state as it changes", async () => {
    const { opened, open } = fakeOpener();
    const hub = createLiveHub(open);
    const state = vi.fn();
    const sub = hub.subscribe(["task"], vi.fn(), state);
    await tick();
    opened[0].state("open");
    expect(state).toHaveBeenCalledWith("open");
    sub.close();
    await tick();
    expect(opened[0].close).toHaveBeenCalled();
    expect(hub.carried()).toEqual([]);
  });

  it("asks the server for the topics in the address", () => {
    expect(liveUrl()).toBe("/api/v1/live");
    expect(liveUrl(["task", "notification"])).toBe("/api/v1/live?topics=notification,task");
  });
});
