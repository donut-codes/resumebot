// What every screen asks the app instead of being handed it: where in the app you are, and whether something elsewhere
// changed. App provides them once; a screen reads them with a hook, so no screen's props carry the route or a refresh
// counter down through its children. (What Notifications holds is state/inboxStore.ts.)
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { parseRoute, serializeRoute, type Route } from "./route";
import { confirmLeave } from "./unsavedGuard";

// --- something elsewhere changed ------------------------------------------------------------------------------------------

const RefreshContext = createContext(0);

/** Clicking the tab you are on loads that screen again: the number changes, and every load that lists it runs again. */
export function RefreshProvider({ value, children }: { value: number; children: ReactNode }) {
  return <RefreshContext.Provider value={value}>{children}</RefreshContext.Provider>;
}

export function useRefreshKey(): number {
  return useContext(RefreshContext);
}

// --- where you are -------------------------------------------------------------------------------------------------------

export interface RouteApi {
  route: Route;
  /** The address as it stands, "#/agents/how". */
  hash: string;
  /** Writes a route to the address (a new history entry, so Back returns to where you were). A screen with edits that are
   * not saved asks first; if the person stays, nothing happens. */
  go: (next: Route) => void;
}

const RouteContext = createContext<RouteApi | null>(null);

/** Where you are lives in the address, not in state: the tab, the agent and the process tab open are all read from the hash
 * (lib/route.ts), so a refresh keeps your place and Back goes back. This holds the hash and follows it. */
export function RouteProvider({ children }: { children: ReactNode }) {
  const [hash, setHash] = useState(() => window.location.hash);
  const route = useMemo(() => parseRoute(hash), [hash]);

  const go = useCallback((next: Route) => {
    const h = serializeRoute(next);
    if (h !== window.location.hash) {
      if (!confirmLeave()) return; // a screen has edits that are not saved: the person decides first
      window.history.pushState(null, "", h);
    }
    setHash(h);
  }, []);

  // Back/forward, pasted links and in-page links (the Pipeline's "See where it is") change the hash.
  const shown = useRef(hash);
  shown.current = hash;
  useEffect(() => {
    function onHash() {
      const next = window.location.hash;
      if (next !== shown.current && !confirmLeave()) {
        // Stay: the address goes back to the page that is still on show.
        window.history.pushState(null, "", shown.current || "#/");
        return;
      }
      setHash(next);
    }
    window.addEventListener("hashchange", onHash);
    window.addEventListener("popstate", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("popstate", onHash);
    };
  }, []);

  const value = useMemo(() => ({ route, hash, go }), [route, hash, go]);
  return <RouteContext.Provider value={value}>{children}</RouteContext.Provider>;
}

export function useRoute(): RouteApi {
  const api = useContext(RouteContext);
  if (!api) throw new Error("useRoute needs a RouteProvider above it");
  return api;
}
