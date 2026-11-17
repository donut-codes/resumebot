// The live stream (GET /api/v1/live): server-sent events the backend pushes as changes are committed, typed by event
// name (`LiveEvents`). The page is told what changed instead of asking again. EventSource reconnects by itself; the
// first message on every connection is `hello` (with the counts when roles or counts were asked for), and `resync` means
// messages were dropped. A stream asks for the topics it shows (`?topics=notification,task`); state/live.ts keeps the
// one stream the whole page shares.

import type { LiveEvents } from "./types/pipeline";

export const LIVE_PATH = "/api/v1/live";

/** The messages a stream can ask for by name; `hello` and `resync` always come. */
export type LiveTopic = "changed" | "role" | "counts" | "packet" | "summary" | "task" | "notification" | "employer" | "profile_version" | "site";

export type LiveHandler = <K extends keyof LiveEvents>(event: K, data: LiveEvents[K]) => void;

export interface LiveConnection {
  close: () => void;
}

/** What opens a stream: the browser's EventSource by default, a fake one in tests. */
export type EventSourceFactory = (url: string) => EventSource;

const EVENTS: (keyof LiveEvents)[] = [
  "hello", "changed", "role", "counts", "packet", "summary", "task", "notification", "employer", "profile_version", "site", "resync",
];

/** The stream's address for these topics (every topic when none are named). */
export function liveUrl(topics?: readonly LiveTopic[]): string {
  if (!topics || topics.length === 0) return LIVE_PATH;
  return `${LIVE_PATH}?topics=${[...topics].sort().join(",")}`;
}

/** Opens the stream and hands every message to `onEvent`; `onState` says when it is open and when it dropped. */
export function openLive(
  onEvent: LiveHandler,
  onState: (state: "open" | "closed") => void = () => {},
  make?: EventSourceFactory,
  topics?: readonly LiveTopic[],
): LiveConnection {
  if (!make && typeof EventSource === "undefined") {
    // No stream in this browser (or in a test's): the page shows what it read and says the stream is closed.
    onState("closed");
    return { close: () => {} };
  }
  const source = (make ?? ((url: string) => new EventSource(url)))(liveUrl(topics));
  for (const name of EVENTS) {
    source.addEventListener(name, (msg) => {
      const raw = (msg as MessageEvent<string>).data;
      let data: unknown = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        return;
      }
      onEvent(name, data as LiveEvents[typeof name]);
    });
  }
  source.onopen = () => onState("open");
  source.onerror = () => onState("closed");
  return { close: () => source.close() };
}
