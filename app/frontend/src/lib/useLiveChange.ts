import { createContext, createElement, useContext, useEffect, useRef, type ReactNode } from "react";
import type { LiveArea, LiveEvents } from "../api/types/pipeline";
import { liveHub, type LiveHub } from "../state/live";

// The hub the hooks listen on: the page's one stream by default, a test's own through the provider.
const HubContext = createContext<LiveHub>(liveHub);

/** Hands the hooks below a live hub of their own (tests). */
export function LiveHubProvider({ hub, children }: { hub: LiveHub; children?: ReactNode }) {
  return createElement(HubContext.Provider, { value: hub }, children);
}

interface LiveChangeOptions {
  /** Only a `changed` that names this id, or names none, counts. */
  id?: string;
  /** `false` (or no area) leaves the hook idle. */
  enabled?: boolean;
}

/** Calls `onChange` when the server says rows behind `area` changed, when the stream opens again after it dropped (the
 * catch-up: messages may have gone by) and on `resync`. It asks nothing on a timer. A stream that was already open when
 * the screen mounted is not an edge: the screen's own first load is the catch-up. `onChange` is read fresh, so an inline
 * closure is fine. */
export function useLiveChange(area: LiveArea | undefined, onChange: () => void, options: LiveChangeOptions = {}): void {
  const hub = useContext(HubContext);
  const changeRef = useRef(onChange);
  const idRef = useRef(options.id);
  useEffect(() => {
    changeRef.current = onChange;
    idRef.current = options.id;
  });
  const enabled = options.enabled !== false && area !== undefined;
  useEffect(() => {
    if (!enabled) return undefined;
    let subscribing = true;
    const connection = hub.subscribe(
      ["changed"],
      (event, data) => {
        if (event === "resync") {
          changeRef.current();
          return;
        }
        if (event !== "changed") return;
        const said = data as LiveEvents["changed"];
        if (said.area !== area) return;
        const id = idRef.current;
        if (id !== undefined && said.ids && said.ids.length > 0 && !said.ids.includes(id)) return;
        changeRef.current();
      },
      (state) => {
        // The hub tells a new listener the state it is in at once (while `subscribing`): that is not an edge.
        if (state === "open" && !subscribing) changeRef.current();
      },
    );
    subscribing = false;
    return () => connection.close();
  }, [hub, area, enabled]);
}
