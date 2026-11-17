// The machinery every screen's store stands on (the screens rebuild spec, "The data layer"), lifted from the
// Pipeline's: a store made once with an immutable state, a hook that reads a slice of it so a component redraws only
// when that slice changes, an optimistic change that puts the old value back when the server refuses, the live stream
// by topic, and a start that is counted, so the store reads and listens while any screen shows it. A store knows nothing
// of how anything looks; screens read from it and change things only through its actions.

import { createContext, createElement, useContext, useRef, useSyncExternalStore, type ReactNode } from "react";
import type { LiveConnection, LiveHandler, LiveTopic } from "../api/live";
import { liveHub, type LiveState } from "./live";

/** A state, its readers, and the one way to change it. */
export function createState<S extends object>(initial: S) {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
    /** Changes part of the state and tells every reader. */
    set(patch: Partial<S> | ((s: S) => Partial<S>)) {
      const next = typeof patch === "function" ? patch(state) : patch;
      state = { ...state, ...next };
      listeners.forEach((l) => l());
    },
  };
}

export interface Readable<S> {
  getState: () => S;
  subscribe: (l: () => void) => () => void;
}

/** What opens a store's stream: the page's shared one by default (`liveHub`), a test's own otherwise. */
export type OpenLive = (onEvent: LiveHandler, onState: (s: LiveState) => void) => LiveConnection;

/** The page's stream, for these topics. */
export function topicsOf(topics: readonly LiveTopic[]): OpenLive {
  return (onEvent, onState) => liveHub.subscribe(topics, onEvent, onState);
}

/** A start that is counted: `begin` runs when the first screen starts the store, and what it returns runs when the last
 * one stops. */
export function counted(begin: () => () => void) {
  let users = 0;
  let end: (() => void) | null = null;
  return {
    start(): () => void {
      users += 1;
      if (users === 1) end = begin();
      let stopped = false;
      return () => {
        if (stopped) return;
        stopped = true;
        users -= 1;
        if (users === 0) {
          end?.();
          end = null;
        }
      };
    },
    users: () => users,
  };
}

/** A change shown at once: `apply` changes the state, `call` asks the server, `done` takes its answer, and `undo` puts
 * the old value back (and says so) when it refuses. True when the server took it. */
export async function optimistic<T>(
  apply: () => void,
  call: () => Promise<T>,
  undo: (err: unknown) => void,
  done: (answer: T) => void = () => {},
): Promise<boolean> {
  apply();
  let answer: T;
  try {
    answer = await call();
  } catch (err) {
    undo(err);
    return false;
  }
  done(answer);
  return true;
}

/** Reads part of a store's state; the component redraws only when that part changes. */
export function useSelect<S, T>(store: Readable<S>, select: (s: S) => T): T {
  const last = useRef<{ state: S; value: T } | null>(null);
  return useSyncExternalStore(store.subscribe, () => {
    const s = store.getState();
    if (last.current && last.current.state === s) return last.current.value;
    const value = select(s);
    if (last.current && Object.is(last.current.value, value)) {
      last.current = { state: s, value: last.current.value };
      return last.current.value;
    }
    last.current = { state: s, value };
    return value;
  });
}

/** A store handed down through the page: its provider, the hook that finds it, and the hook that reads a slice of it. */
export function storeContext<St extends Readable<S>, S>(name: string) {
  const Context = createContext<St | null>(null);
  function Provider({ store, children }: { store: St; children: ReactNode }) {
    return createElement(Context.Provider, { value: store }, children);
  }
  function useStore(): St {
    const store = useContext(Context);
    if (!store) throw new Error(`${name} needs its provider above it`);
    return store;
  }
  function useSlice<T>(select: (s: S) => T): T {
    return useSelect(useStore(), select);
  }
  return { Provider, useStore, useSlice };
}
