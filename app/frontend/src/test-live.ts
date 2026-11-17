// A stand-in for the browser's EventSource and a timer watcher, for the tests of screens that listen to the page's
// live stream (state/live.ts). Only test files import this.
import { act } from "@testing-library/react";
import { vi } from "vitest";
import type { LiveArea } from "./api/types/pipeline";

type Handler = (e: MessageEvent<string>) => void;

/** Puts a fake EventSource where the browser's goes, and hands back what a test needs to speak through it. Call
 * `restore()` after the test. A message sent before the screen has subscribed is sent to nobody, as in the browser. */
export function fakeLive() {
  const sources: FakeSource[] = [];
  class FakeSource {
    url: string;
    handlers: Record<string, Handler[]> = {};
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;
    closed = false;
    constructor(url: string) {
      this.url = url;
      sources.push(this);
    }
    addEventListener(name: string, fn: Handler) {
      (this.handlers[name] ??= []).push(fn);
    }
    close() {
      this.closed = true;
    }
  }
  const slot = globalThis as { EventSource?: unknown };
  const real = slot.EventSource;
  slot.EventSource = FakeSource;

  /** The streams not closed. */
  const streams = () => sources.filter((s) => !s.closed);
  // The hub opens its stream a microtask after a screen subscribes (state/live.ts `settle`): give it that, with no timer.
  async function untilStream() {
    for (let i = 0; i < 20 && streams().length === 0; i++) await act(async () => { await Promise.resolve(); });
  }
  async function send(name: string, data: unknown = {}) {
    await untilStream();
    await act(async () => {
      for (const s of streams()) for (const fn of s.handlers[name] ?? []) fn(new MessageEvent(name, { data: JSON.stringify(data) }));
    });
  }
  return {
    sources,
    streams,
    send,
    /** The server says rows of this area changed. */
    changed: (area: LiveArea, ids: string[] = []) => send("changed", { area, ids }),
    /** The server says messages were dropped. */
    resync: () => send("resync"),
    /** The stream opens. */
    opened: async () => {
      await untilStream();
      await act(async () => { for (const s of streams()) s.onopen?.(); });
    },
    /** The stream drops. */
    dropped: async () => {
      await untilStream();
      await act(async () => { for (const s of streams()) s.onerror?.(); });
    },
    restore: () => {
      if (real === undefined) delete slot.EventSource;
      else slot.EventSource = real;
    },
  };
}

/** Watches the timers a screen sets from now on. `seen()` lists the delays among `delays` that a timer was set with: the
 * ones the old polling used (1.5 s, 2 s, 10 s, 15 s, 30 s). 5 s is left out of the default because the test library's own
 * wait uses it; ask for it by name in a test that waits with nothing but `await` on the live helpers. */
export function pollTimers(delays: number[] = [1500, 2000, 10_000, 15_000, 30_000]) {
  // Not a poll: these two lines watch for polling timers in a test, and set none.
  const timeouts = vi.spyOn(globalThis, "setTimeout");
  const intervals = vi.spyOn(globalThis, "setInterval");
  return {
    seen: () =>
      [...timeouts.mock.calls, ...intervals.mock.calls]
        .map((call) => call[1])
        .filter((ms): ms is number => typeof ms === "number" && delays.includes(ms)),
    stop: () => {
      timeouts.mockRestore();
      intervals.mockRestore();
    },
  };
}
