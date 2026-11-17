// The one live stream the page shares (the screens rebuild spec, "Live events"): every store asks for its own
// topics and is handed only those messages, but the page holds one connection, asking for every topic any store wants.
// When a store wants a topic the stream does not carry, the stream is opened again with it; the stores already there
// are told `resync` once the new stream says hello, since a message may have gone by while it changed. Topics are only
// added while any store listens, so moving between screens does not reopen the stream; when the last store goes, it
// closes.

import { openLive as defaultOpenLive, type LiveConnection, type LiveHandler, type LiveTopic } from "../api/live";
import type { LiveEvents } from "../api/types/pipeline";

export type LiveState = "open" | "closed";
export type Opener = (topics: LiveTopic[], onEvent: LiveHandler, onState: (s: LiveState) => void) => LiveConnection;

interface Listener {
  topics: Set<LiveTopic>;
  onEvent: LiveHandler;
  onState: (s: LiveState) => void;
  /** Joined while the stream was being opened again for someone else: told `resync` at the new stream's hello. */
  resync: boolean;
}

const ALWAYS = new Set<keyof LiveEvents>(["hello", "resync"]);

export function createLiveHub(open: Opener = (topics, onEvent, onState) => defaultOpenLive(onEvent, onState, undefined, topics)) {
  const listeners = new Set<Listener>();
  let connection: LiveConnection | null = null;
  let carried = new Set<LiveTopic>();
  let state: LiveState | null = null;
  let due = false;

  const onEvent: LiveHandler = (event, data) => {
    for (const l of [...listeners]) {
      if (event === "hello" && l.resync) {
        l.resync = false;
        l.onEvent("resync", {} as LiveEvents["resync"]);
      }
      if (ALWAYS.has(event) || l.topics.has(event as LiveTopic)) l.onEvent(event, data);
    }
  };

  const onState = (s: LiveState) => {
    state = s;
    for (const l of [...listeners]) l.onState(s);
  };

  /** Opens, widens or closes the stream to suit who listens. Run after the current task, so the stores that start
   * together (the shell's and a screen's) share the first stream. */
  function settle() {
    due = false;
    if (listeners.size === 0) {
      connection?.close();
      connection = null;
      carried = new Set();
      state = null;
      return;
    }
    const wanted = new Set(carried);
    for (const l of listeners) l.topics.forEach((t) => wanted.add(t));
    if (connection && wanted.size === carried.size) return;
    const had = connection;
    if (had) for (const l of listeners) if ([...l.topics].every((t) => carried.has(t))) l.resync = true;
    carried = wanted;
    had?.close();
    connection = open([...wanted], onEvent, onState);
  }

  function schedule() {
    if (due) return;
    due = true;
    queueMicrotask(settle);
  }

  return {
    /** Hands this listener the messages of its topics (and every `hello` and `resync`) until the returned close. */
    subscribe(topics: readonly LiveTopic[], handler: LiveHandler, stateHandler: (s: LiveState) => void = () => {}): LiveConnection {
      const l: Listener = { topics: new Set(topics), onEvent: handler, onState: stateHandler, resync: false };
      listeners.add(l);
      if (state) stateHandler(state);
      schedule();
      return {
        close: () => {
          listeners.delete(l);
          schedule();
        },
      };
    },
    /** For tests: the topics the stream carries now. */
    carried: () => [...carried].sort(),
  };
}

export type LiveHub = ReturnType<typeof createLiveHub>;

/** The page's stream. */
export const liveHub = createLiveHub();
