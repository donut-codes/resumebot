// For tests only: a live hub whose streams are fakes the test drives, and the wrapper that hands it to the hooks.
import { act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { vi } from "vitest";
import type { LiveHandler, LiveTopic } from "../api/live";
import { createLiveHub, type LiveHub, type LiveState } from "../state/live";
import { LiveHubProvider } from "./useLiveChange";

export function fakeLiveHub() {
  const streams: { topics: LiveTopic[]; push: LiveHandler; state: (s: LiveState) => void; close: ReturnType<typeof vi.fn> }[] = [];
  const hub = createLiveHub((topics, push, state) => {
    const close = vi.fn();
    streams.push({ topics: [...topics].sort(), push, state, close });
    return { close };
  });
  const latest = () => {
    const stream = streams[streams.length - 1];
    if (!stream) throw new Error("No stream is open yet: await flushHub() after mounting.");
    return stream;
  };
  return {
    hub,
    streams,
    /** Says a message on the newest stream. */
    push: ((event, data) => latest().push(event, data)) as LiveHandler,
    /** Says the newest stream opened or dropped. */
    setState: (state: LiveState) => latest().state(state),
  };
}

/** The wrapper for `renderHook`/`render`: the hooks listen on this hub. */
export function liveWrapper(hub: LiveHub) {
  return ({ children }: { children: ReactNode }) => createElement(LiveHubProvider, { hub }, children);
}

/** Lets the hub open the stream a mounted hook asked for (it opens after the current task). */
export const flushHub = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
