import { describe, expect, it, vi } from "vitest";
import { liveUrl, openLive } from "./live";

// The browser's listener for the stream: it hands every named message to the page, `changed` included.

class FakeSource {
  listeners = new Map<string, (msg: MessageEvent<string>) => void>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {}
  addEventListener(name: string, fn: (msg: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {
    this.closed = true;
  }
  emit(name: string, data: unknown) {
    this.listeners.get(name)?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
}

describe("the live stream's listener", () => {
  it("hands a changed message to the page, and asks the server for it by topic", () => {
    const made: FakeSource[] = [];
    const make = (url: string) => {
      const source = new FakeSource(url);
      made.push(source);
      return source as unknown as EventSource;
    };
    const seen = vi.fn();
    openLive(seen, undefined, make, ["changed"]);
    expect(made[0].url).toBe("/api/v1/live?topics=changed");
    made[0].emit("changed", { area: "go" });
    expect(seen).toHaveBeenCalledWith("changed", { area: "go" });
  });

  it("puts the topics in the address in order", () => {
    expect(liveUrl(["task", "changed"])).toBe("/api/v1/live?topics=changed,task");
  });
});
